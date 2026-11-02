"""
PipelineScheduler —— 顺序执行各阶段的调度器。

启动时发现所有已注册阶段，按 `STAGES_ORDER` 排序，并对每个到来的
:class:`~channels.base.MessageEvent` 按顺序执行。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable

from channels.base import MessageEvent
from core.session_key import session_key_for
from core import channel_health
from core.cancellation import clear as clear_cancel
from core.events import (EVT_APPROVAL_REQUEST, EVT_APPROVAL_RESOLVED,
                            EVT_CONTEXT_STATE, EVT_ERROR, EVT_FILES_CHANGED,
                            EVT_TASK_END, EVT_TASK_START)
from core.pipeline.stage import Stage, StageContext, registered_stages

def seed_platform_identity(ctx: StageContext, event: MessageEvent) -> None:
    """把平台身份填进 `ctx.metadata` —— 去重闸读的就是这两格。

    * `message_id` —— `pre_process` 那道去重闸的判据（`core/dedup.py`）。
    * `sender_id`  —— 没有平台消息号时，内容哈希那一支的分母。

    此前没有任何地方写它们，那道闸对每个通道都恒不触发
      （"防重复投递"四个字一直是空头承诺；钉钉那句"重复处理靠 msgId 去重兜着"
      也因此是假的）。
    通道没给就写空串 —— 与"没写"在这里同义（闸自己判非空），
      写进去只是让形状统一、让读的人不必猜"到底有没有这一格"。
    """
    ctx.metadata["message_id"] = str(
        getattr(event.message, "message_id", "") or "")
    ctx.metadata["sender_id"] = str(
        getattr(event.message, "sender_id", "") or "")


# 内置阶段的执行顺序。任何已注册但未列在这里的阶段，会在内置阶段之后运行。
STAGES_ORDER = [
    "PreProcessStage",
    # 无主理人群聊的发言层。它跑完会置
    #   `ctx.metadata["group_reply_done"]`，`AgentLoopStage` 看到就原地返回
    #   （PostProcess 照常落库）。
    "GroupReplyStage",
    "AgentLoopStage",
    "PostProcessStage",
]

# 并发控制
#: 并发控制是"每对话一把锁 + 进程级上限 5"：
#:   · 每对话一把锁 —— 同一对话的轮次串行（会话态：审批真值 / cwd / 清单
#:     不能交错）；
#:   · 上限 5 —— 名额按轮计，而一个对话一次只跑一轮（会话锁），等价于
#:     按对话计。
#: 获取顺序固定：先会话锁、后并发闸（见 `execute()`）—— 反过来会让
#: 在等自己会话的轮次占着并发名额，三个对话各排一条就能把第四个饿死。
_MAX_CONCURRENT_TURNS = 5
#: 活跃表最多记这么久（秒）。`_last_activity` 原来没有任何删除点。
#: 超过这个岁数的条目既不会被 `hot_sessions` 选中（它只问"最近谁活跃"），
#: 也没有别的读者 —— 留着只是让表一直长。
_ACTIVITY_TTL_S = 7 * 24 * 3600
#: 活跃表长到多少条才顺手扫一遍（大多数进程到不了这里，扫描成本≈0）。
_ACTIVITY_SWEEP_AT = 256
_turn_slots = threading.Semaphore(_MAX_CONCURRENT_TURNS)
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def _session_lock_for(session_key: str) -> threading.Lock:
    """取（必要时创建）某个对话的锁。

    `/clear` 时绝不丢锁：purge 是在持锁的那一轮里跑的，丢锁等于
    把正在持有的锁从表里抹掉，同一对话的下一个轮次会拿到新锁，与当前轮
    并行。

    复核过一次，结论是仍然不给删除点：能想到的判据只有
    "没人持有就删"，而 `get` 与 `acquire` 之间有一道缝 —— 正在等这把锁的
    线程拿到的对象会被换掉，同一对话于是真的并行起来。相比之下表本身很小
    （每条约几十字节，键 = 通道键），增长慢得多。-> 记在这里，别再"顺手加个清理"。
    """
    with _session_locks_guard:
        lk = _session_locks.get(session_key)
        if lk is None:
            lk = threading.Lock()
            _session_locks[session_key] = lk
        return lk


def _acquire_turn_slot(session_key: str) -> None:
    """拿并发名额；到顶时先出声再等（绝不静默排队）。

    除了一行日志，还会发一条事件（`turn.queued`），让用户看得见。
    只有 `print` 时，界面看起来就是"发了消息没反应"，而用户不会去看控制台。
    事件发送失败不许影响排队本身（观测不能阻断操作）。
    """
    if not _turn_slots.acquire(blocking=False):
        print("[sched] %s 排队中（并发上限 %d）" % (session_key, _MAX_CONCURRENT_TURNS))
        try:
            from core.events import (EVT_TURN_QUEUED,
                                     TURN_QUEUED_MESSAGE, get_event_bus)
            # 那句话跟着事件走 —— 如果两个前端（`chat.html` 与
            #    `App.jsx`）各写一份文案，同一个事实会漂成两种说法，而用户
            #    正是靠这一句被通知的。
            get_event_bus().emit(EVT_TURN_QUEUED, session_key=session_key,
                                 limit=_MAX_CONCURRENT_TURNS,
                                 message=TURN_QUEUED_MESSAGE)
        except Exception as exc:  # noqa: BLE001
            print("[sched] turn.queued 事件发送失败：%s: %s" % (type(exc).__name__, exc))
        _turn_slots.acquire()


def _origin_of(event: MessageEvent) -> str:
    """`"web"` | `"im"`。Web UI 用它决定最终回复由谁渲染。

    同一个答复会同时经 `/api/chat` 的 POST 响应和 `task.end` 事件到达
    前端，两边都渲染就会重复。前端据此规则二选一。
    """
    platform = str(getattr(getattr(event, "session", None), "platform", "") or "")
    return "web" if platform == "web" else "im"


# ------------------------------------------------------------------
# 上下文状态事件
# ------------------------------------------------------------------

#: 事件里最多携带几个文件。这不是"截断"：`count` 报的永远是真实总数
#: —— 报数，不报字符。
_MAX_FILES_IN_EVENT = 30


def _touched_payload(session_key: str) -> list:
    """本轮改过的文件，整理成前端需要的形状。`take` —— 取走并清空。

    用 `take` 而不是 `peek`：这份载荷只能带这一轮的文件；否则下一轮
    会把上一轮的也算进来（把先前的工作当成"这次的"）。

    `link` 由后端算好（前端不许自己猜路径）—— 它必须与
    `/api/download` 的边界一致；判错会给出一个点开就 403 的链接，比不给更坏。
    """
    from core.touched_files import drain, is_downloadable
    paths = drain(session_key)
    out = []
    for p in paths[:_MAX_FILES_IN_EVENT]:
        row = {
            "path": p,
            "name": p.rsplit("/", 1)[-1] or p,
            "link": bool(is_downloadable(p)),
        }
        # 这一轮改动对应的"改前"版本号。前端用它请求
        #   `/api/file-diff`，于是点开旧消息看到的是那一次的 diff，
        #   而不是最新一次的。
        # 0 = 没有留档（留档从本对话开始；见 `core/file_history.py`）。
        try:
            from core.file_history import latest_version
            row["version"] = latest_version(session_key, p)
        except Exception:                              # noqa: BLE001
            row["version"] = 0
        # 二进制文件（docx / xlsx / 图片 / 压缩包 …）没有"按行看改动"这回事，
        #   前端据此直接把名字接到资源管理器，不弹空浮窗。
        try:
            from pathlib import Path as _P
            from core.workspace import parser_hint_for
            row["binary"] = bool(parser_hint_for(_P(p).suffix)[0])
        except Exception:                              # noqa: BLE001
            row["binary"] = False
        # `+N/-M`：与点开看到的那份 diff 同源（`diff_for` 一次只算一次）。
        #   各算一处的话，列表上的数会与浮窗里的内容对不上。
        #   拿不到就不给（不要 `0` 兜底 —— 那看起来像"这个文件没变"）。
        if row["version"]:
            try:
                from core.file_history import COUNT_MAX_BYTES, diff_for
                d = diff_for(session_key, p, row["version"],
                             max_bytes=COUNT_MAX_BYTES)
                if d.get("added") is not None and d.get("removed") is not None:
                    row["added"] = int(d["added"])
                    row["removed"] = int(d["removed"])
            except Exception:                          # noqa: BLE001
                pass
        out.append(row)
    return out


def _context_state_payload(ctx, session_key: str) -> "dict | None":
    """构造一条 `context.state` 事件的载荷。

    全仓唯一构造这份载荷的地方。它在调度器轮末被调用一次，
    "每轮恰好一条"是结构性成立的，而不是靠记得在每个地方都发。

    为什么放在轮末而不是 `PostProcessStage`：`/compact` 在
    `pre_process` 就 `stop`，post_process 根本不跑 —— 而那恰恰是最需要
    一次上下文读数的一轮（用户刚压完，正想知道现在多大）。把发送点放进阶段
    里，要么漏掉它，要么抄第二份。

    `ctx.metadata["context_usage"]` 有就直接用 —— 那正是 post_process
    刚算出来的同一个 dict（同一个函数、同一份输入），普通轮不额外付
    分词成本。只有命令轮（post_process 没跑）才现算，而那些轮很少。

    粘住的旧值不许冒充新事实：压缩段只在"本轮真的压过"时才带，
    判据是 `ctx.metadata["compressed"]`（post_process 的自动压缩和 `/compact`
    命令都会写它）。`compressor.last_compression` 是粘的 —— 无条件带上
    它，读日志的人数出的"压缩次数"其实是轮数。
    """
    pool = getattr(ctx, "session_pool", None)
    if pool is None:
        return None

    # 记下这一轮实际注入的记忆 —— 上下文面板里"记忆注入"那一格的输入。
    # 只有这个点手上有 `ctx.memory_context`，而面板不能现算（唯一的
    # 入口 `get_context()` 会吃掉 `_pending_memory_inject`）。
    #
    # 只在非空时记：记忆是"首轮一次 + 压缩后再一次"，所以大多数轮它是
    # 空串。无条件记会让面板显示"记忆 = 0"—— 一个看起来像结论的错数。否则
    # 显示的是"上一次真正注入时它有多大"，这正是面板 `source` 所声明的。
    _mc = getattr(ctx, "memory_context", "") or ""
    if _mc:
        try:
            pool.note_memory_context(session_key, _mc)
        except Exception as e:
            print(f"[context] 记忆注入读数没记上（{type(e).__name__}: {e}）—— "
                  f"面板里那一格会停在上一轮的值")

    usage = ctx.metadata.get("context_usage")
    if not isinstance(usage, dict):
        usage = pool.get_context_usage(session_key, ctx.messages)

    payload = {
        "used": usage["estimated_tokens"],
        "window": usage["context_window"],
        "pct": usage["percentage"],
        "threshold_pct": usage["threshold_pct"],
        "threshold_tokens": usage["threshold_tokens"],
        "armed": usage["armed"],
        "window_source": usage["window_source"],
        "messages": len(ctx.messages),
        "factor": usage["factor"],
    }
    #: 这一轮消息表被裁过的原因（没裁过就没有这一格）。
    #: 只在真裁过时出现 —— 无条件带会让"裁过"变成每轮都有的噪声。
    try:
        from core import context_pressure as _cp
        _reason = _cp.take_trim(str(getattr(ctx, "storage_id", "") or ""))
        if _reason:
            payload["trim"] = _reason
    except Exception:                                          # noqa: BLE001
        pass

    if ctx.metadata.get("compressed"):
        stats = pool.last_compression or {}
        pre, post = stats.get("pre_tokens"), stats.get("post_tokens")
        payload["compressed"] = {
            "pre_tokens": pre,
            "post_tokens": post,
            "saved": (pre - post) if (pre is not None and post is not None)
                     else None,
            "mode": stats.get("mode"),
            "source": stats.get("source"),
            "turns": stats.get("compressed_turns"),
            "messages_before": stats.get("old_message_count"),
            "messages_after": stats.get("new_message_count"),
        }
    return payload


def _ensure_builtin_stages_imported() -> None:
    """延迟导入内置阶段，让它们自行注册。"""
    try:
        import core.pipeline.stages.pre_process  # noqa: F401
        import core.pipeline.stages.group_reply  # noqa: F401
        import core.pipeline.stages.agent_loop  # noqa: F401
        import core.pipeline.stages.post_process  # noqa: F401
    except ImportError:
        pass  # Stages will be registered when their modules are imported


#: `(store_kind, 属性名)` —— 待审批状态存在哪几格。
#:
#: 有三个读者依赖这张表：
#:   ① `pending_approval()`  —— 浮窗的渲染源（`GET /api/approval`）
#:   ② 轮首的 `_rid_before` —— 失效信号的"之前是什么"（含 kind）
#:   ③ 轮末的 `emit`        —— `approval.request` / `approval.resolved`
#:
#: 如果每个读者各存一份，那么加 `egress`/`command` 时，`StageContext`、工具、
#: 结算器和调度器投影都改了，这三处一个都没改，用户看到的症状是：
#: 聊天里写着"需要你确认执行这条命令 —— 见浮窗"，而浮窗永远不出现
#: （`GET /api/approval` 返回 `None`）。
#: 这是"要人记得更新的名单会烂"的活标本 —— 而且烂得很安静：没有任何
#: 报错，窗口只是不出现。
#:
#: 所以收成一处。加新 kind 只需改这张表；有断言按
#: `core/approval.py::_TABLES` 反查，漏登记会失败。
_PENDING_STORES: tuple[tuple[str, str], ...] = (
    ("cross_zone", "_pending_cross_zone"),
    ("delete", "_pending_delete"),
    # 批量删除硬闸 —— 自己一格。
    # 绝不与上面的 `delete` 共用：共用是受守卫命令栽过三次的形状。
    #    规则：一格只许一个 kind 用（按 `approval._TABLES` 反查）。
    # 漏了这一行 = 聊天里写着"见浮窗"而浮窗永远不出现 ——
    #    `pending_approval` 会去问一个永远没有值的格。
    ("delete_bulk", "_pending_delete_bulk"),
    # 出网与命令执行共用同一套派发/结算代码，但住在不同的格里
    # （`_pending_egress` / `_pending_command`）。
    ("egress", "_pending_egress"),
    ("command", "_pending_command"),
    # 兜底闸。
    ("destructive", "_pending_destructive"),
    # 计划批准。
    # 漏了这一行，`pending_approval` 会去问一个永远没有值的格 ——
    #    同样是"聊天说见浮窗、浮窗不出现"。
    ("plan", "_pending_plan"),
    # ICE 放行句柄背后的真值。
    # 漏了同样会坏。
    ("ice_reveal", "_pending_ice"),
    # 工作区之外写文件 —— 被审批物是目录。
    # 自己一格（一格只许一个 kind 用）。漏了同样会坏。
    ("write_outside", "_pending_write_outside"),
    # 提问面板 —— 模型向用户出的选择题。
    # 自己一格（一格只许一个 kind 用）。漏了就是聊天说"请你选一下"
    #    而浮窗永远不出现。
    ("ask_user", "_pending_ask"),
    # 接管的逐应用授权。自己一格（一格只许一个 kind 用）。
    ("computer_app", "_pending_computer_app"),
    # 钩子要求人工确认（`PreToolUse` 的 `ask`）。自己一格。
    # 漏了这一行同样坏：聊天/回执里写着要确认，而浮窗永远不出现。
    ("hook_ask", "_pending_hook_ask"),
)


class PipelineScheduler:
    """Orchestrates the ordered execution of pipeline stages.

    Usage::

        scheduler = PipelineScheduler(session_pool, provider, config)
        await scheduler.initialize()

        async def on_event(event: MessageEvent) -> None:
            response = await scheduler.execute(event)
            # deliver response to user ...
    """

    def __init__(
        self,
        session_pool,
        provider,
        config: dict | None = None,
    ) -> None:
        _ensure_builtin_stages_imported()

        # 按 STAGES_ORDER 排序；未列出的阶段排在最后
        def _sort_key(cls: type[Stage]) -> int:
            try:
                return STAGES_ORDER.index(cls.__name__)
            except ValueError:
                return len(STAGES_ORDER)

        self._stage_classes = sorted(registered_stages, key=_sort_key)
        self._stages: list[Stage] = []

        self._session_pool = session_pool
        self._provider = provider
        self._config = config or {}

        # 跨区审批状态
        # 按对话保存挂起的审批请求，让框架可以在不经过 LLM 的情况下拦截
        # 用户回答。
        self._pending_cross_zone: dict[str, dict] = {}
        # 按对话的删除审批免问标志
        self._session_skip_delete_approval: set[str] = set()
        # 按对话的跨区"本对话内不再询问"。真值在这里；
        # `wm.cross_zone_session_auto` 是它每轮的投影 —— 与上面的删除免问标志
        # 同形、同理由（读者在 skill 里，没有会话身份）。
        # 它不能并进 `wm.auto_approve`：那个字段会被
        # `to_settings_dict()` 写进 `settings.json`，会把一次对话授权变成
        # 持久设置。
        self._session_cross_zone_auto: set[str] = set()
        # 按对话、按 kind 统计"问了几次"（驱动升级项）。
        # 按 kind 分。出网与命令在 2 次升级、跨区在 3 次 —— 共用一个
        # 数字会让它们互相提前升级。
        #   `{session_key: {kind: count}}`
        self._approval_ask_counts: dict[str, dict[str, int]] = {}
        # 按对话、按 kind 的已批准目标集合（域名 / 收件人 / 命令）。
        # `"*"` = 全部放行。
        # 真值在这里；`wm._target_session` 是它每轮的投影 —— 与上面两个
        # `_session_*` 字段同形、同理由（读者在 skill/工具里，没有会话身份）。
        #   `{session_key: {kind: set(subject)}}`
        self._session_target_grants: dict[str, dict[str, set]] = {}
        # 按对话的命令工作目录。
        # 方向与上面几格相反：写手是工具
        # （`core/shell.py::run_command` 把 bash 回报的 `$PWD` 写进
        # `wm.session_cwd`），调度器轮末收回、轮首再投影出去。别的格是
        # "载体 → 真值"，这一格是"投影 → 真值"。
        #   `{session_key: "/abs/path"}`
        # 必须在对话维度上，不能做成进程级单值：本仓有前科（一个号称
        #   session 级、实际进程级的删除授权）。
        #   `{session_key: str}`
        self._session_cwd: dict[str, str] = {}
        # 按对话的待审批出网 / 命令。
        # 命令审批只有一档（守卫档已移除），所以这一格与它的 kind 今天
        # 是 1:1。
        # 读者仍必须从载荷取 kind（`pend["kind"]`）—— 见
        # `_PENDING_STORES`。
        self._pending_egress: dict[str, dict] = {}
        self._pending_command: dict[str, dict] = {}
        # 兜底闸（不可逆、且自身没有审批发射点的工具）。
        self._pending_destructive: dict[str, dict] = {}
        # 计划批准（退出计划模式的工具发出的载荷）。
        self._pending_plan: dict[str, dict] = {}
        # ICE 放行真值（放行工具发出的载荷）。
        self._pending_ice: dict[str, dict] = {}
        # 审批定时器不在这张表上 —— 它们住在
        # `core/approval_timeout.py::_timers`（模块级）。
        # 理由：`/clear` 的清理走 `state_registry._lazy(module, func)`，而它
        #    只认模块级函数；scheduler 是实例，放这里就清不掉，上一段
        #    对话的倒计时会继续跑，并对新一段对话的审批开火。
        # 按对话的待审批删除
        self._pending_delete: dict[str, dict] = {}
        # 批量删除硬闸 —— 自己一格（绝不与 `_pending_delete` 共用）。
        # 这个属性与 `_PENDING_STORES` 里那一行必须成对存在：有行没属性
        #    会在下一轮抛
        #    `AttributeError: 'PipelineScheduler' object has no attribute …`。
        self._pending_delete_bulk: dict[str, dict] = {}
        # 工作区之外写文件 —— 自己一格（同上成对规则）。
        self._pending_write_outside: dict[str, dict] = {}
        #: 提问面板 —— 自己一格（同上成对规则）。
        self._pending_ask: dict[str, dict] = {}
        # 接管的逐应用授权。
        self._pending_computer_app: dict[str, dict] = {}
        #: 钩子要求人工确认 —— 自己一格（同上成对规则）。
        self._pending_hook_ask: dict[str, dict] = {}

        # 活跃表 —— 每个对话最后一次收到消息的单调时间。IM 选址用它判断
        # "最近只有一个活跃，直连 / 多个，问编号"。
        # 只在内存里：重启后为空，0 个活跃，走默认键。这是正确行为，
        #    不是缺陷 —— "猜最近那个"正是它取代掉的 bug。
        self._last_activity: dict[str, float] = {}

        # 进度事件流
        # 默认取进程级单例；测试可替换替身（scheduler.events = FakeBus()）。
        from core.events import get_event_bus
        self.events = get_event_bus()

        # 可选的生命周期回调 —— 由宿主（Web UI / CLI）设置，用于显示忙碌状态。
        # Web UI 已改为从事件流折叠 busy/task 状态（事件还带会话身份），不再
        # 使用这两个钩子。签名保持不变，以免破坏其他宿主。
        self.on_task_start: Callable[[str], None] | None = None
        self.on_task_end: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # IM 选址的两个辅助
    # ------------------------------------------------------------------

    def _session_exists(self, key: str) -> bool:
        """编号/绑定指向的对话还在不在？（不在就报错，而不是静默接到别处。）

        判不出来，当成存在（fail-open）：目的是报告指向已消失对话的
        绑定；数据库不可达时把一切编号都判死，只会让路由整体停摆。这个
        fail-open 方向是有意的，与安全闸的 fail-closed 不是同一类。

        这里的 `key` 是通道键，而
        `session_pool.session_exists()` 会先走 `_resolve_sid` 再查存储面 ——
        "通道问、存储答"，这正是它该有的形状。
        要记住：它只问不建（`_resolve_sid` 可能加载/建对话）。它唯一的
        用处是 `im_routing.route` 里那两处"已存在的绑定还指不指得过去"——
        两处都只问已存在的键，从不问新键，行为保持等价，不会无声漂移。
        """
        try:
            return bool(self._session_pool.session_exists(key))
        except Exception:
            return True

    def _title_of(self, key: str) -> str:
        """选址列表里的一行人话：对话最近一条用户消息的前 24 个字符
    （取不到就报键）。"""
        try:
            return self._session_pool.session_preview(key, 24) or key
        except Exception:
            return key

    def _project_session_flags(self, session_key: str, user_text: str = "") -> None:
        """把按对话的审批真值投影到 workspace manager。

        真值都在本调度器上（按对话）；`wm` 上的字段是它们的投影，因为唯一的
        读者在 skill 里、没有会话身份。投影在每轮开局刷新，所以换一个对话
        来问，它看到的就是 False / 0 / 空。

        投影失败不许静默 —— 打出来 —— 而且失败方向必须安全
        （退化成"再问一次"，绝不退化成"不再问"）。

        安全方向由轮末的清理保证，不是由这个函数保证。只写这里不够：
        这个函数自己也会失败（registry / wm 拿不到），失败时字段会保留上一轮的
        值 —— 对话 A 答了"不再询问"，True，接着 B 的投影失败，B 继承了 True。

        `WorkspaceManager.clear_approval_projection()` 提供保证：它在轮末把
        投影字段清成安全值，本函数在轮首再写回真值。于是投影失败退化成
        "全都关着"（= 再问一次），绝不会"继承上一轮"。

        下面逐字段的 try/except 只保证一次写入失败不影响其他字段。
        """
        try:
            from bus.registry import get_workspace_manager
            wm = get_workspace_manager()
        except Exception as exc:
            print(f"[pipeline] approval projection could not reach the workspace "
                  f"manager ({type(exc).__name__}: {exc}) — this turn will ask again")
            return

        if wm is None:
            return

        # 盖属主章 —— 下面这些字段是进程级单份，并发轮次会交错投影；
        # 读侧（`wm._projection_is_mine`）靠这枚章判断"这份投影是不是当前
        # 对话的"。必须在写字段之前写（没有快照时，cwd 的写入章会回退
        # 到它）。
        wm._projected_for = session_key

        # 按对话的真值 → wm 上的投影。逐字段独立：一次失败不影响其他。
        for name, value in (
            ("skip_delete_approval",
             session_key in self._session_skip_delete_approval),
            ("cross_zone_session_auto",
             session_key in self._session_cross_zone_auto),
            ("approval_ask_counts",
             dict(self._approval_ask_counts.get(session_key, {}))),
            # 本轮是谁在问 —— 让 `wm` 能比对一次性授权的属主
            # （见 `WorkspaceManager.has_approval()`）。这一条每轮都变。
            ("approval_scope", session_key),
            # 命令工作目录（"框架记住 cwd"）。
            # 空串也必须写 —— 它表示"这个对话没有记住的目录"，工具的
            # `_resolve_cwd` 会退化成工作区。跳过它会让上一轮的目录留在
            # `wm` 上（"只增不减"的形状）。
            ("session_cwd", self._session_cwd.get(session_key, "")),
            # 本轮的用户消息 —— 出网闸要问"这个域名是用户点过名的
            #   吗"（只允许访问用户以某种形式提供过的域名）。
            #   必须走投影：工具处理器的签名是 `handler(**tool_args)`，
            #   看不到 `ctx`。
            #   所以与 `session_cwd` / `approval_scope` 同一条规矩：轮首写、
            #   轮末随 `reset_session_projections()` 清。每轮都变
            #   （就是本轮那一句）。
            ("turn_user_message", str(user_text or "")),
        ):
            try:
                setattr(wm, name, value)
            except Exception as exc:
                print(f"[pipeline] projection of {name} failed "
                      f"({type(exc).__name__}: {exc}) — this turn will ask again")

        # 按目标、按对话的放行。不并进上面的循环 —— 它不是标量赋值，而是
        # 整组替换（`set_target_session`），每个 kind 调一次。
        #
        # 必须整组替换，而且每个 kind 都要过一遍：只投影出现过的 kind
        #    会让上一次投影留下的 kind 继续活着 —— "只增不减"的形状。
        #    先把这个对话的全部 kind 清掉，再写回真值。
        #
        # 两个来源在这里合流：
        #   · `_session_target_grants` —— 本对话内批准（选项 2，内存）
        #   · `core.allowlist`         —— 持久白名单（选项 5 / 设置页 /
        #                                 手改文件）
        # 合流的原因：工具侧只需要问一个问题"这个目标能不能过"，不该关心许可
        # 是这次谈成的还是以前留下的。
        try:
            from core.allowlist import (KINDS as _WL_KINDS,
                                        SESSION_KINDS as _SESS_KINDS,
                                        list_for as _wl_list)
            truth = self._session_target_grants.get(session_key, {}) or {}
            # 两张表一起遍历。只遍历 `KINDS`（"egress"/"command"）会让
            # 只存在于会话作用域的 kind（例如 `destructive`）永远不被投影
            # 所以 `has_target_grant("destructive", …)` 恒为 False，"本对话内
            # 不再询问"不生效，浮窗反复出现。
            # 所以 `KINDS`（有文件域）+ `SESSION_KINDS`（仅会话级）。
            for kind in tuple(_WL_KINDS) + tuple(_SESS_KINDS):
                # 每轮现读，不缓存 —— 手改文件必须在下一轮生效。文件
                # 不到 1 KB。
                persisted = set(_wl_list(kind))
                wm.set_target_session(
                    kind, set(truth.get(kind, set())) | persisted)
                # **单独报告来自文件的 `*`** —— `has_target_grant` 的一个
                #   布尔说不清命中的是全局开关还是具体某一条，而只有裸 `*`
                #   （开关）才应该走分类器。
                #   判据是它来自哪里：这里报 `persisted`（磁盘上那份），
                #   不含用户在本对话里当场批准的（选项 5）—— 那些仍然短路。
                wm.set_target_wildcard(kind, "*" in persisted)
        except Exception as exc:
            print(f"[pipeline] projection of target grants failed "
                  f"({type(exc).__name__}: {exc}) — this turn will ask again")

    # ------------------------------------------------------------------
    def pending_approval(self, session_key: str) -> dict | None:
        """这个对话当前挂着的待审批 —— 浮窗的渲染源。

        从 state 读，绝不从事件重建（存活性只能来自对当前进程的观察）。
        一旦回答，载体就被清空，这里返回 `None`，没有幽灵浮窗；
        刷新/重连只是重新读一次。

        IM 对话也可以调用 —— 返回 `None` 或一个 payload。前端不需要
        知道对话身份：它只问"现在有没有要审的"。
        """
        from core.approval import build_payload, render_delete_body

        for _store_kind, attr in _PENDING_STORES:
            pend = (getattr(self, attr) or {}).get(session_key) or {}
            if not pend.get("active"):
                continue
            # 真正的 kind 取自载荷，不取自格的键。今天 kind 与格是
            #   1:1，所以这一行看不出差别 —— 但只要一个格能装两个 kind，
            #   "格的键"与"公开的 kind"就会分叉，而那个格的每个读者都得记得
            #   去载荷里取。不要删。
            kind = str(pend.get("kind") or _store_kind)

            stage = str(pend.get("stage") or "ask")
            # 优先使用工具已经拼好的正文（`pending_payload` 的
            #   `body`）。命令/出网这些 kind 退到 `_DEFAULT_BODY`，只会渲染到
            #   冒号就停。
            # 修法不是在这里重新拼 —— 那会变成第二个渲染源。
            body = str(pend.get("body") or "")
            if kind == "delete" and not body:
                # 使用工具拼好的列表，不要重新渲染。
                #
                # 在这里重新渲染会产生第二个渲染源，而且没有 >10 的
                # 折叠逻辑（那一半在 `skills/file_parser/handler.py`）。
                # 后果：12 个文件时，IM 通道列 5 个 + "另外 7 个"，而浮窗列出
                # 全部 12 个 —— 同一个审批两份正文。
                #
                # 这个兜底只在"载荷里还没有 `file_list` 的旧挂起"上走到。
                #    它用的是同一个策略函数，只是少了文件大小 —— 所以
                #    折叠规则仍然只有一个来源。
                file_list = str(pend.get("file_list") or "")
                if not file_list:
                    from core.approval import compact_file_list
                    names = [os.path.basename(str(p))
                             for p in (pend.get("file_paths") or [])]
                    file_list = compact_file_list(names)
                body = render_delete_body(file_list)
            return build_payload(
                kind,
                int(pend.get("ask_index")
                    or self._approval_ask_counts.get(session_key, {}).get(kind, 0)
                    or 1),
                body=body, stage=stage,
                request_id=str(pend.get("request_id") or ""),
                # 命令域的前缀规则（空 = 整条精确匹配）—— 浮窗用它把
                #   选项 2 渲染成用户看得懂的规则。
                rule=str(pend.get("rule") or ""),
                # 提问面板的选项表是动态的（由模型提供），不是固定的
                #   `_TABLES` 之一 —— 原样传下去（选项真值只住在挂起载荷里，
                #   一处）。
                options=pend.get("options") or None,
                multi=bool(pend.get("multiSelect")),
                # 提问面板的标题与原始问题 —— 不带的话，浮窗会拿
                #   `body`（带编号的菜单）当问题，把选项抄两遍。
                title=str(pend.get("header") or ""),
                ask_question=str(pend.get("question") or ""),
            )
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _rid_pair(snapshot: dict, store_kind: str) -> tuple:
        """从轮首快照里取 `(request_id, kind)`。形状不对就当"轮首没有"，
        绝不抛异常。

        为什么不能直接 `rid_before.get(k, (None, k))`：`.get` 的默认值
        只在键缺失时生效 —— 键存在但值是 `None` 时它照样返回 `None`，
        然后 `before, before_kind = None` 抛 `TypeError`。

        这个异常不是无害的：它跑在 `_execute_serial` 的轮末、
        `event.reply_callback(event)` 之前，而且没有 try，一旦抛出，
        用户这一轮收不到任何回复。

        所以与 `core/message_history.py::_call_ids` 同一条判据：
        一个发信号的辅助函数因为输入形状不对而崩掉，是最坏的失败方向。
        把坏形状当成"轮首没有"，代价最多是少发一条 `resolved`（噪声），
        绝不会吞掉用户的回复（事故）。

        它还挡住一种无声的坏法：如果 `before` 是长度为 2 的字符串
        （`"ab"`），`a, b = "ab"` 会不报错地解成 `'a'`/`'b'` —— 错得看不
        出来。所以这里显式校验，不信任 Python 的解包。
        """
        val = snapshot.get(store_kind) if isinstance(snapshot, dict) else None
        if isinstance(val, (tuple, list)) and len(val) == 2:
            return val[0], val[1]
        return None, store_kind

    def emit_approval_signals(self, bus, session_key: str,
                              rid_before: dict) -> None:
        """轮末发审批的失效信号：`approval.request` / `.resolved`。

        *rid_before* 是轮首拍下的 `{store_kind: (request_id, kind)}` 快照。

        信号必须在"它让读者去取的那份 state"可读之后才发（即持久化
        之后）。发早了，前端会立刻 `GET /api/approval`、读到空、浮窗永不出现。
        这不是"慢一拍"，而是"永远错"。

        `kind` 取自轮首快照，不取自当前载荷：结算会清空载荷
        （`pend.clear()`），于是 `pend.get("kind")` 取不到，代码会退到格的键
        —— 同一条审批的 `resolved` kind 就会与 `request` 不同。轮首把 kind
        一起拍下来，在这里使用。

        把它抽成方法，理由与 `persist_turn_carriers` 相同：住在
        `execute()` 里时测试只能碰到它的副本 —— 而副本正是反复出现的失败形状。
        """
        for _store_kind, attr in _PENDING_STORES:
            store = getattr(self, attr) or {}
            before, before_kind = self._rid_pair(rid_before, _store_kind)
            pend = store.get(session_key) or {}
            now = pend.get("request_id")
            # 有载荷就用载荷的 kind（那是当下的真相）；
            # 载荷已经不在（结算掉了）就用轮首快照的那个。
            pub = str(pend.get("kind") or before_kind or _store_kind)

            # 两条 `if`，不是 `if/elif`。
            #
            # 一轮可以同时"结算旧的"和"创建新的"。用 `elif` 的话，前者
            # 永远不会被发出，那条 `request` 就悬在日志里（验证过一天内
            # 19 条 request 对 14 条 resolved；差的 5 条正是这么来的）。
            #
            # 暴露它的序列：批准一次 → 挂起被消费、命令执行 → 同一轮里又发问，
            # 建了新的 request。第一条 request 永远等不到 `resolved`。
            #
            # 所以判据：两件都是真事就两条都发。顺序是"先结束、后开始"。
            #
            # 这个"做差"设计有一个结构性盲区：同一个 request_id 在
            # 一轮之内被结算又重建（`before == now`）时，两条都不发。
            # 今天不会发生，因为 `new_request_id()` 用 `uuid4()`（每次发问都
            # 必须不同；重问同一条命令也会得到新 id）。如果那个函数哪天变成
            # 确定性 id，这里就会漏。
            if before and before != now:
                bus.emit(EVT_APPROVAL_RESOLVED, session_key=session_key,
                         kind=str(before_kind or pub), request_id=before)
            if now and now != before:
                bus.emit(EVT_APPROVAL_REQUEST, session_key=session_key,
                         kind=pub, request_id=now)

    # ------------------------------------------------------------------
    # 审批超时 —— auto 档下"没人可答"时的出路
    # ------------------------------------------------------------------
    #
    # 它解决什么：auto 档下，分类器答不了的挂起会被退回给人。没有超时，
    # 这些挂起就没有上限 —— 用户走开之后会发现这一轮直接停住了。
    # 具体规则、范围、超时结算成什么，都在 `core/approval_timeout.py`；
    # 这一节只做三件事：arm · cancel · fire。
    #
    # **为什么用 `loop.call_later` 而不是 `threading.Timer`**：挂起状态是在
    #   `execute()` 里产生的，那里手上就有正在运行的 loop；六个通道各用
    #   不同的 loop（飞书那个属于第三方阻塞栈），用线程就得知道该投到哪个
    #   loop。`call_later` 既不需要这个知识，也没有跨线程机制。

    def _remember_event(self, session_key: str, event) -> None:
        """轮首记住那条真事件 —— 超时之后要靠它的 `reply_callback`
        把模型收尾的那句话送出去（伪造事件没有回复通道）。"""
        from core import approval_timeout as _AT
        _AT.remember(session_key, event)

    def _sync_approval_timer(self, session_key: str) -> None:
        """轮末调一次，幂等：这个对话现在该不该有倒计时？

        arm 与 cancel 住在一处 —— 轮末时"新建"与"结算掉"是同一个事实
        （当前有没有待审批？）。分开写，迟早有人只改一半（要人记得更新的
        名单会烂）。
        """
        import asyncio
        from core import approval_timeout as _AT

        if self.pending_approval(session_key) is None:
            _AT.cancel(session_key)                  # 结算掉了 / 本来就没有
            return
        try:
            loop = asyncio.get_running_loop()
        except Exception:                            # noqa: BLE001
            return
        if _AT.arm(session_key, loop, self._fire_approval_timeout):
            print("[approval-timeout] 有待审批 —— %d 秒后"
                  "按「没有可问的通道」结算（`/clear` 或回答即撤）"
                  % _AT.seconds(session_key))

    def _fire_approval_timeout(self, session_key: str) -> None:
        """到点了 —— 拿真事件重入 `execute`，走与用户回答同一条路。

        重入前再确认一次"还没被回答"：用户可能在最后一刻答了，而
        `call_later` 回调与那一轮排在同一个 loop 上，顺序由 loop 决定。
        """
        import copy
        import asyncio
        from core import approval_timeout as _AT

        _AT.mark_fired(session_key)
        if self.pending_approval(session_key) is None:
            return                                    # 已经答过了 / 撤了
        event = _AT.event_of(session_key)
        if event is None:
            print("[approval-timeout] 到点了，但没记住那条事件 —— "
                  "**不做任何事**（回今天的行为：继续等）")
            return
        try:
            evt = copy.copy(event)
            evt.extras = dict(event.extras or {})
            evt.extras["approval_timeout"] = True
            evt._stopped = False
        except Exception as exc:                      # noqa: BLE001
            print("[approval-timeout] 造超时事件失败（%s）—— 不做任何事"
                  % type(exc).__name__)
            return
        try:
            asyncio.ensure_future(self.execute(evt))
            print("[approval-timeout] %s：到点，按「没有可问的通道」结算"
                  % session_key)
        except Exception as exc:                      # noqa: BLE001
            print("[approval-timeout] 重入失败（%s）—— 不做任何事"
                  % type(exc).__name__)

    # ------------------------------------------------------------------
    def load_pending_into_ctx(self, ctx, session_key: str) -> None:
        """轮首把挂着的待审批从按对话的存储投影进 `ctx`。

        与 :meth:`persist_turn_carriers` 严格对称，并且只从
          `_PENDING_STORES` 派生 —— 加一个 kind 只需改那张表，别处不用动。
          （这里手写 kwargs 漏掉四格时，用户看到的症状是"审批浮窗问了我
          三次"。）

        只投影 `ctx` 本来就有的字段（`hasattr`）：`ice_reveal` 那一格
          今天不走 `ctx`（见 `_PENDING_STORES`），为它造一个动态属性会改变
          行为。不要随手这样做。
        是同一个 dict，不是拷贝 —— 结算器的 `_drop_pending` 是
          `pend.clear()`，两边必须看到同一个对象（"清空之前抓取"的前提）。
        """
        for _kind, _storename in _PENDING_STORES:
            _attr = _storename[1:]
            if not hasattr(ctx, _attr):
                continue
            _pend = (getattr(self, _storename) or {}).get(session_key)
            if _pend is not None:
                setattr(ctx, _attr, _pend)

    def persist_turn_carriers(self, ctx, session_key: str) -> None:
        """轮末把 `ctx` 上的载体收回按对话的真值表。

        所有载体共用一条规则：载体为真就记录；为假（`False` / `0` /
        `{}` / 空串）就丢掉。这不是风格问题 —— "只增不减"会让
        授权在 `/clear` 之后活到下一段对话。

        `plan` 与其余载体一样走这个循环：它的被审批物就是计划本身。
        所以同一条规则：载体为假就 `del`。

        为什么抽成方法：让测试直接调用生产代码；测试若抄一份逻辑
        去做断言，那份副本就是第二套真相源。
        """
        # 待审批载体 —— 从 `_PENDING_STORES` 派生，绝不手抄
        #
        # 在这里手写第二张表会漂：它容易漏掉 `ice_reveal` 那一格，
        #    而漏项本身不会报错。
        #    所以规则：一个真相，一个地方。格名单住在 `_PENDING_STORES`；
        #    这段代码只许派生：加一个 kind 只改那一张表。
        #    漏一条的失败形状是 ——聊天说"见浮窗"，浮窗永远不出现
        #    （载荷没被存下来，`pending_approval` 查的是一个空格）。
        #
        # `ctx` 上的属性名 = 格名去掉前导下划线（`_pending_delete` →
        #    `pending_delete`）。取不到就说明这个 kind 不走 `ctx`（今天只有
        #    `ice_reveal` —— 那是它自己的设计，不是这张表的漏洞）。
        for _kind, _storename in _PENDING_STORES:
            _attr = _storename[1:]
            _store = getattr(self, _storename)
            _pend = getattr(ctx, _attr, None)
            if _pend:
                _store[session_key] = _pend
            elif session_key in _store:
                del _store[session_key]

        # 三个布尔/集合
        # 同一条规则：载体为真就记录，为假就丢掉。
        # `/clear` 把这些载体置为 False/0，所以这些 `elif` 真的会触发，而不是
        # 只增不减。
        if ctx.session_skip_delete:
            self._session_skip_delete_approval.add(session_key)
        elif session_key in self._session_skip_delete_approval:
            self._session_skip_delete_approval.discard(session_key)

        if ctx.session_cross_zone_auto:
            self._session_cross_zone_auto.add(session_key)
        elif session_key in self._session_cross_zone_auto:
            self._session_cross_zone_auto.discard(session_key)

        if ctx.approval_ask_counts:
            self._approval_ask_counts[session_key] = dict(ctx.approval_ask_counts)
        elif session_key in self._approval_ask_counts:
            del self._approval_ask_counts[session_key]

        # 按目标的对话授权（载体 → 真值）
        # 过滤掉空集合：`{k: set()}` 是真值，不过滤的话，一个"看起来有、
        #    其实什么都没有"的对话会留在表里 —— 下一轮投影会写一堆空集合，
        #    看着没问题，却永远恢复不出任何权限。
        _tg = {k: set(v) for k, v in (ctx.session_target_grants or {}).items() if v}
        if _tg:
            self._session_target_grants[session_key] = _tg
        elif session_key in self._session_target_grants:
            del self._session_target_grants[session_key]

        # 命令工作目录（从投影收回真值）
        # 方向与上面几格相反：写手是工具（它看不到 `ctx`，只能写
        # `wm.session_cwd` 这个投影），调度器在轮末把它收回对话真值表。
        # 空，删除，不是记一个空串 —— 与上面 `_tg` 同一条规则
        # （`/clear` 把 `wm` 上那格置空，这一句就是让清除生效的地方）。
        _cwd_now = ""
        _cwd_mine = False
        try:
            from bus.registry import get_workspace_manager as _get_wm
            _wm_now = _get_wm()
            # 读原始格 + 写入章（不走 property 的读闸）：并发下必须
            # 分清"读闸返回空"与"真的空"，否则别人的投影会让这一句删掉本
            # 对话的真值。不是写给我的，一个字节都不动。
            _cwd_now = str(getattr(_wm_now, "_session_cwd", "") or "")
            _cwd_mine = (getattr(_wm_now, "_cwd_written_by", "") == session_key)
        except Exception:
            pass
        if _cwd_mine:
            if _cwd_now:
                self._session_cwd[session_key] = _cwd_now
            elif session_key in self._session_cwd:
                del self._session_cwd[session_key]

    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """实例化所有已注册的阶段。

        这里是"框架启动了"的唯一一处 —— 两个入口都只调它，所有必须
        随框架启动的东西挂在这里，没有任何入口会漏掉（写两处迟早漏一处）。

        监听器自启动放在 `try` 里，失败只报告不抛 —— 一个目录监听器
        起不来，不是禁止框架启动的理由。
        它不在这里重新读设置：`watchers` 的 `enabled` / `auto_start`
        已经由 `WorkspaceManager` 从 `settings.json` 读进来了，再读一遍就是
        第二个真相源。
        """
        self._stages = [cls() for cls in self._stage_classes]
        self._apply_configured_model()
        #: 三档代理落到环境变量上 —— 只认 env 的 SDK（飞书 `lark_oapi`
        #:   建 WS 时裸调 `requests.post`）才吃得到那张口径表。
        #:   放在这里是因为这是"框架启动了"的唯一一处（两个入口都过它）。
        try:
            from core import net_proxy as _np
            _changed = _np.apply_env_policy()
            if _changed:
                print("[proxy] 「%s」档已落到环境变量：%s"
                      % (_np.mode(), ", ".join(_changed)))
        except Exception as _e:                                   # noqa: BLE001
            print("[proxy] 环境变量口径没落下去（只认 env 的 SDK 会跟随系统）："
                  "%s: %s" % (type(_e).__name__, _e))
        #: 附件回收：后台线程 + 一天一次（`attachment_gc.maybe_prune_daily`）。
        #:   判据是"启动是唯一不会漏的入口"（同监听器自启动那一段），
        #:   而扫描/删文件不许挡住启动，放线程里、异常只报告。
        try:
            import threading
            from core.attachment_gc import maybe_prune_daily

            def _gc_bg() -> None:
                try:
                    st = maybe_prune_daily()
                    if st and st.get("deleted"):
                        print("[attachments] 回收 %d 个未被引用的附件（%d 字节）"
                              % (st["deleted"], st["freed"]))
                    elif st and st.get("skipped"):
                        print("[attachments] 本次未回收（%s）" % st["skipped"])
                except Exception as exc:                   # noqa: BLE001
                    print("[attachments] 回收失败（不影响框架）：%s: %s"
                          % (type(exc).__name__, exc))
                #: 框架自己的运行目录（计划 / 工具结果 / 通道收件媒体 /
                #:   任务日志）原来谁都不收。挂在启动这一下 ——
                #:   "启动是唯一不会漏的入口"（同上一条判据）。
                try:
                    from core.retention import prune_runtime_dirs
                    prune_runtime_dirs()
                except Exception as exc:                   # noqa: BLE001
                    print("[retention] 运行目录回收失败（不影响框架）：%s: %s"
                          % (type(exc).__name__, exc))

            threading.Thread(target=_gc_bg, name="attachment-gc",
                             daemon=True).start()
        except Exception as exc:                           # noqa: BLE001
            print("[attachments] 回收线程没起来（不影响框架）：%s: %s"
                  % (type(exc).__name__, exc))
        try:
            from bus.registry import get_workspace_manager
            wm = get_workspace_manager()
            if wm is not None:
                wm.autostart_watchers()
        except Exception as exc:                  # noqa: BLE001
            print("[scheduler] 监听器自启动失败（不影响框架启动）：%s: %s"
                  % (type(exc).__name__, exc))

    def _apply_configured_model(self) -> None:
        """把"默认启用"那条配置条目接成这个进程使用的 provider。

        为什么放在这里（而不是 `main.py` 与 `web/server.py` 各写
          一遍）：这里是"框架启动了"的唯一一处 —— 两个入口加上
          `/api/restart`（它在同一进程里再调一次 `initialize()`）都会经过
          这里，写两处迟早漏一处。

        任何失败都不改变现状（fail-open）：没有默认条目 / 装不上，
          沿用 `config.yaml` 的 provider（构造时已注入），行为零变化。
          这也是它可以直接放在主路径上的原因。

        它现在只管"进程默认"这一格 —— 每轮真正生效的 provider 来自
          :meth:`_resolve_turn_model`，它问的是"这一段冻的是哪条配置"。
          本函数仍然要跑，有两个理由：
            ① 它是尚未冻结的对话的起点（第一轮用的就是它）；
            ② 它是所有没有会话身份的调用脚下的那一格（启动自检、群成员
              / 子 agent 的 `ensure_provider` 兜底）。

        关于"不热插拔"：在有轮次运行时替换进程级字段，会让它们
          观察到半新半旧的状态（压缩窗口 / 闸门 / token 估算都读它，
          而且最多有 5 轮并发），这是最难归因的故障之一。但"所以必须重启"
          只从"一个进程只有一个 provider"这个前提推出来 —— 按对话查实例
          不需要这个前提。

          实例查找现在按对话进行（`MemoryManager._provider_for`），所以
          改默认根本不替换任何实例：它只改变尚未冻结的对话下一次捕获到
          的东西。运行中的对话继续读自己冻结的那条配置，不存在半新半旧
          （同一段内的轮次串行，不同段本来就应当不同）。
        """
        try:
            from core import model_configs as _mc
            cid = _mc.default_id()
            if not cid:
                return
            inst = _mc.provider_for(cid)
            if inst is None:
                return
            self._provider = inst
            try:
                from bus.registry import get_memory_manager
                mgr = get_memory_manager()
                if mgr is not None:
                    # 顺序承重：先换实例，再报告阈值 —— 否则报告的数字属于
                    #   旧窗口（来源必须跟着值走）。
                    mgr._provider = inst
                    mgr.report_thresholds()
            except Exception as exc:              # noqa: BLE001
                print("[scheduler] 记忆层换模型失败（它仍用原窗口）：%s: %s"
                      % (type(exc).__name__, exc))
        except Exception as exc:                  # noqa: BLE001
            print("[scheduler] 模型配置装载失败，沿用 config.yaml 那个模型：%s: %s"
                  % (type(exc).__name__, exc))

    @staticmethod
    def _group_host_provider(session_key: str):
        """群对话，主理人配置的模型对应的 provider；不是群 / 没配置 /
        装不上，`None`。

        没有这一条，群轮会用进程默认模型，主理人的 `model` 设置只影响
          派活 —— 只要两个模型不同，"用户是在跟主理人说话"就是假的。

        它不冻结 `session_model`：主理人的模型可以在群设置里更改，
          冻结之后就再也不会跟着变，每轮现解析（`instance_for` 按
          `updated_at` 缓存实例，代价可忽略）。
        用户为这一段显式选过的模型优先 —— 那发生在 `resolve()` 里；
          非空时根本走不到这个函数。
        """
        key = str(session_key or "")
        prefix = "workspace:group:"
        if not key.startswith(prefix):
            return None
        try:
            from core import groups as _groups
            from core import model_configs as _mc
            g = _groups.get(key[len(prefix):])
            if not g:
                return None
            host = next((m for m in g.get("members") or [] if m.get("host")), None)
            ref = str((host or {}).get("model") or "").strip()
            if not ref:
                return None
            entry = _mc.get(ref) if _mc.is_id(ref) else _mc.find_by_ref(ref)
            cid = str((entry or {}).get("id") or "")
            if not cid:
                return None
            return _mc.instance_for(cid, fallback=None)
        except Exception as exc:                                # noqa: BLE001
            print(f"[scheduler] 群主理人的模型解析失败，沿用进程那个："
                  f"{type(exc).__name__}: {exc}")
            return None

    def _resolve_turn_model(self, session_key: str):
        """这一轮该用哪条配置、哪个 provider。

        返回 `(config_id, provider instance)`；`config_id == ""` 表示
        "跟随默认"（此时 provider 就是进程那个，行为零变化）。

        三个判断各自住在一个地方：归一化在 `core/session_model._norm`，
        "该用哪条"在 `session_model.resolve`，"拿实例"在
        `model_configs.instance_for` —— 本函数只负责串联。

        装不上，返回 `("", 进程 provider)`，也就是"这一轮不冻结"。
        返回 `cid` 会让轮末冻结一条根本跑不起来的配置，这一段会永久卡在
        "解析到 A、实际跑 B"，改默认也救不回来。宁可这一轮不冻结，让它下次
        跟随默认。
        """
        try:
            from core import model_configs as _mc
            from core import session_model as _sm
            cid = _sm.resolve(session_key)
            if not cid:
                #: 有主理人的群对话，用主理人配置的模型
                #:   （用户是在跟主理人说话）。`("", inst)` = 用这个实例，但
                #:   不冻结（见 `_group_host_provider`）。
                host_inst = self._group_host_provider(session_key)
                if host_inst is not None:
                    return "", host_inst
                #: "跟随默认"意味着取当前默认条目，不是"启动时接上的
                #:   那个实例"。直接返回 `self._provider` 会让配置页上的改动
                #:   （能力 / 地址 / 模型）只有重启框架后才生效。
                #:   仍返回 `("", inst)` —— 跟随默认不冻结；改变的只是
                #:   "默认"的含义：从"启动快照"变成"此刻那一条"。
                _default_id = _mc.default_id()
                if _default_id:
                    _default_inst = _mc.instance_for(_default_id, fallback=None)
                    if _default_inst is not None:
                        return "", _default_inst
                return "", self._provider
            inst = _mc.instance_for(cid, fallback=None)
            if inst is None:
                print(f"[scheduler] 这一段冻的配置 '{cid}' 今天装不上 —— "
                      f"这一轮沿用原模型（且**不冻**，下一轮仍跟默认走）")
                return "", self._provider
            return cid, inst
        except Exception as exc:                  # noqa: BLE001
            print("[scheduler] 这一段模型解析失败，沿用进程那个：%s: %s"
                  % (type(exc).__name__, exc))
            return "", self._provider

    # ------------------------------------------------------------------
    def is_busy(self, session_key: str) -> bool:
        """这个会话现在有一轮在跑吗（后台通知不抢正在发生的对话）。"""
        return _session_lock_for(session_key).locked()

    def idle_seconds(self, session_key: str) -> float:
        """上一次活动之后过了多久。从没活动过，无穷大（可以起）。"""
        last = self._last_activity.get(session_key)
        return float("inf") if last is None else time.monotonic() - last

    def activity_snapshot(self) -> dict:
        """近活跃会话 `{键: 单调时间}` 的只读副本。

        存在的理由：通道要判"这条消息的目标会话唯不唯一"，
          而那份判据住在 `core/im_routing.py::hot_sessions`。
          别让通道自己去摸 `_last_activity` —— 那是本类的私有字段，
            摸的人一多，改它的形状就没完没了。
        """
        return dict(self._last_activity)

    async def execute(self, event: MessageEvent) -> str:
        """跑一轮 —— 每对话一把锁 + 进程级上限 5。

        同一对话的轮次串行（会话态：审批真值 / cwd / 清单不能交错）；
        不同对话并行。一个全局 `_EXECUTE_LOCK` 会让两条消息 ——
        "在桌面上做一半、走开用微信继续" —— 互相排队。到达上限时，
        `_acquire_turn_slot` 打一行日志并等待。

        并发让两个进程级字段必须具备归属检查（否则会在对话之间串味）：
          · `request_snapshot` —— 现在是 `ContextVar`（任务作用域，自动清理）；
          · `wm` 上的投影字段（审批 / cwd）—— 盖 `_projected_for` 章，
            读侧由 `_projection_is_mine()` 检查（见 `core/workspace.py`）。

        这里不能直接 `with lock`：同一条 loop 上两个协程时，持锁者的
        `await` 会让出，另一个协程调 `acquire` 会把整个线程堵死 —— 持锁者
        再也回不来。这就是死锁。所以两次获取都走 `run_in_executor`：等待发生在
        线程池线程上，事件循环照常运行。

        获取顺序固定：先会话锁、后并发闸 —— 反过来，一个在等自己对话的
        轮次会占着并发名额（三个对话各排一条就能把第四个饿死）。
        """
        # 通道心跳（`core/channel_health`）：六个通道共用这一个入口，
        #   一处就能记下"这条通道最后一次真的把消息送进管线是什么时候"。
        #   判据见那个模块：`running: true` 与"真的在收"在界面上必须分得开。
        channel_health.note(str(getattr(event.session, "platform", "") or ""))
        # IM 选址：只对 IM；要么直连、要么扣住问编号、要么重放扣住的
        # 消息。`None` = 不处理（Web / CLI / 框架命令 / 已改派过）。
        try:
            from core import im_routing
            decision = im_routing.route(
                event, self._last_activity, self._title_of, self._session_exists)
        except Exception as exc:  # noqa: BLE001
            print("[routing] 选址失败（这一条按默认键走）：%s: %s"
                  % (type(exc).__name__, exc))
            decision = None
        if decision is not None and decision.kind == "ask":
            text = decision.question
            event.extras["response"] = text
            if event.reply_callback:
                try:
                    event.reply_callback(event)
                except Exception:
                    pass
            return text
        if decision is not None and decision.kind == "direct" and decision.key:
            event.extras["routed_session_key"] = decision.key
            # 只有改派过才贴头（与默认键相同就没什么可告知的）。
            if decision.key != session_key_for(event.session):
                event.extras["routing_header"] = True
        if decision is not None and decision.kind == "replay" and decision.event is not None:
            event = decision.event          # 重放扣住的那条（回复天然回它自己的渠道）

        session_key = event.extras.get("routed_session_key") or session_key_for(event.session)
        _now = time.monotonic()
        self._last_activity[session_key] = _now
        if len(self._last_activity) > _ACTIVITY_SWEEP_AT:
            _cutoff = _now - _ACTIVITY_TTL_S
            for _k in [k for k, t in self._last_activity.items() if t < _cutoff]:
                self._last_activity.pop(_k, None)
        loop = asyncio.get_running_loop()
        lock = _session_lock_for(session_key)
        await loop.run_in_executor(None, lock.acquire)
        try:
            await loop.run_in_executor(None, _acquire_turn_slot, session_key)
            try:
                return await self._execute_serial(event)
            finally:
                # "接管"有两个状态 —— 只有模型操作电脑时才 `armed`
                #    （物理输入会退出），并且轮末关闭，好让用户继续输入
                #    下一条指令。只 disarm 持有者的那一轮。
                try:
                    from core.computer import session as _cu
                    _cu.disarm_for(session_key, "turn_end")
                except Exception as exc:             # noqa: BLE001
                    print("[computer] 轮末关闭操作检测失败：%s: %s"
                          % (type(exc).__name__, exc))
                _turn_slots.release()
        finally:
            lock.release()

    async def _execute_serial(self, event: MessageEvent) -> str:
        """对 *event* 跑完所有阶段（一轮中的串行部分）。

        返回最终回复字符串（没有产生回复时为空串）。
        """
        # 从用户消息派生一个简短的任务标签
        user_text = event.message.content or ""
        task_label = user_text[:30].replace("\n", " ") + (
            "..." if len(user_text) > 30 else ""
        )

        # session_key 必须在 `try` 之前算出来：`on_task_start` 钩子如果在它
        # 算出来之前触发，就拿不到会话身份（多通道路由到同一个 scheduler
        # 单例时无法区分），而 `finally` 里的 `task.end` 也需要它。这是事件流
        # 能按对话路由的前提。
        # 会话键只在一处推导 —— 私聊跨通道共用一条键；如果路由层已经改派过
        # （扣住重放 / 指定），以它为准。
        session_key = event.extras.get("routed_session_key") or session_key_for(event.session)
        # 轮首记住真事件 —— 超时之后要靠它的 `reply_callback` 送出模型
        #   收尾的那句话（见 `_remember_event`）。
        try:
            self._remember_event(session_key, event)
        except Exception:                             # noqa: BLE001
            pass                                      # 记不住最坏 = 超时不重入，不弄挂这一轮
        bus = self.events
        _t0 = time.monotonic()
        ctx: StageContext | None = None      # ← finally 的守卫

        # 新一轮开始：把上一条消息留下的错误红条换成绿条（`retry_ok`）。
        # 判据：那是"上一轮的结果"，不该跟着这一段对话一直挂着；
        #   而这一轮真失败了会在下面重新挂上一条新的。
        try:
            from core import llm_retry as _lr
            _lr.clear(session_key)
        except Exception:                                # noqa: BLE001
            pass

        if bus is not None:
            bus.emit(EVT_TASK_START, session_key=session_key,
                     label=task_label, origin=_origin_of(event))

        if self.on_task_start:
            try:
                self.on_task_start(task_label)
            except Exception as exc:
                print(f"[pipeline] on_task_start hook failed: "
                      f"{type(exc).__name__}: {exc}")

        try:
            # 把这个对话的审批真值投影到 WM 上
            #
            # 真值是本调度器上的按对话容器；workspace manager 上的字段是
            # 投影，每轮刷新一次，因为唯一的读者在 skill 里、没有会话身份
            # 可用来限定作用域。所有读审批标志的地方都问这一份投影。
            #
            # 没有投影时，用户回答"2"会直接写进程级全局，而没有任何地方
            #    把它改回 False —— 提示语承诺"本对话内不再询问"，而同一进程里
            #    以后每段对话都会静默继承它。跨区标志同形：一个任何会话都能
            #    消费的进程级布尔。
            self._project_session_flags(session_key, user_text)

            # 这一轮用哪条配置 / 哪个 provider
            # 必须在构造 `ctx` 之前解析 —— `provider=` 就是它的产物。
            # 任何解析失败都回退到进程 provider（fail-open，与
            #   `_apply_configured_model` 相同）：行为零变化。
            _mcid, _turn_provider = self._resolve_turn_model(session_key)

            # 框架代笔的 user 消息（后台任务回报 / 定时任务）：
            #    它们会落成 `role=user` 的行，于是重新加载时会长成一个用户气泡
            #    （重启后聊天记录里冒出一条绿色气泡，内容是
            #    "后台任务回报 · 这是框架转来的任务结果…"）。
            #    所以打一个 `kind`，显示侧据此不收它（`is_conversation_row`）；
            #    模型那一侧一个字节不变（装载按 role 读，不按这个 kind）。
            _origin = str((getattr(event, "extras", None) or {}).get("origin") or "")
            _u_kind = "framework_note" if _origin in ("background", "cron") else ""

            ctx = StageContext(
                session=event.session,
                user_message=user_text,
                message_chain=event.message.message_chain,
                # 所有阶段共用的依赖
                session_pool=self._session_pool,
                provider=_turn_provider,
                #: 轮末的惰性冻结冻的就是这一格。
                model_config_id=_mcid,
                config=self._config,
                pending_cross_zone=self._pending_cross_zone.get(session_key, {}),
                pending_delete=self._pending_delete.get(session_key, {}),
                # Egress / command execution
                pending_egress=self._pending_egress.get(session_key, {}),
                pending_command=self._pending_command.get(session_key, {}),
                # Fallback gate
                pending_destructive=self._pending_destructive.get(session_key, {}),
                # Plan approval
                pending_plan=self._pending_plan.get(session_key, {}),
                session_skip_delete=session_key in self._session_skip_delete_approval,
                session_cross_zone_auto=session_key in self._session_cross_zone_auto,
                approval_ask_counts=dict(self._approval_ask_counts.get(session_key, {})),
                # 载体的初值 = 上一轮的会话真值（本轮若有人批准，结算会往里加）。
                session_target_grants={
                    k: set(v) for k, v in
                    (self._session_target_grants.get(session_key, {}) or {}).items()},
                # 进度事件流
                events=bus,
                event=event,
            )
            if _u_kind:
                ctx.metadata["user_message_kind"] = _u_kind
            seed_platform_identity(ctx, event)
            # 把挂着的待审批投影进 `ctx` —— 与轮末的
            #     `persist_turn_carriers` 严格对称，并且从 `_PENDING_STORES`
            #     派生（一个真相，一个地方）。
            #     手工只传部分格子，会让 `delete_bulk` / `write_outside` /
            #     `ask_user` / `computer_app` 从来进不了 ctx，用户点了
            #     "允许"，`_dispatch_approval` 在 `ctx` 上找不到它，把那句话
            #     当普通消息喂给模型，模型又调一次工具，浮窗再次出现。
            self.load_pending_into_ctx(ctx, session_key)

            # 轮首快照审批身份
            # 必须在阶段运行之前取，而且取的必须是 `request_id`
            # 字符串。
            # 两个坑：
            #   1. `ctx.pending_*` 与 `self._pending_*[key]` 是同一个 dict
            #      （构造 ctx 时用 `dict.get`，不是拷贝），结算的
            #      `_drop_pending` 会原地清空它。放在阶段循环之后取，读到的
            #      永远是已清空的 `None` —— `resolved` 就永远不会发。
            #   2. 存字符串，不存 dict：dict 会被原地修改，字符串不会。
            #
            # **快照是 `(request_id, kind)` 一对，不只是 request_id。**
            #    同一条审批可能产生 kind 与 `request` 不同的 `resolved`。
            #    根因：结算时载荷已经被清空（`_drop_pending` =
            #    `pend.clear()`），`pend.get("kind")` 取不到，代码退到了格的
            #    键。
            # 所以轮首把 kind 一起拍下来，轮末使用它 —— 不要再去问一个已经不
            #    存在的载荷。
            _rid_before = {
                _store_kind: (
                    (getattr(self, attr) or {}).get(session_key, {}).get("request_id"),
                    str((getattr(self, attr) or {}).get(session_key, {}).get("kind")
                        or _store_kind),
                )
                for _store_kind, attr in _PENDING_STORES
            }

            for stage in self._stages:
                try:
                    ctx = await stage.process(ctx)
                except Exception as exc:
                    stage_name = type(stage).__name__
                    exc_name = type(exc).__name__
                    exc_msg = str(exc)
                    print(f"[pipeline] ERROR in {stage_name}: "
                          f"{exc_name}: {exc_msg}")
                    if bus is not None:
                        bus.emit(EVT_ERROR, session_key=session_key,
                                 stage=stage_name, exc_name=exc_name,
                                 message=exc_msg)
                    ctx.metadata["stop"] = True
                    ctx.metadata["error"] = exc_msg
                    ctx.metadata["failed_stage"] = stage_name
                    # 这一轮以错误结束 —— 通知钩子（只收它的 context）。
                    try:
                        from core import hooks as _hooks
                        _hooks.stop_failure(getattr(ctx, "session_id", "") or "",
                                            stage_name, exc_msg)
                    except Exception:                          # noqa: BLE001
                        pass
                    # 用户可见的错误文案只有一处
                    # if/elif 链住在这里，会让流式路径完全没有它 ——
                    # 同一个故障在两条路上看起来不同（`(error: Connection
                    # error.)`）。
                    # 所以搬进 `core/llm_errors.explain`，两条路共用。
                    # 401 / 400 / 429 的现有措辞逐字保留（用户认得出）。
                    try:
                        from core import llm_errors as _lle
                        _prov = getattr(ctx, "provider", None)
                        ctx.response = _lle.explain(
                            exc_msg, exc_name=exc_name, stage=stage_name,
                            model=str(getattr(_prov, "_model", "") or ""),
                            base_url=str(getattr(_prov, "_base_url", "") or ""))
                    except Exception:                            # noqa: BLE001
                        ctx.response = (
                            f"⚠️ 内部处理异常 [{stage_name}]: {exc_name}"
                        )
                    break

                if ctx.metadata.get("stop"):
                    break

            # 去重账本：没成功就把位子放回去
            # `pre_process` 占位、`post_process` 走完才落账；中间任何一条失败路径
            # （模型超时 / 阶段抛异常 / 中途 `stop`）走到这里时 `dedup_done` 都没有
            # 所以放位，平台重投时还能被正常处理。
            # 旧行为是"在最前面就记账"，一次失败之后，重投被自己的账本挡下，
            # 而账上写着"处理过了"（用户什么都没收到）。
            _ded = (ctx.metadata or {}).get("dedup")
            if _ded and not ctx.metadata.get("dedup_done"):
                try:
                    from core.dedup import default as _dedup_default
                    _dedup_default().release(**_ded)
                except Exception as exc:                # noqa: BLE001
                    print(f"[dedup] 放位失败（{type(exc).__name__}: {exc}）"
                          f" —— 这条消息的重投可能被当成重复")

            # 轮末收口：还在重试的清掉（否则 `/api/status` 每 5 秒
            # 把它读回来，网络恢复了红条也不消失）；失败收尾的留着 ——
            # 那是给用户看的结果，下一条消息发出时由 `clear()` 换成绿条。
            try:
                from core import llm_retry as _lr
                _lr.reset_if_retrying(session_key)
            except Exception as exc:                            # noqa: BLE001
                print(f"[llm] 重试状态清理失败（{type(exc).__name__}: {exc}）"
                      f" —— 下一轮的横幅可能还挂着")

            # 轮末：把 `ctx` 上的载体收回按对话的真值表
            # 抽成方法是为了让测试能调用真的那份：测试若抄这 60 行
            #    去断言，就是在维护第二套真相源。现在测试问的是生产代码。
            self.persist_turn_carriers(ctx, session_key)

            # 上下文状态
            # 这是它唯一的发送点。放在轮末而不是阶段里，是为了覆盖
            # 所有路径：普通轮 / 命令轮（`/compact`、`/clear`、
            # `/model`…）/ 报错轮 —— 后两类 post_process 不会跑。
            #
            # 失败路径不许静默：打一行，让人看得见。但它绝不能弄挂
            # 这一轮：走到这里时模型已经跑完，一条读数不该让用户丢掉工作。
            if bus is not None:
                try:
                    _ctx_state = _context_state_payload(ctx, session_key)
                except Exception as exc:                 # noqa: BLE001
                    print(f"[context] 状态读数失败 "
                          f"({type(exc).__name__}: {exc}) —— 这一轮没有"
                          f"上下文事件")
                    _ctx_state = None
                if _ctx_state is not None:
                    bus.emit(EVT_CONTEXT_STATE, session_key=session_key,
                             **_ctx_state)


            # 审批失效信号
            # 必须在持久化之后发，而且必须在这里发 —— 不能在
            # `_check_cross_zone_pending` 里发。原因是观察到的顺序问题：
            #
            #   在阶段里发信号 → 前端立刻 GET /api/approval → 读
            #   `self._pending_*` → 还没持久化，读到空 → 浮窗永不出现
            #   （用户只能手动打 "1" 才完成审批）
            #
            # 规则：只有在"它让读者去取的那份 state"可读之后，才能发信号。
            # 这不是"提前一拍"—— 提前发的信号一定读到空，永远是错的。
            if bus is not None:
                self.emit_approval_signals(bus, session_key, _rid_before)

            # 审批超时：arm / cancel 都在这里
            # 规则：轮末"现在有没有待审批"就是全部事实 —— 有就挂倒计时，没有
            # 就撤销（用户刚答过，正是这一轮撤销它）。
            # 放在 `emit_approval_signals` 之后：那次调用是让前端去读
            #   state，能读到之后才谈得上倒计时；它的撤销语义也同源。
            try:
                self._sync_approval_timer(session_key)
            except Exception as exc:                  # noqa: BLE001
                print("[approval-timeout] 同步定时器失败（%s）—— 这一轮不受影响"
                      % type(exc).__name__)

            # 回复头只加给 IM，而且只加在被改派过的轮次 —— 前端看得见
            # 自己在哪个任务里，不需要头；而且只加在送达文本上，不加进
            # 落库副本（post_process 已经存完了）。
            if event.extras.get("routing_header") and _origin_of(event) == "im":
                ctx.response = "【%s】\n%s" % (self._title_of(session_key), ctx.response)

            # 调用通道的回复回调
            #: 按行送达（`/resume` / `/change` 的列表）：微信客户端会把
            #:   换行折成空格，十行列表糊成一张墙。
            #:   这个标记只给通道看；Web/CLI 不看它，行为一个字节不变。
            if ctx.metadata.get("deliver_lines"):
                event.extras["deliver_lines"] = True

            if event.reply_callback:
                event.extras["response"] = ctx.response
                try:
                    event.reply_callback(event)
                except Exception:
                    pass

            # 在无主理人群聊里，成员回复不是 `ctx.response`
            #   （那是"主持人的一句话"）—— 它们是几条独立的可见消息。Web 侧
            #   在这里拿到它们。
            if ctx.metadata.get("group_replies") is not None:
                event.extras["group_replies"] = ctx.metadata["group_replies"]

            # 本轮产出的产物（图 / 音 / 视频）。与 `group_replies`
            #   同一条路：走 `event.extras`（不是新事件）—— `/api/chat` 的
            #   响应与 `task.end` 读的是同一份。
            if ctx.metadata.get("artifacts") is not None:
                event.extras["artifacts"] = ctx.metadata["artifacts"]

            # 这一轮是用户打断收尾的（`/api/chat` 从 `extras` 读；
            #   SSE 那条走 `EVT_TASK_END` 的关键字参数 —— 两条都要有）。
            event.extras["cancelled"] = bool(ctx.metadata.get("cancelled"))

            # 这一轮是不是停在等审批上。
            #   模型挂起之后一个字都不会说，而回复是空的并不等于"它没回答"
            #   —— 前端据此不画那条空气泡（面板本身就是信号）。
            #   判据取自权威 state（存着的那条待审批），不是"回复为空"。
            try:
                event.extras["awaiting_approval"] = (
                    self.pending_approval(session_key) is not None)
            except Exception:                        # noqa: BLE001
                event.extras["awaiting_approval"] = False

            return ctx.response

        finally:
            # 轮末清一次性授权（必须放在 `finally` 里）
            #
            # 如果放在主 `try` 体里，那条路径上漏出的任何异常都会跳过
            # 这个块，而它清的三样东西都只活一轮：
            #   * `reset_one_time_approval()`  —— 跨区一次性授权
            #   * `reset_delete_approval()`     —— 删除一次性授权
            #   * `clear_approval_projection()` —— 四个投影字段
            # 跳过的后果是 `_one_time_delete_approval` 变成进程级布尔，同一对话
            # 下一轮的删除完全不问。
            #
            # 放在 `finally` 里的理由与下面那段相同：这个清理必须每轮发生一次，
            # 而 `finally` 正是表达这件事的地方 —— 正常返回、阶段抛异常、提前
            # break 都会走到。
            #
            # 不要把它挪回 `try` 体：有断言故意在那里抛异常，并要求清理仍然
            # 发生。
            try:
                from bus.registry import get_workspace_manager
                wm = get_workspace_manager()
                if wm:
                    wm.reset_one_time_approval()
                    wm.reset_delete_approval()
                    # 按目标授权里的一次性那一半（投影那一半由下面的
                    # `clear_approval_projection()` 清）。
                    wm.reset_target_grants()
                    # 同时把投影字段清成安全值。下一轮开局会写回真值；
                    # 但如果那次投影自己失败，留下的就是这里的"全关"，而不是
                    # 上一个对话的值。见 `clear_approval_projection`。
                    wm.clear_approval_projection()
            except Exception:
                pass

            # 清中断标志：在轮末，不在轮首
            #
            # 在轮首清是错的：用户点了中断，标志被设上，但下一轮会无条件
            # 抹掉它，而上一个循环可能还没读到，中断静默失效。标志没有"轮次
            # 身份"，所以只能在这一轮结束时清。
            #
            # 三种情况因此都正确：
            #  · 轮中被读到，循环消费并返回（那里也会再清一次）；
            #  · 设得太晚没被读到，这一轮正常结束，在这里清掉，不影响后续轮次；
            #  · 没有轮次在跑时被设上，它不会被下一轮开局抹掉，但那一轮开头的
            #    检查会读到它 —— 用户确实要求过停下，停下来并告知是安全的。
            clear_cancel(session_key)

            # 收件箱取空
            #
            # 这一轮结束了，而排队的那几句话里可能有一句没赶上步的边界
            # （走到最后一步之后才到 / 这一轮被刹住了）。它们必须在这里取干净：
            #  · 留着，下一次对话的第一轮会凭空出现一句"上一段说的话"；
            #  · 丢掉，用户打的那句话没了（比多一句更坏）。
            # 所以取出来交给 `task.end`，前端用同一条气泡接着跑一轮。
            #   （形状与 Cherry 的 `steer-undelivered` 同：回落成排队，不是消失。）
            _steer_left: list = []
            try:
                from core import steer as _steer_inbox
                _steer_left = _steer_inbox.drain(session_key)
            except Exception as exc:                 # noqa: BLE001
                print(f"[steer] 轮末取空失败（{type(exc).__name__}: {exc}）"
                      f" —— 这一轮没有 steer_leftover")
            # 两条出口都要有：`extras` 给 `/api/chat`，关键字参数给 SSE。
            try:
                event.extras["steer_leftover"] = _steer_left
            except Exception:                        # noqa: BLE001
                pass

            # 轮末事件。它住在 `finally` —— 无论正常返回、阶段抛异常还是提前
            # break，都恰好发一次。
            if bus is not None:
                bus.emit(
                    EVT_TASK_END,
                    session_key=session_key,
                    ok=bool(ctx is not None
                            and not ctx.metadata.get("error")),
                    duration_ms=int((time.monotonic() - _t0) * 1000),
                    tokens=(ctx.tokens_used if ctx is not None else 0),
                    response=((ctx.response if ctx is not None else "") or ""),
                    # 这一轮一共调了几次工具。
                    # 这里的 `.get(..., 0)` 不是"读不到 = 0"：在没有跑
                    #   agent 循环的轮次（`/clear`、`/` 命令…）上，工具调用数
                    #   真的是 0 —— 那是事实，不是兜底。
                    tool_count=((ctx.metadata.get("tool_count", 0)
                                 if ctx is not None else 0)),
                    # 这一轮落库的是哪两行
                    #   （`{"user": <行号>, "assistant": <行号>}`）。
                    #   聊天消息上的星标依赖它 —— 收藏存的是引用。
                    #   拿不到是 `None`，不是 `{}`：前端按"有没有值"
                    #   决定要不要点亮。
                    turn_rows=((ctx.metadata.get("turn_rows"))
                               if ctx is not None else None),
                    # 这一轮是不是 `/clear` 命令轮？
                    #   前端据此不把回执落进聊天气泡 —— 否则它会渲染成
                    #   "窗口上下文已清空……"，那是框架的界面文案，看起来像
                    #   "模型突然说了句话"。
                    #   它同时是"清空聊天区"的信号：`/clear` 换了新段，
                    #   旧段就不该继续留在屏幕上。
                    #   这里的 `.get(...)` 不是兜底：每一轮要么真的清过，
                    #   要么真的没清 —— 两者都是事实。
                    #   没有 `ctx`（一个阶段都没跑），`False` —— 绝不猜成
                    #   `True`：那会让前端吞掉一条真回复。
                    cleared=bool(ctx is not None
                                 and ctx.metadata.get("cleared")),
                    # 这一轮属于哪一段对话。
                    #   红点与"排队中"标记要点在左栏的某一行上，而名册按
                    #   `storage_id` 键 —— `session_key` 分不出段：私聊在
                    #   所有通道上塌成同一条键（`workspace:private`）。
                    #   所以没有这一格，前端只知道"有人跑完了一轮"，不知道是哪段。
                    #   拿不到是空串（没有阶段跑过）—— 不是 `None`：
                    #   空串就是"这一轮不属于任何段"那个事实（`/clear` 轮）。
                    storage_id=((ctx.storage_id if ctx is not None else "") or ""),
                    # 这一轮产出的图 / 音 / 视频（没有就是 `None`，不是
                    #   `[]` —— 读不到不是空）。
                    artifacts=(ctx.metadata.get("artifacts")
                               if ctx is not None else None),
                    # 这一轮是不是"用户打断"收尾的？
                    #   前端据此把 `response` 标成回执（不是模型说的话）：
                    #   用户发下一句时撤掉它、且不让它抢"最后一条消息"的身份
                    #   （否则活着的步骤流永远不算最后一条，假「正在处理」）。
                    #   与 `awaiting_approval` 不同，它两条路都要有：
                    #   走 `extras`（`/api/chat` 读）+ 这里的关键字参数（SSE 读）
                    #   —— 缺一条，先到的那条就没标记，去重后留下的正是没标的。
                    cancelled=bool(ctx is not None
                                   and ctx.metadata.get("cancelled")),
                    # 这一轮没赶上的 steer：`[{id, text}]`。
                    #   前端拿它用同一条气泡接着跑一轮 —— 不许丢、不许留给下一轮。
                    #   空列表 = "这一轮没有多余的"（那是一个事实，不是兜底值）。
                    steer_leftover=_steer_left,
                    origin=_origin_of(event),
                )

            # 本轮改过的文件
            # 必须排在 `task.end` 之后 —— 如果更早发，前端此时活动框
            #   还开着，会把这一条并进上一个框，而它需要的是该条消息
            #   下方一个独立的下拉框。
            #   这属于"在 `task.end` 之后到达的事件"：前端必须自己收口。
            #
            # 用 `take`，不是 `peek` —— 它只能带这一轮的文件；
            #   否则下一轮会把上一轮的也算进去。
            # 失败要出声，但绝不能弄挂这一轮。
            if bus is not None:
                try:
                    _files = _touched_payload(session_key)
                except Exception as exc:                 # noqa: BLE001
                    print(f"[files] 改动清单发送失败 "
                          f"({type(exc).__name__}: {exc}) —— 这一轮没有"
                          f"改动文件事件")
                    _files = None
                if _files:
                    bus.emit(EVT_FILES_CHANGED, session_key=session_key,
                             files=_files, count=len(_files))
            if self.on_task_end:
                try:
                    self.on_task_end()
                except Exception as exc:
                    print(f"[pipeline] on_task_end hook failed: "
                          f"{type(exc).__name__}: {exc}")
