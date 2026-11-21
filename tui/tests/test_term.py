"""终端能力探测的断言。

这一层最容易"看起来没事、实际把程序弄崩"：它碰的是 ctypes 和 Windows 控制台句柄，
失败点在别人的进程状态里。所以只钉两件事 —— 不抛、给的数能用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import term  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def main() -> int:
    # 1) 不抛。测试跑在管道里（stdout 不是控制台），Windows 上 GetConsoleMode
    #    会失败 —— 那正是"输出被重定向"的正常情形，必须安静地返回 False，
    #    而不是抛出去让程序起不来。
    try:
        ok = term.enable_ansi()
    except Exception as exc:                                 # noqa: BLE001
        raise AssertionError(f"enable_ansi 抛了 {type(exc).__name__}: {exc}")
    check(isinstance(ok, bool), "enable_ansi 要返回 bool")

    # 2) 非 Windows 上 ANSI 本来就有，不该报"不支持"
    if os.name != "nt":
        check(ok is True, "非 Windows 上 enable_ansi 应为 True")

    # 3) 幂等。第二次调用的结果必须和第一次一样 —— 主循环里每次开临时区
    #    都会问一遍，答案变了会出现"第一轮有临时区、第二轮没有"这种怪象。
    check(term.enable_ansi() == ok, "enable_ansi 必须幂等")

    # 4) 宽度兜底：取不到时不能是 0。宽度 0 会让折行函数死循环，
    #    表现是开一轮对话就卡死。
    w = term.terminal_width()
    check(isinstance(w, int) and w >= 20, f"宽度不该小于 20，实际 {w}")

    # 5) 宽度为 0 的极端输入也要被兜住
    old = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "0"
    try:
        w0 = term.terminal_width()
    finally:
        if old is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = old
    check(w0 >= 20, f"COLUMNS=0 时应回落到默认值，实际 {w0}")

    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
