# -*- coding: utf-8 -*-
"""从 `styles.css` 的浅色令牌推导深色令牌，写回同一个文件。

    .venv/Scripts/python.exe scripts/derive_dark_palette.py           # 生成并写入
    .venv/Scripts/python.exe scripts/derive_dark_palette.py --check   # 只比对，不写

## 为什么是脚本

`styles.css` 里 300 多个颜色令牌每个只定义一次，而深色迟早要改浅色那一侧
（新加一个令牌、调一个色值）。手抄两份必然漂，用"浅色是真相、深色是推导"
这条单向依赖，浅色改了重跑一次即可。

## 规则（按令牌家族）

| 家族 | 深色怎么算 |
|---|---|
| `*-bg-*`（大背景 / 框 / 各类淡色底） | 明度压到 0.07–0.26，饱和度乘 0.55 |
| `*-fg-*`（正文 / 次要文字） | 明度提到 0.72–0.90，饱和度乘 0.35 |
| `*-line`（边框 / 分隔线） | 明度压到 0.20–0.26 |
| `*-accent-*` / `danger` / `warn` 的 fg | 保留色相，明度提到 0.62–0.80 |
| `shadow`（`rgba(0,0,0,a)`） | alpha 乘 1.6（深色下投影要更实才看得见） |
| `on-danger` / `error` 上的白字 | 原样保留（红底白字在两种主题下都成立） |

值本身是 `var(--th-*)` 的令牌不重复定义 —— 它们跟着主题轴走。

## 边界

生成块由 `/* @generated dark-palette:start */` … `:end */` 夹住，只替换中间那段；
浅色那一侧一个字节都不动。别手改生成块 —— 下一跑会覆盖。
"""

from __future__ import annotations

import colorsys
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "packages" / "ui" / "src" / "styles.css"

START = "/* @generated dark-palette:start */"
END = "/* @generated dark-palette:end */"

#: 深色下那六个主题轴的默认值（浅色那份在 `:root` 里，由 `theme.js` 对账）。
#: 2026-11-02：从"带绿调的深色"改成中性深色（底/框/栏都不带色相），
#:   绿色只留在强调色（品牌三色）上 —— 见 `_convert` 里的 `neutral` 那一支。
DARK_AXES = {
    "--th-side-bg": "#17181a",
    "--th-side-fg": "#cfd2d4",
    "--th-app-bg": "#0f1011",
    "--th-box-bg": "#191a1c",
    "--th-box-fg": "#e6e7e8",
    "--th-select": "#2e3238",
}

