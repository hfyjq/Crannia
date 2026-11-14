"""
两条路的刹车逐条对应 —— 流式路径不许绕开 sync 路径上的闸。

为什么立这个文件
================
`core/agent_loop.py` 有 sync 与 stream 两条循环。刹车（中断 / 重复工具调用）
只做在一条路上的话，另一条就会绕开它。判据不是"照意思写一遍"，是
同一组场景在两条路上各跑一遍、结果对得上 —— 所以本文件每条都跑两次。

四条场景（每条都比对：返回的文本 / 派发了几次 / 事件序列 / 悬空 tool_call）

  1. 中断（迭代开头） —— 两条路都不发任何事件（那道闸刻意在 `turn.start` 上面）
  2. 中断（工具派发前） —— 两条路都必须补上没跑的 `tool_call`
     （不补就是悬空引用，下一轮上游硬 400），且 `turn.start`/`turn.end` 成对
  3. 重复断路器（同工具同参数连续 3 次）—— 两条路的阈值与文案一致
  4. 正常一轮 —— 两条路的事件序列逐字相同

用法:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_stream_brakes.py
"""

import asyncio
import json
import sys
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from providers.base import StreamChunk  # noqa: E402
from core.agent_loop import BRAKE_WRAP_MARK, CANCEL_REPLY  # noqa: E402

# 模式影响派发，本文件必须自己钉模式。
# 不钉就会继承 `data/settings.json` 的 `default_mode`：
# `plan`，写工具被计划模式的闸拒掉（症状是 `calls == []`）·
# `auto`，闸去调分类器（吃掉 fake provider 的脚本化回复）。
# 一个测试文件一个字都不该关心模式 —— 触发路径见
# `tests/_sandbox.py::neutralise_settings`。
from _sandbox import neutralise_settings  # noqa: E402
neutralise_settings()


# ASCII-only 输出 —— 控制台默认 GBK，emoji 会让整个运行崩掉。

_SESS = "probe:brakes"

#: 临时 `GUIDANCE.md` 里的标记 —— 用来分辨"领域那一片跟没跟着激活走"
#: （通用纪律常开，不造靶子的话两种配置逐字相同）。
_MARK = "PROBE-DOMAIN-MARKER"


def _tool_call(name, args, id):
    return {"id": id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class _temp_domain:
    """临时给某个 skill 造一片 `GUIDANCE.md`。

    为什么必须造：通用纪律常开，不做点什么的话，
    "激活/没激活"两种配置的块逐字相同，那两条断言会恒真
    （没有靶子的不变量是装饰品）。
    造一片带标记的领域文件，才分得出"领域那一片到底跟没跟着激活走"。
    """

    def __init__(self, skill: str, text: str):
        self._skill, self._text, self._tmp = skill, text, None

    def __enter__(self):
        import tempfile
        from core import skill_guidance as SG
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self._tmp.name) / self._skill
        d.mkdir()
        (d / SG.DOMAIN_FILE).write_text(self._text, encoding="utf-8")
        self._saved = SG._skills_root
        SG._skills_root = lambda: Path(self._tmp.name)
        return self

    def __exit__(self, *exc):
        from core import skill_guidance as SG
        SG._skills_root = self._saved
        self._tmp.cleanup()
        return False


# ------------------------------------------------------------------
# 两个同形的替身 provider：脚本一样，只是接口不同
# ------------------------------------------------------------------

def _is_wrap_up(messages) -> bool:
    """这一轮是不是刹车之后的收尾调用。

    判据是那句话，不是 `tools=[]` —— 注册表空的时候，**循环里的正常调用
    也是空 `tools`**。标记取自 `core.agent_loop.BRAKE_WRAP_MARK`（同源，不会漂）。
    """
    return BRAKE_WRAP_MARK in str((messages or [{}])[-1].get("content") or "")


class _SyncProvider:
    _model = "probe-model"

    def __init__(self, script, sess):
        self._script, self._i, self._sess = script, 0, sess

    def chat(self, messages, tools, system=None, thinking=None):
        # 刹车之后那次收尾调用是 `tools=[]`（没有工具）。
        #   夹具这里回一个空回答，走"退回原文案"那一支，
        #   于是本文件几条刹车断言照旧打在规范的刹车文案上。
        #   收尾的成功那一支有它自己的读者
        #   （`test_the_brake_makes_the_model_wrap_up_instead_of_dropping_the_work`）——
        #   别把两者混成一条，那样两边都测不到。
        if _is_wrap_up(messages):
            return types.SimpleNamespace(content="", tool_calls=None,
                                         total_tokens=1, reasoning_content=None)
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if step.get("cancel"):
            from core.cancellation import request_cancel
            request_cancel(self._sess)
        return types.SimpleNamespace(
            content=step.get("content", ""),
            tool_calls=step.get("tool_calls"),
            total_tokens=step.get("tokens", 5),
            reasoning_content=None,
        )


class _StreamProvider:
    _model = "probe-model"

    def __init__(self, script, sess):
        self._script, self._i, self._sess = script, 0, sess

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        # 收尾那次调用没有工具 —— 与 sync 逐字同一条（见上面那段）。
        if _is_wrap_up(messages):
            yield StreamChunk(finish_reason="stop",
                              usage={"prompt_tokens": 1, "completion_tokens": 0})
            return
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if step.get("cancel"):
            from core.cancellation import request_cancel
            request_cancel(self._sess)
        if step.get("content"):
            yield StreamChunk(content=step["content"], finish_reason="stop")
        for i, tc in enumerate(step.get("tool_calls") or []):
            yield StreamChunk(tool_call={"id": tc["id"], "type": "function",
                                         "function": tc["function"]},
                              tool_call_index=i)
        yield StreamChunk(finish_reason="tool_calls" if step.get("tool_calls") else "stop",
                          usage={"prompt_tokens": 1, "completion_tokens": 1})


