"""Windows 原生地基 —— 全仓唯一碰 `user32` / `kernel32` 的地方。

为什么单独一格：`core/security.py` 管文件，`core/command_policy.py` 管命令，
而接管要的是窗口与输入 —— 那是第三根轴，塞进哪一边都会把两边的判据搅浑。

三条不许回退的纪律：

1. 坐标一律物理像素 —— 进这个模块的每个跨进程调用都套 `dpi_aware()`。
   不声明 DPI 感知时 Windows 返回虚拟化坐标，125% 缩放下点击系统性偏移，
   而且不报错。
2. **句柄一律 `wintypes.HWND` / `c_void_p` 作 `restype`** —— ctypes 默认返回
   `c_int`，在 x64 上会把 64 位 HWND 截断，症状是"窗口时有时无"。
3. 只读枚举不抛异常 —— 单个窗口取不到标题/进程名时给空串并留着它，
   坏一个窗口不该让整张列表消失。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

#: 原生调用不可用（非 Windows）时抛这个 —— 上层把它变成一句人话。
class UnsupportedPlatform(RuntimeError):
    pass


if IS_WINDOWS:                                       # pragma: no cover - 平台分支
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
else:                                                # pragma: no cover
    user32 = kernel32 = gdi32 = dwmapi = advapi32 = None


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise UnsupportedPlatform("接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform)


# 常量（只放用到的；数值照 Win32 SDK）
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SW_RESTORE = 9
SW_SHOW = 5
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100
GA_ROOT = 2


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25

DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

ERROR_ACCESS_DENIED = 5


# 结构体
class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


# 原型（argtypes / restype 一处定死）
if IS_WINDOWS:                                       # pragma: no cover - 平台分支
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.IsHungAppWindow.argtypes = [wintypes.HWND]
    user32.IsHungAppWindow.restype = wintypes.BOOL
    user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_uint]
    user32.RedrawWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD,
                                        ctypes.POINTER(GUITHREADINFO)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.GetDpiForSystem.argtypes = []
    user32.GetDpiForSystem.restype = wintypes.UINT
    # 真实鼠标 / 键盘 + 剪贴板
    # 这一组是 legacy 那条路：微信（Qt）只认它。
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_size_t]
    user32.mouse_event.restype = None
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE,
                                   wintypes.DWORD, ctypes.c_size_t]
    user32.keybd_event.restype = None
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    user32.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
    user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int

    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_void_p


# DPI
class dpi_aware:
    """把当前线程临时设成 per-monitor v2，退出时还原。

    用线程级而不是进程级：进程可能早就建过窗口（托盘 / Flask），
      那时 `SetProcessDpiAwarenessContext` 会失败 —— 而它不会告诉你
      （返回 FALSE，坐标继续是虚拟化的）。线程级随时可设。
    """

    def __enter__(self):
        self._prev = None
        if not IS_WINDOWS:
            return self
        try:
            self._prev = user32.SetThreadDpiAwarenessContext(
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        except Exception:                            # noqa: BLE001 - 老系统没有这个函数
            self._prev = None
        return self

    def __exit__(self, *exc):
        if IS_WINDOWS and self._prev:
            try:
                user32.SetThreadDpiAwarenessContext(self._prev)
            except Exception:                        # noqa: BLE001
                pass
        return False


def dpi_wrapped(fn):
    """把整个函数体包在 `dpi_aware()` 里。

    本机：只在取窗口矩形时套一次是不够的 ——
      UIA 返回的 `BoundingRectangle` 是按调用线程的 DPI 感知级别换算的。
      线程不是 per-monitor v2 时，UIA 给的是逻辑坐标（窗口矩形
      311..1093 物理，而按钮矩形 248..408 逻辑，点到了窗口外面，
      而且 `SendInput` 返回成功）。读树、算坐标、注入三处都要在同一个
      DPI 上下文里。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with dpi_aware():
            return fn(*args, **kwargs)

    return wrapper


_UINT64 = (1 << 64) - 1
_DPI_CONTEXT_UNAWARE = (-1) & _UINT64
_DPI_CONTEXT_UNAWARE_GDI_SCALED = (-5) & _UINT64


