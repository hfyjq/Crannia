"""
Unit test for mid-term compression logic.

Validates:
  1. Token estimation triggers at the right threshold
  2. Turn boundary detection
  3. Compression produces shorter message list
  4. Backup is persisted to disk
  5. Context usage display helpers
  6. Post-compression _saved_count reset

This test uses a MockProvider and small context_window so
compression triggers after a few turns — no long conversation needed.

Usage:
    .venv/Scripts/python.exe tests/test_compressor.py
"""

import sys
import os
import tempfile
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from providers.base import BaseProvider


# ------------------------------------------------------------------
# Mock Provider — simulates LLM for compression summary
# ------------------------------------------------------------------

class MockProvider(BaseProvider):
    """Fake provider that returns a fixed summary.

    Inherits the real `estimate_request_tokens`: the token arithmetic
    under test is the production arithmetic.  A hand-copied estimator
    would let this file pass while the gate's number drifted.
    """

    def __init__(self, context_window: int = 4000):
        self._context_window = context_window
        self.chat_calls = 0  # track how many times chat() was called
        self.last_messages = None  # 记录"喂进去的是什么"

    @property
    def context_window(self) -> int:
        return self._context_window

    def count_tokens(self, text: str) -> int:
        """Naive token counter: ~1 token per char for Chinese, ~0.25 for English."""
        return len(text)

    async def chat_stream(self, messages, tools=None, system=None, thinking=None):
        raise NotImplementedError("streaming is not exercised here")

    def chat(self, messages: list[dict], tools=None, system=None, thinking=None):
        """Return a mock summary."""
        self.chat_calls += 1
        self.last_messages = messages
        # Return a ChatResponse-like object
        class FakeResponse:
            content = "用户讨论了文档分析系统的压缩功能测试，涉及多个技术话题。"
            tool_calls = None
            prompt_tokens = 200
            completion_tokens = 30

            @property
            def total_tokens(self):
                return self.prompt_tokens + self.completion_tokens

        return FakeResponse()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def make_long_conversation(turns: int) -> list[dict]:
    """
    Build a synthetic conversation with *turns* user↔assistant exchanges.

    Each message has ~200 chars so we can control when the threshold is hit.
    """
    messages = [
        {
            "role": "system",
            "content": "You are a document analysis assistant with persistent memory. " * 3,
        }
    ]
    for i in range(turns):
        messages.append({
            "role": "user",
            "content": f"[Turn {i}] 请帮我分析这份文档的内容，提取关键信息。关于第{i}章的细节，我需要了解更多上下文。这些数据是否与之前的发现一致？" * 2,
        })
        messages.append({
            "role": "assistant",
            "content": f"[Turn {i}] 根据分析，第{i}章的主要内容涉及几个重要主题。首先是关于数据一致性的讨论，其次是方法论的验证。结果表明之前的假设基本成立。" * 2,
        })
    return messages


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

def test_estimate_message_tokens():
    """Verify token estimation includes role overhead."""
    provider = MockProvider(context_window=4000)
    messages = make_long_conversation(1)
    total = provider.estimate_message_tokens(messages)
    assert total > 0, "Token estimate should be > 0"
    print(f"  ✅ estimate_message_tokens: {total} tokens for {len(messages)} messages")


def test_compression_triggers():
    """Compression should trigger when token estimate > 85% of context_window."""
    from core.memory.compressor import Compressor

    provider = MockProvider(context_window=2000)  # small window → easy trigger
    compressor = Compressor(threshold=0.85, keep_recent_turns=3)

    # Build conversation that exceeds threshold
    messages = make_long_conversation(8)  # ~8 * (200+200) + overhead ≈ 3200+ tokens > 1700
    total = provider.estimate_message_tokens(messages)
    threshold = int(2000 * 0.85)

    print(f"  total_tokens={total}, threshold={threshold}")
    assert compressor.should_compress(total, 2000), \
        f"should_compress should return True: {total} > {threshold}"

    # Run compression
    compressed, was_compressed = compressor.check_and_compress(
        session_id="test_session_1",
        messages=messages,
        provider=provider,
        context_window=2000,
    )

    assert was_compressed, "Compression should have been triggered"
    assert len(compressed) < len(messages), \
        f"Compressed list ({len(compressed)}) should be shorter than original ({len(messages)})"
    assert compressed[0]["kind"] == "history_summary", \
        "First message should be the tagged summary"
    assert compressed[0]["role"] == "user", \
        "the summary is a user turn (P1) — see test_summary_message_is_marked"
    assert "<historical_context>" in compressed[0]["content"], \
        "Summary should contain <historical_context> tag"

    # Verify stats
    assert compressor.last_compression is not None
    stats = compressor.last_compression
    assert stats["compressed_turns"] > 0
    assert stats["old_message_count"] == len(messages)
    assert stats["new_message_count"] == len(compressed)

    print(f"  ✅ Compression triggered: {len(messages)} → {len(compressed)} messages")
    print(f"     Stats: {stats}")


