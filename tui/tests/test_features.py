"""这轮新接线功能的断言：多行输入、斜杠补全、插话与中断、流式预览、多选审批。

它们都是"接线"—— 每一根线两头都连着真东西（后端接口、事件流），
接错的表现不是报错，而是"按了没反应"或"话送去了别处"。
所以每一根都从按键喂到 FakeBackend 的账本上，中途不跳。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from textual import events  # noqa: E402
from textual.widgets import Static  # noqa: E402

from crannia_tui import events as E  # noqa: E402
from crannia_tui.app import CranniaApp  # noqa: E402
from rich.text import Text  # noqa: E402

from crannia_tui.ui import (AgentMarkdown, ApprovalPanel, BtwPanel,  # noqa: E402
                            Composer, HeaderBar, SPIN_FRAMES, StatusBar,
                            StatusLine, SuggestBar, TaskDetail, Transcript,
                            UpdateHint, harden_soft_breaks, local_path_of)

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def block_text(w) -> str:
    """对话区里任意一块的字（Markdown 有 `.source`，Static 有 `.content`）。"""
    src = getattr(w, "source", None)
    return src if isinstance(src, str) else str(getattr(w, "content", ""))


def render(w) -> str:
    """部件的 _build 产物渲染成文本（Table 不能用 str() 看内容）。"""
    from rich.console import Console
    con = Console(width=72, force_terminal=False, legacy_windows=False)
    with con.capture() as cap:
        con.print(w)
    return cap.get()


async def wait_for_state(app, pred, limit: float = 3.0) -> None:
    """等一个"后台线程 -> call_from_thread"的落定。"""
    waited = 0.0
    while not pred() and waited < limit:
        await asyncio.sleep(0.05)
        waited += 0.05


class FakeBackend:
    """按路径记账并回不同的包。送到哪个接口、带了什么，测试都要能翻账。"""

    base = ""
    token = ""

    def __init__(self, pending: dict | None = None,
                 steer_accepts: bool = True) -> None:
        self.pending = pending
        self.steer_accepts = steer_accepts
        self.calls: list[tuple[str, dict]] = []
        #: `/api/status` 回什么 —— 模拟"用户在 GUI 里改了模式"。
        self.mode = "手动"
        self.effort = "high"
        #: `GET /api/tasks` 回什么（后台 / 子代理）。
        self.tasks = {"background": [], "subagents": []}
        #: `/api/reveal` 回什么。默认是"定位成功"。
        self.reveal_reply = (200, json.dumps(
            {"ok": True, "path": "D:/ws/x.md", "dir": False}))
        self._lock = threading.Lock()

    def get_json(self, path: str) -> dict:
        if path == "/api/approval":
            return {"pending": self.pending}
        if path == "/api/status":
            return {"mode_name": self.mode, "effort_name": self.effort}
        if path == "/api/tasks":
            return self.tasks
        return {}

    def post_json(self, path: str, payload: dict, timeout: float | None = None):
        with self._lock:
            self.calls.append((path, payload))
        if path == "/api/steer":
            return 200, json.dumps({"accepted": self.steer_accepts,
                                    "reason": "" if self.steer_accepts else "idle"})
        if path == "/api/stop":
            return 200, json.dumps(
                {"note": "已打断，请告诉Crannia应该怎么做"})
        if path == "/api/background":
            return 200, json.dumps({"ok": True, "count": 1,
                                    "note": "已把 1 个任务转到后台"})
        if path == "/api/reveal":
            return self.reveal_reply
        return 200, json.dumps({"reply": ""})

    def stop(self) -> None:
        pass


class _App(CranniaApp):
    # 样式表按"定义 App 的那个模块"解析，子类在 tests/ 下，所以要给绝对路径。
    CSS_PATH = str(HERE / "crannia_tui" / "app.tcss")

    def _boot(self) -> None:                      # 不连后端
        pass


def ev(_type: str, **data) -> E.Event:
    return E.Event(type=_type, data=data)


CATALOG = [
    {"cmd": "/compact", "desc": "压缩对话记录", "implemented": True},
    {"cmd": "/context", "desc": "查看上下文构成", "implemented": True},
    {"cmd": "/clear", "desc": "清空上下文", "implemented": True},
]


async def wait_for(fake: FakeBackend, count: int, limit: float = 3.0) -> None:
    waited = 0.0
    while len(fake.calls) < count and waited < limit:
        await asyncio.sleep(0.05)
        waited += 0.05


def test_composer_keys() -> None:
    """回车发送；Shift+Enter / Ctrl+J 换行不发送。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()

            await pilot.press(*"第一行")
            await pilot.press("shift+enter")
            await pilot.pause()
            check("\n" in composer.text, "Shift+Enter 应当换行而不发送")
            check(not fake.calls, "换行不该触发发送")

            await pilot.press(*"第二行")
            await pilot.press("ctrl+j")
            await pilot.pause()
            check(composer.text == "第一行\n第二行\n", "Ctrl+J 同样换行")

            await pilot.press(*"第三行")
            await pilot.press("enter")
            await wait_for(fake, 1)
            check(len(fake.calls) == 1, "回车才发送")
            path, payload = fake.calls[0]
            check(payload.get("text") == "第一行\n第二行\n第三行",
                  f"多行文本要完整送出，实际 {payload.get('text')!r}")
            check(composer.text == "", "发送后输入框应清空")

    asyncio.run(scenario())


def test_numpad_keys() -> None:
    """小键盘的 `/ * + - .` 在一些终端里只有键名、没有字符。

    这些终端把键发成 `CSI <码> ~`（除号是 `ESC[47~`），Textual 认出
    `slash` 却不给 character；`TextArea` 只按 character 插入，于是
    按键到了、字没出来。Composer 按键盘面补一次。
    """
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            c = app.query_one(Composer)
            # `_boot` 在测试里被跳过，输入框还是开机时的禁用态 —— 先开回来。
            c.disabled = False
            c.focus()
            # `divide` 是 Windows 终端上小键盘除号的真实键名（不是 `slash`）；
            # 其余是同一批"有键名、没字符"的键。
            for key in ("divide", "asterisk", "plus", "minus", "full_stop"):
                c.post_message(events.Key(key, None))
            await pilot.pause()
            check(c.text == "/*+-.", f"小键盘应能出字，实际 {c.text!r}")

            # 带字符的事件照旧走 TextArea，别插两次
            c.post_message(events.Key("slash", "/"))
            await pilot.pause()
            check(c.text == "/*+-./", f"带字符时不应重复插入，实际 {c.text!r}")

    asyncio.run(scenario())