def dpi_awareness_of(hwnd) -> int:
    """窗口的 DPI 感知级别：0=不感知 · 1=系统感知 · 2=逐显示器感知。

    不能拿 `GetWindowDpiAwarenessContext` 的返回值直接和 -1/-5 比 ——
      本机它返回的是 24592 这种真实句柄值，而预定义值只用于传入。
      必须再过一道 `GetAwarenessFromDpiAwarenessContext`。
    """
    if not IS_WINDOWS:
        return 1
    try:
        ctx = user32.GetWindowDpiAwarenessContext(int(hwnd))
        if not ctx:
            return 1
        return int(user32.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(int(ctx))))
    except Exception:                                # noqa: BLE001 - 老系统没有这些函数
        return 1


def is_dpi_unaware(hwnd) -> bool:
    """这个窗口是不是DPI 不感知（自绘 96 DPI，由系统整体放大）。

    这决定截图该怎么取尺寸：不感知的窗口 `PrintWindow` 只画出逻辑像素
      （本机：560×340 的逻辑窗口，在 150% 缩放下画进 842×558 的位图，
      内容只占左上 574×370，右下全是黑的）。感知的窗口按物理像素画。
    """
    return dpi_awareness_of(hwnd) == 0


def system_dpi() -> int:
    if not IS_WINDOWS:
        return 96
    try:
        return int(user32.GetDpiForSystem() or 96)
    except Exception:                                # noqa: BLE001
        return 96


def dpi_of_window(hwnd) -> int:
    if not IS_WINDOWS:
        return 96
    try:
        return int(user32.GetDpiForWindow(int(hwnd)) or 96)
    except Exception:                                # noqa: BLE001
        return 96


class dpi_unaware:
    """把当前线程临时设成 DPI UNAWARE —— 只为读"这个窗口以为自己多大"。"""

    def __enter__(self):
        self._prev = None
        if IS_WINDOWS:
            try:
                self._prev = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-1))
            except Exception:                        # noqa: BLE001
                self._prev = None
        return self

    def __exit__(self, *exc):
        if IS_WINDOWS and self._prev:
            try:
                user32.SetThreadDpiAwarenessContext(self._prev)
            except Exception:                        # noqa: BLE001
                pass
        return False


def outer_rect(hwnd) -> tuple[int, int, int, int]:
    """`GetWindowRect`（物理像素，含不可见的边框）。

    截图用这一格，不用 DWM 的 `EXTENDED_FRAME_BOUNDS`：
      `PrintWindow` 画的是窗口自己的外框（从位图 0,0 开始），而 DWM 矩形
      把左右各 ~7px 的不可见边框排除在外，拿 DWM 矩形当画布会让内容
      和坐标差一圈（本机差 22 物理像素）。
    """
    if not IS_WINDOWS:
        return (0, 0, 0, 0)
    with dpi_aware():
        r = RECT()
        if user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
            return (r.left, r.top, r.right, r.bottom)
    return (0, 0, 0, 0)


def logical_window_rect(hwnd) -> tuple[int, int, int, int]:
    """窗口在它自己的坐标系里的矩形（DPI 不感知窗口 = 逻辑像素）。

    必须用 `GetWindowRect`，不能用 DWM 那条：DWM 的返回值**不做 DPI
      虚拟化**，在 unaware 上下文里照样给物理坐标（本机两边一模一样）。
    """
    if not IS_WINDOWS:
        return (0, 0, 0, 0)
    with dpi_unaware():
        r = RECT()
        if user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
            return (r.left, r.top, r.right, r.bottom)
    return (0, 0, 0, 0)


def virtual_screen() -> tuple[int, int, int, int]:
    """返回 (x, y, 宽, 高) —— 覆盖所有显示器的虚拟屏幕（物理像素）。"""
    _require_windows()
    with dpi_aware():
        return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


# 窗口信息
@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    exe: str                 # 小写 basename，例如 "explorer.exe"
    exe_path: str
    title: str
    class_name: str
    rect: tuple[int, int, int, int]   # left, top, right, bottom（物理像素）
    visible: bool
    minimized: bool
    cloaked: bool

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def label(self) -> str:
        return self.title or self.exe or self.class_name or ("窗口 %s" % self.hwnd)


