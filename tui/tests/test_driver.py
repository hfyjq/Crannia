"""run_tests.py 自己的断言：在临时目录里造假测试，跑驱动，看汇总与退出码。

上一轮补的"读不出输出不许算通过"是真规则，但规则本身没人守着。
这里把四种形态都做成可执行的小文件：正常、退出 1、非 UTF-8、没汇总行。
测试输出仍一律 ASCII。
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import run_tests  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def run_case(name: str, body: str) -> tuple[int, str]:
    """在临时目录里放一个假测试，跑驱动，返回 (退出码, 驱动输出)。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / f"test_{name}.py").write_text(body, encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_tests.main(tests_dir=d)
        return code, buf.getvalue()


def test_good_summary() -> None:
    body = ('import sys\n'
            'print("3 checks: 3 passed, 0 failed")\n'
            'sys.exit(0)\n')
    code, out = run_case("good", body)
    check(code == 0, f"全通过应返回 0，实际 {code}：{out}")
    check("[ok ] test_good.py" in out, f"应有 ok 行：{out}")
    check("3 checks: 3 passed, 0 failed" in out, f"汇总行要照抄：{out}")
    check("全部通过（1 个文件）" in out, f"总数应是 1：{out}")


def test_exit_one() -> None:
    body = ('import sys\n'
            'print("1 checks: 0 passed, 1 failed")\n'
            'sys.exit(1)\n')
    code, out = run_case("badexit", body)
    check(code == 1, f"子进程退出 1 应带红整轮，实际 {code}：{out}")
    check("[FAIL] test_badexit.py" in out, f"应有 FAIL 行：{out}")
    check("1 个文件失败" in out, f"应有失败汇总：{out}")


def test_non_utf8_is_failure() -> None:
    # 直接写 buffer：子进程即使声明了 UTF-8 也能吐出非法字节。
    body = ('import sys\n'
            'sys.stdout.buffer.write(b"\\xff\\xfe bad bytes\\n")\n'
            'sys.stdout.flush()\n'
            'sys.exit(0)\n')
    code, out = run_case("badbytes", body)
    check(code == 1, f"解不开的输出不许算通过，实际 {code}：{out}")
    check("[FAIL] test_badbytes.py" in out, f"应有 FAIL 行：{out}")
    check("stdout 不是 UTF-8" in out, f"要说清失败原因：{out}")


def test_no_summary_is_failure() -> None:
    body = 'import sys\nsys.exit(0)\n'
    code, out = run_case("silent", body)
    check(code == 1, f"没有汇总行不许算通过，实际 {code}：{out}")
    check("[FAIL] test_silent.py" in out, f"应有 FAIL 行：{out}")
    check("stdout 为空，没有汇总行" in out, f"要说清失败原因：{out}")


def test_default_dir_unchanged() -> None:
    check(run_tests.TESTS and run_tests.TESTS[0].parent == HERE / "tests",
          "默认仍应指向本仓 tests/")
    check(all(p.name.startswith("test_") for p in run_tests.TESTS),
          "默认仍只收 test_*.py")


def main() -> int:
    test_good_summary()
    test_exit_one()
    test_non_utf8_is_failure()
    test_no_summary_is_failure()
    test_default_dir_unchanged()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
