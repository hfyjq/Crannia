# -*- coding: utf-8 -*-
"""shell 快照的机械读者。

钉住：

1. 只拍四样：函数、别名、选项、PATH —— 导出变量不进去（密钥不上盘）。
2. 命令真的能用快照里的函数和别名（别名靠 `eval` 的第二遍解析）。
3. 失败退回原状：bash 不存在 / 快照过大，返回不可用，命令仍可执行。
4. `_wrap` 没给快照时逐字保持旧形状（不引入 source/eval）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_shell_snapshot.py
"""

import os
import tempfile
import time
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import shell as S  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


class _Home:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name)
        (self.path / ".bashrc").write_text(
            "myfn() { echo from-fn; }\n"
            "alias myal='echo from-alias'\n"
            "export SUPERSECRET=abc123\n",
            encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.tmp.cleanup()


def test_snapshot_contents():
    with _Home() as home:
        dest = home.path / "snap.sh"
        ok, why = S._write_snapshot(S.bash_path(), dest, home=str(home.path))
        check("生成成功", ok, why)
        text = dest.read_text(encoding="utf-8", errors="replace")
        check("函数进去了", "myfn" in text and "from-fn" in text)
        check("别名进去了", "myal" in text and "from-alias" in text)
        check("PATH 进去了", "export PATH=" in text)
        check("★ 导出变量**没有**进去",
              "SUPERSECRET" not in text and "abc123" not in text)


def test_commands_see_snapshot():
    with _Home() as home:
        dest = home.path / "snap.sh"
        ok, why = S._write_snapshot(S.bash_path(), dest, home=str(home.path))
        assert ok, why
        old_done, old_cache = S._snapshot_done, S._snapshot_cache
        S._snapshot_done, S._snapshot_cache = True, str(dest)
        try:
            r = S._run("myfn", home.path, 5000)
            check("函数可用", (r.get("stdout") or "").strip() == "from-fn",
                  repr(r))
            r = S._run("myal", home.path, 5000)
            check("★ 别名可用（eval 的第二遍解析）",
                  (r.get("stdout") or "").strip() == "from-alias", repr(r))
            r = S._run("echo x$SUPERSECRET", home.path, 5000)
            check("★ 密钥没有进命令环境",
                  (r.get("stdout") or "").strip() == "x", repr(r))
        finally:
            S._snapshot_done, S._snapshot_cache = old_done, old_cache


def test_wrap_shape():
    plain = S._wrap("echo hi")
    check("没快照：不出现 source", "source " not in plain)
    check("没快照：不出现 eval", "eval " not in plain)
    check("尾巴仍在", "__crannia_rc" in plain and "pwd -W" in plain)
    with_snap = S._wrap("echo hi", "/tmp/x.sh")
    check("有快照：先 source 再 eval",
          with_snap.startswith("source ") and "\neval " in with_snap)
    check("有快照：尾巴仍在", "__crannia_rc" in with_snap)


def test_snapshot_is_per_process():
    """快照文件按进程分开 —— 共用一个名字时，谁先退出谁把别人的删掉。

    现场：应用在跑（快照已生成），并行跑一个会执行命令的测试，它覆盖并删除
    那份文件，应用的后续命令静默丢掉函数与别名。
    """
    check("文件名带 PID", str(os.getpid()) in S._SNAPSHOT_DEST.name,
          S._SNAPSHOT_DEST.name)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        old_dest = S._SNAPSHOT_DEST
        try:
            S._SNAPSHOT_DEST = Path(tmp) / "shell_snapshot_99999.sh"
            stale = Path(tmp) / "shell_snapshot_11111.sh"
            fresh = Path(tmp) / "shell_snapshot_22222.sh"
            stale.write_text("# stale\n", encoding="utf-8")
            fresh.write_text("# fresh\n", encoding="utf-8")
            past = time.time() - 2 * 86400
            os.utime(stale, (past, past))
            S._sweep_stale_snapshots()
            check("★ 过期的别人的快照被清掉", not stale.exists())
            check("新的别人的快照留着", fresh.exists())
            check("自己的那份不碰", not S._SNAPSHOT_DEST.exists())
        finally:
            S._SNAPSHOT_DEST = old_dest


def test_cleanup_only_deletes_what_this_process_wrote():
    """收尾删的是本进程写出的那一份，不是"当前目录下的同名文件"。

    现场：测试进程在临时目录里生成快照（沙箱），退出时工作目录已还原成仓库根
再算一次相对路径，删掉的是正在运行的那份生产快照。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as a, \
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as b:
        old_cwd = os.getcwd()
        old_file = S._snapshot_file
        old_cache = S._snapshot_cache
        old_done = S._snapshot_done
        try:
            mine = Path(a) / "shell_snapshot_1.sh"
            mine.write_text("# mine\n", encoding="utf-8")
            other = Path(b) / "shell_snapshot_1.sh"
            other.write_text("# someone else\n", encoding="utf-8")

            S._snapshot_file = str(mine)
            os.chdir(b)
            S._cleanup_snapshot()

            check("★ 自己那份被删掉", not mine.exists())
            check("★ 同名但属于别人的那份不动", other.exists())
        finally:
            os.chdir(old_cwd)
            S._snapshot_file = old_file
            S._snapshot_cache = old_cache
            S._snapshot_done = old_done


def test_cwd_marker_is_pure_ascii():
    """尾巴里只许有 ASCII —— 它和命令输出走同一个流。

    路径里一有中文，那几段 UTF-8 就混进了一个可能是 GBK 的流
整段被判成 cp1252，命令的中文输出全部乱码（现场复现过）。
    """
    check("★ 尾巴全是 ASCII",
          S._wrap("echo hi").isascii() and S._wrap("echo hi", "/tmp/x.sh").isascii(),
          repr(S._wrap("echo hi"))[-80:])

    path = "D:/x/\u526f\u672c"
    hexs = path.encode("utf-8").hex()
    out, cwd = S._split_cwd("hello\n" + S._CWD_MARK + hexs + "\n", "FB")
    check("十六进制还原成路径", cwd == path, cwd)
    check("剩下的 stdout 干净", out == "hello", repr(out))
    check("坏十六进制回退", S._split_cwd(S._CWD_MARK + "zz\n", "FB")[1] == "FB")
    check("奇数长度回退", S._split_cwd(S._CWD_MARK + "abc\n", "FB")[1] == "FB")
    check("空值回退", S._split_cwd(S._CWD_MARK + "\n", "FB")[1] == "FB")


def test_oversize_is_discarded():
    with _Home() as home:
        old = S._SNAPSHOT_MAX_BYTES
        S._SNAPSHOT_MAX_BYTES = 100
        try:
            dest = home.path / "snap.sh"
            ok, why = S._write_snapshot(S.bash_path(), dest, home=str(home.path))
        finally:
            S._SNAPSHOT_MAX_BYTES = old
        check("超过上限：返回不可用", ok is False and "超过" in why, why)
        check("★ 超过上限：文件被删掉（不留半截脚本）", not dest.exists())


def test_bad_bash_returns_failure():
    with _Home() as home:
        ok, why = S._write_snapshot(str(home.path / "no-such-bash"), home.path / "s.sh")
        check("起不来：返回不可用而不是抛异常", ok is False, why)


def main():
    for fn in (test_snapshot_contents,
               test_commands_see_snapshot,
               test_wrap_shape,
               test_snapshot_is_per_process,
               test_cleanup_only_deletes_what_this_process_wrote,
               test_cwd_marker_is_pure_ascii,
               test_oversize_is_discarded,
               test_bad_bash_returns_failure):
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