class _FailingProvider(MockProvider):
    """摘要调用永远返回空 —— 模拟"摘要服务这次没产出"。"""

    def chat(self, messages, tools=None, system=None, thinking=None):
        self.chat_calls += 1

        class _Empty:
            content = ""
            tool_calls = None
            prompt_tokens = 10
            completion_tokens = 0

            @property
            def total_tokens(self):
                return 10

        return _Empty()


def test_w311_failed_summary_still_compresses():
    """摘要失败不许原样返回 —— 否则热会话每轮都在长。

    旧形状是 `return messages, False`，而冷闸滑窗在热会话里永远不剪
    （它要求"上次活动 > 90 分钟"）-> 一次摘要失败之后这个会话永远不缩，
    同时每轮还要全表估算与扫描。失败本身只是一行日志。
    """
    from core.memory.compressor import Compressor

    provider = _FailingProvider(context_window=2000)
    compressor = Compressor(threshold=0.85, keep_recent_turns=3)
    messages = make_long_conversation(8)

    compressed, was_compressed = compressor.check_and_compress(
        session_id="test_session_w311",
        messages=messages,
        provider=provider,
        context_window=2000,
    )

    assert was_compressed, "摘要失败时也要压（兜底），不许原样返回"
    assert len(compressed) < len(messages), (
        "兜底必须真的把较早的轮次移出活动窗口：%d -> %d"
        % (len(messages), len(compressed)))
    assert compressed[0]["kind"] == "history_summary"
    assert "history_search" in compressed[0]["content"], (
        "兜底摘要必须告诉模型去哪儿找回（window_note 同一条判据）")
    assert compressor.last_compression is not None
    assert compressor.last_compression["source"] == "fallback", (
        "来源要如实记成 fallback，不许冒充 llm：%r"
        % (compressor.last_compression.get("source"),))
    print("  [ok] 摘要失败 -> 兜底压缩 %d -> %d，source=fallback"
          % (len(messages), len(compressed)))


def test_compression_not_triggered_when_below_threshold():
    """Compression should NOT trigger when token estimate < 85% of context_window."""
    from core.memory.compressor import Compressor

    provider = MockProvider(context_window=100000)  # huge window
    compressor = Compressor(threshold=0.85, keep_recent_turns=3)

    messages = make_long_conversation(3)
    compressed, was_compressed = compressor.check_and_compress(
        session_id="test_session_2",
        messages=messages,
        provider=provider,
        context_window=100000,
    )

    assert not was_compressed, "Compression should NOT trigger below threshold"
    assert compressed == messages, "Should return original messages unchanged"
    print(f"  ✅ Below threshold: compression correctly skipped")


def test_summary_message_is_marked():
    """The summary must be a TAGGED message, not an on-disk copy.

    Contract: the summary is what has to survive a restart, so it
    must be distinguishable from real conversation turns when the session
    is reloaded.  The loader keeps it because
    kind == "history_summary" — not because of its role.

    The role here is `user`, not `role="system"`.  Reasons — all of
    them the reason the assertion below is written against `kind` and
    not `role`:

      * on the Anthropic wire every in-list `system` message was joined
        into the top-level `system` string, so the summary — *history* —
        was delivered to Claude as a standing instruction;
      * a summary and the system prompt sharing one role is precisely the
        confusion this design avoids; and
      * reference implementations put the summary in a user turn.

    If this assertion is ever changed back to role == "system", the
    Anthropic bug comes back with it.
    """
    from core.memory.compressor import Compressor

    provider = MockProvider(context_window=1500)
    compressor = Compressor(threshold=0.85, keep_recent_turns=2)

    compressed, was_compressed = compressor.check_and_compress(
        session_id="marker_test",
        messages=make_long_conversation(6),
        provider=provider,
        context_window=1500,
    )

    assert was_compressed
    head = compressed[0]
    assert head["role"] == "user", (
        "summary must be a user turn — see the docstring; role=system here "
        "means the Anthropic wire will deliver history as an instruction"
    )
    assert head.get("kind") == "history_summary", (
        "summary must carry kind=history_summary, otherwise the loader "
        "silently drops it on reload (that was B5)"
    )
    assert head["content"].startswith("<historical_context>")
    print("  OK  Summary is a user turn carrying kind=history_summary")


