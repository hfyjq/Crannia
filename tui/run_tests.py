"""跑全部测试。

每个测试文件自己带 `check()` 并返回退出码，所以这里只做两件事：
按名字排序依次跑、汇总结果。不引入测试框架 —— 这个包对外只有两个运行依赖，
测试不该再拖一套进来。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = sorted((HERE / "tests").glob("test_*.py"))


def _decode(raw: bytes) -> str | None:
    """按 UTF-8 解子进程输出。子进程的编码在下面钉死，解不开就是出了意外。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main(tests_dir: Path | str | None = None) -> int:
    """跑一批测试。默认跑本仓 tests/；*tests_dir* 供驱动自身的测试指向临时目录。

    换目录只换"去哪儿找测试"，判定口径、编码处理和汇总格式一律不变。
    """
    tests = (TESTS if tests_dir is None
             else sorted(Path(tests_dir).glob("test_*.py")))
    if not tests:
        print("没有找到测试")
        return 1
    failed = []
    # 子进程统一吐 UTF-8：它默认跟着控制台编码走，父进程按 UTF-8 解就读不出来。
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    for t in tests:
        r = subprocess.run([sys.executable, str(t)], capture_output=True, env=env)
        out = _decode(r.stdout or b"")
        err = _decode(r.stderr or b"")
        lines = (out or "").strip().splitlines()
        summary = lines[-1] if lines else "(无输出)"
        # 输出读不出来不算通过：解不开或干脆没输出，都按失败处理。
        unreadable = out is None or err is None or not lines
        status = "ok " if r.returncode == 0 and not unreadable else "FAIL"
        print(f"[{status}] {t.name:<22} {summary}")
        if r.returncode != 0 or unreadable:
            failed.append(t.name)
            if out is None:
                print("  stdout 不是 UTF-8，读不出来")
            elif err is None:
                print("  stderr 不是 UTF-8，读不出来")
            elif not lines:
                print("  stdout 为空，没有汇总行")
            # 失败时把明细打出来：没有它，这一行"FAIL"等于什么都没说。
            if out is not None:
                print(out[-1500:])
            if err is not None:
                print(err[-800:])
    print()
    if failed:
        print(f"{len(failed)} 个文件失败：{', '.join(failed)}")
        return 1
    print(f"全部通过（{len(tests)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
