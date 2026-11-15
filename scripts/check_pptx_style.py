# -*- coding: utf-8 -*-
"""产物版式体检（幻灯片）—— `layout_spec.py`「幻灯片」段的机械读者。

## 为什么要有它（与 `check_docx_style.py` 是同一件事）

"成文规格 + 机械读者"那套先只做了 docx：
`scripts/` 下 `check_docx_style` / `check_docx_structure` / `check_word_fields`
全是 docx，幻灯片侧一个都没有。规格写在文档里而没有读者，它会漂，
而漂了没有任何东西会红（没有读者的清单会烂）。

它只管版式，不管内容（同 docx 那支的分工）：
· 数对不对（题目做没做对）→ 那是评分器的事
· 结构塌没塌（空标题 / 一页塞满）→ 目前只有 docx 侧有 `check_docx_structure`

## 这份体检不判什么（写下来免得下一个人往里塞）

· "好看"不是判据（可操作的定义才叫判据）——
  这里每一条都能做减法：越界就是越界，11pt 就是小于 12pt。
· 它不判文本溢出 —— 而这一条是量过之后才决定不判的，
  不是没想到。结论如下：

  1. 产物在目标渲染器里没有这个毛病。同一份产物三种渲染器逐页比文字层：
     **PowerPoint 176 行 · WPS 177 行 · LibreOffice 192 行**。源因是转换器给单行
     文本框写 `wrap="none"` + `spAutoFit`（`elements.py:1585`）：
     PowerPoint/WPS 按文字把框撑宽，LibreOffice 忽略 `wrap="none"` 而折行。
那 16 行折行是 LibreOffice 的，不是产物的。
  2. **拿转换器自己的尺子（`estimate_text_width`）做判据 = 恒绿。**
     框宽就是这么算出来的（`= est × 头寸`，`elements.py:1105`），`est`
     永远小于框宽，那条断言一次都红不了，零判别力。
  3. 拿真字形（Pillow）做判据也不够：11 中 10（精度 91%），
     而漏的比报的多（LibreOffice 真折 16 处、召回 63%）。边界情形不可分 ——
     `同比 +114%` 与 `同比 +145%` 框宽/字号/字体逐字节相同，一个折一个不折
没有阈值能把它们分开。

  那么"这页塞不塞得下"归谁？归另一条路：转成图，自己看。
  而那条路的渲染器正是 LibreOffice，单行标签折行会出现在图里 ——
  那句话现在跟着 `convert_to_pdf` 的回执一起到（`handler.py::_DECK_RENDER_NOTE`）。

用法：
    .venv/Scripts/python.exe scripts/check_pptx_style.py <file.pptx> [...]
    # 不给文件，扫 workspace/ 下所有 .pptx

退出码：0 = 全过 · 1 = 有问题 · 2 = 打不开。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from skills.office_parser import layout_spec as _spec  # noqa: E402

#: 画布容差（cm）—— `Cm(33.867)` 与真值来回换算会有零点几毫米的差，
#: 但要分得开 4:3 与 16:9（它们差 8.5cm），所以 0.2 足够宽也足够严。
_CANVAS_TOL = 0.2

#: 越界的容差（cm）—— 只用来吸收浮点与"正好压线"。
_EDGE_TOL = 0.02

#: 纯黑 —— 与 docx 那条同判据（长文里发死；幻灯片的正文同理）。
_PURE_BLACK = "000000"


def _cm(emu) -> float:
    """EMU -> cm。`None`（读不到）原样传出去 —— 别当成 0。"""
    return None if emu is None else emu / 360000.0


def _iter_shapes(shapes):
    """把一页里的形状递归摊平（组里还有形状）。

    这是"绿了但没测到"那一类陷阱。

    `SVG → pptx` 的产物一整页就是一个组：那份 10 页 deck，
    每页顶层只有 1 个 `GROUP`，组里 11 / 19 / 36 个形状。
    只走 `slide.shapes`，检查器每页只看到一个组，
    `has_text_frame` 为假，一条 run 都取不到，所有文字判据
    （字号 / 层级 / 纯黑 / 配色）静默跳过，然后打印「版式符合规格」。

