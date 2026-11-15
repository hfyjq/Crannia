"""
上下文状态事件源。

为什么立这个文件
================
缺的不是度量，是出口：

  · 占用 —— 有（`MemoryManager.get_context_usage` 一直在算，
    每轮 `PostProcessStage` 都调一次）
  · 但「阈值是 token 还是百分比」「窗口那个数是谁声明的」
    「离触发还有多远」—— 只有读过代码的人知道

更缺的是跨通道：这套读数只被 `cli_channel` 打印过一行
（`_display_context_usage`）。WebUI / 飞书 / 微信 / 钉钉 —— 一个字都没有。
而事件流（`core/events.py`）本来就是"channel-agnostic"的，所以出口接在那里。

三条判据（本文件逐条钉住）
------------------------------
① 只有一个载荷构造点、只有一个发送点。理由不是整洁，是覆盖：
   `/compact` 那条路在 `pre_process` 就 `stop`，`post_process` 根本不跑 ——
   而它恰恰是最该报一次上下文的那一轮。发送点放进阶段里 = 要么漏掉它、
   要么抄第二份。
② 粘住的旧值不许伪装成新事实。 `compressor.last_compression` 一直留到
   下一次压缩为止。每轮都塞进载荷，读日志的人会数出"压了 N 次"，
   而真实可能只有 1 次。判据是 `ctx.metadata["compressed"]`（本轮压没压）。
   读不到就是读不到。
③ `armed` 只有一处判定（`Compressor.is_armed`）—— 展示层不许再算一遍。
   抄第二份的代价不是多写一行，是改了预警比例只动一处、两个答案看着都对。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_context_state.py
"""

import ast
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ASCII-only output -- the Windows console defaults to GBK and emoji would
# crash the whole run (and the failure reporter with it).

_SCHED = _PROJECT_ROOT / "core" / "pipeline" / "scheduler.py"
_CHAT = _PROJECT_ROOT / "web" / "templates" / "chat.html"
_COMPACT_OWNER = _PROJECT_ROOT / "core" / "pipeline" / "stages" / "pre_process.py"


# ------------------------------------------------------------------
# 1. 事件类型注册
# ------------------------------------------------------------------

def test_the_event_type_is_registered():
    """`__all__` + `DURABLE_TYPES`。

    负对照按值比，不按名字 —— `DURABLE_TYPES` 装的是
    `"context.state"` 这种值。拿变量名去比会恒假，于是"注册没注册"
    这条断言永远红或者永远绿，取决于怎么写。
    """
    import core.events as E

    assert "EVT_CONTEXT_STATE" in E.__all__, "不在 `__all__` -> 别的模块 import 不到"
    assert E.EVT_CONTEXT_STATE in E.DURABLE_TYPES, (
        "不在 `DURABLE_TYPES` -> **不落盘** -> 「压缩多久发生一次」这个数"
        "永远数不出来（那是阈值至今没有实测依据的直接原因之一）")
    # 负对照：名字不在值集里 —— 它证明上面那条比的是值
    assert "EVT_CONTEXT_STATE" not in E.DURABLE_TYPES, (
        "拿名字比竟然命中了？那条断言的判据要重写")
    print("  [ok] `context.state` 已注册（__all__ + DURABLE_TYPES，按值比）")


# ------------------------------------------------------------------
# 2. 载荷：四个问题都要答得上
# ------------------------------------------------------------------

class _FakePool:
    """只做两件事：给出 usage、给出 last_compression。"""

    def __init__(self, usage, last_compression=None):
        self._usage = usage
        self.last_compression = last_compression
        self.calls = 0

    def get_context_usage(self, session_key, messages):
        self.calls += 1
        return dict(self._usage)


class _Ctx:
    def __init__(self, pool, metadata=None, messages=None):
        self.session_pool = pool
        self.metadata = metadata or {}
        self.messages = messages if messages is not None else []


_USAGE = {
    "estimated_tokens": 42000,
    "message_tokens": 30000,
    "system_tokens": 2000,
    "tool_tokens": 10000,
    "factor": 1.02,
    "context_window": 131072,
    "percentage": 32.0,
    "threshold_pct": 85.0,
    "threshold_tokens": 111411,
    "armed": False,
    "window_source": "声明(config.yaml)",
}


