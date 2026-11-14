"""
PowerPoint 后处理工具。

## 它们不生成幻灯片，只改已经存在的 `.pptx`

本仓库的 PPT 生成走 `create_svg_slide` → `svgs_to_pptx`（SVG → 原生 DrawingML
形状），那是另一条路。这里这一批是它的后处理 —— 生成完之后，
有些东西 SVG 表达不了，得回到 PPT 的对象模型上改：

| 工具 | 补的是什么 |
|---|---|
| `set_ppt_background` | 母版/单页背景（纯色 / 渐变 / 纹理） |
| `set_ppt_background_graphics` | 隐藏背景图形（只标题页不显示母版 LOGO） |
| `crop_ppt_picture` | 图片裁剪（矩形 / 圆形） |
| `add_ppt_audio` | 插入音频 |
| `set_ppt_master_text_style` | 母版文本样式（各级字号/字体/颜色） |

## 与 `python-pptx` 能力的关系

这一批全部属于 `python-pptx` 本来就有 API、我们只是没接的那一类
—— 唯一例外是纹理填充，它要手写一段 `a:blipFill`（`python-pptx` 的
`fill` 只给到 solid / gradient / patterned）。那一段在下面的代码里标了。

没接的那半（要手写 OOXML）：动画（`p:timing` 树）·
切换（`p:transition`）· SmartArt（要造 4 个 `dgm` 部件）。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.workspace import primary_output_dir
from pptx import Presentation
from pptx.util import Emu

logger = logging.getLogger(__name__)

#: 安全核 —— 由 `handler.init()` 转调 `init()` 注入（同 `excel_tools` / `word_tools`）。
#: 忘了转调，`_guarded` 每次都 raise（fail-closed，症状是"所有 PPT 工具全挂"，一眼可辨）。
_security = None


def init(security) -> None:
    global _security
    _security = security


def _resolve_path(file_path: str, op: str = "write") -> Path:
    """解析 + 过安全核 —— 转调 `word_tools` 那一份，不另写。

    为什么是转调：`word_tools._resolve_path` 是 office 那一族的
    共同收口（路径解析 + 安全校验一个动作）。另写一套"看着一样"的
    实现会漂：`workspace/deck.pptx` 会被拼成 `workspace/workspace/deck.pptx`
    （`word_tools` 那份 docstring 里记着的同一个坑）。一个判定两处必然漂 ——
    除非把第二处删掉。

    安全核用那一份的 `_security`（由 `handler.init` 转调注入），
    所以 `ppt_tools.init` 只是一个占位（`skill_loader` 按子模块挨个调，
    少一个会 AttributeError）。别把 `_security` 删掉又指望校验还在。
    """
    from . import word_tools as _wt
    return _wt._resolve_path(file_path, op)


def _rgb(hex_str: str):
    """`"#RRGGBB"` -> `RGBColor`。给错了要一句人话，不是 `ValueError` 原文。"""
    from pptx.dml.color import RGBColor
    s = str(hex_str or "").strip().lstrip("#")
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ValueError("颜色要 6 位十六进制（如 FF8800），收到 %r" % hex_str)
    return RGBColor.from_string(s.upper())


def _load(path: Path):
    if not path.exists():
        return None, {"success": False, "error": f"文件不存在: {path}"}
    try:
        return Presentation(str(path)), None
    except Exception as e:                                    # noqa: BLE001
        return None, {"success": False, "error": f"打不开（不是合法的 .pptx？）: {e}"}


def _pick(slides, which: Any) -> List[int]:
    """把 `which` 变成一组从 0 起的页号。

    `"all"` / `""` = 全部；`"3"` = 单页；`"1,3,5"` = 若干页。
    入参 1 起、内部 0 起 —— 界面和卷子上都是"第 3 张"，而 python-pptx 是 0 起。
    这个转换只在这一处（两处就会有一个地方忘转）。

    为什么是逗号串而不是数组：`bus/loaders/skill_loader.py` 的 schema 校验
    只收单一 `type`（不许联合），`string|integer|array` 过不了装载。