「没红」与「没跑到」长得一模一样（本仓库那条老判据）。
    教训写在这儿：检查器报"干净"之前，先确认它真的看进去了
    （下面 `inspect` 里的自我出声就是干这个的）。
    """
    for sh in shapes:
        yield sh
        inner = getattr(sh, "shapes", None)
        if inner is not None:
            yield from _iter_shapes(inner)


def _xml_text_count(shape) -> int:
    """这个形状的 XML 里有多少个 `<a:t>` 文本节点（不靠 python-pptx 认）。"""
    from pptx.oxml.ns import qn
    try:
        return len(shape._element.findall(".//" + qn("a:t")))
    except Exception:                                  # noqa: BLE001
        return 0


def _runs_of(shape):
    """把一页里所有文本 run 摊平（含表格）。返回 `[(run, 上下文串)]`。

    表格不在 `shape.text_frame` 里（它是 `GraphicFrame`）——
    只走 `has_text_frame` 会把表里的字全部漏掉，而"表里的字号/颜色"
    正是最容易跟正文两个样子的地方。
    """
    out = []
    if getattr(shape, "has_table", False):
        for ri, row in enumerate(shape.table.rows, 1):
            for ci, cell in enumerate(row.cells, 1):
                for para in cell.text_frame.paragraphs:
                    for run in para.runs:
                        out.append((run, "表格 r%dc%d" % (ri, ci)))
        return out
    if getattr(shape, "has_text_frame", False):
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                out.append((run, "文本"))
    return out


def _explicit_pt(run):
    """这个 run 显式指定的字号（pt）；没指定，`None`。

    `None` 不等于 0：没指定，跟着版式/母版走，
    我们不知道它多大 —— 那是"读不到"，不是"很小"。
    """
    size = run.font.size
    return None if size is None else size.pt


def _color_of(run):
    """这个 run 显式指定的文字色（六位十六进制大写）；读不到，`None`。

    主题色 / 自动色读不出 `rgb`，一律当"没指定"，不猜。
    """
    try:
        col = run.font.color
        if col is None or col.type is None:
            return None
        rgb = col.rgb
        return str(rgb).upper() if rgb is not None else None
    except Exception:                      # noqa: BLE001 —— 读不到 ≠ 有毛病
        return None


def inspect(path) -> list:
    """返回问题清单（空 = 干净）。"""
    from pptx import Presentation

    prs = Presentation(str(path))
    bad = []

    # ① 画布是 16:9
    w, h = _cm(prs.slide_width), _cm(prs.slide_height)
    for label, got, want in (("宽", w, _spec.DECK_WIDTH_CM),
                             ("高", h, _spec.DECK_HEIGHT_CM)):
        if got is None:
            bad.append("读不到画布%s —— 母版里没写死" % label)
        elif abs(got - want) > _CANVAS_TOL:
            bad.append("画布%s是 %.2fcm，应接近 16:9 的 %.2fcm"
                       % (label, got, want))
    # 画布读不到，下面几条按 mm 算的判据没有参照，跳过（不是"没问题"）
    if w is None or h is None:
        return bad

    for idx, slide in enumerate(prs.slides, 1):
        page = "第 %d 页" % idx

        # ② 这一页得有东西
        if not len(slide.shapes):
            bad.append("%s 上一个元素都没有 —— 空白页" % page)
            continue

        # 自我出声：XML 里有文字，而一个 run 都没取到
        #那不是"没有文字"，是扫描器没看进去。
        #    `SVG→pptx` 每页顶层是一个组，只走 `slide.shapes`
        #所有文字判据静默跳过，然后报「OK」。
        #    判据同本仓库那条：「没红」与「没跑到」长得一模一样 ——
        #    所以扫描器要自己说"我看了几个"，而不是默默给一个干净。
        _xml_t = sum(_xml_text_count(sh) for sh in _iter_shapes(slide.shapes))
        _runs_all = [r for sh in _iter_shapes(slide.shapes) for r in _runs_of(sh)]
        if _xml_t and not _runs_all:
            bad.append("%s 的 XML 里有 %d 段文字，而扫描器**一条都没取到** —— "
                       "这是**体检器自己的问题**（组没摊平？），不是这一页没问题"
                       % (page, _xml_t))
            continue

        # ③ 元素不许出画布 · ④ 不许压安全边距
        for shape in _iter_shapes(slide.shapes):
            left, top = _cm(shape.left), _cm(shape.top)
            sw, sh = _cm(shape.width), _cm(shape.height)
            if None in (left, top, sw, sh):
                continue                    # 读不到尺寸的形状（少见）不猜
            right, bottom = left + sw, top + sh
            # 满画布的形状是"底"，不是"内容" —— 跳过它。
            #
            # `SVG→pptx` 转换器给每页铺一张满画布的
            # 背景矩形，十页里八页报了"压在安全边距上（left=0 top=0
            # right=33.87 bottom=19.05）"—— 全部是假阳性，而底下真问题
            # 只有两条。判据是这张图本来就要铺满：底图压边距是对的，
            # 压边距的是内容。一个每次都报红 8 条的体检器没人会看第二次。
            if (left <= _EDGE_TOL and top <= _EDGE_TOL
                    and right >= w - _EDGE_TOL and bottom >= h - _EDGE_TOL):
                continue
            if left < -_EDGE_TOL or top < -_EDGE_TOL:
                bad.append("%s 有元素跑到画布**外面**（left=%.2f top=%.2f cm）"
                           % (page, left, top))
            elif right > w + _EDGE_TOL or bottom > h + _EDGE_TOL:
                bad.append("%s 有元素**越出画布**（右 %.2f / 下 %.2f，画布 %.2f×%.2f cm）"
                           % (page, right, bottom, w, h))
            elif (left < _spec.DECK_MARGIN_CM - _EDGE_TOL
                  or top < _spec.DECK_MARGIN_CM - _EDGE_TOL
                  or right > w - _spec.DECK_MARGIN_CM + _EDGE_TOL
                  or bottom > h - _spec.DECK_MARGIN_CM + _EDGE_TOL):
                bad.append("%s 有元素**压在安全边距上**（要留 %.2fcm，"
                           "实际 left=%.2f top=%.2f right=%.2f bottom=%.2f）"
                           % (page, _spec.DECK_MARGIN_CM, left, top, right, bottom))

        # ⑤⑥⑦⑧ 文字：字号 / 层级 / 颜色
        runs = [r for shape in _iter_shapes(slide.shapes) for r in _runs_of(shape)]
        runs = [(r, ctx) for (r, ctx) in runs if (r.text or "").strip()]
        if not runs:
            continue                        # 纯图页是合法的

        sizes = [_explicit_pt(r) for (r, _c) in runs]
        known = [s for s in sizes if s is not None]

        if not known:
            bad.append("%s 有文字，而**字号一个都没显式指定** —— 那它多大"
                       "由版式/母版决定，我们控制不了" % page)
        else:
            small = [(r, ctx, _explicit_pt(r)) for (r, ctx) in runs
                     if _explicit_pt(r) is not None
                     and _explicit_pt(r) < _spec.DECK_MIN_FONT_PT]
            for _r, ctx, pt in small[:3]:
                bad.append("%s 的%s有 %.1fpt 的字 —— 低于可读下限 %.1fpt"
                           % (page, ctx, pt, _spec.DECK_MIN_FONT_PT))
            # 层级：一页要有一个"最大的字号"站出来当标题
            top_pt = max(known)
            if top_pt < _spec.DECK_SUBTITLE_PT:
                bad.append("%s 最大的字号只有 %.1fpt（不到 %.1f）—— 这一页没有标题"
                           % (page, top_pt, _spec.DECK_SUBTITLE_PT))
            rest = [s for s in known if s != top_pt]
            if rest and top_pt < min(rest) * _spec.DECK_TITLE_RATIO:
                bad.append("%s 标题 %.1fpt 与正文 %.1fpt 差不到 %.1f 倍 —— 层级看不出来"
                           % (page, top_pt, min(rest), _spec.DECK_TITLE_RATIO))

        # 按"颜色"报，不按"run"报 —— 一页里同一个越界色出现 20 次
        #   是一个问题，报 20 行只会把真问题淹掉（一份外来的 pptx
        #   在这一条上刷了 9 行同色）。判据同族：报数，不报字符。
        #
        # 判据不能要求"颜色必须是那三个"：幻灯片的配色
        #   按视觉风格现选（瑞士风用 `#111111`/`#6B6B6B` 完全合理）
        #那会在真实产物上报假阳性。现在：**纯黑是硬线，
        #   其余只看"种类数得清"**。理由与证据见 `layout_spec.DECK_MAX_TEXT_COLORS`。
        seen_cols = set()
        for r, _ctx in runs:
            col = _color_of(r)
            if col is None:
                continue                    # 没指定，跟主题走，不判
            seen_cols.add(col)
            if col == _PURE_BLACK:
                bad.append("%s 用了纯黑 #000000 —— 该用墨色 #%s"
                           % (page, _spec.INK))
        if len(seen_cols) > _spec.DECK_MAX_TEXT_COLORS:
            bad.append("%s 的文字用了 %d 种颜色（上限 %d）：%s —— 全篇只该有一个强调色"
                       % (page, len(seen_cols), _spec.DECK_MAX_TEXT_COLORS,
                          "/".join("#" + c for c in sorted(seen_cols))))
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
        targets = sorted(Path("workspace").glob("*.pptx"))
    if not targets:
        print("没有可体检的 .pptx")
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
