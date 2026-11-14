#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""`/compact` 的切点规则。

    .venv/Scripts/python.exe tests/test_compact_turns.py

钉住：轮数少于 `keep_recent_turns` 时，至少压缩到只剩最后一轮；
只有一轮可留时才拒绝。

[!] 输出一律 ASCII（GBK 控制台）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.memory.compressor import Compressor                             # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED = list()


def check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


def _msgs(turns):
    out = []
    for i in range(turns):
        out.append({"role": "user", "content": "u%d" % i})
        out.append({"role": "assistant", "content": "a%d" % i})
    return out


class _StubProvider:
    """只回一句固定摘要 —— 摘要现在一律走模型，夹具必须有个模型。"""

    class _Resp:
        content = "摘要"

    def chat(self, *a, **k):
        return _StubProvider._Resp()


def _run(c, msgs):
    return c._full_compress("s", msgs, _StubProvider(), session_note="note",
                            context_window=10000, usage_total=2000)


def test_two_turns_keep_the_last():
    c = Compressor(keep_recent_turns=3)
    msgs = _msgs(2)
    out, ok = _run(c, msgs)
    check("两轮的会话也能压", ok is True and out is not msgs, len(out))
    check("留下最后一轮", out[-2]["content"] == "u1" and out[-1]["content"] == "a1", out)
    check("开头是一条摘要", out[0].get("kind") == "history_summary", out[0])


def test_many_turns_keeps_configured():
    c = Compressor(keep_recent_turns=3)
    out, ok = _run(c, _msgs(5))
    check("保留 3 轮 + 一条摘要", ok and len(out) == 1 + 3 * 2, len(out))
    check("统计里 kept_turns 与实际一致",
          (c.last_compression or {}).get("kept_turns") == 3, c.last_compression)


def test_single_turn_refuses():
    c = Compressor(keep_recent_turns=3)
    msgs = _msgs(1)
    out, ok = _run(c, msgs)
    check("只有一轮时不压", ok is False and out is msgs, ok)
    check("给出原因", bool(c.last_skip_note), c.last_skip_note)


def main():
    for fn in (test_two_turns_keep_the_last, test_many_turns_keeps_configured,
               test_single_turn_refuses):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
