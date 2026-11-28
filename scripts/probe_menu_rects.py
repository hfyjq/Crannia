"""对「此刻正开着的菜单」读一次菜单自己的项表，并和 UIA 的矩形对照。

## 它回答的问题

`#32768` 自绘菜单的 UIA `BoundingRectangle` 是估算值：项高按窗口高度均分，
与真实行高不一致，越往下偏得越多 → 按它点会落到相邻项上。
`win32.menu_items()` 走 `MN_GETHMENU` + `GetMenuItemRect`，读菜单自己的几何。

这个探针去问：目标程序到底有没有这张表，以及表里的矩形和 UIA 差多少。
有表 → 点击走表；没表 → 回执明说是估计值。

## 用法

先把菜单打开（别关），然后在另一个窗口跑：

    cd <仓库根>
    export PYTHONIOENCODING=utf-8
    .venv/Scripts/python.exe scripts/probe_menu_rects.py

只读不点 —— 跑完菜单还在，可以接着手动验证。`--all` 列出所有 `#32768`
（默认只报有项表的那些）。ASCII 输出。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.computer import capture, uia, win32  # noqa: E402


def _fmt(rect) -> str:
    return "%d,%d-%d,%d" % rect


def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("Windows only")
        return 2
    show_all = "--all" in argv
    popups = [w for w in win32.enum_windows(include_invisible=True,
                                            include_tool_windows=True)
              if capture.is_popup(w) and w.visible]
    if not popups:
        print("没有开着的菜单（#32768 / #32769）。先把目标菜单打开再跑。")
        return 1
    rc = 0
    for w in popups:
        items = win32.menu_items(w.hwnd)
        print("")
        print("[menu] %s hwnd=0x%X pid=%d rect=%s"
              % (w.class_name, int(w.hwnd), w.pid, _fmt(w.rect)))
        if not items:
            print("  Win32 menu table: NONE（这个弹出层没有 HMENU —— 坐标只能靠 UIA 估计）")
            if not show_all:
                rc = 1
            continue
        print("  Win32 menu table: %d items" % len(items))
        for i, (mid, text, rect) in enumerate(items[:12]):
            print("    [%2d] id=%-6d %-28r %s"
                  % (i, mid, (text or "")[:28], _fmt(rect)))
        if len(items) > 12:
            print("    ... %d more" % (len(items) - 12))

        try:
            snap = uia.snapshot(w.hwnd, max_elements=200)
        except uia.UIAError as exc:
            print("  UIA tree: unreadable (%s)" % exc)
            continue
        print("  UIA tree: %d elements" % len(snap.elements))
        for el in snap.elements[:6]:
            print("    uia[%2d] %-12s %-20r %s"
                  % (el.index, el.control_type, (el.name or "")[:20],
                     _fmt(el.rect)))
        n, ids = uia.align_with_menu(snap.elements, items)
        print("  align: %d/%d elements matched" % (n, len(items)))
        if n:
            for el in snap.elements[:6]:
                if el.index in ids:
                    print("    uia[%2d] -> menu id %d, rect %s"
                          % (el.index, ids[el.index], _fmt(el.rect)))
        else:
            rc = 1
    print("")
    print("done (rc=%d)" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