def _setup(sess):
    import bus.registry as R
    from core.cancellation import clear

    R.reset()
    clear(sess)
    calls = []

    def _handler(**kw):
        calls.append(kw)
        return "ok"

    R.register_tool("probe_tool", _handler,
                    {"description": "probe", "inputSchema": {"type": "object"}})

    # 一个自己置中断标志的探针 —— 用来确定性地制造
    # "工具执行期间用户按了停"那一刻（脚本化桩做不到：它只能在模型调用期间插手）。
    def _cancel_handler(**kw):
        calls.append(kw)
        from core.cancellation import request_cancel
        request_cancel(sess)
        return "cancelled-in-flight"

    R.register_tool("cancel_probe_tool", _cancel_handler,
                    {"description": "probe", "inputSchema": {"type": "object"}})
    R.note_discovered(sess, ["cancel_probe_tool"])
    R.note_discovered(sess, ["probe_tool"])   # 在"本轮暴露集"里
    return R, calls


def _run_sync(script, pre_cancel=False, sess=_SESS, max_turns=15):
    _, calls = _setup(sess)
    events = []
    from core.agent_loop import agent_loop
    if pre_cancel:
        from core.cancellation import request_cancel
        request_cancel(sess)
    content, messages, _, _ = agent_loop(
        "hi", _SyncProvider(script, sess), session_id=sess,
        _emit=lambda kind, **kw: events.append(kind),
        max_turns=max_turns)
    return content, messages, calls, events


def _run_stream(script, pre_cancel=False, sess=_SESS, max_turns=15):
    _, calls = _setup(sess)
    events = []
    from core.agent_loop import agent_loop_stream
    if pre_cancel:
        from core.cancellation import request_cancel
        request_cancel(sess)

    async def _drain():
        last = None
        async for st in agent_loop_stream(
                "hi", _StreamProvider(script, sess), session_id=sess,
                _emit=lambda kind, **kw: events.append(kind),
                max_turns=max_turns):
            last = st
        return last

    state = asyncio.run(_drain())
    return state[0], state[1], calls, events


def _dangling(messages):
    """`tool_call` 与 `role: tool` 的差 —— 悬空引用（上游会 400）。"""
    need = [tc["id"] for m in messages
            if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])]
    have = [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]
    return [i for i in need if i not in have]


_BOTH = (("sync", _run_sync), ("stream", _run_stream))


# ------------------------------------------------------------------
# 1. 中断：迭代开头
# ------------------------------------------------------------------

def test_cancel_at_turn_top_brakes_both_paths_identically():
    got = {}
    for name, run in _BOTH:
        content, messages, calls, events = run([{"content": "不该走到这里"}], pre_cancel=True)
        assert "已按你的要求中断本轮" in content, f"{name}: 中断文案不对: {content!r}"
        assert calls == [], f"{name}: 中断之后还派发了工具 {calls}"
        assert events == [], (
            f"{name}: 迭代开头那道闸**刻意不发事件**（那一轮连 turn.start 都没发过 "
            f"—— 给它发收尾才是错的），实得 {events}"
        )
        got[name] = content
    assert got["sync"] == got["stream"], f"两条路的文案不一致: {got}"
    print("  [ok] 中断（迭代开头）：两条路一致，且都不发事件")


# ------------------------------------------------------------------
# 2. 中断：工具派发前（轮内）
# ------------------------------------------------------------------

def test_cancel_during_the_model_call_brakes_both_paths_identically():
    """在模型调用期间按了打断，直接掐，不接收模型回复。

    这条盯的是模型调用期间那个检查点；下一条盯工具执行期间。
    两个检查点各自确定。

    判据的要点是「不接收那条回复」：`assistant` 那条带 `tool_calls` 的
    消息根本没进 `messages`，没有悬空引用要补。
    """
    script = [{"cancel": True, "tool_calls": [_tool_call("probe_tool", {"a": 1}, "call_1")]}]
    got = {}
    for name, run in _BOTH:
        content, messages, calls, events = run(script)
        assert content == CANCEL_REPLY, f"{name}: 文案不对: {content!r}"
        assert calls == [], f"{name}: 都掐了，却还是派发了工具 {calls}"
        assert not _dangling(messages), (
            f"{name}: 留下了悬空 tool_call {_dangling(messages)} —— "
            f"下一轮把这段历史发回上游会硬 400"
        )
        # 这一条是"不接收回复"的判据：整条 assistant 消息都没落
        assert not any(m.get("role") == "assistant" for m in messages), (
            f"{name}: 那条被掐掉的模型回复落进历史了"
        )
        assert events[0] == "turn.start" and events[-1] == "turn.end", (
            f"{name}: 这一轮**已经开始过**，收尾事件必须成对，实得 {events}"
        )
        got[name] = (content, events)
    assert got["sync"] == got["stream"], f"两条路不一致:\n  sync={got['sync']}\n  stream={got['stream']}"
    print("  [ok] 中断（模型调用期间）：两条路一致，且都没接收那条回复")