def test_the_payload_answers_all_four_questions():
    """占用 / 阈值 / 来源 / 是否 armed —— 四条判据。"""
    from core.pipeline.scheduler import _context_state_payload
    ctx = _Ctx(_FakePool(_USAGE), messages=[{}] * 12)
    p = _context_state_payload(ctx, "s1")

    assert p["used"] == 42000 and p["window"] == 131072, p
    assert p["threshold_pct"] == 85.0 and p["threshold_tokens"] == 111411, p
    assert p["window_source"] == "声明(config.yaml)", p
    assert p["armed"] is False, p
    assert p["messages"] == 12, p
    print("  [ok] 载荷含 占用/阈值/来源/armed（+条数与因子）")


def test_metadata_usage_is_reused_so_normal_turns_pay_nothing_extra():
    """普通轮不额外付一次分词 —— 复用 post_process 刚算的那一份。

    这不是优化，是同源：现算一次会多一个"同一件事的两个值"的机会。
    """
    from core.pipeline.scheduler import _context_state_payload
    pool = _FakePool(_USAGE)
    ctx = _Ctx(pool, metadata={"context_usage": dict(_USAGE)})
    p = _context_state_payload(ctx, "s1")
    assert pool.calls == 0, "手上有现成的还去重算了一次（多一个值就有多一个分叉）"
    assert p["used"] == 42000

    pool2 = _FakePool(_USAGE)
    ctx2 = _Ctx(pool2)
    _context_state_payload(ctx2, "s1")
    assert pool2.calls == 1, "命令轮（post_process 没跑）必须自己算一次"
    print("  [ok] 有就复用、没有才现算（且只现算一次）")


# ------------------------------------------------------------------
# 3. 粘住的旧值不许伪装成新事实
# ------------------------------------------------------------------

def test_compression_stats_only_on_the_turn_that_compressed():
    """本文件最重要的一条。

    负对照的构造方式：让 `last_compression` 一直挂着（模拟真实情形 ——
    它确实一直挂着），然后问"没压的那一轮，载荷里有没有它"。
    如果实现写成 `if pool.last_compression:`，这条就会红 ——
    而那条路上的日志会显示"每一轮都压过"。
    """
    from core.pipeline.scheduler import _context_state_payload
    sticky = {"pre_tokens": 118000, "post_tokens": 20000, "mode": "auto",
              "source": "llm", "compressed_turns": 4,
              "old_message_count": 60, "new_message_count": 5}
    pool = _FakePool(_USAGE, last_compression=sticky)

    # (a) 本轮没压 —— 哪怕 last_compression 挂着，也不许出现
    quiet = _context_state_payload(_Ctx(pool), "s1")
    assert "compressed" not in quiet, (
        "没压的那一轮带上了压缩读数 —— `last_compression` 是**粘的**，"
        "读日志的人数出的压缩次数会变成**轮数**。实得 %r" % (quiet.get("compressed"),))

    # (b) 本轮压了 —— 这时才带，且带的是这一次的进出
    loud = _context_state_payload(_Ctx(pool, metadata={"compressed": True}), "s1")
    c = loud["compressed"]
    assert (c["pre_tokens"], c["post_tokens"], c["saved"]) == (118000, 20000, 98000), c
    assert (c["messages_before"], c["messages_after"]) == (60, 5), c
    print("  [ok] 压缩读数只在**本轮真的压过**时才带")


def test_saved_is_None_when_pre_post_are_missing():
    """读不到就是 `None`，不是 `0`。

    `_log_result` 没跑过时 `pre_tokens`/`post_tokens` 就不在 stats 里 ——
    写成 `0` 会看着像一个结论（"压完省了 0"）。
    """
    from core.pipeline.scheduler import _context_state_payload
    pool = _FakePool(_USAGE, last_compression={"mode": "auto"})
    p = _context_state_payload(_Ctx(pool, metadata={"compressed": True}), "s1")
    assert p["compressed"]["saved"] is None, p["compressed"]
    assert p["compressed"]["pre_tokens"] is None, p["compressed"]
    print("  [ok] 缺 pre/post 时 `saved` 是 None 而不是 0")


def test_a_pool_less_context_yields_no_event():
    """没有 pool（纯单测里构造的 ctx），不发，而不是发一条空读数。"""
    from core.pipeline.scheduler import _context_state_payload
    assert _context_state_payload(_Ctx(None), "s1") is None
    print("  [ok] 没有 pool 时返回 None（不发半截事件）")


# ------------------------------------------------------------------
# 4. `armed` 只有一处判定
# ------------------------------------------------------------------