def _read_text(fn_len, fn_text, hwnd) -> str:
    try:
        n = fn_len(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        fn_text(hwnd, buf, n + 1)
        return buf.value or ""
    except Exception:                                # noqa: BLE001
        return ""


def _class_name(hwnd) -> str:
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value or ""
    except Exception:                                # noqa: BLE001
        return ""


def _process_path(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value or ""
        return ""
    finally:
        kernel32.CloseHandle(h)


def _is_cloaked(hwnd) -> bool:
    val = wintypes.DWORD(0)
    try:
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and bool(val.value)
    except Exception:                                # noqa: BLE001
        return False


def _window_rect(hwnd) -> tuple[int, int, int, int]:
    """优先 DWM 的可见边界。

    `GetWindowRect` 会把 DWM 的不可见阴影算进去（最大化窗口左右各多出
    ~7px），按它算出来的"窗口相对坐标"会系统性偏移。DWM 那条拿不到时才退回。
    """
    r = RECT()
    try:
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r))
        if hr == 0:
            return (r.left, r.top, r.right, r.bottom)
    except Exception:                                # noqa: BLE001
        pass
    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return (r.left, r.top, r.right, r.bottom)
    return (0, 0, 0, 0)


def window_info(hwnd) -> WindowInfo | None:
    """一个窗口的快照；窗口已经没了就返回 `None`（不抛）。"""
    _require_windows()
    h = int(hwnd or 0)
    if not h or not user32.IsWindow(h):
        return None
    with dpi_aware():
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        path = _process_path(pid.value) if pid.value else ""
        return WindowInfo(
            hwnd=h,
            pid=int(pid.value),
            exe=os.path.basename(path).lower() if path else "",
            exe_path=path,
            title=_read_text(user32.GetWindowTextLengthW, user32.GetWindowTextW, h),
            class_name=_class_name(h),
            rect=_window_rect(h),
            visible=bool(user32.IsWindowVisible(h)),
            minimized=bool(user32.IsIconic(h)),
            cloaked=_is_cloaked(h),
        )


def enum_windows(*, include_invisible: bool = False,
                 include_cloaked: bool = False,
                 include_tool_windows: bool = False) -> list[WindowInfo]:
    """枚举顶层窗口，按 Z 序（最上面那个在前）。

    默认剔掉不可见 / cloaked（别的虚拟桌面）/ 工具窗口 —— 它们对"接管"
    没有意义，却会把列表撑成几百条。
    """
    _require_windows()
    out: list[WindowInfo] = []
    with dpi_aware():
        def _cb(hwnd, _lparam):
            try:
                info = _window_info_no_dpi(hwnd)
                if info is None:
                    return True
                if not include_invisible and not info.visible:
                    return True
                if not include_cloaked and info.cloaked:
                    return True
                if (not include_tool_windows
                        and (user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                             & WS_EX_TOOLWINDOW)):
                    return True
                if info.width <= 0 or info.height <= 0:
                    return True
                out.append(info)
            except Exception:                        # noqa: BLE001 - 坏一个不毁全表
                pass
            return True

        cb = WNDENUMPROC(_cb)
        user32.EnumWindows(cb, 0)
    return out


def _window_info_no_dpi(hwnd) -> WindowInfo | None:
    """`window_info` 的内核 —— 调用方已经套过 `dpi_aware()`（省掉每窗口一次切换）。"""
    h = int(hwnd or 0)
    if not h or not user32.IsWindow(h):
        return None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    path = _process_path(pid.value) if pid.value else ""
    return WindowInfo(
        hwnd=h,
        pid=int(pid.value),
        exe=os.path.basename(path).lower() if path else "",
        exe_path=path,
        title=_read_text(user32.GetWindowTextLengthW, user32.GetWindowTextW, h),
        class_name=_class_name(h),
        rect=_window_rect(h),
        visible=bool(user32.IsWindowVisible(h)),
        minimized=bool(user32.IsIconic(h)),
        cloaked=_is_cloaked(h),
    )


# 前台 / 激活
def foreground_hwnd() -> int:
    _require_windows()
    return int(user32.GetForegroundWindow() or 0)


def is_foreground(hwnd) -> bool:
    return foreground_hwnd() == int(hwnd or 0)


def pick_main_window(windows, pids):
    """从枚举结果里挑出 Crannia 主窗。

    场景：Edge 全屏播视频时一轮结束，Electron 的 `focus()` 只让窗口
    在任务栏亮黄闪烁（Windows 前台锁），窗口没出来。后端用
    `activate()` 的 `AttachThreadInput` 那条路再拉一次 —— 它需要先知道
    主窗是哪一个。

    判据：
      · 优先 我们自己的 pid（`our_pids()`；生产里后端就是 Electron 拉起的）；
      · 没有（开发态：后端从终端启动、壳是另一个进程）就退到
        `electron.exe` + 标题含 `Crannia` —— 绝不对所有 Electron 应用生效；
      · 多个候选取面积最大的（接管横幅是小窗、且 `skipTaskbar`）。
    """
    def area(w):
        return max(0, w.rect[2] - w.rect[0]) * max(0, w.rect[3] - w.rect[1])

    mine = [w for w in windows if w.title and w.pid in pids]
    if not mine:
        mine = [w for w in windows if w.title
                and (w.exe or "").lower() == "electron.exe"
                and "crannia" in (w.title or "").lower()]
    return max(mine, key=area) if mine else None