def test_cancel_during_a_tool_brakes_both_paths_identically():
    """在工具执行期间按了打断，直接掐，并留一句话给下一轮。

    这一条比上面那条多两件：
      · 本批里后面的 `tool_call` 要补空结果（悬空，上游 400）
      · 记一笔留给下一轮（`note_tool_interrupt`）—— 而这一轮不当场说话
        （返回空串；用户按的是"停"，此刻多说一句是插嘴）
    """
    script = [{"tool_calls": [
        _tool_call("cancel_probe_tool", {"a": 1}, "call_1"),
        _tool_call("probe_tool", {"a": 2}, "call_2"),
    ]}]
    got = {}
    for name, run in _BOTH:
        content, messages, calls, events = run(script)
        assert content == "", f"{name}: 工具被打断那一支**不该对用户说话**，实得 {content!r}"
        assert calls == [{"a": 1}], (
            f"{name}: 只有第一个该跑（它自己置的中断），实得 {calls}")
        assert not _dangling(messages), (
            f"{name}: 悬空 {_dangling(messages)} —— 没给后面那个补空结果")
        assert any(m.get("role") == "tool" and "skipped" in str(m.get("content"))
                   for m in messages), f"{name}: 没补空结果"
        assert events[0] == "turn.start" and events[-1] == "turn.end", (
            f"{name}: 收尾事件必须成对，实得 {events}")
        # 下一轮要兑现的那句话，此刻必须已经记下
        from core.cancellation import take_tool_interrupt
        assert take_tool_interrupt(_SESS), f"{name}: 没记下『上一轮被打断了』"
        got[name] = (content, events, len(calls))
    assert got["sync"] == got["stream"], f"两条路不一致: {got}"
    print("  [ok] 中断（工具执行期间）：两条路一致，补了空结果，且记下了下一轮那句话")


# 一条现在没有确定性夹具的检查点，如实记在这里：
#   `agent_loop` 里"工具派发前"那道闸。
#   它的窗口是「落地后复查」与「派发」之间 —— 而那段里唯一耗时的是
#   `_prefetch_readonly`（它把只读工具丢进线程跑），要触发它，标志必须
#   在那段时间里被另一个线程设上，脚本化桩做不到。
#   它不是死代码：用户在预取期间按停正好落在这里。
#   它与上面两条共用同一套收口（`_fill_skipped_tool_calls` + `_end_turn`），
#     而那一套是确定性的、两条都测了 —— 这也是这里没有硬造一个夹具的理由
#     （造不出来的时候，写一条会自己跳过的假夹具比没有更坏）。


# ------------------------------------------------------------------
# 3. 重复工具调用断路器
# ------------------------------------------------------------------

class _WrapProvider:
    """刹车之后那一次收尾调用单独回答，并把 `tools` 的条数记下来。"""

    _model = "probe-model"

    def __init__(self, script, sess, wrap_text=None, wrap_tool_calls=None):
        self._p = _SyncProvider(script, sess)
        self.calls = []            # 每次调用拿到的 tools 条数
        self._text = wrap_text
        self._tc = wrap_tool_calls

    def chat(self, messages, tools, system=None, thinking=None):
        self.calls.append(len(tools or []))
        if _is_wrap_up(messages):   # ← 收尾那次
            return types.SimpleNamespace(content=self._text or "",
                                         tool_calls=self._tc,
                                         total_tokens=1, reasoning_content=None)
        return self._p.chat(messages, tools, system=system, thinking=thinking)


def test_the_brake_makes_the_model_wrap_up_instead_of_dropping_the_work():
    """被刹车掐掉的不该是"用户的活"，只该是"这一次循环"。

    自动刹车之后如果直接回一句系统提示，用户可能已经付出了一整轮的工作
    （读题面、读参考图、探数据）却拿到零交付。

    判据两条：

    ① 刹车之后确实又问了模型一次，而且那一次 `tools=[]`
       —— 转圈物理上不可能。这一条是"它不是补丁"的全部理由：
       我们换的是它的行动空间，不是再赌一次它会正常。
    ② 它带回来的那段话就是给用户的回复（没被丢掉、也没被原文案盖住）。

    负对照：收尾那次它若还想调工具，不采信，退回原文案
       —— 把"模型又转圈"变成"最坏等于今天"，绝不因此更糟。

    还有一条：那条收尾指令不许进 `messages` —— 它是尾声调用的输入，
       落库会把"框架说的话"变成对话的一部分。
    """
    same = {"tool_calls": [_tool_call("probe_tool", {"a": 1}, "call_x")]}
    script = [same, same, same, {"content": "done"}]
    WRAP = ("做到哪：**已查清** 5 张表的行数 / 还差：4 个真透视表 / "
            "下一步：让我接着做")
    from core.agent_loop import agent_loop

    # ① 正常收尾：工具关掉、它交代、那段话就是回复
    _setup(_SESS)
    p = _WrapProvider(script, _SESS, wrap_text=WRAP)
    content, messages, _sc, _tk = agent_loop("hi", p, session_id=_SESS, _emit=None)
    assert p.calls and p.calls[-1] == 0, (
        f"刹车之后那次调用**不是** `tools=[]`（实得 {p.calls}）—— 它还能继续转圈，"
        f"那就只是换了个地方赌")
    assert content == WRAP, (
        f"收尾那段话没被当成回复（实得 {content!r}）—— 用户还是什么都没拿到")
    assert not any("自动刹车" in str(m.get("content") or "") for m in messages), (
        "那条收尾**指令**进了历史 —— 它只是尾声调用的输入，不该落进 `messages`")

    # ② 负对照：收尾时它还想调工具，退回原文案
    # 必须同时给它一段非空正文 —— 只给 `tool_calls`、正文留空的话，
    #    "采信"与"不采信"结果一样（都落到空串，都退回原文案），
    #    于是这条负对照结构上打不中靶子（变异打上去照样绿 —— 实测抓到过）。
    _setup(_SESS)
    p2 = _WrapProvider(script, _SESS, wrap_text="（这个正文不该被采信）",
                       wrap_tool_calls=[_tool_call("probe_tool", {"a": 9}, "z1")])
    content2, _m2, _c2, _t2 = agent_loop("hi", p2, session_id=_SESS, _emit=None)
    assert "连续 3 次" in content2, (
        f"收尾里它**又想去调工具**，却没退回原文案（实得 {content2!r}）—— "
        f"那等于把「最坏情况」做得比今天更糟")
    print("  [ok] 刹车 -> 关掉工具让它收尾；它若还想调工具则退回原文案")


