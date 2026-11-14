#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""设置页那一行「项目指令文件」—— 纯函数那份自检接进全量 runner。

    .venv/Scripts/python.exe tests/test_desktop_settings.py

判据（软告警）：「设置页一行 + 启动日志一行」。
日志那半是超阈值 `print` 一行、进 `/api/logs`；这一半是这条。

落点在 GUI 侧（GUI 那批出表之前 WebUI 冻结，前端只在 Electron 侧长）。

这份文件管四件事：
  ① 接线：设置页真有那一格、真用了判据模块、数据真从后端来
  ② 后端真的把它交出去了，且读不到 != 0（多带一个 `loaded`）
  ③ node 那份自检真的跑了（node 不在 PATH 时不许静默通过）
  ④ 负对照
  ⑤ 表里每个键都接在一条真键上（按 AST 反查 `POST` / 真跑 `GET`）

本节印 node 那一行时带 `[node]` 前缀：
  `run_tests.py` 读"这个文件跑了几条"用的是汇总行的正则
  （`^\\s*(\\d+) tests:`）—— 而 node 自检印的 `30 tests: …` 长得一模一样
  且排在本文件自己那行之前，报出来的是别人的数。

输出一律 ASCII 安全字符（GBK 控制台）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_UI = _ROOT / "packages" / "ui" / "src"
_JSTEST = _ROOT / "packages" / "ui" / "tests" / "settings-view.test.mjs"
_APP = _UI / "App.jsx"
_VIEW = _UI / "settings-view.js"
_SERVER = _ROOT / "web" / "server.py"
_EN = _ROOT / "locales" / "en.json"
_CSS = _UI / "styles.css"

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(desc: str, ok: bool, note: str = "") -> None:
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc, ("  -- " + note) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


#: 别在这里自己抄一份注释剥离器（两句正则）。
#:   「两份会漂」在测试自己身上同样成立：朴素的正则会
#:   **吃掉字符串里的 `/*` 与 `//`**（如 `Composer.jsx` 的
#:   `ACCEPT = "image/*,…"`，一路吃到下一个 `*/`），它看到的源码是被截过的。
#:共用那一份。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_memory_ui import _strip_js_comments  # noqa: E402


def _table_rows(view: str) -> list:
    """`settings-view.js::ROWS` 里每一条 `{ ... }`（粗切：按行扫 `id:` 起、`}` 止）。

    不 import node：这一节要的是"表里写了哪些 `key`"，
      而那几行是声明（正则读得到、语义没有歧义）。
    """
    body = view.split("export const ROWS", 1)[1].split("];", 1)[0]
    rows = []
    for block in re.split(r"\n  \{ ", body):
        if "id:" not in block:
            continue
        rows.append(block)
    return rows


