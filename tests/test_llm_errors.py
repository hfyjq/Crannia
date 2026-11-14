#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上游报错的用户面 —— 别让用户看到一行 `error`。

症状：网络断一下，聊天里出现

    (error: Connection error.)

看着像模型说的话；没说"这一轮没产出"，也没说下一步做什么；而 `task.end.ok`
还是 true。这一份钉住三件事：

  1. `explain()` 对每一类都给「发生了什么 / 有没有产出 / 下一步」，
     原文放最后一行；
  2. 流式那条不再把错误当正文：打 `MARK` → 阶段剥掉 → `metadata["error"]`
`task.end.ok=false`；
  3. 三条存量文案逐字保留（401 / 400 / 429）。

[!] 输出一律 ASCII（GBK 控制台）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_llm_errors.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


# ----------------------------------------------------------------------
# 1. 每一类的说法
# ----------------------------------------------------------------------

def test_explain():
    print("\n-- 1. 每一类都说人话 + 下一步 --")
    from core.llm_errors import explain

    msg = explain("Connection error.", kind="connection",
                  model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1")
    check("★★ 连不上：说得出**主机**（用户才知道该看哪条网络/代理）",
          "api.deepseek.com" in msg, msg[:120])
    check("★★ 连不上：说得出**这一轮没有产出**",
          "没有产生任何结果" in msg, msg[:160])
    check("★★ 连不上：给出下一步（重发 / 换模型）",
          "重发" in msg and "端配置" in msg, msg[:240])
    check("★ 原始错误在**最后**（诊断要留，但别当头一句）",
          msg.rstrip().endswith("Connection error."), msg[-80:])

    msg = explain("timed out", kind="timeout")
    check("★ 超时：单独一句（不是「连接」那一套）", "超时" in msg and "没有产生任何结果" in msg,
          msg[:100])

    msg = explain("maximum context length is 65536 tokens", kind="context_overflow")
    check("★ 超长：说得出重试没用、要压缩/拆小",
          "/compact" in msg and "拆小" in msg, msg[:160])

    #: 存量文案：用户在设置页找得到它说的那一格 —— 逐字保留。
    msg = explain("Error code: 401 - invalid api key", kind="auth")
    check("★★ 401 的存量文案逐字保留",
          msg.startswith("api密钥错误，请前往端配置页检查您的api可用性"), msg[:60])
    msg = explain("Error code: 429 - rate limit", kind="rate_limit")
    check("★ 429 说「频率超限，稍后重试」",
          msg.startswith("API请求频率超限，请稍后重试"), msg[:60])

    #: 余额与限流是两件事 —— 说错的那一句会让用户一直等一个不会恢复的东西。
    msg = explain("Error code: 402 - Insufficient Balance")
    check("★★ 402 说的是「余额不足」，不是「频率超限」",
          msg.startswith("API余额不足，请检查账号资金"), msg[:60])
    for raw, head in (
            ("Error code: 429 - insufficient_quota", "API余额不足"),
            ("Error code: 500 - internal", "上游服务器故障"),
            ("Error code: 503 - busy", "上游服务器繁忙"),
            ("Error code: 502 - bad gateway", "上游异常"),
            ("Error code: 529 - overloaded", "上游异常"),
            ("Error code: 422 - bad params", "请求参数异常"),
            ("Error code: 413 - request_too_large", "请求错误：消息过大"),
            ("Error code: 403 - Country, region, or territory not supported",
             "上游异常：当前地区不支持服务"),
            ("Error code: 400 - {'error': {'code': '1301', 'message': '敏感内容'}}",
             "上游拒绝请求：含有非法内容"),
    ):
        got = explain(raw)
        check("★ %r 的开头是那一句" % raw[:28], got.startswith(head), got[:60])

    msg = explain("Error code: 400 - Tool names must be unique")
    check("★★ 400 带上游原文（那次真因是工具重名，不是配置）",
          "Tool names must be unique" in msg and "不一定是配置问题" in msg, msg[:200])

    #: 抠得出来就只说那一句（逐字抄的现场回执）。
    #:   判据：用户看到的不该是一坨 `{'error': {...}}`，而是一句"模型名不对"。
    real = ("Error code: 400 - {'error': {'message': 'The supported API model names "
            "are deepseek-flash, deepseek-v4-pro, but you passed deepseek-v4.1-flash', "
            "'type': 'invalid_request_error', 'param': None, "
            "'code': 'invalid_request_error'}}")
    msg = explain(real)
    check("★★ 模型名填错 -> 一句话点破（含上游支持的名字 + 你填的那个）",
          "模型名不对" in msg and "deepseek-flash" in msg
          and "deepseek-v4.1-flash" in msg, msg[:200])
    check("★★ 而且**不再整段倒 JSON**（`invalid_request_error` 不许出现）",
          "invalid_request_error" not in msg, msg[:200])
    check("★ 短：这条不超过 200 字（今天那条原文是 300+）", len(msg) < 200, str(len(msg)))
    check("★ 给出**能兑现的下一步**（端配置页）", "端配置页" in msg, msg[:200])

    generic = ("Error code: 400 - {'error': {'message': 'messages: role must be one "
               "of user/assistant', 'type': 'invalid_request_error'}}")
    msg2 = explain(generic)
    check("★ 其它结构化的 400 -> 只留 `error.message`",
          "role must be one of" in msg2 and "invalid_request_error" not in msg2,
          msg2[:200])

    msg = explain("boom", stage="AgentLoopStage", exc_name="ValueError")
    check("★ 认不出：说得出**是哪一层**（不装懂）",
          "AgentLoopStage" in msg and "ValueError" in msg, msg[:160])

    #: 不给 kind 也要能认出连接错误（流式那条可能只有字符串）
    msg = explain("Connection error.")
    check("★★ 只给字符串也认得出「连接」（按关键词，与 classifier 同口径）",
          "没能连上模型服务" in msg, msg[:100])


# ----------------------------------------------------------------------
# 2. 标记：进程内一跳，剥干净
# ----------------------------------------------------------------------

def test_mark():
    print("\n-- 2. 标记：只在进程内跳一次 --")
    from core.llm_errors import MARK, explain, is_marked, strip_mark
    from core.pipeline.stages.agent_loop import apply_llm_error_mark

    text = explain("Connection error.", kind="connection")
    marked = MARK + text
    check("★ `is_marked` 认得", is_marked(marked))
    check("★ 普通正文不算", not is_marked("hello"))
    check("★ 剥掉之后**一个字节不剩**（用户看不到标记）",
          strip_mark(marked) == text)

    ctx = SimpleNamespace(response=marked, metadata={})
    ok = apply_llm_error_mark(ctx)
    check("★★ 阶段把标记收掉", ok and not is_marked(ctx.response))
    check("★★ 而且写进 `metadata['error']` -> `task.end.ok=false`",
          ctx.metadata.get("error") == text and ctx.metadata.get("llm_error") is True,
          repr(ctx.metadata))
    check("★ 普通回复不被动", apply_llm_error_mark(
        SimpleNamespace(response="正常回答", metadata={})) is False)

    #: 落库/展示的那一份里不许再出现标记（`NUL` 字符会烂在 SQLite / UI 里）
    check("★★ 标记里有 NUL —— 所以**绝不许**漏到用户面",
          "\x00" in MARK, repr(MARK))


# ----------------------------------------------------------------------
# 3. 流式那条真的打标记
# ----------------------------------------------------------------------

class _ErrProvider:
    _model = "test-model"
    _base_url = "https://api.example.com/v1"

    async def chat_stream(self, messages, tools=None, system=None, thinking=None):
        from providers.base import StreamChunk
        yield StreamChunk(error="Connection error.", error_kind="connection")


def test_stream_marks():
    print("\n-- 3. 流式：错误 chunk -> 人话 + 标记 --")
    from core.agent_loop import agent_loop_stream
    from core.llm_errors import MARK, is_marked

    async def _drain():
        last = None
        async for st in agent_loop_stream("hi", _ErrProvider(), session_id="",
                                          _emit=None):
            last = st
        return last

    state = asyncio.run(_drain())
    content = str(state[0] if state else "")
    check("★★ 结果是**标记 + 人话**（不再是 `(error: …)`）",
          is_marked(content), content[:80])
    check("★★ 人话里说得出「没连上」与「重发」",
          "没能连上模型服务" in content and "重发" in content, content[:200])
    check("★ 旧形状 `(error: ` **不再出现**", "(error: " not in content, content[:80])
    check("★ 标记在**最前面**（阶段靠它认）", content.startswith(MARK))


# ----------------------------------------------------------------------
# 4. 两个 provider 都把类别带出来
# ----------------------------------------------------------------------

def test_providers_carry_kind():
    print("\n-- 4. 两个 provider 都带 `error_kind` --")
    from providers.base import StreamChunk
    check("★ `StreamChunk` 有这一格",
          "error_kind" in getattr(StreamChunk, "__dataclass_fields__", {}))
    oa = (_ROOT / "providers" / "openai_compat.py").read_text(encoding="utf-8")
    an = (_ROOT / "providers" / "anthropic" / "client.py").read_text(encoding="utf-8")
    check("★ OpenAI 兼容那条带上了", "error_kind=kind or transport" in oa)
    check("★ Anthropic 那条也带上了",
          "classify_transport_error(e)" in an and "error_kind=" in an)
    check("★★ 两条都**出声**（控制台此前什么都没有）",
          oa.count("[llm] 流式失败") >= 1 and "[llm] 流式失败" in an)

    from providers.base import classify_transport_error

    class APIConnectionError(Exception):
        pass

    class APITimeoutError(Exception):
        pass

    check("★ 按**异常类型**认连接", classify_transport_error(APIConnectionError("x")) == "connection")
    check("★ 按异常类型认超时", classify_transport_error(APITimeoutError("x")) == "timeout")
    check("★ 认不出回空串（不是瞎猜）", classify_transport_error(ValueError("x")) == "")


def main() -> int:
    print("=" * 62)
    print("llm errors -- user-facing presentation")
    print("=" * 62)
    for fn in (test_explain, test_mark, test_stream_marks,
               test_providers_carry_kind):
        try:
            fn()
        except Exception as exc:                                # noqa: BLE001
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
