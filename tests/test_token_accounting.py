"""
Regression tests for the memory layer's token account.

The cost of everything that is NOT history was guessed from two constants
that were wrong in OPPOSITE directions:

  * tool_count = len(list_tools()) -- every registered tool (~100),
    while the wire only ever carries _CORE_TOOLS | _discovered (22 and
    up): a ~4.5x over-count;
  * tool_tokens = tool_count * 60 -- while `bus/registry.py`'s own
    comment in the same repo uses 150/tool: a 2.5x under-count.

The net error was not computable by reading the code, so it could not be
fixed by adjusting a constant. The cost is now MEASURED from the objects
that are actually sent.

Also covered here (same function, same account):
  * budget_chars = history_budget_tokens * 4 used cl100k's English
    ratio while the repo's own measurement says Chinese is ~1.9 chars per
    token. The account now reuses `core.summarizer`'s constant instead of
    holding a second, contradicting one.
  * the load loop charged `len(content)` but injected `content[:2000]`
    -- charged more than it sent, so history loaded short.

**The measurement must not end in a cached token count.** Such a count
is a second instrument: the gate would add it to a factored message
estimate, and the calibrator's ground truth would contain a part nobody
measured. Counting happens only inside
`BaseProvider.estimate_request_tokens`, so what the manager hands over is
the objects — hence the tests below assert on the parts that travel,
not on numbers computed here.

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_token_accounting.py
"""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from providers.base import BaseProvider

# ASCII-only output -- the Windows console defaults to GBK and emoji would
# crash the whole run (and the failure reporter with it).

_CHARS_PER_TOKEN = 1.5  # mirrors core/summarizer.CONSERVATIVE_CHARS_PER_TOKEN


class FakeProvider(BaseProvider):
    """Deterministic provider: 1 token per character.

    Inherits the real token arithmetic.  Reimplementing
    `estimate_request_tokens` here would let consumers drift apart --
    a test that copies the formula only proves the copy agrees with itself.
    """

    def __init__(self, context_window: int = 131072):
        self._window = context_window
        self.count_calls = 0

    @property
    def context_window(self) -> int:
        return self._window

    def count_tokens(self, text: str) -> int:
        self.count_calls += 1
        return len(text)

    async def chat_stream(self, messages, tools=None, system=None, thinking=None):
        raise NotImplementedError("streaming is not exercised here")

    def chat(self, messages, tools=None, system=None, thinking=None):
        raise NotImplementedError("no LLM in this test")


def _make_mgr(tmp_dir, provider=None):
    """Build a MemoryManager rooted entirely in *tmp_dir*."""
    import core.memory.manager as manager_mod


    cfg = {
        "memory": {
            "storage_path": str(Path(tmp_dir) / "memory"),
            "short_term": {"max_history": 100},
            "compression": {"token_threshold": 0.85, "keep_recent_turns": 3},
            "long_term": {"chroma_path": str(Path(tmp_dir) / "chroma")},
            "semantic": {"db_path": str(Path(tmp_dir) / "profile.db")},
            "session_memory": {"sessions_dir": str(Path(tmp_dir) / "sessions")},
        }
    }
    mgr = manager_mod.MemoryManager(cfg, provider=None)
    mgr._provider = provider
    return mgr


def _patch_registry(names, schemas, active_error=None):
    """Patch bus.registry's discovery API. Returns a restore() callable."""
    import bus.registry as reg

    saved = (
        reg.get_active_tool_names,
        reg.get_tool_schemas_active,
        reg.list_tools,
    )

    def _active(session_id=""):
        if active_error is not None:
            raise active_error
        return set(names)

    def _active_schemas(session_id=""):
        # The wire-set lookup the manager actually calls.  Both entry
        # points raise, so a test that injects a failure does not depend
        # on which one the production path happens to use.
        if active_error is not None:
            raise active_error
        return list(schemas)

    reg.get_active_tool_names = _active
    reg.get_tool_schemas_active = _active_schemas
    reg.list_tools = lambda: list(names)

    def _restore():
        (
            reg.get_active_tool_names,
            reg.get_tool_schemas_active,
            reg.list_tools,
        ) = saved

    return _restore


_SCHEMAS = [
    {"type": "function", "function": {"name": "parse_pdf",
                                      "description": "Parse a PDF file",
                                      "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "web_search",
                                      "description": "Search the web",
                                      "parameters": {"type": "object"}}},
]