def _check_g9_note(view: str, app: str) -> None:
    """保留期与上限要写在界面上，不能只在文档里。

    设计判据：「保留期｜三十天前以及更久依然保留」·「上限｜后续在设置里注明」。

    这一节判的不是那句话的措辞，是它的同源性 —— 那句话会念出提醒阈值，
    而拿它干活的代码在 `App.jsx`。两处各写一个 `50` 就会各自漂，
    而漂了没有任何东西会红（一个口径一个地方）。
判据：两个常量在 `settings-view.js` 里只有一处定义，说明里念的是常量。
    用户把文案改短了：只念提醒阈值；页长常量仍然存在（`App.jsx` 在用）。
    """
    src = _strip_js_comments(view)
    vals = {}
    for name in ("THREAD_LIST_LIMIT", "THREAD_NOTICE_AT"):
        # ① 常量自己只有一处定义（多了就是两个真相源）
        n_def = len(re.findall(r"export const %s\s*=" % name, src))
        check("★ `%s` 在 `settings-view.js` 里**恰好定义一次**" % name, n_def == 1,
              "定义 %d 次" % n_def)
        m = re.search(r"export const %s\s*=\s*(\d+)" % name, src)
        vals[name] = m.group(1) if m else None
        check("★ `%s` 的值是 %s（改它 = 一次要签字的改动，不是静默漂）"
              % (name, vals[name]), vals[name] is not None, "")
        # ② `App.jsx` 里是取用它，不是自己写死一个
        check("★ `App.jsx` 从 `settings-view.js` 取用 `%s`" % name,
              name in app and ("export const %s" % name) not in app, "")

    #: 只有 `thread_notice` 那一行与这两个数有关 —— 判据必须收窄到它。
    #:   写成「这个数在整个文件里只出现一次」会假红：
    #:   `50` 还是 `memory_extract_every_n_turns` 那一行的 `max: 50`。
    #:   （判据要收窄到「我要防的那个对象长什么样」。）
    row = ""
    for blk in _table_rows(view):
        if 'id: "thread_notice"' in blk or "id: 'thread_notice'" in blk:
            row = blk
    check("★ 先自证：找得到 `thread_notice` 那一行", bool(row), repr(row[:60]))
    if not row:
        return
    # 表是按行粗切的，块里带着注释；下面判的是代码，先剥掉。
    code = _strip_js_comments(row)
    #: 这段说明只念提醒阈值，不再念页长
    #:   （`THREAD_LIST_LIMIT` 仍由 `App.jsx` 取用，常量那两条在上面）。
    check("★★ 说明里写的是提醒触发条件（用户的文案）",
          "对话条目超过" in code and "出现提醒" in code, code[:220])
    # 从模板串插值改成模板 + `noteVars` —— 键是中文模板，
    #    模块级插值会让 `t()` 拿到 "…超过 50 时…" 而表里没有这一行（英文界面回落中文）。
    check("★★ 它念提醒阈值时把常量交给 `noteVars`（值是模板）",
          bool(re.search(r"noteVars\s*:\s*\{\s*n\s*:\s*THREAD_NOTICE_AT\s*\}", code))
          and "{n}" in code, code[:220])
    check("★★ 那句话里**不许**有 `${`（插值会把 i18n 的键改掉）",
          "${" not in code, code[:220])
    for name, val in vals.items():
        if not val:
            continue
        stray = re.findall(r"(?<![\d.\w])%s(?![\d\w])" % val, code)
        check("★★ 那一行里**没有裸的 `%s`**（念的是常量，不是又抄一遍）" % val,
              not stray, "裸字面量 %d 处：%s" % (len(stray), code[:200]))
    # 纯文本那一格：`` 会原样显示**。
    check("★★ 说明里不许有 markdown 的 `**`（那一格是 `<span>` 纯文本）",
          "**" not in code, code[:220])
    # 负对照：判据自己也要能红（合成样本，`**` 与裸字面量各一条）
    check("★★ 负对照：合成样本里那个 `**` 抓得住",
          "**" in _strip_js_comments('id: "thread_notice", note: "这句要**加粗**"'), "")
    check("★★ 负对照：合成样本里那个裸的 `%s` 抓得住" % (vals["THREAD_NOTICE_AT"] or "50"),
          bool(re.findall(r"(?<![\d.\w])%s(?![\d\w])" % (vals["THREAD_NOTICE_AT"] or "50"),
                          _strip_js_comments('note: `对话条目超过 50 时将出现提醒`'))), "")


def _check_row_i18n(view: str) -> None:
    """设置表里那几格是「渲染时才查表」的 —— 键在运行时才知道。

    `test_i18n_coverage` 只扫 `t("字面量")`，这一族它明写着不管。
    真机上就漏过一条：往期公告那行的说明在英文界面里还是中文。
    """
    en = json.loads(_EN.read_text(encoding="utf-8"))
    fields = ("label", "note", "why", "button", "emptyLabel",
              "warnTitle", "warnBody", "ph")
    miss = []
    for block in _table_rows(view):
        rid = (re.search(r'id:\s*"([^"]+)"', block) or [None, "?"])[1]
        for f in fields:
            for m in re.finditer(r'(?<![A-Za-z0-9_])%s:\s*"((?:[^"\\]|\\.)*)"' % f, block):
                val = m.group(1).replace("\\\\", "\\").replace('\\"', '"')
                if re.search(r"[\u4e00-\u9fff]", val) and val not in en:
                    miss.append("%s.%s" % (rid, f))
    check("★ 表里每个可见字段都有英文译文", not miss, ", ".join(miss))


def _row(view: str, rid: str) -> str:
    """按 `id` 取那一行（粗切，与 `_table_rows` 同一套）。

    返回的是原文（带注释）—— 下面判的是代码，各条自己剥。
    """
    for blk in _table_rows(view):
        if ('id: "%s"' % rid) in blk or ("id: '%s'" % rid) in blk:
            return blk
    return ""


