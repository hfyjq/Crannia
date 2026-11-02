#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""出网的第三、第四半：预批准表 + 用户点名。

    .venv/Scripts/python.exe tests/test_egress_preapproved.py

## 它治什么

实测：自动模式下让它读一份政府周报，`web_fetch` 每次都弹窗 ——
因为出网那一闸只认"会话级 / 一次性 / 用户设置页加的持久白名单 / 通配"
每一个从没见过的域名都要问一次。

设计理念：只允许访问用户以某种形式提供过的域名
两半：我们预置的表 + 用户当场点名的。

## 三条判据（每条都在下面有正反两面）

1. 预批准表：命中不问。 `www.` 不剥（有意这样做）—— 这是已知副作用，
   有断言钉着，不是漏了。
2. 用户点名：写法最大化（`stats.gov.cn` / `www.stats.gov.cn` 两侧都认），
   但按边界匹配，`xstats.gov.cn` 与 `stats.gov.cn.evil.com` 都不算。
3. 判据自己也要能红 —— 恒真的断言不是护栏。
   所以每个"该 False"的形状都配一条同形的"该 True"做对照。

[!] 输出一律 ASCII（GBK 控制台；GBK 棘轮）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from _sandbox import prod_diff, prod_snapshot, sandbox                      # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(desc: str, ok: bool, note: str = "") -> None:
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + str(note)) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


def _pp():
    from core import egress_preapproved as M
    return M


def _eg():
    from core import egress_gate as G
    return G


def _fake_wm(**kw):
    """一个假 wm —— 只满足 `is_granted` 要问的那几个属性。

    它不是真 `WorkspaceManager`；三层里 ① 的真行为由
    `tests/test_egress_gate.py` 覆盖，这里问的是"③ 有没有接上"。
    """
    class W:
        turn_user_message = ""
        _approval_scope = "s:probe"

        def has_target_grant(self, kind, subject, **k):
            return False
    w = W()
    for k, v in kw.items():
        setattr(w, k, v)
    return w


def _granted(host, path="/", **wmkw):
    """把假 wm 装上，跑一次 `is_granted`。"""
    import bus.registry as R
    saved = R.get_workspace_manager
    R.get_workspace_manager = lambda: _fake_wm(**wmkw)
    try:
        return _eg().is_granted(host, path)
    finally:
        R.get_workspace_manager = saved


# ----------------------------------------------------------------------
# 1. 表的形状
# ----------------------------------------------------------------------

def test_the_table_is_well_formed():
    print("\n-- 1. 表的形状（两组都在、无重复、条目像域名）--")
    M = _pp()
    check("常量是 tuple（照 `PROTECTED_PATHS` 的风格）",
          isinstance(M.PREAPPROVED_HOSTS, tuple), type(M.PREAPPROVED_HOSTS).__name__)
    check("★ 自证：表**非空**（空表会让下面每一条都恒真）",
          len(M.PREAPPROVED_HOSTS) > 20, "len=%d" % len(M.PREAPPROVED_HOSTS))
    check("★ 第一组（编程/文档站）在",
          "docs.python.org" in M.PREAPPROVED_HOSTS
          and "developer.mozilla.org" in M.PREAPPROVED_HOSTS)
    check("★ 第二组（我们自己的官方站）在",
          "www.gov.cn" in M.PREAPPROVED_HOSTS
          and "zh.wikipedia.org" in M.PREAPPROVED_HOSTS)
    check("★ 无重复（重复 = 一次没人复核的粘贴）",
          len(M.PREAPPROVED_HOSTS) == len(set(M.PREAPPROVED_HOSTS)),
          "%d vs %d" % (len(M.PREAPPROVED_HOSTS), len(set(M.PREAPPROVED_HOSTS))))
    bad = [e for e in M.PREAPPROVED_HOSTS
           if "/" in e or " " in e or e != e.strip().lower()]
    check("★ 每条都是**裸 host**（带路径的走 `PATH_SCOPED`；全小写、无空格）",
          not bad, repr(bad[:5]))
    check("带路径那几条的 host 段不是整站（`github.com` 整站太宽）",
          M.PATH_SCOPED and all(M.host_of_entry(e) != e for e in M.PATH_SCOPED))


