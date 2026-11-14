"""按窗口截图。

主路 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` —— 它让目标窗口自己
把内容画进我们的 DC，所以被别的窗口挡住也能拍到。次路 `BitBlt` 从屏幕
位图抓可见区域（只在目标已经是前台时有意义）。

拍不到就说拍不到：
- 最小化 / cloaked（另一个虚拟桌面），直接报错，不做"拍一张黑图"；
- `PrintWindow` 失败或整张全黑，退 `BitBlt`；仍然全黑，返回图片 + 一句
  "看起来是黑的（可能是独占全屏 / 硬件合成 / DRM）" —— 图照样给，
  但不许让模型以为它看见了（检测可以错，打码不能错）。

不做 WGC（`Windows.Graphics.Capture` 要 `winsdk` 或原生 helper）。
   独占全屏游戏这类场景，`PrintWindow` 本来也拍不到 —— 如实标。
"""

from __future__ import annotations

import ctypes
import io
import sys
from ctypes import wintypes
from dataclasses import dataclass, field

from . import win32

if win32.IS_WINDOWS:                                 # pragma: no cover - 平台分支
    _u = win32.user32
    _g = win32.gdi32

    _u.GetWindowDC.argtypes = [wintypes.HWND]
    _u.GetWindowDC.restype = wintypes.HDC
    _u.GetDC.argtypes = [wintypes.HWND]
    _u.GetDC.restype = wintypes.HDC
    _u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _u.ReleaseDC.restype = ctypes.c_int
    _u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _u.PrintWindow.restype = wintypes.BOOL
    _g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _g.CreateCompatibleDC.restype = wintypes.HDC
    _g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _g.SelectObject.restype = wintypes.HGDIOBJ
    _g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _g.DeleteObject.restype = wintypes.BOOL
    _g.DeleteDC.argtypes = [wintypes.HDC]
    _g.DeleteDC.restype = wintypes.BOOL
    _g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                             wintypes.UINT, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.UINT]
    _g.GetDIBits.restype = ctypes.c_int
    _g.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.HDC,
                          ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    _g.BitBlt.restype = wintypes.BOOL

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020

#: 像素判黑阈值：抽样（每 17 个像素看一个）全黑才算"看起来是黑的"。
_BLACK_SAMPLE = 17


class CaptureError(RuntimeError):
    def __init__(self, brief: str, detail: str = ""):
        super().__init__(brief)
        self.brief = brief
        self.detail = detail


@dataclass
class CaptureResult:
    png: bytes
    width: int
    height: int
    rect: tuple[int, int, int, int]
    notes: list[str] = field(default_factory=list)

    @property
    def looks_black(self) -> bool:
        return bool(self.notes)


def _bitmap_info(w: int, h: int):
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                    ("bmiColors", wintypes.DWORD * 3)]

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h                       # 负数 = 自上而下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0                   # BI_RGB
    return bi


def _grab(hwnd: int, rect, render: tuple[int, int], via_screen: bool,
          notes: list[str]):
    """`render` = 位图尺寸（DPI 不感知窗口要按逻辑尺寸开，否则右下是黑的）。"""
    from PIL import Image
    l, t, r, b = rect
    w, h = max(0, r - l), max(0, b - t)
    rw, rh = render
    if w <= 0 or h <= 0 or rw <= 0 or rh <= 0:
        raise CaptureError("窗口没有可见区域（宽或高为 0）")
    src = _u.GetDC(0) if via_screen else _u.GetWindowDC(hwnd)
    if not src:
        raise CaptureError("拿不到窗口的设备上下文（窗口可能已经关了）")
    mem = _g.CreateCompatibleDC(src)
    bmp = _g.CreateCompatibleBitmap(src, rw, rh)
    old = _g.SelectObject(mem, bmp)
    try:
        if via_screen:
            # 屏幕位图是物理像素，这一支必须按物理尺寸来（调用方保证 render==(w,h)）
            ok = _g.BitBlt(mem, 0, 0, min(w, rw), min(h, rh), src, l, t, SRCCOPY)
            notes.append("抓的是屏幕可见区域（目标窗口在前台）")
        else:
            ok = (_u.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
                  or _u.PrintWindow(hwnd, mem, 0))
        if not ok:
            raise CaptureError("这个窗口拒绝被截图（PrintWindow 返回 0）")
        bi = _bitmap_info(rw, rh)
        buf = ctypes.create_string_buffer(rw * rh * 4)
        got = _g.GetDIBits(mem, bmp, 0, rh, buf, ctypes.byref(bi), 0)
        if got != rh:
            raise CaptureError("截图取回失败（GetDIBits 只拿到 %d/%d 行）" % (got, rh))
        return Image.frombuffer("RGBA", (rw, rh), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        if old:
            _g.SelectObject(mem, old)
        _g.DeleteObject(bmp)
        _g.DeleteDC(mem)
        _u.ReleaseDC(0 if via_screen else hwnd, src)


def _looks_all_black(img) -> bool:
    px = img.load()
    w, h = img.size
    for y in range(0, h, _BLACK_SAMPLE):
        for x in range(0, w, _BLACK_SAMPLE):
            if px[x, y] != (0, 0, 0):
                return False
    return True


def capture_window(hwnd: int, *, allow_screen_fallback: bool = True) -> CaptureResult:
    """按窗口截图，返回 PNG 字节 + 尺寸 + 说明。

    返回的图一律是物理像素、和 `info.rect` 一比一 —— DPI 不感知的窗口
      先按逻辑尺寸渲染，再放大到物理尺寸。不做这一步，模型看到的是
      "左边一张小图、右边一大片黑"，而它按图里的像素算出来的坐标会整体偏小
      （本机 150% 缩放下实际偏 1.46 倍）。
    """
    if not win32.IS_WINDOWS:
        raise CaptureError("接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform)
    info = win32.window_info(hwnd)
    if info is None:
        raise CaptureError("窗口已经不在了")
    if info.minimized:
        raise CaptureError("窗口已最小化 —— 先 `activate_window` 再观察")
    if info.cloaked:
        raise CaptureError("窗口在另一个虚拟桌面上（cloaked），拍不到")
    notes: list[str] = []
    # 画布用 GetWindowRect（含不可见边框），不用 DWM 矩形 —— 见 `win32.outer_rect`
    rect = win32.outer_rect(info.hwnd)
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        rect = info.rect
    pw, ph = rect[2] - rect[0], rect[3] - rect[1]
    render = (pw, ph)
    if win32.is_dpi_unaware(info.hwnd):
        lr = win32.logical_window_rect(info.hwnd)
        lw, lh = lr[2] - lr[0], lr[3] - lr[1]
        if lw > 0 and lh > 0:
            render = (lw, lh)
    try:
        img = _grab(info.hwnd, rect, render, False, notes)
    except CaptureError:
        if not allow_screen_fallback or not win32.is_foreground(info.hwnd):
            raise
        img = _grab(info.hwnd, rect, (pw, ph), True, notes)
    if img.size != (pw, ph):
        from PIL import Image
        img = img.resize((pw, ph), Image.LANCZOS)
    if _looks_all_black(img):
        notes.append("这张图**整张是黑的** —— 可能是独占全屏 / 硬件合成 / DRM，"
                     "或者窗口内容还没画出来；别把它当成'看见了'")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=False)
    return CaptureResult(png=out.getvalue(), width=pw, height=ph, rect=rect, notes=notes)
