# -*- coding: utf-8 -*-
"""产出的 docx 结构体检。

## 为什么要有它

一份 7 节的报告，7 张表约 40 个数逐条回核原文全对 ——
而结构塌了两处（打开产物就看出来了）：

```
五、业务亮点与战略进展
  5.1 … 5.5   ← 五个小标题底下一个字都没有
  [List Bullet] × 21   ← 21 条内容全堆在最后（归属全丢）
六、风险与关注点 / 七、结论与展望   ← 降级成普通段落（没有标题样式）
```

本仓库原有的验收只核数（判据：「产出里若含算出来的数：交回前逐条回到来源核一遍」）
—— 数全对，结构塌了没有任何东西会红。这个脚本就是那一格。

## 它报什么（三样，按严重度）

1. 空标题 —— 「叶子标题」底下没有内容。
   判据：从本标题到下一个「层级 ≤ 自己」的标题之间，必须至少有一个非标题项。
   父标题后面跟子标题不算空（`二、…` → `2.1 …` 是正常的），
   文档标题（H1 后面直接 H2）也不算 —— 判据不能把这两种误报。
   实际坏的是：`5.1` 后面直接是 `5.2`，兄弟之间没有内容。
2. 没有内容的小节占比（空标题 / 全部标题）—— 给一个数，便于跨版本比。
3. 正文是不是一坨 —— 最长的那一段有多长。六、七章会被压成
   两个大段落（因为 `add_block` 撞了 20 块上限），那一段会特别长。
   判据是"看起来不像正常排版"，不是硬阈值 —— 报数，由人判断。

## 它不核数

数由「回原始来源核」那条纪律管。这一支只管结构 —— 两件事，别混。

用法：
    .venv/Scripts/python.exe scripts/check_docx_structure.py <file.docx> [...]
    # 不给文件，扫 workspace/ 下所有 .docx
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: 一段"看起来不对劲"的报数线（提示，不是判死 —— 排版本来就该由人看）。
#: 这条线是量出来的、不是拍的：本机 5 份 docx 里，正常报告最长段
#: 331 字符，而那份被 `add_block` 上限压出来的六、七章是 641 字符。
#:500 落在中间，两头都不贴边。
_LONG_PARA_HINT = 500


def _items(doc):
    """按文档顺序产出段落与表格（表格不在 `doc.paragraphs` 里）。"""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for ch in doc.element.body.iterchildren():
        if ch.tag == qn("w:p"):
            yield Paragraph(ch, doc)
        elif ch.tag == qn("w:tbl"):
            yield Table(ch, doc)


def _level(it) -> int | None:
    """标题层级；不是标题返回 `None`。"""
    from docx.text.paragraph import Paragraph
    if isinstance(it, Paragraph) and it.style.name.startswith("Heading"):
        try:
            return int(it.style.name.split()[-1])
        except (ValueError, IndexError):
            return 1
    return None


def inspect(path: Path) -> dict:
    """返回读数（纯函数，不打印）。"""
    from docx import Document
    from docx.text.paragraph import Paragraph
    doc = Document(str(path))
    items = list(_items(doc))
    filled = [it for it in items if isinstance(it, Paragraph) and it.text.strip()]
    heads = [it for it in filled if _level(it) is not None]

    empty, longest, longest_text = [], 0, ""
    for p in filled:
        if len(p.text) > longest and _level(p) is None:
            longest, longest_text = len(p.text), p.text

    for i, it in enumerate(items):
        L = _level(it)
        if L is None or not getattr(it, "text", "").strip():
            continue
        has_body = False
        for nxt in items[i + 1:]:
            NL = _level(nxt)
            if NL is not None and NL <= L:
                break
            if NL is None:
                has_body = True
                break
        if not has_body:
            empty.append(it.text.strip())

    return {
        "file": path.name,
        "paragraphs": len(filled),
        "tables": sum(1 for it in items if not isinstance(it, Paragraph)),
        "headings": len(heads),
        "empty_headings": empty,
        "longest_para": longest,
        "longest_text": longest_text,
    }


def report(path: Path) -> bool:
    """打印一份读数。返回 True = 有问题（给退出码用）。"""
    r = inspect(path)
    print("─" * 62)
    print("%s" % r["file"])
    print("  段落 %d · 表格 %d · 标题 %d"
          % (r["paragraphs"], r["tables"], r["headings"]))
    bad = False
    if r["empty_headings"]:
        bad = True
        n, tot = len(r["empty_headings"]), max(r["headings"], 1)
        print("  [!!] **空标题 %d / %d**（叶子标题底下没有内容）:" % (n, tot))
        for h in r["empty_headings"][:12]:
            print("        - %s" % h[:60])
        if n > 12:
            print("        … 还有 %d 个" % (n - 12))
    else:
        print("  [ok] 没有空标题")
    if r["longest_para"] > _LONG_PARA_HINT:
        bad = True
        print("  [!]  最长的一段 %d 字符（像被压过 —— 正常排版不该有这种长段）:"
              % r["longest_para"])
        print("        %s…" % r["longest_text"][:80])
    else:
        print("  [ok] 最长段落 %d 字符" % r["longest_para"])
    return bad


def main(argv: list[str]) -> int:
    if argv:
        files = [Path(a) for a in argv]
    else:
        files = sorted((_ROOT / "workspace").glob("*.docx"))
    if not files:
        print("没有 .docx 可查")
        return 0
    anybad = False
    for f in files:
        if not f.is_file():
            print("  跳过（不存在）: %s" % f)
            continue
        anybad = report(f) or anybad
    print("─" * 62)
    print("结论: %s" % ("**有结构问题**" if anybad else "结构 OK"))
    return 1 if anybad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