def test_suggest() -> None:
    """输入 / 起头时列候选：↑↓ 选、回车填入、Esc 收起、一屏 6 条。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            app._catalog = CATALOG
            bar = app.query_one(SuggestBar)

            await pilot.press("/")
            await pilot.pause()
            check(bar.display, "输入 / 后应当出现候选条")
            check(len(app._matches) == 3, f"三条都匹配，实际 {len(app._matches)}")

            await pilot.press(*"co")
            await pilot.pause()
            check([m["cmd"] for m in app._matches] == ["/compact", "/context"],
                  f"应过滤成两条，实际 {[m['cmd'] for m in app._matches]}")

            # ↓ 只挪选择，不动输入框
            await pilot.press("down")
            await pilot.pause()
            check(app._match_i == 1, f"↓ 应选中第二条，实际 {app._match_i}")
            check(composer.text == "/co", f"选择不该改输入，实际 {composer.text!r}")

            # 回车：把选中的填进去，光标落在末尾（还能接参数）
            await pilot.press("enter")
            await pilot.pause()
            check(composer.text == "/context ", f"回车应填入，实际 {composer.text!r}")
            check(not bar.display, "填入后候选应收起")

            # 已经打全的命令：回车就是发送（不该再"填入"一次）
            composer.text = "/compact"
            await pilot.pause()
            await pilot.press("enter")
            await wait_for(fake, 1)
            path, payload = fake.calls[-1]
            check(path == "/api/chat" and payload.get("text") == "/compact",
                  f"打全后回车应发送，实际 {path} {payload!r}")

            # Esc：收起候选，输入框里的字不动
            composer.text = "/co"
            await pilot.pause()
            check(bar.display, "重新输入应再次弹出")
            await pilot.press("escape")
            await pilot.pause()
            check(not bar.display, "Esc 应收起候选")
            check(composer.text == "/co", f"Esc 不该改输入，实际 {composer.text!r}")

            # 一屏 6 条：选中越过第 6 条时窗口滚动
            app._catalog = [{"cmd": f"/cmd{i}", "desc": "", "implemented": True}
                            for i in range(9)]
            composer.text = "/cmd"
            await pilot.pause()
            check(len(app._matches) == 9, f"九条都应匹配，实际 {len(app._matches)}")
            for _ in range(7):
                await pilot.press("down")
            await pilot.pause()
            check(app._match_i == 7, f"应选到第 8 条，实际 {app._match_i}")
            check(app._suggest_top == 2,
                  f"选择过第 6 行后窗口应滚动，实际 top={app._suggest_top}")

            # 带上参数（出现空格）后候选收起
            composer.text = "/skill list"
            await pilot.pause()
            check(not bar.display, "带参数后候选应收起")

            # 不是 / 起头：候选不出现
            composer.text = "普通消息"
            await pilot.pause()
            check(not bar.display, "普通输入不弹候选")

    asyncio.run(scenario())


def test_steer_and_stop() -> None:
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()

            # 1) 忙着的时候说一句话 → 走 steer 侧信道，不走 chat
            app._busy = True
            await pilot.press(*"先别删那个文件")
            await pilot.press("enter")
            await wait_for(fake, 1)
            check(fake.calls[0][0] == "/api/steer",
                  f"忙时应走 steer，实际 {fake.calls[0][0]}")
            check(fake.calls[0][1].get("text") == "先别删那个文件",
                  "原文要送到收件箱")
            # 收下了就不再补一句"已插话"：黄字本身就是回执。
            texts = [str(w.content) for w in app.query("Static")]
            check(not any("已插话" in t for t in texts), "不该再补插话提示")

            # 2) 忙着的时候敲命令 → 不进 steer（后端拒收命令），排队走 chat
            await pilot.press(*"/compact")
            await pilot.press("enter")
            await wait_for(fake, 2)
            check(fake.calls[1][0] == "/api/chat", "命令应排队走 chat")

            # 3) Esc → 中断当前轮
            await pilot.press("escape")
            await wait_for(fake, 3)
            check(fake.calls[-1][0] == "/api/stop", "Esc 应请求中断")
            await pilot.pause()
            texts = [str(w.content) for w in app.query("Static")]
            check(any("已打断，请告诉Crannia应该怎么做" in t for t in texts),
                  "中断回执应为统一文案")

            # 4) 审批挂着时 Esc 不中断 —— 那时"停"的语义是模糊的
            fake.pending = {"request_id": "r1", "kind": "command",
                            "body": "x", "options": [
                                {"key": "allow", "label": "同意"}],
                            "default_key": "allow"}
            app._on_event(ev(E.APPROVAL_REQUEST, request_id="r1"))
            await pilot.pause()
            n = len(fake.calls)
            await pilot.press("escape")
            await pilot.pause()
            check(len(fake.calls) == n, "审批挂着时 Esc 不应触发中断")

    asyncio.run(scenario())


def test_steer_fallback() -> None:
    """后端拒收（没在跑）时回落成排队 —— 用户的话必须有个去处。"""
    async def scenario() -> None:
        fake = FakeBackend(steer_accepts=False)
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()

            app._busy = True
            await pilot.press(*"还在吗")
            await pilot.press("enter")
            await wait_for(fake, 2)
            paths = [c[0] for c in fake.calls]
            check(paths == ["/api/steer", "/api/chat"],
                  f"被拒后应回落成 chat，实际 {paths}")

    asyncio.run(scenario())


def test_delta_ignored() -> None:
    """TUI 不做逐字输出（明文决定，见 app.py 模块 docstring）。

    delta 事件必须被安静地忽略：不挂预览块、不崩 —— 它在事件流里照样
    会来（桌面端要用），忽略是这里的姿态，不是"没接到"。
    """
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            before = len(app.query(".agent"))
            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.DELTA, text="你好"))
            app._on_event(ev(E.DELTA, text="，世界"))
            await pilot.pause()
            check(len(app.query(".agent")) == before, "delta 不该在对话区挂任何块")
            check(app._busy, "忽略 delta 不该影响轮次状态")

            # 权威文本照常上屏 —— 没有 delta，整段也一个字不少
            app._on_event(ev(E.SAY, text="你好，世界！"))
            await pilot.pause()
            texts = [block_text(w) for w in app.query(".agent")]
            check(any("你好，世界！" in t for t in texts), "整段正文应上屏")

    asyncio.run(scenario())


def test_banner() -> None:
    """活动横幅：转轮 + 每轮一句的随机短语 + 计时 + 本轮输出 token。

    口径与 GUI（StatusBar.jsx）逐条对齐，见 `StatusLine` 的 docstring。
    """
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            status = app.query_one(StatusLine)

            status.begin()
            status._since = time.monotonic() - 5     # 时钟拨到 5s，免得等
            status.spin()
            out = render(status._build())
            check(any(f in out for f in SPIN_FRAMES), "转轮字符要在横幅上")
            check("…" in out, "短语后面要有省略号")
            check("5.00s" in out, "计时 10s 以内给两位小数（GUI 口径）")

            status.set_tokens(301000)
            out = render(status._build())
            check("301k token" in out, f"本轮输出 token 要出现，实际 {out!r}")

            # 延迟出现：新一轮的头 0.75s 不露头（零点几秒的活不闪横幅）
            status.begin()
            status._since = time.monotonic() - 0.3
            check(render(status._build()).strip() == "", "头 0.75s 横幅不应露头")

            # 等审批：计时冻结（那段时间不是它在干活，是它在等人）
            status._since = time.monotonic() - 5
            status.pause()
            frozen = status._elapsed()
            time.sleep(0.15)
            check(abs(status._elapsed() - frozen) < 0.05,
                  f"等审批时计时应冻结，{frozen} -> {status._elapsed()}")
            status.resume()
            time.sleep(0.15)
            check(status._elapsed() > frozen + 0.1, "恢复后应从冻住的值继续走")

            # 整条一个样式且加粗：满屏正文里要一眼扫得到这一行
            import io as _io

            from rich.console import Console as _Console

            status.begin()
            status._since = time.monotonic() - 5
            con = _Console(file=_io.StringIO(), width=70, force_terminal=True,
                           record=True, color_system="truecolor",
                           legacy_windows=False)
            con.print(status._build())
            out = con.export_text(styles=True)
            check("\x1b[1;" in out, f"横幅应加粗：{out!r}")
            check(out.count("\x1b[") == 2,
                  f"整条只该有一次样式切换（加粗+颜色同一段）：{out!r}")

            status.idle()
            check(render(status._build()).strip() == "", "空闲时横幅清空")

    asyncio.run(scenario())


def test_approval_multi() -> None:
    """多选：空格勾选，回车按选项顺序拼 key。"""
    async def scenario() -> None:
        fake = FakeBackend({"request_id": "r1", "kind": "ask", "multi": True,
                            "body": "要哪几项？",
                            "options": [
                                {"key": "a", "label": "甲"},
                                {"key": "b", "label": "乙"},
                                {"key": "c", "label": "丙"}],
                            "default_key": "a"})
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            mount = lambda: app._on_event(ev(E.APPROVAL_REQUEST, request_id="r1"))
            mount()
            await pilot.pause()
            panel = app.query_one(ApprovalPanel)

            # 勾 1 和 3：空格勾当前项，下移到第 3 项再空格
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.pause()
            check(panel._ticked == {0, 2}, f"应勾上第 1、3 项，实际 {panel._ticked}")

            await pilot.press("enter")
            await wait_for(fake, 1)
            path, payload = fake.calls[-1]
            check(payload.get("choice") == "a,c",
                  f"应按选项顺序拼 key，实际 {payload.get('choice')!r}")

    asyncio.run(scenario())


def test_compact_progress() -> None:
    """压缩：黄横幅（自己的计时）→ 结论落步骤流，文案取后端 `message`。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.COMPACT_PROGRESS, state="start"))
            await pilot.pause()
            banner = app.query_one(StatusLine)
            check(banner._compact_since > 0, "压缩中应走自己的计时")
            out = str(banner._build())
            check("正在压缩" in out, f"横幅应写正在压缩：{out!r}")
            # 黄色由样式表决定，这里看基色字段
            check(banner._colour == "yellow", "压缩横幅应是黄色")

            app._on_event(ev(E.COMPACT_PROGRESS, state="ok",
                             message="压缩完成，当前上下文：3%。", percent=3))
            await pilot.pause()
            check(banner._compact_since == 0, "压缩结束应回到普通态")
            texts = [str(w.content) for w in app.query("Static")]
            check(any("压缩完成，当前上下文：3%。" in t for t in texts),
                  f"结论应进步骤流：{texts[-3:]}")
            check(app._ctx_pct == 3, f"上下文读数应更新，实际 {app._ctx_pct}")
            hint = str(app.query_one(StatusBar).query_one("#hint").content)
            check("上下文：3%/85%" in hint, f"右下角应显示上下文：{hint!r}")

            # 降级：文案照后端走，颜色黄
            app._on_event(ev(E.COMPACT_PROGRESS, state="degraded",
                             message="当前压缩器异常，本轮压缩质量降低，请关注"))
            await pilot.pause()
            texts = [str(w.content) for w in app.query("Static")]
            check(any("本轮压缩质量降低" in t for t in texts), "降级文案应上屏")

    asyncio.run(scenario())


