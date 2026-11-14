#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP 管理 —— 停用名单 · 归属 · 那条端点。

    .venv/Scripts/python.exe tests/test_mcp_servers.py

## 它治什么

内置 MCP 需要一个自己的归属，否则停不掉：

  · `mcpx/servers/web_search.py` 那两处 `register_tool` 不传 owner
默认 `framework`；
  · 而 `unregister_owner` / 按归属过滤的路子都明确拒摘 `framework`
    （`bus/registry.py` 那条"摘掉等于把框架拆了"）。

给内置 MCP 一个自己的归属（`mcp:<服务器名>`），停用名单按归属走。

## 这一条最要紧的两处

1. 两个集合分得开：`_QUARANTINED`（框架判的"坏了"）与
   `_MCP_DISABLED`（用户点的"我不想让它跑"）。合并的话，
   「解封」会顺手把用户停用的也放出来（标签在说谎）。
2. 内置 MCP 必须排在技能之前（`bus/core.py`）—— 换了归属之后，
   `register_tool` 那条"外来登记不许顶掉"就只认先来后到了。

[!] 全程 `sandbox()`（换 cwd）+ 首尾指纹机检：生产 `data/` 一个字节不许动。
[!] 输出一律 ASCII（GBK 控制台）。
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


def _fresh(tmp: Path):
    """干净三件套：注册表 + 屏蔽账本 + 停用账本，都指向临时目录。"""
    import importlib
    from bus import mcp_disabled as D
    from bus import quarantine as Q
    from bus import registry as R
    importlib.reload(R)
    Q.reset_for_tests()
    Q.LEDGER_PATH = tmp / "quarantine.json"
    D.reset_for_tests()
    D.LEDGER_PATH = tmp / "mcp_disabled.json"
    return R, Q, D


def _boot_mcp(R):
    """把内置 MCP 那两件真注册进去（只注册 schema，不联网）。"""
    from mcpx.servers import web_search
    return web_search.register_tools()


# 1. 名单：内置 MCP 服务器（两向）

def test_the_builtin_roster_is_two_way():
    print("\n-- 1. `BUILTIN_SERVERS`：名单 <-> `mcpx/servers/*.py` 两向 --")
    from mcpx.servers import BUILTIN_SERVERS, owner_of
    d = _ROOT / "mcpx" / "servers"
    for name in BUILTIN_SERVERS:
        check("★ 名单里的 `%s` 真有 `mcpx/servers/%s.py`" % (name, name),
              (d / (name + ".py")).is_file(), "")
    on_disk = {p.stem for p in d.glob("*.py") if not p.stem.startswith("_")}
    check("★★ 成对：磁盘上的每一个 `mcpx/servers/*.py` 都在名单里（漏登记 -> 它不显示）",
          on_disk == set(BUILTIN_SERVERS), "磁盘 %r vs 名单 %r" % (sorted(on_disk),
                                                                  sorted(BUILTIN_SERVERS)))
    check("★ 归属键是 `mcp:<名>`（与 `bus/quarantine.owner_for_mcp` 逐字同形）",
          owner_of("web_search") == "mcp:web_search", owner_of("web_search"))


# 2. 归属：内置 MCP 不再挂在 `framework` 底下