_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")
_RGBA = re.compile(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$")


def _hex_to_rgb(h: str):
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _rgb_to_hex(r, g, b) -> str:
    return "#%02x%02x%02x" % (max(0, min(255, round(r))),
                              max(0, min(255, round(g))),
                              max(0, min(255, round(b))))


def _family(name: str) -> str:
    """令牌名 → 家族。顺序承重：先判 `*-line` 再判 `*-fg` / `*-bg`。"""
    if name.startswith("--c-on-danger") or name.startswith("--c-error-on"):
        return "keep"
    #: 判片段不判结尾：`--c-app-line-01` 这种带序号的名字才是常态，
    #: 判结尾会把它们全漏进 `fg` 那一族（生成出来是一片亮色边框）。
    if "-line" in name or name == "--line":
        return "line"
    if "-fg" in name or name in ("--muted", "--green", "--green-dark", "--clay"):
        return "fg"
    if "-bg" in name or name in ("--mint", "--side"):
        return "bg"
    if "shadow" in name:
        return "shadow"
    return "fg"


def _shift(name: str, fam: str, l: float, s: float) -> tuple:
    """浅色的 (明度, 饱和度) -> 深色的那一对。

    底色 / 框 / 文字 / 分隔线一律去色（中性灰深色）；只有状态色
    （danger / warn / error）与品牌三色保留色相。
    """
    tinted = ("danger", "warn", "error")
    if fam == "fg":
        #: 保留层次：浅色里"正文 / 次要 / 灰"的明度差在这里要继续存在，
        #:   所以给 0.52–0.94 这个跨度，而不是把它们压成同一档。
        l2 = 0.52 + l * 0.42
        if name in ("--green", "--green-dark", "--clay"):
            s2 = s * 0.75
        elif any(w in name for w in tinted):
            #: 状态色同一枚令牌身兼两职（红底白字 / 深色文字），
            #:   所以夹在 0.45–0.56：太亮白字读不清，太暗当文字看不见。
            l2 = min(0.56, max(0.45, l2))
            s2 = min(1.0, s * 0.85)
        else:
            s2 = 0.0
    elif fam == "bg":
        l2 = 0.07 + (1.0 - l) * 0.20
        s2 = s * 0.55 if any(w in name for w in tinted) else 0.0
    elif fam == "line":
        l2 = 0.19 + (1.0 - l) * 0.09
        s2 = s * 0.35 if any(w in name for w in tinted) else 0.0
    else:
        l2, s2 = l, s
    return min(0.92, max(0.05, l2)), min(1.0, s2)


def _convert(name: str, value: str):
    """返回深色值；`None` = 这个令牌不重复定义。"""
    value = value.strip()
    if value.startswith("var("):
        return None
    fam = _family(name)

    m = _HEX.match(value)
    if m:
        r, g, b = _hex_to_rgb(value)
        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        l2, s2 = _shift(name, fam, l, s)
        r2, g2, b2 = colorsys.hls_to_rgb(h, l2, s2)
        return _rgb_to_hex(r2 * 255, g2 * 255, b2 * 255)

    m = _RGBA.match(value)
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        if fam == "keep":
            return value
        #: 深色底上的投影要更实；纯黑再压一点亮度，避免与背景糊在一起。
        a2 = min(0.92, a * 1.6)
        if fam == "shadow":
            #: 投影也去色 —— 浅色那份是墨绿阴影，落在中性深色上会留一层绿雾。
            _h, _l, _s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
            _r, _g, _b = colorsys.hls_to_rgb(_h, _l, 0.0)
            return "rgba(%d,%d,%d,%.2f)" % (round(_r * 255), round(_g * 255),
                                            round(_b * 255), a2)
        #: 半透明底色也要走同一套中性化 —— 只调 alpha 的话，
        #:   `rgba(24,34,27,…)` 那种墨绿会在中性深色里留下绿雾。
        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        l2, s2 = _shift(name, fam, l, s)
        r2, g2, b2 = colorsys.hls_to_rgb(h, l2, s2)
        return "rgba(%d,%d,%d,%.2f)" % (round(r2 * 255), round(g2 * 255),
                                        round(b2 * 255), a2)
    if fam == "keep":
        return value
    return None


def _root_block(text: str) -> tuple[int, int]:
    i = text.find(":root{")
    if i < 0:
        raise SystemExit("styles.css 里没找到 :root{")
    depth, j = 0, i + len(":root{") - 1
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return i, j + 1
        j += 1
    raise SystemExit(":root 块没有结束")


def build(text: str) -> str:
    i, j = _root_block(text)
    block = text[i:j]
    pairs = re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;}]+)", block)
    lines = ["%s" % START,
             "/* 本块由 scripts/derive_dark_palette.py 生成 —— 别手改，改浅色那一侧后重跑。 */",
             ':root[data-theme="dark"]{']
    for name, val in DARK_AXES.items():
        lines.append("  %s: %s;" % (name, val))
    seen = set()
    for name, val in pairs:
        if name in DARK_AXES or name in seen:
            continue
        seen.add(name)
        dark = _convert(name, val)
        if dark is not None:
            lines.append("  %s: %s;" % (name, dark))
    lines.append("}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    text = io.open(CSS, encoding="utf-8").read()
    generated = build(text)
    if START in text and END in text:
        a = text.index(START)
        b = text.index(END) + len(END)
        new = text[:a] + generated.rstrip("\n") + text[b:]
    else:
        new = text.rstrip("\n") + "\n\n" + generated
    if "--check" in args:
        ok = new == text
        print("[dark] %s" % ("已是最新" if ok else "生成块与当前不一致 —— 重跑本脚本"))
        return 0 if ok else 1
    if new == text:
        print("[dark] 已是最新，没有写入")
        return 0
    io.open(CSS, "w", encoding="utf-8", newline="").write(new)
    n = generated.count(";")
    print("[dark] 已写入 %d 条令牌（含 6 个主题轴）" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
