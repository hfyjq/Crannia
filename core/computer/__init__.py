"""「接管」的桌面底座。

分工：

| 模块 | 管什么 |
|---|---|
| `win32.py` | 全仓唯一碰 `user32` / `kernel32` 的地方：DPI、窗口枚举、前台、锁屏、完整性级别 |
| `uia.py` | UI Automation（纯 ctypes COM，不依赖 comtypes / pywin32） |
| `capture.py` | 按窗口截图（`PrintWindow`，失败退 `BitBlt`） |
| `input.py` | 输入注入：`SendInput`（默认）+ 真实鼠标 `SetCursorPos`/`mouse_event`（`pointer="legacy"`，微信那类 Qt 窗口）+ 剪贴板粘贴（`input_method="paste"`，中文/输入法）· 组合键 / 滚轮 / 拖拽 |
| `guard.py` | 硬拒绝：应用黑名单、Windows 键、锁屏、前台校验 |

三条判据（写在这里，因为每个模块都要遵守）：

1. 坐标一律物理像素 —— 每次跨进程调用前把当前线程设成
   `PER_MONITOR_AWARE_V2`（`win32.dpi_aware()`）。不声明，125%/150% 下
   Windows 给的是虚拟化坐标，点击系统性偏移，而且不报错。
2. 失败要出声 —— UIPI 挡住的 `SendInput`、最小化窗口、cloaked 窗口、
   独占全屏的黒图，一律带一句人话回给模型/用户，不静默。
3. 这是最小闭环 —— 浏览器接管、WGC、全屏捕获都不在这里。
"""
