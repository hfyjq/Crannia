"""crannia 的终端界面（全屏）。

为什么是全屏而不是滚动式：这个界面要固定顶部信息栏和底部输入区，
而这两件事在滚动式布局里做不到 —— 往上翻两屏，模型名和工作目录就滚没了，
回答"我现在在哪个仓库、用哪个模型"得先翻回去。

代价是退出后屏幕清空、终端自带的搜索与框选失效。这是明确取舍：
这个界面要的是"随时能确认状态"，不是"事后翻记录"。

另一个明文决定：**不做逐字输出**。后端有 `EVT_DELTA`（流式增量），
桌面端拿它做打字机效果；这里刻意不接 —— 每片一画就是每秒几十次全文重排，
而终端里"整段落定"才是可读的单位（能翻、能搜、能选中复制的那一行，
从出现的那一刻起就该是定稿）。要接的话，删掉事件分流里 DELTA 的忽略
分支、加回预览块的生命周期，并在 `say` 到达时把预览换掉 ——
但先想清楚值不值。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from . import approval as A
from . import clipboard
from . import events as E
from .backend import Backend, BackendError
from .client import EventStream
from .commands import model_command, parse_command, short_path, targets_full_access
from .i18n import t
from .ui import (ApprovalPanel, BtwPanel, Composer, HeaderBar, ModelEditPanel,
                 StatusBar, StatusLine, SuggestBar, TaskDetail, ToolCard,
                 Transcript, UpdateHint, retry_label, services_label)

def _fmt_pct(v: float) -> str:
    """百分比：保留一位小数，整数就省掉 `.0`。

    后端给的 `pct` 已经是 `round(x, 1)`，一位小数不许再四舍五入 —— 窗口
    一百万 token 时，一千三和一千四都落在 1% 里，取整之后读数整场不动。
    """
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _fmt_task_ms(ms) -> str:
    """任务用时：不足一分钟给秒，之后 `1m21s`（GUI 的 `_elapsedLabel` 同形）。"""
    try:
        n = int(ms or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 60000:
        return f"{round(n / 1000)}s"
    return f"{n // 60000}m{round((n % 60000) / 1000)}s"


def _fmt_duration(seconds: float) -> str:
    """一轮的用时：`1h1m1s` / `2m3s` / `45s`（与最终回复下面那行同形）。"""
    n = max(0, int(round(seconds)))
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%dm%ds" % (h, m, s)
    if m:
        return "%dm%ds" % (m, s)
    return "%ds" % s


#: 补全里不出现的命令（切会话 / 回退历史）：TUI 不重画转写区，列出来等于
#: 承诺一件做不到的事。手打也会被 `commands.parse_command` 拦下。
_SUGGEST_HIDDEN = ("/resume", "/rewind")


def _recap_body(text: str) -> str:
    """`/recap` 的回复带着后端那句头（`#recap：` / `#recap-`），剥掉再显示。"""
    body = (text or "").strip()
    for prefix in ("#recap：", "#recap-"):
        if body.startswith(prefix):
            return body[len(prefix):].strip()
    return body


#: 一轮的上限。实际取决于模型和工具链，这里只防"永远等下去"。
TURN_TIMEOUT = 600

#: 窗口标题。默认标题是 cmd.exe 的完整路径 —— 任务栏上那一排分不出谁是谁。
_WINDOW_TITLE = "Crannia"


def _set_console_title(title: str) -> str | None:
    """把窗口标题设成 *title*，返回原来那个（读不到就返回 None）。

    Windows 走 `SetConsoleTitleW`：控制台 API，不会和 Textual 正在写的转义流
    抢同一个 stdout（OSC 序列插进半帧中间会把画面弄花）。其它平台用 OSC 0，
    那是那边唯一的标准做法，代价是读不回旧值。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            buf = ctypes.create_unicode_buffer(512)
            k32.GetConsoleTitleW(buf, 512)
            old = buf.value
            k32.SetConsoleTitleW(title)
            return old
        except (AttributeError, OSError):
            pass
    try:
        sys.__stdout__.write(f"\x1b]0;{title}\x07")
        sys.__stdout__.flush()
    except (AttributeError, OSError, ValueError):
        pass
    return None


#: 后端起来的这段时间里，屏幕上要有个明确的说法。
#: 十几秒的空白会被当成"启动失败"，而它其实只是在装 bus 和技能。
_BOOTING = "正在初始化…"

