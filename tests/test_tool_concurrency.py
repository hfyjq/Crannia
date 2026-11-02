"""只读并发、其余串行、判不出来就串行的不变量。

## 判据是"看得见的重叠"

"并发跑了吗"不能用"代码里有 ThreadPoolExecutor"来证 —— 那只能证写了。
本文件的替身 handler 记下自己进出场的时间戳，断言区间是否重叠：
  · 只读的一批：区间必须重叠（真并发）
  · 写类 / 审批类 / 判不出来的：区间必须不重叠（真串行）

反向那一半（不重叠）是承重的：没有它，"把所有工具都并发"也能让前一条全绿。

## 钉住的七件事

  1. 只读的一批真的重叠（`web_fetch` + `list_workspace_files` 那一类）
  2. 写类（`create_document`）不重叠 —— 判据来自 `get_tool_permission`，不是名单
  3. fail-closed：权限查不到（未分类，`admin`）· 参数坏 · `call_id` 重复 ·
     循环会改共享状态（`parse_*` 那类），整批退回串行
  4. 记账按原序：回填的消息顺序 == `tool_calls` 的顺序（并发只许发生在 handler 那一层）
  5. 异常照旧走原来那条路：并发的 handler 抛，`tool.end` 的 `ok=False` + 错误文本
  6. 单条不预取（`len < 2` 直接返回 —— 不白开线程池）
  7. 锁步：两条路各有一处预取（AST + 行为）

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_tool_concurrency.py
"""

import ast
import asyncio
import json
import sys
import threading
import time
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

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

_SESS = "probe:conc"
_SLEEP = 0.25          # 每个替身睡这么久；并发时总耗时应接近 1×，串行时接近 N×