# ------------------------------------------------------------------
# 1. The parts handed to the gate are the objects that go on the wire
# ------------------------------------------------------------------

def test_non_history_parts_are_the_wire_set():
    """两个部分是本轮真实 wire 集与组装后的系统提示本身。

    Passing the whole registry's tool count here (~100) while the wire
    carries 22 is a 4.5x over-count that no constant can fix, because the
    wire set grows as the model discovers tools.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr = _make_mgr(tmp, FakeProvider())
        # 99 registered names, 2 of them on the wire.
        names = [f"tool_{i}" for i in range(99)] + ["parse_pdf", "web_search"]
        restore = _patch_registry(names, _SCHEMAS)
        try:
            system_text, tool_schemas = mgr._non_history_cost("sid")
        finally:
            restore()

        assert tool_schemas == _SCHEMAS, (
            "the wire set's schemas must travel, not the registry's"
        )
        assert len(tool_schemas) != len(names), "measured the whole registry"
        assert system_text, "the assembled system prompt must travel"
        print(f"  [ok] {len(tool_schemas)} schemas travel (registry holds "
              f"{len(names)}) + {len(system_text):,} chars of prompt")


def test_non_history_parts_are_not_cached():
    """每次都重新组装 —— 缓存会量一个已经不存在的提示词。

    `LEAF_memory.md` is rewritten by the background note thread every
    turn, so the prompt genuinely changes between turns.  A TTL cache would
    return a stale length for a prompt that no longer exists.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        import core.prompt_assembler as pa

        seen = []

        def _fake_prompt(*a, **k):
            seen.append(1)
            return "P" * (100 + len(seen))

        saved = pa.assemble_system_prompt
        pa.assemble_system_prompt = _fake_prompt
        try:
            mgr = _make_mgr(tmp, FakeProvider())
            first, _ = mgr._non_history_cost("sid")
            second, _ = mgr._non_history_cost("sid")
        finally:
            pa.assemble_system_prompt = saved

        assert first != second, (
            "the system prompt was reused across calls -- a cache is back"
        )
        assert len(seen) == 2, seen
        print(f"  [ok] prompt re-assembled per call ({len(first)} -> "
              f"{len(second)} chars, no stale length)")


# ------------------------------------------------------------------
# 2. A failed measurement must be VISIBLE
# ------------------------------------------------------------------