def window_health(hwnd) -> dict:
    """窗口现在能不能收输入。

    场景：微信从托盘被 `ShowWindow` 叫出来后，标题栏像激活、× 也是亮的，
    但点不动（人点也不动）；`enabled/hung` 全是正常 —— 标准标志看不出
    这种死法。补两个更硬的观测：
      · `active/focus`：GUI 线程的 Active/Focus 是不是这个窗口（或其子窗口）；
      · `point_hit`：窗口中心 `WindowFromPoint` 命中的根窗口是不是它
        （微信的中心/左侧点击会穿透到后面的终端，只有左上一块命中自己）。
    """
    _require_windows()
    h = int(hwnd or 0)
    if not h or not user32.IsWindow(h):
        return {"exists": False, "visible": False, "enabled": False, "hung": False,
                "active": False, "focus": False, "point_hit": False}
    visible = bool(user32.IsWindowVisible(h))
    minimized = bool(user32.IsIconic(h))
    active = focus = point_hit = False
    with dpi_aware():
        try:
            def _root(x):
                return user32.GetAncestor(x, GA_ROOT) if x else 0
            gi = GUITHREADINFO()
            gi.cbSize = ctypes.sizeof(GUITHREADINFO)
            tid = user32.GetWindowThreadProcessId(h, None)
            if user32.GetGUIThreadInfo(tid, ctypes.byref(gi)):
                active = bool(gi.hwndActive and _root(gi.hwndActive) == h)
                focus = bool(gi.hwndFocus and _root(gi.hwndFocus) == h)
            if visible and not minimized:
                l, t, r, b = outer_rect(h)
                if r > l and b > t:
                    hp = user32.WindowFromPoint(
                        wintypes.POINT((l + r) // 2, (t + b) // 2))
                    point_hit = bool(hp and _root(hp) == h)
        except Exception:                            # noqa: BLE001 - 诊断，不反噬
            pass
    return {
        "exists": True,
        "visible": visible,
        "enabled": bool(user32.IsWindowEnabled(h)),
        "hung": bool(user32.IsHungAppWindow(h)),
        "active": active,
        "focus": focus,
        "point_hit": point_hit,
    }


def activate(hwnd) -> tuple[bool, str]:
    """尽力把窗口带到前台。返回 `(成功, 人话原因)`。

    Windows 拒绝后台进程抢前台（`SetForegroundWindow` 返回 FALSE 而不报错）。
    这里用 `AttachThreadInput` 把当前线程挂到前台线程上再试一次 —— 这是
    没有签名 / 没有 `uiAccess` 的进程能做到的上限。失败就如实说，
    不做"假装成功"（检测可以错，打不着不能装）。
    """
    _require_windows()
    h = int(hwnd or 0)
    if not h or not user32.IsWindow(h):
        return False, "窗口已经不在了"
    if is_foreground(h):
        return True, ""
    with dpi_aware():
        # 微信最小化到托盘时窗口是 `SW_HIDE` 的
        #   （`IsIconic` 为假），只 restore 看不见它。
        #   用 SW_RESTORE（不是 SW_SHOW）：SW_SHOW 只把它画出来，
        #   微信的内部"主窗已显示"状态没切过来，标题栏像激活、点不动，
        #   得从托盘再点一次才恢复。SW_RESTORE 走的是系统 restore 路径。
        if not user32.IsWindowVisible(h) or user32.IsIconic(h):
            user32.ShowWindow(h, SW_RESTORE)
        # 从托盘恢复时应用自己会"黑闪一下"重绘；我们这里
        #   主动强制重绘一次（含子窗口），尽量复现它那条路径。
        try:
            user32.RedrawWindow(
                h, None, None,
                RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW)
            user32.UpdateWindow(h)
        except Exception:                            # noqa: BLE001
            pass
        if user32.SetForegroundWindow(h) and is_foreground(h):
            return True, ""
        # 退一步：把当前线程挂到前台线程上（失败也不抛）
        try:
            target_thread = user32.GetWindowThreadProcessId(h, None)
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            me = kernel32.GetCurrentThreadId()
            attached = False
            if fg_thread and fg_thread != me:
                attached = bool(user32.AttachThreadInput(fg_thread, me, True))
            try:
                user32.BringWindowToTop(h)
                user32.SetForegroundWindow(h)
            finally:
                if attached:
                    user32.AttachThreadInput(fg_thread, me, False)
        except Exception:                            # noqa: BLE001
            pass
        if is_foreground(h):
            return True, ""
        info = window_info(h)
    label = info.label if info else ("窗口 %s" % h)
    return False, "Windows 拒绝把「%s」带到前台（缺少前台权限）" % label


# 启动程序
def find_taskbar() -> int:
    """任务栏（`Shell_TrayWnd`）的 hwnd；找不到返回 0。"""
    _require_windows()
    for w in enum_windows(include_invisible=True, include_tool_windows=True):
        if w.class_name == "Shell_TrayWnd" and (w.exe or "").lower() == "explorer.exe":
            return w.hwnd
    return 0


def _uia_elements(hwnd: int, max_elements: int = 200):
    from . import uia
    return uia.snapshot(hwnd, max_elements=max_elements, max_depth=20,
                        include_offscreen=True).elements


def restore_via_tray(title: str, exe: str = "") -> bool:
    """让应用自己从托盘恢复（UIA 触发托盘图标的默认动作）。

    微信从托盘被 `ShowWindow`/任务栏按钮叫出来后
    "亮着但点不动"（中心/左侧点击穿透到后面的终端）；只有从溢出托盘点它的
    图标（`QSystemTrayIcon` 的默认动作）才恢复成可点击。
这是最后一条、也是唯一被实际救回来的路径。
    """
    _require_windows()
    taskbar = find_taskbar()
    if not taskbar:
        return False
    name = str(title or "").strip()
    if not name:
        return False
    try:
        from . import uia
        elems = _uia_elements(taskbar)

        def _match(e, *, running_ok: bool):
            if e.control_type != "Button" or name not in (e.name or ""):
                return False
            if not running_ok and "运行窗口" in (e.name or ""):
                return False
            return True

        # ① 任务栏自身的托盘图标（非“运行窗口”那个按钮）
        for e in elems:
            if _match(e, running_ok=False):
                try:
                    uia.invoke(taskbar, e.path)
                    return True
                except Exception:                    # noqa: BLE001
                    continue
        # ② 打开溢出托盘，找图标
        ov = next((e for e in elems if e.control_type == "Button" and
                   ("显示隐藏" in (e.name or "") or "Show hidden" in (e.name or ""))),
                  None)
        if ov is not None:
            try:
                uia.invoke(taskbar, ov.path)
            except Exception:                        # noqa: BLE001
                pass
            time.sleep(0.8)
        for w in enum_windows(include_invisible=True, include_tool_windows=True):
            if w.class_name != "TopLevelWindowForOverflowXamlIsland":
                continue
            for e in _uia_elements(w.hwnd):
                if _match(e, running_ok=True):
                    try:
                        uia.invoke(w.hwnd, e.path)
                        return True
                    except Exception:                # noqa: BLE001
                        continue
        # ③ 任务栏“运行窗口”按钮（最弱的一档）
        for e in elems:
            if _match(e, running_ok=True):
                try:
                    uia.invoke(taskbar, e.path)
                    return True
                except Exception:                    # noqa: BLE001
                    continue
    except Exception:                                # noqa: BLE001
        return False
    return False


def shell_execute(target: str) -> tuple[bool, str]:
    """用 `ShellExecuteW(..., "open", ...)` 启动一个程序（`launch_app`）。

    只传一个目标，不接受命令行参数 —— 参数是执行代码的另一条通道
      （`run_command` 的审批），别从这里绕过去。
    返回值 >32 才算成功（这是 `ShellExecute` 的老约定；错误码是 0/2/3/5/31 那种小整数）。
    """
    _require_windows()
    try:
        r = shell32.ShellExecuteW(None, "open", str(target), None, None, 1)
        code = int(r or 0) if not isinstance(r, int) else int(r)
        if code > 32:
            return True, ""
        return False, "ShellExecute 返回 %d" % code
    except Exception as exc:                         # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


# 锁屏 / 完整性级别
def input_desktop_name() -> tuple[str, int]:
    """返回 `(输入桌面名, 错误码)` —— 名字空串 = 拿不到（多半是锁屏了）。"""
    _require_windows()
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not hdesk:
        return "", ctypes.get_last_error()
    try:
        buf = ctypes.create_unicode_buffer(256)
        need = wintypes.DWORD(0)
        ok = user32.GetUserObjectInformationW(
            hdesk, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(need))
        return (buf.value if ok else ""), 0
    finally:
        user32.CloseDesktop(hdesk)


def is_locked() -> bool:
    """锁屏检测。

    判据两层：`OpenInputDesktop` 失败，锁着；名字是 `Winlogon` /
      `ScreenSaver`，锁着。拿不到就按"锁着"处理（fail-closed：
      这一支朝"停"那边倒，代价只是少做一次动作）。
    """
    if not IS_WINDOWS:
        return False
    name, _err = input_desktop_name()
    if not name:
        return True
    return name.lower() in ("winlogon", "screensaver")


def integrity_rid(pid: int) -> int | None:
    """进程完整性级别的 RID：0x1000=中 · 0x2000=高（提权）· 0x3000=系统。

    拿不到（权限不够 / 进程已退出）返回 `None` —— 不要猜成中。
    """
    _require_windows()
    if pid <= 0:
        return None
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
            return None
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0,
                                     ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TokenIntegrityLevel, buf,
                                            size.value, ctypes.byref(size)):
            return None
        # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES { PSID; DWORD } }
        sid = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents.Label.Sid
        if not sid:
            return None
        raw = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))
        sub_count = raw[1]
        if sub_count <= 0:
            return None
        # SID = rev(1) + count(1) + authority(6) + subauthority[](4 each)
        start = ctypes.addressof(raw.contents) + 8 + 4 * (sub_count - 1)
        return int(ctypes.c_uint32.from_address(start).value)
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(h)


