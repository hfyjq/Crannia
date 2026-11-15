"""呈现与执行走同一个判定的不变量。

## 它治的是什么

`tools=` 实际上只是建议。`get_tool_handler(name)` 查的是全量注册表
（约 100 个工具），不带会话、不查本轮 wire 集 —— 唯一失败路径是 `handler is None`
（`"unknown tool"`）。本轮没发给模型的工具，模型只要报出名字就能执行。

那两张表的形状：

    core/agent_loop.py  handler = get_tool_handler(tool_name)   # 两个派发口都是它
    bus/registry.py     return _registry["tools"].get(name)     # 全量，不看会话

两条真实后果：
  1. 模型可以调一个没拿到 schema 的工具，参数只能瞎猜；而"坏 JSON 被静默降成 `{}`"
变成一次静默空参调用
  2. 任何"禁用一个工具"的意图（权限层、将来的 enable/disable）
     在这个边界上不成立

判据：只藏 schema 而只过滤呈现，模型仍能执行一个提示里说不存在的工具；
呈现与执行必须由同一个判定统一管辖。

## 钉住的七件事

  1. 一个判定，两个视图 —— `get_tool_schemas_active`（呈现）与
     `is_tool_active`（执行）都只能经 `get_active_tool_names`（AST 反查）
  2. 没呈现的工具：handler 一次都不许被调用（不是"返回错误"，是没碰）
  3. 拒绝必须带路线，且那条路线真的走得通（`search_tools('<name>')`
     真的能返回它）—— 给一条走不通的路线比不给更坏，模型会原地转圈
  4. 拒绝必须回填成 `role: tool` —— 那条 `tool_call` 已随 assistant 消息
     进了历史，不回填就是悬空引用，下一轮发回上游直接 400
  5. 正对照：同一个调用，`note_discovered` 之后必须跑起来 ——
     没有它，"永远拒绝"也能让前四条全绿
  6. 锁步：流式那条路给出逐字相同的拒绝
  7. `_parser` 不许被门禁拦住 —— `_auto_parse` 拿 handler 是框架自己在做事
     （模型给的是 `file_path`，不是工具名），拦住它 = 摘要/检索整条路失效

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_tool_gate.py
"""

import ast
import asyncio
import json
import sys
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_SCRIPTS = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from providers.base import StreamChunk  # noqa: E402

# 模式影响派发，本文件必须自己钉模式。
# 不钉就会继承开发机 `data/settings.json` 的 `default_mode`：
# `plan`，写工具被计划模式的闸拒掉（症状是 `calls == []`）·
# `auto`，闸去调分类器（吃掉 fake provider 的脚本化回复）。
# 用一次 `/mode plan` 就可能让6 个文件一次全红，
# 而它们一个字都不该关心模式。触发路径见 `tests/_sandbox.py::neutralise_settings`。
from _sandbox import neutralise_settings  # noqa: E402
neutralise_settings()


# ASCII-only 输出 —— 控制台默认 GBK，emoji 会让整个运行崩掉。

_SESS = "probe:gate"