def test_builtin_mcp_has_its_own_owner():
    print("\n-- 2. 归属：内置 MCP 有自己的 owner（修前是 `framework`）--")
    with tempfile.TemporaryDirectory() as d:
        R, _Q, _D = _fresh(Path(d))
        n = _boot_mcp(R)
        check("setup：注册了两个工具", n == 2, str(n))
        check("★★ `web_search` 的归属是 `mcp:web_search`（**不是** `framework`）",
              R.get_tool_owner("web_search") == "mcp:web_search",
              repr(R.get_tool_owner("web_search")))
        check("★ `web_fetch` 同一份归属", R.get_tool_owner("web_fetch") == "mcp:web_search",
              repr(R.get_tool_owner("web_fetch")))
        #: 成对：别的框架工具仍是 `framework` —— 只写上面那条的话，
        #:   "全都换成 mcp:" 也能全绿。
        R.register_tool("probe_fw", lambda **k: 1, {"description": "x"})
        check("★★ 成对：框架自己的工具仍挂在 `framework` 底下（没被一起改掉）",
              R.get_tool_owner("probe_fw") == R.FRAMEWORK_OWNER,
              repr(R.get_tool_owner("probe_fw")))
        check("★★ 而且按归属能**单独**摘掉它（修前 `framework` 拒摘，所以做不到）",
              R.unregister_owner("mcp:web_search") == 2
              and "web_search" not in R.list_tools()
              and "probe_fw" in R.list_tools(), "")


# 3. 停用：当场生效、而且真的可逆

def test_disable_takes_effect_and_is_reversible():
    print("\n-- 3. 停用：呈现与执行两侧同时生效 + 启用来得回来 --")
    with tempfile.TemporaryDirectory() as d:
        R, _Q, D = _fresh(Path(d))
        _boot_mcp(R)
        R.register_tool("probe_fw", lambda **k: 1, {"description": "x"})
        R.note_discovered("s1", ["probe_fw"])       # 非常驻的那些要先被发现才进活跃集
        check("setup：`web_search` 本来在活跃集里（它是常驻工具）",
              "web_search" in R.get_active_tool_names("s1"), "")

        D.disable("mcp:web_search")
        act = R.get_active_tool_names("s1")
        check("★★ 停用之后**两个工具都走**（粒度是服务器，不是单个工具名）",
              not ({"web_search", "web_fetch"} & act), repr(sorted(act & {"web_search", "web_fetch"})))
        check("★★ **执行侧**（`is_tool_active`）同一个判定",
              R.is_tool_active("s1", "web_search") is False, "")
        check("★★ **呈现侧**（那批 schema）也一起走 —— 两份是同一个函数",
              "web_search" not in {s["function"]["name"]
                                   for s in R.get_tool_schemas_active("s1")}, "")
        check("★★ 成对：**别的工具一格没少**（停用不会把框架自己的工具带走）",
              "probe_fw" in act, "")
        check("★ 系统级：别的会话一样受影响", R.is_tool_active("s9", "web_search") is False, "")

        check("★★ 可逆：启用之后**当场**回来", D.enable("mcp:web_search")
              and R.is_tool_active("s1", "web_search") is True, "")
        check("★ 启用一个没被停用的 -> False（**不是**「假装成功」）",
              D.enable("mcp:nope") is False, "")


# 4. 两个集合分得开（这是本模块存在的理由）

def test_the_two_ledgers_do_not_leak_into_each_other():
    print("\n-- 4. 屏蔽（框架判的）与停用（用户点的）**互不干扰** --")
    with tempfile.TemporaryDirectory() as d:
        R, Q, D = _fresh(Path(d))
        _boot_mcp(R)
        R.register_tool("t_skill", lambda **k: 1, {"description": "x"}, owner="skill:demo")
        R.note_discovered("s1", ["t_skill"])

        Q.quarantine("skill:demo", code="handler_missing", reason="x")
        D.disable("mcp:web_search")
        check("setup：两边各走一格",
              not R.is_tool_active("s1", "t_skill") and not R.is_tool_active("s1", "web_search"), "")

        Q.release("skill:demo")
        check("★★ **解封只放框架判的那一半** —— 用户停用的 MCP **不许**被顺手放出来",
              R.is_tool_active("s1", "t_skill") is True
              and R.is_tool_active("s1", "web_search") is False,
              repr(sorted(R.get_active_tool_names("s1") & {"t_skill", "web_search"})))
        check("★ 成对：**启用也只放停用的那一半**（屏蔽的那条仍不在了）",
              D.enable("mcp:web_search") and R.is_tool_active("s1", "web_search") is True, "")
        D.disable("mcp:web_search")
        Q.quarantine("skill:demo", code="handler_missing", reason="x")
        D.enable("mcp:web_search")
        check("★ 反方向：启用**不清**屏蔽账本", R.is_tool_active("s1", "t_skill") is False, "")


