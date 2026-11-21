"""标志与头部布局的断言。

这三条都不是"样式问题"，是改错了不会报错的那类：

· 标志换个字符就可能从 C 变回方括号、或者竖线细得不像同一笔，
  跑起来一切正常，只有人眼看得出来；
· 标志或文字比容器高一行时，最后一行会被直接裁掉，
  界面上只是"少了一条线"；
· 抬头该居中却靠左时，没有任何报错。

所以钉三件事：形状的接法、容器装得下它、以及居中写在样式表里。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import ui  # noqa: E402

_n = 0

#: 整块。竖线用它：半行高的横线与整格宽的竖线等粗（终端格子高约是宽的两倍）。
#: 右半块 `▐` 只有半格宽，夹在两条横线之间看起来像萎缩了。
VERTICAL = "█"

#: 标志下面的文字最多四行：标题 / 模型与档位 / 分隔线 / 目录。
TEXT_ROWS = 4


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def css_block(selector: str) -> str:
    """取样式表里某个选择器的规则体。找不到返回空串。"""
    css = (HERE / "crannia_tui" / "app.tcss").read_text(encoding="utf-8")
    m = re.search(r"^" + re.escape(selector) + r"\s*\{(.*?)\}", css, re.S | re.M)
    return m.group(1) if m else ""


def header_height() -> int:
    """从样式表里读出头部高度。

    刻意不写死数字：测试要钉的是"容器装得下内容"这条关系，
    而不是"高度等于 8"—— 将来改标志行数时，高度跟着改就该通过。
    """
    m = re.search(r"height:\s*(\d+)", css_block("HeaderBar"))
    return int(m.group(1)) if m else 0


def main() -> int:
    rows = ui.LOGO.split("\n")

    # 1) 形状：两条横线 + 中间两行竖线。少于三行拼不出 C，
    #    多到五行以上在头部里就比下面的文字高太多。
    check(3 <= len(rows) <= 5, f"标志行数应在 3..5，实际 {len(rows)}")

    top, bottom = rows[0], rows[-1]
    middle = rows[1:-1]
    check(len(middle) >= 2, f"竖线至少要有两行，否则笔画比例失衡（实际 {len(middle)}）")

    # 2) 左上、左下两格必须是空的 —— 这是"是 C 不是 ["的唯一判据。
    #    填上这两格，三笔在最左侧连成直线，读出来就是方括号。
    check(top.startswith(" "), "左上角必须空出一格，否则读成方括号 [")
    check(bottom.startswith(" "), "左下角必须空出一格，否则读成方括号 [")

    # 3) 横线比竖线右缩一格，于是竖线的右边缘正好抵住横线的起点。
    #    缩两格以上会断开，缩零格就是上面那个方括号。
    bar_left = len(top) - len(top.lstrip(" "))
    check(bar_left == 1, f"横线应比竖线右缩一格，实际缩进 {bar_left}")
    check(bottom.index(bottom.lstrip(" ")) == bar_left, "上下两条横线的缩进必须一致")

    # 4) 竖线是整块，不能退回右半块。
    #    半格宽 + 半格高的横线 ⇒ 竖线只有横线一半粗，看上去像萎缩了。
    check(VERTICAL == "█", "竖线常量应是整块 U+2588")
    for i, row in enumerate(middle, start=2):
        check(row.startswith(VERTICAL), f"第 {i} 行应以整块起头，实际 {row[:1]!r}")
    check("▐" not in ui.LOGO, "标志里不许再出现右半块 ▐（竖线会只有一半粗）")

    # 5) 上下两条横线长度相同，否则 C 一头长一头短，缺口看着歪。
    check(len(top.strip()) == len(bottom.strip()),
          "上下横线长度应相等，否则缺口不对称")

    # 6) 容器装得下：标志 + 最多四行文字。装不下时最后一行被裁掉，
    #    界面上只表现为"少一条线"，没有任何报错。
    h = header_height()
    need = len(rows) + TEXT_ROWS
    check(h >= need, f"app.tcss 的 HeaderBar 高度 {h} 装不下 {need} 行")

    # 7) 居中：文字逐行居中写在样式表里；标志是定宽块，逐行居中会把它
    #    拆散，所以整块补同一段左空白（样式表的 align-horizontal 对定宽
    #    子部件不生效 —— 实测过）。
    check("text-align: center" in css_block("HeaderBar #info"),
          "#info 要有 text-align: center（文字逐行居中）")
    centered = ui._center_block(ui.LOGO, 20).plain.split("\n")
    pad = (20 - 5) // 2
    check(all(r.startswith(" " * pad) for r in centered),
          f"宽 20 时标志每行都应补 {pad} 个左空白")
    check([r[pad:] for r in centered] == rows,
          "补空白不许改动行内字符与相对位置")
    check(ui._center_block(ui.LOGO, 4).plain == ui.LOGO,
          "宽度装不下时补 0 个空白，不许截断标志")

    # 8) 分隔线：每个字形换一种颜色（设计意图是创造性与生命力），
    #    窄终端按宽度截断。
    full = ui._separator(200)
    glyphs = sum(1 for ch in ui._SEPARATOR_TEXT if ch != " ")
    check(len(ui._SEPARATOR_PALETTE) >= 10,
          f"调色板至少要 10 色，实际 {len(ui._SEPARATOR_PALETTE)}")
    check(len(full.spans) == glyphs,
          f"每个非空白字形应各占一段颜色，实际 {len(full.spans)} / {glyphs}")
    check(all(str(s.style) in ui._SEPARATOR_PALETTE for s in full.spans),
          "分隔线的每一段都必须取自调色板")
    check(len(set(str(s.style) for s in full.spans)) >= 10,
          "整条至少要把 10 种颜色都用上")
    check(len(full.plain) > 60, f"分隔线全长应在 60 字符以上，实际 {len(full.plain)}")
    check(len(ui._separator(10).plain) == 10, "分隔线宽 10 时应截成 10 个字符")

    # 9) 后台服务读数：关着要说清还有几条定时任务没跑；没有数就不占位置。
    text, warn = ui.services_label({"background_services": False, "cron_jobs": 3})
    check("关" in text and "3" in text and warn,
          "开关关着时要报出未启用的定时条数并染黄")
    check(ui.services_label({"background_services": True,
                             "channels_running": 1, "cron_jobs": 2})
          == ("通道 1 · 定时 2", False),
          "开关开着时报通道数与定时条数")
    check(ui.services_label({"background_services": True,
                             "channels_running": 0, "cron_jobs": 0})
          == ("", False),
          "没有通道也没有定时任务时这一格不出现")
    check(ui.services_label({"background_services": False, "cron_jobs": None})
          == ("后台服务：关", True),
          "读数拿不到时只说关着，不编一个 0")

    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
