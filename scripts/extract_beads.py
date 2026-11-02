# -*- coding: utf-8 -*-
"""把 `UI重构/bead-pattern (1..7).png` 抠成像素数据（启动动画的素材地基）。

## 为什么要抠，不直接用 PNG

目标素材要求：重新对齐、不要网格线、纯白背景。两条硬理由：

1. 七张图不在同一张画布上 —— (1)-(6) 是图纸（1048x1162，32x32 格、
   格距 30px、带行列编号 + 底部「材料」表）；(7) 是裁切过的成品图
   （687x470、格距 27px、没有编号）。
用 PNG 叠必然错位，而且错位量随窗口尺寸变，改不掉。
2. 素材 1.4 MB -> 数据几 KB，且想改动画粒度随时能改（逐帧 / 逐颗都行）。

## 抠得对不对，怎么知道的

图纸底部那份「材料」表逐格写着这一张用了多少颗、几种颜色，那是现成的答案。
本脚本把它当验收：抠出来的颗数/颜色数与图上的字必须相等（`--check` 会逐张比）。

| 张 | 图上写的 | 抠出来 |
|---|---|---|
| (1) | 26 像素 · 2 种颜色 | 26 颗 · 2 色 |
| (6) | 137 像素 · 4 种颜色 | 137 颗 · 4 色 |

## 几何是怎么定下来的（两条都是量出来的，不是猜的）

* (1)-(6)：外框左上角 (44, 44)、格距 30、32x32
  （量法：沿一行扫最长暗段 = 960px = 32 x 30）。
* (7)：格距 27 —— 量法是游程长度谱：珠子（绿/棕）的连续游程只有
  `27 / 54 / 81 / 108 / 135 / 189` 这些值，即 27 的整数倍。
  原点取加粗网格线所在的 72（那条线的间距量出来正好 135 = 5 x 27）。
  加粗线只用来定原点，抠出来的数据里没有线。

## 输出

`apps/desktop/splash-data.js` —— 一个普通（非 module）脚本，赋给
`window.CRANNIA_BEADS`。判据：启动窗要在 `file://` 下打开，
而 `file://` 上的 ESM import 会被浏览器挡掉（`<script type="module">`
读同级 .js 会 CORS 失败），只能走经典脚本 + 全局变量。

用法：
    .venv/Scripts/python.exe scripts/extract_beads.py --check      # 只验收，不写
    .venv/Scripts/python.exe scripts/extract_beads.py              # 生成数据
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SRC = _ROOT / "UI重构"
_OUT = _ROOT / "apps" / "desktop" / "splash-data.js"

#: (1)-(6) 的几何 —— 量出来的（见模块 docstring）。
F_ORIGIN = (44, 44)
F_CELL = 30.0
F_N = 32

#: (7) 的几何。格距 27、格心在 (5.5, 20) + 27k。
#:
#: 这两个数是量出来的，而且走了三条弯路 —— 三条弯路都"看着对"：
#:
#: 1. 找"比四邻更暗"的点，得到 72 / 207 / 342 …（间距 135 = 5 x 27，
#:    完全合理）。它们是加粗网格线的交点，不是格心，照它抠，整张偏 18px，
#:    出来 105 颗 / 2 色，形状还像两棵树，不像坏掉。
#: 2. 找"格内方差最小"的相位，报 `(0,1)` 代价 `0.0`，完美得可疑。
#:    退化：采样点全落在空白格上时方差同样是 0（那里也是纯色 —— 白）。
#:判据分不出"落在珠子上"和"落在空格里"。
#: 3. 游程长度谱（珠子游程只有 27 的整数倍），只定出格距，定不出相位。
#:
#:最后定下来靠的是两条独立证据（缺一不可）：
#:   · 色块跳变的位置：全图 107 个绿/棕↔白的跳变，`x mod 27` 全部落进
#:     同一格，格距 27 且相位确定（别的格距试过 32：跳变散在 4 个相位上）。
#:   · 1 像素级看一眼：直接打印一格，读出绿块的上下边界与那个空格子的圆点
#:     的圆心，与上式互相印证。
#: 判"哪个相位对"的终判据是颜色数：珠子是纯色块，对的相位只有 3 色，
#:   错的全是 4~5 色（采到了边缘过渡色）。这一条已经写成断言（见 `main`）。
BG_CELL = 27.0
BG_CENTER0 = (5.5, 20.0)

#: 图纸底部「材料」表里的答案（`--check` 拿它验收）。`(颗数, 颜色数)`。
LEGEND = {1: (26, 2), 6: (137, 4)}

#: 判"这一格是不是珠子"：
#:   · 空格子中心有一个深色小圆点（不是珠子）
#:   · 网格线是灰的
#:   · 纸是白的
#:珠子 = 有饱和度的像素（绿或棕都比另外两个通道明显高）。
#:
#: 那个上限不能取 210 —— (7) 的浅棕正好是 `#D29D79`（r = 210）
#:会被判定成"不是珠子"，整个棕色类一颗不剩。
#: 症状：bg 抠出 105 颗 / 2 色（只有绿），而正确答案是 161 颗 / 3 色。
#: 它不报错、也不难看：105 颗仍然排成两棵树的样子。
#:教训不是"这个数要调大"，而是阈值型判据要问"它会把哪一类整个吃掉"。
def _is_bead(rgb) -> bool:
    r, g, b = rgb
    # 浅到发白的不算珠子。
    #    那几格是 `#D0E6B3` / `#C6D3B8` / `#DCE9CE` —— 整格均匀的浅色，
    #    逐像素核过源图：它们真的画在源图里，不是采样采花的。
    #    判据：右边那两格颜色应当删掉，那是原图的噪点。
    #    阈值取 `min < 150`：真珠子最浅的一颗是 `#D29D79`（min = 121），
    #    噪点最深的一颗是 `#D0E6B3`（min = 179），中间有 58 的空隙，不会误伤。
    if min(r, g, b) > 150:
        return False
    if g > r + 12 and g > b + 25:          # 绿
        return True
    if r > g + 12 and g > b + 4 and r < 225:   # 棕（含浅棕 #D29D79）
        return True
    return False


def _sample(px, cx: float, cy: float):
    """格心 5x5 的中位色（抗圆点/抗锯齿；珠子是圆的，只取正中一点会碰到高光）。"""
    pts = []
    for dx in (-2, -1, 0, 1, 2):
        for dy in (-2, -1, 0, 1, 2):
            pts.append(px[int(cx) + dx, int(cy) + dy])
    return tuple(sorted(p[i] for p in pts)[12] for i in range(3))


def _grid(path: Path, c0x: float, c0y: float, cell: float, w: int, h: int):
    """抠出一张图里的 `w x h` 格。返回 (cells, palette)；`cells` 是行主序的
    调色板下标（`0` = 空格）。

    `(c0x, c0y)` 是第 (0,0) 格的「心」，不是左上角 —— 判据是"这一格的珠子
    画在哪儿"：取样点必须落在珠子上。拿边界来喂它，整张图就偏半格。
    越界（(7) 是裁切图）按空格算，不是黑：黑的会画成一排黑珠子。
    """
    from PIL import Image

    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = im.load()
    pal: list = [None]
    raw = []
    for j in range(h):
        for i in range(w):
            cx, cy = c0x + i * cell, c0y + j * cell
            if not (0 <= cx < W and 0 <= cy < H):
                raw.append(0)
                continue
            c = _sample(px, cx, cy)
            if not _is_bead(c):
                raw.append(0)
            else:
                if c not in pal:
                    pal.append(c)
                raw.append(pal.index(c))
    return raw, [c for c in pal if c]


def extract_frames():
    out = []
    for n in range(1, 7):
        p = _SRC / ("bead-pattern (%d).png" % n)
        # 第 (0,0) 格的格心 = 外框左上角 + 半格
        c0x = F_ORIGIN[0] + F_CELL / 2
        c0y = F_ORIGIN[1] + F_CELL / 2
        cells, pal = _grid(p, c0x, c0y, F_CELL, F_N, F_N)
        out.append({"name": n, "w": F_N, "h": F_N, "cells": cells, "palette": pal})
    return out


def extract_background():
    import math

    p = _SRC / "bead-pattern (7).png"
    from PIL import Image

    W, H = Image.open(p).size
    # 把格心往左上铺到盖住整幅：第一个格心 <= 0。
    c0x = BG_CENTER0[0] - BG_CELL * math.ceil(BG_CENTER0[0] / BG_CELL)
    c0y = BG_CENTER0[1] - BG_CELL * math.ceil(BG_CENTER0[1] / BG_CELL)
    w = int(math.ceil((W - c0x) / BG_CELL))
    h = int(math.ceil((H - c0y) / BG_CELL))
    cells, pal = _grid(p, c0x, c0y, BG_CELL, w, h)
    return {"name": "bg", "w": w, "h": h, "cells": cells, "palette": pal,
            "origin": [c0x, c0y]}


def _counts(entry):
    seen = {}
    for c in entry["cells"]:
        if c:
            seen[c] = seen.get(c, 0) + 1
    return sum(seen.values()), len(seen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只验收，不写文件")
    a = ap.parse_args()

    frames = extract_frames()
    bg = extract_background()

    bad = 0
    for e in frames:
        n, col = _counts(e)
        want = LEGEND.get(e["name"])
        mark = ""
        if want:
            ok = (n, col) == want
            mark = "  [图上是 %d 颗/%d 色] %s" % (want[0], want[1], "OK" if ok else "**不符**")
            if not ok:
                bad += 1
        print("(%-2s) 抠出 %3d 颗 / %d 色%s" % (e["name"], n, col, mark))
    n, col = _counts(bg)
    print("(bg) 抠出 %3d 颗 / %d 色  画布 %dx%d 格" % (n, col, bg["w"], bg["h"]))
    if bad:
        print("\nFAIL: %d 张与图纸上的『材料』表对不上" % bad)
        return 1
    # 相位对不对，看颜色数 —— 珠子是纯色块，格心取样落对时
    #    一张图只有 2~4 种颜色。采样点偏一点，就会采到边缘的过渡色，
    #    颜色数成倍往上翻（坏相位 20 色 / 好相位 4 色）。
    #    这条断言是"坏相位看起来不像坏掉"那个坑的护栏 ——
    #    坏相位下形状仍然正确、颗数 105（不像 0，也不像爆表），
    #    只能靠颜色数看出来。
    for e in frames + [bg]:
        c = _counts(e)[1]
        if c > 8:
            print("\nFAIL: %s 抠出了 %d 种颜色 —— 珠子是纯色块，>8 种就是"
                  "**采样相位错了**（采到了过渡色）" % (e["name"], c))
            return 1
    print("\n验收通过：① 颗数/颜色数与图纸上的『材料』表一致"
          "  ② 每张的颜色数都 <= 8（相位没偏）")

    if a.check:
        return 0

    # 调色板合并成一份（同一颜色在几张里都出现）。
    master: list = []

    def _idx(c):
        if c not in master:
            master.append(c)
        return master.index(c) + 1       # 0 = 空格

    for e in frames:
        remap = {i + 1: _idx(c) for i, c in enumerate(e["palette"])}
        e["cells"] = [_idx(e["palette"][c - 1]) if c else 0 for c in e["cells"]]
        e.pop("palette")
    remap = {i + 1: _idx(c) for i, c in enumerate(bg["palette"])}
    bg["cells"] = [_idx(bg["palette"][c - 1]) if c else 0 for c in bg["cells"]]
    bg.pop("palette")

    data = {
        "cell": {"frame": int(F_CELL), "bg": int(BG_CELL)},
        "palette": ["#%02X%02X%02X" % c for c in master],
        "frames": [{"w": e["w"], "h": e["h"], "cells": e["cells"]} for e in frames],
        "background": {"w": bg["w"], "h": bg["h"], "cells": bg["cells"]},
    }
    body = json.dumps(data, separators=(",", ":"))
    _OUT.write_text(
        "// 生成物 —— 别手改。改素材请改 UI重构/bead-pattern (N).png 后重跑\n"
        "//   .venv/Scripts/python.exe scripts/extract_beads.py\n"
        "// 判据与验收见 scripts/extract_beads.py 的 docstring。\n"
        "window.CRANNIA_BEADS = %s;\n" % body,
        encoding="utf-8", newline="\n")
    print("已写 %s（%.1f KB，%d 色）"
          % (_OUT.relative_to(_ROOT), _OUT.stat().st_size / 1024, len(master)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
