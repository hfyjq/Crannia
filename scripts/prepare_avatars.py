# -*- coding: utf-8 -*-
"""群成员头像 —— 本项目自绘的 8 张像素头像。

    .venv/Scripts/python.exe scripts/prepare_avatars.py
    .venv/Scripts/python.exe scripts/prepare_avatars.py --check
    .venv/Scripts/python.exe scripts/prepare_avatars.py --check --out <别的目录>

## 为什么自绘

外部生成的 2048² 图（即便裁掉图上的生成水印）来源的再分发权不明确
不能作为 Apache-2.0 项目的随包素材。
这里用纯代码画的像素头像：没有外部源图、没有水印、没有版权问题。

## 产物契约

  · 槽位 `slot-0 .. slot-7`（`core/groups.AVATAR_SLOTS` 与
    `packages/ui/src/avatar.js::AVATAR_SLOTS` 两边都是 8）；
  · 160×160 RGBA；
  · 圆外透明（角落 alpha=0）、中心不透明（alpha=255）。

输出一律 ASCII（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "packages" / "ui" / "src" / "assets" / "avatar"

SIZE = 160
SLOTS = 8
PX = SIZE // 16                 # 16×16 的像素格

#: (背景, 皮肤, 头发, 衣服, 特征色)
PALETTES = (
    ((223, 233, 224), (250, 216, 183), (58, 48, 44), (74, 110, 92), (70, 70, 70)),
    ((233, 224, 238), (240, 200, 165), (92, 64, 51), (92, 74, 130), (70, 70, 70)),
    ((224, 233, 245), (255, 224, 196), (196, 150, 74), (58, 96, 150), (70, 70, 70)),
    ((245, 235, 220), (214, 168, 128), (40, 40, 40), (168, 88, 80), (70, 70, 70)),
    ((224, 242, 236), (188, 140, 102), (30, 30, 30), (60, 130, 120), (70, 70, 70)),
    ((240, 228, 236), (245, 206, 172), (150, 92, 60), (120, 90, 150), (70, 70, 70)),
    ((230, 232, 218), (255, 214, 180), (70, 70, 80), (90, 120, 70), (70, 70, 70)),
    ((228, 230, 244), (210, 160, 120), (120, 70, 50), (150, 100, 70), (70, 70, 70)),
)


def _px(d, x, y, w, h, color):
    d.rectangle([x * PX, y * PX, (x + w) * PX - 1, (y + h) * PX - 1], fill=color)


def avatar(i: int):
    """画第 *i* 张（0..7）。纯几何，无外部素材。"""
    from PIL import Image, ImageDraw

    bg, skin, hair, shirt, dark = PALETTES[i % len(PALETTES)]
    im = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([0, 0, SIZE - 1, SIZE - 1], fill=bg + (255,))     # 圆外透明

    _px(d, 4, 12, 8, 4, shirt)                                  # 身体
    _px(d, 4, 4, 8, 8, skin)                                    # 头
    _px(d, 4, 3, 8, 2, hair)                                    # 头发
    if i % 4 == 0:
        _px(d, 3, 4, 1, 3, hair); _px(d, 12, 4, 1, 3, hair)
    elif i % 4 == 1:
        _px(d, 4, 3, 8, 3, hair)
    elif i % 4 == 2:
        _px(d, 3, 3, 10, 2, hair); _px(d, 3, 5, 1, 2, hair)
    else:
        _px(d, 4, 3, 3, 1, hair); _px(d, 9, 3, 3, 1, hair)
    _px(d, 6, 7, 1, 1, dark)                                    # 眼
    _px(d, 9, 7, 1, 1, dark)
    _px(d, 7, 9, 2, 1, dark)                                    # 嘴

    if i == 0:                      # 眼镜
        _px(d, 5, 6, 2, 1, dark); _px(d, 9, 6, 2, 1, dark)
    elif i == 1:                    # 腮红
        _px(d, 4, 9, 1, 1, (230, 140, 140)); _px(d, 11, 9, 1, 1, (230, 140, 140))
    elif i == 2:                    # 蝴蝶结
        _px(d, 8, 2, 2, 1, (200, 80, 120))
    elif i == 3:                    # 雀斑
        _px(d, 5, 8, 1, 1, dark); _px(d, 10, 8, 1, 1, dark)
    elif i == 4:                    # 耳机
        _px(d, 3, 5, 1, 3, dark); _px(d, 12, 5, 1, 3, dark); _px(d, 4, 3, 8, 1, dark)
    elif i == 5:                    # 胡子
        _px(d, 6, 9, 4, 1, hair)
    elif i == 6:                    # 帽子
        _px(d, 3, 2, 10, 2, (70, 110, 170))
    elif i == 7:                    # 耳环
        _px(d, 3, 8, 1, 1, (220, 180, 60)); _px(d, 12, 8, 1, 1, (220, 180, 60))
    return im


def build(out_dir: Path = OUT_DIR) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(SLOTS):
        p = out_dir / ("slot-%d.png" % i)
        avatar(i).save(str(p), "PNG", optimize=True)
        print("slot-%d (%dx%d)" % (i, SIZE, SIZE))
    print("wrote %d avatars under %s" % (SLOTS, out_dir))
    return 0


def check(out_dir: Path = OUT_DIR) -> int:
    from PIL import Image

    bad = []
    names = sorted(p.name for p in out_dir.glob("slot-*.png")) if out_dir.is_dir() else []
    if names != ["slot-%d.png" % i for i in range(SLOTS)]:
        bad.append("slot files = %r" % (names,))
    for i in range(SLOTS):
        p = out_dir / ("slot-%d.png" % i)
        if not p.is_file():
            continue
        im = Image.open(p)
        if im.size != (SIZE, SIZE):
            bad.append("%s size=%r" % (p.name, im.size))
        if im.mode != "RGBA":
            bad.append("%s mode=%s" % (p.name, im.mode))
        px = im.convert("RGBA").load()
        if px[0, 0][3] != 0 or px[SIZE - 1, 0][3] != 0:
            bad.append("%s corner alpha != 0" % p.name)
        if px[SIZE // 2, SIZE // 2][3] != 255:
            bad.append("%s center alpha != 255" % p.name)
    if bad:
        print("avatar check FAILED:")
        for b in bad:
            print("  - " + b)
        return 1
    print("avatar check OK: %d files, %dx%d RGBA, corner transparent"
          % (SLOTS, SIZE, SIZE))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default=str(OUT_DIR))
    a = ap.parse_args(argv)
    return check(Path(a.out)) if a.check else build(Path(a.out))


if __name__ == "__main__":
    raise SystemExit(main())