def _make_mgr(tmp_dir, provider):
    """一台只落在 *tmp_dir* 里的 MemoryManager（不碰生产目录）。"""
    import core.memory.manager as manager_mod


    cfg = {"memory": {
        "storage_path": str(Path(tmp_dir) / "memory"),
        "short_term": {"max_history": 100},
        "compression": {"token_threshold": 0.85, "keep_recent_turns": 3},
        "session_memory": {"sessions_dir": str(Path(tmp_dir) / "sessions")},
    }}
    mgr = manager_mod.MemoryManager(cfg, provider=None)
    mgr._provider = provider
    return mgr


def _fake_provider(window=131072, window_source="声明(config.yaml)"):
    from providers.base import BaseProvider

    class _P(BaseProvider):
        """1 字符 = 1 token，但继承真的 token 算术（抄公式的测试
        只证明抄的那份和自己一致）。"""

        def __init__(self):
            self._window = window
            self.window_source = window_source

        @property
        def context_window(self):
            return self._window

        def count_tokens(self, text):
            return len(text)

        def chat(self, messages, tools=None, system=None, thinking=None):
            raise NotImplementedError

        async def chat_stream(self, messages, tools=None, system=None,
                              thinking=None):
            raise NotImplementedError

    return _P()


def test_is_armed_boundary_is_the_threshold_times_the_ratio():
    """0.75 × 阈值 —— 边界两侧各钉一个数。"""
    from core.memory.compressor import Compressor
    c = Compressor(threshold=0.85)
    # 阈值 = int(1000 * 0.85) = 850；0.75 × 850 = 637.5
    assert c.is_armed(638, 1000) is True, "638/850 = 0.7506 该算已进预警区"
    assert c.is_armed(637, 1000) is False, "637/850 = 0.7494 不该算"
    assert c.is_armed(0, 0) is False, "窗口为 0 不许除零"
    print("  [ok] `is_armed` 的边界就是 阈值 × 0.75")


def test_get_context_usage_takes_armed_from_the_compressor():
    """负对照：把 `Compressor.is_armed` 换掉，`get_context_usage` 必须跟着变。

    这条红的唯一方式，是展示层自己又算了一遍那个比例
    （例如内联 `total / int(window * ratio) >= 0.75`）——
    而那种代码两个答案看着都对，改比例只动一处。
    """
    tmp = tempfile.mkdtemp(prefix="lv_ctx_")
    try:
        p = _fake_provider(window=131072)
        mgr = _make_mgr(tmp, p)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):          # `_non_history_cost` 的降级噪声
            u = mgr.get_context_usage([{"role": "user", "content": "hi"}])

        assert u["context_window"] == 131072
        assert u["threshold_tokens"] == int(131072 * mgr.compressor._threshold)
        assert u["threshold_pct"] == round(mgr.compressor._threshold * 100, 1)

        real = mgr.compressor.is_armed
        try:
            mgr.compressor.is_armed = lambda t, w: True
            with contextlib.redirect_stdout(io.StringIO()):
                u2 = mgr.get_context_usage([{"role": "user", "content": "hi"}])
        finally:
            mgr.compressor.is_armed = real

        assert u2["armed"] is True, (
            "把 `Compressor.is_armed` 改成恒真之后 `armed` 还是 False —— "
            "说明 `get_context_usage` **自己又算了一遍**那比例（"
            "一个判定同时管多处时不许抄第二份）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  [ok] `armed` 跟着 `Compressor.is_armed` 走（负对照：换成恒真它必须变）")


def test_window_source_is_carried_through():
    """来源随值走。只给值不给来源，读的人无从判断它是不是保守兜底。"""
    tmp = tempfile.mkdtemp(prefix="lv_ctx_")
    try:
        mgr = _make_mgr(tmp, _fake_provider(window_source="保守兜底(未声明)"))
        with contextlib.redirect_stdout(io.StringIO()):
            u = mgr.get_context_usage([])
        assert u["window_source"] == "保守兜底(未声明)", u
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  [ok] `window_source` 从 provider 带到了读数里")


def test_log_headroom_is_gated_by_that_same_judgement():
    """抽取之后行为不变：那条日志出不出现，由 `is_armed` 一处说了算。

    这是「把内联式子换成方法调用」这件事唯一需要防的方向 ——
    换错了不会报错，只会让那行日志从此不出声（没有读者的守卫会腐烂）。
    """
    from core.memory.compressor import Compressor
    c = Compressor(threshold=0.85)
    bd = {"total": 800, "messages": 500, "system": 100, "tools": 100,
          "raw": 300, "factor": 2.0}
    msgs = [{"role": "user", "content": "x"}]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        c._log_headroom(bd, 1000, msgs, "auto")
    assert "[compress]" in buf.getvalue(), (
        "800/850 = 0.94，早过 0.75 了却没出声 —— 闸门接错了")

    c.is_armed = lambda t, w: False
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        c._log_headroom(bd, 1000, msgs, "auto")
    assert buf.getvalue() == "", (
        "闸门说「没进预警区」却照样打了那一行 —— 判定的接线没生效")
    print("  [ok] `_log_headroom` 的闸门就是 `is_armed`（两向都验过）")


