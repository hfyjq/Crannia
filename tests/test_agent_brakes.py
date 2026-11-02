"""刹车：中断本轮 + 重复工具调用异常断路器。

为什么需要这两道闸 —— 主循环若只有 `max_turns` 数数，既抓不到
"模型在转圈"，又会让正常的长任务提前失败。两道闸各管一件事，
先有刹车，轮次上限才只是兜底。

两道闸的分工：
  * 中断 —— 用户能停（`/stop` / Web 的「中断」按钮）。人对成本的最终否决权。
  * 重复工具调用 —— 抓"模型转圈"：同一工具同一参数再来一次，结果不会变，
    但每轮都要付全量的 token。数数（`max_turns`）只在跑疯时兜底，抓不到这个。

诚实的边界：`provider.chat()` 同步阻塞、不可打断，所以中断只保证
下一次迭代之前停下。下面的测试把它钉死成契约。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_agent_brakes.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.agent_loop as AL  # noqa: E402
import bus.registry as R  # noqa: E402
from core.cancellation import clear, is_cancelled, pending, request_cancel  # noqa: E402
from core.events import EVT_TURN_END, EVT_TURN_START  # noqa: E402
from core.session_key import session_key_for  # noqa: E402

SESS = "brake:private:probe"

# ASCII-only 输出 —— Windows 控制台默认 GBK，emoji 会让整个运行崩掉。


# ------------------------------------------------------------------
# 假 provider：只回答，不联网
# ------------------------------------------------------------------

class _Resp:
    def __init__(self, content=None, tool_calls=None, prompt_tokens=10,
                 completion_tokens=5):
        self.content = content
        self.tool_calls = tool_calls
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.reasoning_content = None

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens


class _CountingProvider:
    """记录被调用了几次 —— 这正是断言刹车生效的可数证据。"""

    def __init__(self, script):
        #: script: 一个函数 (call_index) -> _Resp
        self._script = script
        #: 循环里的模型调用次数（= 工具调用轮次）
        self.calls = 0
        #: 刹车之后那次收尾调用（`tools=[]`）。
        #:   它不算一轮循环，所以分开数 —— 否则"跑到上限该是几次"这条判据
        #:   会被它 +1 打歪，而它恰恰是故意多出来的那一次。
        self.wrap_calls = 0

    def chat(self, messages, tools=None, system=None, thinking=None):
        if AL.BRAKE_WRAP_MARK in str((messages or [{}])[-1].get('content') or ''):
            self.wrap_calls += 1
            # 回空回答，收尾走"退回原文案"那一支：
            #   本文件几条断言打在规范的刹车文案上（和 `test_stream_brakes` 同一条取舍）。
            return _Resp(content="")
        i = self.calls
        self.calls += 1
        return self._script(i, messages)


def _tool_call(name, args_json='{"path":"a.txt"}', call_id="c1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args_json},
    }


def _dangling_tool_calls(messages):
    """返回没有对应 tool 结果的 tool_call id 列表（必须是空的）。

    必须用生产里那一份（`core/message_history.check`），不在这里另写一套
    —— 两套实现就是两套真相，而测试用的判据必须和生产的是同一个。

    生产那份更严：它还能看出孤儿结果、重复 id、
    "结果排在调用之前"；那三种在只比集合的实现下完全正常。
    换用它之后这个文件顺带多挡了三种形状。
    """
    from core.message_history import check as _check

    return list(_check(messages).unanswered)


# ------------------------------------------------------------------
# 1. 中断：第二次调用模型之前必须停
# ------------------------------------------------------------------

def test_cancel_before_the_loop_stops_immediately():
    """迭代开头那道闸：标志已经置上时，一次模型调用都不该发生。

    与下一条分开写是刻意的：这条只钉"进来就被拦"，
    下一条钉"跑到一半被拦" —— 搅在一起谁也钉不住。
    """
    clear(SESS)
    request_cancel(SESS)          # 用户在这一轮开始之前就要求停下

    p = _CountingProvider(lambda i, m: _Resp(content="should never be reached"))
    out, msgs, _c, _t = AL.agent_loop(
        "hi", p, messages=[{"role": "user", "content": "hi"}],
        max_turns=30, session_id=SESS,
    )

    assert p.calls == 0, f"标志已置上却还是调了 {p.calls} 次模型"
    assert "中断" in out, out
    assert msgs[-1]["content"] == out, "中断的话应当作为 assistant 消息落进历史"
    assert is_cancelled(SESS) is False, "读到就该清掉（否则会误杀后面的轮次）"
    print("  [ok] 迭代开头那道闸：一次模型调用都没发生")


def test_cancel_mid_call_stops_before_dispatching_tools():
    """在模型调用期间按了打断，工具一次都不许跑。

    语义：正在飞的那次请求打不断（我们仍然不取消那次 HTTP 请求），
    但工具不许执行 —— LLM 调用无副作用，工具调用有，所以"放弃这次请求"可以，
    "掐断正在跑的工具"不行。现在这条路上我们不再等那次请求，
    并且不接收它的回复（`CANCEL_REPLY`），所以
    「assistant 那条带 `tool_calls` 的消息落进历史」这件事不会发生 ——
    没有悬空要补，因为那条消息根本没写出来。
    """
    clear(SESS)

    # 一个探针工具：只要被执行过就能被发现
    ran = []
    R.register_tool(
        "probe_brake_tool", lambda **kw: (ran.append(kw), {"ok": True})[1],
        {"description": "probe", "inputSchema": {"type": "object", "properties": {}}},
    )
    # 执行侧还要过暴露集那道闸（`bus.registry.is_tool_active`）。不声明它的话，
    # `ran == []` 就有两个原因（刹车拦住了 / 门禁拒了），而这条测试
    # 要证的只有第一个 —— 那样它会在刹车被删掉之后照样绿。
    R.note_discovered(SESS, ["probe_brake_tool"])

    def script(i, messages):
        if i == 0:
            request_cancel(SESS)          # 用户在这一轮跑着的时候按了中断
            return _Resp(tool_calls=[_tool_call("probe_brake_tool", "{}")])
        return _Resp(content="should never be reached")

    p = _CountingProvider(script)
    out, msgs, _c, _t = AL.agent_loop(
        "hi", p, messages=[{"role": "user", "content": "hi"}],
        max_turns=30, session_id=SESS,
    )

    assert p.calls == 1, (
        f"掐掉之后不该再多打一次模型 —— 实际 {p.calls} 次"
    )
    assert ran == [], "工具被执行了 —— 那条路一道闸都没拦住"
    # 中断时返回的固定文案
    assert out == AL.CANCEL_REPLY, out
    # "不接收模型回复"的判据：那条带 `tool_calls` 的 assistant 消息
    #    根本没落进历史，没有悬空要补，也不该多出一条中断消息
    #    （`CANCEL_REPLY` 是一次渲染，不是一条消息）。
    assert _dangling_tool_calls(msgs) == [], (
        "提前停下留下悬空 tool_call —— 下一轮发回上游会 400"
    )
    assert not any(m.get("role") == "assistant" for m in msgs), (
        f"那条被掐掉的回复落进历史了：{msgs!r}"
    )
    assert is_cancelled(SESS) is False, "读到就该清掉（否则会误杀后面的轮次）"
    print("  [ok] 模型调用期间被打断：模型调了 1 次，工具一次没跑，回复没进历史")


def test_flag_is_cleared_at_turn_end_by_the_scheduler():
    """清在轮末，不在轮首 —— 而且要驱真的调度器，不是 helper。

    必须驱真的调度器，不是 helper：手调 `clear()` 再断言能跑完，
    只证明了 helper 会清、以及 `agent_loop` 不带标志时能跑 ——
    完全没测调度器那条真实路径（而清在哪一端正是关键）。

    为什么必须是轮末（而不是轮首）：标志没有"轮次身份"。清在轮首的话，
    用户点了中断，标志设上，下一个开局的轮次无条件抹掉它，
    而上一个循环可能还没读到，中断静默失效。
    """
    import asyncio

    from channels.base import (
        ChannelMetadata, Message as ChannelMessage, MessageEvent,
        MessageSession, MessageType,
    )
    from channels.message import MessageChain, Plain
    from core.pipeline.scheduler import PipelineScheduler
    from core.pipeline.stage import Stage, StageContext

    class _FakeBus:
        def __init__(self):
            self.sent = []

        def emit(self, type_, session_key="", **data):
            self.sent.append((type_, session_key, data))

    class _StopStage(Stage):
        """什么也不做的桩：让这一轮干净地跑完（不碰 provider / 不联网）。"""

        async def process(self, ctx: StageContext) -> StageContext:
            ctx.response = "stub"
            ctx.metadata["stop"] = True
            return ctx

    session = MessageSession("web", MessageType.PRIVATE, "probe")
    key = session_key_for(session)
    event = MessageEvent(
        message=ChannelMessage(
            sender_id="u", message_chain=MessageChain([Plain(text="hi")]),
            sender_name="U", message_type=MessageType.PRIVATE, session=session,
        ),
        session=session,
        platform_meta=ChannelMetadata(name="t", description="d",
                                      platform_type="web"),
    )

    sched = PipelineScheduler(session_pool=None, provider=None, config={})
    sched.events = _FakeBus()
    sched._stages = [_StopStage()]

    clear(key)
    request_cancel(key)                      # 空闲时被设上（比如空闲点了按钮）
    assert is_cancelled(key) is True, "setup: 标志没设上"

    asyncio.run(sched.execute(event))

    assert is_cancelled(key) is False, (
        "轮末没清标志 —— 它会一直留着，误杀这个会话后面每一轮"
    )
    assert key == "workspace:private", (
        f"会话键的形态变了（{key}）—— §8.1 #3 起私聊跨通道同一条；"
        f"形态由 core/session_key.py 一处给"
    )
    print("  [ok] 调度器在**轮末**清掉标志（真调度器、真事件、真键）")


def test_web_stop_key_matches_the_event_key():
    """Web 的 `/api/stop` 与 `/api/chat` 必须用同一个会话键。

    这条钉住一个真实故障：`/api/stop` 若自己写
    字面量 `"web:cli:default"` 当键，而调度器/循环用的是 `str(event.session)`
    = `"web:private:web:cli:default"`，标志置在没人读的键上：那一轮照跑满
    30 轮，接口却回 `{"ok": true}` 谎报成功（说一套做一套）。

    做法：把两端的推导对上 —— helper 导出的键 vs 同一个 session 造出来的
    MessageEvent 的键。顺带把生产里真实的键钉成字面量（库里已有既有记录）。
    """
    from channels.base import (
        ChannelMetadata, Message as ChannelMessage, MessageEvent, MessageType,
    )
    from channels.message import MessageChain, Plain
    from web.server import _web_session, WEB_SESSION_ID

    session = _web_session()
    event = MessageEvent(
        message=ChannelMessage(
            sender_id="u", message_chain=MessageChain([Plain(text="hi")]),
            sender_name="U", message_type=MessageType.PRIVATE, session=session,
        ),
        session=session,
        platform_meta=ChannelMetadata(name="t", description="d",
                                      platform_type="web"),
    )

    assert str(event.session) == str(session), (
        "调度器看到的键与 /api/stop 用的键不一致 —— 中断会静默失效"
    )
    assert str(session) == "web:private:web:cli:default", (
        f"Web 会话键变了（{str(session)}）—— 与库里既有的行会对不上（B6c 那类后果）"
    )
    assert WEB_SESSION_ID == "web:cli:default", (
        "session_id 常量变了 —— 那是**会话 id**，不是键；改它等于换会话"
    )
    print(f"  [ok] /api/stop 与事件用同一个键：{str(session)}")


def test_wrong_session_is_not_cancelled():
    """中断是按会话的 —— 别把别人的轮次掐了。"""
    clear(SESS)
    request_cancel("other:private:someone")
    p = _CountingProvider(lambda i, m: _Resp(content="done"))
    out, _m, _c, _t = AL.agent_loop(
        "hi", p, messages=[{"role": "user", "content": "hi"}],
        max_turns=5, session_id=SESS,
    )
    assert out == "done", out
    clear("other:private:someone")
    print("  [ok] 中断只对它自己的会话生效")


# ------------------------------------------------------------------
# 2. 重复工具调用断路器
# ------------------------------------------------------------------

def test_repeated_identical_tool_call_trips_the_breaker():
    """同一工具 + 同一参数连续 3 次，停，并且说清楚为什么。"""
    clear(SESS)
    # 永远回同一个工具、同一参数 —— 这就是"转圈"
    p = _CountingProvider(
        lambda i, m: _Resp(tool_calls=[_tool_call("list_workspace_files", "{}")])
    )
    out, msgs, _c, _t = AL.agent_loop(
        "hi", p, messages=[{"role": "user", "content": "hi"}],
        max_turns=30, session_id=SESS,
    )

    assert "重复" in out or "连续" in out, out
    assert "list_workspace_files" in out, out
    assert p.calls < 30, f"断路器没生效，跑满了一轮上限（{p.calls} 次）"
    assert _dangling_tool_calls(msgs) == [], "留下了悬空 tool_call"
    print(f"  [ok] 连续重复被拦下（第 {p.calls} 次调用就停了，而不是 30）")


def test_different_args_do_not_trip():
    """参数不同就不算转圈 —— 同一工具反复用（每次换文件）是正常干活。"""
    clear(SESS)
    seen = {"n": 0}

    def script(i, messages):
        if i >= 5:
            return _Resp(content="done")
        seen["n"] += 1
        return _Resp(tool_calls=[
            _tool_call("list_workspace_files", '{"path":"f%d.txt"}' % i)
        ])

    p = _CountingProvider(script)
    out, _m, _c, _t = AL.agent_loop(
        "hi", p, messages=[{"role": "user", "content": "hi"}],
        max_turns=30, session_id=SESS,
    )
    assert out == "done", out
    assert seen["n"] == 5, seen
    print(f"  [ok] 同工具、换参数跑了 {seen['n']} 次没被误伤")


def test_negative_control_without_the_breaker_it_runs_to_max_turns():
    """负对照：把两把尺都调到天上，同一个脚本应当跑满 max_turns。

    这条证明 `test_repeated_identical_tool_call_trips_the_breaker` 里
    「第几次就停了」是断路器干的，不是别的什么碰巧。

    刹车 2 有第二把尺（整轮累计，`_SIG_REPEAT_LIMIT`）——
    只关第一把不够（同一个调用重复 8 次会被第二把抓住）。负对照必须把被测的东西真关掉。
    """
    clear(SESS)
    saved = (AL._REPEAT_TOOL_LIMIT, AL._SIG_REPEAT_LIMIT)
    AL._REPEAT_TOOL_LIMIT = 10_000
    AL._SIG_REPEAT_LIMIT = 10_000
    try:
        p = _CountingProvider(
            lambda i, m: _Resp(tool_calls=[_tool_call("list_workspace_files", "{}")])
        )
        out, _m, _c, _t = AL.agent_loop(
            "hi", p, messages=[{"role": "user", "content": "hi"}],
            max_turns=8, session_id=SESS,
        )
        assert p.calls == 8, (
            f"负对照失效：去掉断路器后应跑满 8 次，实际 {p.calls} 次"
        )
        assert "上限" in out, out
        assert p.wrap_calls == 1, (
 f"：刹车之后该有**一次**收尾调用（工具关掉），实得 {p.wrap_calls} 次"
            f" —— 那正是「被掐的不该是用户的活」这条的落点")
        # 那段收尾必须追加回 `messages`。
        #   判据是落库那条路只存 `messages`（`record_turn` 在 `messages is not None`
        #   时忽略 `assistant_message`），不在 `messages` 里 = 用户看得到、库里没有：
        #   `/resume` 看不到 · `history_search` 找不到 · 下一轮模型不知道自己交付过什么。
        _hits = [m for m in _m
                 if m.get("role") == "assistant" and out in (m.get("content") or "")]
        assert _hits, (
 "★ 收尾回复没进 `messages` —— 它会**用户看得到、库里没有**："
            "`/resume` 看不到 · `history_search` 找不到 · 下一轮模型不知道自己交付过什么")
        # 负对照方向：只许一条（正常收工那条路本来就会写一条，
        #   要是这里再写一条，历史里就是两条逐字相同的话）。
        assert len(_hits) == 1, f"收尾回复被写了 {len(_hits)} 条，应当恰好 1 条"
        print(f"  [ok] 关掉两把尺就跑满 max_turns（{p.calls} 次 + 1 次收尾）—— 对照成立")
    finally:
        AL._REPEAT_TOOL_LIMIT, AL._SIG_REPEAT_LIMIT = saved


def test_a_cycle_of_calls_trips_the_second_ruler():
    """循环（不连续）也要被拦住。

    典型形状：`read_excel_data` 调了 99 次，而连续相同的最大长度 = 1
    —— 它是循环（1月→2月→4月→9月→3月→5月→…），同一个签名整轮出现 11~12 次。
