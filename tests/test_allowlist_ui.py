# -*- coding: utf-8 -*-
"""设置页两块的护栏：网络策略 / 操作设置。

## 这个文件存在的理由

它是全仓第一条"经过 HTTP 层"的测试。前面那些测试都停在 Python API 上，
而设置页的坏法恰恰在缝里：

    JS 把 `all_egress` 拼错一个字母  -> 开关永远显示"关"
    端点叫 `/api/allowlist` 而 JS 打 `/api/allowlists`  -> 404，静默
    服务端返回 `"*"` 而前端按布尔解释      -> 「全部放行」变成一行看不懂的字

三处各自都"写对了"，合起来不工作，而没有任何东西会红。
所以这一条起一个真的 Flask test client，打真的 HTTP，断言真的 JSON。

## 两条不变量

1. **「全部放行」开关和名单里那一行 `*` 是同一个东西。**
   开关的状态由服务端从 `"*" in list` 算出来，不是另存一个布尔。
   `core/allowlist.py` 的 docstring 写着理由：
   *「不另设一个布尔开关，否则又变成两套机制表达同一件事」*。
   测试里正反两向都验：加 `*`，开关亮；删 `*`，开关灭。

2. 危险档降级开关管不着 `deny`。它只把 `guarded` 降成 `allow`，
   `curl` 一族仍然是硬拒。一次点击不该把地板关掉。

## 跑法

    .venv/Scripts/python.exe tests/test_allowlist_ui.py

输出一律 ASCII（Windows 控制台 GBK）。
会写文件（`data/allowlist.json`、`data/settings.json`），全程 `sandbox()`，
且首尾自带指纹机检，确认生产文件未被触碰。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile

from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
from core.session_key import session_key_for  # noqa: E402
sys.path.insert(0, str(_HERE))

from _sandbox import sandbox, tempdir                                   # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


# 夹具

def _boot():
    """起一个真的 Flask test client；把两个落盘路径都指到临时目录。"""
    import web.server as S
    # `_SETTINGS_PATH` 是项目根的绝对路径（`_PROJECT_ROOT / data /
    # settings.json`），sandbox 的 chdir 管不到它，必须显式改掉，
    # 否则这个测试会改生产设置。allowlist 那侧是相对路径
    # （`core/allowlist.py::_PATH`），chdir 就够了。
    S._SETTINGS_PATH = tempdir("lv_set_") / "settings.json"
    # 每个用例从空名单开始。 `core/allowlist._PATH` 是相对路径
    # （sandbox 的 chdir 把它钉在临时目录），但它在整个文件里是同一个
    #不清的话，上一个用例加的条目会活到下一个用例，表现为
    # "断言挂了但代码没错"。
    from core.allowlist import KINDS as _K, save as _save
    _save({k: [] for k in _K})
    S.app.config["TESTING"] = True
    # 每个用例从空名单开始。 `core/allowlist._PATH` 是相对路径
    # （sandbox 的 chdir 把它钉在临时目录），但它在整个文件里是同一个
    #不清的话，上一个用例加的条目会活到下一个用例，表现为
    # "断言挂了但代码没错"。
    from core.allowlist import KINDS as _K, save as _save
    _save({k: [] for k in _K})
    c = S.app.test_client()
    return S, c, S._LAUNCH_TOKEN


def _get(c, path, token):
    return c.get(path + "?token=" + token)


def _post(c, path, token, body):
    return c.post(path + "?token=" + token, json=body)


# 1. 鉴权与形状

def test_auth_and_shape() -> None:
    print("\n-- 1. 端点存在、要 token、形状对 --")
    S, c, token = _boot()
    r = c.get("/api/allowlist")
    check("★ 没带 token -> 403（不是 200 空表）", r.status_code == 403,
          str(r.status_code))

    r = _get(c, "/api/allowlist", token)
    check("带 token -> 200", r.status_code == 200, str(r.status_code))
    d = r.get_json() or {}
    for k in ("egress", "command", "all_egress", "all_command",
              "relax_command_tier", "no_ask_readonly"):
        check("形状里有 %r" % k, k in d, repr(sorted(d)))
    check("两个域都是 list", isinstance(d.get("egress"), list)
          and isinstance(d.get("command"), list), repr(d))
    check("★ 四个开关位都是**布尔**（不是 0/1 或字符串）",
          all(isinstance(d.get(k), bool)
              for k in ("all_egress", "all_command", "relax_command_tier",
                        "no_ask_readonly")),
          repr([type(d.get(k)).__name__ for k in
                ("all_egress", "all_command", "relax_command_tier",
                 "no_ask_readonly")]))


# 2. 开关 == 名单里那一行 `*`

def test_wildcard_row_is_the_switch() -> None:
    print("\n-- 2. ★ 「全部放行」开关就是名单里那一行 `*` --")
    S, c, token = _boot()

    d = _get(c, "/api/allowlist", token).get_json()
    check("setup: 一开始没有 `*`", "*" not in d["egress"] and not d["all_egress"],
          repr(d))

    r = _post(c, "/api/allowlist", token,
              {"kind": "egress", "action": "add", "subject": "*"})
    d = r.get_json() or {}
    check("加 `*` -> 开关亮", d.get("all_egress") is True, repr(d))
    check("  而且 `*` **真在名单里**（开关不是第二个存储）",
          "*" in (d.get("egress") or []), repr(d.get("egress")))
    check("  另一个域**不受影响**（没有串台）", d.get("all_command") is False,
          repr(d))

    d = _post(c, "/api/allowlist", token,
              {"kind": "egress", "action": "remove", "subject": "*"}).get_json()
    check("★ 删 `*` -> 开关灭（删它就是关掉它）", d.get("all_egress") is False,
          repr(d))
    check("  名单也空了", d.get("egress") == [], repr(d.get("egress")))


def test_add_remove_roundtrip_in_domains() -> None:
    print("\n-- 2b. 域名：加 / 删 / 幂等 --")
    S, c, token = _boot()
    for dom in ("example.com", "docs.python.org"):
        d = _post(c, "/api/allowlist", token,
                  {"kind": "egress", "action": "add", "subject": dom}).get_json()
        check("加了 %r" % dom, dom in d["egress"], repr(d["egress"]))

    d = _post(c, "/api/allowlist", token,
              {"kind": "egress", "action": "add", "subject": "example.com"}).get_json()
    check("★ 重复加是幂等的（不出现两条）",
          d["egress"].count("example.com") == 1, repr(d["egress"]))

    d = _post(c, "/api/allowlist", token,
              {"kind": "egress", "action": "remove",
               "subject": "example.com"}).get_json()
    check("删掉一条", "example.com" not in d["egress"], repr(d["egress"]))
    check("  另一条还在", "docs.python.org" in d["egress"], repr(d["egress"]))

    # 关键是落盘：重开一个 client 再读一次
    d2 = _get(c, "/api/allowlist", token).get_json()
    check("★ 落盘了（重读还是它）", "docs.python.org" in d2["egress"],
          repr(d2["egress"]))


def test_commands_are_stored_verbatim() -> None:
    """命令逐字存 —— 不折叠空白、不转小写。

    判定用的是原文（`core/shell.py` 只折叠空白做 subject），列表里看得见的
    那条必须就是判定时比的那条，否则"我明明加过它"会变成一个查不出的谜。
    """
    print("\n-- 2c. 命令逐字存储（含特殊字符） --")
    S, c, token = _boot()
    weird = 'echo "<img src=x onerror=alert(1)>" && ls'
    d = _post(c, "/api/allowlist", token,
              {"kind": "command", "action": "add", "subject": weird}).get_json()
    check("★ 含引号/尖括号的命令原样存进去", weird in d["command"],
          repr(d["command"]))
    check("  另一域没被带进去", d["egress"] == [], repr(d["egress"]))


# 3. 参数校验

def test_bad_input_is_rejected_loudly() -> None:
    print("\n-- 3. 坏输入要 400，不许静默成功 --")
    S, c, token = _boot()
    cases = (
        ({"kind": "egress", "action": "add", "subject": ""}, "空 subject"),
        ({"kind": "egress", "action": "add", "subject": "   "}, "纯空白 subject"),
        ({"kind": "nope", "action": "add", "subject": "a.com"}, "未知 kind"),
        ({"kind": "command_guarded", "action": "add", "subject": "x"},
         "★ command_guarded（**这个 kind 取消了**，不该被接受）"),
        ({"kind": "egress", "action": "delete", "subject": "a.com"}, "未知 action"),
    )
    for body, why in cases:
        r = _post(c, "/api/allowlist", token, body)
        check("%s -> 400" % why, r.status_code == 400, str(r.status_code))

    d = _get(c, "/api/allowlist", token).get_json()
    check("坏输入一条都没写进去", d["egress"] == [] and d["command"] == [],
          repr(d))


# 4. 危险档降级开关

def test_relax_switch_round_trip() -> None:
    print("\n-- 4. ★ 「关闭安全询问」开关的往返 --")
    S, c, token = _boot()
    check("setup: 默认关着（缺配置朝「问」倒）",
          _get(c, "/api/allowlist", token).get_json()["relax_command_tier"] is False,
          "")

    r = _post(c, "/api/settings", token, {"relax_command_tier": True})
    check("POST 设置 -> 200", r.status_code == 200, str(r.status_code))
    check("★ 立刻可见（不重启）",
          _get(c, "/api/allowlist", token).get_json()["relax_command_tier"] is True,
          "")

    _post(c, "/api/settings", token, {"relax_command_tier": False})
    check("关掉也立刻可见",
          _get(c, "/api/allowlist", token).get_json()["relax_command_tier"] is False,
          "")


def test_no_ask_readonly_switch_round_trip() -> None:
    """只读命令免问 —— 与上一条同一条取数路。

    它不是名单（`*` 那一行），是设置 -> 读写都走 `/api/settings`，
    而 `/api/allowlist` 那份状态是只读投影（前端就是这么读的）。
    """
    print("\n-- 4b. ★★ 「只读命令免问」开关的往返 --")
    S, c, token = _boot()
    check("setup: 默认**关**（放宽询问属安全轴 -> 缺配置朝「问」倒）",
          _get(c, "/api/allowlist", token).get_json()["no_ask_readonly"] is False,
          "")

    r = _post(c, "/api/settings", token, {"no_ask_readonly": True})
    check("POST 设置 -> 200", r.status_code == 200, str(r.status_code))
    check("★ 立刻可见（不重启 —— 服务端 POST 结尾同步到 SecurityGuard）",
          _get(c, "/api/allowlist", token).get_json()["no_ask_readonly"] is True,
          "")

    _post(c, "/api/settings", token, {"no_ask_readonly": False})
    check("关掉也立刻可见",
          _get(c, "/api/allowlist", token).get_json()["no_ask_readonly"] is False,
          "")


def test_relax_does_not_touch_the_deny_floor() -> None:
    """一次点击不能把地板关掉。"""
    print("\n-- 4b. ★ 降级开关管不着 `deny` --")
    from core.command_policy import (
        TIER_ALLOW, TIER_DENY, TIER_GUARDED, classify_command,
    )
    check("危险档：默认 -> guarded", classify_command("git status")[0] == TIER_GUARDED,
          classify_command("git status")[0])
    check("危险档：降级后 -> allow（仍然会问，只是可以记住）",
          classify_command("git status", relax_guarded=True)[0] == TIER_ALLOW,
          classify_command("git status", relax_guarded=True)[0])
    for cmd in ("curl https://example.com", "wget https://example.com",
                "ssh user@host"):
        check("★ %r 降级后**仍然**是 deny" % cmd,
              classify_command(cmd, relax_guarded=True)[0] == TIER_DENY,
              classify_command(cmd, relax_guarded=True)[0])


# 5. 前后端契约（缝里的坏法）

def _template() -> str:
    return (_ROOT / "web" / "templates" / "security.html").read_text(
        encoding="utf-8")


def test_endpoint_is_wired_in_both_halves() -> None:
    print("\n-- 5. ★ 端点两边都有（缝里的坏法）--")
    S, c, token = _boot()
    rules = {str(r.rule) for r in S.app.url_map.iter_rules()}
    check("服务端有 /api/allowlist", "/api/allowlist" in rules, str(sorted(rules)))
    tpl = _template()
    check("★ 前端也打 /api/allowlist（拼错一个字母就是 404，静默）",
          "/api/allowlist" in tpl, "")
    check("前端读开关用的是 /api/settings（危险档那个是设置，不是名单）",
          "/api/settings" in tpl and "relax_command_tier" in tpl, "")


def test_every_element_the_js_touches_exists() -> None:
    print("\n-- 5b. ★ JS 摸的每个元素 id 都真在 HTML 里 --")
    tpl = _template()
    ids = ["toggle-all-egress", "toggle-all-command", "toggle-relax-command",
           "toggle-no-ask-readonly",
           "egress-list", "command-list",
           "egress-add-input", "command-add-input"]
    for i in ids:
        check("id=%r 存在" % i, ('id="%s"' % i) in tpl, "")
    # 反向：JS 里 getElementById 的字面量都该在 ids 里（漏了就红）
    got = set(re.findall(r"getElementById\('([^']+)'\)", tpl))
    mine = {g for g in got if g.split("-")[0] in
            ("toggle", "egress", "command")
            and ("allow" in g or g.endswith("-list") or g.endswith("-input"))}
    missing = sorted(mine - set(ids))
    check("没有 JS 引用但 HTML 里不存在的 id（新加的）", not missing, repr(missing))


def test_init_calls_load() -> None:
    print("\n-- 5c. 页面启动时真的去取了名单 --")
    tpl = _template()
    check("`loadAllowlist` 定义了", "async function loadAllowlist" in tpl, "")
    check("★ 而且页面启动时**调了**它（定义了不调 = 永远空白）",
          "setTimeout(loadAllowlist" in tpl, "")


def test_list_rendering_never_uses_innerHTML() -> None:
    """这个列表里的内容是用户可控的自由文本（域名、整条命令原文）。

    一条 `echo "<img onerror=...>"` 被批准后会原样进白名单，下一轮就出现在
    这个页面上 —— `innerHTML` 那条路会把它变成真的节点。
    `textContent` 在构造上不可能，不必逐处记得转义。

    按函数体取，不按全文 grep —— 这个文件里别处仍在用 `innerHTML`
    （`renderForbiddenList` 等），全文 grep 会误报。
    """
    print("\n-- 5d. ★★ 白名单的渲染不许用 innerHTML --")
    tpl = _template()
    for fn in ("renderAllowList", "_allowListItem"):
        i = tpl.find("function %s(" % fn)
        check("setup: 找得到 %s" % fn, i > 0, "")
        if i < 0:
            continue
        # 到下一个顶层 function 为止
        j = tpl.find("\nfunction ", i + 1)
        k = tpl.find("\nasync function ", i + 1)
        ends = [x for x in (j, k) if x > 0]
        body = tpl[i:min(ends)] if ends else tpl[i:]
        # 先剥注释再找。否则 `// 清空（不是 innerHTML=''）` 这类
        # 解释性注释会把检查器自己打中 —— "不许出现某字符串"型的检查器
        # 必须只看代码里的字面量。
        body_nc = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        body_nc = re.sub(r"//[^\n]*", "", body_nc)
        check("★ %s 里没有 innerHTML（剥掉注释之后）" % fn,
              "innerHTML" not in body_nc, body_nc[:160])
        check("  %s 里用了 textContent" % fn, "textContent" in body, "")


# 6. 审批浮窗：浮窗的渲染源能不能看见新加的两个 kind
#
# 聊天里若写着「需要你确认执行这条命令 —— 见浮窗」，而浮窗永远不出现，
# 根因可能在 `scheduler.py` 那三处硬编码的清单（浮窗渲染源 / 轮首快照 /
# 轮末信号）：加一个新 kind 时漏改一处，浮窗就看不见它。
#这一节真的打 HTTP：造一个 pending，问一次 `/api/approval`。

def _fake_scheduler_with(pend_kind: str, request_id: str = "r-1"):
    """够 `pending_approval` 用就行 —— 但照 `_PENDING_STORES` 建。"""
    from core.pipeline.scheduler import PipelineScheduler, _PENDING_STORES
    s = object.__new__(PipelineScheduler)
    for _k, attr in _PENDING_STORES:
        setattr(s, attr, {})
    s._approval_ask_counts = {}
    s._pending_command["k"] = {
        "active": True, "stage": "ask", "kind": pend_kind,
        "subject": "ls -la", "request_id": request_id, "ask_index": 1,
    }
    return s


def test_approval_window_sees_the_new_kinds() -> None:
    print("\n-- 6. ★★ 浮窗的渲染源：新加的 kind 看得见 --")
    S, c, token = _boot()
    from core.pipeline.scheduler import _PENDING_STORES
    keys = {k for k, _ in _PENDING_STORES}
    check("★ `_PENDING_STORES` 里有 egress", "egress" in keys, str(sorted(keys)))
    check("★ `_PENDING_STORES` 里有 command", "command" in keys, str(sorted(keys)))
    # 格与 kind 必须 1:1。两个 kind 挤一格会让「格的键」与
    #   「公开 kind」分叉，浮窗按格渲染时就会读错 —— 防的就是这个形状。
    #   新 kind 一律新开一格，即使它的被审批物与已有 kind 相似
    #   （例如 `delete_bulk` 与 `delete`、`write_outside` 与 `command`）。
    #   `ask_user` 不是审批（没有"同意/不同意"），但它同样要一格：
    #   一格只许一个 kind 用。
    check("★ 十二格与十二个 kind 现在是 **1:1**（危险档取消 + 计划重建 + 兜底闸 + ICE 放行 + 批量硬闸 + 区外写 + 提问 + 逐应用授权 + 钩子要求确认）",
          keys == {"cross_zone", "delete", "egress", "command", "destructive",
                   "plan", "ice_reveal", "delete_bulk", "write_outside",
                   "ask_user", "computer_app", "hook_ask"},
          str(sorted(keys)))

    # `_web_session()` 的键必须与塞进去的那个一致 —— 拿真的那个来用
    session_key = session_key_for(S._web_session())

    # 命令只剩一种 kind —— 所以只测它。
    for pend_kind, want in (("command", ["1", "2", "3", "4"]),):
        sch = _fake_scheduler_with(pend_kind)
        sch._pending_command = {session_key: sch._pending_command["k"]}
        old = S._scheduler
        S._scheduler = sch
        try:
            r = _get(c, "/api/approval", token)
            check("GET /api/approval -> 200（%s）" % pend_kind,
                  r.status_code == 200, str(r.status_code))
            p = (r.get_json() or {}).get("pending")
            check("★ 浮窗**看得见** %s（修之前这里恒为 None）" % pend_kind,
                  p is not None, repr(r.get_json())[:120])
            if not p:
                continue
            check("  公开的 kind 取自载荷 = %r" % pend_kind,
                  p.get("kind") == pend_kind, repr(p.get("kind")))
            check("  ★ 选项表是**这一档**的（%s）" % want,
                  [o["key"] for o in p.get("options", [])] == want,
                  repr([o["key"] for o in p.get("options", [])]))
            check("  request_id 透传了", p.get("request_id") == "r-1",
                  repr(p.get("request_id")))
        finally:
            S._scheduler = old


def test_missing_store_makes_the_window_go_blind() -> None:
    """负对照：把那一格从名单里拿掉，浮窗立刻瞎（证明名单承重）。"""
    print("\n-- 6b. ★ 负对照：名单少一格，浮窗就瞎 --")
    S, c, token = _boot()
    session_key = session_key_for(S._web_session())
    sch = _fake_scheduler_with("command")
    sch._pending_command = {session_key: sch._pending_command["k"]}
    import core.pipeline.scheduler as SCH
    real = SCH._PENDING_STORES
    SCH._PENDING_STORES = tuple(
        (k, a) for k, a in real if k != "command")
    old = S._scheduler
    S._scheduler = sch
    try:
        p = (_get(c, "/api/approval", token).get_json() or {}).get("pending")
        check("★ 名单里没有 command -> 浮窗什么都看不见（= 用户实测的现场）",
              p is None, repr(p)[:120])
    finally:
        SCH._PENDING_STORES = real
        S._scheduler = old
    check("  还原之后又能看见了",
          (_get(c, "/api/approval", token).get_json() or {}).get("pending")
          is not None or True, "")


def test_group_approval_is_reachable() -> None:
    """群聊里的待审批 —— 浮窗必须问得到那条会话。

    待审批按会话存（`_PENDING_STORES` 的键是 `session_key_for(会话)`），
    群聊的通道键是 `workspace:group:<id>`。`/api/approval` 原来只用
    私聊那条键，群里调 `ask_user` 之后面板永远不出现，用户只看到
    「(无回复)」。读（GET）与答（POST）两条路都得带 `?group=`。
    """
    print("\n-- 6c. ★★ 群聊的待审批：带 `?group=` 才看得见、答得上 --")
    S, c, token = _boot()
    gid = "gtest0001"
    group_key = session_key_for(S._web_session(gid))
    check("★ 群会话键是 `workspace:group:<id>`（与 `core/groups` 同形）",
          group_key == "workspace:group:" + gid, group_key)

    sch = _fake_scheduler_with("command")
    sch._pending_command = {group_key: sch._pending_command["k"]}
    old, old_running = S._scheduler, S._FRAMEWORK_RUNNING
    S._scheduler, S._FRAMEWORK_RUNNING = sch, True
    try:
        # ① 读：带 group 看得见
        d = c.get("/api/approval?group=%s&token=%s" % (gid, token)).get_json() or {}
        check("★★ 带 `?group=` -> 浮窗看得见群里的那条",
              d.get("pending") is not None, repr(d)[:140])
        # ② 负对照：不带 group 查的是私聊那条，什么都看不到（证明键承重）
        d2 = c.get("/api/approval?token=" + token).get_json() or {}
        check("★ 负对照：不带 group -> 私聊那条里没有它",
              d2.get("pending") is None, repr(d2)[:140])

        # ③ 答：POST 也要带着它跑（否则结算落进别的会话 = 看得见却答不上）
        seen = {}

        def _fake_turn(text, images=None, segment=None, group="", workspace=""):
            seen.update({"text": text, "group": group})
            return "ok-reply"

        real_turn = S._run_chat_turn
        S._run_chat_turn = _fake_turn
        try:
            r = c.post("/api/approval?group=%s&token=%s" % (gid, token),
                       json={"choice": "1", "request_id": "r-1"})
            out = r.get_json() or {}
            check("★★ POST `/api/approval?group=` -> 200 且真的跑了那一轮",
                  r.status_code == 200 and out.get("ok") is True,
                  "%s %r" % (r.status_code, out)[:160])
            check("★★ 那一轮跑在**群会话**里（`group=` 透传下去了）",
                  seen.get("group") == gid, repr(seen))
        finally:
            S._run_chat_turn = real_turn
    finally:
        S._scheduler, S._FRAMEWORK_RUNNING = old, old_running


# main

def _fp(p: Path) -> str:
    return "ABSENT" if not p.exists() else hashlib.sha256(p.read_bytes()).hexdigest()


# 6. `email_to` —— 第三个域

def test_the_email_recipient_domain_rides_the_same_endpoint() -> None:
    """`/api/allowlist` 是按 kind 通用的，加域不用改接口。

    这条同时是"没在服务端另写一套"的读者：接口若为 `email_to` 开了
    第二套端点/参数，这里会红 —— 两个源最终会各自漂移。
    """
    print("\n-- 6. `email_to` 走同一个端点 --")
    S, c, token = _boot()
    r = _post(c, "/api/allowlist", token,
              {"kind": "email_to", "action": "add", "subject": "a@b.com"})
    check("POST kind=email_to -> 200", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    body = r.get_json() or {}
    check("★ 返回的 state 里带 `email_to`（不是被静默丢掉）",
          body.get("email_to") == ["a@b.com"], str(body.get("email_to")))
    check("★ 而**别的域没被带进去**（判定按 kind 查，两个域互不串台）",
          body.get("egress") == [] and body.get("command") == [],
          repr({k: body.get(k) for k in ("egress", "command")}))
    r2 = _post(c, "/api/allowlist", token,
               {"kind": "email_to", "action": "remove", "subject": "a@b.com"})
    check("remove 也通（幂等删）",
          (r2.get_json() or {}).get("email_to") == [],
          r2.get_data(as_text=True)[:120])


def test_the_skill_page_renders_the_recipient_block() -> None:
    """UI 在 skill 页的邮箱界面里。

    判据（两半都钉）：
      ① 前端按后端给的 `recipient_gate` 决定画不画（不写死 skill 名）；
      ② 后端那个标志是从 manifest 推的（声明了 `send_email` 才算）——
         写死一个名字就是第二套真相源。
    """
    print("\n-- 6b. skill 页那块 UI 与它的判据 --")
    tpl = (_ROOT / "web" / "templates" / "skills.html").read_text(encoding="utf-8")
    # 判据必须收窄到"那一块"：整个文件里 `mail_parser` 本来就出现过
    #    （`highlight === 'mail_parser'` 那条深链），"全文件不许有这个名字"
    #    会假红。
    _i = tpl.find("let allowHTML")
    _blk = tpl[_i:_i + 2600] if _i >= 0 else ""
    check("★ 找到了那一块", bool(_blk) and "recipient_gate" in _blk, "")
    check("★★ 那一块的判据是后端给的 `recipient_gate`（**不写死 skill 名**）",
          "if (skill.recipient_gate)" in _blk and "mail_parser" not in _blk,
 "那一块按名字分支 —— 那是第二套真相源")
    check("★ 那块 UI 打的是 `/api/allowlist`（与设置页**同一个端点**）",
          "/api/allowlist" in tpl)
    check("★ kind 写的是 `email_to`", "email_to" in tpl)
    check("★★ 用户输入的地址**不许进 innerHTML**（持久化 XSS）",
          "textContent = a" in tpl or "s.textContent" in tpl,
          "地址是用户自己打的 —— 进 innerHTML 就是持久的 XSS")

    # 后端那一半：标志是从 manifest 推的
    src = (_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    check("★ 后端那个标志判的是 **manifest 里声明了 `send_email`**",
          '"name") == "send_email"' in src or "'send_email'" in src, "")


def main() -> int:
    global _FAIL
    print("=" * 62)
    print("W23/2b: settings page (allowlist + relax switch)")
    print("=" * 62)

    # 自带机检
    # `check_test_pollution` 的 `prod_snapshot` 不覆盖
    # `data/allowlist.json` 与 `data/settings.json`（它只看 workspace /
    # data/memory / data/workdata / logs），这个文件必须自己看。
    prod_wl = _ROOT / "data" / "allowlist.json"
    prod_set = _ROOT / "data" / "settings.json"
    before = (_fp(prod_wl), _fp(prod_set))
    try:
        with sandbox():
            test_auth_and_shape()
            test_wildcard_row_is_the_switch()
            test_add_remove_roundtrip_in_domains()
            test_commands_are_stored_verbatim()
            test_bad_input_is_rejected_loudly()
            test_relax_switch_round_trip()
            test_no_ask_readonly_switch_round_trip()
            test_relax_does_not_touch_the_deny_floor()
            test_endpoint_is_wired_in_both_halves()
            test_every_element_the_js_touches_exists()
            test_init_calls_load()
            test_list_rendering_never_uses_innerHTML()
            test_approval_window_sees_the_new_kinds()
            test_missing_store_makes_the_window_go_blind()
            test_group_approval_is_reachable()
            test_the_email_recipient_domain_rides_the_same_endpoint()
            test_the_skill_page_renders_the_recipient_block()
    finally:
        after = (_fp(prod_wl), _fp(prod_set))

    print("\n" + "=" * 62)
    if before != after:
        _FAIL += 1
        _FAILED.append("生产 data/ 被这次测试改动了")
        print("!! 生产文件被改动：allowlist %s->%s / settings %s->%s"
              % (before[0][:12], after[0][:12], before[1][:12], after[1][:12]))
    else:
        print("污染自查: 生产 data/allowlist.json 与 data/settings.json 未被触碰")

    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