def test_measurement_failure_falls_back_loudly():
    """兜底不能静默失效 —— 失败必须打出原因。

    回退到猜测的常量是最坏的一种：失败时账目会静默变回本文件要消除的
    那几个数。现在它降级成"只算 messages"并说出来。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr = _make_mgr(tmp, FakeProvider())
        names = [f"tool_{i}" for i in range(99)]
        restore = _patch_registry(
            names, _SCHEMAS, active_error=RuntimeError("registry exploded")
        )
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                system_text, tool_schemas = mgr._non_history_cost("sid")
        finally:
            restore()

        out = buf.getvalue()
        assert tool_schemas == [], tool_schemas
        assert not system_text, f"{len(system_text)} chars of prompt leaked"
        assert "RuntimeError" in out, f"exception type not reported: {out!r}"
        assert "registry exploded" in out, f"reason not reported: {out!r}"
        assert "60" not in out, (
            f"the old guessed constant came back as a silent fallback: {out!r}"
        )
        print("  [ok] failure printed the type and the reason")


# ------------------------------------------------------------------
# 3. The system prompt travels as itself
# ------------------------------------------------------------------

def test_system_prompt_is_the_assembled_one():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        import core.prompt_assembler as pa

        saved = pa.assemble_system_prompt
        pa.assemble_system_prompt = lambda *a, **k: "S" * 777
        try:
            mgr = _make_mgr(tmp, FakeProvider())
            system_text, _ = mgr._non_history_cost("sid")
        finally:
            pa.assemble_system_prompt = saved

        assert system_text == "S" * 777, len(system_text)
        assert system_text != "S" * 1500, (
            "measurement fell back to the hardcoded 1500"
        )
        print(f"  [ok] the assembled prompt itself travels ({len(system_text)} "
              f"chars; the old constant was a number, not a text)")


# ------------------------------------------------------------------
# 4. One char-per-token constant, sourced from core.summarizer
# ------------------------------------------------------------------

def test_char_conversion_has_a_single_source():
    """`* 4` (cl100k's English ratio) must not come back."""
    import core.memory.manager as manager_mod
    from core.summarizer import CONSERVATIVE_CHARS_PER_TOKEN as summarizer_const

    assert manager_mod.CONSERVATIVE_CHARS_PER_TOKEN is summarizer_const, (
        "manager.py must import the constant, not define a second one"
    )
    assert summarizer_const == _CHARS_PER_TOKEN

    src = (_PROJECT_ROOT / "core" / "memory" / "manager.py").read_text(
        encoding="utf-8"
    )
    assert "history_budget_tokens * 4" not in src, (
        "the cl100k English ratio (x4) is back in the load budget"
    )
    assert "budget_chars = int(history_budget_tokens" in src
    print(f"  [ok] single source, x{summarizer_const} chars/token")


# ------------------------------------------------------------------
# 5. The budget charges what it actually sends
# ------------------------------------------------------------------

def test_history_budget_charges_what_it_sends():
    """预算按实发长度（截断后）扣，而不是按未截断的 len()。

    balanced tier caps the history budget at 16,384 tokens =>
    16,384 x 1.5 = 24,576 chars.  Messages are stored at 5,000 chars but
    injected truncated to 2,000, so all 10 must load (10 x 2,000 = 20,000
    <= 24,576).  Charging the untruncated length would admit only 4
    (4 x 5,000 = 20,000), which is the bug this guards.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr = _make_mgr(tmp, FakeProvider(context_window=131072))
        sid = mgr.short_term.init_session()

        for i in range(10):
            mgr.short_term.save_message(
                sid,
                {"role": "user", "content": f"[{i}]" + "x" * 4996},
                token_count=0,
            )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _, messages = mgr.load_session(sid)

        blob = "\n".join(str(m.get("content", "")) for m in messages)
        loaded = sum(1 for i in range(10) if f"[{i}]" in blob)
        assert loaded == 10, (
            f"only {loaded}/10 messages loaded -- the budget is charging "
            f"untruncated lengths again"
        )
        # And each one really was truncated on the way in.
        assert "x" * 3000 not in blob, "injected content was not truncated"

        # Negative control -- replay the OLD rule (charge the untruncated
        # len(content)) against the same budget.  Without this the test
        # could pass vacuously, e.g. if the budget were so large that
        # neither rule ever hit it.
        budget_chars = 16384 * _CHARS_PER_TOKEN
        old_loaded, old_chars = 0, 0
        for _ in range(10):
            if old_chars + 5000 > budget_chars and old_loaded:
                break
            old_chars += 5000
            old_loaded += 1
        assert old_loaded == 4, (
            f"negative control broken: the old rule would load {old_loaded}, "
            f"so this scenario does not discriminate"
        )

        print(f"  [ok] loaded {loaded}/10 (budget 16,384 x 1.5 = 24,576 chars, "
              f"charged 2,000 each; the old rule loaded only {old_loaded})")


# ------------------------------------------------------------------
# 6. The startup self-check prints both chains, separately
# ------------------------------------------------------------------

def test_startup_report_separates_the_two_chains():
    """启动自检必须把「压缩阈值」与「装载上限」分开列。

    把它们并成一条链比较会得出跨量纲的数字。
    报告若把它们并成一行，同一个误读就会再发生一次。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr = _make_mgr(tmp, FakeProvider(context_window=131072))
        restore = _patch_registry(["parse_pdf", "web_search"], _SCHEMAS)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                report = mgr.describe_thresholds()
                mgr.report_thresholds()
        finally:
            restore()

        out = buf.getvalue()
        assert report["compression_threshold"]["value"] == int(131072 * 0.85), report
        assert report["history_ceiling_balanced"]["value"] == 16384
        # Both numbers appear, but on DIFFERENT lines.
        threshold_line = [l for l in out.splitlines() if "压缩阈值" in l][0]
        ceiling_line = [l for l in out.splitlines() if "装载上限" in l][0]
        assert "111,411" in threshold_line, threshold_line
        assert "16,384" in ceiling_line, ceiling_line
        assert "不在同一条链上" in ceiling_line, ceiling_line
        # The non-history cost is reported as MEASURED, in the units the
        # gate uses, and split into its parts for auditing.
        nh = report["non_history_measured"]
        assert nh["value"] > 0 and nh["raw"] > 0
        assert nh["tool_tokens"] > 0, "the wire set's schemas were not counted"
        assert nh["system_tokens"] > 0
        assert nh["tool_count"] == len(_SCHEMAS)
        non_history_line = [l for l in out.splitlines() if "非历史实际开销" in l][0]
        assert "都进压缩闸门" in non_history_line, non_history_line
        print(f"  [ok] threshold {report['compression_threshold']['value']:,} "
              f"and ceiling {report['history_ceiling_balanced']['value']:,} "
              f"reported on separate lines")


# ------------------------------------------------------------------
# assistant 消息上 `content` 之外的那两样
# ------------------------------------------------------------------


def test_reply_side_payloads_are_counted():
    """`reasoning_content` 与 `tool_calls` 也在 wire 上，必须算。

    它们不在 `m["content"]` 里，而估算器若只读那一个键，就会漏掉它们。
    规模示例（334 条消息 · 同一把尺）：`content` 56,262 tokens，
    `reasoning_content` 152,596（2.7 倍），估算器报 71,638，
    provider 报 243,855。

    这不是"显示不准"：压缩闸门（`core/memory/compressor.py`）
    调的是同一个函数，它一直拿一个小了 3.4 倍的数在判该不该压。
    """
    p = FakeProvider()          # 1 token / 字符，差额就是那段的字符数
    body = {"role": "assistant", "content": "hello"}
    reasoning = "想" * 400
    calls = [{"id": "c1", "type": "function",
              "function": {"name": "read_excel_data",
                           "arguments": "{\"file_path\": \"a.xlsx\"}"}}]

    base = p.estimate_request_tokens([dict(body)])["messages"]
    with_r = p.estimate_request_tokens([{**body, "reasoning_content": reasoning}])
    with_tc = p.estimate_request_tokens([{**body, "tool_calls": calls}])

    assert with_r["messages"] - base == len(reasoning), (
        f"思考正文没被算进去：{with_r['messages']} vs {base}（差 "
        f"{with_r['messages'] - base}，应为 {len(reasoning)}）")
    expect_tc = len(json.dumps(calls, ensure_ascii=False))
    assert with_tc["messages"] - base == expect_tc, (
        f"工具参数没被算进去：{with_tc['messages']} vs {base}")
    # 两样一起出现时要相加，不是取大的那个
    both = p.estimate_request_tokens([{**body, "reasoning_content": reasoning,
                                       "tool_calls": calls}])
    assert both["messages"] - base == len(reasoning) + expect_tc
    print(f"  [ok] reasoning +{len(reasoning)} / tool_calls +{expect_tc} "
 f"=> 都进同一条请求的账")


def test_only_the_declared_shapes_are_counted():
    """负对照：只有 `str` 的 `reasoning_content` 与非空 `tool_calls` 才算。

    「读不到 ≠ 0」在这里的方向是：认不出来就当没有（返回 0），
    而不是给那个格子编一个数。反过来说，"空的两样"必须一个字都不加
    —— 否则每个 assistant 消息都会平白多出几 token，而那是一个看起来像结论的错数。
    """
    p = FakeProvider()
    base = p.estimate_request_tokens([{"role": "assistant", "content": "x"}])["messages"]
    for empty in (None, "", [], {}):
        assert p.estimate_request_tokens(
            [{"role": "assistant", "content": "x", "reasoning_content": empty}]
        )["messages"] == base, f"reasoning_content={empty!r} 不该加 token"
        assert p.estimate_request_tokens(
            [{"role": "assistant", "content": "x", "tool_calls": empty}]
        )["messages"] == base, f"tool_calls={empty!r} 不该加 token"
    # 形状坏掉时不许抛 —— 它跑在压缩闸门里，抛一次整轮就没了
    for weird in (123, ["a"], {"a": 1}, "not-a-list"):
        got = p.estimate_request_tokens(
            [{"role": "assistant", "content": "x", "reasoning_content": weird}]
        )["messages"]
        assert got >= base, (weird, got, base)
    print("  [ok] 认不出来 / 空 => 一个字都不加，且不抛")


# ------------------------------------------------------------------

def main() -> int:
    tests = [
        test_non_history_parts_are_the_wire_set,
        test_non_history_parts_are_not_cached,
        test_measurement_failure_falls_back_loudly,
        test_system_prompt_is_the_assembled_one,
        test_char_conversion_has_a_single_source,
        test_history_budget_charges_what_it_sends,
        test_startup_report_separates_the_two_chains,
        test_reply_side_payloads_are_counted,
        test_only_the_declared_shapes_are_counted,
    ]
    print("=" * 60)
    print("token accounting (R2, post-R3)")
    print("=" * 60)
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print("=" * 60)
    print(f" {len(tests) - failed}/{len(tests)} passed")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
