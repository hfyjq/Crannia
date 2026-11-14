# -*- coding: utf-8 -*-
"""`core/context_pressure.py` 的机械读者。

钉住：
1. 水位以下是什么都不做（同一个对象原样返回）。
2. 过 80%，收缩旧工具输出；读数能对上。
3. 提醒只在过线时才挂，且挂在最后一条 user 后面（副本）。
4. 窗口读不到，不猜、不动。
5. 两条循环路径都接了（sync / stream 各一处）。

ASCII-only 输出。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.context_pressure import (  # noqa: E402
    FORCE_RATIO, WARN_RATIO, append_notice, notice_text, ratio_of,
    trim_for_pressure,
)

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


class _P:
    def __init__(self, window):
        self.context_window = window

    def estimate_request_tokens(self, messages, system=None, tools=None):
        n = sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else 0
                for m in messages)
        return {"total": n, "messages": n, "system": 0, "tools": 0}


def _msgs(tools_n=8, size=2000):
    out = [{"role": "user", "content": "go"}]
    for i in range(tools_n):
        out.append({"role": "assistant", "content": ""})
        out.append({"role": "tool", "content": "y" * size})
    return out


def test_ratio_and_constants():
    check("窗口读不到 => -1", ratio_of(10, None) == -1.0, "")
    check("80 / 95 两个水位", (WARN_RATIO, FORCE_RATIO) == (0.80, 0.95),
          "%r %r" % (WARN_RATIO, FORCE_RATIO))
    check("提醒文案非空", bool(notice_text()), "")


def test_below_the_line_does_nothing():
    msgs = _msgs()
    p = _P(1_000_000)                       # 占用远低于 80%
    out, st = trim_for_pressure(msgs, p)
    check("★ 返回**同一个对象**（没复制、没改）", out is msgs, "")
    check("没缩任何东西", st["trimmed"] == 0, repr(st))


def test_above_the_line_trims():
    msgs = _msgs(8, 12_000)                 # 约 96k 字符（旧的 2 条 > 8KiB）
    p = _P(120_000)                         # 占用 ~80%
    out, st = trim_for_pressure(msgs, p)
    check("★ 缩了", st["trimmed"] > 0, repr(st))
    check("★ 占用降了", st["ratio_after"] < st["ratio_before"], repr(st))
    check("入参没被改", len(msgs[2]["content"]) == 12_000,
          str(len(msgs[2]["content"])))
    check("返回的是新列表", out is not msgs, "")


def test_no_window_means_no_guess():
    msgs = _msgs()
    out, st = trim_for_pressure(msgs, _P(None))
    check("窗口读不到 => 什么都不做", out is msgs and st["trimmed"] == 0, repr(st))


def test_notice_is_appended_as_a_copy():
    msgs = _msgs(1, 10)
    out = append_notice(msgs)
    check("★ 原列表没被改", msgs[0]["content"] != out[0]["content"], "")
    check("提醒在最后一条 user 后面",
          any(m.get("role") == "user" and "context_notice" in (m.get("content") or "")
              for m in out), "")


def test_compaction_request_lifecycle():
    from core import context_pressure as cp
    sid = "probe-session"
    cp.forget_session(sid)
    check("没读数 => 不在窗口", not cp.armed(sid), "")
    check("不在窗口时请求 => 不受理", cp.request_compaction(sid) is False, "")
    cp.note_ratio(sid, 0.9)
    check("过了 80% => 进窗口", cp.armed(sid), "")
    check("请求被受理", cp.request_compaction(sid) is True, "")
    check("★ 取一次就清掉（每次请求只消费一次）",
          cp.take_compaction_request(sid) is True and
          cp.take_compaction_request(sid) is False, "")
    cp.forget_session(sid)
    check("/clear 之后什么都不剩", not cp.armed(sid) and
          cp.take_compaction_request(sid) is False, "")


def test_tool_is_session_only_and_windowed():
    """`request_compaction` 只在压力窗口里发给模型。"""
    from bus.core import init_bus
    from bus import registry as R
    from core import context_pressure as cp
    init_bus({})
    check("注册了", "request_compaction" in R.list_tools(), "")
    check("★ 是会话条件工具（不常驻）",
          R.is_session_only_tool("request_compaction"), "")
    cp.forget_session("w1")
    check("窗口外不发", "request_compaction" not in R._session_extra_tools("w1"), "")
    cp.note_ratio("w1", 0.9)
    check("★ 窗口内才发", "request_compaction" in R._session_extra_tools("w1"), "")
    check("权限档 = write（要串行、不许只读子 agent 用）",
          R.get_tool_permission("request_compaction") == "write", "")
    check("不可逆 = False（压缩不删原话）",
          R.get_tool_destructive("request_compaction") is False, "")
    cp.forget_session("w1")


def test_loop_consumes_the_request_and_forces_at_95():
    src = (_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    check("★ 两条路都消费「模型请求」标记",
          src.count("take_compaction_request(session_id)") == 2, "")
    check("★ 两条路都用 FORCE_RATIO 判强压",
          src.count(">= FORCE_RATIO") == 2, "")


def test_both_loop_paths_are_wired():
    src = (_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    check("★ 收缩与提醒在 sync / stream **各一处**",
          src.count("trim_for_pressure(") == 2 and src.count("append_notice(") == 2,
          "trim=%d notice=%d" % (src.count("trim_for_pressure("),
                                 src.count("append_notice(")))


def main() -> int:
    for fn in (test_ratio_and_constants,
               test_below_the_line_does_nothing,
               test_above_the_line_trims,
               test_no_window_means_no_guess,
               test_notice_is_appended_as_a_copy,
               test_compaction_request_lifecycle,
               test_tool_is_session_only_and_windowed,
               test_loop_consumes_the_request_and_forces_at_95,
               test_both_loop_paths_are_wired):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                           # noqa: BLE001
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
