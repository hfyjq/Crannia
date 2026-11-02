"""
Unit tests for Pipeline stages and scheduler.

Validates:
  1. Stage registration via @register_stage decorator
  2. Stage ordering by STAGES_ORDER
  3. StageContext defaults
  4. PipelineScheduler initialization
  5. Custom stage integration

Usage:
    cd <仓库根>
    python tests/test_pipeline.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

def test_stage_registration():
    """@register_stage should add classes to registered_stages list."""
    from core.pipeline.stage import Stage, StageContext, register_stage
    from core.pipeline.stage import registered_stages as reg

    # Remember initial count
    initial_count = len(reg)

    @register_stage
    class TestStage(Stage):
        async def process(self, ctx: StageContext) -> StageContext:
            return ctx

    assert TestStage in reg
    assert len(reg) == initial_count + 1

    # Clean up — remove our test stage
    reg.remove(TestStage)

    print(f"  [ok] Stage registration: {initial_count} -> {initial_count + 1} -> {len(reg)}")


def test_stage_context_defaults():
    """StageContext should have sensible defaults."""
    from core.pipeline.stage import StageContext
    from channels.base import MessageSession, MessageType

    session = MessageSession("test", MessageType.PRIVATE, "u1")
    ctx = StageContext(session=session, user_message="hello")

    assert ctx.session == session
    assert ctx.user_message == "hello"
    assert ctx.messages == []
    assert ctx.memory_context == ""
    assert ctx.response == ""
    assert ctx.session_pool is None
    assert ctx.provider is None
    assert ctx.metadata == {}

    print(f"  [ok] StageContext defaults: session={ctx.session}, "
          f"message='{ctx.user_message}'")


def test_stage_context_mutable():
    """Stages should be able to modify StageContext."""
    from core.pipeline.stage import StageContext
    from channels.base import MessageSession, MessageType

    session = MessageSession("test", MessageType.PRIVATE, "u1")
    ctx = StageContext(session=session, user_message="hello")

    # Simulate PreProcess
    ctx.messages = [{"role": "system", "content": "You are helpful."}]
    ctx.memory_context = "<background_context>data</background_context>"

    # Simulate AgentLoop
    ctx.response = "Hello, user!"

    # Simulate PostProcess
    ctx.metadata["context_usage"] = {"percentage": 42.0}

    assert len(ctx.messages) == 1
    assert ctx.response == "Hello, user!"
    assert ctx.metadata["context_usage"]["percentage"] == 42.0

    print(f"  [ok] StageContext mutable: {len(ctx.messages)} msgs, "
          f"response='{ctx.response}'")


def test_pipeline_scheduler_init():
    """PipelineScheduler should initialize without errors."""
    from core.pipeline.scheduler import PipelineScheduler

    # Scheduler with None deps (for testing)
    scheduler = PipelineScheduler(
        session_pool=None,
        provider=None,
        config={"tool_result_max_chars": 1000},
    )

    assert scheduler._config["tool_result_max_chars"] == 1000
    assert len(scheduler._stage_classes) >= 3  # PreProcess, AgentLoop, PostProcess

    class_names = [cls.__name__ for cls in scheduler._stage_classes]
    assert "PreProcessStage" in class_names
    assert "AgentLoopStage" in class_names
    assert "PostProcessStage" in class_names

    print(f"  [ok] PipelineScheduler: {len(scheduler._stage_classes)} stages "
          f"({', '.join(class_names)})")


def test_custom_stage_integration():
    """A custom stage should be sortable in STAGES_ORDER."""
    from core.pipeline.stage import Stage, StageContext, register_stage
    from core.pipeline.stage import registered_stages as reg
    from core.pipeline.scheduler import STAGES_ORDER

    @register_stage
    class CustomStage(Stage):
        async def process(self, ctx: StageContext) -> StageContext:
            ctx.metadata["custom"] = True
            return ctx

    # Verify it's registered
    assert CustomStage in reg

    # Verify it gets a sort index (after built-in stages)
    idx = -1
    try:
        idx = STAGES_ORDER.index("CustomStage")
    except ValueError:
        idx = len(STAGES_ORDER)

    # It should either be in the order list or after everything
    assert idx >= 0

    # Clean up
    reg.remove(CustomStage)

    print(f"  [ok] CustomStage: registered, STAGES_ORDER index={idx}")


def test_stage_context_event_fields():
    """StageContext 的 events/event 字段默认 None 且可赋值。

    回归意义：这两个字段是追加到 dataclass 末尾的，既有调用点全部
    关键字传参，所以必须证明追加没有破坏构造。
    """
    from core.pipeline.stage import StageContext
    from channels.base import MessageSession, MessageType

    session = MessageSession("test", MessageType.PRIVATE, "u1")

    ctx = StageContext(session=session, user_message="hi")
    assert ctx.events is None
    assert ctx.event is None

    sentinel = object()
    ctx2 = StageContext(session=session, user_message="hi", events=sentinel,
                        event=sentinel)
    assert ctx2.events is sentinel and ctx2.event is sentinel
    # 位置参数仍然可用（前两个字段是必填）
    ctx3 = StageContext(session, "hi")
    assert ctx3.user_message == "hi"

    print("  [ok] StageContext event fields (default None, kwarg + positional safe)")


def test_scheduler_emits_lifecycle():
    """scheduler.execute 发出 task.start / task.end，带正确会话身份。

    全程不触网：真阶段被替换成一个立刻 stop 的 stub。
    """
    import asyncio
    from core.pipeline.scheduler import PipelineScheduler
    from core.pipeline.stage import Stage, StageContext
    from channels.base import (
        ChannelMetadata, Message as ChannelMessage, MessageEvent,
        MessageSession, MessageType,
    )
    from channels.message import MessageChain, Plain

    class _FakeBus:
        """记录 emit 调用，不起线程、不落盘。"""

        def __init__(self) -> None:
            self.sent: list[tuple] = []

        def emit(self, type_, session_key="", **data):
            self.sent.append((type_, session_key, data))

    class _StubStage(Stage):
        async def process(self, ctx: StageContext) -> StageContext:
            ctx.response = "stub-reply"
            ctx.metadata["stop"] = True      # 后面的真阶段不跑
            return ctx

    session = MessageSession("test", MessageType.PRIVATE, "u1")
    event = MessageEvent(
        message=ChannelMessage(
            sender_id="u",
            message_chain=MessageChain([Plain(text="hello there")]),
            sender_name="U",
            message_type=MessageType.PRIVATE,
            session=session,
        ),
        session=session,
        platform_meta=ChannelMetadata(name="t", description="d",
                                      platform_type="test"),
    )

    sched = PipelineScheduler(session_pool=None, provider=None, config={})
    bus = _FakeBus()
    sched.events = bus
    sched._stages = [_StubStage()]           # 换掉 PreProcess/AgentLoop/PostProcess

    out = asyncio.run(sched.execute(event))

    types = [t for t, _, _ in bus.sent]
    assert types == ["task.start", "task.end"], types
    assert out == "stub-reply"

    # 会话身份必须到位 —— 这正是旧钩子拿不到、也是事件流能按会话路由的前提
    from core.session_key import session_key_for
    # 会话键统一推导 —— 私聊跨通道同一条
    _expect = session_key_for(session)
    assert bus.sent[0][1] == _expect, bus.sent[0][1]
    assert bus.sent[1][1] == _expect, bus.sent[1][1]

    end_data = bus.sent[1][2]
    assert end_data["ok"] is True
    assert end_data["origin"] == "im"        # platform_type="test" 非 web
    assert isinstance(end_data["duration_ms"], int)
    assert end_data["response"] == "stub-reply"
    # `task.end` 必须有 `tool_count` —— 否则
    #   「这一轮一共调了几次工具」只能事后从事件日志里数。
    #   判据是键在且是个数，不是"等于几"：这里那个 stub 阶段没跑
    #      agent 循环，0 是事实（没有工具可调），不是"读不到"。
    assert "tool_count" in end_data, "task.end 又丢回 tool_count 了"
    assert end_data["tool_count"] == 0, end_data["tool_count"]

    # task.start 带上了标签
    assert bus.sent[0][2]["label"].startswith("hello there")

    print(f"  [ok] scheduler lifecycle events ({types}, "
          f"session={bus.sent[0][1]}, origin=im)")


# ------------------------------------------------------------------
# `tool_count` 的读数
# ------------------------------------------------------------------

def test_tool_count_counts_the_fillbacks():
    """本轮新增的 `role=tool` 消息数 = 工具调用数。

    判据是一一对应：每条 `tool_call` 有且只有一条 `role=tool` 回填
    （上游硬要求，少一条 400），数回填就是数调用。
    只数本轮新增那段（`start` 之前的历史一条都不算）—— 否则第二轮
    会把第一轮的数一起带上，而那个数看起来是对的（只多不少）。
    """
    from core.pipeline.stages.agent_loop import _count_tool_calls

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "assistant", "tool_calls": [{"id": "b"}, {"id": "c"}]},
        {"role": "tool", "tool_call_id": "b", "content": "{}"},
        {"role": "tool", "tool_call_id": "c", "content": "{}"},
    ]
    assert _count_tool_calls(msgs, 0) == 3, _count_tool_calls(msgs, 0)
    # 只数本轮新增：从第 4 条起数，只剩 2 条
    assert _count_tool_calls(msgs, 3) == 2, _count_tool_calls(msgs, 3)
    # 什么都没新增，0（不是 None）
    assert _count_tool_calls(msgs, len(msgs)) == 0
    # 观测不许弄挂被观测的那一轮：坏输入返回 0 而不是抛
    assert _count_tool_calls(None, 0) == 0
    print("  [ok] tool_count = 本轮新增的 role=tool 条数（只读、坏输入不抛）")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def test_seed_platform_identity_feeds_the_dedup_gate():
    """去重闸的输入：`ctx.metadata` 里的 `message_id` / `sender_id`。

    * `pre_process` 读这两格（`platform != "cli" and message_id` 才判重）。
    * 此前没有任何地方写它们，那道闸对每个 IM 通道恒不触发。