def test_context_readout_matches_gui() -> None:
    """上下文读数与 GUI 同一口径：同源 `pct`、一位小数、`armed` 追加提示。

    曾经 `int(round(pct))`：窗口一百万 token 时 1.3% 和 1.0% 都显示成 1%
    —— 整场对话读数一动不动，看着像坏了。后端给的是 `round(x, 1)`，
    一位小数就该一位小数地显示。
    """
    async def scenario() -> None:
        app = _App(backend=FakeBackend())         # type: ignore[arg-type]
        async with app.run_test(size=(110, 18)) as pilot:
            await pilot.pause()
            bar = app.query_one(StatusBar)

            def hint() -> str:
                return str(bar.query_one("#hint").content)

            app._on_event(ev(E.CONTEXT_STATE, pct=1.3, threshold_pct=85.0,
                             armed=False))
            await pilot.pause()
            check("上下文：1.3%/85%" in hint(), f"应保留一位小数：{hint()!r}")

            # 整数不带 `.0`（后端的 85.0 显示成 85）
            check("85.0%" not in hint(), "整数不该拖 .0 尾巴")

            # 整数占用：1.0 -> 1（不是 1.0）
            app._on_event(ev(E.CONTEXT_STATE, pct=1.0, threshold_pct=85.0,
                             armed=False))
            await pilot.pause()
            check("上下文：1%/85%" in hint(), f"整数应显示成 1%：{hint()!r}")

            # armed：追加提示（与 GUI 那条 `.ctx-readout.armed` 同语义）
            app._on_event(ev(E.CONTEXT_STATE, pct=78.4, threshold_pct=85.0,
                             armed=True))
            await pilot.pause()
            check("上下文：78.4%/85% · 接近触发" in hint(),
                  f"armed 应追加提示：{hint()!r}")

    asyncio.run(scenario())