def _check_w180(view: str, app: str) -> None:
    """把 GUI 设置页做完 —— 七格逐格钉。

    盘点的问题有三类：一些没实现的（比如清空记忆库）· 一些没统一观感的
    （下拉能不能改成和投入档一样的窗口）· 还有一些没接线的表格和名单。

    这一节的判据是「那一格现在是什么」，不是"代码里有没有某个词" ——
    所以每一格都问「它现在长什么样」，并且成对读（表里那一行 + `App.jsx` 里
    那一条路）。只读一头的话，表里改了而界面没接会一路绿。
    """
    # ① 命令白名单：从 `moved` 接成编辑器（照隔壁白名单那一格）
    r = _strip_js_comments(_row(view, "cmd_list"))
    check("★ ① 命令白名单：是个真编辑器（`kind: \"allowlist\"`）",
          'kind: "allowlist"' in r, r[:120])
    check("★ ① 它挂在 `command` 那份名单上（与「执行·全部放行」同一个 kind）",
          'allowlist: "command"' in r, r[:120])
    check("★ ① 它说清了匹配口径（整条精确，不是前缀）",
          "精确匹配" in r, r[:200])

    # ② 清空记忆库：从 `notdone` 接成按钮 且必须问一次
    r = _strip_js_comments(_row(view, "memory_clear"))
    check("★ ② 清空记忆库：是个真按钮（`kind: \"act\"`）",
          'kind: "act"' in r and 'act: "clear_memory"' in r, r[:140])
    check("★★ ② 它标了 `danger` -> 走那道 5 秒倒计时（端点自己要求的）",
          "danger: true" in r, r[:140])
    check("★★ ② 界面那一侧**真的有**那道倒计时，而且到点打的是那个端点",
          "const [ask, setAsk] = useState" in app and "/api/memory/clear" in app, "")
    check("★★ ② 而它**不许**在 `run()` 里直接打（那就不问了）—— 见 `danger` 那一行",
          re.search(r'if \(row\.danger\) \{ setAsk\(', app) is not None, "")

    # ④ 倒计时只解禁，不替人按（调整：存在用户反馈）
    #: 原话：「都应该是倒计时结束后解禁按钮，而不是自动点了」。
    #: 这条对两种弹窗都成立：ICE（反诈骗）与 `ask`（不可逆动作）。
    #: 自动执行 = 把"确认"这一步删掉 —— 那两个端点都不可逆。
    def _effect(dep):
        """整个 `useEffect(() => { … }, [dep]);` —— 从 `useEffect` 起算。

        不能从 `if (!ask …)` 那一行起算：那样"在它之前插一句自动执行"
          就绕过了这条断言（第一次变异验证就是这么假绿的）。
        """
        j = app.find("}, [%s]);" % dep)
        if j < 0:
            return ""
        i = app.rfind("useEffect(() => {", 0, j)
        return app[i:j] if i >= 0 else ""

    ask_eff = _effect("ask")
    warn_eff = _effect("warn")
    check("★ ④ 两个倒计时 effect 都找得到（找不到 = 下面两条空转）",
          bool(ask_eff) and bool(warn_eff),
          "ask=%d warn=%d" % (len(ask_eff), len(warn_eff)))
    check("★★ ④ `ask` 的倒计时里**不许**调 `doDanger`（解禁 ≠ 执行）",
          "doDanger" not in ask_eff, ask_eff[:160])
    check("★★ ④ `warn`（ICE）的倒计时里**不许**写设置（解禁 ≠ 关掉）",
          "saveBody" not in warn_eff and "save(" not in warn_eff, warn_eff[:160])
    check("★ ④ 而按钮的 `onClick` 里**有**那一次真正的执行",
          "doDanger(row)" in app, "")

    # ③ 恢复默认设置：同理
    r = _strip_js_comments(_row(view, "reset"))
    check("★ ③ 恢复默认设置：是个真按钮",
          'kind: "act"' in r and 'act: "restore_defaults"' in r, r[:140])
    check("★ ③ 界面那一侧真的打了那个端点",
          "/api/settings/restore-default" in app, "")

    # ④ 反馈：只开一条外链，框架侧零外发
    #   判据：反馈通道不该要求用户先把邮箱凭据交出去。
    r = _strip_js_comments(_row(view, "issues"))
    check("★ ④ 反馈：一颗真动作（`act: \"open_issues\"`）",
          'kind: "act"' in r and 'act: "open_issues"' in r, r[:140])
    check("★★ ④ 它是**纯本机动作**（`local: true`）—— 不打任何端点",
          "local: true" in r, r[:140])
    _api = (_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    check("★★ ④ 后端**再也没有** `/api/feedback`（它曾拿用户的邮箱把日志发出去）",
          "/api/feedback" not in _api, "端点还在")
    check("★★ ④ 界面上也没有了（面板文件已删、App 不再引用）",
          not (_ROOT / "packages" / "ui" / "src" / "Feedback.jsx").is_file()
          and "/api/feedback" not in app
          and "Feedback" not in app.split("function ")[0], "")
    _plt = _ROOT / "packages" / "ui" / "src" / "project-links.js"
    _pl = _plt.read_text(encoding="utf-8") if _plt.is_file() else ""
    check("★★ ④ 外链走 `window.open`（交给系统浏览器），不经过框架",
          "window.open" in _pl and "ISSUES_URL" in _pl and "PROJECT_URL" in _pl, "")
    check("★ ④ 而界面真的取了那两个常量",
          "ISSUES_URL" in app and "PROJECT_URL" in app, "")

    # ⑤ 规避目录：从 `moved` 接成名单编辑器
    r = _strip_js_comments(_row(view, "forbidden"))
    check("★ ⑤ 规避目录：是个真编辑器（`kind: \"list\"`）",
          'kind: "list"' in r, r[:140])
    check("★ ⑤ 它读写的是 `settings.json` 那一格（不是另一个端点）",
          'key: "forbidden_directories"' in r, r[:160])
    check("★★ ⑤ 界面那一侧有那条路，而且打的是 `/api/settings`",
          re.search(r"async function liChange\(", app) is not None
          and re.search(r'jsonRequest\("POST", \{ \[row\.key\]: next \}\)', app) is not None, "")

    # ⑥ 配色自定义：没做，而且是量过之后才不做的
    r = _strip_js_comments(_row(view, "theme"))
    #: 从 `moved` 接成真编辑器。
    #:   判据要两半：新 kind 在 且 旧的 `moved` 不在 ——
    #:   只查前半句的话，"两行都留着"照样绿（模板里先匹配到哪一行就渲染哪一行）。
    check("★ ⑥ 配色那一格是个真编辑器（`kind: \"theme\"`，不再是 `moved`）",
          'kind: "theme"' in r and 'kind: "moved"' not in r, r[:160])
    #: 判据是如实交代存哪：配色只存这台机器（GUI 自己的
    #:   localStorage），既不进后端、也没有跟 WebUI 共用一份。
    #:   这句是给用户看的承诺 —— 以后真要改成共用，先改这句再改代码。
    # 文案统一之后这句是「仅对本机生效」—— 判据仍看有没有如实交代
    #: （两种写法都认；把披露删掉照样红）。
    check("★★ ⑥ 而它如实写着「只存这台机器」（配色不进后端，也不跟 WebUI 共用）",
          ("这台机器" in r) or ("本机" in r), r[:260])

    # ⑦ 下拉统一观感
    #   判据分三层，一层都不许省：① 原生 `<select>` 一个不剩 ② 用的是
    #   同一个组件（不是"画一个像的"）③ 那个组件只有一份（没留旧副本）。
    check("★★ ⑦ `App.jsx` 里**一个原生 `<select>` 都不剩**（`choice` 那批换掉了）",
          "<select" not in _strip_js_comments(app), "")
    check("★★ ⑦ `choice` 那一支用的是**同一个组件** `UpPicker`（不是画一个像的）",
          re.search(r'import \{ UpPicker \} from "\./UpPicker\.jsx"', app) is not None
          and "<UpPicker fixed" in app, "")
    _UP = _UI / "UpPicker.jsx"
    up = _UP.read_text(encoding="utf-8") if _UP.is_file() else ""
    check("★★ ⑦ 而它**只有一份**（`Composer.jsx` 里没留旧的那个）",
          bool(up) and "function UpPicker(" not in (_UI / "Composer.jsx").read_text(encoding="utf-8"),
          "")
    #   `fixed` 那一档是必须的：设置那一列是 `overflow:auto`，绝对定位的
    #   菜单会被裁掉。实现是 portal 到 body + 量出来的坐标。
    check("★★ ⑦ `fixed` 那一档 portal 到 `body`（不然菜单会被那一列裁掉）",
          "createPortal" in up and "document.body" in up, "")
    check("★★ ⑦ 配套：点外面关的 handler **得认 `.picker-menu`**"
          "（portal 之后它不再是 `.popover-host` 的后代）",
          'closest(".popover-host, .picker-menu")' in app, "")

    # 负对照：上面那条「一个原生 select 都不剩」得能红。
    fake = _strip_js_comments(
        'if (row.kind === "choice") { return <select className="set-input" />; }')
    check("★★ 负对照：合成样本里那个原生 `<select>` 抓得住", "<select" in fake, "")


def _post_accepts(server_src: str) -> set:
    """`POST /api/settings` 收哪些键 —— 按 AST 取（不 grep）。

    三处都要取，少一处就会假红/假绿：
      ① `if "X" in data:`（显式分支）
      ② `settings["X"] = …`（写进去的那个名字）
      ③ 白名单循环：`for key in ("a","b",…): if key in data:` ——
         那一大片键（`cross_zone_enabled` / `stream_output` / …）只在元组里，
         按前两条取会一条都取不到，而那正是最容易看走眼的地方。
    """
    import ast
    tree = ast.parse(server_src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "api_save_settings")
    keys = set()
    #: `名字 -> 那个常量的真值`（只认 `from M import C as N` 里 C 是真的字符串常量
    #: 那一种；取不到就跳过 —— 不猜）。
    aliases = {}
    import importlib
    sys.path.insert(0, str(_ROOT))
    for n in ast.walk(fn):
        if isinstance(n, ast.ImportFrom):
            try:
                mod = importlib.import_module(n.module)
            except Exception:                                   # noqa: BLE001
                continue
            for al in n.names:
                v = getattr(mod, al.name, None)
                if isinstance(v, str):
                    aliases[al.asname or al.name] = v
    for n in ast.walk(fn):
        if isinstance(n, ast.Compare) and isinstance(n.left, ast.Constant) \
                and isinstance(n.left.value, str):
            for op in n.ops:
                if isinstance(op, (ast.In, ast.NotIn)):
                    keys.add(n.left.value)
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Subscript):
            t = n.targets[0]
            if isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str):
                keys.add(t.slice.value)
        if isinstance(n, ast.For) and isinstance(n.iter, ast.Tuple):
            for elt in n.iter.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    keys.add(elt.value)
            #: 元组的元素也可能又是一个元组（`for _k, _v in ((a, 1), (b, 2))`）。
            #:   少这一层就会把一整批键判成"不收"，而那看起来像真的漏了。
            for elt in n.iter.elts:
                if isinstance(elt, ast.Tuple) and elt.elts and                         isinstance(elt.elts[0], ast.Constant) and                         isinstance(elt.elts[0].value, str):
                    keys.add(elt.elts[0].value)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)                 and n.func.attr in ("get", "pop") and n.args                 and isinstance(n.args[0], ast.Constant)                 and isinstance(n.args[0].value, str):
            keys.add(n.args[0].value)
        #: 还有一处名字不是字面量的：`if _PRESET_KEY in data:`
        #:   —— 它来自 `from core.framework_preset import (… SETTING_KEY as _PRESET_KEY …)`。
        #:   按常量取取不到，而"我没查到"会被读成"它不收"。
        #:真去把那两个常量问出来（import 那个模块，不做任何别的事）。
        if isinstance(n, ast.Compare):
            for side in [n.left] + list(n.comparators):
                if isinstance(side, ast.Name) and side.id in aliases:
                    keys.add(aliases[side.id])
    return keys


