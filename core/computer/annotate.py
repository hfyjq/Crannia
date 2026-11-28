"""截图的标注层 —— 只动图，不动 UI。

四件事：

1. `crop_zoom`：按 0–1000 比例的中心 / 倍率裁一块并放大（微信那种自绘 UI 的主力）。
2. `grid`：在图上画 10% 一格的带标签网格（模型可以直接读比例，不用估像素）。
3. `click_marker`：在上一击的实际落点画红十字（连续校准，不靠三角测试）。
4. `element_marks`：把元素树里的 `[N]` 画到图上的矩形上（把"报坐标"变成"挑编号"）。

全部用 Pillow（已有依赖），任何一步失败都原样返回 —— 标注是附加物，
   不该把一次观察弄崩（同 `skill_index` 的判据）。
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font(px: int):
    for path in _FONT_CANDIDATES:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, px)
        except Exception:                            # noqa: BLE001
            continue
    try:
        return ImageFont.load_default()
    except Exception:                                # noqa: BLE001
        return None


def _open(png: bytes):
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _save(img) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _map_rect(coord_rect, img_w: int, img_h: int, rect):
    """物理矩形 -> 这张图里的像素矩形（按比例映射）。"""
    l, t, r, b = coord_rect
    w = max(1, r - l)
    h = max(1, b - t)
    x1 = int(round((rect[0] - l) * img_w / float(w)))
    y1 = int(round((rect[1] - t) * img_h / float(h)))
    x2 = int(round((rect[2] - l) * img_w / float(w)))
    y2 = int(round((rect[3] - t) * img_h / float(h)))
    return (max(0, x1), max(0, y1),
            min(img_w, max(x1 + 1, x2)), min(img_h, max(y1 + 1, y2)))


def crop_zoom(png: bytes, coord_rect, crop_rect, *,
              min_long_edge: int = 900, max_long_edge: int = 2000) -> bytes:
    """按物理 `crop_rect` 从图里裁一块；太小就整数倍放大（不超 `max_long_edge`）。"""
    try:
        img = _open(png)
        box = _map_rect(coord_rect, img.width, img.height, crop_rect)
        out = img.crop(box)
        long_edge = max(out.width, out.height)
        if long_edge < min_long_edge:
            factor = min(4, max(2, min_long_edge // max(1, long_edge)))
            out = out.resize((out.width * factor, out.height * factor),
                             Image.LANCZOS)
        if max(out.width, out.height) > max_long_edge:
            k = max_long_edge / float(max(out.width, out.height))
            out = out.resize((max(1, int(out.width * k)),
                              max(1, int(out.height * k))), Image.LANCZOS)
        return _save(out)
    except Exception:                                # noqa: BLE001
        return png


def grid(png: bytes, *, avoid=None) -> bytes:
    """10% 一格的带标签网格（标签就是 100…900 的比例值）。

    `avoid=(x_frac, y_frac)`：以这个点（0–1 的图内比例）为圆心留一个洞 ——
    标记（红十字/环）压在上面时，网格线穿过中心会被模型读成"标记的十字臂"
    （真机原话）。
    """
    try:
        img = _open(png)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        w, h = img.size
        line_w = max(1, max(w, h) // 900)
        font = _font(max(13, min(w, h) // 55))
        fh = getattr(font, "size", 11) if font is not None else 11
        color = (255, 70, 0, 190)
        ax = ay = 0
        ar = 0
        if avoid is not None:
            ax = int(round(avoid[0] * w))
            ay = int(round(avoid[1] * h))
            ar = max(12, min(w, h) // 40)

        def _v(x: int) -> None:
            if ar and abs(x - ax) <= ar:
                d.line([(x, 0), (x, max(0, ay - ar))], fill=color, width=line_w)
                d.line([(x, min(h, ay + ar)), (x, h)], fill=color, width=line_w)
            else:
                d.line([(x, 0), (x, h)], fill=color, width=line_w)

        def _hh(y: int) -> None:
            if ar and abs(y - ay) <= ar:
                d.line([(0, y), (max(0, ax - ar), y)], fill=color, width=line_w)
                d.line([(min(w, ax + ar), y), (w, y)], fill=color, width=line_w)
            else:
                d.line([(0, y), (w, y)], fill=color, width=line_w)

        for i in range(1, 10):
            x = int(w * i / 10.0)
            y = int(h * i / 10.0)
            _v(x)
            _hh(y)
            if font is not None:
                label = str(i * 100)
                # 顶部 x 标签
                tw = d.textlength(label, font=font)
                d.rectangle([x + 2, 1, x + 6 + tw, 2 + fh + 2],
                            fill=(255, 255, 255, 220))
                d.text((x + 4, 2), label, fill=(200, 40, 0, 255), font=font)
                # 左侧 y 标签
                d.rectangle([1, y + 2, 5 + tw, y + 6 + fh],
                            fill=(255, 255, 255, 220))
                d.text((3, y + 3), label, fill=(200, 40, 0, 255), font=font)
        return _save(Image.alpha_composite(img, overlay))
    except Exception:                                # noqa: BLE001
        return png


#: 点击标记的候选色 / 光标标记的候选色 —— 两组互斥。
#: 判据"红十字是上一次点落在哪、准星是我读到它在哪"要求两者分得开；
#: 自动选色只能在各自那组里挑，不能把点击选成青色。
_CLICK_COLORS = ((230, 30, 30), (255, 210, 0), (0, 200, 80),
                 (230, 0, 200), (255, 255, 255))
_CURSOR_COLORS = ((0, 180, 210), (30, 120, 255), (150, 80, 255))


def _dist2(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
            + (a[2] - b[2]) ** 2)


def pick_marker_color(img, x: int, y: int, rad: int, candidates):
    """在标记点周围较大一块区域里，挑一个"最不接近任何背景像素"的候选色。

    采样范围 = max(标记直径的 2 倍, 图短边的 1/16, 10px)。

    范围不是越大越好：真机色块墙上，48px 半径在 644x386 的放大图里跨到了
    相邻色块（每块仅约 214x193）→ 对整块区域"都不混"的色，对标记所在的那
    一小块反而对比不足。以标记尺度为主，兼顾"标记可能压在图标上"。
    判据是 max-min —— 对每个候选算"与区域内最接近它的像素的距离"，
    取最坏情况最好的那个，直接对应"标记落在区域里哪儿都不会混"。
    """
    try:
        sr = max(int(rad) * 4, min(img.width, img.height) // 16, 10)
        box = (max(0, x - sr), max(0, y - sr),
               min(img.width, x + sr), min(img.height, y + sr))
        region = img.crop(box).convert("RGB")
        # 降采样到几百个点：够代表这块区域，又不拖慢一次观察。
        small = region.resize((max(1, region.width // 8),
                               max(1, region.height // 8)))
        px = list(small.getdata())
        if not px:
            return candidates[0]
        best, best_score = candidates[0], -1.0
        for c in candidates:
            worst = min(_dist2(c, p) for p in px)
            if worst > best_score:
                best, best_score = c, worst
        return best
    except Exception:                                # noqa: BLE001
        return candidates[0]


def _ring_point(d, x: int, y: int, rad: int, color) -> None:
    """轻量标记：空心圆环，圆心留空。圆心就是位置。

    局部放大图的专用形态 —— 放大图里被观察的目标本身就小，红十字的臂
    与「LAST CLICK」标签会压在目标上。真实反馈：圆心画实心点会盖住
    1–2 像素的纤细目标（线端点 / 光标点），所以环里什么都不画。
    白色衬里是兜底：主体色由 `pick_marker_color` 选，但区域颜色多样时
    仍可能有一块跟它相近，衬里保证环的轮廓至少有一层看得见。
    """
    lw = max(1, rad // 4)
    box = [x - rad, y - rad, x + rad, y + rad]
    d.ellipse(box, outline=(255, 255, 255, 220), width=lw + 2)
    d.ellipse(box, outline=color, width=lw)


def click_marker(png: bytes, coord_rect, point, *, compact: bool = False) -> bytes:
    """在实际落点画红十字 + 圈 + 一句 `LAST CLICK`。

    `compact=True`（局部放大图）改画轻量版：空心圈 + 圆心点，不写文字 ——
    高倍放大时固定下限让十字臂与标签相对变大（实测 scale=8 时标签约占
    图宽 11%），会挡在要观察的目标上。
    """
    try:
        img = _open(png)
        l, t, r, b = coord_rect
        w = max(1, r - l)
        h = max(1, b - t)
        x = int(round((point[0] - l) * img.width / float(w)))
        y = int(round((point[1] - t) * img.height / float(h)))
        if not (0 <= x < img.width and 0 <= y < img.height):
            return png
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        if compact:
            rad = max(4, min(img.width, img.height) // 60)
            rgb = pick_marker_color(img, x, y, rad, _CLICK_COLORS)
            _ring_point(d, x, y, rad, rgb + (240,))
            return _save(Image.alpha_composite(img, overlay))
        rad = max(10, min(img.width, img.height) // 70)
        # 颜色按"标记点周围一块区域"自动挑（满屏红叉/红底的装饰图上，
        # 写死的红标记会淹进背景）。候选集与光标标记互斥。
        color = pick_marker_color(img, x, y, rad, _CLICK_COLORS) + (235,)
        lw = max(2, rad // 4)
        d.line([(x - rad, y), (x + rad, y)], fill=color, width=lw)
        d.line([(x, y - rad), (x, y + rad)], fill=color, width=lw)
        d.ellipse([x - rad // 2, y - rad // 2, x + rad // 2, y + rad // 2],
                  outline=color, width=lw)
        # 不再画「LAST CLICK」白底标签：真机实测它的白底（约 67×19 图像素）
        # 正好横穿一条 2px 的线，把要看的线截断了。十字 + 圈本身已经
        # 认得出来（回执里也写着"红十字画的是实际落点"）。
        return _save(Image.alpha_composite(img, overlay))
    except Exception:                                # noqa: BLE001
        return png


def cursor_marker(png: bytes, coord_rect, point, *,
                  compact: bool = False) -> bytes:
    """把上次 `cursor_position` 读到的光标位置画成青色准星 + 一句 `CURSOR`。

    `compact=True`（局部放大图）同样换成空心圈 + 圆心点，理由同 `click_marker`。

    与 `click_marker` 的红十字必须分得开：红十字是"上一次点落在哪"，
    这里画的是"报数那一刻光标在哪"。两者同形的话，模型会把"我读到它在这"
    读成"我点中了这"。

    只标一次（`session.take_cursor`），而且报数之后再画 —— 中间的移动
    由回执里那句"这是读数那一刻的位置"负责说清楚，不靠图。
    """
    try:
        img = _open(png)
        l, t, r, b = coord_rect
        w = max(1, r - l)
        h = max(1, b - t)
        x = int(round((point[0] - l) * img.width / float(w)))
        y = int(round((point[1] - t) * img.height / float(h)))
        if not (0 <= x < img.width and 0 <= y < img.height):
            return png
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        if compact:
            rad = max(4, min(img.width, img.height) // 60)
            rgb = pick_marker_color(img, x, y, rad, _CURSOR_COLORS)
            _ring_point(d, x, y, rad, rgb + (240,))
            return _save(Image.alpha_composite(img, overlay))
        rad = max(8, min(img.width, img.height) // 90)
        color = pick_marker_color(img, x, y, rad, _CURSOR_COLORS) + (235,)
        lw = max(2, rad // 4)
        # 准星：四段短线 + 中间一个空心圈（不与红十字的实心交叉同形）。
        d.line([(x - rad, y), (x - rad // 2, y)], fill=color, width=lw)
        d.line([(x + rad // 2, y), (x + rad, y)], fill=color, width=lw)
        d.line([(x, y - rad), (x, y - rad // 2)], fill=color, width=lw)
        d.line([(x, y + rad // 2), (x, y + rad)], fill=color, width=lw)
        d.ellipse([x - rad // 2, y - rad // 2, x + rad // 2, y + rad // 2],
                  outline=color, width=lw)
        font = _font(max(13, rad))
        if font is not None:
            label = "CURSOR"
            fh = getattr(font, "size", 11)
            tw = d.textlength(label, font=font)
            d.rectangle([x + rad + 2, y - fh // 2 - 2,
                         x + rad + tw + 8, y + fh // 2 + 2],
                        fill=(255, 255, 255, 225))
            d.text((x + rad + 5, y - fh // 2), label, fill=color, font=font)
        return _save(Image.alpha_composite(img, overlay))
    except Exception:                                # noqa: BLE001
        return png


def element_marks(png: bytes, coord_rect, elements, *,
                  max_marks: int = 80) -> tuple[bytes, list[int]]:
    """把元素的 `[N]` 画在图上，返回 (新图, 实际画了的编号)。

    ## 为什么这件事值得做

    模型给坐标是一次空间回归：它得把"这个按钮在哪"估成两个数，像素级误差
    直接变成点偏。给出编号以后同一件事变成分类：它在候选里挑一个。
    `element_index` 那条路本来就有（精确点击），缺的只是让模型在图上看见
    哪个编号对应哪个框 —— 树文本里 250 行，模型对不上号。

    ## 三条规矩

    - 编号沿用元素树的 `[N]`，不重新排序。重排之后"图上的 3"与"树里的 [3]"
      不是同一个东西，而这个函数唯一的功能判据就是两边对得上。
    - 只画 `coord_rect` 内的、`enabled` 且非 `offscreen` 的元素。画到图外等于
      教模型点一个看不见的目标；画禁用元素等于给它一条必失败的路。
    - 超过 `max_marks` 就截断（默认 80）。再多的编号会糊成一片，模型也数不过来；
      截断的事实由调用方在文本里说出来。
    """
    try:
        img = _open(png)
        w, h = img.size
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        font = _font(max(12, min(w, h) // 70))
        frame = (0, 150, 235, 235)
        lw = max(2, min(w, h) // 500)
        fh = getattr(font, "size", 11) if font is not None else 11
        l, t, r, b = coord_rect
        boxes: list[tuple[int, int, int, int, str]] = []
        for el in elements or ():
            if len(boxes) >= max(0, int(max_marks)):
                break
            if getattr(el, "offscreen", False) or not getattr(el, "enabled", True):
                continue
            rect = getattr(el, "rect", None)
            if not rect or len(rect) != 4:
                continue
            try:
                if (rect[2] <= l or rect[0] >= r
                        or rect[3] <= t or rect[1] >= b):
                    continue
                x1, y1, x2, y2 = _map_rect(coord_rect, w, h, rect)
            except Exception:                        # noqa: BLE001 - 坏矩形跳过
                continue
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            boxes.append((x1, y1, x2, y2, str(getattr(el, "index", ""))))
        if not boxes:
            return png, []
        for x1, y1, x2, y2, _label in boxes:
            d.rectangle([x1, y1, x2, y2], outline=frame, width=lw)
        for x1, y1, x2, y2, label in boxes:
            if not label:
                continue
            if font is not None:
                tw = d.textlength(label, font=font)
            else:
                tw = 6 * len(label)
            # 标签默认贴在框的上沿外侧；贴出图外就翻到内侧
            ty = y1 - fh - 3
            if ty < 0:
                ty = y1 + 1
            tx = x1
            if tx + tw + 6 > w:
                tx = max(0, x2 - tw - 6)
            d.rectangle([tx, ty, tx + tw + 6, ty + fh + 3],
                        fill=(255, 255, 255, 235))
            d.text((tx + 3, ty + 1), label, fill=(0, 70, 190, 255), font=font)
        return _save(Image.alpha_composite(img, overlay)), [int(b[4]) for b in boxes if b[4].isdigit()]
    except Exception:                                # noqa: BLE001
        return png, []


def crop_rect_for(rect, zx: int, zy: int, scale: int):
    """0–1000 的中心 + 倍率 -> 物理裁剪矩形（夹在窗口内）。

    纯函数，测试直接打它（不需要截图 / 不需要窗口）。
    """
    l, t, r, b = rect
    w = max(1, r - l)
    h = max(1, b - t)
    scale = max(2, min(8, int(scale or 3)))
    cw = max(24, w // scale)
    ch = max(24, h // scale)
    cx = l + int(round(max(0, min(1000, int(zx))) / 1000.0 * w))
    cy = t + int(round(max(0, min(1000, int(zy))) / 1000.0 * h))
    x1 = max(l, min(r - cw, cx - cw // 2))
    y1 = max(t, min(b - ch, cy - ch // 2))
    return (x1, y1, x1 + cw, y1 + ch)