def test_steer_line_turns_normal() -> None:
    """插话：先黄 + 黄点；`steer.delivered` 到了转回正常样式。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True
            app._send("插一句")
            await pilot.pause()
            check(len(app._steer_lines) == 1, "应记下这条待读的插话")
            mid, (block, text) = next(iter(app._steer_lines.items()))
            check(block.content.plain.startswith("● "),
                  f"应先带黄点：{block.content.plain!r}")

            app._on_event(ev(E.STEER_DELIVERED, items=[mid]))
            await pilot.pause()
            check(app._steer_lines == {}, "送达后应从待读表里摘掉")
            check(block.content.plain.startswith("【User】"),
                  f"送达后应转正常样式：{block.content.plain!r}")

    asyncio.run(scenario())


def test_queued_message_waits_yellow() -> None:
    """排队的那句话黄着等：`task.start` 一到才转正（不是发出去就变白）。

    跑着的时候发的话有两种去处：插话（`steer.delivered` 转正）和排队
    （`task.start` 转正）。两种都先黄着 —— 黄 = 还没被读进去，转白 = 轮到了。
    只转一条：一次转两条的话，还没轮到的那条会提前变白。
    """
    async def scenario() -> None:
        app = _App(backend=FakeBackend())         # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            app._busy = True
            app._send("排队的第一句")
            app._send("排队的第二句")
            await pilot.pause()
            check(len(app._pending_lines) == 2, "两条都该是待读")
            blocks = [b for b, _t in app._steer_lines.values()]
            check(all(b.content.plain.startswith("● ") for b in blocks),
                  "排队中的话应带黄点")

            # 第一轮开始：只转正最早的那条
            app._on_event(ev(E.TASK_START, label="x"))
            await pilot.pause()
            check(blocks[0].content.plain.startswith("【User】"),
                  "最早那条应转正")
            check(blocks[1].content.plain.startswith("● "),
                  "还没轮到的那条应保持黄")
            check(app._pending_lines == [list(app._steer_lines)[0]],
                  f"待读表应剩一条，实际 {app._pending_lines}")

            # 第二轮开始：最后一条也转正
            app._on_event(ev(E.TASK_START, label="y"))
            await pilot.pause()
            check(blocks[1].content.plain.startswith("【User】"), "第二条应转正")
            check(app._pending_lines == [], "待读表应清空")

    asyncio.run(scenario())


def test_attach_image() -> None:
    """Alt+V：图挂在输入框里当 `【图片n】`，提交时按占位符取用。

    图本身不进正文（终端里显示不了位图），正文留的是占位符 —— 用户能像
    改字一样删掉它，删了就等于撤回那张图。
    """
    from crannia_tui import app as app_mod

    img1, img2 = "data:image/png;base64,AAA", "data:image/png;base64,BBB"
    seen: list = []

    def stub() -> str | None:
        return seen.pop(0) if seen else None

    original = app_mod.clipboard.read_image
    app_mod.clipboard.read_image = stub
    try:
        async def scenario() -> None:
            fake = FakeBackend()
            app = _App(backend=fake)              # type: ignore[arg-type]
            async with app.run_test() as pilot:
                await pilot.pause()
                composer = app.query_one("#composer", Composer)
                composer.disabled = False
                composer.focus()

                seen.extend([img1, img2])
                await pilot.press("alt+v")
                await wait_for_state(app, lambda: len(app._images) == 1)
                check(composer.text == "【图片1】",
                      f"光标处应留下占位符：{composer.text!r}")

                await pilot.press("alt+v")
                await wait_for_state(app, lambda: len(app._images) == 2)
                check(composer.text == "【图片1】【图片2】",
                      f"第二张应编号为 2：{composer.text!r}")

                # 删掉第一个占位符 = 撤回那张图
                composer.text = "【图片2】这张图里是什么"
                await pilot.pause()
                await pilot.press("enter")
                await wait_for(fake, 1)
                path, payload = fake.calls[-1]
                check(path == "/api/chat", f"应发聊天：{path}")
                check(payload.get("text") == "【图片2】这张图里是什么",
                      f"正文应带占位符：{payload.get('text')!r}")
                check(payload.get("images") == [img2],
                      f"只该发留下的那张：{payload.get('images')!r}")
                check(app._images == [], "发完应清空，下一句从头编号")

                # 剪贴板里没图：不插占位符，只留一句提示
                seen.clear()
                before = composer.text
                await pilot.press("alt+v")
                await wait_for_state(
                    app, lambda: any("剪贴板里没有图片" in block_text(w)
                                     for w in app.query("Static")))
                check(composer.text == before, "没图时不该改输入框")

        asyncio.run(scenario())
    finally:
        app_mod.clipboard.read_image = original


def test_attach_image_error() -> None:
    """读剪贴板失败要出声（没装、超时），不能静默什么都不发生。"""
    from crannia_tui import app as app_mod

    def boom() -> str | None:
        raise app_mod.clipboard.ClipboardError("找不到命令行解释器")

    original = app_mod.clipboard.read_image
    app_mod.clipboard.read_image = boom
    try:
        async def scenario() -> None:
            app = _App(backend=FakeBackend())     # type: ignore[arg-type]
            async with app.run_test() as pilot:
                await pilot.pause()
                composer = app.query_one("#composer", Composer)
                composer.disabled = False
                composer.focus()
                await pilot.press("alt+v")
                await wait_for_state(
                    app, lambda: any("剪贴板读取失败" in block_text(w)
                                     for w in app.query("Static")))
                check(composer.text == "", "失败时不该插占位符")

        asyncio.run(scenario())
    finally:
        app_mod.clipboard.read_image = original


def test_link_targets() -> None:
    """链接分两扇门：网址交给系统，本机路径交给后端定位。

    判据与 GUI 的 `openpath.js` 同一张表 —— 两边分类不一致，同一条链接
    在桌面端和终端里会去两个不同的地方。
    """
    check(local_path_of("workspace/x.md") == "workspace/x.md", "相对路径")
    check(local_path_of("D:/ws/x.md") == "D:/ws/x.md", "绝对路径")
    check(local_path_of("file:///D:/ws/x.md") == "D:/ws/x.md", "file:// 剥成本机路径")
    check(local_path_of("file://server/share/x.md") is None, "UNC 不认，当网址")
    check(local_path_of("https://a.com/x") is None, "网址")
    check(local_path_of("mailto:a@b.com") is None, "mailto 是网址那扇门")
    check(local_path_of("") is None, "空的不当路径")


def test_click_link_reveals() -> None:
    """点回复里的本机路径 → 只走后端 `/api/reveal`（不交给系统打开）。

    走 `_broker_event` 而不是直接调处理器：真实点击正是那条路
    （`@click=link(href)` → 动作 → `LinkClicked`）。基类那次"交给系统打开"
    是类级处理器，覆写顶不掉它 —— 少了 `open_links=False`，一次点击会
    **两个都发生**（文件被默认程序打开，资源管理器也跳出来）。
    """
    from rich.style import Style

    class Click:
        """`_broker_event` 只读 `style`，然后 `stop()`。"""

        def __init__(self, href: str) -> None:
            self.style = Style.from_meta({"@click": f"link({href!r})"})

        def stop(self) -> None:
            pass

    async def scenario() -> None:
        app = _App(backend=FakeBackend())         # type: ignore[arg-type]
        async with app.run_test(size=(90, 20)) as pilot:
            await pilot.pause()
            app.query_one(Transcript).add_agent_text(
                "产物：[报告](workspace/x.md) 也见 [文档](https://a.com)")
            await pilot.pause()
            block = list(app.query_one(AgentMarkdown).query("*"))[0]

            revealed: list[str] = []
            opened: list[str] = []
            app.reveal = lambda p: revealed.append(p)          # type: ignore[method-assign]
            app.open_url = lambda u, **k: opened.append(u)     # type: ignore[method-assign]

            await app._broker_event("click", Click("workspace/x.md"), block)
            await pilot.pause()
            check(revealed == ["workspace/x.md"],
                  f"本机路径应走 reveal，实际 {revealed}")
            check(opened == [],
                  f"本机路径不该被系统打开（那是一次点击两个动作）：{opened}")

            await app._broker_event("click", Click("https://a.com"), block)
            await pilot.pause()
            check(opened == ["https://a.com"],
                  f"网址应交给系统，实际 {opened}")
            check(revealed == ["workspace/x.md"], "网址不该走 reveal")

    asyncio.run(scenario())


def test_reveal_note() -> None:
    """定位回执：成功说"已定位"，失败照后端那句（含"找过哪儿"）。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test(size=(90, 20)) as pilot:
            await pilot.pause()

            before = len(app.query("Static"))
            app.reveal("workspace/x.md")
            await wait_for(fake, 1)
            await pilot.pause()
            check(fake.calls[-1] == ("/api/reveal", {"path": "workspace/x.md"}),
                  f"应带上路径：{fake.calls[-1]}")
            check(len(app.query("Static")) == before,
                  "成功不该再补一句回执（资源管理器已经把结果显示在眼前）")

            fake.reveal_reply = (404, json.dumps(
                {"ok": False, "error": "这个路径不存在：x.md（找过：D:/ws）"}))
            app.reveal("x.md")
            await wait_for_state(
                app, lambda: any("这个路径不存在" in block_text(w)
                                 for w in app.query("Static")))
            texts = [block_text(w) for w in app.query("Static")]
            check(any("这个路径不存在" in t for t in texts),
                  f"失败应照后端那句：{texts[-3:]}")

    asyncio.run(scenario())


