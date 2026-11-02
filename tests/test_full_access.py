#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「完全权限」—— 第四档模式的行为判据。

    .venv/Scripts/python.exe tests/test_full_access.py

钉住六件事（每条都对应一条既有硬闸或一项行为约束）：

  1. `full_access` 在模式表里、能 `/mode` 设进去，但不能当默认档；
  2. 审批不问而放：命令 / 删除 / 跨区 / 出网 —— 发的是同一张一次性授权
     （`_pre_grant_for_auto`），不是另开一套；
  3. 批量删除仍问一次 —— 完全权限发的单条删除授权越不过批量删除的熔断；
  4. 定时任务不继承（`origin == "cron"`，这一支不成立）；
  5. ICE 信息层派生关闭（退出模式自己恢复）；
  6. 一条源级锚点：设置页不许把 `full_access` 当默认档卖。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from _sandbox import sandbox, prod_snapshot, prod_diff   # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(desc: str, ok: bool, note: str = "") -> None:
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + note) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


_SID = "probe:private:full"


class _FakeWM:
    """只记"发了哪张证"的替身 —— 判据是发的载体对不对，不是真的授权表。"""

    def __init__(self):
        self.approval_scope = "scope-full"
        self.approval_ask_counts: dict = {}
        self.security = None
        self.skip_delete_approval = False
        self.grants: list = []

    def grant_target_once(self, kind, subject, scope):
        self.grants.append((kind, subject, scope))

    def grant_one_time_delete_approval(self, owner, subject):
        self.grants.append(("delete", subject, owner))

    def grant_one_time_approval(self, owner):
        self.grants.append(("cross_zone", "", owner))

    def has_target_grant(self, kind, subject):
        return False


def _with_wm(wm, fn):
    import bus.registry as R
    saved = R.get_workspace_manager
    try:
        R.get_workspace_manager = lambda: wm
        return fn()
    finally:
        R.get_workspace_manager = saved