def test_context_usage_display():
    """Test MemoryManager.get_context_usage() returns correct structure."""
    from core.memory.manager import MemoryManager

    provider = MockProvider(context_window=65536)

    # Create a minimal config
    config = {
        "memory": {
            "enabled": True,
            "storage_path": ":memory:",  # won't be used in this test
        }
    }

    # `ignore_cleanup_errors=True`：Windows 上 sqlite 的 WAL 锁释放不是瞬时的，
    #   连接关了那一瞬间目录还可能删不掉 —— 不吞这个就变成"测试失败"。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        # Point all storage paths to tmpdir
        config["memory"]["storage_path"] = tmpdir
        config["memory"]["short_term"] = {"max_history": 100}
        config["memory"]["compression"] = {"token_threshold": 0.85, "keep_recent_turns": 3}

        manager = MemoryManager(config, provider)

        messages = make_long_conversation(5)
        usage = manager.get_context_usage(messages)

        assert "estimated_tokens" in usage
        assert "context_window" in usage
        assert "percentage" in usage
        assert "threshold_pct" in usage
        assert usage["context_window"] == 65536
        assert usage["percentage"] > 0
        assert usage["threshold_pct"] == 85.0

        print(f"  ✅ Context usage: {usage['percentage']}% "
              f"({usage['estimated_tokens']}/{usage['context_window']} tokens)")

        manager.close()


def test_note_is_input_not_a_substitute():
    """摘要一律由模型写；会话笔记只作输入，不再是零成本替身。

    两轮会话手动压缩时，笔记曾被直接顶成摘要 —— 那几千字符全是
    「做到哪 / 下一步 / 关键决定」，是状态，不是「这段对话发生过什么」。
    长对话最缺的恰恰是后者。

    所以这条钉两个方向：有笔记也要调模型（笔记随输入喂进去让它并进摘要）；
    没有笔记 / 笔记是空白，同样调模型。
    """
    from core.memory.compressor import Compressor

    with tempfile.TemporaryDirectory() as tmpdir:
        # ---- Case 1: with session_note → no LLM call ----
        provider1 = MockProvider(context_window=1500)
        compressor1 = Compressor(
            threshold=0.85, keep_recent_turns=2,
        )

        messages = make_long_conversation(6)
        session_note = (
            "# Session Note\n\n"
            "## Current Status\n- Testing bypass\n\n"
            "## Key Decisions\n- Use session note\n\n"
            "## Open Questions\n(None)\n\n"
            "## Next Steps\n- Verify\n"
        )

        calls_before = provider1.chat_calls
        compressed, was_compressed = compressor1.check_and_compress(
            session_id="bypass_test_1",
            messages=messages,
            provider=provider1,
            context_window=1500,
            session_note=session_note,
        )

        assert was_compressed, "Compression should trigger"
        assert provider1.chat_calls > calls_before, (
            "有笔记时也必须调模型（笔记是输入，不是替身）"
            f"(calls: {calls_before} → {provider1.chat_calls})"
        )
        assert "<historical_context>" in compressed[0]["content"]
        assert compressor1.last_compression["source"] == "llm"
        #: 笔记要真的进输入面，否则它只是"没被用上"
        _seen = "\n".join(str(m.get("content") or "")
                          for m in (provider1.last_messages or []))
        assert "Testing bypass" in _seen, "笔记没有进摘要输入面"
        print("  [ok] 有笔记 -> 仍走模型，笔记进输入面")

        # ---- Case 2: without session_note → fall back to LLM ----
        provider2 = MockProvider(context_window=1500)
        compressor2 = Compressor(
            threshold=0.85, keep_recent_turns=2,
        )

        calls_before2 = provider2.chat_calls
        compressed2, was_compressed2 = compressor2.check_and_compress(
            session_id="bypass_test_2",
            messages=messages,
            provider=provider2,
            context_window=1500,
            session_note=None,  # explicitly None
        )

        assert was_compressed2, "Compression should trigger"
        assert provider2.chat_calls > calls_before2, (
            f"LLM SHOULD be called when no session_note "
            f"(calls: {calls_before2} → {provider2.chat_calls})"
        )
        assert compressor2.last_compression["source"] == "llm"
        print("  ✅ no session_note → LLM fallback works")

        # ---- Case 3: empty session_note → fall back to LLM ----
        provider3 = MockProvider(context_window=1500)
        compressor3 = Compressor(
            threshold=0.85, keep_recent_turns=2,
        )

        calls_before3 = provider3.chat_calls
        compressed3, was_compressed3 = compressor3.check_and_compress(
            session_id="bypass_test_3",
            messages=messages,
            provider=provider3,
            context_window=1500,
            session_note="   ",  # whitespace only
        )

        assert was_compressed3
        assert provider3.chat_calls > calls_before3, (
            "LLM SHOULD be called when session_note is whitespace-only"
        )
        assert compressor3.last_compression["source"] == "llm"
        print("  ✅ whitespace session_note → LLM fallback works")

        # ---- Case 4（ ）：有旧摘要 + 有笔记 → 必须合并 ----
        provider4 = MockProvider(context_window=1500)
        compressor4 = Compressor(threshold=0.85, keep_recent_turns=2)

        with_prior = make_long_conversation(6)
        # 旧摘要住在前导块里（`_find_summary_text` 就在那儿找它）
        with_prior.insert(1, {
            "role": "user", "kind": "history_summary",
            "content": "<historical_context>\n上一代摘要：聊过排版与字体\n"
                       "</historical_context>",
        })

        calls_before4 = provider4.chat_calls
        compressed4, was4 = compressor4.check_and_compress(
            session_id="bypass_test_4",
            messages=with_prior,
            provider=provider4,
            context_window=1500,
            session_note=session_note,
        )

        assert was4, "Compression should trigger"
        assert provider4.chat_calls > calls_before4, (
            "有旧摘要时必须走 LLM 合并 —— 直接拿笔记当摘要 "
            "会把上一代整个扔掉"
        )
        assert compressor4.last_compression["source"] == "llm"
        frame = "\n".join(str(m.get("content", ""))
                          for m in (provider4.last_messages or []))
        assert "上一代摘要：聊过排版与字体" in frame, (
            "旧摘要的正文必须进了这次摘要调用的输入"
        )
        assert "Testing bypass" in frame, "会话笔记也要一起喂进去"
        # 新摘要必须是摘要器产出的，不是笔记照抄 —— 这一条对两条路
        #   都成立（笔记当摘要那条已经删掉了）。
        assert "Testing bypass" not in compressed4[0]["content"], (
            "新摘要必须是摘要器产出的，不是笔记照抄")
        # 这一行用 ASCII（不是文件里别处那种 ✅）—— GBK 棘轮只许降：
        #    新加的 print 里每多一个非 GBK 符号就是 +1。
        print("  OK  prior summary + note -> merged through the LLM")