def current_integrity_rid() -> int | None:
    return integrity_rid(int(kernel32.GetCurrentProcessId()))


# 自己的窗口
#: 这些祖先不是我们的壳 —— 走到它们就停（否则"在终端里打字"会被当成点我们自己的界面）
_NOT_OUR_SHELL = frozenset({
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
    "bash.exe", "mintty.exe", "explorer.exe", "pycharm64.exe", "idea64.exe",
    "code.exe", "python.exe", "pythonw.exe", "node.exe",
})


def our_pids() -> set[int]:
    """哪些 pid 算"我们自己" —— Crannia 后端 + 它的桌面壳。

    落在我们自己的窗口（审批面板 / 停止按钮 / 聊天框）上的
      物理输入不算干预。

    不能把所有祖先都算进来 —— 后端常常是用户从终端 / IDE 里启动的，
      那些进程的窗口一旦被豁免，"在终端里打字"就不再触发退出接管，
      而那正是最该触发的一种干预。这里只沿父链找壳（`electron.exe` /
      `crannia.exe`），一遇到终端 / IDE / explorer 就停。
    """
    _require_windows()
    me = int(kernel32.GetCurrentProcessId())
    pids = {me}
    try:
        import psutil
    except Exception:                                # noqa: BLE001 - 没有 psutil 就只认自己
        return pids
    try:
        proc = psutil.Process(me)
    except Exception:                                # noqa: BLE001
        return pids

    shell_path = ""
    try:
        for parent in proc.parents():
            try:
                name = (parent.name() or "").lower()
            except Exception:                        # noqa: BLE001
                break
            if name in ("electron.exe", "crannia.exe"):
                shell_path = (parent.exe() or "")
                break
            if name in _NOT_OUR_SHELL:
                break
    except Exception:                                # noqa: BLE001
        pass

    wanted = {shell_path.lower()} if shell_path else set()
    if not wanted:
        # 打包版直接叫 crannia.exe —— 没有壳祖先时兜这一层；
        # 绝不按 `electron.exe` 兜底（那是所有 Electron 应用的通用名）
        wanted = {"crannia.exe"}
    try:
        for p in psutil.process_iter(["pid", "exe"]):
            try:
                exe = (p.info.get("exe") or "").lower()
                if not exe:
                    continue
                if exe in wanted or os.path.basename(exe) in wanted:
                    pids.add(int(p.info["pid"]))
            except Exception:                        # noqa: BLE001
                continue
    except Exception:                                # noqa: BLE001
        pass
    return pids
