# -*- coding: utf-8 -*-
"""记忆库查看 / 编辑界面 —— 经过 HTTP 层的读者。

## 为什么要有它（与 `test_allowlist_ui.py` 同一条理由）

前端的坏法全在缝里：

    JS 打 `/api/memory/entries`（复数）而端点叫 `/api/memory/entry`  -> 404，静默
    `getElementById('memory-f-descr')` 少一个字母                     -> 永远 null，静默
    服务端返回 `{"success": false}` 而前端只看 HTTP 码                 -> "看起来保存了"

三处各自都"写对了"，合起来不工作，而没有任何东西会红。
这里起一个真的 Flask test client，打真的 HTTP，断言真的 JSON；
并且按模板反查 JS 里那些 id 与端点名。

## 钉住的判据

1. 删除是一条明确的动作：删掉的那条要进封条账本（tombstone），
   而抽取/整理都不许把它重新生成。
这条不测"接口返回 200"，测的是删完之后 `write_entry` 同名真的会被拒。
2. 超限一律拒（不静默砍）—— 让模型/用户以为写进去了是最坏的一种"成功"。
3. 前端零 `innerHTML`：记忆正文是模型写进去的文本，拼进 `innerHTML`
   就是把模型的输出变成真的节点（持久化 XSS）。按函数体查（剥注释）。
4. id 与端点名两边对得上（这是这个文件存在的主要理由）。

全程 `sandbox()` + 首尾指纹机检。输出一律 ASCII。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_memory_ui.py
"""

from __future__ import annotations

import json
import re
import sys
from urllib.parse import quote
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from _sandbox import prod_diff, prod_snapshot, sandbox  # noqa: E402

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


def _boot():
    import web.server as S
    S.app.config["TESTING"] = True
    return S, S.app.test_client(), S._LAUNCH_TOKEN


def _get(c, path, token):
    # 拼查询串要看 path 里有没有 `?` —— 无条件加 `?token=` 会变成
    #    `/api/memory/entry?name=X?token=Y`，把 token 变成 name 的一部分，
    #    服务端读不到 token，403，而症状看起来像"鉴权坏了"。
    sep = "&" if "?" in path else "?"
    return c.get(path + sep + "token=" + token)


def _post(c, path, token, body):
    sep = "&" if "?" in path else "?"
    return c.post(path + sep + "token=" + token, json=body)


def _reset() -> None:
    """清空记忆库与账本 —— 每个用例从空库开始。

    它们是相对路径（sandbox 的 chdir 把它们钉在临时目录），但在整个文件里是同一个
不清的话，上一个用例写的条目会活到下一个用例，表现为"断言挂了但代码没错"。
    （`test_allowlist_ui` 在名单那边有同样的清空要求。）
    """
    from core import memory_store as MS
    for p in MS.FACTS_ROOT.rglob("*.md"):
        p.unlink()
    if MS.TOMBSTONES_PATH.exists():
        MS.TOMBSTONES_PATH.unlink()


def _mk(c, token, name, desc="一句钩子", content="正文内容"):
    return _post(c, "/api/memory/entry", token,
                 {"name": name, "description": desc, "content": content})


# ----------------------------------------------------------------------
# 1. 鉴权与形状
# ----------------------------------------------------------------------

def test_auth_and_shape() -> None:
    print("\n-- 1. 端点存在、要 token、形状对 --")
    _S, c, token = _boot()
    _reset()
    check("没带 token -> 403（不是 200 空表）",
          c.get("/api/memory").status_code == 403)

    r = _get(c, "/api/memory", token)
    check("带 token -> 200", r.status_code == 200, str(r.status_code))
    d = r.get_json() or {}
    for k in ("ok", "entries", "tombstones", "limits"):
        check("形状里有 %r" % k, k in d, repr(sorted(d)))
    check("entries / tombstones 都是 list",
          isinstance(d.get("entries"), list) and isinstance(d.get("tombstones"), list),
          repr(d))
    for k in ("name", "description", "body"):
        check("limits 里有 %r" % k, k in (d.get("limits") or {}),
              repr(d.get("limits")))


# ----------------------------------------------------------------------
# 2. 增 / 改 / 读
# ----------------------------------------------------------------------