def test_summary_carries_verbatim_user_lines_and_changed_files():
    """「用户原话」与「动过的文件」由框架给，不经模型改写。"""
    from core.memory.compressor import Compressor, _recent_user_utterances
    from core.touched_files import forget as forget_touched, note as note_touched

    messages = make_long_conversation(6)
    messages[1] = {"role": "user", "content": "原话甲：把表格列宽固定成 12"}
    messages[3] = {"role": "user", "content": "原话乙：不要动第二张表"}
    # 框架写的 user 行（`kind` 非空）不是用户原话，必须被排除
    messages[2] = {"role": "user", "kind": "window_note",
                   "content": "框架说明行，不是原话"}

    utterances = _recent_user_utterances(messages)
    assert "原话甲：把表格列宽固定成 12" in utterances, utterances
    assert "原话乙：不要动第二张表" in utterances, utterances
    assert not any("框架说明行" in u for u in utterances), utterances
    assert len(utterances) <= 12, len(utterances)

    key = "workspace:private:utt-test"
    forget_touched(key)
    note_touched(key, "workspace/a.docx")
    note_touched(key, "workspace/b.xlsx")
    try:
        provider = MockProvider(context_window=1500)
        compressor = Compressor(threshold=0.85, keep_recent_turns=2)
        compressed, was = compressor.check_and_compress(
            session_id="utt_test", messages=messages, provider=provider,
            context_window=1500, channel_key=key)
        assert was
        body = compressed[0]["content"]
        assert "## 用户原话" in body, body[:300]
        assert "原话甲：把表格列宽固定成 12" in body, "原话被改写了"
        assert "## 动过的文件" in body, body[-400:]
        assert "a.docx" in body and "b.xlsx" in body
        print("  [ok] 原话逐字 + 动过的文件都进了摘要消息")
    finally:
        forget_touched(key)


class _SometimesProvider(MockProvider):
    """前 N 次返回空摘要，之后正常 —— 用来走失败阶梯。

    继承 MockProvider 是为了拿到真的 token 估算器：闸门量的就是那个数。
    """

    def __init__(self, fails: int, text: str = "模型摘要",
                 context_window: int = 1500) -> None:
        super().__init__(context_window=context_window)
        self.calls = 0
        self._fails = fails
        self._text = text
        self.inputs: list = []

    def chat(self, messages, tools=None, system=None, thinking=None):
        self.calls += 1
        self.inputs.append(list(messages))

        class _R:
            content = "" if self.calls <= self._fails else self._text

        return _R()


