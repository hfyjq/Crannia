"""列出弹出菜单里每个元素支持哪些无障碍模式。

## 它回答的问题

「元素支持 Invoke」只说明这个方法存在，不说明控件按元素执行了它。
如果同一个元素还支持 LegacyIAccessible，那条路走的是 MSAA 的
`accDoDefaultAction`，与 Invoke 可能不是同一个实现 —— 可以拿来做对照。

这个探针只问「支持哪些」，不执行任何动作，所以菜单不会被关掉。

## 用法

菜单开着时跑：

    cd <仓库根>
    export PYTHONIOENCODING=utf-8
    .venv/Scripts/python.exe scripts/probe_menu_patterns.py
    .venv/Scripts/python.exe scripts/probe_menu_patterns.py 0xB05C8

ASCII 输出。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.computer import capture, uia, win32  # noqa: E402

#: 我们认识的模式（ID 见 UIAutomationClient.h）。Legacy 走 MSAA。
_PATTERNS = (
    (10000, "Invoke"),
    (10002, "Value"),
    (10005, "ExpandCollapse"),
    (10008, "SelectionItem"),
    (10015, "Toggle"),
    (10017, "ScrollItem"),
    (10018, "LegacyIAccessible"),
)


def _pick_window(argv: list[str]) -> int:
    if len(argv) > 1:
        return int(argv[1], 0)
    pops = [w for w in win32.enum_windows(include_invisible=True,
                                          include_tool_windows=True)
            if capture.is_popup(w) and w.visible]
    if not pops:
        print("没有开着的弹出菜单。先把菜单打开，或直接给 hwnd。")
        return 0
    print("using hwnd=0x%X class=%s pid=%d"
          % (int(pops[0].hwnd), pops[0].class_name, pops[0].pid))
    return int(pops[0].hwnd)


def _modes_for(hwnd: int, el) -> list[str]:
    """这个名字下的元素支持哪些模式（只问不执行）。"""
    out = []
    try:
        raw = uia.resolve(hwnd, el.path)
    except uia.UIAError:
        return ["<gone>"]
    try:
        for pid, name in _PATTERNS:
            try:
                pat = uia._pattern(raw, pid)
            except Exception:                    # noqa: BLE001 - 问不到就是没有
                pat = None
            if pat:
                out.append(name)
                uia._release(pat)
    finally:
        uia._release(raw)
    return out


def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("Windows only")
        return 2
    hwnd = _pick_window(argv)
    if not hwnd:
        return 1
    snap = uia.snapshot(hwnd, max_elements=200)
    print("elements: %d" % len(snap.elements))
    summary: dict[str, int] = {}
    for el in snap.elements:
        modes = _modes_for(hwnd, el)
        for m in modes:
            summary[m] = summary.get(m, 0) + 1
        print("  [%2d] %-12s id=%-6s %s"
              % (el.index, el.control_type, el.automation_id or "-",
                 ",".join(modes) or "<none>"))
    print("")
    print("summary: " + (", ".join("%s=%d" % kv for kv in sorted(summary.items()))
                         or "none"))
    print("")
    print("拿来做对照的那条：同一批元素里 LegacyIAccessible 与 Invoke 都在的，"
          "可以分别试一次，看哪条真正执行了那个命令。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
