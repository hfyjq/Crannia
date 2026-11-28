"""连读三次同一个窗口的 UIA 树，看元素顺序是否稳定。

## 它回答的问题

观察时记下的元素路径是「从根往下第几个孩子」。这个路径只在两次遍历
树形状相同时才指向同一个元素。自绘菜单可能会重绘、异步填充、随高亮
变化，那时路径会安静地指到别的元素上——不报错，只是打错目标。

这个探针连拍三次，比较：
  · 第 i 个元素在三次里是不是同一个（靠 control_type / automation_id /
    name / rect 认）；
  · 同一个 automation_id 在三次里是不是同一个下标。

两种结论对应两种修法：
  · 下标漂移：元素身份要换成 UIA RuntimeId，路径只作快速路径；
  · 下标稳定：身份没问题，错在控件对 Invoke 的实现。

## 用法

先把目标菜单打开（别关），然后：

    cd <仓库根>
    export PYTHONIOENCODING=utf-8
    .venv/Scripts/python.exe scripts/probe_uia_stability.py
    .venv/Scripts/python.exe scripts/probe_uia_stability.py 0xB05C8   # 指定 hwnd

只读不点。ASCII 输出。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.computer import capture, uia, win32  # noqa: E402


def _key(el) -> tuple:
    return (el.control_type, el.automation_id, el.name, el.rect)


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


def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("Windows only")
        return 2
    hwnd = _pick_window(argv)
    if not hwnd:
        return 1

    snaps = []
    for _ in range(3):
        snaps.append(uia.snapshot(hwnd, max_elements=200))
        time.sleep(0.3)
    for i, s in enumerate(snaps):
        print("snap%d: %d elements, %dms" % (i, len(s.elements), s.took_ms))

    base = snaps[0]
    worst = 0
    for i, s in enumerate(snaps[1:], start=1):
        moved = 0
        for a, b in zip(base.elements, s.elements):
            if _key(a) != _key(b):
                moved += 1
                if moved <= 10:
                    print("  pos %d:  %r\n        != %r"
                          % (a.index, _key(a), _key(b)))
        print("snap%d vs snap0: %d/%d positions differ (len %d vs %d)"
              % (i, moved, len(base.elements), len(base.elements), len(s.elements)))
        worst = max(worst, moved)

    # 同一个 automation_id 在三次里的下标
    ids = []
    for s in snaps:
        m = {}
        for el in s.elements:
            if el.automation_id:
                m.setdefault(el.automation_id, el.index)
        ids.append(m)
    all_ids = sorted(set(ids[0]) & set(ids[1]) & set(ids[2]))
    shifted = [k for k in all_ids
               if not (ids[0][k] == ids[1][k] == ids[2][k])]
    print("")
    print("common automation_ids: %d, moved index: %d" % (len(all_ids), len(shifted)))
    for k in shifted[:10]:
        print("  id %s: %d -> %d -> %d" % (k, ids[0][k], ids[1][k], ids[2][k]))

    print("")
    if worst or shifted:
        print("VERDICT: unstable -- element identity must not be a child path")
        return 1
    print("VERDICT: stable -- paths point at the same elements across reads")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