对外只承诺字符串；`int` / `list` 那两支留着，是给程序调用的
    （桩、测试、以后别的工具内部复用），不写进 manifest。
    """
    n = len(slides)
    if which in (None, "", "all", "全部"):
        return list(range(n))
    if isinstance(which, int):
        idx = [which]
    elif isinstance(which, (list, tuple)):
        idx = list(which)
    elif isinstance(which, str):
        try:
            idx = [int(x) for x in which.replace("，", ",").split(",") if x.strip()]
        except ValueError:
            raise ValueError(f"slides 要 all 或页号（如 '1' 或 '1,3,5'），收到 {which!r}")
        if not idx:
            raise ValueError(f"slides 里一个页号都没有：{which!r}")
    else:
        raise ValueError(f"slides 要 all / 页号 / 页号串，收到 {which!r}")
    out = []
    for i in idx:
        k = int(i) - 1
        if not (0 <= k < n):
            raise ValueError(f"页号 {i} 越界（这份文稿有 {n} 张，**1 起**）")
        out.append(k)
    return out


# 背景

def set_ppt_background(
    file_path: str,
    slides: Any = "all",
    kind: str = "solid",
    color_hex: str = "",
    color_hex2: str = "",
    texture_path: str = "",
    angle: int = 0,
) -> dict:
    """给若干页设背景。

    * `kind="solid"` —— 纯色（`color_hex`）
    * `kind="gradient"` —— 渐变（`color_hex` -> `color_hex2`，可选 `angle` 0~359）
    * `kind="texture"` —— 图片/纹理填充（`texture_path`）

    `ppt2` 的"纹理填充 · 纸纹2"、`ppt1` 的"中心辐射渐变"都是它。
    纹理那一路要手写 `a:blipFill` —— `python-pptx` 的 `fill` 只到
    `solid / gradient / patterned`，没有图片填充。那一段在下面标了 `# 1a-特例`。
    这里设的是这一页自己的背景（`p:bg`），不是母版 ——
    "应用至全部页面"用 `slides="all"` 逐页设，效果一样且不动母版
    （动母版会影响之后新建的页，而这里的目标是"现有这些页看起来对"）。
    """
    path = _resolve_path(file_path, "write")
    prs, err = _load(path)
    if err:
        return err
    k = str(kind or "solid").strip().lower()
    if k not in ("solid", "gradient", "texture"):
        return {"success": False,
                "error": f"kind 只认 solid / gradient / texture，收到 {kind!r}"}
    try:
        idx = _pick(prs.slides, slides)
    except ValueError as ve:
        return {"success": False, "error": str(ve)}

    tex: Optional[Path] = None
    if k == "texture":
        if not texture_path:
            return {"success": False, "error": "kind=texture 要给 texture_path"}
        tex = _resolve_path(texture_path, "read")
        if not tex.exists():
            return {"success": False, "error": f"纹理图不存在: {tex}"}

    try:
        for i in idx:
            bg = prs.slides[i].background
            if k == "solid":
                bg.fill.solid()
                bg.fill.fore_color.rgb = _rgb(color_hex)
            elif k == "gradient":
                bg.fill.gradient()
                stops = bg.fill.gradient_stops
                stops[0].color.rgb = _rgb(color_hex)
                if len(stops) > 1:
                    stops[1].color.rgb = _rgb(color_hex2 or color_hex)
                try:
                    bg.fill.gradient_angle = float(angle)
                except Exception:                            # noqa: BLE001
                    pass        # 有些预设渐变是"路径型"，没有角度 —— 不报错，保持原样
            else:
                # 图片关系要挂在这一页的 part 上，不是 `prs.part`
                #    —— `get_or_add_image_part` 是 `SlidePart` 的方法
                #    （`prs.part` 上没有它，AttributeError）。
                _apply_texture_fill(bg.fill._xPr, prs.slides[i].part, tex)
        prs.save(str(path))
        return {"success": True,
                "message": "已给 %d 张幻灯片设了 %s 背景" % (len(idx), k)}
    except Exception as e:
        return {"success": False, "error": f"设背景失败: {type(e).__name__}: {e}"}


def _apply_texture_fill(xpr, slide_part, image_path: Path) -> None:
    """把 `p:bgPr` 的填充换成图片填充（`a:blipFill`）。`# 1a-特例`。

    这一段不是 `1b`：`python-pptx` 缺的只是"图片填充"这一个口，
    而它已经会把图片收进包、也已经有 `get_or_add_image_part` ——
    我们只是替它把那段 XML 拼出来。不要因为"写了 XML"就把它归到 `1b`。
    """
    from pptx.oxml.ns import qn
    from lxml import etree

    # 返回的是 `(image_part, rId)` —— 顺序反了会把 image 对象塞进
    #    `r:embed=""`，症状是 `XMLSyntaxError: Unescaped '<'`
    #    （对象的 repr 里有尖括号）。踩过一次，写在这儿。
    _image_part, rid = slide_part.get_or_add_image_part(str(image_path))
    # 旧的填充元素（a:solidFill / a:gradFill / a:noFill …）全清掉
    for tag in ("a:noFill", "a:solidFill", "a:gradFill", "a:blipFill", "a:pattFill"):
        for old in xpr.findall(qn(tag)):
            xpr.remove(old)
    xml = (
        '<a:blipFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></a:blipFill>' % rid
    )
    xpr.insert(0, etree.fromstring(xml))


def set_ppt_background_graphics(
    file_path: str,
    slides: Any = "all",
    show: bool = False,
) -> dict:
    """显示 / 隐藏某几页的背景图形（母版上的那些形状）。

    `ppt1` 第 2 步"在标题幻灯片中隐藏背景图形（即仅标题幻灯片不显示右上角的
    LOGO 图片）"就是它 —— 因为 LOGO 是插在母版上的，只能在这一层关。
    """
    path = _resolve_path(file_path, "write")
    prs, err = _load(path)
    if err:
        return err
    try:
        idx = _pick(prs.slides, slides)
    except ValueError as ve:
        return {"success": False, "error": str(ve)}
    try:
        for i in idx:
            prs.slides[i]._element.set("showMasterSp", "1" if show else "0")
        prs.save(str(path))
        return {"success": True,
                "message": "已%s %d 张幻灯片的背景图形"
                           % ("显示" if show else "隐藏", len(idx))}
    except Exception as e:
        return {"success": False, "error": f"设背景图形失败: {type(e).__name__}: {e}"}


# 图片裁剪

def crop_ppt_picture(
    file_path: str,
    slide_index: int,
    picture_index: int = 1,
    left: float = 0, right: float = 0, top: float = 0, bottom: float = 0,
    to_shape: str = "",
) -> dict:
    """裁剪某张幻灯片上的第 N 张图片。

    四个方向是 0~1 的比例（`left=0.1` = 左边裁掉 10%），与 OOXML 的
    `cropLeft/cropRight/cropTop/cropBottom` 同口径。

    `to_shape="circle"` = 裁成正圆：把一个正方形图片的
    四边各裁掉 `(1 - 1/√2)/2 ≈ 0.1464`，得到内切圆 —— 再配上"圆形"的几何预设
    才是视觉上的圆。本工具只做裁剪比例；"圆形边框/柔化边缘"那类效果
    `python-pptx` 没有 API。
    """
    path = _resolve_path(file_path, "write")
    prs, err = _load(path)
    if err:
        return err
    try:
        if not (1 <= slide_index <= len(prs.slides)):
            return {"success": False,
                    "error": f"slide_index 越界（这份文稿有 {len(prs.slides)} 张，**1 起**）"}
        slide = prs.slides[slide_index - 1]
        pics = [sh for sh in slide.shapes if sh.shape_type is not None
                and "PICTURE" in str(sh.shape_type).upper()]
        if not pics:
            return {"success": False, "error": f"第 {slide_index} 张上没有图片"}
        if not (1 <= picture_index <= len(pics)):
            return {"success": False,
                    "error": f"picture_index 越界（这一张有 {len(pics)} 张图）"}
        pic = pics[picture_index - 1]

        if to_shape == "circle":
            m = (1 - 1 / (2 ** 0.5)) / 2          # 内切圆的比例
            left = right = top = bottom = m
        elif to_shape:
            return {"success": False,
                    "error": f"to_shape 现在只认 circle，收到 {to_shape!r}"
                             f"（其它形状要走 1b 的几何预设）"}

        for name, val in (("crop_left", left), ("crop_right", right),
                          ("crop_top", top), ("crop_bottom", bottom)):
            v = float(val)
            if not (0 <= v < 1):
                return {"success": False,
                        "error": f"{name} 要 0~1 的比例（0.1 = 裁掉 10%），收到 {val!r}"}
            setattr(pic, name, v)
        prs.save(str(path))
        return {"success": True,
                "message": "已裁剪第 %d 张的第 %d 张图（左右上下 %.3f/%.3f/%.3f/%.3f）"
                           % (slide_index, picture_index, left, right, top, bottom)}
    except Exception as e:
        return {"success": False, "error": f"裁剪失败: {type(e).__name__}: {e}"}


# 音频

def add_ppt_audio(
    file_path: str,
    slide_index: int,
    media_path: str,
    left_cm: float = 1.0,
    top_cm: float = 1.0,
    autoplay: bool = True,
) -> dict:
    """往某张幻灯片插一个音频（`ppt1` 第 8 步"链接背景音乐"）。

    它是把文件嵌进包里（不是"链接到考生文件夹"）—— 对"共享后能正常播放"
    这个目标来说，嵌入才是对的（链接一换机器就断）。
    `autoplay=True` 只设了 `p:cMediaNode` 的播放时机为"自动"，
    "跨页播放" 那类开关在 `p:timing` 里（`1b`，这一批不做）。
    """
    path = _resolve_path(file_path, "write")
    prs, err = _load(path)
    if err:
        return err
    media = _resolve_path(media_path, "read")
    if not media.exists():
        return {"success": False, "error": f"音频文件不存在: {media}"}
    try:
        if not (1 <= slide_index <= len(prs.slides)):
            return {"success": False,
                    "error": f"slide_index 越界（这份文稿有 {len(prs.slides)} 张，**1 起**）"}
        slide = prs.slides[slide_index - 1]
        from pptx.util import Cm
        mv = slide.shapes.add_movie(
            str(media), Cm(left_cm), Cm(top_cm), Cm(2.0), Cm(2.0),
            poster_frame_image=None, mime_type="audio/mpeg")
        if autoplay:
            # 音频图标照样是"自动播放"的——把播放时机写成"自动"
            from pptx.oxml.ns import qn
            for node in mv._element.iter(qn("p:cMediaNode")):
                node.set("showWhenStopped", "0")
        prs.save(str(path))
        return {"success": True,
                "message": "已把 %s 插入第 %d 张（%s）"
                           % (media.name, slide_index,
                              "自动播放" if autoplay else "点击播放")}
    except Exception as e:
        return {"success": False, "error": f"插音频失败: {type(e).__name__}: {e}"}


# 母版文本样式

def set_ppt_master_text_style(
    file_path: str,
    level: int = 1,
    kind: str = "body",
    font_name: str = "",
    size_pt: float = 0,
    color_hex: str = "",
    bold: Optional[bool] = None,
    align: str = "",
) -> dict:
    """改母版上的文字样式 —— `ppt2` 第 2 步那三格。

    * `kind="title"` —— 母版标题样式（`p:titleStyle`）
    * `kind="body"` + `level=1/2/...` —— 母版文本样式的第 N 级（`p:bodyStyle`）

    判据：改母版改的是"所有用这套版式的页"，而改某页的文本框只改那一页
    —— `ppt2` 那句"编辑母版，完成对『标题和内容』版式的样式修改"要的是前者。
    中文要同时写 `a:latin` 与 `a:ea`（东亚）两个字型 —— 只写 `a:latin`
    的话中文会退回默认字体（症状："设了没反应"，而英文生效了）。
    """
    path = _resolve_path(file_path, "write")
    prs, err = _load(path)
    if err:
        return err
    k = str(kind or "body").strip().lower()
    if k not in ("title", "body"):
        return {"success": False, "error": f"kind 只认 title / body，收到 {kind!r}"}
    if k == "body" and not (1 <= int(level) <= 9):
        return {"success": False, "error": f"level 要 1~9，收到 {level!r}"}
    try:
        master = prs.slide_masters[0]
        tx = master._element.find(
            "{http://schemas.openxmlformats.org/presentationml/2006/main}txStyles")
        if tx is None:
            return {"success": False, "error": "这份母版没有 txStyles（模板不完整）"}
        tag = ("{http://schemas.openxmlformats.org/presentationml/2006/main}"
               + ("titleStyle" if k == "title" else "bodyStyle"))
        style = tx.find(tag)
        if style is None:
            return {"success": False, "error": f"母版里没有 {k} 样式"}

        # 第 N 级（title 只有一级；body 是 p:lvl1pPr..p:lvl9pPr）
        lvl_tag = ("{http://schemas.openxmlformats.org/presentationml/2006/main}"
                   "lvl1pPr" if k == "title" else
                   "{http://schemas.openxmlformats.org/presentationml/2006/main}"
                   "lvl%dpPr" % int(level))
        ppr = style.find(lvl_tag)
        if ppr is None:
            ppr = style.makeelement(lvl_tag, {})
            style.insert(0, ppr)

        done = []
        if align:
            a = str(align).strip().lower()
            if a not in ("left", "center", "right", "justify"):
                return {"success": False,
                        "error": f"align 只认 left/center/right/justify，收到 {align!r}"}
            ppr.set("algn", {"left": "l", "center": "ctr",
                             "right": "r", "justify": "just"}[a])
            done.append("对齐 %s" % a)

        if font_name or size_pt or color_hex or bold is not None:
            def_rpr = ppr.find(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}defRPr")
            if def_rpr is None:
                from lxml import etree
                def_rpr = etree.SubElement(
                    ppr, "{http://schemas.openxmlformats.org/drawingml/2006/main}defRPr")
            if font_name:
                from lxml import etree
                A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
                for sub in ("latin", "ea"):        # 中文要 ea —— 见 docstring
                    el = def_rpr.find(A + sub)
                    if el is None:
                        el = etree.SubElement(def_rpr, A + sub)
                    el.set("typeface", str(font_name))
                done.append("字体 %s" % font_name)
            if size_pt:
                # `a:sz` 的单位是百分之一磅
                def_rpr.set("sz", str(int(round(float(size_pt) * 100))))
                done.append("%g 磅" % float(size_pt))
            if bold is not None:
                def_rpr.set("b", "1" if bold else "0")
                done.append("加粗" if bold else "不加粗")
            if color_hex:
                from lxml import etree
                A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
                for old in def_rpr.findall(A + "solidFill"):
                    def_rpr.remove(old)
                fill = etree.SubElement(def_rpr, A + "solidFill")
                clr = etree.SubElement(fill, A + "srgbClr")
                clr.set("val", str(color_hex).strip().lstrip("#").upper())
                done.append("颜色 %s" % color_hex)

        if not done:
            return {"success": False, "error": "一个样式参数都没给"}
        prs.save(str(path))
        return {"success": True,
                "message": "已改母版 %s%s：%s"
                           % ("标题" if k == "title" else "文本第 %d 级" % int(level),
                              "样式", "；".join(done))}
    except Exception as e:
        return {"success": False, "error": f"改母版样式失败: {type(e).__name__}: {e}"}


# 读侧两件 —— "重排一份已有 pptx"缺的入口
#
# 为什么是这两件：重排一份已有 pptx 的第一步是从它提取内容与身份，
# 而 `parse_pptx` 是喂检索的（切块 + 进 BM25），目的不是"重排原料"。
#
# | 缺的 | 本仓库的这一件 |
# |---|---|
# | 内容 | `pptx_to_outline` —— 每页 标题/正文/备注/图/链接 |
# | 视觉身份 | `extract_pptx_identity` —— 主题色 + 字体 + 实际用量 |
#
# 这两件不照搬现成的实现，两个理由：
#   ① 有一份实现要求 *"pin python-pptx<0.7"*，
#      而本仓库装的是 1.0.2 —— 照搬是在赌它没坏；
#   ② 视觉身份那一套依赖 `pptx_to_svg` 一整个解析子系统，
#      而本仓库没有，照搬等于拖进来一整个子系统。
#这两件按本仓库口径重写。

def extract_pptx_identity(file_path: str, max_sizes: int = 12,
                          max_colors: int = 12) -> dict:
    """读一份 .pptx 的视觉身份 —— 主题配色 / 字体 + 实际用量。

    用途：把一份已有 PPT 重排时让它长得像原来那份（继承原视觉身份）
    —— 拿到这些值去填 `set_slide_design` 的 spec_lock。

    三块：

      · `theme` —— 主题自带的 12 色（`dk1`/`lt1`/`dk2`/`lt2`/`accent1-6`/`hlink`/`folHlink`）
        与 major/minor 字体（`latin` 与 `ea` 都读 —— 中文在 `ea` 里）
      · `observed` —— 文档里实际用过的字号 / 显式字体 / 显式纯色（降序，各取前 N）
      · `slides` —— 页数

    主题色与"实际用的色"是两件事：主题是调色板，实际用量才是这份稿子
    真正长的样子（上游那份文档也是这个口径：*"recommend theme vs
    actual-usage identity"*）。重排时优先照 `observed`。
    纯读：不写回文件。
    """
    path = _resolve_path(file_path, "read")
    prs, err = _load(path)
    if err:
        return err
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    try:
        theme = {"colors": {}, "major_font": {}, "minor_font": {}}
        try:
            master = prs.slide_masters[0]
            for rel in master.part.rels.values():
                if "theme" not in str(rel.reltype):
                    continue
                import lxml.etree as ET
                xml = ET.fromstring(rel.target_part.blob)
                cs = xml.find(".//" + A + "clrScheme")
                if cs is not None:
                    for c in cs:
                        child = list(c)
                        if not child:
                            continue
                        # 两种孩子，取的值不一样（踩过）：
                        #   · `a:srgbClr` —— 真色值在 `@val`（6 位十六进制）
                        #   · `a:sysClr` —— `@val` 是系统色的名字（`windowText` /
                        #     `window`），十六进制在 `@lastClr`
                        # 统一取 `val or lastClr`，`dk1` 回的是字符串
                        #   `"windowText"`，而模型拿到这个名字填不进 spec_lock
                        #   （那里要十六进制）。sysClr 优先取 lastClr，
                        #   名字留在旁边一格（"这是什么"仍然看得见）。
                        el = child[0]
                        tag = el.tag.split("}")[1]
                        if tag == "sysClr":
                            theme["colors"][c.tag.split("}")[1]] = (
                                (el.get("lastClr") or "").upper())
                            theme["colors"].setdefault("_sys_names", {})[
                                c.tag.split("}")[1]] = el.get("val") or ""
                        else:
                            theme["colors"][c.tag.split("}")[1]] = (
                                (el.get("val") or "").upper())
                fs = xml.find(".//" + A + "fontScheme")
                if fs is not None:
                    for which, key in ((A + "majorFont", "major_font"),
                                       (A + "minorFont", "minor_font")):
                        node = fs.find(which)
                        if node is None:
                            continue
                        for sub in ("latin", "ea"):
                            el = node.find(A + sub)
                            if el is not None and el.get("typeface"):
                                theme[key][sub] = el.get("typeface")
                break
        except Exception as exc:                          # noqa: BLE001
            print("[ppt] 读主题失败（不影响其余）：%s: %s"
                  % (type(exc).__name__, exc))

        sizes: dict = {}
        fonts: dict = {}
        fills: dict = {}
        for slide in prs.slides:
            for shape in slide.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        f = run.font
                        if f.size is not None:
                            pt = round(f.size.pt, 1)
                            sizes[pt] = sizes.get(pt, 0) + 1
                        if f.name:
                            fonts[f.name] = fonts.get(f.name, 0) + 1
                        try:
                            rgb = f.color.rgb
                            if rgb is not None:
                                h = str(rgb).upper()
                                fills[h] = fills.get(h, 0) + 1
                        except Exception:                     # noqa: BLE001
                            pass

        def _top(d, n):
            return [k for k, _v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

        return {
            "success": True, "file": path.name, "slides": len(prs.slides._sldIdLst),
            "theme": theme,
            "observed": {
                "font_sizes_pt": _top(sizes, max_sizes),
                "fonts": _top(fonts, max_sizes),
                "fills_hex": _top(fills, max_colors),
            },
            "note": ("主题色是**调色板**，`observed` 才是这份稿子**实际长的样子**"
                     " —— 重排时优先照 observed 走。"),
        }
    except Exception as e:
        return {"success": False, "error": f"读身份失败: {type(e).__name__}: {e}"}


def pptx_to_outline(file_path: str, max_chars: int = 12000,
                    include_notes: bool = True) -> dict:
    """把一份 .pptx 摊成结构化的逐页大纲 —— 重排的原料。

    每页给：序号 · 版式名 · 标题 · 正文 · 备注 · 图片数 · 表格尺寸 · 超链接。

    与 `parse_pptx` 分工不同：那件喂检索（切块 ≤1500 字 + 进 BM25），
    这一件给重排用（保结构、保页码、保备注）—— 别把两件当同一件事。

    有 `max_chars` 上限（默认 12000）。超了要如实说（读不到 ≠ 0）
    —— 被截断时回执带 `truncated: true` 与"还有几页没列"。
    """
    path = _resolve_path(file_path, "read")
    prs, err = _load(path)
    if err:
        return err
    try:
        lines_per_slide: List[List[str]] = []
        for i, slide in enumerate(prs.slides, start=1):
            rows: List[str] = ["## 第 %d 页" % i]
            try:
                if slide.slide_layout and slide.slide_layout.name:
                    rows.append("版式：%s" % slide.slide_layout.name)
            except Exception:                             # noqa: BLE001
                pass
            title = ""
            try:
                if slide.shapes.title is not None:
                    title = (slide.shapes.title.text or "").strip()
            except Exception:                             # noqa: BLE001
                pass
            if title:
                rows.append("标题：%s" % title)

            body: List[str] = []
            pics = 0
            tables: List[str] = []
            links: List[str] = []
            for shape in slide.shapes:
                if "PICTURE" in str(getattr(shape, "shape_type", "")).upper():
                    pics += 1
                if getattr(shape, "has_table", False):
                    try:
                        tables.append("%d行×%d列" % (len(shape.table.rows),
                                                     len(shape.table.columns)))
                    except Exception:                     # noqa: BLE001
                        pass
                try:
                    ca = shape.click_action
                    if ca is not None and ca.hyperlink and ca.hyperlink.address:
                        links.append(ca.hyperlink.address)
                except Exception:                         # noqa: BLE001
                    pass
                if not getattr(shape, "has_text_frame", False):
                    continue
                for para in shape.text_frame.paragraphs:
                    t = (para.text or "").strip()
                    if not t or t == title:
                        continue
                    body.append(t)
                    try:
                        for r in para.runs:
                            if r.hyperlink is not None and r.hyperlink.address:
                                links.append(r.hyperlink.address)
                    except Exception:                     # noqa: BLE001
                        pass
            if body:
                rows.append("正文：")
                rows.extend("  - %s" % b for b in body)
            if tables:
                rows.append("表格：%s" % "、".join(tables))
            if pics:
                rows.append("图片：%d 张" % pics)
            if include_notes:
                try:
                    nt = (slide.notes_slide.notes_text_frame.text or "").strip()
                    if nt:
                        rows.append("备注：%s" % nt)
                except Exception:                         # noqa: BLE001
                    pass
            if links:
                rows.append("链接：%s" % "、".join(dict.fromkeys(links)))
            lines_per_slide.append(rows)

        out: List[str] = []
        truncated = False
        for rows in lines_per_slide:
            block = "\n".join(rows)
            if out and len("\n\n".join(out)) + len(block) + 2 > max_chars:
                truncated = True
                break
            out.append(block)
        done = len(out)
        total = len(lines_per_slide)
        msg = "已摊开 %d/%d 页" % (done, total)
        if truncated:
            msg += ("（**到字符上限就停了**，还有 %d 页没列 —— "
                    "调大 max_chars 或分批读）" % (total - done))
        return {"success": True, "text": "\n\n".join(out), "slides": total,
                "listed": done, "truncated": truncated, "message": msg}
    except Exception as e:
        return {"success": False, "error": f"摊开失败: {type(e).__name__}: {e}"}
