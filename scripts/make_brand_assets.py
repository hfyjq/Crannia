#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成品牌素材 —— 可复现，不依赖外部素材。

    .venv/Scripts/python.exe scripts/make_brand_assets.py

产出：

  · `web/static/img/logo.svg`          —— 干净的绿色像素 C（透明底、无网格/圆点）
  · `crannia.ico` / `lf logo_256x256.ico` —— 同一设计的方形多尺寸图标

设计：取自参考图，只取绿色 C 本体：
3 格宽 × 4 格高，左上/左下两角削掉：

    .##
    #..
    #..
    .##

全部素材是我们自己画的，与 Apache-2.0 发布无版权冲突。
颜色与几何一处定义；改设计只改这里，再跑一次。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

_ROOT = Path(__file__).resolve().parent.parent

GREEN = (107, 191, 64)

#: 像素 C 的格子：`(row, col)`，3 列 × 4 行（左上/左下两角削掉）
C_CELLS = (
    (0, 1), (0, 2),
    (1, 0),
    (2, 0),
    (3, 1), (3, 2),
)

#: 方形画布 4×4（C 占 3×4，水平居中，x 偏移 0.5）
VIEW = 4
X_OFFSET = 0.5


def logo_svg() -> str:
    """返回 4×4 viewBox 的 SVG（透明底，只有绿色像素块）。"""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4" '
        'width="256" height="256" shape-rendering="crispEdges">',
        '<title>Crannia pixel C</title>',
        f'<g fill="#{GREEN[0]:02x}{GREEN[1]:02x}{GREEN[2]:02x}">',
    ]
    for r, c in C_CELLS:
        parts.append(f'<rect x="{X_OFFSET + c:g}" y="{r}" width="1" height="1"/>')
    parts.append('</g>')
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def _draw_logo(px: int) -> Image.Image:
    """画一张 *px*×*px* 的 RGBA 图（透明底，像素 C 居中）。"""
    im = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    unit = px / VIEW
    for r, c in C_CELLS:
        x0 = (X_OFFSET + c) * unit
        y0 = r * unit
        d.rectangle([round(x0), round(y0),
                     round(x0 + unit) - 1, round(y0 + unit) - 1],
                    fill=GREEN + (255,))
    return im


def write_logo_and_icons() -> None:
    svg = _ROOT / "web" / "static" / "img" / "logo.svg"
    svg.write_text(logo_svg(), encoding="utf-8")
    print("[asset] wrote", svg.relative_to(_ROOT))

    canvas = _draw_logo(1024)
    for name in ("crannia.ico",):
        p = _ROOT / name
        canvas.save(str(p), format="ICO",
                    sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                           (32, 32), (16, 16)])
        print("[asset] wrote", name)


def main() -> int:
    write_logo_and_icons()
    import prepare_avatars                  # 同目录（脚本目录自动在 sys.path 上）
    prepare_avatars.build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