# ----------------------------------------------------------------------
# 2. 预批准表：命中与不命中（正反成对）
# ----------------------------------------------------------------------

def test_is_preapproved_boundaries():
    print("\n-- 2. `is_preapproved`：命中，以及**三个不该命中的同形** --")
    f = _pp().is_preapproved
    check("在表里 -> True", f("docs.python.org") is True)
    check("★★ 负对照①：**子域不命中的那一侧** —— `www.docs.python.org` -> False"
          "（`www.` 刻意不剥，表里该写 `www.` 的就逐条写）",
          f("www.docs.python.org") is False)
    check("★★ 负对照②：**表里没有的 -> False**（不是「有 host 就放」）",
          f("evil.example.com") is False)
    check("★ 空 host -> False（fail-closed）", f("") is False)
    check("★ 大小写不敏感（DNS 本来就不敏感）",
          f("DOCS.Python.ORG") is True)


def test_path_scoped_entries_need_a_path_boundary():
    print("\n-- 3. 带路径的条目：**路径段边界**（判据）--")
    f = _pp().is_preapproved
    check("`vercel.com/docs/...` -> True", f("vercel.com", "/docs/x") is True)
    check("`vercel.com/docs`（精确相等）-> True",
          f("vercel.com", "/docs") is True)
    check("★★ 负对照：`/docs-evil` **不许**命中"
          "（不然就是前缀匹配，不是段边界）",
          f("vercel.com", "/docs-evil") is False)
    check("★★ 负对照：`vercel.com` **根路径**不许命中（整站太宽）",
          f("vercel.com", "/") is False)
    check("★ 没给 path 时默认 `/` -> 只有裸 host 条目能过",
          f("docs.python.org") is True and f("github.com") is False)


# ----------------------------------------------------------------------
# 4. 预批准表接进 `is_granted`（三层）
# ----------------------------------------------------------------------

def test_the_preapproved_table_is_wired_into_is_granted():
    print("\n-- 4. 预批准表真的接进 `is_granted` 了（不是写了个没人调的表）--")
    check("★★ ③ 预批准表命中 -> 放行（① 什么都没批过）",
          _granted("docs.python.org") is True)
    check("★★ ① 没批、③ 不在表 -> 照问（fail-closed）",
          _granted("evil.example.com") is False)
    check("★ 用户在消息里提到完整地址也照样问（提到不等于授权）",
          _granted("stats.gov.cn",
                   turn_user_message="看下 https://stats.gov.cn/周报") is False)
    check("★ 带路径的条目走的是 path（根路径不放行）",
          _granted("vercel.com", "/") is False
          and _granted("vercel.com", "/docs/x") is True)


# ----------------------------------------------------------------------
# 6. 选项 6「信任此站点」—— 唯一一处从弹窗写盘的档
# ----------------------------------------------------------------------

def _settle(key, kind, tmp: Path, subject="stats.gov.cn"):
    """把 `_settle_egress` 真跑一次（落盘落到 tmp，不碰生产 `data/`）。"""
    import types
    import bus.registry as R
    from core import allowlist as WL
    from core.security import SecurityGuard
    from core.workspace import WorkspaceManager
    from core.pipeline.stages import pre_process as PP

    guard = SecurityGuard(allowed_dirs=[str(tmp)], command_blacklist=[],
                          audit_log_path=str(tmp / "audit.log"))
    wm = WorkspaceManager(guard, {})
    wm._approval_scope = "s:probe"
    saved_g, saved_p = R.get_workspace_manager, WL._PATH
    R.get_workspace_manager = lambda: wm
    WL._PATH = tmp / "allowlist.json"
    try:
        ctx = types.SimpleNamespace(
            user_message="", approval_replay=None, metadata={}, response=None,
            session_target_grants={}, session_key="s:probe", storage_id="s:probe",
            event=None, config={},
        )
        pend = {"subject": subject, "rule": "", "request_id": "r1"}
        fn = PP._SETTLERS.get(kind)
        fn(key, pend, ctx)
        return WL.list_for("egress")
    finally:
        R.get_workspace_manager, WL._PATH = saved_g, saved_p