def test_repeat_breaker_brakes_both_paths_identically():
    same = {"tool_calls": [_tool_call("probe_tool", {"a": 1}, "call_x")]}
    script = [same, same, same, {"content": "done"}]
    got = {}
    for name, run in _BOTH:
        content, messages, calls, events = run(script)
        assert "连续 3 次" in content, f"{name}: 断路器没触发或文案不对: {content!r}"
        assert len(calls) == 2, (
            f"{name}: 前两次该真跑、第三次该被拦，实得派发 {len(calls)} 次"
        )
        assert not _dangling(messages), f"{name}: 悬空 {_dangling(messages)}"
        got[name] = (content, len(calls))
    assert got["sync"] == got["stream"], f"两条路不一致: {got}"
    print("  [ok] 重复断路器：两条路的阈值与文案一致（第三次被拦）")


# ------------------------------------------------------------------
# 5. `max_turns` 耗尽 —— 两条路必须说同一句话
# ------------------------------------------------------------------
#
# [!] 两条路必须说同一句话：同一次刹车，一条说中文并带上「已完成的步骤都
#    保留在会话里」、另一条说英文旧文案且少了那半句 —— 用户得到的解释会随
#    "有没有开流式"而变。四个基础场景覆盖不到这条路径，所以单列一条。
#
# 脚本必须用不同参数：同参数会先撞上「重复断路器」（场景 3），
#    那就测不到 max_turns 这条路了 —— 夹具到不了的条件 = 没测到。
def test_max_turns_brakes_both_paths_identically():
    script = [
        {"tool_calls": [_tool_call("probe_tool", {"a": 1}, "c1")]},
        {"tool_calls": [_tool_call("probe_tool", {"a": 2}, "c2")]},
    ]
    got = {}
    for name, run in _BOTH:
        content, messages, calls, events = run(script, max_turns=2)
        assert "已达到单轮工具调用上限 2 次" in content, (
            f"{name}: max_turns 耗尽的收尾文案不对: {content!r}"
        )
        assert "已完成的步骤都保留在会话里" in content, (
 f"{name}: 少了那半句「已经做的没丢」—— 那正是 #7 抱怨的那点"
        )
        assert len(calls) == 2, f"{name}: 两轮各派发一次，实得 {len(calls)}"
        assert not _dangling(messages), f"{name}: 悬空 {_dangling(messages)}"
        # 收尾回复必须追加回 `messages` —— 两条路都要。
        #   判据是落库那条路只存 `messages`（`record_turn` 在 `messages is not None`
        #   时忽略 `assistant_message`），不在里面 = 用户看得到、库里没有。
        #   恰好 1 条：0 条 = 不落库 · 2 条 = 写重了。
        _hits = [m for m in messages
                 if m.get("role") == "assistant" and content in (m.get("content") or "")]
        assert len(_hits) == 1, (
            f"{name}: 收尾回复在 `messages` 里应当**恰好一条**，实得 {len(_hits)} 条"
        )
        got[name] = content
    assert got["sync"] == got["stream"], (
        "两条路的收尾文案不一致（锁步）: sync=%r stream=%r"
        % (got["sync"], got["stream"])
    )
    print("  [ok] max_turns 耗尽：两条路逐字同文，且都说了「已完成的还在」")


# ------------------------------------------------------------------
# 4. 正常一轮：事件序列逐字相同
# ------------------------------------------------------------------# ------------------------------------------------------------------
# 4. 正常一轮：事件序列逐字相同
# ------------------------------------------------------------------

def test_normal_turn_emits_the_same_event_sequence_on_both_paths():
    script = [
        {"content": "我先读一下。", "tool_calls": [_tool_call("probe_tool", {"a": 1}, "call_1")]},
        {"content": "做完了。"},
    ]
    got = {}
    for name, run in _BOTH:
        content, messages, calls, events = run(script)
        assert len(calls) == 1, f"{name}: 实得 {calls}"
        assert content == "做完了。", f"{name}: {content!r}"
        assert not _dangling(messages), name
        got[name] = events

    # `delta` 只可能出现在流式那条路上 —— 它是"模型正在吐字"的增量，
    #    而 sync 路径拿到的是一整块回复（没有增量这个概念）。
    #比对时把它滤掉，单独断言它只属于流式。
    seq = {k: [e for e in v if e != "delta"] for k, v in got.items()}
    assert "delta" not in got["sync"], (
        f"sync 路径不该有 `delta`（它没有增量）: {got['sync']}"
    )
    assert "delta" in got["stream"], (
        f"流式路径**没有**发 `delta` —— 用户看不到字往外流: {got['stream']}"
    )
    assert seq["sync"] == seq["stream"], (
        f"两条路的**事件序列**（滤掉 delta 之后）不一致:\n"
        f"  sync={seq['sync']}\n  stream={seq['stream']}"
    )
    assert "tool.start" in got["stream"] and "tool.end" in got["stream"], got["stream"]
    assert "say" in got["stream"], (
        f"模型中途说的话没发给用户（`EVT_SAY`）: {got['stream']}"
    )
    print(f"  [ok] 正常一轮：两条路事件序列逐字相同（{len(seq['stream'])} 个"
          f" + 流式独有的 {len(got['stream']) - len(seq['stream'])} 个 delta）")


# ------------------------------------------------------------------
# 5. 锁步：技能索引两条路都必须带
# ------------------------------------------------------------------

class _CapturingSync:
    """记下每次调用真正收到的 messages。"""

    _model = "probe-model"

    def __init__(self, script, sess):
        self._p = _SyncProvider(script, sess)
        self.seen = []

    def chat(self, messages, tools, system=None, thinking=None):
        self.seen.append([dict(m) for m in messages])
        return self._p.chat(messages, tools, system=system)