# 4b. 停用之后模型拿到的那句话

def test_a_disabled_tool_gets_its_own_message():
    """停用一条工具之后，模型拿到的话必须指得出真的出路。

    危险形状：停用 `web_search` 后问「今天有什么新闻」，模型 `search_tools`
    走两趟、把 `web_search` 与 `web_fetch` 各调一次，两次都拿到「该工具在
    当前会话不可用」—— 而它刚被告知「搜一下就能拉回来」（同一句里写着的）
白烧两轮，而且不知道该做什么。

    判据：拒绝必带路线，而路线必须是真的。
    """
    print("\n-- 4b. 停用之后那句话：路线是真的（不是那句）--")
    from core import agent_loop as AL
    msg = AL._disabled_message("web_search", "mcp:web_search")
    d11 = AL._not_offered_message("web_search")
    check("★★ 与「没呈现的工具不许跑」那句**不是同一句**（合并 = 路线有一半是假的）", msg != d11, "")
    check("★★ 它**不**承诺 `search_tools`（那正是它要替掉的那句）",
          "search_tools" not in msg, repr(msg)[:200])
    check("★★ 它点名**真的**那条出路（用户能一键启用）",
          "MCP" in msg and "re-enable" in msg, repr(msg)[:200])
    check("★ 而且说清了「什么都没执行」（同两条既有话术的规矩）",
          "nothing was executed" in msg, repr(msg)[:200])
    #: 判据本身：归属没被停用时它不认领（否则一次停用会把所有工具都拒了）。
    with tempfile.TemporaryDirectory() as d:
        R, _Q, D = _fresh(Path(d))
        _boot_mcp(R)
        D.disable("mcp:web_search")
        check("★ 停用中 -> 认领（返回归属键）",
              AL._disabled_owner("web_search") == "mcp:web_search", "")
        check("★★ 成对：**没被停用**的框架工具 -> 不认领",
              AL._disabled_owner("probe_fw") is None, "")
        D.enable("mcp:web_search")
        check("★ 启用之后 -> 不认领了", AL._disabled_owner("web_search") is None, "")


# 5. 接线 + 账本落点

def test_the_wiring_is_structurally_present():
    print("\n-- 5. 接线：账本落点 / 启动加载 / 端点 / 顺序 --")
    def rd(p):
        return (_ROOT / p).read_text(encoding="utf-8", errors="replace")

    check("★ 停用账本落在 `data/runtime/`（运行数据那条规矩）",
          'LEDGER_PATH = Path("data/runtime/mcp_disabled.json")' in rd("bus/mcp_disabled.py"),
          "")
    core = rd("bus/core.py")
    check("★★ 启动时把停用账本装进闸门（与屏蔽账本**同一处**，早于任何装载）",
          "bus.mcp_disabled import load" in core, "")
    #: 顺序承重：换了归属之后，"外来登记不许顶掉"只认先来后到。
    check("★★ **MCP 排在技能之前**（顺序承重 —— 否则一个叫 `web_search` 的包会顶掉内置的）",
          core.index("# -- MCP (v1.0.0) --") < core.index("# -- Skills --"), "")

    srv = rd("web/server.py")
    check("★ 三个端点都在（看 / 停用 / 启用）",
          '"/api/mcp/servers"' in srv and '"/api/mcp/disable"' in srv
          and '"/api/mcp/enable"' in srv, "")
    check("★★ `get_status()` **不再是没有调用者的死代码**（设计稿点名的）",
          "get_status()" in srv, "")
    check("★ 内置那份名单只有一处（`mcpx/servers/__init__.py`），且 `manager` 按它去 import",
          "BUILTIN_SERVERS" in rd("mcpx/manager.py")
          and "BUILTIN_SERVERS" in rd("mcpx/servers/__init__.py"), "")


