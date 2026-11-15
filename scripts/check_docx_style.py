# -*- coding: utf-8 -*-
"""产物版式体检 —— `layout_spec.py` 那套规格的机械读者。

## 为什么要有它

`layout_spec.py` 里写的每一条（正文不是纯黑 · 一个强调色 · 标题字号递减 ·
表头有底纹 · 框线不是纯黑 · 页面是 A4）都是判据 —— 而判据没有读者就会漂。
结构塌了没有任何东西会红，直到用户打开产物才会发现。
这一支补的是另一面：结构对了，样式错了，同样没人报。

它只管版式，不管内容：
· 数对不对 → 逐条回原始来源核
· 结构塌没塌（空标题 / 一坨长段）→ `scripts/check_docx_structure.py`
三件事，三个读者，别混。

用法：
    .venv/Scripts/python.exe scripts/check_docx_style.py <file.docx> [...]
    # 不给文件，扫 workspace/ 下所有 .docx

退出码：0 = 全过 · 1 = 有问题 · 2 = 打不开。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: A4 的标称尺寸与容差（cm）。容差给 0.2 —— python-docx 默认模板是 Letter
#: （21.59 x 27.94），与 A4 拉开 0.59，这个容差分得开。
_A4_W, _A4_H, _TOL = 21.0, 29.7, 0.2

#: 纯黑 —— 正文用它会"发死"。
_PURE_BLACK = "000000"

#: 表头底纹要"浅但有"：RGB 三通道平均值落在这个区间。
_HEAD_BG_LO, _HEAD_BG_HI = 200, 250


def _rgb(hex_str: str):
    try:
        return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, TypeError):
        return None


def _flow(doc) -> list:
    """按文档顺序把正文拆成 `(kind, text)`：
    `divider`（带下边框的段落）· `text` · `empty` · `table`。

    走 body 而不是 `doc.paragraphs` —— 表格不在里面，顺序会错。
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    out = []
    for ch in doc.element.body.iterchildren():
        if ch.tag == qn("w:p"):
            par = Paragraph(ch, doc)
            ppr = ch.find(qn("w:pPr"))
            pbdr = ppr.find(qn("w:pBdr")) if ppr is not None else None
            is_div = pbdr is not None and pbdr.find(qn("w:bottom")) is not None
            txt = par.text.strip()
            out.append(("divider" if is_div else ("text" if txt else "empty"), txt))
        elif ch.tag == qn("w:tbl"):
            out.append(("table", ""))
    return out