def test_stream_down_banner() -> None:
    """事件流断开 → 红条常驻；恢复 → 收掉。与 llm 故障按来源互不干扰。

    断了的时候步骤流不会来 —— 那是"静默"的失败：回复照样由 POST 带回，
    只有活动块凭空少掉，界面上看就是"模型没干活"。
    """
    async def scenario() -> None:
        app = _App(backend=FakeBackend())         # type: ignore[arg-type]
        async with app.run_test(size=(100, 18)) as pilot:
            await pilot.pause()
            banner = app.query_one(StatusLine)

            app._on_event(ev(E.LLM_RETRY, kind="connection", text="",
                             retry=1, max=10, phase="retry"))
            await pilot.pause()
            check("网络连接异常" in str(banner._build()), "llm 故障应显示")

            # 事件流断开：后发生的说现在的状态
            app._apply_stream_state("步骤流已断开：URLError")
            await pilot.pause()
            check("步骤流已断开" in str(banner._build()),
                  f"流断开应接管横幅：{str(banner._build())!r}")

            # llm 恢复：只摘自己的；流还断着，那条横幅要接着显示
            app._on_event(ev(E.LLM_RETRY_OK))
            await pilot.pause()
            check("步骤流已断开" in str(banner._build()),
                  "llm 恢复不该把流断开的横幅顺走")

            # 流恢复：横幅收掉
            app._apply_stream_state("connected")
            await pilot.pause()
            check(banner._faults == {}, "流恢复后横幅应清空")

    asyncio.run(scenario())


def test_fault_banner() -> None:
    """故障横幅：全红一行、不带转轮、按 GUI 的三阶段给不给次数。

    界面静止时最需要的正是它 —— 等上游重连的几十秒里，绿条继续转圈
    等于说"一切正常"。
    """
    async def scenario() -> None:
        app = _App(backend=FakeBackend())         # type: ignore[arg-type]
        async with app.run_test(size=(100, 18)) as pilot:
            await pilot.pause()
            app._on_event(ev(E.TASK_START, label="x"))
            banner = app.query_one(StatusLine)

            # 后端没给文案 -> 按类型兜底（与 GUI 的 retryLabel 逐字一致）
            app._on_event(ev(E.LLM_RETRY, kind="connection", text="",
                             retry=2, max=10, phase="retry"))
            await pilot.pause()
            out = str(banner._build())
            check("！ 网络连接异常--请检查宽带互联网连接" in out,
                  f"文案应取兜底表：{out!r}")
            check("正在重试 2/10" in out, f"常规重试要带次数：{out!r}")
            check("…" not in out, "红条不该有转轮/省略号")

            # 后端给了文案就用后端的
            app._on_event(ev(E.LLM_RETRY, kind="server_busy",
                             text="模型服务繁忙（余额/限流）", retry=3, max=10,
                             phase="retry"))
            await pilot.pause()
            check("模型服务繁忙（余额/限流）" in str(banner._build()),
                  "后端文案优先")

            # 静默期 / 收尾：不报次数
            app._on_event(ev(E.LLM_RETRY, kind="timeout", text="",
                             retry=1, max=10, phase="silent"))
            await pilot.pause()
            check("正在重试" not in str(banner._build()),
                  f"静默期不该报次数：{str(banner._build())!r}")
            app._on_event(ev(E.LLM_RETRY, kind="timeout", text="",
                             retry=10, max=10, phase="failed"))
            await pilot.pause()
            check("正在重试" not in str(banner._build()), "收尾也不报次数")

            # 恢复：横幅收掉，回到活动横幅（这一轮还在跑）
            app._on_event(ev(E.LLM_RETRY_OK))
            await pilot.pause()
            check(banner._faults == {}, "恢复后故障文案应清空")
            check(app._busy, "恢复不该影响这一轮的状态")

    asyncio.run(scenario())