# 6. 端点（真 Flask test client）

def test_the_endpoints():
    print("\n-- 6. 端点：看 / 停用 / 启用（真 HTTP）--")
    with tempfile.TemporaryDirectory() as d:
        R, _Q, D = _fresh(Path(d))
        _boot_mcp(R)
        import web.server as S
        S.app.config["TESTING"] = True
        c = S.app.test_client()
        tok = S._LAUNCH_TOKEN

        r = c.get("/api/mcp/servers")                                  # 没带 token
        check("★ 没带 token -> 403", r.status_code == 403, str(r.status_code))

        was = S._FRAMEWORK_RUNNING
        S._FRAMEWORK_RUNNING = False
        try:
            body = c.get("/api/mcp/servers?token=" + tok).get_json()
            check("★★ 框架没跑 -> **空表 + error**（不冒充「没有服务器」）",
                  body.get("servers") == [] and bool(body.get("error")), repr(body)[:160])
        finally:
            S._FRAMEWORK_RUNNING = True                     # 下面几段要它"在跑"

        body = c.get("/api/mcp/servers?token=" + tok).get_json()
        rows = body.get("servers") or []
        web = next((x for x in rows if x.get("name") == "web_search"), None)
        check("★ 内置那条列出来了", web is not None, repr(rows)[:200])
        if web:
            check("★★ 它带黄标要的 `builtin: true`", web.get("builtin") is True, repr(web))
            check("★ 它的工具名是从注册表现数的（不是手抄）",
                  set(web.get("tools") or []) == {"web_search", "web_fetch"}, repr(web))
        check("★ `status` 是 `get_status()` 给的（真读数，不再是常数 1）",
              (body.get("status") or {}).get("builtin_servers") == 1,
              repr(body.get("status")))

        r = c.post("/api/mcp/disable?token=" + tok, json={"name": "no-such-server"})
        check("★★ 不认识的名字 -> 404（名字只当键，构造不出 owner）",
              r.status_code == 404, str(r.status_code))
        r = c.post("/api/mcp/disable?token=" + tok, json={})
        check("★ 缺 name -> 400", r.status_code == 400, str(r.status_code))

        r = c.post("/api/mcp/disable?token=" + tok, json={"name": "web_search"})
        check("★ 停用 -> ok", r.status_code == 200 and r.get_json().get("ok"),
              repr(r.get_json())[:160])
        check("★★ 停用**当场**生效（工具真的不发了）",
              not R.is_tool_active("s1", "web_search"), "")
        check("★ 而账本真的落盘了（重启之后还认）",
              "mcp:web_search" in D.all_entries(), repr(D.all_entries()))

        r = c.post("/api/mcp/enable?token=" + tok, json={"name": "web_search"})
        check("★ 启用 -> 回来", r.status_code == 200 and R.is_tool_active("s1", "web_search"),
              repr(r.get_json())[:160])
        check("★ 账本里那条真的没了", "mcp:web_search" not in D.all_entries(), "")
        S._FRAMEWORK_RUNNING = was


def main() -> int:
    print("=" * 62)
    print("MCP management -- disable list / owners / endpoints")
    print("=" * 62)
    before = prod_snapshot()
    try:
        with sandbox():
            for fn in (test_the_builtin_roster_is_two_way,
                       test_builtin_mcp_has_its_own_owner,
                       test_disable_takes_effect_and_is_reversible,
                       test_the_two_ledgers_do_not_leak_into_each_other,
                       test_a_disabled_tool_gets_its_own_message,
                       test_the_wiring_is_structurally_present,
                       test_the_endpoints):
                try:
                    fn()
                except Exception as exc:                            # noqa: BLE001
                    check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        leaked = prod_diff(before, prod_snapshot())
        check("★★ 全程**生产 `data/` 一个字节都没动**",
              not leaked, repr(leaked)[:200])
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
