"""截图的标注层 —— 只动图，不动 UI。

三件事：

1. `crop_zoom`：按 0–1000 比例的中心 / 倍率裁一块并放大（微信那种自绘 UI 的主力）。
2. `grid`：在图上画 10% 一格的带标签网格（模型可以直接读比例，不用估像素）。
3. `click_marker`：在上一击的实际落点画红十字（连续校准，不靠三角测试）。

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


def grid(png: bytes) -> bytes:
    """10% 一格的带标签网格（标签就是 100…900 的比例值）。"""
    try:
        img = _open(png)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        w, h = img.size
        line_w = max(1, max(w, h) // 900)
        font = _font(max(13, min(w, h) // 55))
        fh = getattr(font, "size", 11) if font is not None else 11
        color = (255, 70, 0, 190)
        for i in range(1, 10):
            x = int(w * i / 10.0)
            y = int(h * i / 10.0)
            d.line([(x, 0), (x, h)], fill=color, width=line_w)
            d.line([(0, y), (w, y)], fill=color, width=line_w)
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


def click_marker(png: bytes, coord_rect, point) -> bytes:
    """在实际落点画红十字 + 圈 + 一句 `LAST CLICK`。"""
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
        rad = max(10, min(img.width, img.height) // 70)
        color = (230, 0, 0, 235)
        lw = max(2, rad // 4)
        d.line([(x - rad, y), (x + rad, y)], fill=color, width=lw)
        d.line([(x, y - rad), (x, y + rad)], fill=color, width=lw)
        d.ellipse([x - rad // 2, y - rad // 2, x + rad // 2, y + rad // 2],
                  outline=color, width=lw)
        font = _font(max(13, rad))
        if font is not None:
            label = "LAST CLICK"
            fh = getattr(font, "size", 11)
            tw = d.textlength(label, font=font)
            d.rectangle([x + rad + 2, y - fh // 2 - 2,
                         x + rad + tw + 8, y + fh // 2 + 2],
                        fill=(255, 255, 255, 225))
            d.text((x + rad + 5, y - fh // 2),
                   label, fill=(200, 0, 0, 255), font=font)
        return _save(Image.alpha_composite(img, overlay))
    except Exception:                                # noqa: BLE001
        return png


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