def test_write_read_roundtrip() -> None:
    print("\n-- 2. 写一条 -> 列表里看得见 -> 点开读得回全文 --")
    _S, c, token = _boot()
    _reset()
    r = _mk(c, token, "咖啡偏好", "用户爱喝手冲", "手冲，浅烘。")
    check("POST entry -> 200 + ok", r.status_code == 200
          and (r.get_json() or {}).get("ok") is True, str(r.get_json()))
    check("回执说清是新建还是更新",
          (r.get_json() or {}).get("created") is True, str(r.get_json()))

    d = _get(c, "/api/memory", token).get_json()
    names = [e["name"] for e in d["entries"]]
    check("★ 列表里出现了它", names == ["咖啡偏好"], repr(names))
    e = d["entries"][0]
    check("列表**不带正文**（只给字符数）",
          "content" not in e and e.get("chars") == len("手冲，浅烘。"), repr(e))

    d2 = _get(c, "/api/memory/entry?name=" + quote("咖啡偏好"), token).get_json()
    check("★ 点开能读到全文", d2.get("content") == "手冲，浅烘。", repr(d2))

    # 同名再写 = 更新（不是新增）
    r2 = _mk(c, token, "咖啡偏好", "改了钩子", "改成深烘。")
    check("同名再写 -> 更新（created=False）",
          (r2.get_json() or {}).get("created") is False, str(r2.get_json()))
    d3 = _get(c, "/api/memory", token).get_json()
    check("  条目数还是 1（没写重）", len(d3["entries"]) == 1, repr(d3["entries"]))

    r404 = _get(c, "/api/memory/entry?name=" + quote("没有这条"), token)
    check("读不存在的 -> 404", r404.status_code == 404, str(r404.status_code))


def test_limits_are_rejected_not_truncated() -> None:
    print("\n-- 3. ★ 超限**拒**，不静默砍 --")
    _S, c, token = _boot()
    _reset()
    from core import memory_store as MS
    r = _mk(c, token, "标题" * 30, "钩子", "正文")   # name > 40
    check("name 超限 -> 400", r.status_code == 400, str(r.status_code))
    check("  而且说清限额", str(MS.MAX_NAME_CHARS) in str(r.get_json()),
          str(r.get_json()))
    r = _mk(c, token, "标题", "钩子" * 80, "正文")   # description > 120
    check("description 超限 -> 400", r.status_code == 400, str(r.status_code))
    r = _mk(c, token, "标题", "钩子", "x" * (MS.MAX_BODY_CHARS + 1))
    check("正文超限 -> 400", r.status_code == 400, str(r.status_code))
    check("★ 三次拒绝之后库还是空的（没有半截写入）",
          _get(c, "/api/memory", token).get_json()["entries"] == [])


# ----------------------------------------------------------------------
# 4. 删除 = 用户动作 + 封条真的挡得住重新生成
# ----------------------------------------------------------------------

def test_delete_writes_tombstone_and_blocks_regeneration() -> None:
    print("\n-- 4. ★ 删除进封条账本，而封条真的挡住抽取/整理重新生成 --")
    _S, c, token = _boot()
    _reset()
    from core import memory_store as MS
    _mk(c, token, "临时决定", "一句钩子", "正文")

    r = _post(c, "/api/memory/delete", token, {"name": "临时决定"})
    check("删 -> 200 + ok", r.status_code == 200 and (r.get_json() or {}).get("ok"),
          str(r.get_json()))
    d = _get(c, "/api/memory", token).get_json()
    check("从条目列表里消失了", d["entries"] == [], repr(d["entries"]))
    check("★ 进了封条账本", [t.get("name") for t in d["tombstones"]] == ["临时决定"],
          repr(d["tombstones"]))
    check("★ 文件真的没了（不是只从列表里藏起来）",
          not list(MS.FACTS_ROOT.rglob("*.md")), "facts 下还有文件")

    # 这条才是这个机制存在的全部理由：抽取器/巩固器都走 write_entry
    again = MS.write_entry("临时决定", "又想写回来", "抽取器想把它生成回来")
    check("★★ 同名再写被**执行侧拒**（封条挂在写路径上）",
          again.get("success") is False, str(again))
    check("  而且说清「这是用户的意图，不是技术故障」",
          "删" in str(again.get("error", "")), str(again))

    r2 = _post(c, "/api/memory/delete", token, {"name": "没有这条"})
    check("删不存在的 -> 400（不是静默成功）", r2.status_code == 400,
          str(r2.status_code))