# ------------------------------------------------------------------
# 5. 接线：一个载荷构造点 + 一个发送点
# ------------------------------------------------------------------

def test_there_is_exactly_one_emit_site_and_it_is_in_execute():
    """「每轮恰好一条」必须是结构性的，不是靠「记得每处都发」。"""
    tree = ast.parse(_SCHED.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_context_state_payload"]
    assert len(calls) == 1, (
        "`_context_state_payload` 该只有 **1 个**调用点（轮末一处覆盖全部路径），"
        "实得 %d 处 —— 第二处意味着「某条路走不到」又要靠人记着补" % len(calls))

    def _innermost_fn(node):
        """*node* 最内层的宿主函数。"""
        best = None
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                if best is None or fn.lineno > best.lineno:
                    best = fn
        return best

    # 判据是语义的，不是函数名：这个发送点必须和 `EVT_TASK_END`
    #   走同一条路。`task.end` 是本仓库"每一轮都必经"的那个事件
    #   （发在 `finally` 里，命令轮 / 报错轮都跑得到）—— 拿它当基准，
    #   比钉死一个函数名稳：名字会改，而"和 `task.end` 同路"是那条性质本身。
    #
    #   真名是 `_execute_serial`（`execute` 只是加锁的外壳）。
    #      钉在名字上的锚会漂，所以判据取的是"与 `task.end` 同路"。
    task_end_sites = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "emit" and n.args
        and isinstance(n.args[0], ast.Name) and n.args[0].id == "EVT_TASK_END"
    ]
    assert task_end_sites, "找不到 `EVT_TASK_END` 的发射点 —— 基准没了"
    end_fn = _innermost_fn(task_end_sites[0])
    emit_fn = _innermost_fn(calls[0])
    assert emit_fn is not None and end_fn is not None, "找不到宿主函数"
    assert emit_fn.name == end_fn.name, (
        "上下文事件与 `task.end` **不在同一条路上**（%r vs %r）—— "
        "那就意味着有某种轮次走不到它，而要的正是"
        "「命令轮 / 报错轮也报一次」" % (emit_fn.name, end_fn.name))

    assert "EVT_CONTEXT_STATE" in _SCHED.read_text(encoding="utf-8"), \
        "调度器没有 import 那个事件常量"
    print("  [ok] 载荷构造点恰好 1 个调用点，与 `task.end` 同路")


def test_the_emit_failure_is_loud_but_never_kills_the_turn():
    """兜底不许静默（判据 4），但也不许弄挂这一轮 —— 模型那边已经跑完了。"""
    src = _SCHED.read_text(encoding="utf-8")
    i = src.index("_context_state_payload(ctx, session_key)")
    seg = src[max(0, i - 1400):i + 900]
    assert "except Exception" in seg, "没有兜底：一条读数能弄挂整个回合"
    assert "print(" in seg, "兜底静默了 —— 那是判据 4 明令禁止的形状"
    print("  [ok] 构造失败会出声，且不打断这一轮")


def test_both_compression_producers_raise_their_hand():
    """两个生产者、一个判据键。

    这条同时防两个方向：漏标（压了却不说，事件里没有压缩读数）和
    多标（没压却说，回到「粘住的旧值」）。生产者的数量也被钉住 ——
    多一个就得来解释它凭什么算"本轮压过"。
    """
    post = (_PROJECT_ROOT / "core/pipeline/stages/post_process.py").read_text("utf-8")
    pre = _COMPACT_OWNER.read_text(encoding="utf-8")
    assert 'ctx.metadata["compressed"] = True' in post, \
        "post_process 的自动压缩没有举手"
    assert 'ctx.metadata["compressed"] = True' in pre, \
        ("`/compact` 那条路没有举手 —— 而它是**唯一**不经过 post_process 的"
         "压缩路径（`stop` 在 pre_process），不标就永远不会出现在事件里")

    #: 第三个生产者 —— `AgentLoopStage` 的"超长自救"
    #:   （收到 `context_overflow`，压一次再重发）。它凭什么算"本轮压过"：
    #:   它真的压了（`pool.compact` 返回 `done=True`），而且压的是**这一轮
    #:   正在跑的那份 messages** —— 与另外两个是同一件事，只是触发点不同。
    #:   少了这一条，超长自救压完之后 `context.state` 里没有 pre/post，
    #:   而那一轮恰恰是最需要读数的一轮。
    auto = (_PROJECT_ROOT / "core/pipeline/stages/agent_loop.py").read_text("utf-8")
    assert 'ctx.metadata["compressed"] = True' in auto, \
 "超长自救那条路没有举手"

    found = set()
    for path in (_PROJECT_ROOT / "core").rglob("*.py"):
        if 'metadata["compressed"] = ' in path.read_text("utf-8"):
            found.add(path.name)
    assert found == {"post_process.py", "pre_process.py", "agent_loop.py"}, (
        "`ctx.metadata['compressed']` 的生产者变了：%r —— 多一个要解释、"
        "少一个要报警" % sorted(found))
    print("  [ok] 三个压缩生产者都举手（自动 / `/compact` / 超长自救），"
          "且生产者恰好是三个")