class CranniaApp(App):
    CSS_PATH = "app.tcss"
    TITLE = _WINDOW_TITLE
    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("ctrl+l", "clear", "清屏"),
        ("escape", "stop_turn", "中断"),
    ]

    def __init__(self, backend: Backend, launch_cwd: str = "",
                 check_updates: bool = False) -> None:
        super().__init__()
        self.backend = backend
        #: 启动后查一次新版本（只在真实入口打开；界面测试不联网）。
        self._check_updates = bool(check_updates)
        #: 呼出 TUI 时所在的目录 —— 抬头那行路径显示的就是它。
        #: 后端报的 `project_root` 是代码根，不是"用户站在哪"。
        self._launch_cwd = str(launch_cwd or "").strip()
        self.stream: EventStream | None = None
        #: 这一轮有没有往对话区写过东西。`/api/chat` 的返回值与事件流里的
        #: `task.end.response` 常常是同一段文本，靠它决定要不要兜底。
        self._wrote_this_turn = False
        self._busy = False
        #: 本轮的起点（monotonic）—— 最终回复下面那行 `done · 用时 · 时刻` 用它。
        self._turn_t0 = 0.0
        #: `/btw` 窗口开着没有；审批来了先压住，等窗口关掉再弹。
        self._btw_active = False
        self._approval_deferred = False
        #: 窗关早了、那一问还在跑：它的回答要带【btw】落到转写区。
        self._btw_orphan = False
        #: 这一轮是 `/recap`：回答按「recap/回顾：」那一行渲染。
        self._recap_pending = False
        #: 本轮已经打过「记忆已更新」那一行了（存 `_turn_t0`）。
        #: 抽取与巩固两条事件说的是同一件事，叠两行像出了两次问题。
        self._memory_note_seen = 0.0
        #: 状态栏左半的档位。`show(busy=...)` 每轮都要重画右侧提示，
        #: 档位不能跟着清空 —— 它们不是这轮的属性。
        self._mode = ""
        #: 模式的**键**（`manual` / `auto` / `plan` / `full_access`）。
        #: 显示用中文名（`_mode`），判定必须用键 —— 英文界面里名字是英文。
        self._mode_key = ""
        self._effort = ""
        #: 等安全提示回答的那条 `/mode …`（非空 = 面板正占着那个槽）。
        self._safety_cmd = ""
        #: 本次运行里选过「开启，且不再询问」。不落盘：它是这一次运行的让步，
        #: 重启之后重新问一次（完全权限的确认不该变成永久静音）。
        self._safety_skip = False
        #: 底栏那格后台服务读数：文案 + 要不要染黄（关着 = 黄）。
        #: 由 `/api/status` 的 `background_services` / `channels_running` /
        #: `cron_jobs` 算出来，不在这边猜。
        self._services = ""
        self._services_warn = False
        #: 接管开着没有。真相源在后端（`/api/computer-use/status`），
        #: 这里只是每 5 秒同步一次的影子 —— 不许拿它当判据。
        self._takeover = False
        #: 顶栏那行模型标签（"model（配置名）"）—— 轮末刷新时比对用。
        self._model_label = ""
        #: 顶栏那行档位 —— 与上面同理，变了才重画抬头。
        self._header_effort = ""
        #: call_id → (卡片, 调起时刻)。工具可以并发，所以按 id 配对而不按顺序；
        #: 配不上的由 `_finish_turn` 统一标成"没收到结束事件"。
        self._cards: dict[str, tuple[ToolCard, float]] = {}
        #: 当前挂着的那次审批。None = 没有；面板本身常驻（藏着的）。
        self._approval: A.Prompt | None = None
        #: 斜杠补全。目录来自后端 `/api/commands` —— 成员与说明都在后端，
        #: 这里抄一份就是两套真相源，加一条命令时两边漂。
        self._catalog: list[dict] = []
        self._matches: list[dict] = []
        self._match_i = 0
        #: 命令表窗口的顶行。选择越过上下边界时只滚一格，不整屏跳。
        self._suggest_top = 0
        #: `/new` 不立刻发请求（发一句占位文本会变成模型要回答的话）——
        #: 记一个标志，下一句真话用 `new=true` 开新段。
        self._pending_new = False
        #: 上下文读数（`context.state` / 压缩结果）。None = 还没读到。
        #: 存浮点：后端给的是 `round(x, 1)`，取整会让读数在 1M 窗口下
        #: 整场停在同一个数字上。
        self._ctx_pct: float | None = None
        self._ctx_thr = 85.0
        #: 已进预警区、还没到阈值（后端 `armed`，与压缩那条判据同源）。
        self._ctx_armed = False
        #: 插话那句话：client_msg_id -> (部件, 原文)。agent 读到要改回正常色。
        self._steer_lines: dict[str, tuple[Static, str]] = {}
        #: 贴进输入框的图：`{"token": "【图片1】", "data_url": "data:…"}`。
        #: 提交时按 token 在正文里的出现与否取用 —— 用户把 `【图片1】` 删了，
        #: 那张图也就不发了（见 `_submit_text`）。
        self._images: list[dict] = []
        #: 跑着的时候发出去、还没轮到它的那几句（黄字），按发的顺序。
        #: `task.start` 一到就转正最早的那条 —— 它才是刚开始跑的那一轮。
        self._pending_lines: list[str] = []
        #: 后台任务 / 子代理（`GET /api/tasks` 的现拉结果）。
        self._tasks: dict = {}
        #: 底栏导航态：`""` / `"pills"` / `"detail"`，以及选中的是哪一格。
        self._nav = ""
        self._pill = ""
        self._detail_top = 0
        self._task_polling = False
        #: 事件流断没断。重复的同一状态不重画（重连成功后每次心跳都会
        #: 报一次"connected"吗 —— 不会，但保险起见按变化画）。
        self._stream_down = False
        #: 进这个界面之前的窗口标题，`on_unmount` 时还回去。
        self._prev_title: str | None = None
        #: 挂起等审批的卡片：call_id → 卡片。审批的轮次会正常结束（`task.end`），
        #: 所以它们不能留在 `_cards` 里等"没收到结束事件"那道收尾 ——
        #: 结算的 `tool.end` 到了就把它从黄点原地改成最终结局。
        self._pending_cards: dict[str, ToolCard] = {}

    # -- 布局 -----------------------------------------------------

    def compose(self) -> ComposeResult:
        # 抬头不在这里 —— 它是对话区的第一块（见 `on_mount`），会随内容滚走。
        yield Transcript(id="transcript")
        yield StatusLine(id="status")
        yield SuggestBar("", id="suggest")
        yield TaskDetail(id="task-detail")
        yield ModelEditPanel(id="model-edit")
        yield ApprovalPanel(id="approval")
        yield UpdateHint(id="update-hint")
        yield BtwPanel(id="btw-panel")
        yield Composer(id="composer", placeholder=t("我可以怎么帮助您？"))
        yield StatusBar(id="bar")

    def on_mount(self) -> None:
        # 窗口标题：Textual 的 `TITLE` 只喂它自带的 Header 部件，不会写终端 ——
        # 所以这条得自己发。原值留着，退出时还回去（在别人的标签页里跑也干净）。
        self._prev_title = _set_console_title(_WINDOW_TITLE)
        self.set_interval(0.5, self._tick)
        self.set_interval(3.0, self._poll_tasks)
        self.set_interval(0.12, self._spin)
        self.query_one("#composer", Composer).disabled = True
        self.query_one("#status", StatusLine).update(
            Text("  " + t(_BOOTING), style="dim"))
        threading.Thread(target=self._boot, daemon=True).start()

    # -- 启动 -----------------------------------------------------

    def _boot(self) -> None:
        """在后台线程里起后端。

        不能直接在 `on_mount` 里调：`wait_ready()` 会阻塞十几秒，那期间界面
        一帧都不刷 —— 全屏应用卡住比命令行卡住更吓人，因为连光标都不动。
        """
        catalog: list[dict] = []
        try:
            self.backend.start()
            self.backend.wait_ready()
            self.backend.start_framework()
            status = self.backend.get_json("/api/status")
            cat = self.backend.get_json("/api/commands")
            catalog = [c for c in (cat or {}).get("commands") or []
                       if isinstance(c, dict) and c.get("cmd")]
        except Exception as exc:                             # noqa: BLE001
            self.call_from_thread(self._boot_failed, exc)
            return
        self.call_from_thread(self._boot_ready, status or {}, catalog)

    def _boot_failed(self, exc: BaseException) -> None:
        self.query_one(Transcript).add(
            Text(t("后端未就绪：%s") % f"{type(exc).__name__}\n{exc}",
                 style="red"))
        self.query_one("#status", StatusLine).update(
            Text("  " + t("后端未就绪，输入不会生效"), style="red"))

    def _boot_ready(self, status: dict, catalog: list[dict]) -> None:
        model = str(status.get("model") or "").strip()
        name = str(status.get("model_config_name") or "").strip()
        cwd = self._launch_cwd or str(status.get("project_root") or "").strip()
        # 配置名只是给用户区分"哪一套 key"的，和模型名重复时纯属噪声。
        label = f"{model}（{name}）" if name and name.lower() not in model.lower() else model
        self._model_label = label
        # 会话已冻结的那一档优先：`mode_name` / `effort_name` 是设置里写着的
        #   默认值，用户 `/mode` `/effort` 换过之后它不会变（「标签在说谎」）。
        self._mode = str(status.get("mode_session_name")
                         or status.get("mode_name") or "").strip()
        self._mode_key = str(status.get("mode_session")
                             or status.get("mode_configured") or "")
        self._effort = str(status.get("effort_session_name")
                           or status.get("effort_name") or "").strip()
        self.query_one(HeaderBar).show(
            title="Crannia",
            version=str(status.get("version") or "").strip(),
            model=label,
            effort=self._effort,
            cwd=short_path(cwd, max(20, self.size.width - 8)) if cwd else "",
        )
        self._catalog = catalog
        self._statusbar(busy=False)
        composer = self.query_one("#composer", Composer)
        composer.disabled = False
        composer.focus()
        self.query_one("#status", StatusLine).idle()

        self.stream = EventStream(self.backend.base, self.backend.token,
                                  self._on_event_threadsafe,
                                  self._on_stream_state,
                                  session=str(status.get("session") or ""))
        self.stream.start()

        #: ★ 接入一个**已经在跑**的后端时，挂着的审批不会再来一条事件
        #:   （SSE 从 last_seq=0 起，不回放历史）。补读一次；与事件那条路
        #:   共用 `_approval_requested`，靠 `request_id` 去重。
        self._approval_requested()

        if self._check_updates:
            threading.Thread(target=self._check_update, daemon=True).start()

    def _check_update(self) -> None:
        """后台自动更新一次：查、下、装都在这里，装好才出一行提示。

        失败不吵（查不到、已是最新、随桌面端安装都不出声）；只有
        「有新版本但没装成」才说一句，因为那时用户可以手工重试。
        """
        from .paths import crannia_home, update_root
        from .update import auto_update, should_auto_update

        try:
            if not should_auto_update(home=crannia_home(), root=update_root()):
                return
            info = auto_update(crannia_home(), install_root=update_root())
        except Exception:  # noqa: BLE001 - 提示查不到就不提示
            return
        version = str(info.get("version") or "")
        if info.get("ready"):
            message = t("新版本已下载，重启应用以更新")
        elif version and info.get("error"):
            message = t("新版本 %s 下载失败 · 运行 crannia update 重试") % version
        else:
            return
        self.call_from_thread(self._show_update_hint, message)

    def _show_update_hint(self, message: str) -> None:
        try:
            self.query_one("#update-hint", UpdateHint).show(message)
        except Exception:  # noqa: BLE001 - 界面正在退出
            return

    # -- 事件 -----------------------------------------------------

    def _on_event_threadsafe(self, ev: E.Event) -> None:
        """SSE 回调跑在事件线程里，而界面只能在主线程动。"""
        self.call_from_thread(self._on_event, ev)

    def _on_stream_state(self, state: str) -> None:
        """事件流断了要出声：这是"静默"的那种失败 —— 回复照常由 POST 带回，
        只有步骤流凭空少掉，界面上看就是"模型没干活"。"""
        self.call_from_thread(self._apply_stream_state, state)

    def _apply_stream_state(self, state: str) -> None:
        down = state != "connected"
        if down == self._stream_down:
            return                          # 重复的同一状态，不重画
        self._stream_down = down
        status = self.query_one(StatusLine)
        if down:
            status.fault(t("步骤流已断开，正在重连..."), tag="stream")
        else:
            status.clear_fault("stream")

    def _on_event(self, ev: E.Event) -> None:
        transcript = self.query_one(Transcript)

        if ev.type == E.TASK_START:
            self._busy = True
            self._wrote_this_turn = False
            self._turn_t0 = time.monotonic()
            # 轮到排队的那句了：黄字转回正常。
            self._promote_pending()
            self._statusbar(busy=True)
            self.query_one(StatusLine).begin()
            return

        if ev.type == E.TURN_START:
            self._wrote_this_turn = False
            return

        if ev.type == E.TURN_END:
            # 本轮输出 token（不是会话累计 —— 两个数差 99%，口径在
            # StatusLine 的 docstring 里）。横幅上那格等的就是它。
            self.query_one(StatusLine).set_tokens(ev.i("output_tokens"))
            return

        if ev.type == E.TOOL_START:
            self._tool_start(ev)
            return

        if ev.type == E.TOOL_END:
            self._tool_end(ev)
            return

        if ev.type == E.APPROVAL_REQUEST:
            self._approval_requested()
            return

        if ev.type == E.APPROVAL_RESOLVED:
            # 在别处答掉了（桌面端 / 另一个窗口）：把面板撤下来，别让它一直挂着。
            # 这一轮多半已经结束了，TASK_END 会做剩下的收尾。
            self._approval_deferred = False
            if self._approval is not None:
                self._approval_dismiss()
            return

        if ev.type == E.SAY:
            text = ev.s("text").strip()
            if text:
                transcript.add_agent_text(text)
                self._wrote_this_turn = True
            return

        if ev.type == E.RECAP:
            # 回到一段放了一阵的对话：单独一行，别和正文混在一起。
            text = ev.s("recap").strip()
            if text:
                self._add_recap(text)
            return

        if ev.type in (E.MEMORY_SAVED, E.MEMORY_ORGANIZED):
            # 后台记忆动过（写了几条 / 巩固改了几条）—— 同一句回执，一轮最多一行。
            self._add_memory_note()
            return

        if ev.type == E.ICE_CANARY:
            # 出站金丝雀被拦下：安全事实，红标一行（与「自动审查拦截」同一种标）。
            self._add_ice_canary()
            return

        if ev.type == E.ERROR:
            msg = ev.s("message") or ev.s("error") or t("未知错误")
            stage = ev.s("stage")
            line = Text()
            line.append("✗ ", style="red")
            if stage:
                line.append(f"[{stage}] ", style="dim")
            line.append(msg.splitlines()[0], style="red")
            transcript.add(line)
            return

        if ev.type == E.CONTEXT_STATE:
            pct = ev.data.get("pct")
            thr = ev.data.get("threshold_pct")
            if isinstance(pct, (int, float)):
                self._ctx_pct = float(pct)
            if isinstance(thr, (int, float)):
                self._ctx_thr = float(thr)
            self._ctx_armed = bool(ev.data.get("armed"))
            self._statusbar(busy=self._busy)
            return

        if ev.type == E.COMPACT_PROGRESS:
            self._compact_progress(ev)
            return

        if ev.type == E.LLM_RETRY:
            self._llm_retry(ev)
            return

        if ev.type == E.LLM_RETRY_OK:
            self.query_one(StatusLine).clear_fault("llm")
            return

        if ev.type == E.STEER_DELIVERED:
            # agent 读了这句话：黄字转回正常（它已经进上下文，不再"待读"）。
            for mid in ev.data.get("items") or []:
                self._mark_steer(str(mid))
            return

        if ev.type == E.TASK_END:
            self._finish_turn(ev)

    def _finish_turn(self, ev: E.Event) -> None:
        self._busy = False
        self._statusbar(busy=False)
        self.query_one("#status", StatusLine).idle()
        # 审批面板不在这里撤：后端挂起等拍板时轮次本来就会正常结束
        # （task.end、response 为空），答完后另起一轮。撤掉它，面板会在
        # 弹出与 task.end 之间被拆掉。"在别处答掉了"由 APPROVAL_RESOLVED 撤。
        # 一轮结束了还挂着的卡片：它们的结束事件不会再来（后端那一步抛了、
        # 或者这一轮被中断）。留着"运行中"的圆点会让人一直等。
        for card, _t0 in self._cards.values():
            card.orphan()
        self._cards.clear()
        reply = ev.s("response").strip()
        if self._btw_active:
            # 题外话的回答进框里；顶行换成完成读数，转写区一个字都不加。
            self.query_one("#btw-panel", BtwPanel).finish(reply)
            self.query_one(StatusLine).show_done(self._done_label())
        elif self._btw_orphan:
            # 窗关早了：那一问的回答带【btw】落到转写区，不冒充主线回复。
            self._btw_orphan = False
            self._add_btw_note(reply)
        elif self._recap_pending:
            self._recap_pending = False
            self._add_recap(_recap_body(reply))
        else:
            if not self._wrote_this_turn and reply:
                self.query_one(Transcript).add_agent_text(reply)
                self._wrote_this_turn = True
            # 最终回复下面那行：`done · 用时 · 完成时刻`。只在一轮真的说过话时加，
            # 而且不是每个步骤一行 —— 中断 / 挂起等审批那两种没有"完成"可言。
            if (self._wrote_this_turn and not ev.b("cancelled")
                    and not ev.b("awaiting_approval")):
                self._add_done_line()
        composer = self.query_one("#composer", Composer)
        if self._approval is None and not composer.disabled:
            # 审批还挂着时不抢焦点 —— 面板正等着拍板。
            composer.focus()
        # 设置只有一份（后端），这边开机读的那次可能早过时了：
        # 用户完全可能在 GUI 里换了模式/模型/effort 再回来。每轮结束
        # 刷新一次 —— 轮次边界是最自然的同步点，也不多花请求。
        threading.Thread(target=self._refresh_status, daemon=True).start()

    # -- 工具 -----------------------------------------------------

    def _tool_start(self, ev: E.Event) -> None:
        """调起时先挂一张"运行中"的卡。

        不等到结束再一次性输出：工具跑十几秒很常见，那期间屏幕上什么都不动，
        和卡死没区别。卡片先出来、跑完再改，等于把等待可视化。
        """
        card = ToolCard(activity=ev.s("activity"),
                        arg=ev.s("sub_hint") or ev.s("arg_hint"),
                        name=ev.s("name"), classes="tool")
        self.query_one(Transcript).add_card(card)
        call_id = ev.s("call_id")
        if call_id:
            self._cards[call_id] = (card, time.monotonic())

    def _tool_end(self, ev: E.Event) -> None:
        call_id = ev.s("call_id")
        outcome = ev.s("outcome") or "completed"
        reason = ev.s("reason")
        chars = ev.i("result_chars")
        ms = ev.i("duration_ms")

        # 先认"这张卡是不是早先挂起等审批的那张"：审批回答之后，后端会用
        # 同一个 `call_id` 补发一条结算事件。认出来就原地改，而不是再补一张。
        card = self._pending_cards.pop(call_id, None) if call_id else None
        if card is None:
            item = self._cards.pop(call_id, None) if call_id else None
            if item is None:
                # 没配到调起事件 —— 中途才连上事件流时就是这样。补一张卡，
                # 而不是丢掉：一次真实发生过的工具调用凭空消失，比显示得丑更糟。
                card = ToolCard(activity="", arg=ev.s("sub_hint") or ev.s("arg_hint"),
                                name=ev.s("name"), classes="tool")
                self.query_one(Transcript).add_card(card)
            else:
                card = item[0]

        card.done(outcome=outcome, reason=reason, chars=chars, ms=ms)

        # 挂起等审批：这一轮的 `task.end` 是常态（后端就停在那儿等人拍板），
        # 所以把卡片搬出 `_cards`，免得被轮末当成"没收到结束事件"标掉。
        if outcome == "waiting" and call_id:
            self._pending_cards[call_id] = card

    # -- 审批 -----------------------------------------------------

    def _approval_requested(self) -> None:
        """事件只带 `kind` 和 `request_id`，选项表要再拉一次。

        直接拿事件里的字段画是不行的：正文、选项、默认项都是后端
        `build_payload` 现场生成的（"第 N 次被问起才出现的升级项"就在这一步），
        事件里没有。所以先 GET 再画。

        btw 窗开着时先压住：那个窗占着整屏，弹上来会把它盖掉 ——
        关窗之后再走这一条。
        """
        if self._btw_active:
            self._approval_deferred = True
            return
        # 安全提示也占着输入框那个槽 —— 压到它答完再弹（同一套延后机制）。
        if self._safety_cmd:
            self._approval_deferred = True
            return
        # 审批要占输入框那个槽；底栏详细页先收起来，别叠在一起。
        if self._nav:
            self._close_nav()
        try:
            data = self.backend.get_json("/api/approval")
        except Exception as exc:                             # noqa: BLE001
            self.query_one(Transcript).add(
                Text("✗ " + t("审批详情获取失败：%s") % type(exc).__name__,
                     style="red"))
            return
        prompt = A.Prompt.from_payload((data or {}).get("pending") or {})
        if prompt is None:
            # 已经没有待审批：多半刚在别处答过。静默返回 ——
            # 弹一句"没有待审批"只会让人更困惑。
            return
        #: 启动补读与实时事件可能同时到：同一个 request_id 只画一次。
        if (self._approval is not None
                and getattr(self._approval, "request_id", "")
                == getattr(prompt, "request_id", "")):
            return

        self._approval = prompt
        self.query_one(StatusLine).pause()
        # 输入框整个让位 —— 面板占的就是它的槽。选"我有别的话说"时
        # 后端会再发一个文本态请求，面板自己会变成一行输入，不需要这里切换。
        self.query_one("#composer", Composer).display = False
        self.query_one(ApprovalPanel).show_prompt(prompt)
        # 面板一挂，对话区就被挤矮好几行 —— Transcript 的 `on_resize`
        # 会自己贴底（用户若正在翻历史，`_stuck` 是松的，不拽）。

    def _approval_dismiss(self) -> None:
        """撤掉面板、把输入框还回来。幂等 —— 两处都会调它。"""
        self._approval = None
        self.query_one(StatusLine).resume()
        self.query_one(ApprovalPanel).clear()
        composer = self.query_one("#composer", Composer)
        composer.display = True
        if not composer.disabled:
            composer.focus()

    def on_approval_panel_answer(self, event: ApprovalPanel.Answer) -> None:
        # 安全提示复用同一张面板，但回答归本地 —— 不许发到后端
        #   （它没有对应的 `request_id`，发出去只会被当成一次不存在的审批）。
        if self._safety_cmd:
            self._resolve_safety_notice(event.choice)
            return
        self._submit_approval(choice=event.choice, text=event.text)

    def _submit_approval(self, *, choice: str = "", text: str = "") -> None:
        prompt = self._approval
        if prompt is None:
            return
        self._approval_dismiss()
        payload = {"request_id": prompt.request_id}
        if text:
            payload["text"] = text
        else:
            payload["choice"] = choice
        # 回答之后这一轮会接着跑完，耗时按"一轮"给，不是按"一次点击"。
        threading.Thread(target=self._post_approval, args=(payload,),
                         daemon=True).start()

    def _post_approval(self, payload: dict) -> None:
        try:
            code, body = self.backend.post_json("/api/approval", payload,
                                                timeout=TURN_TIMEOUT)
        except Exception as exc:                             # noqa: BLE001
            self.call_from_thread(
                self._approval_failed,
                t("提交审批失败：%s") % type(exc).__name__)
            return
        if code != 200:
            # 受理失败（已答过 / 对不上）不影响这一轮继续 —— 只是这条回答没人收。
            self.call_from_thread(
                self._approval_failed,
                t("审批未受理（%s）：%s") % (code, body[:160]))

    def _approval_failed(self, message: str) -> None:
        self.query_one(Transcript).add(Text("✗ " + message, style="red"))
        # 这里不动 `_busy`：回答被拒不代表这轮停了，事件流会告诉我们它怎么样了。

    # -- 计时 -----------------------------------------------------

    def _refresh_status(self) -> None:
        """重读 `/api/status`，把档位与顶栏模型贴回现状。

        审批策略不在同步之列：开关在后端，打开时审批请求根本不会来。
        """
        try:
            status = self.backend.get_json("/api/status")
        except Exception:                                # noqa: BLE001
            return
        # 接管状态是另一个端点（唯一真相源在那儿）。读不到就当"没开"——
        #   它只影响底栏那一格，不影响任何授权判定。
        takeover = False
        try:
            cu = self.backend.get_json("/api/computer-use/status") or {}
            takeover = bool(cu.get("enabled"))
        except Exception:                                # noqa: BLE001
            takeover = False
        self.call_from_thread(self._apply_status, status or {}, takeover)

    def _apply_status(self, status: dict, takeover: bool = False) -> None:
        mode = str(status.get("mode_session_name")
                   or status.get("mode_name") or "").strip()
        self._mode_key = str(status.get("mode_session")
                             or status.get("mode_configured") or "")
        effort = str(status.get("effort_session_name")
                     or status.get("effort_name") or "").strip()
        services, warn = services_label(status)
        services_changed = (services, warn) != (self._services,
                                                self._services_warn)
        self._services, self._services_warn = services, warn
        takeover_changed = takeover != self._takeover
        self._takeover = takeover
        if takeover_changed:
            self._set_takeover_title()
        if (mode != self._mode or effort != self._effort or services_changed
                or takeover_changed):
            self._mode, self._effort = mode, effort
            self._statusbar(busy=self._busy)
        # 顶栏的模型与档位也一样：在 GUI 里换了，这里不跟就是一直报旧值。
        model = str(status.get("model") or "").strip()
        name = str(status.get("model_config_name") or "").strip()
        changed = False
        if model and model != self._model_label:
            self._model_label = (f"{model}（{name}）"
                                 if name and name.lower() not in model.lower()
                                 else model)
            changed = True
        if effort != self._header_effort:
            self._header_effort = effort
            changed = True
        if changed:
            # 状态刷新挂在定时回调上，部件不在时让它跳过，不要抛到回调栈里。
            header = self.query_one_optional(HeaderBar)
            if header is not None:
                header.show(title="Crannia",
                            version=str(status.get("version") or "").strip(),
                            model=self._model_label,
                            effort=effort,
                            cwd=self._launch_cwd or str(status.get("project_root") or "").strip())

    def _spin(self) -> None:
        # 拆屏瞬间可能还有一次已入队的回调，那时部件已经没了。放掉。
        if not self.is_running:
            return
        self.query_one("#status", StatusLine).spin()

    def _tick(self) -> None:
        if not self.is_running:
            return
        now = time.monotonic()
        for card, t0 in self._cards.values():
            card.tick(now - t0)

    # -- 中断与插话 -----------------------------------------------

    def action_stop_turn(self) -> None:
        """Esc：btw 窗开着先关窗；否则请求中断正在跑的那一轮。

        审批挂着的时候不响应 —— 那时"停下来"的语义是模糊的（停的是工具
        还是那轮对话？），而后端的中断只保证"下一次调模型之前停下"。
        """
        if self.query_one(ModelEditPanel).is_open:
            self._close_model_edit()
            return
        if self._btw_active:
            self._close_btw()
            return
        if not self._busy or self._approval is not None:
            return
        threading.Thread(target=self._post_stop, daemon=True).start()

    def _post_stop(self) -> None:
        try:
            code, body = self.backend.post_json("/api/stop", {}, timeout=30)
        except Exception as exc:                             # noqa: BLE001
            self.call_from_thread(self._note,
                                  t("中断失败：%s") % type(exc).__name__)
            return
        note = ""
        if code == 200:
            try:
                note = str(json.loads(body).get("note") or "")
            except json.JSONDecodeError:
                note = ""
        self.call_from_thread(
            self._note, note or t("已打断，请告诉Crannia应该怎么做"))

    def _done_label(self) -> str:
        elapsed = time.monotonic() - (self._turn_t0 or time.monotonic())
        return "done · %s · %s" % (_fmt_duration(elapsed),
                                   time.strftime("%H:%M"))

    def _add_done_line(self) -> None:
        """最终回复下面那行：`done · 1h1m1s · 12:11`（完成时刻，不是开始）。"""
        self.query_one(Transcript).add(Text(self._done_label(), style="dim"))

    def _open_btw(self) -> None:
        """打开 `/btw` 窗：转写区整块让位，顶部只剩横幅。"""
        self._btw_active = True
        self.query_one(Transcript).display = False
        panel = self.query_one("#btw-panel", BtwPanel)
        panel.start()
        panel.focus()
        self.query_one("#composer", Composer).disabled = True

    def _close_btw(self) -> None:
        """Esc：关窗、恢复转写区与输入框；压着的审批这时再弹。"""
        if not self._btw_active:
            return
        # 还没答完就关：那一问继续跑，回答之后带【btw】落到转写区。
        if self._busy:
            self._btw_orphan = True
        self._btw_active = False
        self.query_one("#btw-panel", BtwPanel).display = False
        self.query_one(Transcript).display = True
        composer = self.query_one("#composer", Composer)
        composer.disabled = False
        composer.focus()
        if not self._busy:
            self.query_one(StatusLine).idle()
        if self._approval_deferred:
            self._approval_deferred = False
            self._approval_requested()

    def _add_btw_note(self, text: str) -> None:
        """窗关早了之后的回答：前面挂一个蓝色【btw】，不冒充主线那一轮。"""
        line = Text()
        line.append("【btw】", style="bold blue")
        line.append(" " + (text or t("（没有回答）")))
        self.query_one(Transcript).add(line, classes="agent")

    def _add_recap(self, text: str) -> None:
        """回顾单独起一行：`回顾：…`（英文界面 `recap：…`），不混进普通回复。"""
        line = Text()
        line.append(t("回顾："), style="bold cyan")
        line.append((text or "").strip() or t("（没有内容）"))
        self.query_one(Transcript).add(line, classes="note")

    def _add_memory_note(self) -> None:
        """后台记忆动过 —— 单独一行，橙色。

        一轮最多一行：`memory.saved`（写了几条）与 `memory.organized`
        （巩固改了几条）说的是同一件事，各打一行会读成"出了两次问题"。
        """
        if self._memory_note_seen and self._memory_note_seen == self._turn_t0:
            return
        self._memory_note_seen = self._turn_t0
        line = Text()
        line.append(t("【记忆】"), style="#ff9800")
        line.append(t("记忆已更新") + " · " + time.strftime("%H:%M"),
                    style="#ff9800")
        self.query_one(Transcript).add(line, classes="note")

    def _add_ice_canary(self) -> None:
        """出站金丝雀被拦下 —— 红标一行，与「自动审查拦截」同一种标。

        判据：安全事实必须看得见（旧 WebUI 那一份走的是同一条：
        `[ICE] 金丝雀触发 —— 已阻断`）。它是阻断，不是提示，所以点也染红。
        """
        line = Text()
        line.append("● ", style="red")
        line.append(t("【ICE】金丝雀触发-已阻断"), style="red")
        self.query_one(Transcript).add(line, classes="note")

    def _note(self, text: str) -> None:
        self.query_one(Transcript).add(Text(text), classes="note")

    # -- 输入 -----------------------------------------------------

    def on_composer_submitted(self, event: Composer.Submitted) -> None:
        self._submit_text(event.text.strip())

    def _submit_text(self, raw: str) -> None:
        # 只发"正文里还留着占位符"的那些图：删掉 `【图片1】` 就是撤回那张图。
        # 命令（`/…`）不带图 —— 框架命令拿到的是一串参数文本，图在那儿没处放。
        images = ([] if raw.startswith("/")
                  else [a["data_url"] for a in self._images if a["token"] in raw])
        self._images.clear()
        self._close_nav()
        composer = self.query_one("#composer", Composer)
        composer.text = ""
        self._close_suggest()
        if not raw:
            return

        action, payload = parse_command(raw)
        if action == "exit":
            self.exit()
            return
        if action == "cls":
            self.action_clear()
            return
        if action == "new":
            # 不发占位消息：那句会被模型当真回答。记一个标志，
            # 下一句真话用 `new=true` 开新段；旧对话仍在 `/resume` 里。
            self._pending_new = True
            self.action_clear()
            self._note(t("新会话已就绪：下一句话将开一段新对话"))
            return
        if action == "desktop":
            self._note(t("这条命令在桌面端使用：TUI 不重画切换后的对话。"))
            return
        if action == "chat":
            _kind, _ref = model_command(payload)
            if _kind == "set":
                if self._busy:
                    self._note(t("不可使用，上一轮回答尚未完成。"))
                    return
                self._open_model_edit(_ref)
                return
        if action == "chat" and payload.lower().startswith("/btw"):
            # 题外话走一个独立窗口：不进转写区、也不占它的位置。
            # 跑着的时候不开 —— 那一问会被排到后面，窗口会在等别人的活。
            if self._busy:
                self._add_btw_note(t("不可使用，上一轮回答尚未完成。"))
                return
            self._open_btw()
            self._send(payload, new=self._pending_new, images=images,
                       record=False)
            self._pending_new = False
            return
        if action == "chat" and payload.lower().startswith("/recap"):
            self._recap_pending = True
        # 本地的 `/exit` `/cls` `/new` 上面就返回了；走到这里的是要交给后端的
        # 文本（普通消息，或者 `/skill` `/effort` 这类框架命令）。
        #
        # 切「完全权限」要先过一次安全提示（与 GUI 的 `SafetyNotice` 同义）。
        # 只拦发送、不另实现切换 —— 命令本身仍由后端的 `_cmd_mode` 执行，
        # 两条路不许有两套语义。
        if (action == "chat" and targets_full_access(payload)
                and not self._safety_skip and self._mode_key != "full_access"):
            self._safety_cmd = payload
            self._show_safety_notice()
            return
        self._send(payload, new=self._pending_new, images=images)
        self._pending_new = False

    def _show_safety_notice(self) -> None:
        """切「完全权限」前的那一屏 —— 形状与审批面板一致，警告同 GUI。

        默认落在「取消」上（回车不会误开）；数字键选、回车确认，
        与审批表同一套键位。
        """
        from .approval import Option, Prompt

        body = (t("安全提示") + "\n\n"
                + t("您正在将框架运行模式切换至") + " " + t("完全权限")
                + t("，此模式下 agent 将获得最大权限能力。 agent 的行动将不再询问您，"
                    "仅有部分硬性边界生效。 请注意，您仍然是您的生产安全第一负责人。"))
        prompt = Prompt(
            request_id="local-full-access",
            kind="safety",
            body=body,
            options=(Option("1", t("启用")),
                     Option("2", t("启用，且不再询问")),
                     Option("3", t("取消"))),
            default_key="3",
        )
        self.query_one(ApprovalPanel).show_prompt(prompt)

    def _resolve_safety_notice(self, choice: str) -> None:
        """安全提示的回答：1 开 / 2 开且本次不再问 / 3 取消。"""
        cmd = self._safety_cmd
        self._safety_cmd = ""
        self.query_one(ApprovalPanel).clear()
        composer = self.query_one("#composer", Composer)
        composer.display = True
        if not composer.disabled:
            composer.focus()
        if not cmd or choice == "3":
            self._note(t("已取消切换到完全权限"))
            if cmd:
                # 放回输入框 —— 取消不是"把你的命令吃掉"，改完还能再发。
                composer.text = cmd
                composer.move_cursor(composer.document.end)
            return
        if choice == "2":
            self._safety_skip = True
        self._send(cmd, new=self._pending_new, images=[])
        self._pending_new = False
        if self._approval_deferred:
            self._approval_deferred = False
            self._approval_requested()

    # -- 模型配置编辑（`/model set`）-------------------------------

    def _open_model_edit(self, ref: str) -> None:
        """解析条目并弹出编辑面板。

        密钥只从面板收：命令正文会进聊天记录，打错一个字就不再是命令。
        """
        ref = (ref or "").strip()
        if not ref:
            self._note(t("使用 /model set <编号|配置名|id> 修改现有配置。"))
            return
        try:
            payload = self.backend.get_json("/api/model-configs")
        except Exception as exc:                             # noqa: BLE001
            self._note(t("读取模型配置失败：%s") % type(exc).__name__)
            return
        from . import profile

        entries = profile._entries_from_cards(payload)
        hit, why = profile._resolve(entries, ref)
        if hit is None:
            self._note(why or t("没有找到此配置"))
            return
        self.query_one("#composer", Composer).display = False
        self.query_one(ModelEditPanel).show(hit)

    def _close_model_edit(self) -> None:
        self.query_one(ModelEditPanel).clear()
        composer = self.query_one("#composer", Composer)
        composer.display = True
        composer.focus()

    def on_model_edit_panel_submitted(self, event) -> None:
        panel = self.query_one(ModelEditPanel)
        body = {"id": event.entry_id}
        body.update(event.values)
        try:
            code, text = self.backend.post_json(
                "/api/model-configs/update", body)
            data = json.loads(text or "{}")
        except Exception as exc:                             # noqa: BLE001
            panel.fail(type(exc).__name__)
            return
        if code == 200 and isinstance(data, dict) and data.get("ok"):
            panel.finish()
        else:
            panel.fail(str((data or {}).get("error") or t("后端无应答")))

    def on_model_edit_panel_closed(self, event) -> None:
        self._close_model_edit()

    def on_composer_complete(self, event: Composer.Complete) -> None:
        """Tab：选择下移一格（与 ↓ 等价，给不习惯用方向键的人）。"""
        self._move_suggest(1)

    def on_composer_move(self, event: Composer.Move) -> None:
        self._move_suggest(event.delta)

    def on_composer_accept(self, event: Composer.Accept) -> None:
        """回车：把选中的命令填进输入框，光标落在末尾（后面还能接参数）。"""
        if not self._matches:
            return
        cmd = self._matches[self._match_i]["cmd"]
        composer = self.query_one("#composer", Composer)
        # 已经打全了：回车就是发送（否则完整命令要按两次回车才跑）。
        if composer.text.strip() == cmd:
            self._submit_text(cmd)
            return
        composer.text = f"{cmd} "
        composer.move_cursor(composer.document.end)
        self._close_suggest()

    def on_composer_dismiss(self, event: Composer.Dismiss) -> None:
        """Esc：收起命令表，输入框里的字不动（继续在 / 后面打字）。"""
        self._close_suggest()

    def _move_suggest(self, delta: int) -> None:
        if not self._matches:
            return
        n = len(self._matches)
        self._match_i = (self._match_i + delta) % n
        win = SuggestBar.WINDOW
        if self._match_i < self._suggest_top:
            self._suggest_top = self._match_i
        elif self._match_i >= self._suggest_top + win:
            self._suggest_top = self._match_i - win + 1
        self.query_one(SuggestBar).show(self._matches, self._match_i,
                                        self._suggest_top)

    def _close_suggest(self) -> None:
        self._matches = []
        self._match_i = 0
        self._suggest_top = 0
        self.query_one("#composer", Composer).suggest_open = False
        self.query_one(SuggestBar).clear()

    def on_text_area_changed(self, event) -> None:       # noqa: ANN001（TextArea.Changed）
        self._update_suggest()

    def _update_suggest(self) -> None:
        """输入以 `/` 起头时列出匹配的命令。

        只补命令词本身：后面跟了参数（出现空格）就收起 ——
        那时用户在写参数，候选清单只剩噪声。
        """
        composer = self.query_one("#composer", Composer)
        # 不 strip：回车填入后那格尾随空格正是"该收起了"的信号。
        text = composer.text
        bar = self.query_one(SuggestBar)
        if not text.startswith("/") or " " in text or not self._catalog:
            self._close_suggest()
            return
        matches = [c for c in self._catalog
                   if str(c["cmd"]).startswith(text)
                   and str(c["cmd"]) not in _SUGGEST_HIDDEN]
        if not matches:
            self._close_suggest()
            return
        # 换了候选集就回到第一行；只是多打了一个字（还是同一批）就保留选择。
        if [m["cmd"] for m in matches] != [m["cmd"] for m in self._matches]:
            self._match_i = 0
            self._suggest_top = 0
        self._matches = matches
        self._match_i %= len(matches)
        composer.suggest_open = True
        bar.show(self._matches, self._match_i, self._suggest_top)

    # -- 后台任务 / 子代理 ---------------------------------------

    def _poll_tasks(self) -> None:
        """现拉 `/api/tasks`（只读；真值在服务端 jobs 表里）。"""
        if self._task_polling:
            return
        self._task_polling = True

        def work() -> None:
            try:
                data = self.backend.get_json("/api/tasks")
            except Exception:                            # noqa: BLE001
                data = None
            self._task_polling = False
            if isinstance(data, dict):
                self.call_from_thread(self._apply_tasks, data)

        threading.Thread(target=work, daemon=True).start()

    def _apply_tasks(self, data: dict) -> None:
        self._tasks = data
        bg = data.get("background") or []
        sub = data.get("subagents") or []
        composer = self.query_one("#composer", Composer)
        composer.tasks_ready = bool(bg or sub)
        if self._nav and self._pill:
            alive = bg if self._pill == "bg" else sub
            if not alive:
                self._close_nav()
                return
        self._refresh_task_cell()
        if self._nav == "detail":
            self._show_detail()

    def _refresh_task_cell(self) -> None:
        """重画底栏中间那两格（后台 / 子代理）—— 唯一一处。"""
        bg = len(self._tasks.get("background") or [])
        sub = len(self._tasks.get("subagents") or [])
        self.query_one(StatusBar).show_tasks(
            bg=bg, sub=sub, focus=self._pill if self._nav else "")

    def _set_takeover_title(self) -> None:
        """接管开着时，在输入框边框左上角写黄色的「【接管中】」。

        放这里而不是底栏：底栏那三格在窄终端里会被挤掉（80 列时正好贴边），
        而输入框的边框本来就空着 —— 它出现本身就是"正在接管"。
        """
        composer = self.query_one_optional("#composer", Composer)
        if composer is not None:
            composer.border_title = t("【接管中】") if self._takeover else ""

    def on_composer_nav(self, event: Composer.Nav) -> None:
        act, arg = event.action, event.arg
        bg = len(self._tasks.get("background") or [])
        sub = len(self._tasks.get("subagents") or [])
        composer = self.query_one("#composer", Composer)
        if act == "enter":
            if not (bg or sub):
                return
            self._nav = "pills"
            self._pill = "bg" if bg else "sub"
            composer.nav = "pills"
            self.query_one(StatusBar).show_tasks(
                bg=bg, sub=sub, focus=self._pill)
            return
        if act == "switch":
            if not self._nav:
                return
            self._pill = "sub" if self._pill == "bg" else "bg"
            if not (sub if self._pill == "sub" else bg):
                self._pill = "bg" if self._pill == "sub" else "sub"
            self.query_one(StatusBar).show_tasks(
                bg=bg, sub=sub, focus=self._pill)
            return
        if act == "leave" or act == "close":
            self._close_nav()
            return
        if act == "open":
            if not self._pill:
                return
            self._nav = "detail"
            self._detail_top = 0
            composer.nav = "detail"
            self._show_detail()
            return
        if act in ("detail_up", "detail_down"):
            rows = self._detail_rows()
            if act == "detail_up":
                self._detail_top = max(0, self._detail_top - 1)
            elif self._detail_top + TaskDetail.WINDOW >= len(rows):
                # 已经看到最后一条：↓ 交回底栏（用户定的，不循环）。
                self._nav = "pills"
                composer.nav = "pills"
                self.query_one(StatusBar).show_tasks(bg=bg, sub=sub,
                                                    focus=self._pill)
                return
            else:
                self._detail_top += 1
            self._show_detail()

    def _detail_rows(self) -> list[str]:
        key = "background" if self._pill == "bg" else "subagents"
        rows = []
        for task in self._tasks.get(key) or []:
            label = (task.get("description") or task.get("command")
                     or task.get("task_id") or "").strip().replace("\n", " ")
            if len(label) > 70:
                label = label[:69] + "…"
            rows.append(f"{label}  {_fmt_task_ms(task.get('elapsed_ms'))}")
        return rows

    def _show_detail(self) -> None:
        title = t("后台任务") if self._pill == "bg" else t("子代理")
        self.query_one(TaskDetail).show(title=title, rows=self._detail_rows(),
                                        top=self._detail_top)

    def _close_nav(self) -> None:
        self._nav = ""
        self._pill = ""
        self._detail_top = 0
        self.query_one("#composer", Composer).nav = ""
        self.query_one(TaskDetail).clear()
        data = self._tasks
        self.query_one(StatusBar).show_tasks(
            bg=len(data.get("background") or []),
            sub=len(data.get("subagents") or []), focus="")

    def on_composer_attach_image(self, event: Composer.AttachImage) -> None:
        """Alt+V：读剪贴板里的图。0.4 秒的活，放后台线程。"""
        threading.Thread(target=self._read_clipboard, daemon=True).start()

    def _read_clipboard(self) -> None:
        try:
            data_url = clipboard.read_image()
        except clipboard.ClipboardError as exc:
            self.call_from_thread(self._note, t("剪贴板读取失败：%s") % exc)
            return
        if data_url is None:
            self.call_from_thread(self._note, t("剪贴板里没有图片"))
            return
        self.call_from_thread(self._attach_image, data_url)

    def _attach_image(self, data_url: str) -> None:
        """把图挂进输入框：光标处留一个 `【图片n】` 占位符。

        图本身留在 `_images` 里，提交时按占位符取用 —— 正文与图各走各的，
        用户能像改字一样删掉它（删了就相当于撤回这张图）。
        """
        composer = self.query_one("#composer", Composer)
        if composer.disabled:
            # 还没启动完 / 审批面板占着槽：图无处安放，且占位符插进去也没人看。
            self._note(t("输入框未就绪"))
            return
        token = f"【图片{len(self._images) + 1}】"
        self._images.append({"token": token, "data_url": data_url})
        composer.insert(token)
        composer.focus()

    def on_composer_background(self, event: Composer.Background) -> None:
        """Ctrl+B：把当前前台任务转到后台。转不转得动由后端说了算。"""
        threading.Thread(target=self._post_background, daemon=True).start()

    def _post_background(self) -> None:
        try:
            code, body = self.backend.post_json("/api/background", {}, timeout=30)
        except Exception as exc:                         # noqa: BLE001
            self.call_from_thread(self._note,
                                  t("转后台失败：%s") % type(exc).__name__)
            return
        note = t("转后台失败")
        try:
            data = json.loads(body) or {}
            note = str(data.get("note") or note)
        except json.JSONDecodeError:
            pass
        self.call_from_thread(self._note, note)

    def _llm_retry(self, ev: E.Event) -> None:
        """上游连不上：红条常驻，带「正在重试 n/max」。

        与 GUI 同一条口径（`StatusBar.jsx` 的 red 分支）：三个阶段里
        `silent`（静默期，每 120 秒一次）与 `failed`（这一轮已收尾）都不报次数 ——
        次数在那儿是噪声，用户要知道的是"还在等"。
        """
        phase = ev.s("phase") or "retry"
        label = retry_label(ev.s("kind"), ev.s("text"))
        meta = ""
        if phase not in ("silent", "failed"):
            n, mx = ev.i("retry"), ev.i("max")
            if n >= 1 and mx >= 1:
                meta = t("正在重试 %s/%s") % (min(n, mx), mx)
        self.query_one(StatusLine).fault(label, meta, tag="llm")

    # -- 回复里的本机路径 -----------------------------------------

    def reveal(self, path: str) -> None:
        """点了一下回复里的本机路径：请后端在资源管理器里定位。

        不自己 `startfile` —— 那条路会"用默认程序打开"，模型写个 `.bat`
        的路径就成了一点即运行。后端 `/api/reveal` 只定位、不执行，
        相对路径的解析（按工作区根依次试）也在那边，不抄第二份。
        """
        threading.Thread(target=self._post_reveal, args=(path,),
                         daemon=True).start()

    def _post_reveal(self, path: str) -> None:
        try:
            code, body = self.backend.post_json("/api/reveal", {"path": path},
                                                timeout=20)
        except Exception as exc:                             # noqa: BLE001
            self.call_from_thread(self._note,
                                  t("定位失败：%s") % type(exc).__name__)
            return
        try:
            data = json.loads(body) or {}
        except json.JSONDecodeError:
            data = {}
        if data.get("ok"):
            # 成功不说话：资源管理器已经把结果显示在眼前了，再补一行是噪音。
            return
        self.call_from_thread(
            self._note, str(data.get("error")
                            or t("定位失败（HTTP %s）") % code))

    def _compact_progress(self, ev: E.Event) -> None:
        """压缩的四态：黄条进行中、绿/黄/红各一句结论（文案取后端 `message`）。"""
        state = ev.s("state")
        status = self.query_one(StatusLine)
        if state == "start":
            status.compact_start()
            return
        status.compact_end()
        msg = ev.s("message")
        if state == "ok":
            self.query_one(Transcript).add(
                Text(msg or t("压缩完成。"), style="green"))
        elif state in ("degraded", "broken"):
            self.query_one(Transcript).add(
                Text(msg or t("上下文压缩失败——重启应用以继续此对话"),
                     style="yellow"))
        elif state == "fail":
            reason = ev.s("reason")
            self.query_one(Transcript).add(
                Text(t("压缩失败：%s") % reason if reason else t("压缩失败"),
                     style="red"))
        elif state == "none":
            self.query_one(Transcript).add(
                Text(t("当前上下文容量未达最低压缩量"), style="dim"))
        pct = ev.data.get("percent")
        if isinstance(pct, (int, float)):
            self._ctx_pct = float(pct)
            # 刚压完就离开预警区了（`armed` 要等下一轮的 `context.state`
            # 才会重算）—— 这里跟着清，否则读数会挂着"接近触发"说反话。
            self._ctx_armed = False
            self._statusbar(busy=self._busy)

    def _statusbar(self, *, busy: bool) -> None:
        pct = "—" if self._ctx_pct is None else f"{_fmt_pct(self._ctx_pct)}%"
        # 「完全权限」在底栏只写「完全」—— 四个字那一格太宽，而这一格是常驻的。
        mode = self._mode
        if mode == "完全权限":
            mode = t("完全")
        self.query_one(StatusBar).show(
            mode=mode, effort=self._effort, busy=busy,
            ctx=t("上下文：") + f"{pct}/{_fmt_pct(self._ctx_thr)}%",
            ctx_armed=self._ctx_armed,
            services=self._services, services_warn=self._services_warn)

    def _send(self, text: str, *, new: bool = False,
              images: list[str] | None = None, record: bool = True) -> None:
        """发出去。会阻塞在 HTTP 上，所以真正的请求放在后台线程。

        `record=False` 只给 `/btw` 用：那句话不进转写区（窗口关掉之后
        它也不该假装是对话的一部分）。
        """
        was_busy = self._busy
        # 带图的话不走插话：`/api/steer` 只收文本，图会静默丢掉。
        steering = (was_busy and not new and not text.startswith("/")
                    and not images)
        transcript = self.query_one(Transcript)
        msg_id = ""
        if steering:
            # 跑着的时候补的那句话：先黄着 + 黄点，表示"还没被读进去"；
            # `steer.delivered` 到了再转回正常色（见 `_on_event`）。
            msg_id = uuid.uuid4().hex
            line = Text("● ", style="yellow")
            line.append(f"【User】{text}")
            block = transcript.add(line, classes="user steer")
            self._steer_lines[msg_id] = (block, text)
            self._pending_lines.append(msg_id)
        else:
            if record:
                transcript.add(Text(f"【User】{text}", style="bold green"),
                               classes="user")
        self._wrote_this_turn = False
        self._busy = True
        self._statusbar(busy=True)
        self.query_one(StatusLine).begin()
        if steering:
            # 走侧信道（steer）—— 注进下一步的边界，而不是等这一轮跑完再排。
            # 被拒（没在跑/是命令）就回落成排队。
            threading.Thread(target=self._post_steer, args=(text, msg_id),
                             daemon=True).start()
            return
        threading.Thread(target=self._post, args=(text, new, images),
                         daemon=True).start()

    def _mark_steer(self, msg_id: str) -> None:
        """把一条待读的话改回正常样式。幂等（找不到就算了）。"""
        if msg_id in self._pending_lines:
            self._pending_lines.remove(msg_id)
        item = self._steer_lines.pop(msg_id, None)
        if item is not None:
            block, text = item
            block.update(Text(f"【User】{text}", style="bold green"))

    def _promote_pending(self) -> None:
        """一轮开始：把最早那条排队中的话转正（只转一条 —— 转两条的话，
        还没轮到的那条会提前变白）。"""
        while self._pending_lines:
            mid = self._pending_lines[0]
            if mid in self._steer_lines:
                self._mark_steer(mid)
                return
            self._pending_lines.pop(0)          # 已经转正过的，跳过

    def _post_steer(self, text: str, msg_id: str) -> None:
        payload = {"text": text, "client_msg_id": msg_id}
        try:
            code, body = self.backend.post_json("/api/steer", payload, timeout=60)
        except Exception:                                # noqa: BLE001
            self.call_from_thread(self._mark_steer, msg_id)
            self._post(text, False)                      # 侧信道挂了就排队
            return
        accepted = False
        if code == 200:
            try:
                accepted = bool(json.loads(body).get("accepted"))
            except json.JSONDecodeError:
                accepted = False
        if accepted:
            # 不另发提示：那句话已经以黄色进步骤流，再补一句是重复。
            return
        else:
            # idle / awaiting_approval / command：后端明说收不了，回落成排队。
            # **不转正**：它还没被读进去，黄着才是实话 —— 转正在 `task.start`
            # 那一刻发生（`_promote_pending`），也就是它真开始跑的时候。
            self._post(text, False)

    def _post(self, text: str, new: bool, images: list[str] | None = None) -> None:
        payload = {"text": text}
        if new:
            payload["new"] = True
        if images:
            payload["images"] = images
        try:
            code, body = self.backend.post_json("/api/chat", payload,
                                                timeout=TURN_TIMEOUT)
        except Exception as exc:                             # noqa: BLE001
            self.call_from_thread(
                self._fail, t("请求失败：%s") % f"{type(exc).__name__}: {exc}")
            return
        if code != 200:
            self.call_from_thread(
                self._fail, t("后端返回 %s：%s") % (code, body[:160]))
            return
        try:
            reply = (json.loads(body) or {}).get("reply")
        except json.JSONDecodeError:
            reply = None
        self.call_from_thread(self._maybe_reply, reply)

    def _fail(self, message: str) -> None:
        for mid in list(self._pending_lines):    # 这一轮没起来，别留着黄字
            self._mark_steer(mid)
        self._busy = False
        self.query_one("#status", StatusLine).idle()
        self._statusbar(busy=False)
        if self._approval is not None:
            # 请求都没发出去，审批自然也不会有人来答 —— 撤掉，把输入框还回来。
            self._approval_dismiss()
        if self._btw_active:
            # 窗开着：错就写在框里，别让它一直停在"正在回答…"。
            self._btw_orphan = False
            self.query_one("#btw-panel", BtwPanel).finish("✗ " + message)
            return
        if self._btw_orphan:
            self._btw_orphan = False
            self._add_btw_note("✗ " + message)
            return
        self._recap_pending = False
        self.query_one(Transcript).add(Text("✗ " + message, style="red"))

    def _maybe_reply(self, reply: object) -> None:
        """兜底：事件流没送到时，用 HTTP 返回值补一次。

        判据是"这一轮写过没有"，不是"reply 空不空" —— 空回复也是回复，
        重复打印同一段才难看。
        """
        if isinstance(reply, str) and reply.strip() and not self._wrote_this_turn:
            if self._btw_active:
                self.query_one("#btw-panel", BtwPanel).finish(reply)
                return
            if self._btw_orphan:
                self._btw_orphan = False
                self._add_btw_note(reply)
                return
            if self._recap_pending:
                self._recap_pending = False
                self._add_recap(_recap_body(reply))
                return
            self.query_one(Transcript).add_agent_text(reply)
            self._wrote_this_turn = True

    # -- 命令 -----------------------------------------------------

    def action_clear(self) -> None:
        self._images.clear()
        # 抬头留着：它是对话区第一块，但不是这一会话的内容。
        self.query_one(Transcript).clear_content()
        # 卡片跟着一起没了。留着手里的引用，`_tick` 会去更新已经不存在的部件。
        self._cards.clear()
        self._pending_cards.clear()

    def on_unmount(self) -> None:
        if self._prev_title is not None:
            _set_console_title(self._prev_title)
        if self.stream is not None:
            self.stream.stop()
        self.backend.stop()