def _get_returns() -> set:
    """`GET /api/settings` 真跑一次，回它交出来的那些键。

    为什么不能读源码猜：那个端点是 **`_read_settings()`（整个 `settings.json`）
      再加一堆补丁** —— 文件里本来就写着的键（`stream_output` / `memory_continuity`
      / `boot_on_startup` …）在源码里一个字都找不到，而它们照样会返回。
      按 AST 取会报出一批"GET 不给"的假红（筛子看不见 ≠ 它不存在）。
    只读：GET 不写任何东西（`data/settings.json` 只被读）。
    """
    import importlib
    sys.path.insert(0, str(_ROOT))
    S = importlib.import_module("web.server")
    S._LAUNCH_TOKEN = "test-token-w113-settings"
    S.app.config["TESTING"] = True
    r = S.app.test_client().get("/api/settings?token=test-token-w113-settings")
    assert r.status_code == 200, r.status_code
    return set((r.get_json() or {}).keys())


def _check_table_keys(view: str, server: str) -> None:
    """表里每个 `key` 都必须真接得上。

    判据：`key` 既要在 `GET /api/settings` 给得出来、又要被 `POST` 收。
    少了后半条，画了一个按了没反应的开关，而它长得跟真的一样
    （「标签在说谎」最典型的那种）。
    """
    rows = _table_rows(view)
    with_key = [r for r in rows if "key:" in r]
    check("★ 表读得出来（读不出 = 下面几条空转）",
          len(rows) >= 30 and len(with_key) >= 20, "rows=%d keys=%d" % (len(rows), len(with_key)))

    keys = set()
    for r in with_key:
        m = re.search(r'key:\s*"([^"]+)"', r)
        assert m, r[:80]
        rid = re.search(r'id:\s*"([^"]+)"', r)
        keys.add((rid.group(1) if rid else "?", m.group(1)))

    accepts = _post_accepts(server)
    gives = _get_returns()
    #: 那两颗「全部放行」故意不走 `/api/settings` —— 它们的状态是从名单里
    #:   `"*" in list_for(kind)` 算出来的（`core/allowlist.py` 判据 ②），
    #:   写侧走 `/api/allowlist`，把它们从"POST 收不收"那一问里摘出去，
    #:   同时另判一条（它们得走对端点）。
    #: 别假定"带 `allowlist:` 的行都有 `key`"：
    #:   「访问白名单」那一格（`kind: "allowlist"`）没有 `key` ——
    #:   它的真相是名单本身（`data/allowlist.json`），不是 `/api/settings`
    #:   里的一个键，它不该参与"POST 收不收 / GET 给不给"那一问。
    #:收窄成"有 key 的才进这个集合"。
    al_keys = {m.group(1) for r in rows if "allowlist:" in r
               for m in [re.search(r'key:\s*"([^"]+)"', r)] if m}
    bad_post = sorted(k for _i, k in keys
                      if k.split(".")[0] not in accepts and k not in al_keys)
    bad_get = sorted(k for _i, k in keys if k.split(".")[0] not in gives)
    check("★★★ 表里每一格的键都**被 `POST /api/settings` 收**"
          "（少一条就是「按了没反应的开关」）", not bad_post, "POST 不收：%r" % (bad_post,))
    check("★★ 而它们也都**由 `GET /api/settings` 给出来**（否则界面显示成空）",
          not bad_get, "GET 不给：%r" % (bad_get,))
    check("★ 两处都数到了东西（空集也能「通过」—— 那不算测到）",
          len(accepts) >= 12 and len(gives) >= 25,
          "accepts=%d gives=%d" % (len(accepts), len(gives)))

    #: 两颗「全部放行」故意不走 `/api/settings` —— 它们的状态是从名单里
    #:    `"*" in list_for(kind)` 算出来的（`core/allowlist.py` 判据 ②）
    #:它们有 `allowlist` 字段、没有 `key` 的写法... 而它们有 key（读侧）。
    #:    判据：带 `allowlist` 的那些，键必须也在 GET 里（读侧），而写走 `/api/allowlist`。
    al = [r for r in rows if "allowlist:" in r]
    check("★ 走 `/api/allowlist` 的那几格真打那个端点（不是 `/api/settings`）",
          len(al) >= 2 and "/api/allowlist" in (_APP.read_text(encoding="utf-8")),
          "found=%d" % len(al))

    #: `act` 那一族：每一颗按钮都要有一个真端点（同理，不然是个空按钮）。
    acts = sorted({m.group(1) for r in rows
                   for m in [re.search(r'act:\s*"([^"]+)"', r)] if m})
    check("★ `act` 那一族非空（否则下面那条空转）", len(acts) >= 4, repr(acts))
    #: 动作名 -> 它打的那个路由（一处表；组件里的 `run()` 按名字分派）
    routes = {"prune_history": "/api/file-history/prune",
              "check_update": "/api/update/check",
              "open_logs": "/api/logs",
              #: 打开数据目录 —— 走 `/api/reveal`（只开文件夹，
              #:   不执行任何东西）；路径由 `GET /api/settings.data_dir` 给。
              "open_data_dir": "/api/reveal",
              "restart": "/api/restart",
              # 三颗动作各自有落脚点：
              #   `clear_memory` 打 `/api/memory/clear`（它的 docstring 逐字写着
              #   「界面那边必须先问一次（5 秒倒计时）」）；
              #   `restore_defaults` 打 `/api/settings/restore-default`；
              #   `feedback` 不是一个 `client.call` —— 它把一个表单
              #   打开（`Feedback.jsx` 自己发 `POST /api/feedback`），这一条
              #   问的是"这个动作有没有落脚点"，而它有。
              "clear_memory": "/api/memory/clear",
              "restore_defaults": "/api/settings/restore-default",
              "open_issues": None,
              "open_project": None,
              #: `None` = 纯本机动作，没有端点 ——
              #   安全提示的「不再提醒」存在 GUI 自己的 `localStorage`
              #   （`crannia-app://` 与 WebUI 是两个 origin，服务端碰不到它）。
              #   这一格故意没有路由：给它编一个空端点等于把真相搬到服务端。
              "reset_notices": None,
              #: 「重新运行新手引导」也是纯客户端动作（打开覆盖层）。
              #:   它不碰 `data/.onboarding-done`，也不打任何端点。
              "onboarding_replay": None,
              #: 记忆库子页 —— 纯客户端动作（页面自己拉 `/api/memory` 一族）。
              "open_memory": None,
              #: 往期公告子页 —— 同样是纯客户端动作：内容在构建时就打进包里
              #:   （`docs/updates/*.txt` → `update-notice-content.js` 的 glob），
              #:   不读磁盘、没有端点。
              "open_notices": None,
              #: 自动审查配置子页 —— 纯客户端动作（数据来自 `settings`，
              #:   写走 `/api/settings`；见 `AutoRulesPanel.jsx`）。
              "open_auto_rules": None,
              #: 设置包导出 / 导入—— 纯客户端动作：
              #:   打开 `SettingsTransfer` 浮窗，真正的端点由它自己打
              #:   （`/api/settings/export` · `/api/settings/import`）。
              "export_settings": None,
              "import_settings": None}
    missing = [a for a in acts if a not in routes]
    check("★★ 每个动作名都在**那张路由表**里（拼错 = 按了没反应）", not missing, repr(missing))
    dead = [p for a, p in routes.items()
            if a in acts and p and ('"%s"' % p) not in server]
    check("★★ 而每个路由**真的在 `server.py` 里**（表里写着、服务端没有 = 空按钮）",
          not dead, repr(dead))

    #: 那几格「没做 / 在别处」必须带原因（`why`）—— 光写"尚未实现"用户不知道
    #:    是缺什么（拒绝要带路线）。
    flag = [r for r in rows if re.search(r'kind:\s*"(moved|notdone)"', r)]
    no_why = [r[:60] for r in flag if "why:" not in r]
    check("★★ 标了「没做 / 在别处」的**每一格都写了 `why`**（缺什么、在哪儿）",
          not no_why, repr(no_why))
    #: 这条是"不空转"的守卫，不是覆盖率棘轮 —— 它只要保证上面那条
    #:    （"每一格都写了 why"）有东西可查。接口行被接成真控件之后这一族会变少，
    #:    门槛跟着改小是有据的：那几行是"做完了"，不是"漏了"。
    #:    但它不许降到 0 —— 那才是这条守卫的失效点。
    #: 这一族现在是空的，门槛也是 0 —— 但「空」必须有出处：
    #:   那三格（`删除保护` / 「通知音量」/「导入导出」）都是变成了真功能，不是被删掉。
    #: 门槛不许"悄悄变成 0"（那正是这条守卫防的）；「导入导出」的两个动作在表里，可以核。
    if not flag:
        check("★★ 这一族空了，但「导入导出」是**做完了**（两个动作都在表里）",
              any('act: "export_settings"' in r for r in rows)
              and any('act: "import_settings"' in r for r in rows),
              "flag=0 且两个导出/导入动作都没找到")
    else:
        check("★ 而真有那几格（不是空族）", True)
    #: 「恒定生效、没有开关」那一族（的删除保护）：
    #:   表里得有它，而且 `App.jsx` 得真的渲染它（不是只挂了个 kind 名字）。
    info = [r for r in rows if re.search(r'kind:\s*"info"', r)]
    check("★ 「恒定生效」那一族真有行", len(info) >= 1, "info=%d" % len(info))
    app_src = _strip_js_comments(_APP.read_text(encoding="utf-8"))
    check("★ 而 App 真的渲染了 `kind: \"info\"`", 'row.kind === "info"' in app_src, "")

    #: 上级开关：`needs` 指的那个键必须也是表里某一条的 key（拼错 = 那一格永不出现）。
    all_keys = {k for _i, k in keys}
    needs = sorted({m.group(1) for r in rows
                    for m in [re.search(r'needs:\s*"([^"]+)"', r)] if m})
    orphan = [n for n in needs if n not in all_keys]
    check("★★ 每一格的 `needs` 指的**都是表里真有的那个开关**"
          "（拼错 = 那一格**永远不出现**，而没有任何东西会红）",
          not orphan, "指向了不存在的：%r" % (orphan,))
    check("★ 而 `needs` 真用上了（`设置.txt` 末尾那条规矩）", len(needs) >= 4, "needs=%d" % len(needs))

    #: 监听器那一格。
    #:   没有它时 `#02` 会告诉用户"某个监听没跑起来"，而 GUI 里连"监听器"
    #:   三个字都没有 —— 用户唯一的路是手改 `settings.json`，而那份文件
    #:   模型也读不到。一条会指向空处的横幅，比不弹更糟。
    check("★★ 监听器那一格在表里（`kind: \"watchers\"`）",
          'kind: "watchers"' in view, "")
    check("★★ 而 App 真的渲染了它（不是只挂了个 kind 名字）",
          'row.kind === "watchers"' in app_src, "")
    for _needle in ('"/api/watchers"',
                    '`/api/watchers/${encodeURIComponent(id)}/toggle`',
                    '`/api/watchers/${encodeURIComponent(id)}`'):
        check("★ 组件打的是 %s" % _needle, _needle in app_src, "")
    check("★★ 那三个路由后端真有（拼错 = 一颗按了没反应的按钮）",
          '@app.route("/api/watchers"' in server
          and '@app.route("/api/watchers/<watcher_id>/toggle"' in server
          and '@app.route("/api/watchers/<watcher_id>", methods=["DELETE"])' in server,
          "")
    #: 「开着」与「在跑」必须分开画 —— 目录不存在时正是"启用着、但一条都
    #:   不会触发"，只画一个开关就是在说这半句话。
    #: 条目画在 `ListEditor.jsx` 里（设置那一行只留摘要 + 一颗「设置」）。
    _ed = _strip_js_comments(
        (_ROOT / "packages" / "ui" / "src" / "ListEditor.jsx").read_text(encoding="utf-8"))
    check("★★ 「开着」与「在跑」是两个独立的显示（`wt-dot` 用 running）",
          "wt-dot" in _ed and "w.running" in _ed, "")