class _CapturingStream:
    _model = "probe-model"

    def __init__(self, script, sess):
        self._p = _StreamProvider(script, sess)
        self.seen = []

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        self.seen.append([dict(m) for m in messages])
        async for ch in self._p.chat_stream(messages, tools, system=system):
            yield ch


def test_both_paths_carry_the_skill_index():
    """技能索引在两条路上都必须带，且只挂这一轮的第一次请求。

    少了这一段，同一会话的输入 token 会少约 296，
    也就是一打开流式，被移出常驻的那批工具就等于不可达。

    断言打在真正发出去的消息上（不是 grep 代码字面量 ——
    注释里也会出现 `<skill_index>`）。
    """
    script = [
        {"content": "我先读一下。", "tool_calls": [_tool_call("probe_tool", {"a": 1}, "call_1")]},
        {"content": "做完了。"},
    ]
    got = {}
    for name, script_ok in (("sync", _CapturingSync), ("stream", _CapturingStream)):
        _, calls = _setup(_SESS)
        p = script_ok(script, _SESS)
        if name == "sync":
            from core.agent_loop import agent_loop
            agent_loop("hi", p, session_id=_SESS, _emit=None)
        else:
            from core.agent_loop import agent_loop_stream

            async def _drain():
                async for _st in agent_loop_stream("hi", p, session_id=_SESS, _emit=None):
                    pass
            asyncio.run(_drain())

        first = p.seen[0]
        later = p.seen[1] if len(p.seen) > 1 else []
        has_first = any("<skill_index>" in str(m.get("content"))
                        for m in first if m.get("role") == "user")
        has_later = any("<skill_index>" in str(m.get("content"))
                        for m in later if m.get("role") == "user")
        got[name] = (has_first, has_later)

    assert got["sync"][0], "sync：第一次请求该带索引（夹具坏了？）"
    assert got["stream"][0], (
        "★ **流式路径没带技能索引** —— 模型于是不知道有哪些能力，"
        "被移出常驻的工具全部不可达（实测输入 token 少 296 就是它）"
    )
    assert not got["sync"][1] and not got["stream"][1], (
        f"索引**只该挂在第一次请求**上（之后 tools= 里已有真 schema，再带是白付钱）: {got}"
    )
    print("  [ok] 技能索引：两条路都带，且都只挂第一次请求")


def test_both_paths_carry_the_skill_guidance():
    """引导片段在两条路上都必须带。

    它与技能索引同一个挂载点（最后一条 user 消息的尾巴），但触发条件不同
    —— 索引是 `turn == 0`，本块是「通用纪律常开 + 领域片段看激活」。

    断言打在真正发出去的消息上（同上面那条的判据：代码注释里也有那个字面量）。
    两个方向都还在，只是分界线换了：块总在，
    而领域片段（`probe_skill` 的 `GUIDANCE.md`）只有激活才在 ——
    那一半是成本判据的守门人（领域片按请求计费）。
夹具里给它造一片真 `GUIDANCE.md`，否则两种配置逐字相同、这条会变成恒真。
    """
    script = [{"content": "做完了。"}]
    got = {}
    marker = _MARK
    with _temp_domain("probe_skill", marker + " 权威是 X。\n"):
        for name, cls in (("sync", _CapturingSync), ("stream", _CapturingStream)):
            row = {}
            for activated in (False, True):
                R, _calls = _setup(_SESS)
                # 两格会话态都要自己清：`R.reset()` 不清 `_discovered`
                #    （它只清工具/档位那几张表），而上一次循环正是靠它激活的
                #不清的话 `activated=False` 那一轮会继承上一轮的激活
                #    （症状：stream/False 也带了块）。
                R.forget_discovered(_SESS)
                R.note_discovered(_SESS, ["probe_tool", "cancel_probe_tool"])
                # 注册一个属于 skill 的探针工具：归属由 owner 决定，
                # 而"激活"只认 `skill:` 前缀（框架工具永远不触发）。
                R.register_tool("probe_skill_tool", lambda **kw: "ok",
                                {"description": "probe",
                                 "inputSchema": {"type": "object"}},
                                owner="skill:probe_skill")
                if activated:
                    R.note_discovered(_SESS, ["probe_skill_tool"])

                p = cls(script, _SESS)
                if name == "sync":
                    from core.agent_loop import agent_loop
                    agent_loop("hi", p, session_id=_SESS, _emit=None)
                else:
                    from core.agent_loop import agent_loop_stream

                    async def _drain():
                        async for _st in agent_loop_stream("hi", p, session_id=_SESS,
                                                           _emit=None):
                            pass
                    asyncio.run(_drain())

                head = " ".join(str(m.get("content")) for m in p.seen[0]
                                if m.get("role") == "user")
                row[activated] = ("<skill_guidance>" in head, marker in head)
            got[name] = row

    for name in ("sync", "stream"):
        has_block, has_domain = got[name][False]
        assert has_block, (
 f"★ {name}：没激活时就**完全没有**引导片段 —— 要的是"
            f"**通用纪律常开**（常驻技能工具永远不进 `_discovered`）: {got}")
        assert not has_domain, (
            f"★ {name}：没激活却带了**领域片段** —— 那一半仍然是按需的"
            f"（按请求计费）: {got}")
    for name in ("sync", "stream"):
        has_block, has_domain = got[name][True]
        assert has_block and has_domain, (
            f"★ {name}：skill 被激活了，块或领域片段没到 —— "
            f"夹具坏了？还是触发判据改了？（流式那条尤其："
            f"打开逐字输出就静默少一块，而没有任何地方会红）: {got}")
    print("  [ok] 引导片段：两条路都带；通用纪律常开、领域片段看激活")


def _first_divergence(a, b):
    """两次 payload 从第几条消息起不一样（全等则给 `min(len)`）。"""
    for j in range(min(len(a), len(b))):
        if a[j] != b[j]:
            return j
    return min(len(a), len(b))


