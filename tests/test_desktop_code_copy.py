"""桌面端代码块复制按钮的读者：把四个 node 测试接进全量 runner。

## 它为什么存在

前端 `packages/ui` 没有测试设施，逻辑测试用 node 直接跑 `.mjs`。
但"写了测试没人跑"等于没写 —— 这个读者把它们接进来，
和 `test_desktop_markdown.py` 同一条路。

## 它钉什么

  ① `codeCopy.test.mjs`：按钮文案三态、失败退路、回落时长；
  ② `codeFidelity.test.mjs`：围栏语言任意 token（不只主流）、code 块
     文本逐字原样（不多空格、不吞尾空行）—— 这是"原样复制"的前提；
  ③ `commandLang.test.mjs`：DNA 级命令语言识别（只认"另一边语法上不可能"的特征，
     漏判可接受、误判为零）；
  ④ `codeBlockEdges.test.mjs`：空块 / 半截 / 边界；
  ⑤ `Markdown.jsx` 真的接了复制按钮（写了组件没人用是常见形状）；
  ⑥ 只在 GUI：WebUI 的 chat.html 不 import Markdown.jsx。

node 不在 PATH 就当场红，不静默跳过。输出一律 ASCII（GBK 控制台）。

用法：
    .venv/Scripts/python.exe tests/test_desktop_code_copy.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "packages" / "ui" / "tests"
_MD = _ROOT / "packages" / "ui" / "src" / "Markdown.jsx"
_WEB_CHAT = _ROOT / "web" / "templates" / "chat.html"

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, ok: bool, note: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  [ok]   " + label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [!!]   " + label + ("  -- " + note if note else ""))


def _run_mjs(node: str, name: str) -> None:
    path = _TESTS / name
    r = subprocess.run([node, str(path)], capture_output=True, text=True,
                       cwd=str(_ROOT), timeout=120)
    tail = (r.stdout or r.stderr or "").strip().splitlines()
    check("%s 全过" % name,
          r.returncode == 0 and "0 failed" in (r.stdout or ""),
          (tail[-1] if tail else "no output") + " / rc=%d" % r.returncode)


def main() -> int:
    print("=" * 62)
    print("Desktop code-block copy: labels / fidelity / wiring / GUI-only")
    print("=" * 62)

    node = shutil.which("node")
    check("node on PATH (no silent skip)", node is not None,
          "node not found; the guard cannot run")
    if node:
        print("\n-- node-level assertions --")
        _run_mjs(node, "codeCopy.test.mjs")
        _run_mjs(node, "codeFidelity.test.mjs")
        _run_mjs(node, "commandLang.test.mjs")
        _run_mjs(node, "codeBlockEdges.test.mjs")

    print("\n-- wiring --")
    src = _MD.read_text(encoding="utf-8") if _MD.exists() else ""
    check("Markdown.jsx imports copyText", "copyText" in src)
    check("代码块渲染真的挂了 CodeBlock（写了没人用等于没做）",
          "<CodeBlock" in src and "function CodeBlock" in src)
    check("按钮复制的是这段代码本身，不是整条回复",
          "copyText(code)" in src)
    check("语言标注接了检测器（没标语言时才特征识别）",
          "detectCommandLanguage(code)" in src and "import { detectCommandLanguage" in src)
    check("有围栏标记时直接用标记（比猜测优先）",
          "const shown = lang" in src and "languageLabel(lang)" in src)
    check("流式未闭合块不给复制按钮（防半截可复制）",
          "b.closed === false" in src and "md-copying" in src)

    md_src = (_ROOT / "packages" / "ui" / "src" / "markdown.js").read_text(encoding="utf-8")
    check("解析器标记闭合状态（closed 字段）",
          "closed" in md_src and "let closed = false" in md_src)

    print("\n-- GUI only (WebUI is a separate renderer) --")
    # WebUI 与桌面 GUI 是两套独立渲染：GUI 走 packages/ui 的 Markdown.jsx，
    # WebUI 的 chat.html 用 el('div','bubble-text', text) + CSS pre-wrap
    # 渲染纯文本，没有 markdown 解析、没有 <code> 块概念 —— 那边不存在
    # "给代码块加复制按钮"的落点，功能自然只在 GUI。
    web = _WEB_CHAT.read_text(encoding="utf-8") if _WEB_CHAT.exists() else ""
    check("chat.html 不 import Markdown.jsx / @crannia/ui",
          "Markdown.jsx" not in web and "@crannia/ui" not in web)
    check("chat.html 用纯文本渲染（bubble-text + textContent 路线）",
          "'bubble-text'" in web and "white-space" in web)
    check("chat.html 没有代码块复制按钮（copy button / clipboard.writeText 复制正文）",
          "md-copy" not in web and "codeCopy" not in web)
    # web/ 整棵目录都不许引用 GUI 的复制实现（唯一允许的 packages/ui 提及是注释）
    web_src = ""
    for p in (_ROOT / "web").rglob("*"):
        if p.suffix in (".html", ".js", ".css", ".py") and p.is_file():
            try:
                web_src += p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    check("web/ 全目录没有 GUI 复制模块的引用",
          "codeCopy.js" not in web_src and "from \"./Markdown.jsx\"" not in web_src)

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