def test_failed_summary_falls_back_to_note_and_marks_it():
    """两级都没产出 -> 笔记顶上，并且模型看到"这是降级摘要"。"""
    from core.memory.compressor import Compressor, breaker_open, forget_breaker

    forget_breaker("fb_test")
    provider = _SometimesProvider(fails=99)
    compressor = Compressor(threshold=0.85, keep_recent_turns=2)
    messages = make_long_conversation(8)
    compressed, was = compressor.check_and_compress(
        session_id="fb_test", messages=messages, provider=provider,
        context_window=1500, session_note="笔记：做到第 3 步，下一步跑测试")

    assert was, "降级也必须真压"
    assert provider.calls == 2, "失败后必须重试一次（calls=%d）" % provider.calls
    body = compressed[0]["content"]
    assert "做到第 3 步" in body, "笔记没顶上"
    assert "降级摘要" in body and "history_search" in body, (
        "模型看到的摘要必须写明这是降级产物、以及去哪儿找回")
    assert compressor.last_compression["level"] == "note"
    print("  [ok] 模型摘要失败 -> 重试 -> 笔记顶上（对模型标注降级）")


def test_retry_shortens_the_input():
    """第二次尝试用的是缩短过的输入。"""
    from core.memory.compressor import Compressor, forget_breaker

    forget_breaker("retry_test")
    provider = _SometimesProvider(fails=1)
    compressor = Compressor(threshold=0.85, keep_recent_turns=2)
    compressor.check_and_compress(
        session_id="retry_test", messages=make_long_conversation(8),
        provider=provider, context_window=1500, session_note="")

    assert provider.calls == 2
    first = provider.inputs[0][-1]["content"]
    second = provider.inputs[1][-1]["content"]
    assert len(second) <= len(first), (len(first), len(second))
    assert compressor.last_compression["level"] == "retry"
    body = compressor.last_compression
    assert body["source"] == "llm"
    print("  [ok] 重试用缩短的输入，来源仍记 llm")


def test_breaker_opens_after_three_degraded():
    """连续三次降级 -> 熔断；之后不再花模型调用，但仍然压。"""
    from core.memory import compressor as C

    C.forget_breaker("brk_test")
    provider = _SometimesProvider(fails=99)
    comp = C.Compressor(threshold=0.85, keep_recent_turns=2)
    msgs = make_long_conversation(8)

    levels = []
    for i in range(3):
        comp.check_and_compress(
            session_id="brk_test", messages=msgs, provider=provider,
            context_window=1500, session_note="笔记：第 %d 次" % i)
        levels.append(comp.last_compression["level"])
    assert levels == ["note", "note", "note"], levels
    assert C.breaker_open("brk_test"), "三次降级后应当熔断"
    assert comp.last_compression["breaker_tripped"] is True, "第三次要报'刚熔断'"

    calls_before = provider.calls
    comp.check_and_compress(
        session_id="brk_test", messages=msgs, provider=provider,
        context_window=1500, session_note="笔记：第四次")
    assert provider.calls == calls_before, "熔断后不许再试模型摘要"
    assert comp.last_compression["level"] == "note", "熔断后仍然压（降级路径）"
    print("  [ok] 三次降级 -> 熔断 -> 不再花模型调用，仍照压")

    # 手动压缩 = 用户说"再试一次" -> 清零
    C.forget_breaker("brk_test")
    assert not C.breaker_open("brk_test")


def test_progress_payload_three_outcomes():
    """界面那一格：ok 带百分比 / degraded / broken 各有各的话。"""
    from core.memory.compressor import progress_payload

    ok = progress_payload({"level": "llm", "post_tokens": 82_000}, 900_000)
    assert ok["state"] == "ok" and ok["percent"] == 9, ok
    assert "9" in ok["message"], ok
    degraded = progress_payload({"level": "note", "post_tokens": 10}, 100)
    assert degraded["state"] == "degraded"
    assert "质量降低" in degraded["message"], degraded["message"]
    assert "重启应用" not in degraded["message"], "降级不等于要重启"
    blocked = progress_payload(None, 100, payload_blocked=True)
    assert blocked["state"] == "degraded" and "重启应用" in blocked["message"]
    broken = progress_payload({"level": "cut", "breaker_tripped": True}, 100)
    assert broken["state"] == "broken" and "压缩器异常" in broken["message"]
    unknown = progress_payload({"level": "llm"}, None)
    assert unknown["percent"] is None, "读不到百分比不许编"
    print("  [ok] ok/degraded/broken 三种收尾各一句话")


def test_summary_lists_read_files_as_pointers():
    """读过但没改过的文件：只留路径（+ 还在不在），不挂正文。"""
    from core import file_tools as FT
    from core.memory.compressor import Compressor
    from core.touched_files import forget as forget_t, note as note_t

    key = "workspace:private:reads-test"
    FT.forget_reads(key)
    forget_t(key)
    with tempfile.TemporaryDirectory() as td:
        here = Path(td) / "a.txt"
        here.write_text("x", encoding="utf-8")
        gone = Path(td) / "gone.txt"
        gone.write_text("x", encoding="utf-8")
        for p in (here, gone):
            FT._note_read(key, p)
        gone.unlink()                         # 读过之后被删了 -> 已不在
        note_t(key, str(here))                # 改过的那个不该在「读过的」里重复
        try:
            provider = MockProvider(context_window=1500)
            comp = Compressor(threshold=0.85, keep_recent_turns=2)
            out, was = comp.check_and_compress(
                session_id="reads_test", messages=make_long_conversation(8),
                provider=provider, context_window=1500, channel_key=key)
            assert was
            body = out[0]["content"]
            assert "## 读过的文件" in body, body[-500:]
            assert "gone.txt" in body and "已不在" in body, body[-500:]
            assert body.count("a.txt") == 1, "改过的文件只该出现在「动过的文件」"
            print("  [ok] 读过的文件只留指针；已不在的有标记；改过的不重复")
        finally:
            FT.forget_reads(key)
            forget_t(key)