def _tool_call(name, args, id):
    return {"id": id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class _SyncProvider:
    _model = "probe-model"

    def __init__(self, script):
        self._script, self._i = script, 0

    def chat(self, messages, tools, system=None, thinking=None):
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return types.SimpleNamespace(
            content=step.get("content", ""),
            tool_calls=step.get("tool_calls"),
            total_tokens=5,
            reasoning_content=None,
        )


class _StreamProvider:
    _model = "probe-model"

    def __init__(self, script):
        self._script, self._i = script, 0

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if step.get("content"):
            yield StreamChunk(content=step["content"], finish_reason="stop")
        for i, tc in enumerate(step.get("tool_calls") or []):
            yield StreamChunk(tool_call={"id": tc["id"], "type": "function",
                                         "function": tc["function"]},
                              tool_call_index=i)
        yield StreamChunk(
            finish_reason="tool_calls" if step.get("tool_calls") else "stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1})


def _setup(sess, discover=True, tool="probe_hidden", extra=None):
    """装一个不在常驻集里的探针工具。

    `discover=False` 是负向那一半：注册表里有它（`get_tool_handler`
    会返回 handler）、但模型这一轮没被发过它。

    `extra` 必须走这个参数、不能在外面 `register_tool` —— 这个函数会
    `R.reset()`，在它之后注册的东西会被 `_run_*` 里那次重新装载抹掉，
    于是测试会因为"工具没注册"而红，理由却看起来像门禁的错。
    """
    import bus.registry as R
    from core.cancellation import clear

    R.reset()
    R.forget_discovered(sess)
    clear(sess)
    calls = []

    def _handler(**kw):
        calls.append(kw)
        return "ok"

    R.register_tool(tool, _handler,
                    {"description": "probe", "inputSchema": {"type": "object"}})
    if extra:
        R.register_tool(extra[0], extra[1],
                        {"description": "probe", "inputSchema": {"type": "object"}})
    if discover:
        R.note_discovered(sess, [tool])
    return R, calls


def _run_sync(script, sess=_SESS, discover=True, tool="probe_hidden", extra=None):
    _, calls = _setup(sess, discover=discover, tool=tool, extra=extra)
    from core.agent_loop import agent_loop
    content, messages, _, _ = agent_loop(
        "hi", _SyncProvider(script), session_id=sess)
    return content, messages, calls


def _run_stream(script, sess=_SESS, discover=True, tool="probe_hidden", extra=None):
    _, calls = _setup(sess, discover=discover, tool=tool, extra=extra)
    from core.agent_loop import agent_loop_stream

    async def _drain():
        last = None
        async for st in agent_loop_stream(
                "hi", _StreamProvider(script), session_id=sess):
            last = st
        return last

    state = asyncio.run(_drain())
    return state[0], state[1], calls


def _tool_results(messages):
    return [m for m in messages if m.get("role") == "tool"]


def _tool_calls_in(messages):
    out = []
    for m in messages:
        if m.get("role") == "assistant":
            out.extend(m.get("tool_calls") or [])
    return out


def _dangling(messages):
    """`tool_call` 有、`role: tool` 结果没有 —— 上游会 400 的那种悬空。"""
    want = [tc.get("id") for tc in _tool_calls_in(messages)]
    have = [m.get("tool_call_id") for m in _tool_results(messages)]
    return [i for i in want if i not in have]


_ONE_CALL = [{"tool_calls": [_tool_call("probe_hidden", {"a": 1}, "call_1")]},
             {"content": "done"}]


# ------------------------------------------------------------------
# 1. 一个判定，两个视图（AST：唯一真值源是 get_active_tool_names）
# ------------------------------------------------------------------

def test_both_views_go_through_one_resolver():
    """呈现与执行不许各算各的 —— 两个视图都必须经 `get_active_tool_names`。

    理由：这两处只要有一处自己算一遍（"常驻集在后面 30 行抄一份"），
    它们就会分叉而无人报警 —— 而"呈现与执行不同判定"正是这个洞本身。
    AST 而不是 grep：这两个函数的 docstring 里就写着 `get_active_tool_names`。
    """
    tree = ast.parse((_PROJECT_ROOT / "bus" / "registry.py").read_text(encoding="utf-8"))
    bodies = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                "get_tool_schemas_active", "is_tool_active"):
            bodies[node.name] = {
                getattr(sub.func, "id", None) or getattr(sub.func, "attr", "")
                for sub in ast.walk(node) if isinstance(sub, ast.Call)
            }
    assert set(bodies) == {"get_tool_schemas_active", "is_tool_active"}, (
        f"找不到那两个视图（改名了？）: {sorted(bodies)}")
    bad = [n for n, calls in bodies.items() if "get_active_tool_names" not in calls]
    assert not bad, (
        f"这些视图没有走 `get_active_tool_names`：{bad} —— "
        f"它们必须共用同一个判定，不是各算一遍"
    )
    print("  [ok] 呈现与执行共用 get_active_tool_names（2 个视图）")


def test_gate_and_wire_agree_on_the_same_session():
    """同一个会话上：wire 里没有它、判定也说不行；发现之后两边同时变行。

    这是第 1 条的行为侧 —— AST 能证明"都调了那个函数"，不能证明"调了之后
    真的同源"。两个方向都断言，免得它变成恒真式。
    """
    R, _ = _setup("probe:gate:agree", discover=False)
    wire = {t["function"]["name"] for t in R.get_tool_schemas_active("probe:gate:agree")}
    assert "probe_hidden" not in wire, "夹具不成立：探针一开始就在 wire 上"
    assert R.is_tool_active("probe:gate:agree", "probe_hidden") is False, (
        "wire 里没有它、判定却说行 —— 两个视图已经开始分叉了")

    R.note_discovered("probe:gate:agree", ["probe_hidden"])
    wire2 = {t["function"]["name"] for t in R.get_tool_schemas_active("probe:gate:agree")}
    assert "probe_hidden" in wire2 and R.is_tool_active("probe:gate:agree", "probe_hidden")
    print("  [ok] wire 与判定同进同退")


# ------------------------------------------------------------------
# 2/3/4/5. 执行侧：拒绝的形状 + 正对照
# ------------------------------------------------------------------

def test_hidden_tool_is_never_called():
    """没呈现的工具：handler 一次都不许被调用。

    判据是"没碰"而不是"返回了错误" —— 后者在 handler 有副作用
    （写文件、发信、跑命令）时已经晚了。
    """
    content, messages, calls = _run_sync(_ONE_CALL, discover=False)
    assert calls == [], f"没呈现的工具被执行了：{calls}"
    results = _tool_results(messages)
    assert results, "拒绝没有回填成 role: tool（悬空引用）"
    assert "probe_hidden" in results[-1]["content"], results[-1]["content"]
    print("  [ok] 未呈现的工具：handler 零调用，且拒绝了")


def test_refusal_carries_a_route_that_actually_works():
    """拒绝必须带路线，而那条路线真的走得通。

    给一条走不通的路线比不给更坏：模型照做一次、发现不行，就会开始
    换着名字试（转圈），而日志里看起来只是"模型很笨"。
    所以这里拿真 `search_tools` 去走一遍那句话说的方法。
    """
    import bus.registry as R
    R2, _ = _setup("probe:gate:route", discover=False)
    _content, messages, _calls = _run_sync(_ONE_CALL, sess="probe:gate:route",
                                           discover=False)
    msg = _tool_results(messages)[-1]["content"]
    assert "search_tools('probe_hidden')" in msg, (
        f"拒绝没带路线（模型只会换个名字接着试）: {msg}")

    # 路线真的走得通 —— 用真工具、真查询跑一遍
    found = R2.search_tools("probe_hidden")
    names = [t["name"] for t in found.get("tools", [])]
    assert "probe_hidden" in names, (
        f"拒绝里让模型去 search_tools('probe_hidden')，而它搜不到：{names} —— "
        f"这条路线是空头支票"
    )
    print("  [ok] 拒绝带路线，且路线真的到得了")


def test_refusal_is_written_back_as_a_paired_tool_message():
    """拒绝必须以原 `tool_call_id` 回填 —— 否则悬空引用，上游硬 400。

    这条是"拒绝"与"跳过"的分界：跳过（`messages.pop` / `continue`）能让
    前两条照样绿，而把下一轮直接打死（实测过那个 400）。
    """
    for runner, label in ((_run_sync, "sync"), (_run_stream, "stream")):
        content, messages, calls = runner(_ONE_CALL, sess=f"probe:gate:pair:{label}",
                                          discover=False)
        assert calls == [], f"{label}: 没呈现的工具被执行了"
        assert _dangling(messages) == [], (
            f"{label}: 拒绝留下了悬空 tool_call {_dangling(messages)} —— "
            f"下一轮发回上游会 400")
        results = _tool_results(messages)
        assert results[-1].get("tool_call_id") == "call_1", (
            f"{label}: 回填没配对到原 tool_call_id: {results[-1]}")
    print("  [ok] 两条路的拒绝都配对回填（无悬空）")


def test_discovered_tool_runs_positive_control():
    """正对照：同一次调用，发现之后必须跑起来。

    没有这一条，前三条对"永远拒绝"的实现也全绿 —— 那种假绿就是这个形状。
    """
    for runner, label in ((_run_sync, "sync"), (_run_stream, "stream")):
        content, messages, calls = runner(_ONE_CALL, sess=f"probe:gate:pos:{label}",
                                          discover=True)
        assert len(calls) == 1, (
            f"{label}: 已发现的工具没跑起来（calls={calls}）—— "
            f"门禁把路堵死了，不是只堵了没呈现的那些")
        assert calls[0] == {"a": 1}, f"{label}: 参数没原样传进去: {calls[0]}"
    print("  [ok] 正对照：发现之后两条路都真的执行了")


def test_core_tool_still_runs_without_discovery():
    """常驻工具和"发现"无关 —— 它本来就每一轮都在 wire 上。"""
    ran = []
    extra = ("list_workspace_files", lambda **kw: (ran.append(kw), "[]")[1])
    script = [{"tool_calls": [_tool_call("list_workspace_files", {}, "c1")]},
              {"content": "done"}]
    _run_sync(script, sess="probe:gate:core", discover=False, extra=extra)
    assert ran, "常驻工具被拦住了 —— 门禁的判据不是『在本轮 wire 上』吗"
    print("  [ok] 常驻工具不经发现也能跑")


def test_unknown_tool_keeps_its_own_message():
    """不存在与没发给你是两句话 —— 混了就会给一条走不通的路线。

    `search_tools` 按名字搜不到一个不存在的工具，对不存在的那种说
    「去搜一下这个名字」是撒谎，模型搜完一无所获，然后开始瞎猜名字。

    这句话从 `Error: unknown tool 'x'` 换成了带信息的版本。
    那条禁令没变（`search_tools(` 仍然不许出现 —— 它禁的是
    「按名字承诺」这个形状），加的是三样：

      ① 说清「这个名字压根不存在、重试没用」 —— 否则模型的默认反应是
         换个大小写、或换个相近的名字再来一次；
      ② 给出名字最接近的已注册工具（纯查表）；
      ③ 仍然不承诺 `search_tools('<name>')`。

    ② 与 ③ 的分工是承重的：说得出就说得具体（相近的名字是事实），
    说不出的绝不兜底成一条走不通的路。
    """
    _content, messages, calls = _run_sync(
        [{"tool_calls": [_tool_call("probe_hiden", {}, "c1")]},
         {"content": "done"}], sess="probe:gate:unknown", discover=False)
    msg = _tool_results(messages)[-1]["content"]
    assert calls == [], "不存在的工具居然被执行了"
    assert "no tool named 'probe_hiden'" in msg, f"没走「不存在」那条分支: {msg}"
    assert "does not exist at all" in msg, (
        f"没说清「这名字压根不存在」—— 模型的默认反应会是重试同一个名字: {msg}")
    assert "search_tools(" not in msg, (
        f"对**不存在**的工具给了 `search_tools('<name>')` 那个形状的路线: {msg}")
    # ② 模糊匹配那一半：建议的名字必须真的在注册表里（不是编出来的）
    assert "'probe_hidden'" in msg, (
        f"错记了一个字母（probe_hiden），却没建议真名字 probe_hidden: {msg}")
    import bus.registry as R
    assert "probe_hidden" in R.list_tools(), "夹具不成立：建议的名字不在注册表里"
    print("  [ok] 不存在 / 没发给你 分成两句话（且建议的名字是真名字）")


def test_unknown_tool_does_not_invent_close_names():
    """负对照：凑不出相近的名字时，一个都不许编。

    `difflib` 的 `cutoff` 就是为这个设的。把它关掉（`cutoff=0.0`）之后
    `zzz_qqq_not_a_tool` 也会「匹配上」三个毫不相干的工具，而模型会
    顺着一条假线索再烧一轮 —— 那比不说话更坏。
    """
    from core.agent_loop import _close_tool_names

    _setup("probe:gate:close", discover=False)
    assert _close_tool_names("zzz_qqq_not_a_tool") == [], (
        "给一个八竿子打不着的名字也凑出了「最接近的」—— cutoff 失效了")
    assert _close_tool_names("probe_hiden"), (
        "反向：真错记了一个字母的名字反而一个建议都没有 —— 上面那条会空跑")
    print("  [ok] 凑不出相近的名字时不编（cutoff 真的在起作用）")


# ------------------------------------------------------------------
# 6. 锁步 + 7. 不许拦到框架自己
# ------------------------------------------------------------------

def test_stream_refusal_is_byte_identical_to_sync():
    """两条路的拒绝逐字相同 —— 同一个判定，同一个答案。

    与 `test_stream_brakes.py` 的两条 AST 钩子断言是两个角度：那里钉
    "有没有调用"，这里钉"给出的答案一不一样"（调了同一个函数、却在校验顺序上
    分叉，是可能的）。
    """
    _c1, sync_msgs, _ = _run_sync(_ONE_CALL, sess="probe:gate:ident:sync",
                                  discover=False)
    _c2, stream_msgs, _ = _run_stream(_ONE_CALL, sess="probe:gate:ident:stream",
                                      discover=False)
    a = _tool_results(sync_msgs)[-1]["content"]
    b = _tool_results(stream_msgs)[-1]["content"]
    # 这条必须先证明自己拿到的是拒绝：负对照实测（把门禁改成恒真）
    # 两条路会双双执行成功、双双回 "ok" —— 相等，于是这条照样绿。
    # 「两边一样」只有在两边都真的拒绝了的时候才说明问题。
    assert "not available in this session" in a, (
        f"夹具不成立：拿到的不是拒绝，而是 {a!r} —— 相等这件事就毫无意义了")
    assert a == b, f"两条路的拒绝不一样：\nsync  : {a}\nstream: {b}"
    print("  [ok] 两条路的拒绝逐字相同")


def test_parser_lookup_is_not_gated():
    """`_auto_parse` 拿 handler 不许被门禁拦住。

    那里是框架自己在做事：模型给的是 `file_path`，工具名是框架按扩展名
    选的。拦住它 = 摘要/检索整条路失效，而症状是"摘要功能坏了"，
    与这个门禁毫无表面关联 —— 正是那种最贵的连环伤。

    行为断言（不是 AST）：注册一个没有呈现的 `parse_pdf`，直接问 `_parser` 要。
    """
    import bus.registry as R
    from core.agent_loop import _parser

    R.reset()
    R.forget_discovered("probe:gate:parser")
    sentinel = lambda **kw: "parsed"  # noqa: E731
    R.register_tool("parse_pdf", sentinel,
                    {"description": "probe", "inputSchema": {"type": "object"}})
    assert not R.is_tool_active("probe:gate:parser", "parse_pdf"), \
        "夹具不成立：parse_pdf 居然是常驻/已发现"
    got = _parser("parse_pdf")
    assert got is sentinel, (
        "`_parser` 被门禁拦住了 —— `_auto_parse` 是框架自己在选工具名，"
        "模型从头到尾没提过它")
    print("  [ok] _parser 不经门禁（框架自己做事的那条路）")


def test_refused_tool_is_not_audited_as_called():
    """审计日志不许把拒绝记成 "called"。

    只看名字的实现（`perm in ("write","admin")` 就打印
    `[audit] ADMIN tool called: ...`）会让被拒绝的调用照样留下一条
    "这个工具被调用过"的记录 —— 而拒绝掉的那条恰恰是最需要记清楚的。
    实测：`no_such_tool_at_all({})` 也"被调用"了。

    两个方向都断言：拒绝的不许有（否则是假记录）、真跑过必须有
    （否则修法可能是"把审计整条删掉"）。
    """
    import contextlib
    import io

    def _capture(discover):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _run_sync(_ONE_CALL, sess=f"probe:gate:audit:{discover}",
                      discover=discover)
        return buf.getvalue()

    refused = _capture(False)
    assert "[audit]" not in refused, (
        "被拒绝的调用留下了一条 `[audit] ... called` —— 那是**假记录**，"
        f"而且正好记反了：\n{refused}")

    ran = _capture(True)
    assert "[audit] ADMIN tool called: probe_hidden" in ran, (
        f"真派发过的 admin 工具没有审计行了（修法不能是把审计删掉）:\n{ran}")
    print("  [ok] 审计只记真派发过的（拒绝的不留假记录）")


def test_both_dispatch_sites_call_the_gate():
    """两个派发口都必须有门禁 —— 只补一个就是"开关一开围栏就没了"。

    （`test_stream_brakes.py` 的钩子清单里也加了这两个名字；这里再钉一次，
    是因为这条不变量自己得有个读者，不能只借别人的。）
    """
    tree = ast.parse((_PROJECT_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8"))
    bodies = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name in ("agent_loop", "agent_loop_stream"):
            bodies[node.name] = {
                getattr(sub.func, "id", None) or getattr(sub.func, "attr", "")
                for sub in ast.walk(node) if isinstance(sub, ast.Call)
            }
    assert set(bodies) == {"agent_loop", "agent_loop_stream"}
    for name in ("is_tool_active", "_not_offered_message", "_unknown_tool_message"):
        missing = [f for f, called in bodies.items() if name not in called]
        assert not missing, (
            f"`{name}` 在 {missing} 里没有调用 —— 那条路上的 tools= 又变回建议了")
    print("  [ok] 两个派发口都有门禁（is_tool_active + 两条拒绝话术）")


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