def test_option_6_writes_the_persistent_allowlist():
    print("\n-- 6. 选项 6「信任此站点」：落盘，而且**只给 egress** --")
    from core.approval import EGRESS_OPTIONS, GRANT_PERSIST
    opt6 = [o for o in EGRESS_OPTIONS if o.key == "6"]
    check("EGRESS_OPTIONS 里有键位 6", len(opt6) == 1,
          repr([o.key for o in EGRESS_OPTIONS]))
    check("它的 grant 是**持久档**", bool(opt6) and opt6[0].grant == GRANT_PERSIST,
          repr(opt6[0].grant) if opt6 else "")
    check("★★ **第一次问就给**（`escalate_at == 0`）—— 用户明示"
          "「就第一次，这个和其他命令不一样」",
          bool(opt6) and opt6[0].escalate_at == 0,
          repr(opt6[0].escalate_at) if opt6 else "")
    check("★★ 负对照：键位 `3` **还是**「不同意」（**没有为它重排** —— 重排会让老习惯点错）",
          [o.label for o in EGRESS_OPTIONS if o.key == "3"] == ["不同意"],
          repr([o.label for o in EGRESS_OPTIONS if o.key == "3"]))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        got = _settle("6", "egress", tmp)
        check("★★ 结算之后它**真的在白名单里了**（这就是「以后不再询问」）",
              got == ["stats.gov.cn"], repr(got))
        #: 负对照：换成别的 kind —— 今天的选项表里只有 egress 挂了 6
        #:别处收到它是配置错误，必须什么都不记（fail-closed）。
        got2 = _settle("6", "destructive", tmp)
        check("★★ 负对照：别的 kind 收到 6 -> **一个字节都不写**（fail-closed）",
              got2 == ["stats.gov.cn"], repr(got2))


# ----------------------------------------------------------------------
# 7. 让自动模式碰得到出网
# ----------------------------------------------------------------------

def _boot_min():
    """只装跑这几条要用的：文件原语（给 `read_file`）+ MCP 内置（给 `web_fetch`）。"""
    import bus.registry as R
    from bus.loaders import skill_loader  # noqa: F401  (确保注册表就绪)
    from mcpx.servers import web_search
    web_search.register_tools()
    try:
        R.register_file_tools()
    except Exception:                                  # noqa: BLE001
        pass
    return R


def test_the_review_can_see_the_egress_tools():
    print("\n-- 7. ★★ 第③层「只读跳过」**不再跳掉会弹窗的那些** --")
    R = _boot_min()
    from core.auto_review import SKIP_READONLY, skip_reason
    for t, args in (("web_fetch", {"url": "https://example.com/x"}),):
        check("★★ `%s` **不**被「只读」跳过（它有自己的审批发射点）" % t,
              skip_reason(t, args, session_id="s1") != SKIP_READONLY,
              repr(skip_reason(t, args, session_id="s1")))
    #: 负对照 —— 少了它，返回"一律不跳过"也能全绿。
    check("★★ 负对照：**真的只读**的（`read_file`）**照旧跳过**（一格没变）",
          skip_reason("read_file", {"file_path": "x"}, session_id="s1") == SKIP_READONLY,
          repr(skip_reason("read_file", {"file_path": "x"}, session_id="s1")))
    check("★ 负对照：`web_search` 同样跳过（它**没有**审批发射点，进去只会被无谓地拒）",
          skip_reason("web_search", {"query": "x"}, session_id="s1") == SKIP_READONLY, "")