def main() -> int:
    print("=" * 62)
    print("Settings page: the instruction-file line ")
    print("=" * 62)

    app = _strip_js_comments(_APP.read_text(encoding="utf-8"))
    view = _strip_js_comments(_VIEW.read_text(encoding="utf-8"))
    server = _SERVER.read_text(encoding="utf-8")
    css = _CSS.read_text(encoding="utf-8")

    print("\n-- 1. 接线：设置页真有那一格、真用了判据模块 --")
    check("★ 设置页里有「项目指令文件」那一格（用户说的那「一行」）",
          "项目指令文件" in app, "")
    check("★★ 用的是判据模块（不是把文案/千分位内联在 JSX 里）",
          'from "./settings-view.js"' in app and "leafNotice(" in app, "")
    check("★ 数据来自后端（不是界面自己算一个）",
          "settings.leaf_md" in app, "")
    check("★★ 那条判据是**纯函数**（组件里不自己拼千分位）",
          "export function leafNotice" in view and "withCommas" in view, "")

    print("\n-- 2. 后端真的把它交出去了，且读不到 != 0 --")
    check("★★ `/api/settings` 真的带上了 `leaf_md`",
          '"leaf_md"' in server and "leaf_md_status" in server, "")
    check("★★ **读不到 != 0**：多带一个 `loaded` 让界面分得开",
          '"loaded"' in server and "bool(_leaf.get(\"files\"))" in server, "")
    _lm = (_ROOT / "core" / "leaf_md.py").read_text(encoding="utf-8")
    check("★ 判据（超没超）**由后端算**（`total > WARN_CHARS`），界面不自己比两个数",
          "total > WARN_CHARS" in _lm, "")

    print("\n-- 2b. 后端那个快照函数回得出那几格（当场调一次，不是读源码猜）--")
    try:
        sys.path.insert(0, str(_ROOT))
        from core.prompt_assembler import leaf_md_status
        snap = leaf_md_status()
        check("★ `leaf_md_status()` 回 `files/chars/warn_chars/over` 四格",
              {"files", "chars", "warn_chars", "over"} <= set(snap), repr(snap))
    except Exception as exc:                                    # noqa: BLE001
        check("★ `leaf_md_status()` 调得动", False, "%s: %s" % (type(exc).__name__, exc))

    print("\n-- 3. 告警色在 CSS 里有定义（写对了名字没写样式 -> 什么都不报）--")
    check("★ `.setting .warn` 有定义（且**不是**危险红 —— 它是提示不是错）",
          ".setting .warn" in css and "#9a6b12" in css, "")

    print("\n-- 4. node 在不在（不在就红，不许静默跳过）--")
    node = shutil.which("node")
    if not node:
        check("node 在 PATH 上", False,
              "node NOT on PATH -- this guard CANNOT run (and must not pass silently)")
        print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
        return 1
    print("  [ok] node = %s" % node)

    print("\n-- 5. node 自检（还没装载 / 没超 / 超了，三格逐条）--")
    r = subprocess.run([node, str(_JSTEST)], capture_output=True, text=True,
                       encoding="utf-8", cwd=str(_ROOT), timeout=60)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    check("node 自检全过", r.returncode == 0, out[-400:])
    print("      [node] " + (out.splitlines()[0] if out else "(no output)"))

    print("\n-- 6. `App.jsx` / `settings-view.js` 编译得过 --")
    script = (
        "const ps = process.argv.slice(1);"
        "import('vite').then(async (v) => {"
        "  const fs = await import('node:fs');"
        "  for (const p of ps) {"
        "    const lang = p.endsWith('.jsx') ? 'jsx' : 'js';"
        "    await v.transformWithOxc(fs.readFileSync(p, 'utf8'), p, { lang });"
        "  }"
        "  console.log('JSX OK');"
        "}).catch(e => { console.log('JSX FAIL: ' + String(e.message).split('\\n')[0]);"
        "                process.exit(1); });"
    )
    r = subprocess.run([node, "-e", script, str(_APP), str(_VIEW)], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(_ROOT), timeout=120)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    check("★ 两个文件都编译得过", r.returncode == 0 and "JSX OK" in out, out[-300:])

    print("\n-- 7. 负对照 --")
    bad = app.replace("leafNotice(", "")
    check("★ 负对照：把判据模块那处调用删掉 -> 第 1 节那条判据能红",
          "leafNotice(" not in bad, "")
    bad2 = server.replace('settings["leaf_md"] = _leaf', "")
    check("★ 负对照：后端那一行删掉 -> 第 2 节那条判据能红",
          ('settings["leaf_md"] = _leaf' not in bad2), "")

    print("\n-- 8. 批次 4：**表里每一格都接在一条真键上** --")
    _check_table_keys(view, server)

    print("\n-- 9.：保留期与上限那句说明--")
    _check_g9_note(view, app)

    print("\n-- 10.：设置页那七格--")
    _check_w180(view, app)

    print("\n-- 11. 表里每个可见字段都有英文译文 --")
    _check_row_i18n(view)

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