只认"连续"的第一把尺永远看到 1，一次都不会触发
    （这样的轮次能烧掉 685K token 且零答案）。

    本条的分辨力来自脚本：故意让相邻两次不同（否则第一把尺就抓了，
    测不到第二把）。负对照见下一条。
    """
    clear(SESS)
    cycle = ['{"path":"a"}', '{"path":"b"}', '{"path":"c"}']
    seq = {"i": 0}

    def script(i, messages):
        if seq["i"] >= 40:
            return _Resp(content="done")
        k = cycle[seq["i"] % len(cycle)]
        seq["i"] += 1
        return _Resp(tool_calls=[_tool_call("list_workspace_files", k)])

    p = _CountingProvider(script)
    out, _m, _c, _t = AL.agent_loop(
        "hi", p, messages=[{"role": "user", "content": "hi"}],
        max_turns=30, session_id=SESS,
    )
    # 返回的第四格是 token 数，不是轮数，用脚本自己的计数当轮数。
    assert "同样的参数" in out, "第二把尺没生效，实得：%r" % out[:200]
    assert "上限" not in out, "跑到 max_turns 才停 —— 那不是熔断，是兜底"
    assert p.calls < 30, "没在跑完之前停（跑了 %d 轮）" % p.calls
    print("  [ok] 循环（相邻都不同）在第 %d 轮被第二把尺拦住" % p.calls)


def test_negative_control_the_cycle_runs_on_when_the_second_ruler_is_off():
    """负对照：只关第二把尺，那个循环应当跑满 max_turns。

    证明上一条的红是第二把尺干的，不是别的。
    """
    clear(SESS)
    saved = AL._SIG_REPEAT_LIMIT
    AL._SIG_REPEAT_LIMIT = 10_000
    try:
        cycle = ['{"path":"a"}', '{"path":"b"}', '{"path":"c"}']
        seq = {"i": 0}

        def script(i, messages):
            k = cycle[seq["i"] % len(cycle)]
            seq["i"] += 1
            return _Resp(tool_calls=[_tool_call("list_workspace_files", k)])

        p = _CountingProvider(script)
        out, _m, _c, _t = AL.agent_loop(
            "hi", p, messages=[{"role": "user", "content": "hi"}],
            max_turns=8, session_id=SESS,
        )
        assert p.calls == 8, "负对照失效：关掉第二把尺后应跑满 8 次，实际 %d" % p.calls
        assert "上限" in out, out
        print("  [ok] 只关第二把尺 -> 循环跑满 max_turns（%d 次）" % p.calls)
    finally:
        AL._SIG_REPEAT_LIMIT = saved


# ------------------------------------------------------------------
# 3. 轮次事件的完整性：发过 turn.start 的轮次必须有 turn.end
# ------------------------------------------------------------------

def _emit_into(sink):
    """把 `_emit` 回调收到的类型按顺序收进 *sink*。"""
    def emit(type_, **data):
        sink.append(type_)
    return emit


def test_turn_end_is_emitted_on_the_mid_turn_brake_paths():
    """发过 `turn.start` 的轮次必须有 `turn.end` —— 两条轮内刹车都要发。

    钉住的不变量：`_end_turn` 的 docstring 写着
    「每一条离开循环的路径都必须发它」，两条轮内刹车也不例外。
    漏发时，`turn.end` 是 durable、`turn.start` 不是
    （`core/events.py:95`），所以盘上会永久缺一条，事件流对不齐。

    判据用相对形式（start 过就要有 end），不用绝对次数：
    绝对次数会随实现细节漂移，而这条不变量不该漂。
    """
    clear(SESS)
    R.register_tool(
        "probe_brake_tool", lambda **kw: {"ok": True},
        {"description": "probe", "inputSchema": {"type": "object", "properties": {}}},
    )

    # 情形 1：工具派发前的中断
    ev = []

    def script(i, messages):
        if i == 0:
            request_cancel(SESS)
            return _Resp(tool_calls=[_tool_call("probe_brake_tool", "{}")])
        return _Resp(content="should never be reached")

    AL.agent_loop("hi", _CountingProvider(script),
                  messages=[{"role": "user", "content": "hi"}],
                  max_turns=30, session_id=SESS, _emit=_emit_into(ev))
    starts, ends = ev.count(EVT_TURN_START), ev.count(EVT_TURN_END)
    assert starts == ends == 1, (
        f"中断路径：turn.start {starts} 次、turn.end {ends} 次 —— 应当各 1"
    )

    # 情形 2：重复工具调用断路器
    ev.clear()
    clear(SESS)
    AL.agent_loop(
        "hi",
        _CountingProvider(
            lambda i, m: _Resp(tool_calls=[_tool_call("list_workspace_files", "{}")])
        ),
        messages=[{"role": "user", "content": "hi"}],
        max_turns=30, session_id=SESS, _emit=_emit_into(ev),
    )
    starts, ends = ev.count(EVT_TURN_START), ev.count(EVT_TURN_END)
    assert starts == ends > 0, (
        f"断路器路径：turn.start {starts} 次、turn.end {ends} 次"
    )

    # 负对照：迭代开头那道闸刻意不发（那一轮连 turn.start 都没有）
    ev.clear()
    clear(SESS)
    request_cancel(SESS)
    AL.agent_loop("hi", _CountingProvider(lambda i, m: _Resp(content="x")),
                  messages=[{"role": "user", "content": "hi"}],
                  max_turns=5, session_id=SESS, _emit=_emit_into(ev))
    assert ev.count(EVT_TURN_START) == 0 and ev.count(EVT_TURN_END) == 0, (
        f"迭代开头那道闸不该发轮次事件（实际收到 {ev}）—— "
        "那一轮根本没开始，给它发收尾事件才是错的"
    )
    print("  [ok] 轮内刹车补发 turn.end；迭代开头那道闸刻意不发")


# ------------------------------------------------------------------

def _main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