def test_the_guidance_block_adds_no_new_cache_break():
    """引导片段不许引入新的分叉点（前缀缓存的判据）。

    这是这一笔里唯一一条关于钱的断言。挂载点或"挂不挂"的判据一旦选错
    —— 比如每轮换个位置、或者这轮挂下轮摘 —— 就会每一轮都废掉服务端的前缀缓存，
    也就是 `prompt cache` 全量重算（45x 那一类）。

    判据不是"后一次是前一次的前缀" —— 那条本来就假：
    技能索引只挂 `turn == 0`（没有代价，因为分叉点正好落在索引自己身上）。
    所以正确的判据是边界条件：
    有引导与没引导，相邻两次请求的分叉点位置必须一一相同 ——
    也就是"这块没有多带来一次缓存失效"。

    写关于钱的断言之前，先把已有的分叉点列出来 —— 否则会红在一条
    已知行为上，而不是红在你要测的那一改动上。
    """
    from core import skill_guidance as SG

    script = [
        {"content": "第一步。", "tool_calls": [_tool_call("probe_tool", {"a": 1}, "call_1")]},
        {"content": "第二步。", "tool_calls": [_tool_call("probe_tool", {"a": 2}, "call_2")]},
        {"content": "第三步。", "tool_calls": [_tool_call("probe_tool", {"a": 3}, "call_3")]},
        {"content": "做完了。"},
    ]
    got = {}
    for name, cls in (("sync", _CapturingSync), ("stream", _CapturingStream)):
        row = {}
        for activated in (False, True):
            R, _calls = _setup(_SESS)
            R.forget_discovered(_SESS)
            R.note_discovered(_SESS, ["probe_tool", "cancel_probe_tool"])
            R.register_tool("probe_skill_tool", lambda **kw: "ok",
                            {"description": "probe",
                             "inputSchema": {"type": "object"}},
                            owner="skill:probe_skill")
            if activated:
                R.note_discovered(_SESS, ["probe_skill_tool"])

            p = cls(script, _SESS)
            if name == "sync":
                from core.agent_loop import agent_loop
                agent_loop("hi", p, session_id=_SESS, _emit=None)
            else:
                from core.agent_loop import agent_loop_stream

                async def _drain():
                    async for _st in agent_loop_stream("hi", p, session_id=_SESS,
                                                       _emit=None):
                        pass
                asyncio.run(_drain())

            seen = p.seen
            has = any("<skill_guidance>" in str(m.get("content"))
                      for m in seen[0] if m.get("role") == "user")
            # 块总在（通用纪律常开）；激活只影响领域那一片。
            assert has, (
 f"{name}: 第 0 轮完全没有引导片段 —— 要的是通用纪律常开"
                f"（夹具坏了的话，下面那半条会变成恒真）")
            row[activated] = [_first_divergence(seen[i], seen[i + 1])
                              for i in range(len(seen) - 1)]
        got[name] = row

    for name in ("sync", "stream"):
        assert got[name][True] == got[name][False], (
            "★ %s：带上引导片段之后，**分叉点的位置变了** ——\n"
            "  没引导 %r\n  有引导 %r\n"
            "  -> 这一块每轮都在改写前缀缓存（前缀缓存被改写的代价）。\n"
            "  正确形状是：它挂在**最后一条 user 消息的尾巴**上，**引入之后不再摘**。"
            % (name, got[name][False], got[name][True]))
        assert got[name][True][0] == 0, (
            f"{name}: 夹具坏了 —— 第一对之间竟然没有分叉点（技能索引该制造一个）")
    print("  [ok] 引导片段：没引入新的分叉点（两条路，逐对相同）")


class _SyncActivateAfterTurn1(_CapturingSync):
    """第 0 轮之后才激活 —— 模拟"模型到第 5 轮才搜到那个工具"。"""

    def __init__(self, script, sess, tool):
        super().__init__(script, sess)
        self._sess, self._n, self._tool = sess, 0, tool

    def chat(self, messages, tools, system=None, thinking=None):
        out = super().chat(messages, tools, system=system, thinking=thinking)
        self._n += 1
        if self._n == 1:
            from bus import registry as _R
            _R.note_discovered(self._sess, [self._tool])
        return out


class _StreamActivateAfterTurn1(_CapturingStream):
    def __init__(self, script, sess, tool):
        super().__init__(script, sess)
        self._sess, self._n, self._tool = sess, 0, tool

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        self.seen.append([dict(m) for m in messages])
        self._n += 1
        if self._n == 1:
            from bus import registry as _R
            _R.note_discovered(self._sess, [self._tool])
        async for ch in self._p.chat_stream(messages, tools, system=system):
            yield ch


