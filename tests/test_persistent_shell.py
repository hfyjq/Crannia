# -*- coding: utf-8 -*-
"""`core/persistent_shell.py` 的机械读者。

钉住：

1. 状态跨调用保留：`export` / `cd` 在同一个会话里活到下一条命令；
2. 超时 = 重置：命令超时，杀进程、下一条命令起新的，且会话内状态丢失
   （回执里说明）；
3. shell 自己退出（`exit`），同样的重置，下一条照跑；
4. 标记不被命令输出骗到：命令打印同样的串不会被当成"这条结束了"；
5. 清点：`close_session` 真的把会话摘掉；
6. 入口互斥：`persistent` 与 `background` 同时给，拒（先于审批）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_persistent_shell.py
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import persistent_shell as PS  # noqa: E402
from tests._sandbox import sandbox  # noqa: E402

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


def test_state_persists():
    with sandbox(["workspace"]) as root:
        wd = root / "workspace"
        (wd / "sub").mkdir()
        sid = "probe-state"
        try:
            r1 = PS.run(sid, "export CRANNIA_PROBE=42", wd, 10000)
            check("export 这条正常结束", r1.get("exit_code") == 0, r1)
            r2 = PS.run(sid, "echo $CRANNIA_PROBE", wd, 10000)
            check("★ export 跨调用保留",
                  "42" in (r2.get("stdout") or ""), r2)
            r3 = PS.run(sid, "cd sub", wd, 10000)
            check("cd 这条正常结束", r3.get("exit_code") == 0, r3)
            r4 = PS.run(sid, "pwd -W 2>/dev/null || pwd", wd, 10000)
            check("★ cd 跨调用保留（回执里的 cwd 跟着变）",
                  str(r4.get("cwd") or "").replace("\\", "/").endswith("/sub"),
                  r4.get("cwd"))
        finally:
            PS.close_session(sid)


def test_exit_code_and_output():
    with sandbox(["workspace"]) as root:
        sid = "probe-rc"
        try:
            r = PS.run(sid, "echo hello; (exit 3)", root / "workspace", 10000)
            check("输出回来了", "hello" in (r.get("stdout") or ""), r)
            check("退出码如实回报", r.get("exit_code") == 3, r)
            check("★ 两个流合并在 stdout 里且标明",
                  r.get("streams_merged") is True and r.get("stderr") == "", r)
        finally:
            PS.close_session(sid)


def test_timeout_resets():
    with sandbox(["workspace"]) as root:
        sid = "probe-timeout"
        wd = root / "workspace"
        try:
            PS.run(sid, "export CRANNIA_PROBE=42", wd, 10000)
            r = PS.run(sid, "sleep 5", wd, 1000)
            check("★ 超时被如实报告", r.get("timed_out") is True, r)
            check("★ 超时后 shell 被重置", r.get("shell_reset") is True, r)
            r2 = PS.run(sid, "echo [$CRANNIA_PROBE]", wd, 10000)
            check("★★ 重置后会话内状态确实没了",
                  "[]" in (r2.get("stdout") or ""), r2)
        finally:
            PS.close_session(sid)


def test_exit_resets():
    with sandbox(["workspace"]) as root:
        sid = "probe-exit"
        wd = root / "workspace"
        try:
            r = PS.run(sid, "exit", wd, 10000)
            check("★ shell 退出被如实报告",
                  r.get("shell_reset") is True and "退出" in str(r.get("note") or ""), r)
            r2 = PS.run(sid, "echo again", wd, 10000)
            check("★ 下一条命令照跑（换了一个 shell）",
                  "again" in (r2.get("stdout") or ""), r2)
        finally:
            PS.close_session(sid)


def test_marker_in_output_is_not_the_end():
    with sandbox(["workspace"]) as root:
        sid = "probe-marker"
        try:
            code = ("printf '\\x01CRANNIA_END\\x01 999\\n'; echo after")
            r = PS.run(sid, code, root / "workspace", 10000)
            check("★★ 命令自己打印同样的串不会被当成结束",
                  "after" in (r.get("stdout") or "")
                  and r.get("exit_code") == 0, r)
        finally:
            PS.close_session(sid)


def test_close_session():
    with sandbox(["workspace"]) as root:
        sid = "probe-close"
        try:
            PS.run(sid, "echo x", root / "workspace", 10000)
            check("起过之后是活的", PS.session_alive(sid) is True, "")
            PS.close_session(sid)
            check("★ 清点把它摘掉", (sid in PS._sessions) is False
                  and PS.session_alive(sid) is False, "")
            PS.close_session(sid)          # 幂等
            check("重复清点不抛", True, "")
        finally:
            PS.close_session(sid)


def test_entry_rejects_background():
    from core import shell as SH

    r = SH.run_command(command="echo x", persistent=True, background=True)
    check("★ persistent + background 被拒",
          r.get("denied") is True and "不能同时用" in str(r.get("error") or ""), r)


def main():
    for fn in (test_state_persists,
               test_exit_code_and_output,
               test_timeout_resets,
               test_exit_resets,
               test_marker_in_output_is_not_the_end,
               test_close_session,
               test_entry_rejects_background):
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
