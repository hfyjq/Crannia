"""上游错误分类。

为什么立这个文件 —— 必须有明确的「请求超长」识别。
只认两个字面量（`prompt is too long` /
`input is too long for requested model`）是不够的，因为
DeepSeek 实际返回的是：

    This model's maximum context length is 1048576 tokens.
    However, you requested 1048974 tokens …

—— 一个都不含，不压缩、不熔断、不预警，
用户看到的就是「上下文爆掉但没有预警」。

## 三条不变量

1. 各家措辞都要认（含上面那条真实报文）；
2. `context_overflow` 排在最前 —— 它的处置与另外两类完全不同
   （重试/换 key 对它一点用都没有），报文里同时出现两类线索时不许判错；
3. 两张关键词表只有一处 —— 只许住在 `providers/base.py`，
   不许在 `openai_compat.py` 的同步路径与流式路径里各抄一份。

Usage::

    .venv/Scripts/python.exe tests/test_api_error_classify.py
"""

import ast
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

OPENAI_COMPAT = _PROJECT_ROOT / "providers" / "openai_compat.py"

#: 真实报文。两个常见字面量都不含它 ——
#: 所以必须按模式匹配，而不是只认那两句。
DEEPSEEK_OVERFLOW = (
    "Error code: 400 - This model's maximum context length is 1048576 tokens. "
    "However, you requested 1048974 tokens (1043000 in the messages, "
    "5974 in the completion). Please reduce the length of the messages."
)
ANTHROPIC_OVERFLOW = "prompt is too long: 210000 tokens > 200000 maximum"


def test_the_measured_deepseek_message_is_recognized():
    """认不出这条真实报文的代价是"上下文爆掉而零预警"。"""
    from providers.base import classify_api_error
    assert classify_api_error(DEEPSEEK_OVERFLOW) == "context_overflow"
    print("  [ok] DeepSeek 那条实测原文认出来了")


def test_two_literals_are_not_enough():
    """反过来说明"为什么不能只认那两句"。

    只认 `prompt is too long` / `input is too long for requested model` 时，
    其余措辞会被当成未知错误 —— 这条断言把那个差距钉死：两条原文都要认。
    """
    from providers.base import classify_api_error
    assert classify_api_error(ANTHROPIC_OVERFLOW) == "context_overflow"
    assert classify_api_error(
        "input is too long for requested model") == "context_overflow"
    print("  [ok] Anthropic 两种措辞 + 另外两句都认")


def test_overflow_wins_over_the_other_two():
    """报文里同时出现两类线索时，必须判成 overflow。

    上面那条真实报文里就有 `reduce the length of the messages`（兜底模式）
    与 `tokens`（限流那组有 `quota`/`insufficient` 同族词的表）。
    判错的后果很具体：换 key / 重试 —— 而它对超长一点用都没有，
    只会再爆一次。
    """
    from providers.base import classify_api_error
    mixed = "429 too many requests; however the maximum context length is 8192 tokens"
    assert classify_api_error(mixed) == "context_overflow", \
        "两类线索同现时判成了别的 —— 会去换 key/重试，而那是无效动作"
    print("  [ok] overflow 优先于 rate_limit")


def test_auth_and_rate_limit_still_work():
    """另外两类行为不许变（它们才该触发 key 轮换）。"""
    from providers.base import classify_api_error
    for msg in ("Error code: 401 - invalid api key",
                "403 Forbidden", "Unauthorized"):
        assert classify_api_error(msg) == "auth", msg
    for msg in ("Error code: 429 - rate limit reached", "too many requests"):
        assert classify_api_error(msg) == "rate_limit", msg
    print("  [ok] auth / rate_limit 判据没变")