def test_the_guidance_shows_up_mid_task_and_then_stays():
    """中途才激活也要生效，而且生效之后不许再摘。

    两半，各打一个真会犯的错：

    · "只在第 0 轮挂" —— 那正是技能索引的形状（它是对的，因为它的判据
      就是 `turn == 0`）。照抄到引导片段上就错了：激活可能发生在更晚的轮次
      （模型到那时才 `search_tools` 到工具），纪律永远不出现。
    · "挂一次就不再挂" —— 第 k 轮有、第 k+1 轮没有，第 k+1 轮从那条
      user 消息起整段前缀作废。判据是
      第 k+1 轮必须是第 k 轮的延长。

    判"关于钱的事"之前，先把已有的分叉点列出来。
    """
    from core import skill_guidance as SG

    script = [
        {"content": "第一步。", "tool_calls": [_tool_call("probe_tool", {"a": 1}, "call_1")]},
        {"content": "第二步。", "tool_calls": [_tool_call("probe_tool", {"a": 2}, "call_2")]},
        {"content": "第三步。", "tool_calls": [_tool_call("probe_tool", {"a": 3}, "call_3")]},
        {"content": "做完了。"},
    ]
    _ctx = _temp_domain("probe_late", _MARK + " 权威是 Y。\n")
    _ctx.__enter__()
    try:
        for name, cls in (("sync", _SyncActivateAfterTurn1),
                          ("stream", _StreamActivateAfterTurn1)):
            R, _calls = _setup(_SESS)
            R.forget_discovered(_SESS)
            R.note_discovered(_SESS, ["probe_tool", "cancel_probe_tool"])
            R.register_tool("probe_late_tool", lambda **kw: "ok",
                            {"description": "probe",
                             "inputSchema": {"type": "object"}},
                            owner="skill:probe_late")

            p = cls(script, _SESS, "probe_late_tool")
            if name == "sync":
                from core.agent_loop import agent_loop
                agent_loop("hi", p, session_id=_SESS, _emit=None)
            else:
                from core.agent_loop import agent_loop_stream

                async def _drain():
                    async for _st in agent_loop_stream("hi", p, session_id=_SESS,
                                                       _emit=None):
                        pass
                asyncio.run(_drain())

            seen = p.seen
            assert len(seen) >= 3, f"{name}: 只发了 {len(seen)} 次请求，夹具坏了"

            def _has(msgs):
                return any("<skill_guidance>" in str(m.get("content"))
                           for m in msgs if m.get("role") == "user")

            def _domain(msgs):
                return any(_MARK in str(m.get("content"))
                           for m in msgs if m.get("role") == "user")

            # 块从第 0 轮就在（通用纪律常开）；领域那一片才是
            #   "中途激活也得跟上来"的那个对象。
            assert _has(seen[0]) and not _domain(seen[0]), (
                f"{name}: 第 0 轮该有通用纪律、不该有领域片段 —— 夹具坏了")
            assert _domain(seen[1]), (
                "★ %s：激活发生在第 0 轮之后，**第 1 轮的领域片段却没带上** —— "
                "只在 `turn == 0` 挂（照抄了技能索引的形状）？那样"
                "**中途激活等于不生效**。" % name)
            assert _has(seen[2]) and _domain(seen[2]), (
                "★ %s：第 1 轮挂了、第 2 轮又没了 —— **挂一次就摘**？" % name)
            d = _first_divergence(seen[1], seen[2])
            assert d == min(len(seen[1]), len(seen[2])), (
                "★ %s：第 2 轮**改写了第 1 轮的第 %d 条消息** —— 前缀缓存从那里"
                "起全部作废（前缀缓存被改写的代价）。\n  before: %r\n  after : %r"
                % (name, d, seen[1][d: d + 1], seen[2][d: d + 1]))
    finally:
        _ctx.__exit__()
    print("  [ok] 中途激活：下一轮就带上，且之后只延长不改写（两条路）")


# ------------------------------------------------------------------
# 6. 结构性锁步：sync 调用的功能钩子，流式那条也必须有
# ------------------------------------------------------------------

def test_streaming_path_calls_the_same_feature_hooks_as_sync():
    """两条路的功能钩子必须成对 —— 这是"打地鼠"的终结者。

    典型的漂移是"sync 有、stream 没有"，形状一模一样：

        技能索引整段缺失（输入 token 少 296）
        `search_tools` 不记发现（`note_discovered` 零调用），搜到了也调不到
        历史自洽检查没有（`check_history` 零调用），不平衡的历史静默 400

    一条条补是打地鼠；这条按 AST 把同一份清单在两条路上各查一遍
    （AST 不是 grep —— 注释里出现这些名字不算数）。
    """
    import ast as _ast

    src = (_PROJECT_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)

    hooks = ["build_skill_index", "append_skill_index",
             # 记忆索引与技能索引同一族 —— 只补一条路
             # 就是"打开流式就没有长期记忆索引"。
             "build_memory_index", "append_memory_index",
             "note_discovered",
             "check_history", "_fill_skipped_tool_calls", "is_cancelled",
             "describe_tool", "arg_hint", "clear_cancel",
             # 呈现与执行的同一个判定。只补 sync 那条路就是这个测试要治的病 ——
             # 拒绝对两条路必须给同一个答案，否则"开关一开，围栏就没了"。
             "is_tool_active", "_not_offered_message",
             # 名字不存在那条分支的拒绝话术。只有 sync 那条路带信息的话，
             # 打开逐字输出就退回一句 `Error: unknown tool 'x'` —— 静默的降级。
             "_unknown_tool_message",
             # 参数解析的唯一入口。只有 sync 那条路设防 = 打开流式就能带着空参跑。
             "_parse_tool_args", "_bad_args_message",
             # 子 agent 的派发与"子里不许开审批"那道闸。流式那条路漏了它
             #打开逐字输出就没有子 agent，而且子 agent 会拿一个
             # 永远兑现不了的审批请求（挂死父那一轮）。
             "_dispatch_subagent", "_approval_in_child_message",
             "depth",
             # 群成员派活。只接一条路就是"打开逐字输出，群里的成员派不出去"，
             # 而它一个字都不会红（`ask_member` 照在 `tools=` 里）。
             "dispatch_from_session",
             # 记忆抽取 fork 的执行白名单（拒的话术与判定都得成对 ——
             # 只有 sync 设防 = 打开逐字输出白名单就没了）。
             "_allowlist_denied_message",
             # skill 级引导片段。挂钩子（生成/挂载）两处都得成对，少一处
             # 就是"打开逐字输出就没有引导片段"（而它一个字都不会红）。
             # `note_skill_used` 不在清单里 —— "被调用也算激活"那个判据已删除。
             "build_skill_guidance", "append_skill_guidance",
             # 运行时事故摘要。漏一条路就是"打开逐字输出，用户问『这个横幅
             # 什么意思、帮我修』，模型手里没有账本"。
             "build_note", "append_incident_note",
             # 刹车之后的收尾调用。漏一条路就是"打开逐字输出，
             # 被刹车的用户还是零交付"。
             "_brake_wrap_up",
             # 编程纪律：写代码后的验证路线 + 交卷前闸。漏一条路就是
             # "打开逐字输出，改完不验也能交"。
             "observe", "take_verify_nudge"]

    bodies = {}
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and \
                node.name in ("agent_loop", "agent_loop_stream"):
            names = set()
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Call):
                    f = sub.func
                    names.add(getattr(f, "id", None) or getattr(f, "attr", ""))
            bodies[node.name] = names

    assert set(bodies) == {"agent_loop", "agent_loop_stream"}, (
        f"没找到那两个函数（改名了？）: {sorted(bodies)}"
    )
    print("  [检查] sync 调用 %d 个名字 / stream 调用 %d 个"
          % (len(bodies["agent_loop"]), len(bodies["agent_loop_stream"])))

    missing = [h for h in hooks if h in bodies["agent_loop"]
               and h not in bodies["agent_loop_stream"]]
    assert not missing, (
        f"★ 这些钩子 **sync 有、流式没有**：{missing} —— "
 f"打开流式之后它们会静默失效（实测就是撞了三处这样的洞）。"
        f"要补，或者把它显式写进本测试的『已知差异』里并说明为什么。"
    )
    assert len([h for h in hooks if h in bodies["agent_loop"]]) >= 8, (
        "清单里落在 sync 那条路上的钩子太少了 —— 这条断言本身在失去靶子"
    )
    print("  [ok] 两条路的功能钩子成对（%d 个）" % len(hooks))