def inspect(path) -> list:
    """返回问题清单（空 = 干净）。"""
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(str(path))
    bad = []

    # ① 页面是 A4
    sec = doc.sections[0] if doc.sections else None
    if sec is None:
        bad.append("文档没有节（section），读不到页面设置")
    else:
        for label, got, want in (("宽", sec.page_width.cm, _A4_W),
                                 ("高", sec.page_height.cm, _A4_H)):
            if abs(got - want) > _TOL:
                bad.append("页面%s是 %.2fcm，应接近 A4 的 %.1fcm" % (label, got, want))

    # ② 正文不是纯黑，而且必须显式指定（不指定 = 落回自动色 = 纯黑）
    try:
        normal = doc.styles["Normal"]
        col = normal.font.color
        rgb = col.rgb if (col and col.type) else None
        if rgb is None:
            bad.append("正文（Normal）没指定颜色 —— 不指定就落回自动色，也就是纯黑")
        elif str(rgb).upper() == _PURE_BLACK:
            bad.append("正文（Normal）用的是纯黑 #000000 —— 长文里发死")
    except KeyError:
        bad.append("模板里没有 Normal 样式")

    # ③ 各级标题共用一个强调色，且字号严格递减
    heads = []
    for name in ("Heading 1", "Heading 2", "Heading 3"):
        try:
            st = doc.styles[name]
        except KeyError:
            continue
        c = st.font.color
        heads.append((name,
                      st.font.size.pt if st.font.size else None,
                      str(c.rgb).upper() if (c and c.type and c.rgb) else None))
    if not heads:
        bad.append("三级标题样式一个都没有")
    else:
        cols = {h[2] for h in heads}
        if None in cols:
            bad.append("有标题样式没指定颜色：" +
                       ", ".join(h[0] for h in heads if h[2] is None))
        elif len(cols) > 1:
            bad.append("标题用了不止一个颜色（%s）—— 全篇只该有一个强调色"
                       % "/".join(sorted(cols)))
        sizes = [h[1] for h in heads]
        if any(s is None for s in sizes):
            bad.append("有标题样式没指定字号：" +
                       ", ".join(h[0] for h in heads if h[1] is None))
        else:
            for i in range(len(sizes) - 1):
                if sizes[i] <= sizes[i + 1]:
                    bad.append("%s(%.1f) 不小于 %s(%.1f) —— 层级看不出来"
                               % (heads[i][0], sizes[i], heads[i + 1][0], sizes[i + 1]))

    # ④ 表格：表头有底纹 + 加粗；框线不是纯黑
    for ti, table in enumerate(doc.tables, 1):
        # 组件表（KPI 卡那类）不是数据表，没有"表头"可言，跳过。
        #   判据打在 `w:tblCaption` 上（`layout_spec.COMPONENT_MARK`）。
        cap = table._tbl.tblPr.find(qn("w:tblCaption"))
        if cap is not None and (cap.get(qn("w:val")) or "").startswith("lv:"):
            continue
        rows = list(table.rows)
        if not rows:
            continue
        head_cells = list(rows[0].cells)
        shaded = 0
        bold = 0
        for c in head_cells:
            tcpr = c._tc.tcPr
            shd = tcpr.find(qn("w:shd")) if tcpr is not None else None
            fill = shd.get(qn("w:fill")) if shd is not None else None
            rgb = _rgb(fill) if fill else None
            if rgb and _HEAD_BG_LO <= sum(rgb) / 3 <= _HEAD_BG_HI:
                shaded += 1
            if any(r.bold for p in c.paragraphs for r in p.runs):
                bold += 1
        if shaded == 0:
            bad.append("表 %d 的表头**没有底纹** —— 表头和表体分不开" % ti)
        if bold == 0:
            bad.append("表 %d 的表头**没有加粗**" % ti)
        borders = table._tbl.tblPr.find(qn("w:tblBorders"))
        if borders is not None:
            for edge in ("top", "left", "bottom", "insideH", "insideV"):
                el = borders.find(qn("w:" + edge))
                if el is not None and (el.get(qn("w:color")) or "").upper() == _PURE_BLACK:
                    bad.append("表 %d 的框线是纯黑 —— 该用发丝线" % ti)
                    break

    # ⑤ 分隔线：别落在首尾、别连着来
    # 判据为什么不是"密度"：那一份有 10 条，逐条看过 —— 全是章节之间的
    #   （每条紧挨着下一个 `##`），那正是分隔线该待的地方。
    #   密度阈值要么松到抓不住（10 条 / 88 段 = 11%），要么紧到误伤正常文档。
    #打在这两个实际会坏的形状上。
    flow = [f for f in _flow(doc) if f[0] != "empty"]
    for i, (kind, _txt) in enumerate(flow):
        if kind != "divider":
            continue
        if i == 0 or i == len(flow) - 1:
            bad.append("分隔线落在文档的**开头/结尾** —— 它没有分隔任何东西")
        elif flow[i - 1][0] == "divider" or flow[i + 1][0] == "divider":
            bad.append("有**连着两条**分隔线（中间没有内容）")
    return bad


def report(path) -> bool:
    """打一份体检报告。返回"过没过"。"""
    name = Path(path).name
    print("-" * 62)
    print(name)
    try:
        bad = inspect(path)
    except Exception as exc:                      # noqa: BLE001
        print("  [!!] 打不开 / 读不了：%s: %s" % (type(exc).__name__, exc))
        return False
    if not bad:
        print("  [ok] 版式符合规格")
        print("-" * 62)
        return True
    for b in bad:
        print("  [!]  %s" % b)
    print("-" * 62)
    return False


def main(argv) -> int:
    targets = [Path(a) for a in argv[1:]]
    if not targets:
        targets = sorted(Path("workspace").glob("*.docx"))
    if not targets:
        print("没有可体检的 .docx")
        return 0
    ok = True
    for t in targets:
        if not Path(t).exists():
            print("  [!!] 文件不存在：%s" % t)
            ok = False
            continue
        ok = report(t) and ok
    print("\n结论: %s" % ("**版式 OK**" if ok else "**有版式问题**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
