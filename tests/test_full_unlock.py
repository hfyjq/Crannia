# -*- coding: utf-8 -*-
"""权限解禁（`core/unlock.py`）—— 完全权限下把剩下的那几道阻力也拿掉。

钉住五件事：

1. 只认真布尔：`"true"` / `1` 一律当没写（与其余布尔配置同一条）；
2. 只在完全权限下生效（别的模式打开这个开关 = 什么都不变）；
3. `pin()` 之后执行层问得到的那个上下文是 `True`，`reset()` 之后是 `False`；
4. 受保护路径（`.env` 一族）解禁前 `FORBIDDEN`、解禁后当普通路径；
5. `deny` 档命令解禁前拒绝、解禁后不再走那条拒绝分支。

跑法：
    .venv/Scripts/python.exe tests/test_full_unlock.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import sandbox  # noqa: E402

import core.unlock as U  # noqa: E402

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


def _write(data: dict) -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/settings.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_only_real_bool_counts():
    with sandbox():
        _write({U.SETTING_KEY: True})
        check("true -> 开", U.enabled() is True)
        for bad in ("true", 1, "1", [1], {"on": True}):
            _write({U.SETTING_KEY: bad})
            check("%r -> 当没写（关）" % (bad,), U.enabled() is False)
        _write({})
        check("没有这个键 -> 关", U.enabled() is False)


def test_only_in_full_access():
    with sandbox():
        from core import modes as M
        _write({U.SETTING_KEY: True})
        for mode in ("manual", "auto", "plan"):
            sid = "unlock-" + mode
            M.set_mode(sid, mode)
            check("%s 档 -> 不生效" % mode, U.active(sid) is False)
        sid = "unlock-full"
        M.set_mode(sid, "full_access")
        check("★★ full_access 档 -> 生效", U.active(sid) is True)
        _write({U.SETTING_KEY: False})
        check("开关关着 -> 即使 full_access 也不生效", U.active(sid) is False)


def test_pin_and_reset():
    with sandbox():
        from core import modes as M
        sid = "unlock-pin"
        M.set_mode(sid, "full_access")
        _write({U.SETTING_KEY: True})
        U.reset()
        check("默认不 bypass（fail-closed）", U.bypassing() is False)
        check("pin() 返回开", U.pin(sid) is True)
        check("★ pin 之后执行层问得到 True", U.bypassing() is True)
        U.reset()
        check("reset 之后回 False", U.bypassing() is False)
        _write({U.SETTING_KEY: False})
        check("开关关着时 pin 不打开", U.pin(sid) is False and U.bypassing() is False)


def _guard(tmp: Path):
    from core.security import SecurityGuard
    return SecurityGuard(
        allowed_dirs=["./workspace"], command_blacklist=[],
        audit_log_path=str(tmp / "audit.log"),
        user_home=str(tmp / "fakehome"))


def test_forbidden_paths_open_up():
    with sandbox() as tmp:
        (tmp / "fakehome").mkdir(exist_ok=True)
        guard = _guard(tmp)
        from core.security import PathLevel
        _, lv = guard.classify_path(str(tmp / ".env"))
        check("解禁前：`.env` 是 FORBIDDEN", lv == PathLevel.FORBIDDEN, str(lv))
        from core import modes as M
        sid = "unlock-path"
        M.set_mode(sid, "full_access")
        _write({U.SETTING_KEY: True})
        U.pin(sid)
        try:
            _, lv2 = guard.classify_path(str(tmp / ".env"))
            check("★★ 解禁后：`.env` 当普通路径（可读写）",
                  lv2 == PathLevel.READ_WRITE, str(lv2))
        finally:
            U.reset()
        _, lv3 = guard.classify_path(str(tmp / ".env"))
        check("reset 之后又关了（不是永久改状态）",
              lv3 == PathLevel.FORBIDDEN, str(lv3))


def test_deny_tier_command_open_up():
    with sandbox() as tmp:
        (tmp / "fakehome").mkdir(exist_ok=True)
        (tmp / "workspace").mkdir(exist_ok=True)
        from bus.registry import register_workspace_manager, get_workspace_manager
        from core.workspace import WorkspaceManager
        guard = _guard(tmp)
        wm = WorkspaceManager(guard, {})
        old = get_workspace_manager()
        register_workspace_manager(wm)
        try:
            from core import shell, modes as M
            res = shell.run_command("ssh -V")
            check("解禁前：deny 档被拒",
                  res.get("denied") is True and res.get("tier") == "deny", str(res)[:200])
            sid = "unlock-cmd"
            M.set_mode(sid, "full_access")
            _write({U.SETTING_KEY: True})
            U.pin(sid)
            try:
                res2 = shell.run_command("ssh -V")
                check("★★ 解禁后：不再走那条 deny 拒绝分支",
                      res2.get("tier") != "deny", str(res2)[:200])
            finally:
                U.reset()
        finally:
            register_workspace_manager(old)


def main() -> int:
    for fn in (test_only_real_bool_counts,
               test_only_in_full_access,
               test_pin_and_reset,
               test_forbidden_paths_open_up,
               test_deny_tier_command_open_up):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                        # noqa: BLE001
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