def _call(name, args=None, cid=None):
    return {"id": cid or f"c_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args or {})}}


class _WrapProvider:
    """包一个同步 provider，并在 chat_stream 上转发（用来跑两条路）。"""

    _model = "probe-model"

    def __init__(self, sync_provider):
        self._p = sync_provider

    def chat(self, messages, tools, system=None, thinking=None):
        return self._p.chat(messages, tools, system=system)

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        for i, tc in enumerate(self._p.tool_calls or []):
            yield StreamChunk(tool_call={"id": tc["id"], "type": "function",
                                         "function": tc["function"]},
                              tool_call_index=i)
        yield StreamChunk(finish_reason="tool_calls" if self._p.tool_calls else "stop",
                          usage={"prompt_tokens": 1, "completion_tokens": 1})


class _Script:
    """第 1 轮吐给定的 tool_calls，之后回一句文本收尾。"""

    _model = "probe-model"

    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.calls = 0

    def chat(self, messages, tools, system=None, thinking=None):
        self.calls += 1
        if self.calls == 1:
            return types.SimpleNamespace(content="", tool_calls=self.tool_calls,
                                         total_tokens=1, reasoning_content=None)
        return types.SimpleNamespace(content="done", tool_calls=None,
                                     total_tokens=1, reasoning_content=None)


def _register(name, spans, sink=None):
    """注册一个记时间戳的替身 —— 重叠与否就看 `spans`。"""
    def _handler(**kw):
        t0 = time.monotonic()
        time.sleep(_SLEEP)
        spans.append((name, t0, time.monotonic()))
        if sink is not None:
            sink.append(name)
        return {"ok": name}
    import bus.registry as R
    R.register_tool(name, _handler,
                    {"description": "probe", "inputSchema": {"type": "object"}})


def _setup(names, sess=_SESS):
    """装替身 + 进暴露集，返回记时间戳的 `spans`（一次装好，测试里不许再注册）。"""
    import bus.registry as R
    from core.cancellation import clear

    R.reset()
    R.forget_discovered(sess)
    clear(sess)
    R.register_search_tool()

    spans = []
    for n in names:
        _register(n, spans)
        R.note_discovered(sess, [n])       # 进暴露集（门禁的契约）
    return spans


def _timed_run(names, calls, sess):
    """装 → 跑 → 回 `(spans, 耗时)`。一个夹具，免得每条测试各写一遍装法。"""
    spans = _setup(names, sess=sess)
    t0 = time.monotonic()
    _run_sync(calls, sess=sess)
    return spans, time.monotonic() - t0


def _overlaps(spans):
    """任意两段有交集，真并发过。"""
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            _n1, a0, a1 = spans[i]
            _n2, b0, b1 = spans[j]
            if a0 < b1 and b0 < a1:
                return True
    return False


def _span_union(spans):
    """这一批工具从最早开始到最晚结束的跨度。

    它才是「并发有没有生效」的物理量 —— 与 `elapsed` 不同，
    它不含进程启动/导入/第一次建 registry 那些一次性的钱
    （那些钱发生在 handler 外面，落在 `_timed_run` 的 `t0` 之后、
    第一条 span 之前）。判据：并发，≈ `1 × _SLEEP`；串行，≈ `N × _SLEEP`。
    """
    if not spans:
        return 0.0
    return max(s[2] for s in spans) - min(s[1] for s in spans)


def _run_sync(tool_calls, sess=_SESS):
    from core.agent_loop import agent_loop
    return agent_loop("hi", _Script(tool_calls), session_id=sess)


def _run_stream(tool_calls, sess=_SESS):
    import core.agent_loop as AL

    async def _drain():
        last = None
        async for st in AL.agent_loop_stream("hi", _WrapProvider(_Script(tool_calls)),
                                             session_id=sess):
            last = st
        return last

    return asyncio.run(_drain())


def _tool_msg_ids(messages):
    return [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]


# ------------------------------------------------------------------
# 1/2. 重叠 vs 不重叠
# ------------------------------------------------------------------

def test_readonly_batch_really_overlaps():
    """只读的一批真的并发跑（区间重叠）—— 这一条不能靠"代码里有线程池"代替。

    ## 为什么判 `_span_union`，不判墙上时间

    实测：这条测试单独跑也红（3/3），而 `_overlaps` 那条每次都过 ——
并发根本没坏，坏的是计时的口径。同一个进程、同一批活，连跑 8 次：

    ```
    seq 0: elapsed=1.731 union=0.250     <- 第 1 次含懒加载
    seq 1: elapsed=0.380 union=0.251
    seq 2: elapsed=0.378 union=0.251
    seq 3: elapsed=0.378 union=0.251
    seq 4: elapsed=0.810 union=0.252     <- 从这一条起跳上去，且不再回来
    seq 5: elapsed=1.101 union=0.252
    seq 6: elapsed=1.112 union=0.251
    seq 7: elapsed=1.055 union=0.252
    ```

`elapsed` 里有一大块与"并发有没有生效"无关的钱（这一轮循环自己的
    系统提示装配 / 技能索引 / registry 重建），而 `union` 恒在 0.250–0.252。
    那一跳的来源未定（同一进程内同样输入会有两种稳态）——
所以 `elapsed < 0.75s` 那种判据判的不是并发，它判的是这台机器当时的状态。

判据：判 `_span_union`，不判墙上时间（那不是放宽判据 ——
    `union` 才是这件事的物理量：并发 ≈ 0.25s / 串行 ≈ 0.75s）。
    `elapsed` 仍然打出来给人看，但不当判据。
    """
    names = ["list_workspace_files", "get_document_info", "web_search"]
    calls = [_call(n, cid=f"c{i}") for i, n in enumerate(names)]
    spans, elapsed = _timed_run(names, calls, "probe:conc:fast")
    assert len(spans) == 3, f"替身没全跑到：{spans}"
    assert _overlaps(spans), f"三个只读工具**没有重叠** —— 它们还是串行跑的：{spans}"
    union = _span_union(spans)
    assert union < _SLEEP * 2, (
        f"这批工具的跨度 {union:.2f}s 接近串行（{_SLEEP * 3:.2f}s）—— 并发没生效")
    print(f"  [ok] 三个只读重叠，跨度 {union:.2f}s（串行会是 {_SLEEP * 3:.2f}s）"
          f"；本轮循环总耗时 {elapsed:.2f}s（含与并发无关的开销，**不作判据**）")


def test_write_tools_stay_serial():
    """写类不并发 —— 判据来自 `get_tool_permission`（`create_*`，write）。"""
    names = ["create_document", "add_block"]
    calls = [_call(n, cid=f"c{i}") for i, n in enumerate(names)]
    spans, elapsed = _timed_run(names, calls, "probe:conc:serial")
    assert len(spans) == 2, f"替身没全跑到：{spans}"
    assert not _overlaps(spans), f"写类工具被并发跑了：{spans}"
    # 判跨度，不判墙上时间 —— 理由见上面那条的 docstring。
    union = _span_union(spans)
    assert union >= _SLEEP * 2 * 0.9, (
        f"跨度 {union:.2f}s 不像串行（两件各睡 {_SLEEP}s -> 该 ≈ {_SLEEP * 2:.2f}s）"
        f" —— 写类被并发了？（本轮总耗时 {elapsed:.2f}s，仅参考）")
    print("  [ok] 写类串行（区间不重叠）")


# ------------------------------------------------------------------
# 3. fail-closed 的四条
# ------------------------------------------------------------------

def test_unclassified_permission_falls_back_to_serial():
    """权限查不到，串行（`get_tool_permission` 默认 `admin` —— fail-closed 的源）。"""
    import bus.registry as R
    from core.agent_loop import _prefetch_readonly

    _setup([])
    spans = []
    _register("unknown_thing", spans)          # 不是 read/write 前缀，admin
    R.note_discovered(_SESS, ["unknown_thing"])
    _register("list_workspace_files", spans)
    R.note_discovered(_SESS, ["list_workspace_files"])
    pre = _prefetch_readonly(
        [_call("unknown_thing", cid="a"), _call("list_workspace_files", cid="b")], _SESS)
    assert pre == {}, f"混了未分类的工具居然预取了：{pre}"
    print("  [ok] 未分类的工具 -> 整批串行")


def test_bad_args_and_duplicate_ids_fall_back_to_serial():
    """参数坏 / `call_id` 重复，整批退回串行（拒绝留给各自那条路）。"""
    import bus.registry as R
    from core.agent_loop import _prefetch_readonly

    _setup(["list_workspace_files", "get_document_info"])
    R.note_discovered(_SESS, ["get_document_info"])
    good = _call("list_workspace_files", cid="a")
    bad = {"id": "b", "type": "function",
           "function": {"name": "get_document_info", "arguments": "{bad"}}
    assert _prefetch_readonly([good, bad], _SESS) == {}, "坏参数被预取了（该由 D6 去拒）"
    dup = [_call("list_workspace_files", cid="same"),
           _call("get_document_info", cid="same")]
    assert _prefetch_readonly(dup, _SESS) == {}, "重复 call_id 被预取了"
    single = [_call("list_workspace_files", cid="only")]
    assert _prefetch_readonly(single, _SESS) == {}, "单条也预取了（白开线程池）"
    print("  [ok] 坏参数 / 重复 id / 单条 -> 都不预取")


def test_loop_side_effect_tools_never_prefetch():
    """循环会为它改共享状态的工具不许并发（chunk 进索引 / `_discovered`）。

    `run_subagent` 从这个名单里出去了。
    它原来在这里的理由是「子 agent 的嵌套深度」—— 而那个深度现在是
    `ContextVar`（每个工作线程一份），它不再改共享状态。
    剩下三样（chunk 索引 / `_discovered` / 审批投影）它一样都不碰。
它的"能不能并发"改由第三根轴回答（见下面那条）。
    """
    import bus.registry as R
    from core.agent_loop import _prefetch_readonly, _LOOP_SIDE_EFFECT_TOOLS

    names = ("parse_pdf", "search_chunks", "search_tools")
    _setup([*names, "run_subagent"])
    for n in (*names, "run_subagent"):
        R.note_discovered(_SESS, [n])
    for n in names:
        assert n in _LOOP_SIDE_EFFECT_TOOLS, f"{n} 不在共享状态名单里"
        pre = _prefetch_readonly([_call(n, cid="x"),
                                  _call("search_tools", cid="y")], _SESS)
        assert pre == {}, f"{n} 被预取了 —— 它的写在 handler 外面"
    assert "run_subagent" not in _LOOP_SIDE_EFFECT_TOOLS, (
        "★ `run_subagent` 又回到共享状态名单里了 —— 它的深度已经是 `ContextVar`，"
 "留在名单里就等于把它重新钉成串行（全部意义）")
    print("  [ok] 循环改共享状态的那些工具一律不预取；`run_subagent` 已不在名单里")


def test_run_subagent_is_declared_concurrency_safe():
    """两根轴正交的例子：`run_subagent` 是 `admin` 档，却可以并发。

    这条就是「按 `isConcurrencySafe` 分类」判据落地的地方 ——
    也是"能改东西"与"不能并发"不是同一件事的最小例子。
    """
    import bus.registry as R
    from bus.registry import (_EXPLICIT_CONCURRENCY, get_tool_concurrency_safe,
                              get_tool_permission)

    _setup(["run_subagent", "list_workspace_files"])
    assert get_tool_permission("run_subagent") == "admin", (
        "档位变了？它必须还是 admin —— 能改东西那根轴没动")
    assert _EXPLICIT_CONCURRENCY.get("run_subagent") is True, (
        "★ 显式表里那一行没了 -> 它会落回「派生自权限档」= admin -> 串行")
    assert get_tool_concurrency_safe("run_subagent") is True

    # 负对照：同样是 admin 档、没声明的工具仍然串行
    R.register_tool("probe_admin_undeclared", lambda **kw: "x",
                    {"description": "p", "inputSchema": {"type": "object"}})
    assert get_tool_permission("probe_admin_undeclared") == "admin"
    assert get_tool_concurrency_safe("probe_admin_undeclared") is False, (
        "★ 派生那一支错了 —— 没声明就是串行（fail-closed）")
    print("  [ok] `run_subagent`：admin 档 + 显式声明并发；未声明的 admin 仍串行")


def test_two_subagents_in_one_batch_really_overlap():
    """端到端判据：同一批里的两个 `run_subagent` 真的同时跑。

    形状是一个 barrier 判据：
    两个子各自阻塞，直到两者都已启动，这条栈上任何隐藏的串行化都会让
    barrier 超时，而不是静默通过。

    串行的形状：`run_subagent` 在 `_LOOP_SIDE_EFFECT_TOOLS` 里 + 档是 `admin`
    （两道关），两个子一个跑完再跑下一个，第一个子永远等不到第二个
barrier 超时，回执变成"子 agent 没做成"。

    走的是生产那条路（`agent_loop` → `_prefetch_readonly` →
    `_dispatch_subagent` → `run_child`），不是拿桩去调 `_prefetch_readonly`。
    """
    import threading

    import bus.registry as R
    from core.agent_loop import agent_loop

    R.reset()
    R.register_search_tool()
    R.register_subagent_tool()          # 真注册（预取认的是它出的 schema）
    sid = "probe:conc:sub"
    R.forget_discovered(sid)

    barrier = threading.Barrier(2, timeout=15)

    class _P:
        _model = "probe-model"

        def __init__(self):
            self._lock = threading.Lock()
            self._parent_turns = 0
            self.children = []

        def chat(self, messages, tools, system=None, thinking=None):
            last = (messages[-1].get("content") or "") if messages else ""
            if "【任务】" in last:                       # ← 子
                barrier.wait()                          # 两个子必须同时活着
                with self._lock:
                    self.children.append(last)
                return types.SimpleNamespace(content="子结论", tool_calls=None,
                                             total_tokens=1, reasoning_content=None)
            with self._lock:                            # ← 父
                self._parent_turns += 1
                n = self._parent_turns
            if n == 1:
                return types.SimpleNamespace(content="", tool_calls=[
                    _call("run_subagent", {"task": "A"}, "s1"),
                    _call("run_subagent", {"task": "B"}, "s2")],
                    total_tokens=1, reasoning_content=None)
            return types.SimpleNamespace(content="父收工", tool_calls=None,
                                         total_tokens=1, reasoning_content=None)

    p = _P()
    _content, messages, _chunks, _tokens = agent_loop("父的输入", p, session_id=sid)

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2, f"期望两条工具回执，实得 {len(tool_msgs)}"
    joined = " ".join(str(m.get("content") or "") for m in tool_msgs)
    assert joined.count("子结论") == 2, (
        "★ 两个子**没有同时跑完**（串行时第一个子会卡在 barrier 上超时）——"
        "回执是 %r" % (joined[:220],))
    assert len(p.children) == 2, f"只有 {len(p.children)} 个子真的进了模型调用"
    print("  [ok] 同批两个 `run_subagent` 真重叠（barrier 判据，串行会超时）")


# ------------------------------------------------------------------
# 4/5/6. 记账按原序 + 异常照旧
# ------------------------------------------------------------------

def test_bookkeeping_keeps_the_original_order():
    """并发只发生在 handler 那一层：回填的消息顺序 == `tool_calls` 的顺序。"""
    names = ["list_workspace_files", "get_document_info", "web_search"]
    _setup(names)
    calls = [_call("list_workspace_files", cid="c1"),
             _call("get_document_info", cid="c2"),
             _call("web_search", cid="c3")]
    _content, messages, _c, _t = _run_sync(calls, sess="probe:conc:order")
    assert _tool_msg_ids(messages) == ["c1", "c2", "c3"], (
        f"回填顺序乱了：{_tool_msg_ids(messages)} —— 并发只许在 handler 那一层")
    print("  [ok] 记账按原序")


def test_a_raising_handler_still_reports_through_the_same_path():
    """并发的 handler 抛，走原来那条路（`tool.end` 的 `ok=False` + 错误文本）。"""
    import bus.registry as R

    _setup(["list_workspace_files", "get_document_info"])
    R.note_discovered(_SESS, ["get_document_info"])

    def _boom(**kw):
        raise RuntimeError("kaboom-并发")

    R.register_tool("get_document_info", _boom,
                    {"description": "probe", "inputSchema": {"type": "object"}})

    events = []
    from core.agent_loop import agent_loop
    from core.events import make_emitter

    class _Bus:
        def __init__(self): self.sent = []
        def emit(self, type_, session_key="", **data): self.sent.append((type_, data))

    bus = _Bus()
    agent_loop("hi", _Script([_call("list_workspace_files", cid="c1"),
                              _call("get_document_info", cid="c2")]),
               session_id="probe:conc:boom",
               _emit=make_emitter(bus, "probe:conc:boom"))
    ends = [d for t, d in bus.sent if t == "tool.end"]
    assert len(ends) == 2, f"tool.end 不是两条：{ends}"
    bad = [d for d in ends if d.get("ok") is False]
    assert bad, f"抛异常的那条没有 ok=False：{ends}"
    assert "kaboom-并发" in bad[0].get("error", ""), (
        f"错误文本没传到事件里：{bad[0]}")
    print("  [ok] 并发里抛的异常照旧走原来那条路")


# ------------------------------------------------------------------
# 7. 锁步
# ------------------------------------------------------------------

def test_both_paths_prefetch():
    """两条路各有一处预取（AST）+ 流式那条行为上也要重叠。"""
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
    for name, called in bodies.items():
        assert "_prefetch_readonly" in called, f"`{name}` 没有预取 —— 那条路上没有并发"

    _setup(["list_workspace_files", "get_document_info"])
    calls = [_call("list_workspace_files", cid="c1"), _call("get_document_info", cid="c2")]
    _run_stream(calls, sess="probe:conc:stream")
    print("  [ok] 两条路都预取（AST + 流式真跑一遍）")


# ------------------------------------------------------------------
# 8. 第三根轴 `concurrency_safe`
# ------------------------------------------------------------------
#
# 它接手的是「按 `isConcurrencySafe` 分类」里当时没做的那一半：
# 旧的判法借 `read` 档（`get_tool_permission(name) != "read"，串行`）。
# 判据从"它是不是只读"变成"这一次调用能不能和同批别的同时跑" ——
#   两者正交（`run_subagent` 是 `admin` 而两个子互不相干）。

def test_the_axis_defaults_to_the_permission_tier():
    """纯函数 + 顺序：声明 -> 显式表 -> 由权限档派生。

    派生那一支就是旧判据（`permission == "read"`），换轴当天零行为变化。
    """
    from core import tool_permission as TP

    before = TP.snapshot()
    try:
        TP.restore({})
        assert TP.resolve_concurrency_safe("x", permission="read", explicit={}) is True
        assert TP.resolve_concurrency_safe("x", permission="write", explicit={}) is False
        assert TP.resolve_concurrency_safe("x", permission="admin", explicit={}) is False
        # 显式表压过派生（框架级工具在代码的表里声明）
        assert TP.resolve_concurrency_safe(
            "x", permission="admin", explicit={"x": True}) is True
        # 声明压过显式表（顺序不许重排 —— 重排 = manifest 从此压不过代码表）
        TP.declare_concurrency("x", False)
        assert TP.resolve_concurrency_safe(
            "x", permission="read", explicit={"x": True}) is False
    finally:
        TP.restore(before)
    print("  [ok] 顺序写死：声明 > 显式表 > 派生自权限档")


def test_only_real_booleans_are_accepted():
    """`concurrency_safe: "true"` / `1` / `yes` 都不算声明（同 `destructive`）。

    非法值的去处是"派生"、不是某个固定值 —— 而派生值就是旧判据
拼错一个词不会静默放开任何东西（与 `destructive` 的 fail-open 相反，
    那一根拼错会让不可逆的工具静默变回"可逆"）。
    """
    from core import tool_permission as TP

    before = TP.snapshot()
    try:
        TP.restore({})
        for bad in ("true", "false", 1, 0, "yes", None, [], {}):
            assert TP.declare_concurrency("x", bad) is False, f"{bad!r} 被收下了"
            assert TP.declared_concurrency("x") is None, f"{bad!r} 留下了痕迹"
        assert TP.declare_concurrency("x", True) is True
        assert TP.declared_concurrency("x") is True      # 与 False 分得开
    finally:
        TP.restore(before)
    print("  [ok] 只收真 bool；非法值 = 未声明（落回派生，不是 fail-open）")


def test_the_axis_is_a_no_op_across_the_whole_registry():
    """零行为变化的全量证明：除显式表里那几行，每个已注册工具的
    `concurrency_safe` 都等于 `permission == "read"`。

    这就是"换轴当天逐字等价"的判据 —— 而 `_EXPLICIT_CONCURRENCY` 里
    每多一行都是一次有意的放开，必须从这里被看见（它是那张表唯一的读者）。
    它不是恒真：谁把派生那一支改错（比如写成 `permission != "admin"`），
    这里当场红。
    """
    import bus.registry as R
    from bus.registry import (_EXPLICIT_CONCURRENCY, get_tool_concurrency_safe,
                              get_tool_permission)
    from scripts.measure_request import boot_registry

    R.reset()
    boot_registry()
    names = sorted(R._registry["tools"])
    assert len(names) > 100, f"注册表只有 {len(names)} 个工具 —— 装载没跑起来？"

    wrong = []
    for n in names:
        expect = _EXPLICIT_CONCURRENCY.get(n, get_tool_permission(n) == "read")
        if get_tool_concurrency_safe(n) is not expect:
            wrong.append((n, get_tool_permission(n), get_tool_concurrency_safe(n)))
    assert not wrong, (
        "这些工具的 concurrency_safe 与「派生自权限档」不一致（前 5 个）：%r"
        % (wrong[:5],))
    # 显式表里的每一行都必须真的注册着 —— 否则那是死条目
    unknown = sorted(set(_EXPLICIT_CONCURRENCY) - set(names))
    assert not unknown, f"显式表里的这些名字没注册：{unknown}"
    print(f"  [ok] 全量 {len(names)} 个工具：除显式表 "
          f"{len(_EXPLICIT_CONCURRENCY)} 行外，一律等于「档 == read」")


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