这一条钉的是"喂"那半边（闸自己那半边在 `core/dedup.py` 的单测里）。
    """
    from types import SimpleNamespace
    from core.pipeline.scheduler import seed_platform_identity
    from core.pipeline.stage import StageContext
    from channels.base import MessageSession, MessageType

    ctx = StageContext(
        session=MessageSession("telegram", MessageType.PRIVATE, "7"),
        user_message="hi")
    event = SimpleNamespace(message=SimpleNamespace(
        message_id="4242", sender_id="u-9"))
    seed_platform_identity(ctx, event)
    assert ctx.metadata["message_id"] == "4242", ctx.metadata
    assert ctx.metadata["sender_id"] == "u-9", ctx.metadata

    # 通道没给（Web / CLI / 旧通道）：写空串，闸自己判非空，不触发
    ctx2 = StageContext(
        session=MessageSession("cli", MessageType.PRIVATE, "local"),
        user_message="hi")
    seed_platform_identity(ctx2, SimpleNamespace(message=SimpleNamespace()))
    assert ctx2.metadata["message_id"] == "", ctx2.metadata
    assert ctx2.metadata["sender_id"] == "", ctx2.metadata


def test_a_failed_turn_releases_the_dedup_slot():
    """一轮没成功，去重的占位要放回去。

    旧行为：`pre_process` 一进来就落账，后面任何环节失败，平台重投都会被
    自己的账本挡下（用户什么都收不到），而账上写着"处理过了"。
    新形状：占位在 `pre_process`、落账在 `post_process` 走完，
    其余所有路径由调度器在轮末 `release()` —— 这一条钉的就是那条兜底。
    """
    import asyncio

    import core.dedup as D
    from core.pipeline.scheduler import PipelineScheduler
    from core.pipeline.stage import Stage, StageContext
    from channels.base import (
        ChannelMetadata, Message as ChannelMessage, MessageEvent,
        MessageSession, MessageType,
    )
    from channels.message import MessageChain, Plain

    args = {"platform": "telegram", "sender_id": "u1", "content": "hi",
            "message_id": "m1", "scope": "111"}
    local = D.MessageDeduplicator(max_entries=100, ttl_seconds=60)
    assert local.is_duplicate(**args) is False        # ← pre_process 占位

    class _FailStage(Stage):
        async def process(self, ctx: StageContext) -> StageContext:
            ctx.metadata["dedup"] = args
            ctx.metadata["stop"] = True               # 没走到 post_process
            ctx.metadata["error"] = "boom"
            return ctx

    session = MessageSession("telegram", MessageType.PRIVATE, "111")
    event = MessageEvent(
        message=ChannelMessage(
            sender_id="u1",
            message_chain=MessageChain([Plain(text="hi")]),
            sender_name="U", message_type=MessageType.PRIVATE, session=session),
        session=session,
        platform_meta=ChannelMetadata(name="t", description="d",
                                      platform_type="test"),
    )

    old = D.default
    D.default = lambda: local
    try:
        sched = PipelineScheduler(session_pool=None, provider=None, config={})
        sched.events = None
        sched._stages = [_FailStage()]
        asyncio.run(sched.execute(event))
    finally:
        D.default = old

    assert local.is_duplicate(**args) is False, (
        "失败那一轮没有放位 —— 平台重投会被当成重复投递静默丢掉")
    print("  [ok] failed turn released the dedup slot")


def test_the_dedup_lifecycle_is_wired():
    """三个落点各就各位：占位（pre）· 落账（post）· 放位（调度器轮末）。

    这是接缝读者不是语义读者 —— 语义在 `test_provider_infra.py`
    那三条（`not_persisted_until_done` / `release_allows_a_retry` /
    `scope_separates_chats`）。
    """
    root = Path(__file__).resolve().parent.parent
    pre = (root / "core" / "pipeline" / "stages" / "pre_process.py").read_text(
        encoding="utf-8")
    post = (root / "core" / "pipeline" / "stages" / "post_process.py").read_text(
        encoding="utf-8")
    sched = (root / "core" / "pipeline" / "scheduler.py").read_text(
        encoding="utf-8")
    assert 'ctx.metadata["dedup"] = _dedup_args' in pre, "pre_process 没把参数交出去"
    assert "is_duplicate(" in pre and "mark_done(" not in pre, (
        "占位与落账必须分在两条路上")
    assert "mark_done(" in post, "post_process 没落账"
    assert "release(" in sched and "dedup_done" in sched, (
        "调度器没有那条轮末兜底")
    print("  [ok] dedup lifecycle is wired in three places")


if __name__ == "__main__":
    print("=" * 60)
    print("  Pipeline Tests")
    print("=" * 60)
    print()

    tests = [
        ("Stage registration", test_stage_registration),
        ("StageContext defaults", test_stage_context_defaults),
        ("StageContext mutable", test_stage_context_mutable),
        ("PipelineScheduler init", test_pipeline_scheduler_init),
        ("Custom stage integration", test_custom_stage_integration),
        ("StageContext event fields", test_stage_context_event_fields),
        ("Scheduler lifecycle events", test_scheduler_emits_lifecycle),
        ("W52 #13 tool_count", test_tool_count_counts_the_fillbacks),
        ("dedup gate input (platform identity)",
         test_seed_platform_identity_feeds_the_dedup_gate),
        ("dedup slot released after a failed turn",
         test_a_failed_turn_releases_the_dedup_slot),
        ("dedup lifecycle wiring", test_the_dedup_lifecycle_is_wired),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    print()
    print("=" * 60)
    print(f"  Results: {passed} passed, {failed} failed, {len(tests)} total")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