def main() -> int:
    before = prod_snapshot(_ROOT)

    with sandbox():
        from core import modes as M
        from core.agent_loop import (
            _full_access_active, _full_access_gate, _ice_off_for_session,
            _pre_grant_for_auto,
        )

        M.reset_mode_cache()
        wm = _FakeWM()
        M.set_mode(_SID, "full_access")

        # 1. 模式表与默认档
        print("\n-- 1. mode table --")
        check("full_access is in the table", "full_access" in M.MODE_KEYS, "")
        check("... and is implemented", M.is_implemented("full_access"), "")
        check("... but NOT allowed as a default",
              not M.is_default_allowed("full_access"), "")
        check("other modes ARE allowed as defaults",
              all(M.is_default_allowed(k) for k in
                  ("manual", "auto", "plan")), "")
        check("set_mode accepts it", M.frozen_mode(_SID) == "full_access", "")
        # 文案改成了对外表述（不再用内部词 `deny`），
        #: 判据不变：回执必须说清"哪些仍然硬拒 + 批量删除仍问一次"。
        check("the message says what stays hard-blocked",
              "出网命令" in M.set_mode(_SID, "full_access")
              and "批量删除" in M.set_mode(_SID, "full_access"), "")

        # 2. 不问而放：四种载体
        print("\n-- 2. pre-grant --")
        wm.grants.clear()
        ok = _with_wm(wm, lambda: _full_access_gate(
            _SID, "run_command", {"command": "echo hi"}, origin=""))
        check("command is allowed without asking", ok is True, "")
        check("... with the command carrier",
              any(k == "command" and s == "echo hi" and o == "scope-full"
                  for k, s, o in wm.grants), str(wm.grants))

        wm.grants.clear()
        ok = _with_wm(wm, lambda: _full_access_gate(
            _SID, "delete_files", {"file_paths": ["a.txt"]}, origin=""))
        check("single delete is allowed without asking", ok is True, "")
        check("... with the delete carrier",
              any(k == "delete" and s == "a.txt" for k, s, o in wm.grants),
              str(wm.grants))

        wm.grants.clear()
        ok = _with_wm(wm, lambda: _full_access_gate(
            _SID, "request_cross_zone_search", {"query": "x"}, origin=""))
        check("cross-zone search is allowed without asking", ok is True, "")
        check("... with the cross-zone carrier",
              any(k == "cross_zone" for k, s, o in wm.grants), str(wm.grants))

        wm.grants.clear()
        ok = _with_wm(wm, lambda: _full_access_gate(
            _SID, "web_fetch", {"url": "https://example.com/a"}, origin=""))
        check("egress is allowed without asking", ok is True, "")
        check("... with the host carrier",
              any(k == "egress" and s == "example.com" for k, s, o in wm.grants),
              str(wm.grants))

        # 不弹窗的工具：什么都不发（不是"给它也发一张"）
        wm.grants.clear()
        ok = _with_wm(wm, lambda: _full_access_gate(
            _SID, "read_file", {"path": "x"}, origin=""))
        check("a read-only tool gets no grant at all",
              ok is False and not wm.grants, str(wm.grants))

        # 3. 定时任务不继承
        print("\n-- 3. cron does not inherit --")
        wm.grants.clear()
        check("origin=cron -> not active",
              _with_wm(wm, lambda: _full_access_active(_SID, "cron")) is False, "")
        check("... and no grant is issued",
              _with_wm(wm, lambda: _full_access_gate(
                  _SID, "run_command", {"command": "echo hi"},
                  origin="cron")) is False and not wm.grants, str(wm.grants))

        # 4. ICE 派生关闭
        print("\n-- 4. ICE is derived off --")
        check("full access -> ICE info layer off",
              _ice_off_for_session(_SID) is True, "")
        M.set_mode(_SID, "manual")
        check("leaving the mode restores ICE (derived, not rewritten)",
              _ice_off_for_session(_SID) is False, "")
        #: 逐个档位走一遍：只有 full_access 派生关闭 ICE，来回切换要能恢复。
        for _other in ("auto", "plan"):
            M.set_mode(_SID, _other)
            check("★ %s keeps ICE on (only full_access derives it off)" % _other,
                  _ice_off_for_session(_SID) is False, "")
        M.set_mode(_SID, "full_access")
        check("★ re-entering full_access turns it off again (round trip)",
              _ice_off_for_session(_SID) is True, "")

        # 5. 批量删除仍然问一次（真 handler + 真 WM）
        print("\n-- 5. bulk delete still asks --")
        from core.security import SecurityGuard
        from core.workspace import WorkspaceManager

        (Path("data") / "settings.json").write_text(
            json.dumps({"delete_batch_limit": 1}), encoding="utf-8")
        guard = SecurityGuard(
            allowed_dirs=[str(Path("workspace").resolve())],
            command_blacklist=[],
            audit_log_path=str(Path("logs") / "audit_fa.log"))
        real = WorkspaceManager(guard, {})
        real.approval_scope = _SID
        a = Path("workspace") / "fa_a.txt"
        b = Path("workspace") / "fa_b.txt"
        a.write_text("a", encoding="utf-8")
        b.write_text("b", encoding="utf-8")

        import skills.file_parser.handler as H
        H.init(guard)

        def _run():
            granted = _pre_grant_for_auto(
                "delete_files", {"file_paths": [str(a), str(b)]}, _SID)
            res = H.delete_files(file_paths=[str(a), str(b)])
            return granted, res

        granted, res = _with_wm(real, _run)
        check("the single-delete grant was issued", granted is True, "")
        check("★★ but 2 files > limit 1 => the bulk gate STILL asks",
              isinstance(res, dict)
              and res.get("status") == "pending_delete_approval"
              and res.get("approval_kind") == "delete_bulk", str(res)[:180])

        # 6. 设置页不许卖默认档（源级）
        print("\n-- 6. settings wiring --")
        sv = (_ROOT / "web" / "server.py").read_text(encoding="utf-8")
        check("settings payload carries default_allowed",
              '"default_allowed": m.default_allowed' in sv, "")
        check("POST /api/settings refuses it as a default",
              "is_default_allowed(_mode_key)" in sv, "")
        sview = (_ROOT / "packages" / "ui" / "src" / "settings-view.js").read_text(
            encoding="utf-8")
        check("the settings page filters it out",
              "default_allowed !== false" in sview, "")
        comp = (_ROOT / "packages" / "ui" / "src" / "Composer.jsx").read_text(
            encoding="utf-8")
        #: 红色徽标是噪声，不许回来。
        check("★ 红色徽标已删（用户）",
              "fa-badge" not in comp, "")
        #: 但「保护已降级」那句话没丢 —— 挂在模式 chip 的悬停说明上。
        check("★★ ICE 已关闭的说明还在 —— 挂在模式 chip 的 title 上",
              "ICE 信息层已关闭" in comp
              and 'title={mode === "full_access"' in comp, "")

        M.reset_mode_cache()

    leaked = prod_diff(before, prod_snapshot(_ROOT))
    check("no writes outside the sandbox", not leaked, str(leaked)[:200])

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