def test_the_two_paths_differ_only_where_documented():
    """两条路的已知差异必须是写下来的那几个，不许悄悄多。"""
    import ast as _ast

    src = (_PROJECT_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    bodies = {}
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and \
                node.name in ("agent_loop", "agent_loop_stream"):
            names = set()
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Call):
                    f = sub.func
                    names.add(getattr(f, "id", None) or getattr(f, "attr", ""))
            bodies[node.name] = names

    only_sync = sorted(bodies["agent_loop"] - bodies["agent_loop_stream"])
    # 同步路径独有、而我们已知并接受的（每条都要说得出理由）
    accepted = {
        "chat",                    # 接口不同：sync 用 chat，流式用 chat_stream
        "_compress_cross_zone_noise",       # 已知缺口（跨区噪声压缩），未镜像
        "_cross_zone_snapshot_index",       # 同上（它只是那个缺口的配套变量）
        # 同名不同物：sync 那条路还剩一处 `json.loads`，解析的是工具结果
        # （跨区快照，`:714`），不是参数 —— 它就是上面那个未镜像的
        # `_compress_cross_zone_noise` 缺口的配套。
        # 参数解析收成一个入口之后，这个调用才从交集里显形。
        "loads",
        # 「直接掐」：同一个功能，两条路用了不同机制 ——
        # 这是有意的，不是漏镜像。
        # · sync 那条只有一个等待点（整条响应），必须把调用丢进
        #   `ThreadPoolExecutor` 再轮询，才有第二个可检查的时刻；
        # · 流式那条天然有轮询点（每个分片之间），一行 `is_cancelled` 就够，
        #   不需要线程。
        #差别是 `_chat_cancellable` 这一个 helper 的存在与否，
        #   而用户可见行为、文案、检查点位置三样都逐字相同
        #   （`CANCEL_REPLY` 是同一个常量，两条路都 `yield` 它一次）。
        # 真去"镜像"它反而是错的：把流式也塞进线程池，等于给一条
        #   本来就是增量的路加一层缓冲。
        "_chat_cancellable",
        # 「被上游截断不许当成说完了」：**同一个判据，
        # 两条路取值的载体不同** —— 这是有意的，不是漏镜像。
        # · sync 那条手里是一个响应对象（`ChatResponse`），从它身上读字段；
        # · 流式那条手里是一条流，`finish_reason` 是攒在
        #   `stream_finish` 里的一个字符串，没有对象可读。
        #差别只是"从对象取"还是"从字符串取"这一步，
        #   判据（`_terminal_reply`）与那句话两条路是同一个函数、同一份文案。
        # 真去"镜像"它 = 给流式那条假造一个对象来满足对称，那是本末倒置。
        "_finish_of",
        # 「压缩回调」：同一个动作，两条路的等待方式不同。
        # · sync 那条直接 `compact_cb()`（它本来就在一个工作线程里跑）；
        # · 流式那条 `await asyncio.to_thread(compact_cb)` —— 压缩是一次
        #   同步的摘要调用，不许在事件循环里阻塞（会掐掉别的流的心跳）。
        #差别只是"怎么等"，压什么、压完怎么替换都逐字相同。
        "compact_cb",
        #  批 2：`_media_artifacts` 已镜像到流式那条（流里攒下的
        #   媒体在流结束时收口），它从这张清单里销账了。别再加回来：
        #   那等于宣布"流式又丢媒体"，而 `test_artifact_inline` 的场景 2b 会红。
        "get", "items", "print", "len", "any", "range", "str", "append", "index",
        "format", "setdefault", "startswith", "keys", "endswith", "lower", "first",
        "join", "getattr", "deepcopy", "list", "isinstance", "int", "add", "copy",
    }
    unexpected = [n for n in only_sync if n not in accepted]
    assert not unexpected, (
        f"sync 独有、而**没有登记为已知差异**的调用：{unexpected} —— "
        f"要么镜像到流式那条路，要么加进 `accepted` 并写明理由"
    )
    print("  [ok] sync 独有调用都在已登记的差异清单里（%d 个）" % len(only_sync))


# ------------------------------------------------------------------

def _main():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