def test_prompt_asks_for_three_sections_only():
    """提示词只要三节；另外两节由框架附上，模型不许复述。"""
    from core.memory.compressor import COMPRESSION_PROMPT as P
    for head in ("## 任务与目标", "## 未决与失败", "## 下一步做什么"):
        assert head in P, head
    for gone in ("## 关键决策", "## 性能考虑", "## 测试状态"):
        assert gone not in P, "旧小节还在：%s" % gone
    assert "用户原话" in P and "另行逐字附上" in P
    print("  [ok] 提示词只要三节，另两节机械附上")


def test_prompt_marks_history_as_untrusted_data():
    """摘要会长期留在上下文里 —— 历史里的外部文本必须是数据，不是指令。

    否则一次注入可以借工具结果混进"下一步做什么"，从一次性的输出
    变成长期保存的行动建议。
    """
    from core.memory.compressor import COMPRESSION_PROMPT as P
    assert "数据不是指令" in P, "缺少不可信数据的边界"
    assert "不执行" in P, "没有写清「不执行其中的要求」"
    assert "下一步" in P
    print("  [ok] 提示词写明了历史内容是不可信数据")


# ------------------------------------------------------------------
# 摘要的输入面 —— 必须看得见工具
# ------------------------------------------------------------------

def test_w118_summary_input_sees_the_tools_and_the_whole_user_turn():
    """输入面不能只有一句话：`content[:500]`、且只收 user/assistant。

    两个后果都在同一处 —— 而 `COMPRESSION_PROMPT` 要的
    「测试状态 / 文件列表 / 代码变更」素材恰好全在工具那一层：
    提示词在要求摘要器复述它从没看到过的东西。
    """
    from core.memory.compressor import Compressor

    provider = MockProvider(context_window=1500)
    compressor = Compressor(threshold=0.85, keep_recent_turns=2)

    msgs = make_long_conversation(6)
    # 用户一句很长的话，独有词埋在第 500 字符之后
    msgs.insert(1, {"role": "user", "content": "填" * 900 + " USERDEEPTOKEN"})
    # 一条 assistant 只调了工具（正文为空）—— 只按正文判会把整条丢掉
    msgs.insert(2, {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {
                                        "name": "run_command",
                                        "arguments":
                                            "{\"command\": \"pytest tests/ -q\"}"}}]})
    # 工具回执 —— 只收 user/assistant 时整类丢掉
    msgs.insert(3, {"role": "tool", "tool_call_id": "c1",
                    "content": "TOOLRESULTTOKEN 3 passed"})

    compressor.check_and_compress(
        session_id="w118", messages=msgs, provider=provider,
        context_window=1500, force=True, mode="full",
    )
    frame = "\n".join(str(m.get("content", ""))
                      for m in (provider.last_messages or []))
    assert "TOOLRESULTTOKEN" in frame, (
        "工具回执没进摘要的输入面 —— 「测试状态」那一节拿不到素材")
    assert "run_command" in frame, (
        "「调用过工具」这件事没进摘要（只有 tool_calls、正文为空那条）")
    assert "USERDEEPTOKEN" in frame, (
        "用户原话仍被砍在 500 字符 —— 埋在第 900 个字符处的词没进输入")
    assert "Tool(run_command)" in frame, "工具回执没带上是哪个工具"
    print("  OK  W118 input face: tools + full user turn reach the summarizer")


def test_w118_input_budget_says_so_when_it_truncates():
    """总闸超了要如实出声（少也要说是少了）。"""
    import core.memory.compressor as C

    provider = MockProvider(context_window=1500)
    compressor = C.Compressor(threshold=0.85, keep_recent_turns=2)
    saved = C._SUMMARY_IN_TOTAL_MAX
    C._SUMMARY_IN_TOTAL_MAX = 300          # 逼出阀门
    try:
        compressor.check_and_compress(
            session_id="w118b", messages=make_long_conversation(6),
            provider=provider, context_window=1500, force=True, mode="full",
        )
        frame = "\n".join(str(m.get("content", ""))
                          for m in (provider.last_messages or []))
        assert "没能纳入本次摘要输入" in frame, (
            "超了输入上限却一声不响 —— 静默少一截就是这一批要消灭的形状")
    finally:
        C._SUMMARY_IN_TOTAL_MAX = saved
    print("  OK  W118 input budget discloses truncation")