def test_pre_grant_actually_lands_on_the_egress_carrier():
    print("\n-- 8. ★★ 判了「安全」之后，授权**真的发得出去**（那条警告）--")
    import tempfile
    _boot_min()
    import bus.registry as R
    from core.agent_loop import _pre_grant_for_auto
    from core.egress_gate import host_of
    from core.security import SecurityGuard
    from core.workspace import WorkspaceManager
    with tempfile.TemporaryDirectory() as td:
        g = SecurityGuard(allowed_dirs=[td], command_blacklist=[],
                          audit_log_path=str(Path(td) / "a.log"))
        wm = WorkspaceManager(g, {})
        wm._approval_scope = "s:probe"
        saved = R.get_workspace_manager
        R.get_workspace_manager = lambda: wm
        try:
            for t in ("web_fetch",):
                args = {"url": "https://example.com/x"}
                ok = _pre_grant_for_auto(t, args, "s:probe")
                check("★★ `%s` 预授权返回 True，**且闸查得到**" % t,
                      ok is True and wm.has_target_grant(
                          "egress", host_of(args["url"])) is True, repr(ok))
        finally:
            R.get_workspace_manager = saved


def test_egress_denials_do_not_trip_the_breaker():
    print("\n-- 9. ★★ 出网那一类的拦截**不计入熔断**（用户）--")
    _boot_min()
    from core import auto_review as AC
    from core.agent_loop import _auto_review_gate

    saved_cls, saved_mode = AC.classify, None
    try:
        #: 要打的是 `current_mode` —— `_auto_review_gate` 逐字 `from core.modes
        #:   import current_mode`（不是 `frozen_mode`）。打错函数时，
        #:   闸第一行就 return 了，而那条负对照照样绿不了 ——
        #:   它报的是 `(0,0) -> (0,0)`，看起来像"守卫生效了"，其实是"闸没跑"。
        #:   （"没红"与"没跑到"长得一模一样。）
        from core import modes
        saved_mode = modes.current_mode
        modes.current_mode = lambda sid="": "auto"
        #: 分类器一律判「拦」—— 我们只问"这一次拦有没有推进熔断计数"。
        AC.classify = lambda provider, transcript, action: AC.Verdict(
            allowed=False, rule="probe", reason="探针")
        for tool, args in (("web_fetch", {"url": "https://example.com/x"}),
                           ("run_command", {"command": "echo hi"})):
            AC.reset_total(AC.denial_state("s:probe"))
            AC.denial_state("s:probe").consecutive = 0
            st = AC.denial_state("s:probe")
            before = (st.consecutive, st.total)
            _auto_review_gate("s:probe", tool, args, [], None)
            after = (st.consecutive, st.total)
            if tool == "web_fetch":
                check("★★ 出网被拦 -> 熔断计数**一动不动**（%s -> %s）"
                      % (before, after), before == after, repr((before, after)))
            else:
                #: 负对照 —— 少了它，"什么都不计数"也能全绿。
                check("★★ 负对照：**别的**工具被拦 -> 计数**照常 +1**（%s -> %s）"
                      % (before, after),
                      after == (before[0] + 1, before[1] + 1), repr((before, after)))
    finally:
        AC.classify = saved_cls
        if saved_mode is not None:
            from core import modes as _m
            _m.current_mode = saved_mode

    from core.approval_timeout import DEFAULT_SECONDS
    check("★ 超时改成 **120 秒**（用户「改成两分钟等待」）",
          DEFAULT_SECONDS == 120, repr(DEFAULT_SECONDS))


# ----------------------------------------------------------------------

def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    before = prod_snapshot()
    with sandbox():
        for _name, fn in tests:
            try:
                fn()
            except AssertionError as exc:
                check(_name, False, "AssertionError: %s" % exc)
            except Exception as exc:                                        # noqa: BLE001
                check(_name, False, "%s: %s" % (type(exc).__name__, exc))
    leaked = prod_diff(before, prod_snapshot())
    check("** 全程生产目录一个字节没动", not leaked, repr(leaked)[:200])
    print("\n== summary ==")
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    for f in _FAILED:
        print("  - %s" % f)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
