"""
Reachability tests for the mid-term compression threshold.

依据:

    每个触发式机制（压缩、限流、清理、兜底）都必须能回答一句话：
    "在最大真实负载下，它什么时候触发？" 答不出来，它就是死代码。

    测试里加一条"可达性断言"：构造最大真实负载，断言机制确实触发过。

为什么这个文件存在: 一条断言曾声称"中期压缩永不触发"，依据是

    装载上限 16,384 tokens vs 阈值 111,411 tokens，差 6.8 倍

但那两个数不在同一条链上:

  * `ceiling_tokens` 只压在 `load_session` 的装载循环里
    (`manager.py`), 而 `load_session` 全文没有 `check_and_compress`;
  * `should_compress` 跑在 `record_turn` 里的活的消息表上,
    不受该 ceiling 约束; 而它量的又是 `estimate_message_tokens`
    (tiktoken cl100k x 校准因子), 装载侧的 16,384 是另一套口径。

所以"6.8 倍"是跨链比较。到底触不触发算不出来 —— 本文件把它变成
可复跑的断言, 这是它唯一的目的。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_compression_reachability.py
"""

import contextlib
import io
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from providers.base import BaseProvider

# ASCII-only output -- the Windows console defaults to GBK and emoji would
# crash the whole run (and the failure reporter with it).

WINDOW = 131072          # config.yaml:model.context_window (declared)
RATIO = 0.85             # config.yaml:memory.compression.token_threshold
THRESHOLD = int(WINDOW * RATIO)          # 111,411
LOAD_CEILING = 16384                     # balanced tier, manager.py
CHARS_PER_TOKEN = 1.5                    # core.summarizer, measured


class MockProvider(BaseProvider):
    """1 token per character, so the arithmetic is inspectable by hand.

    Subclasses the real `BaseProvider` rather than reimplementing
    `estimate_request_tokens`: this file's whole subject is whether the
    gate's number equals the request's cost, and a hand-copied estimator
    would make the test agree with itself instead of with the code
    (three consumers with three formulas would all look green).
    """

    def __init__(self, context_window: int = WINDOW):
        self._window = context_window
        self.chat_calls = 0

    @property
    def context_window(self) -> int:
        return self._window

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def chat_stream(self, messages, tools=None, system=None, thinking=None):
        raise NotImplementedError("streaming is not exercised here")

    def chat(self, messages, tools=None, system=None, thinking=None):
        self.chat_calls += 1

        class _R:
            content = "compressed summary"
            tool_calls = None
            prompt_tokens = 200
            completion_tokens = 20

            @property
            def total_tokens(self):
                return self.prompt_tokens + self.completion_tokens

        return _R()


def make_conversation(turns: int, chars_per_message: int) -> list[dict]:
    """system + *turns* user/assistant pairs, ~*chars_per_message* each."""
    filler = "关于文档分析与上下文管理的讨论。" * 200
    messages = [{"role": "system", "content": "You are an assistant." * 20}]
    for i in range(turns):
        messages.append({
            "role": "user",
            "content": f"[Turn {i} user] " + filler[:chars_per_message],
        })
        messages.append({
            "role": "assistant",
            "content": f"[Turn {i} assistant] " + filler[:chars_per_message],
        })
    return messages


def _compressor():
    from core.memory.compressor import Compressor
    return Compressor(threshold=RATIO, keep_recent_turns=3)


# ------------------------------------------------------------------
# 1. Positive: a maximal realistic live load DOES trigger
# ------------------------------------------------------------------