def test_fault_banner_is_all_red() -> None:
    """红线整条都要是红的（转轮、文案、次数同一色）。"""
    import io

    from rich.console import Console

    banner = StatusLine()
    # 直接摆字段：`fault()` 会调 `_paint()`，那要有一个活着的 App。
    banner._faults = {"llm": ("网络异常", "正在重试 1/3")}
    # 写进 StringIO：既拿到带样式的导出，又不会往测试输出里嵌一行横幅。
    con = Console(file=io.StringIO(), width=80, force_terminal=True,
                  record=True, color_system="truecolor", legacy_windows=False)
    con.print(banner._build())
    out = con.export_text(styles=True)
    check("\x1b[31m" in out, f"应带红色：{out!r}")
    plain = out.replace("\x1b[31m", "").replace("\x1b[0m", "")
    check("网络异常" in plain and "正在重试 1/3" in plain, "文案与次数都在同一行")
    check(plain.count("\x1b[") == 0,
          f"整行只该有一次颜色切换，不该在中途换色：{out!r}")


def test_task_pills_nav() -> None:
    """底栏两格：有才显示、↓进入、←→换、回车开详细页、↓回底栏、Esc 收起。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test(size=(90, 20)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            app._apply_tasks({"background": [
                {"task_id": "b1", "command": "sleep 100", "elapsed_ms": 81000},
                {"task_id": "b2", "command": "npm run dev", "elapsed_ms": 4000},
            ], "subagents": [
                {"task_id": "s1", "description": "等待爬虫响应", "elapsed_ms": 4000},
            ]})
            await pilot.pause()
            bar = app.query_one(StatusBar)
            tasks_cell = str(bar.query_one("#tasks").content)
            check("2个后台" in tasks_cell and "1个子代理" in tasks_cell,
                  f"两格应显示计数：{tasks_cell!r}")

            await pilot.press("down")
            await pilot.pause()
            check(app._nav == "pills" and app._pill == "bg",
                  f"↓ 应进入底栏第一格，实际 {app._nav}/{app._pill}")
            check(composer.nav == "pills", "输入框要知道自己在选底栏")

            await pilot.press("right")
            await pilot.pause()
            check(app._pill == "sub", f"→ 应换到子代理格，实际 {app._pill}")

            await pilot.press("enter")
            await pilot.pause()
            detail = app.query_one(TaskDetail)
            check(app._nav == "detail" and detail.display, "回车应打开详细页")
            check("子代理" in str(detail.content), "详细页标题应是子代理")
            check("等待爬虫响应" in str(detail.content), "条目应列出名称")

            # 只有一条：再 ↓ 就回底栏（不循环）
            await pilot.press("down")
            await pilot.pause()
            check(app._nav == "pills", f"末条 ↓ 应回底栏，实际 {app._nav}")

            await pilot.press("escape")
            await pilot.pause()
            check(app._nav == "" and not detail.display, "Esc 应关闭并回输入框")

            # 归零后两格消失，↓ 不再进入
            app._apply_tasks({"background": [], "subagents": []})
            await pilot.pause()
            check(str(bar.query_one("#tasks").content).strip() == "",
                  "没有任务时两格不应显示")
            await pilot.press("down")
            await pilot.pause()
            check(app._nav == "", "没有任务时 ↓ 不该进入底栏")

    asyncio.run(scenario())


def test_ctrl_b_background() -> None:
    """Ctrl+B：把前台任务转后台，回执上屏。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            await pilot.press("ctrl+b")
            await wait_for(fake, 1)
            check(fake.calls[-1][0] == "/api/background",
                  f"应发到 /api/background，实际 {fake.calls[-1]}")
            texts = [str(w.content) for w in app.query("Static")]
            check(any("已把 1 个任务转到后台" in t for t in texts), "回执应上屏")

    asyncio.run(scenario())


def test_soft_breaks_hardened() -> None:
    """单换行要定成硬换行，否则 Markdown 把它折成空格，一行一项的表糊成一段。

    `/effort` 的回执就是这么被挤成一整行的。围栏与表格里的换行不动。
    """
    check(harden_soft_breaks("a\nb") == "a  \nb", "相邻两行应补硬换行")
    check(harden_soft_breaks("a\n\nb") == "a\n\nb", "空行分隔的段落不动")
    code = "```py\nx = 1\ny = 2\n```"
    check(harden_soft_breaks(code) == code, "代码围栏内不许动")
    table = "| a | b |\n| - | - |"
    check(harden_soft_breaks(table) == table, "表格行不许动")

    async def scenario() -> None:
        app = _App(backend=FakeBackend())         # type: ignore[arg-type]
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.SAY, text="当前档位：3 high\n"
                                        "1 low - 2 medium - 3 high\n"
                                        "用 /effort <名称或数字> 切换档位"))
            await pilot.pause()
            await pilot.pause()
            strips = [st.text for st in app.screen._compositor.render_strips()]
            check(any("当前档位：3 high" in t for t in strips), "第一行应上屏")
            check(any(t.strip().startswith("1 low - 2 medium") for t in strips),
                  f"档位表应独占一行：{strips[-6:]}")
            check(not any("3 high 1 low" in t for t in strips),
                  "不应被折成一句话（两行粘在一起）")

    asyncio.run(scenario())


def test_window_title() -> None:
    """窗口标题：进界面设成 Crannia，退出把原值还回去。

    Textual 的 `TITLE` 只喂它自带的 Header 部件，**不会写终端标题** ——
    所以这条得自己发（`_set_console_title`）。启动器另有一行 `title Crannia`，
    覆盖 Python 起来之前那一秒。
    """
    from crannia_tui import app as app_mod

    seen: list[str] = []

    def stub(title: str) -> str:
        seen.append(title)
        return "原来的标题"

    original = app_mod._set_console_title
    app_mod._set_console_title = stub
    try:
        async def scenario() -> None:
            app = _App(backend=FakeBackend())     # type: ignore[arg-type]
            async with app.run_test() as pilot:
                await pilot.pause()
                check(app.title == "Crannia", f"App.title 应是 Crannia：{app.title!r}")
                check(seen == ["Crannia"], f"进界面应先设标题：{seen}")

        asyncio.run(scenario())
    finally:
        app_mod._set_console_title = original

    check(seen == ["Crannia", "原来的标题"],
          f"退出应还原原标题，实际 {seen}")