def test_restore_unseals_but_does_not_bring_content_back() -> None:
    print("\n-- 5. 解除封条：名字能再写，但**内容不回来** --")
    _S, c, token = _boot()
    _reset()
    from core import memory_store as MS
    _mk(c, token, "误删的", "钩子", "正文")
    _post(c, "/api/memory/delete", token, {"name": "误删的"})

    r = _post(c, "/api/memory/restore", token, {"name": "误删的"})
    check("解除 -> 200 + ok", r.status_code == 200 and (r.get_json() or {}).get("ok"),
          str(r.get_json()))
    d = _get(c, "/api/memory", token).get_json()
    check("账本里没了", d["tombstones"] == [], repr(d["tombstones"]))
    check("★ 条目**没有**自己回来（内容删除时就没了）", d["entries"] == [],
          repr(d["entries"]))
    check("★ 但名字解封了：现在写得进去",
          MS.write_entry("误删的", "重写一遍", "内容得重新写").get("success") is True)
    check("再解除一次 -> 400（不存在的封条）",
          _post(c, "/api/memory/restore", token, {"name": "误删的"}).status_code == 400)


def test_write_switch_is_not_bypassed_by_the_ui() -> None:
    print("\n-- 6. ★ 写记忆开关关着时，界面**不绕过**它 --")
    _S, c, token = _boot()
    _reset()
    p = Path("data/settings.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"memory_write": False}), encoding="utf-8")
    r = _mk(c, token, "写不进去", "钩子", "正文")
    check("写端点 -> 400", r.status_code == 400, str(r.status_code))
    body = str(r.get_json())
    check("★ 回执给的是**路线**（去设置页打开），不是含糊的失败",
          "设置" in body and "memory_write" in body, body)
    check("库里还是空的", _get(c, "/api/memory", token).get_json()["entries"] == [])


# ----------------------------------------------------------------------
# 7. 缝：id 与端点名两边对得上
# ----------------------------------------------------------------------

_TEMPLATE = _ROOT / "web" / "templates" / "security.html"


def _strip_js_comments(src: str) -> str:
    """剥掉 `//` 与 `/* */` —— 只在字符串外面剥。

    不剥的话，注释里那句「零 innerHTML」自己就会被判成违规
    （`test_allowlist_ui` 有同样的约束）。

    用两句朴素的正则去剥，会吃掉字符串里的那两个序列。反例：

        const ACCEPT = "image/*,application/pdf,text/*,…";

    那个 `/*` 被当成注释开头，正则一路吃到**后面最近的一个 `*/`**。
    若附近恰好有一个 docblock 的 `*/`，只吞掉一小段；
    若最近的 `*/` 跑到三千字符以外，一整段源码被吞，
    `toAttachment` / `openPicker` 全"不见了"，
    而报错长得像代码被删了（报错说的地方未必是病因）。

判据：这一层的输入里有字符串，而正则没有词法作用域 —— 只能自己走一遍。
    已知边界：它不认正则字面量（用斜杠包起来的那些）—— 本仓这几个文件里没有，
    有了要另外说一声（那需要真正的前后文判断，不是几行能收的）。
    它也不删内容：注释只是被跳过，其余字符逐字保留（含换行）。
    """
    out: list = []
    i, n = 0, len(src)
    quote = ""
    while i < n:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:          # 转义：连下一个字符一起吃掉
                out.append(src[i + 1]); i += 2; continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'`":
            quote = ch; out.append(ch); i += 1; continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            if j == -1:
                break                              # 到最后一行都是注释
            i = j                                  # 换行本身留着（行号因此还对得上）
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            end = (j + 2) if j != -1 else n
            # 块注释里面的换行要留：留了之后"第 N 行"与原文对得上
            #   （整块连换行一起删，块注释之后的行号会全部漂掉 ——
            #   而任何按行数的东西都会静默指错地方）。
            out.append("\n" * src.count("\n", i, end))
            i = end
            continue
        out.append(ch); i += 1
    return "".join(out)


def _js_function_body(src: str, name: str) -> str:
    """按函数体取（不按全文 grep）—— 这个文件里别处仍在用别的写法。

    「签名之后第一个 `{`」是错的，而且错得很安静：那对
    `function f(a, b)` 是对的，但参数解构
    （`export function Drawer({ panel, onClose })`）时，第一个
    `{` 是参数表里那个，取出来的"函数体"其实是参数表本身（十几个字符），
    于是「里面没有 `addEventListener`」这类断言全部恒真。
先跳过配平的参数表，再找函数体。
    同族：断言在基线里就是绿的，它把每个退化都"抓住"（恒真）。
    """
    m = re.search(r"(?:async\s+)?function\s+%s\s*\(" % re.escape(name), src)
    if not m:
        return ""
    # 跳过参数表：从 `(` 开始配平到它那一只 `)`
    depth, k = 0, m.end() - 1
    while k < len(src):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    i = src.index("{", k)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    return src[i:]


def test_strip_js_comments_does_not_eat_strings() -> None:
    """自造靶子 —— `_strip_js_comments` 不许吃掉字符串里的那两个序列。

    ## 它为什么必须有这一条

    这个函数是共用件（`test_main_ui` · `test_desktop_ipc` · `test_desktop_mode` …
    一大票都在用它）。朴素的正则会吃掉字符串里的 `/*`：
    典型例子是 `Composer.jsx` 的 `ACCEPT = "image/*,application/pdf,…"` ——
    那个 `/*` 被当成注释开头，一路吃到**后面最近的一个 `*/`**。
    这类错误很不容易显形：若紧接就有一个 docblock 的 `*/`，
    只吞掉一小段、代码没伤着；若最近的 `*/` 在三千字符以外
一整段源码被吞，而报错长得像代码被删了。

判据：这一层的输入里有字符串。下面三条靶子是合成的
    （真实那份在 `Composer.jsx` 里，而真实靶子会被别人改 —— 合成靶子不会）。
    """
    # ① `image/*` 那种 MIME 通配 —— 真实例
    s1 = 'const A = "image/*,text/*";\nconst b = 1; // 注释\nconst c = 2;'
    out = _strip_js_comments(s1)
    assert '"image/*,text/*"' in out, "字符串里的 `/*` 被当成注释开头了：%r" % out
    assert "// 注释" not in out, "真注释没被剥掉：%r" % out
    assert "const c = 2;" in out, "真注释之后的内容被吞了：%r" % out

    # ② `http://` —— 同一个病的另一面（`//` 那一支）
    s2 = 'const u = "http://127.0.0.1:5000";\nconst d = 3;'
    out2 = _strip_js_comments(s2)
    assert '"http://127.0.0.1:5000"' in out2, "字符串里的 `//` 被当成注释开头了：%r" % out2
    assert "const d = 3;" in out2, "那一行之后的内容被吞了：%r" % out2

    # ③ 真块注释照旧要剥（别为了修 ①② 把它关掉了），且换行留住（行号还对得上）
    s3 = "const e = 1;\n/* 一段\n块注释 */\nconst f = 2;"
    out3 = _strip_js_comments(s3)
    assert "块注释" not in out3 and "const f = 2;" in out3, repr(out3)
    assert out3.count("\n") == s3.count("\n"), "换行被吃掉了（行号会漂）：%r" % out3

    # ④ 转义与模板串：`\` 后面那个引号不结束字符串
    s4 = 'const g = "a\\"/*b*/c";\nconst h = 4;'
    out4 = _strip_js_comments(s4)
    assert "const h = 4;" in out4 and "/*b*/" in out4, repr(out4)


def test_js_function_body_handles_destructured_params() -> None:
    """自造靶子 —— `_js_function_body` 对参数解构的函数要抽对。

    取"签名之后第一个 `{`"会安静地假绿：`export function Drawer({ panel, onClose })`
    的第一个 `{` 是参数表里那个，抽出来的"函数体"其实是参数表，
    「里面没有 `addEventListener`」这类断言全部恒真。
    真实靶子（`Rail.jsx` 的 `Drawer`）确实有解构参数，所以它一抽就错 ——
    但没有反例就没有判别力：这条用合成源码把
    "解构参数 + 体内有标记"这个形状钉死，下次再改这个抽取器它会红。
    """
    print("")
    print("-- 自造靶子：函数体抽取要跨过参数解构 --")
    # 靶子写成单行 —— 免得在报告里再跟转义符打一次架。
    src = 'export function Drawer({ panel, onClose, client }) { const MARK = "BODY_MARK"; }'
    body = _js_function_body(src, "Drawer")
    check("抽出来的体里有体内的标记", "BODY_MARK" in body, repr(body))
    check("★ 而且**不含参数表**（参数名一个都不该在里面）",
          "onClose" not in body, repr(body))
    plain = _js_function_body("function f(a, b) { return a + b; }", "f")
    check("普通签名照样抽得对（没被这次修坏）",
          "return a + b;" in plain, repr(plain))


def test_memory_renderers_never_use_innerHTML() -> None:
    print("\n-- 7. ★ 记忆那几个渲染函数里**零 innerHTML** --")
    raw = _TEMPLATE.read_text(encoding="utf-8")
    src = _strip_js_comments(raw)
    for fn in ("memoryLoad", "memoryRow", "memoryTombRow", "memoryEdit", "memorySave"):
        body = _js_function_body(src, fn)
        check("%s 里没有 innerHTML（剥注释之后）" % fn, body and "innerHTML" not in body,
              body[:160])


def test_js_ids_and_endpoints_exist_in_the_template_and_routes() -> None:
    print("\n-- 8. ★★ 缝：JS 里的 id / 端点名两边都对得上 --")
    raw = _TEMPLATE.read_text(encoding="utf-8")
    declared = set(re.findall(r'id="([^"]+)"', raw))
    used = set(re.findall(r"getElementById\('([^']+)'\)", raw))
    missing = sorted(i for i in used if i.startswith("memory-") and i not in declared)
    check("★ JS 里 getElementById 的每个 memory-* id 模板里都有", not missing,
          repr(missing))

    _S, _c, _t = _boot()
    import web.server as S
    routes = {str(r.rule) for r in S.app.url_map.iter_rules()}
    called = set(re.findall(r"api\('(/api/memory[^']*)'", raw))
    bare = {p.split('?')[0] for p in called}
    missing_routes = sorted(p for p in bare if p not in routes)
    check("★ JS 打的每个 /api/memory* 端点都真的注册了", not missing_routes,
          "JS 打了 %r，已注册 %r" % (sorted(bare), sorted(routes)))
    check("带了查询串的形态（`?name=` 那种）也算得对",
          "/api/memory/entry" in bare, repr(sorted(called)))


# ----------------------------------------------------------------------
# 9. 清空记忆库
# ----------------------------------------------------------------------

def test_clear_empties_the_library_and_seals_every_name() -> None:
    print("\n-- 9. ★ 清空 = 全删 + 全落封条 --")
    _S, c, token = _boot()
    _reset()
    from core import memory_store as MS
    for i in range(3):
        _mk(c, token, "条目%d" % i)
    check("起点 3 条",
          len(_get(c, "/api/memory", token).get_json()["entries"]) == 3)

    r = _post(c, "/api/memory/clear", token, {})
    check("清空 -> 200 + ok",
          r.status_code == 200 and (r.get_json() or {}).get("ok"), str(r.get_json()))
    body = r.get_json() or {}
    check("回执带 count=3", body.get("count") == 3, str(body))

    d = _get(c, "/api/memory", token).get_json()
    check("条目全没了", d["entries"] == [], repr(d["entries"]))
    check("★ 三条**全部**落进封条账本",
          sorted((t.get("name") or "") for t in d["tombstones"])
          == ["条目0", "条目1", "条目2"], repr(d["tombstones"]))
    check("★ 文件真的没了（不是只从列表里藏起来）",
          not list(MS.FACTS_ROOT.rglob("*.md")), "facts 下还有文件")

    # 这一条才是这件事的全部理由（与第 4 条同一条判据）：
    #    只删文件不落封条，那批名字下一条抽取还能重生。
    for i in range(3):
        again = MS.write_entry("条目%d" % i, "又想写回来", "抽取器想把它生成回来")
        check("★★ 「条目%d」同名再写被拒（封条挂在写路径上）" % i,
              again.get("success") is False, str(again))


def test_clear_on_an_empty_library_is_a_clean_zero() -> None:
    """「本来就空」与「删的时候出错了」要分得开。"""
    _S, c, token = _boot()
    _reset()
    r = _post(c, "/api/memory/clear", token, {})
    check("空库清空 -> 200（不是错误）", r.status_code == 200, str(r.status_code))
    body = r.get_json() or {}
    check("count=0 且 failed 是**空表**（不是缺这一格）",
          body.get("count") == 0 and body.get("failed") == [], str(body))


def test_clear_writes_the_ledger_once_not_once_per_entry() -> None:
    """原子性判据：账本一趟只写一次。

    循环调 `delete_entry` 的话，每条都会 `load_tombstones()` +
    `_save_tombstones()`，而后者是整表重写（写 tmp + `os.replace`）
N 条 = N 次写盘、N 个中间态（中途崩一下停在"删了一半、账本记了一半"）。
    判据落在调用次数上，不落在"看起来快了"。
    """
    _S, c, token = _boot()
    _reset()
    from core import memory_store as MS
    for i in range(4):
        _mk(c, token, "批%d" % i)

    seen: list[int] = []
    real = MS._save_tombstones
    MS._save_tombstones = lambda tob: (seen.append(len(tob)), real(tob))[1]
    try:
        r = _post(c, "/api/memory/clear", token, {})
    finally:
        MS._save_tombstones = real

    check("清空成功", r.status_code == 200, str(r.status_code))
    check("★★ 4 条只写盘 1 次", len(seen) == 1, repr(seen))
    check("  而写的那一次里 4 条都在（不是分 4 次各写一条）", seen == [4], repr(seen))


def test_clear_button_and_countdown_are_wired() -> None:
    """接线：一个没人能按的端点等于没有。

    这一格是不可逆的，界面上必须先停一下（5 秒倒计时），
    且那套要与 ICE 总闸那份同形（本仓对"不可逆动作"只有一种做法）。
    """
    raw = _TEMPLATE.read_text(encoding="utf-8")
    check("★ 界面上有那个按钮", 'id="memory-btn-clear"' in raw)
    check("★ 它打的是清空端点", "api('/api/memory/clear'" in raw)
    check("★ 有确认弹窗", 'id="memory-clear-modal"' in raw)
    check("★ 有 5 秒倒计时（同一个形状：按钮先禁用、数到 0 才可点）",
          'id="memory-clear-secs"' in raw
          and "memory-clear-confirm" in raw)
    check("★ 弹窗里写明了**不可逆**与**封条只保证不重生**",
          "内容不会保留" in raw and "不会自己回来" in raw)
    check("★ 关掉弹窗要能把计时器停掉（否则下次打开从半路开始数）",
          "function closeMemoryClear" in raw and "clearInterval(_memClearTimer)" in raw)
    # 复刻 ICE 那一套的形状（两个数都要是 5 —— 不是硬编一个数字给测试看，
    # 而是两份实现不许漂）
    check("★ 倒计时起点也是 5（与 ICE 总闸一致）",
          raw.count("let left = 5;") == 2, str(raw.count("let left = 5;")))


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------

def _main() -> int:
    global _FAIL
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    before = prod_snapshot()
    with sandbox():
        for _name, fn in tests:
            try:
                fn()
            except AssertionError as exc:
                check(_name, False, "AssertionError: %s" % exc)
            except Exception as exc:  # noqa: BLE001
                check(_name, False, "%s: %s" % (type(exc).__name__, exc))
    leaked = prod_diff(before, prod_snapshot())
    if leaked:
        _FAIL += 1
        print("\n  [FAIL] ★ 测试污染了生产目录：%r" % (leaked,))
    # 汇总行的形状要和 `scripts/check_progress.py` 认的那三个正则之一对得上
    #    （`^(\d+)/(\d+) passed` / `Results: N passed` / `^(\d+) tests: N passed`）
    #    —— 写成 "N/M checks passed" 会让 `run` 锚点永远红（它匹配不上汇总行）。
    print("\n%d/%d passed" % (_PASS, _PASS + _FAIL))
    if _FAILED:
        print("失败项：")
        for x in _FAILED:
            print("  - " + x)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(_main())