def test_maximal_live_load_triggers():
    """构造最大真实负载，断言机制确实触发过（判据 2 的硬要求）。"""
    messages = make_conversation(turns=40, chars_per_message=3000)
    provider = MockProvider()
    total = provider.estimate_message_tokens(messages)

    assert total > THRESHOLD, (
        f"the fixture is not actually a maximal load: {total} <= {THRESHOLD}"
    )

    comp = _compressor()
    assert comp.should_compress(total, WINDOW) is True, (
        f"threshold unreachable at {total} vs {THRESHOLD}"
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out_messages, was_compressed = comp.check_and_compress(
            "sid", messages, provider, WINDOW, session_note=None, mode="auto",
        )

    assert was_compressed is True, (
        "check_and_compress did not compress a list above the threshold"
    )
    assert len(out_messages) < len(messages), (
        f"compression returned {len(out_messages)} >= {len(messages)}"
    )
    print(f"  [ok] {total:,} tokens > {THRESHOLD:,} -> "
          f"{len(messages)} -> {len(out_messages)} messages")


# ------------------------------------------------------------------
# 2. Negative control: the load ceiling alone can NEVER trigger
# ------------------------------------------------------------------

def test_load_ceiling_alone_cannot_trigger():
    """装载上限约束的那份历史，单独装回去不足以触发。

    这条把"装载上限 16,384 压制阈值 111,411"这个主张变成一条断言，
    并明确它只对恢复路径成立: 装载侧的上限算出来的
    字符额度，折算回 token 后远低于阈值。压缩能不能被触发，取决于
    恢复之后活消息表长了多少，不取决于这个 ceiling。

    如果哪天有人把 ceiling 提到阈值之上，这条断言会失败 —— 那正是
    我们想被告知的。
    """
    budget_chars = int(LOAD_CEILING * CHARS_PER_TOKEN)   # 24,576
    provider = MockProvider()
    restored = [{"role": "system", "content": "x" * budget_chars}]
    total = provider.estimate_message_tokens(restored)

    comp = _compressor()
    assert comp.should_compress(total, WINDOW) is False, (
        f"the load ceiling now reaches the threshold ({total} vs "
        f"{THRESHOLD}) -- the two chains have been merged, revisit R1"
    )
    print(f"  [ok] load ceiling alone = {total:,} tokens < {THRESHOLD:,} "
          f"(gap {THRESHOLD - total:,})")


# ------------------------------------------------------------------
# 3. The headroom line carries both numbers on ONE line
# ------------------------------------------------------------------

def test_headroom_log_prints_both_numbers_on_one_line():
    """判据 2: 量纲不同的两个数放在一起比较时，必须在同一行打印出来。

    这行还必须让读者用眼睛验算: 三个部分 + 因子都在同一行，
    `messages + system + tools` 乘因子应当等于「当前」。tools 是
    被闸门漏掉的那一项，这行打的正是"闸门外"的那个数。
    """
    provider = MockProvider()
    comp = _compressor()
    system_text = "S" * 300
    tools = [{
        "type": "function",
        "function": {
            "name": "parse_pdf",
            "description": "解析 PDF 文件并返回正文文本，支持分页。",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    # Far below the threshold -> quiet (it would otherwise print every turn)
    quiet = make_conversation(turns=2, chars_per_message=200)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        comp.check_and_compress(
            "sid", quiet, provider, WINDOW, mode="auto",
            system=system_text, tools=tools,
        )
    assert "[compress]" not in buf.getvalue(), (
        f"headroom logged far below the threshold: {buf.getvalue()!r}"
    )

    # Just below the threshold -> the line appears.  The filler length is
    # binary-searched rather than hardcoded, so the fixture stays in the
    # band if WINDOW, the threshold or a part's cost changes.
    turns = 20
    lo, hi = 1, 20000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        probe = provider.estimate_message_tokens(
            make_conversation(turns=turns, chars_per_message=mid),
            system=system_text, tools=tools,
        )
        if probe <= THRESHOLD:
            lo = mid
        else:
            hi = mid - 1
    near = make_conversation(turns=turns, chars_per_message=lo)
    bd = provider.estimate_request_tokens(
        near, system=system_text, tools=tools
    )
    total = bd["total"]
    assert THRESHOLD * 0.75 <= total <= THRESHOLD, (
        f"fixture landed at {total}, outside the near-threshold band "
        f"[{int(THRESHOLD * 0.75)}, {THRESHOLD}] -- the window is too small "
        f"for {turns} turns to reach the threshold at all"
    )
    # Non-vacuity: every part below must be non-zero, or the substring
    # asserts further down would pass on a line that printed "tools 0".
    # (A negative control caught this: with the tools term removed they
    # still passed.)
    assert bd["messages"] > 0 and bd["system"] > 0 and bd["tools"] > 0, bd

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        comp.check_and_compress(
            "sid", near, provider, WINDOW, mode="auto",
            system=system_text, tools=tools,
        )
    line = buf.getvalue()
    assert "当前" in line and "阈值" in line and "还差" in line, line
    assert f"{total:,}" in line, f"the current value is missing: {line!r}"
    assert f"{THRESHOLD:,}" in line, f"the threshold is missing: {line!r}"
    # All three parts, on that same line, so the sum is checkable by eye.
    assert f"messages {bd['messages']:,}" in line, line
    assert f"system {bd['system']:,}" in line, line
    assert f"tools {bd['tools']:,}" in line, (
        f"the tool-schema cost is missing from the headroom line: {line!r}"
    )
    body = [ln for ln in line.splitlines() if "[compress]" in ln][0]
    print(f"  [ok] {body.strip()}")


# ------------------------------------------------------------------
# 4. The sliding window alone is large enough to cross the threshold
# ------------------------------------------------------------------

class RealisticProvider(MockProvider):
    """The REAL instrument: tiktoken cl100k over Chinese x calibration.

    `MockProvider` counts 1 char = 1 token, which is ~2.2x more permissive
    than cl100k on Chinese (repo's own measurement: 0.86 chars/token), so a
    test built on it says nothing about whether the real gate fires.  This
    one reproduces the real chain:

        estimate_message_tokens = sum(count_tokens(content) + 4) * factor

    with `count_tokens` = len / 0.86 and `factor` read from
    `data/memory/calibration.json` (1084 samples).
    """

    CHARS_PER_TOKEN = 0.86   # cl100k on Chinese -- measured value
    CALIBRATION = 0.9238     # data/memory/calibration.json

    def count_tokens(self, text: str) -> int:
        return int(len(text) / self.CHARS_PER_TOKEN)

    def estimate_message_tokens(self, messages: list[dict]) -> int:
        total = sum(
            self.count_tokens(m.get("content") or "") + 4 for m in messages
        )
        return int(total * self.CALIBRATION)


def test_sliding_window_can_cross_the_threshold():
    """100 条窗口装得下越过阈值所需的量 —— 所以压缩不是死代码。

    The gate compares `estimate_message_tokens` (cl100k x factor) against
    111,411.  The live list is capped at max_history = 100 messages, so
    the question "can it ever fire?" is exactly: does 100 messages worth of
    realistic content get there?

        raw tiktoken needed  = 111,411 / 0.9238 = 120,600
        chars needed         = 120,600 x 0.86   = 103,716
        per message          = 103,716 / 100    = ~1,037 chars

    Tool results are truncated at 6,000 chars and restored history at 2,000,
    so ~1,037 chars/message is ordinary, not extreme.  **"永不触发"这个
    主张被推翻** —— 而它现在是一条断言，不是一句话。
    """
    provider = RealisticProvider()
    comp = _compressor()
    max_history = 100

    crossing = 1037
    under = make_conversation(
        turns=max_history // 2, chars_per_message=crossing - 200
    )[:max_history]
    over = make_conversation(
        turns=max_history // 2, chars_per_message=crossing + 200
    )[:max_history]

    assert len(under) == max_history, len(under)
    under_tokens = provider.estimate_message_tokens(under)
    over_tokens = provider.estimate_message_tokens(over)

    assert comp.should_compress(over_tokens, WINDOW) is True, (
        f"a full {max_history}-message window at ~{crossing} chars/message "
        f"does NOT reach the threshold ({over_tokens:,} vs {THRESHOLD:,}) -- "
        f"compression really is dead code, revisit R1"
    )
    print(f"  [ok] {max_history} msgs x ~{crossing:,} chars -> "
          f"{over_tokens:,} tokens > {THRESHOLD:,}: FIRES")
    print(f"       (X{comp._threshold} of {WINDOW:,}; "
          f"one notch below: {under_tokens:,} "
          f"{'still fires' if comp.should_compress(under_tokens, WINDOW) else 'quiet'})")


# ------------------------------------------------------------------

def main() -> int:
    tests = [
        test_maximal_live_load_triggers,
        test_load_ceiling_alone_cannot_trigger,
        test_headroom_log_prints_both_numbers_on_one_line,
        test_sliding_window_can_cross_the_threshold,
    ]
    print("=" * 60)
    print("compression reachability")
    print(f"  window={WINDOW:,}  ratio={RATIO}  threshold={THRESHOLD:,}")
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