def test_scroll_repaints() -> None:
    """滚轮/滚动必须真的重绘，不只是滚动值变了。

    `Transcript.watch_scroll_y` 覆盖了父类的同名 watcher；少了 `super()`
    那一行，滚动条位置与 `_refresh_scroll()` 都不会跑 —— 表现为"滚了没反应"。
    """
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test(size=(60, 14)) as pilot:
            await pilot.pause()
            t = app.query_one(Transcript)
            for i in range(30):
                t.add(Text(f"第 {i:02d} 行"))
            for _ in range(3):
                await pilot.pause()

            def screen() -> str:
                return "\n".join(st.text
                                 for st in app.screen._compositor.render_strips())

            before = screen()
            t.scroll_to(y=6, animate=False)
            await pilot.pause()
            check(t.scroll_y == 6, f"滚动值应变，实际 {t.scroll_y}")
            check(screen() != before, "滚动后画面必须重绘（否则看起来像卡死）")
            if t.show_vertical_scrollbar:
                check(t.vertical_scrollbar.position == 6,
                      f"滚动条应跟到 6，实际 {t.vertical_scrollbar.position}")

    asyncio.run(scenario())


def test_header_scrolls_away() -> None:
    """抬头只在启动时打印一次，随后被内容自然顶走（不是固定区）。

    固定在最上面是给"随时要确认"的信息留的位置，而这三行一辈子只变一次。
    注意不能退回 `Transcript.anchor()`：它在内容不满一屏时会把整块内容
    压到底部（合成器按 `virtual - container` 算滚动量，负数不夹取），
    抬头会沉到输入框上方去。
    """
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test(size=(60, 14)) as pilot:
            await pilot.pause()
            head = app.query_one(HeaderBar)
            t = app.query_one(Transcript)
            check(head.region.y >= 0, f"启动时抬头应在顶部，实际 y={head.region.y}")
            check(t.scroll_y == 0, f"内容不满一屏时不该预滚，实际 {t.scroll_y}")

            for i in range(15):
                t.add(Text(f"第 {i} 条消息"))
            await pilot.pause()
            await pilot.pause()
            check(t.scroll_y > 0, "内容超屏后应贴底")
            check(head.region.y < 0,
                  f"抬头应随内容滚走，实际 y={head.region.y}")
            check(t.children and t.children[0] is head,
                  "抬头应是对话区第一块（不是浮在区外的固定条）")

            # 往上翻能翻回抬头（它只是滚走了，不是消失了）
            t.scroll_to(y=0, animate=False)
            await pilot.pause()
            await pilot.pause()
            check(t.scroll_y == 0, f"应能滚回顶部，实际 {t.scroll_y}")

    asyncio.run(scenario())


def test_status_refreshed_each_turn() -> None:
    """设置只有一份（后端）：TUI 不许把档位缓存到会话结束。

    用户在 GUI 里把模式从「手动」改成「自动」再回来，这边状态栏必须跟上 ——
    开机读一次就冻住的话，显示的是一句假话。审批策略同理归后端管
    （自动放行跨区时 `approval.request` 根本不会来，TUI 无需自己判）。
    """
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            check(app._mode == "" and app._effort == "", "初始未读到档位")
            fake.mode, fake.effort = "自动", "low"

            # 轮次结束 -> 后台重读 /api/status
            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.TASK_END, ok=True, response=""))
            await wait_for_state(app, lambda: app._mode == "自动")
            check(app._mode == "自动" and app._effort == "low",
                  f"轮末应跟上后端档位，实际 {app._mode}/{app._effort}")
            state = app.query_one("#state", Static)
            text = str(state.content)
            check("自动" in text and "low" in text,
                  f"状态栏应显示新档位，实际 {text!r}")

    asyncio.run(scenario())


def test_header_shows_launch_cwd() -> None:
    """抬头那行路径是"呼出的目录"，不是后端的代码根。

    设计里目录就是这一次会话的工作目录（D3）；后端报的 `project_root`
    是代码根，拿它显示等于把"用户站在哪"换成了"程序装在哪"。
    """
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake, launch_cwd="D:/work/here")   # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            app._boot_ready({"model": "deepseek-flash",
                             "model_config_name": "",
                             "project_root": "D:/crannia/code",
                             "version": "1.0.0",
                             "mode_name": "手动",
                             "effort_name": "xhigh"}, [])
            await pilot.pause()
            info = app.query_one(HeaderBar).query_one("#info").content
            text = str(info)
            check("D:/work/here" in text, f"抬头应显示呼出目录，实际 {text!r}")
            check("D:/crannia/code" not in text, "抬头不该显示后端代码根")
            check("deepseek-flash | xhigh" in text,
                  f"应显示模型与档位，实际 {text!r}")
            styles = [str(s.style) for s in info.spans]
            check("#4caf50" in styles, f">_ 应为绿色，实际 {styles!r}")

    asyncio.run(scenario())


def test_approval_survives_task_end() -> None:
    """审批挂起时后端就是会发 task.end（空 response）—— 面板必须活着。

    后端挂起等拍板时轮次本来就会正常结束（task.end、response 为空），
    答完后 POST /api/approval 另起一轮 —— "结束还挂着审批"是常态。
    """
    async def scenario() -> None:
        fake = FakeBackend({"request_id": "r1", "kind": "cross_zone",
                            "body": "要跨区搜索，批吗？",
                            "options": [{"key": "allow", "label": "同意"},
                                        {"key": "deny", "label": "不同意"}],
                            "default_key": "allow"})
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            app._on_event(ev(E.TASK_START, label="查假条"))
            app._on_event(ev(E.APPROVAL_REQUEST, request_id="r1"))
            await pilot.pause()
            check(app._approval is not None, "审批面板应挂上")

            # 轮次"结束"（后端挂审批时的常态收尾）
            app._on_event(ev(E.TASK_END, ok=True, response=""))
            await pilot.pause()
            check(app._approval is not None,
                  "task.end 不该撤审批面板 —— 挂起等拍板时轮次就是会正常结束")
            check(app.query_one(ApprovalPanel).display, "面板应仍然可见")
            check(app.query_one("#composer", Composer).display is False,
                  "输入框应继续让位给面板")

    asyncio.run(scenario())