# ------------------------------------------------------------------
# 摘要里的路标
# ------------------------------------------------------------------

def test_w168_summary_pointer_does_not_accumulate():
    """路标挂在包裹外面 —— 连压两次，它只许有一条。

    判据：`_find_summary_text` 若写成"把两个标签减掉、
    其余原样返回"，包裹外的东西就会被当成上一代摘要往后喂，每压一次
    多一条。修法是只取包裹里的正文（`strip_summary_wrapper`）。
    """
    from core.memory.compressor import (
        SUMMARY_POINTER, Compressor, _find_summary_text,
    )

    provider = MockProvider(context_window=1500)
    c = Compressor(threshold=0.85, keep_recent_turns=1)

    out1, ok1 = c.check_and_compress(
        session_id="ptr", messages=make_long_conversation(4),
        provider=provider, context_window=1500, force=True, mode="full",
    )
    assert ok1 and out1[0].get("kind") == "history_summary"
    assert out1[0]["content"].count(SUMMARY_POINTER) == 1, "第一代就该有路标"

    # 把第一代的产物当成"上一代摘要"再压一次（它落在 head 里）
    out2, ok2 = c.check_and_compress(
        session_id="ptr", messages=out1 + make_long_conversation(3)[1:],
        provider=provider, context_window=1500, force=True, mode="full",
    )
    assert ok2
    assert out2[0]["content"].count(SUMMARY_POINTER) == 1, (
        "路标被当成摘要正文吞进去、再吐出来了 —— 它逐代累积了")
    # 判据的下一层（直接打在那个函数上，上面那两条只是它的后果）
    # 不能拿 `out2[1:]` 去试：`_full_compress` 会把旧摘要从 head 里摘掉
    #    （它是"吸收"，不是"留着"），out2 里根本没有旧摘要行。
    head = [{"role": "user", "kind": "history_summary",
             "content": "<historical_context>\nOLDSUM\n</historical_context>"
                        "\n\n" + SUMMARY_POINTER}]
    prior = _find_summary_text(head)
    assert prior == "OLDSUM", (
        "剥包裹没剥干净 —— 往下一代喂的应当是包裹里的正文，实得 %r" % prior)
    assert SUMMARY_POINTER not in prior, (
        "_find_summary_text 把包裹外面的路标算成摘要正文了")
    print("  OK  W168 signpost: one copy, outside the wrapper, never fed forward")


# ------------------------------------------------------------------
# `/compact` 是手动逃生口，不是"等阈值"
# ------------------------------------------------------------------

def test_force_compacts_below_the_threshold():
    """判据：在远低于阈值的会话里按 `/compact`，真的压了。

    若 `mode="full"` 不在阈值豁免里，
    阈值之下 `check_and_compress` 第一句就返回 `(messages, False)`。
    """
    from core.memory.compressor import Compressor
    c = Compressor(threshold=0.85, keep_recent_turns=3)
    #: 占用要过 10%（新判据）但低于 85%（阈值），force 才该压。
    p = MockProvider(context_window=8_000)
    msgs = make_long_conversation(8)
    assert not c.should_compress(1_400, 8_000)
    out, done = c.check_and_compress("t:force", msgs, p, p.context_window,
                                     session_note="这是会话笔记",
                                     mode="full", force=True)
    assert done is True, (
        "阈值之下 `force=True` 没压 —— `/compact` 仍然是个摆设")
    assert len(out) < len(msgs), "压完条数没少 (%d -> %d)" % (len(msgs), len(out))
    assert c.last_skip_note == "", (
        "压成了却留着一条「没压」的说明：%r" % c.last_skip_note)


def test_without_force_the_reason_is_the_threshold_not_the_turn_count():
    """判据：回话里的原因与真实原因一致。

    真因是「没过阈值」；若回的是 "Not enough turns to compact."，
    那就是 一个确定的错原因（标签在说谎的同族）。

    这条同时把两个方向都钉上：
      · 没过阈值，说明里必须点出阈值/占比，不许提"轮数"；
      · 轮数真不够，那时才许提轮数（下一条）。
    """
    from core.memory.compressor import Compressor
    c = Compressor(threshold=0.85, keep_recent_turns=3)
    p = MockProvider(context_window=1_000_000)
    msgs = make_long_conversation(8)
    out, done = c.check_and_compress("t:noforce", msgs, p, p.context_window,
                                     session_note="笔记", mode="full")
    assert done is False and out == msgs, "没过阈值不该压"
    note = c.last_skip_note
    assert note, "没压却不给原因 —— 回话那边就只剩沉默或编一句"
    assert "压缩线" in note or "阈值" in note, (
        "没点出真因（没过阈值）：%r" % note)
    assert "轮" not in note or "轮次不够" not in note, (
        "又把真因说成了'轮数不够' —— 那正是这条要修的：%r" % note)


