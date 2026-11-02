# -*- coding: utf-8 -*-
"""前端里斜杠开头的资源路径的机械读者。

## 为什么要有它

桌面壳若用 `loadFile()` 从 `file://` 加载页面，`/static/img/logo.svg`
会解析成 `file:///static/img/logo.svg`（盘根目录），404，图变成裂图，
而页面其余部分一切正常。两种表现：

| # | 位置 | 症状 |
|---|---|---|
| 1 | `dist/index.html` 里的 `src="/assets/index-*.js"`（vite 默认 `base:"/"`） | 整个应用一片空白（JS 没加载） |
| 2 | `App.jsx` / `Welcome.jsx` 里的 `src="/static/img/logo.svg"` | 欢迎页上一个裂图，其余正常 |

没有机械读者的清单会烂，把这条判据钉下来。

页面改走自定义协议 `crannia-app://` 之后 `file://` 整类退场，
而那个 scheme 解析得对、壳还会把 `/static/…` 转给后端，裸斜杠不再致命。
判据留着，理由换了：资源的 base 只许一处（`client.asset(...)`）——
散出去的那些，下次换壳看起来只是"图没出来"，不会报错。
见 `packages/ui/src/api.js::asset` 顶上那段。

## 钉什么

资源路径只许出现在 `client.asset(...)` 里 —— 那正是壳注入 base 的那道缝
（`packages/ui/src/api.js`：*本模块不知道自己在哪个壳里跑*）。

`/api/...` 不在管辖内：它们走 `client.call(...)`，那边本来就拼
`baseUrl`（而且它们看起来一样是斜杠开头 —— 所以这条判据必须只认资源前缀，
不然会把一整套正确的 API 调用判红）。

## 自证

`test_the_guard_can_go_red` 拿合成的坏样本验它抓得住；另一条拿好样本
验它不误伤 `client.asset(...)` 与 `/api/...`（成对负对照）。

用法：
    .venv/Scripts/python.exe tests/test_desktop_assets.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))

from _sandbox import prod_diff, prod_snapshot, sandbox  # noqa: E402
from test_memory_ui import _strip_js_comments  # noqa: E402

#: 前端源码的根（只管会被 vite 打进 bundle 的那些）。
#: 不含 `apps/desktop/index.html` —— 那是 vite 的开发入口，
#: 里面 `/src/main.jsx` 是给 dev server 看的，构建时会被重写成相对路径。
_SRC_DIRS = [_ROOT / "packages" / "ui" / "src", _ROOT / "apps" / "desktop" / "src"]

#: 会被解析成"站点根"的资源前缀。故意不含 `api`（见模块 docstring）。
_RES_PREFIXES = ("static", "assets", "img", "fonts", "css", "js")

#: 一个斜杠开头的字符串字面量（单引号 / 双引号 / 反引号都算）。
_LITERAL_RE = re.compile(r"""(["'`])(/(?:%s)[^"'`]*)\1""" % "|".join(_RES_PREFIXES))
#: CSS 的 `url(...)` 可以不加引号 —— 上面那条只认带引号的，
#: 于是漏掉了 `background: url(/assets/bg.png);`。漏的方向正好是"看起来干净"。
_URL_RE = re.compile(r"url\(\s*(/(?:%s)[^)\s\"']*)" % "|".join(_RES_PREFIXES))
#: 允许它的唯一上下文：紧挨着的 `client.asset(` / `.asset(` 调用。
_ALLOW_BEFORE = re.compile(r"\.asset\(\s*$")


def _iter_sources():
    for d in _SRC_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.suffix in (".jsx", ".js", ".css", ".ts", ".tsx"):
                yield p


def _find_bad(src_stripped: str):
    """返回 `[(literal, 前 40 字符)]` —— 那些不在 `.asset(` 里的绝对资源路径。"""
    out = []
    for m in _LITERAL_RE.finditer(src_stripped):
        before = src_stripped[max(0, m.start() - 60):m.start()]
        if _ALLOW_BEFORE.search(before):
            continue
        out.append((m.group(2), before[-40:].replace("\n", " ")))
    # CSS 的 `url(...)`（可以不带引号）—— 见 `_URL_RE` 上面的 。
    for m in _URL_RE.finditer(src_stripped):
        out.append((m.group(1), "url("))
    return out


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


def test_no_bare_resource_paths_in_the_frontend():
    print("\n-- 1. 前端源码里没有裸的斜杠开头资源路径 --")
    files = list(_iter_sources())
    check("setup: 扫到了前端源文件", len(files) >= 8, "%d 个" % len(files))
    bad = []
    for p in files:
        txt = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix == ".css":
            # CSS 走 `url(...)`，`_strip_js_comments` 对它是无害的（它只剥 // 与 /* */）
            body = _strip_js_comments(txt)
        else:
            body = _strip_js_comments(txt)
        for lit, ctx in _find_bad(body):
            bad.append("%s: %s   (前面是: %s)" % (p.relative_to(_ROOT), lit, ctx))
    check("★ 一个都没有（`file://` 下它们会变成盘根目录 -> 裂图 / 白屏）",
          not bad, "\n      ".join(bad))


def test_the_data_file_is_loaded_relatively():
    print("\n-- 2. 启动小窗的素材是**相对**引的 --")
    p = _ROOT / "apps" / "desktop" / "splash.html"
    src = p.read_text(encoding="utf-8")
    check("★ `<script src=\"splash-data.js\">`（不是 `/…`）",
          re.search(r'<script\s+src="splash-data\.js"', src) is not None,
          "裸斜杠的话，file:// 下同样会 404")
    check("★ 而且**是经典脚本**，不是 `type=\"module\"`",
          not re.search(r'<script[^>]*\bsrc="splash-data\.js"[^>]*type=', src),
          "—— 虽然实测 Electron 里 module 也能读（见 G19），但经典脚本少一层依赖")


def test_the_guard_can_go_red_and_does_not_overreach():
    """成对负对照：坏样本要红，好样本（`asset(...)` / `/api/...`）不许红。"""
    print("\n-- 3. 判据本身：抓得住坏的、不误伤好的 --")
    bad = [
        'const LOGO = "/static/img/logo.svg";',
        '<img src="/static/img/logo.svg" />',
        "background: url(/assets/bg.png);",
        '<link rel="stylesheet" href="/css/x.css">',
    ]
    for s in bad:
        hit = _find_bad(_strip_js_comments(s))
        check("坏样本被抓：%s" % s[:44], len(hit) == 1, repr(hit))

    good = [
        'const LOGO = client.asset("/static/img/logo.svg");',
        'await client.call(`/api/threads?limit=${THREAD_LIST_LIMIT}`);',
        'await client.call("/api/messages?storage_id=x");',
        'import { App } from "@crannia/ui";',
        '// 注释里写 /static/img/logo.svg 不算 —— 它会被剥掉',
        'const p = "/help";',                      # 不是资源前缀，不管
    ]
    for s in good:
        hit = _find_bad(_strip_js_comments(s))
        check("好样本放行：%s" % s[:44], not hit, repr(hit))


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
    print("\n%d/%d passed" % (_PASS, _PASS + _FAIL))
    if _FAILED:
        print("失败项：")
        for x in _FAILED:
            print("  - " + x)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(_main())