def test_update_hint() -> None:
    """新版本提示：没有新版本时不占行；有就贴在输入框上方并带出版本号。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            hint = app.query_one("#update-hint", UpdateHint)
            check(hint.display is False, "没有更新时不占行")
            app._show_update_hint("新版本已下载，重启应用以更新")
            await pilot.pause()
            check(hint.display is True, "装好之后显示提示")
            check("重启应用以更新" in str(hint.render()), "提示要说出下一步")
    asyncio.run(scenario())


def test_done_line() -> None:
    """一轮说完之后在最终回复下面加一行 `done · 用时 · 时刻`；中断不加。"""
    async def scenario() -> None:
        app = _App(backend=FakeBackend())         # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            transcript = app.query_one(Transcript)
            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.SAY, text="答案"))
            app._on_event(ev(E.TASK_END, response="答案"))
            await pilot.pause()
            last = str(transcript.children[-1].render())
            check(last.startswith("done · "), "最终回复下面应有 done 行：%r" % last)
            check(last.count(" · ") == 2, "形状是 done · 用时 · 时刻：%r" % last)

            n = len(transcript.children)
            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.TASK_END, response="", cancelled=True))
            await pilot.pause()
            check(len(transcript.children) == n, "中断那一轮不该加 done 行")
    asyncio.run(scenario())


def test_switch_commands_are_desktop_only() -> None:
    """/resume 与 /rewind 改的是后端那一段对话，TUI 不重画 —— 不发、说清。"""
    async def scenario() -> None:
        from crannia_tui.commands import parse_command

        check(parse_command("/resume 2")[0] == "desktop", "/resume 应拦")
        check(parse_command("/rewind")[0] == "desktop", "/rewind 应拦")
        check(parse_command("/fork")[0] == "chat", "fork 的副本与屏幕一致，不用拦")

        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            app._catalog = CATALOG + [{"cmd": "/resume", "desc": "",
                                       "implemented": True}]
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            await pilot.press("/")
            await pilot.press(*"re")
            await pilot.pause()
            check(not app.query_one(SuggestBar).display,
                  "/resume 不该出现在补全里")

            app._submit_text("/resume 2")
            await pilot.pause()
            check(not fake.calls, "切会话的命令不该发给后端")
            last = str(app.query_one(Transcript).children[-1].render())
            check("桌面端" in last, "要说清去桌面端用：%r" % last)
    asyncio.run(scenario())


def test_btw_window() -> None:
    """/btw：整屏让给一个蓝框；回答进框里，顶行换成 done；Esc 关窗恢复。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            composer.focus()
            app._submit_text("/btw 今天天气怎么样")
            await pilot.pause()
            await wait_for(fake, 1)
            panel = app.query_one("#btw-panel", BtwPanel)
            check(panel.display, "发 /btw 之后窗口要开")
            check(app.query_one(Transcript).display is False, "转写区让位")
            check(composer.disabled, "输入框让位")
            check("正在回答" in panel.text(), panel.text())
            check(any(p == "/api/chat" and "今天天气" in str(d.get("text", ""))
                      for p, d in fake.calls), "要真的发给后端")

            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.TASK_END, response="今天晴，二十度。"))
            await pilot.pause()
            check("今天晴" in panel.text(), "回答要进框里：%r" % panel.text())
            top = str(app.query_one(StatusLine).render())
            check("done · " in top, "顶行换成完成读数：%r" % top)

            await pilot.press("escape")
            await pilot.pause()
            check(not panel.display, "Esc 应关窗")
            check(app.query_one(Transcript).display is True, "转写区恢复")
            check(not composer.disabled, "输入框恢复")
    asyncio.run(scenario())


def test_btw_defers_approval() -> None:
    """btw 窗开着的时候来了审批：先压住，关窗之后再送进来。"""
    pending = {"request_id": "r1", "kind": "cross_zone",
               "body": "要跨区搜索，批吗？",
               "options": [{"key": "allow", "label": "同意"},
                           {"key": "deny", "label": "不同意"}],
               "default_key": "allow"}
    async def scenario() -> None:
        fake = FakeBackend(pending)
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Composer)
            composer.disabled = False
            app._submit_text("/btw 顺带问一句")
            await pilot.pause()
            app._on_event(ev(E.APPROVAL_REQUEST, request_id="r1"))
            await pilot.pause()
            check(app._approval_deferred, "审批要先压住")
            check(app._approval is None, "窗口开着时不弹审批")
            check(not app.query_one(ApprovalPanel).display, "面板不该出现")

            await pilot.press("escape")
            await pilot.pause()
            check(app._approval is not None, "关窗后审批要送进来")
            check(app.query_one(ApprovalPanel).display, "面板要出现")
    asyncio.run(scenario())


def test_btw_orphan_and_recap() -> None:
    """窗关早了：回答带蓝色【btw】落回转写区；recap 单独一行。"""
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            transcript = app.query_one(Transcript)
            composer = app.query_one("#composer", Composer)
            composer.disabled = False

            # 1) 跑着的时候不许再开 btw 窗（那一问会被排到后面）。
            app._busy = True
            app._submit_text("/btw 插一句")
            await pilot.pause()
            check(not app.query_one("#btw-panel", BtwPanel).display,
                  "跑着的时候不该开窗")
            app._busy = False

            # 2) 开窗、没答完就 Esc：回答带【btw】落到转写区。
            app._submit_text("/btw 顺带问一句")
            await pilot.pause()
            await wait_for(fake, 1)
            await pilot.press("escape")
            await pilot.pause()
            check(app.query_one(Transcript).display is True, "转写区恢复")
            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.TASK_END, response="题外话的答案"))
            await pilot.pause()
            last = str(transcript.children[-1].render())
            check("【btw】" in last and "题外话的答案" in last,
                  "孤儿回答要带【btw】：%r" % last)
            check("done · " not in last, "btw 的回答不挂 done 行")

            # 3) `/recap` 的回复剥掉后端那句头，单独一行。
            app._submit_text("/recap")
            await wait_for(fake, 2)
            app._on_event(ev(E.TASK_START, label="x"))
            app._on_event(ev(E.TASK_END, response="#recap：我们刚在改打包链"))
            await pilot.pause()
            last = str(transcript.children[-1].render())
            check("回顾：我们刚在改打包链" in last, last)
            check("#recap" not in last, "后端那句头要剥掉：%r" % last)

            # 4) 自动回顾事件也走同一行。
            app._on_event(ev(E.RECAP, recap="打包链已经收尾"))
            await pilot.pause()
            last = str(transcript.children[-1].render())
            check("回顾：打包链已经收尾" in last, last)
    asyncio.run(scenario())


def main() -> int:
    test_composer_keys()
    test_numpad_keys()
    test_suggest()
    test_steer_and_stop()
    test_steer_fallback()
    test_delta_ignored()
    test_banner()
    test_approval_multi()
    test_approval_survives_task_end()
    test_status_refreshed_each_turn()
    test_header_shows_launch_cwd()
    test_compact_progress()
    test_context_readout_matches_gui()
    test_steer_line_turns_normal()
    test_queued_message_waits_yellow()
    test_attach_image()
    test_attach_image_error()
    test_link_targets()
    test_click_link_reveals()
    test_reveal_note()
    test_stream_down_banner()
    test_fault_banner()
    test_fault_banner_is_all_red()
    test_task_pills_nav()
    test_ctrl_b_background()
    test_header_scrolls_away()
    test_scroll_repaints()
    test_window_title()
    test_soft_breaks_hardened()
    test_update_hint()
    test_done_line()
    test_switch_commands_are_desktop_only()
    test_btw_window()
    test_btw_defers_approval()
    test_btw_orphan_and_recap()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