# ------------------------------------------------------------------
# 6. 前端：读数必须与 `#status-metrics` 平级
# ------------------------------------------------------------------

def test_the_readout_is_a_sibling_of_the_metrics_span():
    """这是一个只在页面上错、不报任何错的坑。

    `refreshStatus()` 每 5 秒执行 `met.textContent = ...`，而真 DOM 赋
    `textContent` 会清空子节点。读数一旦嵌进那一格，就会被每 5 秒
    静默擦掉一次 —— 症状只是"这功能好像不出现"。
    """
    html = _CHAT.read_text(encoding="utf-8")
    blocks = re.findall(r'<div class="status-right">(.*?)</div>', html, re.S)
    assert len(blocks) == 1, "`.status-right` 块找到 %d 个，判据不成立" % len(blocks)
    block = blocks[0]

    assert 'id="status-ctx"' in block, "读数不在状态条里"
    assert 'id="status-metrics"' in block, "指标格不在状态条里"

    i_ctx = block.index('id="status-ctx"')
    i_ctx_close = block.index("</span>", i_ctx)
    i_met = block.index('id="status-metrics"')
    assert i_ctx_close < i_met, (
        "读数被**嵌进**了 `#status-metrics` 里（它的 `</span>` 在 metrics "
        "开始之后）—— `refreshStatus()` 每 5 秒把那一格整块重写，"
        "读数会被静默擦掉，而界面上不会有任何报错")

    # 负对照：上面那条判据必须真的能分辨「平级」与「嵌套」
    nested = ('<div class="status-right">'
              '<span id="status-metrics"><span id="status-ctx"></span></span>'
              '</div>')
    b2 = re.findall(r'<div class="status-right">(.*?)</div>', nested, re.S)[0]
    j_ctx, j_met = b2.index('id="status-ctx"'), b2.index('id="status-metrics"')
    assert b2.index("</span>", j_ctx) > j_met, \
        "负对照失效：嵌套写法本该被上面那条判据判红"
    print("  [ok] 读数与 `#status-metrics` 平级（附嵌套写法的负对照）")


def _strip_js_line_comments(seg: str) -> str:
    """剥掉 JS 的行注释 —— 检查代码，不检查文档。

    不剥会自己打自己：函数体的注释里写着「不 `openActivity`」，
    而断言按字面量找 `openActivity(`，命中的正是那句注释。
    读文档的检查器会自己打自己。
    """
    out = []
    for line in seg.split(chr(10)):
        k = line.find("//")
        out.append(line if k < 0 else line[:k])
    return chr(10).join(out)


def test_the_fold_writes_only_into_that_box():
    """折叠函数只碰状态条那一格 —— 它不许往对话流里插东西。

    判据不是风格：`context.state` 每轮一条，进对话流就是每轮一行噪声。
    """
    html = _CHAT.read_text(encoding="utf-8")
    i = html.index("function foldContextState")
    # 切到下一个顶层 `function` 为止 —— 固定字符窗口会越界进
    # `foldEvent`（它本来就有 `openActivity(`），于是断言命中的是别的函数。
    # 两处独立错误叠在一起，报出来只像一处。
    j = html.index("function ", i + 10)
    code = _strip_js_line_comments(html[i:j])
    for forbidden in ("openActivity(", "appendMsg(", "pushDelta("):
        assert forbidden not in code, (
            "`foldContextState` 里出现了 %r —— 上下文读数是**环境读数**，"
            "不是「一件做过的事」" % forbidden)
    print("  [ok] 折叠只写状态条，不碰对话流")


def main():
    tests = [
        (name, obj) for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            failed += 1
            print("  FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d tests: %d passed, %d failed" % (len(tests), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
