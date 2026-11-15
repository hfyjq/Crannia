"""
Pipeline Stage ABC + StageContext.

A Stage processes a :class:`~channels.base.MessageEvent`.  Stages are
executed in order by :class:`PipelineScheduler`.  Three built-in stages
are provided out of the box.

To add a custom stage, subclass `Stage` and call `register_stage`:

    @register_stage
    class MyStage(Stage):
        async def process(self, ctx: StageContext) -> StageContext:
            ...
            return ctx
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from channels.base import MessageSession
    from channels.message import MessageChain


# ------------------------------------------------------------------
# Stage registry
# ------------------------------------------------------------------

registered_stages: list[type["Stage"]] = []


def register_stage(cls: type["Stage"]) -> type["Stage"]:
    """Decorator: register a Stage subclass for pipeline execution."""
    registered_stages.append(cls)
    return cls


# ------------------------------------------------------------------
# StageContext — data flowing through pipeline stages
# ------------------------------------------------------------------

@dataclass
class StageContext:
    """Mutable context passed between pipeline stages.

    Shared dependencies (session_pool, provider, config) are set by
    the PipelineScheduler and available to all stages.

    Per-turn data (session, messages, response, etc.) is populated
    by earlier stages and consumed by later ones.
    """

    # ---- set by scheduler before each pipeline run ----
    session: "MessageSession"
    user_message: str              # plain-text fallback (backward compat)
    #: 这一轮允许调用的工具（来自 `allowed-tools` 声明）。None = 不限制。
    #: 只在 `/命令` 那一轮生效；下一条普通消息不带限制。
    tool_allowlist: "frozenset[str] | None" = None
    session_pool: Any = None       # SessionPool
    provider: Any = None           # LLM provider client
    config: dict = field(default_factory=dict)
    pending_cross_zone: dict = field(default_factory=dict)  # cross-zone approval state
    pending_delete: dict = field(default_factory=dict)      # delete approval state
    #: 批量删除的硬闸 —— 工具自己判定
    #: 「这次要删的个数 > 阈值」时升上来的那一次。
    #: 它有自己的格，不与 `pending_delete` 共用 —— 共用会让
    #: 「不同意」被读成「同意」，无限审批循环；这条判据记在
    #: `core/approval.py` 的注释里。
    #: 判据一句话：一格只许一个 kind 用。
    pending_delete_bulk: dict = field(default_factory=dict)
    #: 工作区之外写文件 —— 被审批物是一个目录
    #: （`pend["subject"]`）。判据：工作区外写，走审批。
    #: 同样自己一格（判据同上面 `pending_delete_bulk` 那条：
    #: 一格只许一个 kind 用）。公开的 kind 一律取 `pend["kind"]`。
    pending_write_outside: dict = field(default_factory=dict)
    #: 提问面板 —— 模型主动向用户出的选择题。
    #: 与上面那一族共用挂起/结算的地基，但语义不同：
    #: 审批 = 求授权（同意之后重放工具、真的去做）；提问 = 问意见
    #:答案就是那次调用的结果（`approval_replay["result"]`），不重放。
    #: 自己一格（判据同 `pending_delete_bulk`：一格只许一个 kind 用）。
    pending_ask: dict = field(default_factory=dict)
    #: 接管的逐应用授权（逐应用 · 每会话授一次）。被审批物是 exe 名
    #: （`pend["subject"]`）。
    #: 自己一格（判据同 `pending_delete_bulk`：一格只许一个 kind 用）。
    pending_computer_app: dict = field(default_factory=dict)
    #: 钩子要求人工确认（`PreToolUse` 的 `permissionDecision: "ask"`）。
    #: 被审批物是这一次工具调用（`pend["subject"]` = 工具名）。
    #: 自己一格（判据同 `pending_delete_bulk`：一格只许一个 kind 用）。
    #: 与其余审批的差别：结算不发授权 —— 重放直调工具 handler，
    #:   `PreToolUse` 根本不会再跑（见 `_settle_hook_ask`）。
    pending_hook_ask: dict = field(default_factory=dict)
    #: 出网审批（`web_fetch` / `send_email` 的目标）。
    pending_egress: dict = field(default_factory=dict)
    #: 执行审批（`run_command`）。
    #: 命令的审批只有一种档位（没有「危险档」）——
    #: 所以这一格只装 `command` 一种 kind。但读者仍然取自载荷
    #: （`pend["kind"]`），因为这是"格"而不是"kind"的表。
    pending_command: dict = field(default_factory=dict)
    #: 兜底闸 —— 不可逆、而**没有自己的审批
    #: 发射点的工具。被审批物是工具名**（`pend["subject"]`）。
    #: 与上面四格的区别见 `core/approval.py::DESTRUCTIVE_OPTIONS` 的注释：
    #: 那四条问的是"这个目标批过没有"，这一条问的是"这个工具有没有发射点"。
    pending_destructive: dict = field(default_factory=dict)
    #: 计划批准。被审批物是计划本身
    #: （`pend["subject"]` = slug，文件路径由 `core/plan.py` 现算）。
    #: 它不按目标授权 —— 结算器翻的是模式（退出计划模式），
    #: 授权只是为了让那次重放走得通。
    pending_plan: dict = field(default_factory=dict)
    session_skip_delete: bool = False  # skip delete approvals this session
    #: 本会话「不再询问跨区搜索」。这是真相的载体 ——
    #: 调度器轮末按它增删 `_session_cross_zone_auto`，再每轮开局投影到 wm。
    #: `/clear` 把它置 False 就等于清零（跟 `session_skip_delete` 同一条路）。
    session_cross_zone_auto: bool = False
    #: 本会话已经被问过几次（按审批种类分）。
    #: 检测器 +1；工具据此决定要不要出升级项「本次对话内自动放行」。
    #: 同样是载体，真相在调度器上。
    #:
    #: 必须按 kind 分开计数。出网与执行都有升级项（`escalate_at=2`，
    #: 跨区是 3），共用一个标量会让它们互相顶早 —— 批两个域名之后，
    #: 跨区的第 5 项会在第 1 次被问时冒出来（`core/approval.py` 的
    #: `ASK_COUNTED_KINDS` 注释里那条警告，说的就是这个）。
    approval_ask_counts: dict = field(default_factory=dict)
    #: 本会话按目标放行的载体 —— `{kind: {subject, ...}}`。
    #: 真相的载体（与 `session_skip_delete` / `session_cross_zone_auto` 同一条路）：
    #: 结算时写它，调度器轮末据它增删 `_session_target_grants`，再每轮开局投影到 wm。
    #: `/clear` 把它置 `{}` 就等于清零 —— 这是它必须存在而不是从 wm 读回的理由。
    #: `"*"` = 该 kind 全部放行（选项 5）。
    session_target_grants: dict = field(default_factory=dict)
    #: 审批的回答不新开一轮 —— 它把那次被挂起的
    #: 工具调用补上结果（同意 = 零消息，见 `core/agent_loop.py`
    #: 的 `replay_pending_call`）。
    #:
    #: 写它的是结算器（`pre_process._settle_*`），读它的是 `AgentLoopStage`：
    #:   * `{"call_id", "name", "arguments"}` —— 同意，重放那次调用
    #:   * `{"call_id", "deny"}`               —— 拒绝，给那次调用回一句拒绝
    #:   * 另有 `"fallback"` = 补不成时用的旧回执（"请重新提交…"），
    #:     找不到那条消息时退回去 —— 退路必须留着，否则用户批了却什么都没发生。
    #:
    #: 不属于 `_PENDING_STORES` 那一族：那几张表是"还挂着的待审批"，
    #: 要落盘、要跨轮、要浮窗渲染；这一格只活在"回答的那一轮"，不落盘
    #: （下一轮它的信息已经变成历史里那条被替换过的 tool 结果了）。
    approval_replay: dict = field(default_factory=dict)

    # ---- rich content (set by scheduler from event.message.message_chain) ----
    message_chain: "MessageChain | None" = None

    #: 这一轮要发给模型的图片块（OpenAI 形状的
    #: content part），由 `PreProcessStage` 从 `message_chain` 现做。
    #:
    #: 为什么不塞进 `user_message`：那条是 `str`，而且它的读者
    #: （命令解析 / 审批回答 / `_validate_input` / 落库）全都按字符串用它。
    #: 把它变成块列表 = 把"唯一的文本入口"改成多态，改一处漏三处。
    #:另开一格，`agent_loop` 拼消息时合流。
    #:
    #: 空列表 = 没有图，与"读不到"是两件事 —— 这里不需要区分
    #: （它是本地结构，数得清）。
    user_images: list = field(default_factory=list)

    # ---- populated by PreProcessStage ----
    messages: list[dict] = field(default_factory=list)
    memory_context: str = ""
    memory_manager: Any = None     # MemoryManager instance
    #: 通道键 —— 这一段对话的身份。
    #: 它由 `core/session_key.py::session_key_for` 推导，`/clear` 不动它
    #: （档位 / 工具发现集 / 取消标志 / ICE / 计划 / 待办 / 审批 / 锁 全认它）。
    #: 它同时当两个键用，存储那一半在 `storage_id` ——
    #: 每一处读/写都要判一次它属于哪一层（认通道的 vs 存史的）。
    session_id: str = ""
    #: 存储键 —— 消息行 / 会话笔记 / 重放落库
    #: 挂在哪个 id 上。它由 `short_term.init_session()` 铸出来，
    #: `/clear` 换一个新的（旧行留着 = 留档），这是它与 `session_id`
    #: 唯一的不同之处，也是会话回退 / 分叉的地基。
    #:
    #: 今天两者仍然恒等（同一个串），所以这一格加起来时
    #: 值一个字节都没变；真正的分叉在 `/clear` 之后。
    storage_id: str = ""

    #: 这一轮用的是哪条模型配置。
    #:
    #: 由调度器在轮首解析一次（`core/session_model.resolve`），
    #: 两个读者：
    #:   · 轮末的惰性冻结（`PostProcessStage`，判据「模型有任意回复」）
    #:     —— 冻的就是这个 id；
    #:   · 界面/事件要说清"这一段正在用哪条"。
    #: `""` = 跟随默认（那一轮没有默认条目，退回 `config.yaml`）。
    #:   它与"有默认、但装不上"在这一格里长得一样 —— 两者下游要做的事
    #:   也正好一致（沿用进程那个 provider），所以刻意不再造一个第三态
    #:   （同 `model_configs.default_id` 那条判据）。
    model_config_id: str = ""

    # ---- populated by AgentLoopStage ----
    response: str = ""
    tokens_used: int = 0

    # ---- populated by PostProcessStage ----
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- progress event stream ----
    events: Any = None
    """EventBus | None.  None 时 emit 静默跳过（测试可注入替身）。"""

    event: Any = None
    """触发本轮的原始 MessageEvent。

    类型标注为 Any 以避免运行期 import 环（channels.base → core 反向依赖）。
    有了它，阶段才第一次能拿到 platform_meta / reply_callback / extras ——
    没有它，唯一的阶段→事件通道是 scheduler 单向写 event.extras["response"]。
    """


# ------------------------------------------------------------------
# Stage ABC
# ------------------------------------------------------------------

class Stage(ABC):
    """One step in the message-processing pipeline.

    Subclasses override `process(ctx)`, optionally modifying *ctx*
    and returning it.
    """

    @abstractmethod
    async def process(self, ctx: StageContext) -> StageContext:
        """Process *ctx* and return it (possibly modified).

        If the stage determines that no further processing is needed
        it may set ctx.metadata["stop"] = True — the scheduler
        will skip remaining stages.
        """
        ...
