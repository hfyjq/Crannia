"""接管的冒烟测试 —— 手动跑，不进测试套件。

它做四件真事情：

1. 开一个 Tk 窗口（我们自己的），用 UIA 读它的树；
2. 用 SendInput 点它的按钮、往输入框里打中文、`Ctrl+A` 覆盖；
3. 滚轮 / 拖拽 / 右键（在 Canvas 上数事件）；
4. 用 PrintWindow 给它拍一张 PNG，检查不是空的。

它会移动你的鼠标、会抢前台 —— 所以它不是 `tests/` 里的一员，
   `run_tests.py` 不会跑到它。跑之前请确保没有正在进行的操作。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe scripts/computer_smoke.py [--keep-png 路径]
"""

import ctypes
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.computer import capture, input as inp, uia, win32  # noqa: E402


def _pump(root, n=10):
    for _ in range(n):
        root.update()
        time.sleep(0.03)


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    keep = ""
    if "--keep-png" in argv:
        keep = argv[argv.index("--keep-png") + 1]

    if not win32.IS_WINDOWS:
        print("SKIP  not running on Windows")
        return 0

    ev = {"wheel": 0, "press": 0, "motion": 0, "release": 0, "right": 0, "click": 0}
    fails = []

    root = tk.Tk()
    root.title("Crannia takeover smoke")
    root.geometry("560x340+180+180")
    var = tk.StringVar()
    ent = tk.Entry(root, textvariable=var, width=30)
    ent.place(x=40, y=30, width=320, height=32)
    tk.Button(root, text="SmokeButton", command=lambda: ev.__setitem__("click", ev["click"] + 1)).place(
        x=40, y=80, width=200, height=44)
    canvas = tk.Canvas(root, bg="#dcdcdc")
    canvas.place(x=40, y=150, width=320, height=150)
    canvas.bind("<MouseWheel>", lambda e: ev.__setitem__("wheel", ev["wheel"] + 1))
    canvas.bind("<ButtonPress-1>", lambda e: ev.__setitem__("press", ev["press"] + 1))
    canvas.bind("<B1-Motion>", lambda e: ev.__setitem__("motion", ev["motion"] + 1))
    canvas.bind("<ButtonRelease-1>", lambda e: ev.__setitem__("release", ev["release"] + 1))
    canvas.bind("<ButtonPress-3>", lambda e: ev.__setitem__("right", ev["right"] + 1))
    root.update()
    time.sleep(0.4)

    child = int(root.winfo_id())
    target = ctypes.windll.user32.GetParent(child) or child
    ok, why = win32.activate(target)
    _pump(root)
    if not ok or not win32.is_foreground(target):
        print("FAIL  cannot bring the smoke window to the foreground: %s" % why)
        root.destroy()
        return 1

    ci = win32.window_info(child)
    scale = (ci.width / max(1, root.winfo_width())) if ci else 1.0

    def cxy(lx, ly):
        return ci.rect[0] + int(lx * scale), ci.rect[1] + int(ly * scale)

    # 1. UIA tree
    try:
        snap = uia.snapshot(target, max_elements=80)
        buttons = [e for e in snap.elements if e.control_type == "Button"]
        print("PASS  uia.snapshot: %d elements, %d ms, truncated=%s"
              % (len(snap.elements), snap.took_ms, snap.truncated))
        if not buttons:
            fails.append("UIA tree has no Button")
    except Exception as exc:                         # noqa: BLE001
        fails.append("uia.snapshot: %s: %s" % (type(exc).__name__, exc))
        buttons = []

    # 2. click the button (by element path, not hardcoded coordinates)
    if buttons:
        try:
            bx, by = uia.click_point(target, buttons[0].path)
            inp.click(bx, by)
            _pump(root)
            if ev["click"] >= 1:
                print("PASS  click by element path (fired %d)" % ev["click"])
            else:
                fails.append("button click did not fire")
        except Exception as exc:                     # noqa: BLE001
            fails.append("click: %s: %s" % (type(exc).__name__, exc))

    # 3. type + ctrl+a
    try:
        x, y = cxy(80, 46)
        inp.click(x, y)
        _pump(root)
        inp.type_text("hello 中文 123")
        _pump(root)
        inp.press_key("ctrl+a")
        _pump(root)
        inp.type_text("replaced")
        _pump(root)
        if var.get() == "replaced":
            print("PASS  type_text + ctrl+a")
        else:
            fails.append("typing produced %r" % var.get())
    except Exception as exc:                         # noqa: BLE001
        fails.append("typing: %s: %s" % (type(exc).__name__, exc))

    # 4. wheel / drag / right click on canvas
    try:
        x, y = cxy(200, 220)
        inp.scroll(x, y, vertical=2)
        _pump(root)
        inp.drag(x, y, x + 90, y + 40)
        _pump(root)
        inp.click(x, y, button="right")
        _pump(root)
        if ev["wheel"] and ev["press"] and ev["release"] and ev["right"]:
            print("PASS  scroll / drag / right click (%s)" % ev)
        else:
            fails.append("canvas events incomplete: %s" % ev)
    except Exception as exc:                         # noqa: BLE001
        fails.append("misc input: %s: %s" % (type(exc).__name__, exc))

    # 5. capture
    try:
        shot = capture.capture_window(target)
        print("PASS  capture: %dx%d, %d bytes, notes=%s"
              % (shot.width, shot.height, len(shot.png), shot.notes or "[]"))
        if keep:
            Path(keep).write_bytes(shot.png)
            print("      png -> %s" % keep)
        else:
            tmp = Path(tempfile.gettempdir()) / "crannia_computer_smoke.png"
            tmp.write_bytes(shot.png)
            print("      png -> %s" % tmp)
    except Exception as exc:                         # noqa: BLE001
        fails.append("capture: %s: %s" % (type(exc).__name__, exc))

    root.destroy()
    if fails:
        print("")
        for f in fails:
            print("FAIL  " + f)
        return 1
    print("")
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