def test_below_ten_percent_refuses_with_one_sentence():
    """占用不到窗口的 10%，不压，且只说那一句（不报轮数）。

    这是 `/compact` 在"刚开聊"时该有的回答。
    """
    from core.i18n import t as tr
    from core.memory.compressor import Compressor
    c = Compressor(threshold=0.85, keep_recent_turns=3)
    p = MockProvider(context_window=1_000_000)     # 占用 0.2% => 远低于 10%
    msgs = make_long_conversation(8)
    out, done = c.check_and_compress("t:tiny", msgs, p, p.context_window,
                                     session_note="笔记", mode="full", force=True)
    assert done is False and out == msgs, "未达 10% 不该假装压过"
    assert c.last_skip_note == tr("当前上下文容量未达最低压缩量"), (
        "只能说那一句（且不许带数字）：%r" % c.last_skip_note)


def test_few_turns_fall_back_to_a_token_tail():
    """轮数不够、但占用过了 10%，按 token 找切点，不按轮。

    判据：最后一条 `user` 消息必须留在保留区（用户这一问不能被压没）。
    """
    from core.memory.compressor import Compressor
    c = Compressor(threshold=0.85, keep_recent_turns=3)
    p = MockProvider(context_window=3_000)         # 2 轮 ≈ 12% => 过 10%
    msgs = make_long_conversation(2)
    out, done = c.check_and_compress("t:few", msgs, p, p.context_window,
                                     session_note="笔记", mode="full", force=True)
    assert done is True, "轮数不够但有量 -> 应当按 token 尾部压（%r）" % c.last_skip_note
    #: 条数不一定变少（摘要顶掉的那几条可能正好等于新增的那一条）——
    #:   判据是"旧的被摘要替换了" + "最后那条 user 还在"。
    assert any(m.get("kind") == "history_summary" for m in out), "没有摘要行"
    assert out != msgs, "输出与输入一模一样"
    last_user = [m for m in msgs if m.get("role") == "user"][-1]
    assert last_user in out, "最后一条 user 被压掉了"

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Mid-term Compression Tests")
    print("=" * 60)
    print()


    tests = [
        ("Token estimation", test_estimate_message_tokens),
        ("Compression triggers above threshold", test_compression_triggers),
        ("failed summary still compresses", test_w311_failed_summary_still_compresses),
        ("No compression below threshold", test_compression_not_triggered_when_below_threshold),
        ("Summary message is tagged", test_summary_message_is_marked),
        ("Context usage display", test_context_usage_display),
        ("Session note is input, not a substitute", test_note_is_input_not_a_substitute),
        ("Summary carries verbatim user lines + changed files",
         test_summary_carries_verbatim_user_lines_and_changed_files),
        ("read files listed as pointers",
         test_summary_lists_read_files_as_pointers),
        ("Prompt asks for 3 sections only", test_prompt_asks_for_three_sections_only),
        ("failed summary -> note fallback (marked for the model)",
         test_failed_summary_falls_back_to_note_and_marks_it),
        ("retry shortens the input", test_retry_shortens_the_input),
        ("breaker opens after three degraded", test_breaker_opens_after_three_degraded),
        ("progress payload: ok / degraded / broken",
         test_progress_payload_three_outcomes),
        # `/compact` 是手动逃生口
        ("W117 /compact forces below threshold",
         test_force_compacts_below_the_threshold),
        ("W117 the skip reason is the threshold",
         test_without_force_the_reason_is_the_threshold_not_the_turn_count),
        ("below 10% of the window refuses with one sentence",
         test_below_ten_percent_refuses_with_one_sentence),
        ("few turns fall back to a token tail",
         test_few_turns_fall_back_to_a_token_tail),
        # 输入面 + 路标
        # 这个 runner 是手写清单 —— 新写的 `test_*` 不登记就静默不跑
        #    （清单型 runner 漏登记 = 零输出、零报错）。
        ("W118 input face sees tools + whole user turn",
         test_w118_summary_input_sees_the_tools_and_the_whole_user_turn),
        ("W118 input budget discloses truncation",
         test_w118_input_budget_says_so_when_it_truncates),
        ("W168 signpost does not accumulate",
         test_w168_summary_pointer_does_not_accumulate),
        ("prompt marks history as untrusted data",
         test_prompt_marks_history_as_untrusted_data),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  ❌ FAIL: {e}")
            import traceback
            traceback.print_exc()

    print()
    print("=" * 60)
    print(f"  Results: {passed} passed, {failed} failed, {len(tests)} total")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