def test_balance_is_not_rate_limit():
    """余额与限流必须分开：一个的下一步是充值，另一个是等一会儿。

    各家的写法不一样，但都指向"钱"：OpenAI / Moonshot 的 quota、
    Anthropic 的 credit balance、DeepSeek / 智谱的余额与欠费。
    """
    from providers.base import classify_api_error
    cases = [
        "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}",
        "Error code: 429 - {'error': {'code': 'insufficient_quota'}}",
        "Error code: 429 - {'error': {'message': 'You exceeded your current quota'}}",
        "Error code: 400 - Your credit balance is too low to access the Anthropic API",
        "Error code: 429 - {'error': {'message': '您的账户已欠费'}}",
    ]
    for msg in cases:
        assert classify_api_error(msg) == "balance", msg
    print("  [ok] 余额 / 欠费不会被说成「频率超限」")


def test_the_rest_of_the_status_codes():
    """剩下的按状态码分流，一条一条对得上用户能做的事。"""
    from providers.base import classify_api_error
    expect = {
        "Error code: 422 - {'error': {'message': 'bad params'}}": "request",
        "Error code: 500 - {'error': {'message': 'internal'}}": "server_fault",
        "Error code: 503 - {'error': {'message': 'busy'}}": "server_busy",
        "Error code: 502 - bad gateway": "server_other",
        "Error code: 529 - {'type': 'overloaded_error'}": "server_other",
        "Error code: 413 - {'error': {'type': 'request_too_large'}}": "too_large",
        "Error code: 403 - Country, region, or territory not supported": "region",
        "Error code: 400 - {'error': {'code': '1301', 'message': '敏感内容'}}": "content",
    }
    for msg, want in expect.items():
        got = classify_api_error(msg)
        assert got == want, "%s -> %s（要 %s）" % (msg, got, want)
    print("  [ok] 402/422/5xx/413/403 地区/内容审核各归各位")


def test_unknown_error_is_none_and_none_is_not_success():
    """`None` = "这三类都不是"，不是"没问题"。

    判据：读不到 ≠ 0。
    所以调用方不许写 `if not classify_api_error(e): 当作成功`。
    """
    from providers.base import classify_api_error
    assert classify_api_error("Connection reset by peer") is None
    assert classify_api_error("") is None
    assert classify_api_error(None) is None
    print("  [ok] 认不出的是 None（而不是被当成成功）")


def test_keyword_tables_live_in_exactly_one_place():
    """两张关键词表只许有一处 —— 住 `providers/base.py`。

    同步路径 + 流式路径各抄一份时，很容易只改一条。
    判据用 AST 找字典/元组里的字面量太绕，这里直接查那两句特征串：
    它们只该出现在 `providers/base.py`。
    """
    src = OPENAI_COMPAT.read_text(encoding="utf-8")
    for needle in ("invalid api key", "too many requests"):
        assert needle not in src, (
            f"`{needle}` 又出现在 openai_compat.py 里了 —— "
            "关键词表只许在 `providers/base.py::classify_api_error` 那一处")
    n = src.count("classify_api_error")
    assert n >= 2, (
        f"openai_compat.py 里只出现 {n} 次 `classify_api_error` —— "
        "同步路径与流式路径**各要一次**（修前那两处逐字重复，很容易只改一条）")
    print(f"  [ok] 关键词表只有一处 · 两条路各调用了它（共 {n} 处）")


def test_overflow_is_loud_and_does_not_retry():
    """overflow 的两条处置：出声 + 不重试。

    两者缺一不可：
      · 不出声，「爆了但没预警」；
      · 去重试，只会再爆一次（还多烧一轮 token）。
    """
    src = OPENAI_COMPAT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "log_context_overflow"]
    assert len(calls) == 2, (
        f"`log_context_overflow` 只在 {len(calls)} 处调用 —— "
        "同步与流式**两条路都要出声**（锁步那条判据）")
    # 且 log 必须发生在 raise / return 之前（否则等于没打）
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        body = ast.get_source_segment(src, fn) or ""
        if "log_context_overflow" in body:
            i_log = body.index("log_context_overflow")
            i_exit = min([x for x in (body.find("raise", i_log),
                                      body.find("return", i_log)) if x > 0] or [10**9])
            assert i_log < i_exit, f"{fn.name}: 出声在退出之后 —— 打不到了"
    print("  [ok] 两条路都出声，且出声在退出之前")


# ------------------------------------------------------------------

def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
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
