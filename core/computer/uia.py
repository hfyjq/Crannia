"""UI Automation —— 纯 ctypes COM，不依赖 comtypes / pywin32。

## 为什么不用现成包

本机 `pywin32` 有装、`comtypes` / `uiautomation` 没装。但 `pywin32` 不在
`pyproject.toml` 的依赖表里 —— 用一个未声明的包会让"换台机器就装不上"。
直接对 `IUIAutomation` 的 vtable 下调用；本机已
`CoCreateInstance` hr=0 → `ElementFromHandle` hr=0 → `get_CurrentName` 读到 33 字符。

## 三条判据

1. 元素不跨调用持有 —— UIA 元素指针是 COM 对象，跨线程 / 跨轮使用要
   marshaling。这里只把树路径（`(2, 0, 5)` 这种子索引串）存进观察快照，
   动作时重新走一遍去解析。窗口变了就解析失败，报"元素不在了，重新观察"，
   这正是 `一次观察 → 一个动作` 想要的失败方向。
2. 读属性不抢焦点 —— `IUIAutomation2::put_AutoSetFocus(FALSE)`（拿不到
   `IUIAutomation2` 就跳过）。UIA 的默认行为会在某些操作里主动 `SetFocus`，
   那会把用户的焦点抢走。
3. 看不见的子树不硬闯 —— 离屏元素默认不返回（`max_elements` / `max_depth`
   双封顶）。canvas / 游戏 / 默认关无障碍的 Electron 本来就会得到空树 ——
   那时截图兜底（`capture.py`），不在这里编一棵假树。

UIA 调用是跨进程的，目标卡死时它会等。`IUIAutomation2` 的连接 / 事务超时
设成 3s / 5s（拿不到那一版就靠 Windows 默认）。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass, field

from . import win32

IS_WINDOWS = sys.platform == "win32"

_OLEAUT32 = ctypes.WinDLL("oleaut32") if IS_WINDOWS else None
if _OLEAUT32 is not None:                            # pragma: no cover - 平台分支
    _OLEAUT32.SysFreeString.argtypes = [ctypes.c_void_p]
    _OLEAUT32.SysFreeString.restype = None
    _OLEAUT32.SysAllocString.argtypes = [ctypes.c_wchar_p]
    _OLEAUT32.SysAllocString.restype = ctypes.c_void_p


class UIAError(RuntimeError):
    """读不到 / 元素不在了 / 这个控件不支持该动作。"""


# GUID / COM 基础
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    from uuid import UUID
    u = UUID(text)
    g = GUID()
    g.Data1 = u.time_low
    g.Data2 = u.time_mid
    g.Data3 = u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


class _V:
    """vtable 索引进来的函数原型（调用方保证索引对；索引写错的症状是崩溃，
    所以每个索引用到时都写一句它是什么。"""
    HRESULT = ctypes.c_long
    ULONG = ctypes.c_ulong


def _fn(ptr, index: int, argtypes: list, restype=_V.HRESULT):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, *argtypes)
    return proto(vtable[index])


def _release(ptr) -> None:
    if not ptr:
        return
    # IUnknown::Release = vtable[2]
    _fn(ptr, 2, [ctypes.c_void_p], _V.ULONG)(ptr)


def _query(ptr, iid_text: str):
    out = ctypes.c_void_p()
    # IUnknown::QueryInterface = vtable[0]
    hr = _fn(ptr, 0, [ctypes.c_void_p, ctypes.POINTER(GUID),
                      ctypes.POINTER(ctypes.c_void_p)])(ptr, ctypes.byref(_guid(iid_text)),
                                                        ctypes.byref(out))
    return out if hr >= 0 and out.value else None


# CLSID / IID（Windows SDK）
CLSID_CUIAutomation = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
IID_IUIAutomation = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"
IID_IUIAutomation2 = "{34723AFF-0C9D-49D0-9896-7AB52DF8CD8A}"

#: auto-set-focus / 连接超时 / 事务超时（毫秒）
_AUTO_SET_FOCUS = 0
_CONN_TIMEOUT_MS = 3000
_TXN_TIMEOUT_MS = 5000

#: 模式 ID（UIAutomationClient.h）
INVOKE_PATTERN = 10000
VALUE_PATTERN = 10002
TOGGLE_PATTERN = 10015
SELECTION_ITEM_PATTERN = 10008
SCROLL_ITEM_PATTERN = 10017
EXPAND_COLLAPSE_PATTERN = 10005

#: 控件类型 ID → 名字
CONTROL_TYPES: dict[int, str] = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}

#: 这些类型在树里即使没名字也值得给模型看（它们能点 / 能输）
_ACTIONABLE = frozenset({"Button", "TabItem", "MenuItem", "ListItem", "CheckBox",
                         "RadioButton", "Edit", "Document", "SplitButton",
                         "Hyperlink", "ComboBox", "TreeItem", "DataItem"})


# 每线程一个 IUIAutomation
_tls = threading.local()


def _automation():
    if not IS_WINDOWS:
        raise UIAError("接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform)
    auto = getattr(_tls, "auto", None)
    if auto:
        return auto
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = _V.HRESULT
    hr = ole32.CoInitializeEx(None, 0)               # COINIT_MULTITHREADED
    # RPC_E_CHANGED_MODE = -2147417850：这个线程已经是 STA 了 —— 照用，别硬切
    if hr < 0 and hr != -2147417850:
        raise UIAError("COM 初始化失败（hr=%s）" % hr)

    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                       ctypes.c_ulong, ctypes.POINTER(GUID),
                                       ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = _V.HRESULT
    ptr = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_CUIAutomation)), None,
                                1, ctypes.byref(_guid(IID_IUIAutomation)),
                                ctypes.byref(ptr))
    if hr < 0 or not ptr.value:
        raise UIAError("UI Automation 起不来（CoCreateInstance hr=%s）" % hr)

    # IUIAutomation2：关掉 auto-set-focus + 设超时（拿不到就算了，不影响主路）
    auto2 = _query(ptr, IID_IUIAutomation2)
    if auto2:
        try:
            # IUIAutomation2::put_AutoSetFocus = vtable[58]
            _fn(auto2, 58, [ctypes.c_void_p, wintypes.BOOL])(auto2, _AUTO_SET_FOCUS)
            # put_ConnectionTimeout = vtable[60]；put_TransactionTimeout = vtable[62]
            _fn(auto2, 60, [ctypes.c_void_p, wintypes.DWORD])(auto2, _CONN_TIMEOUT_MS)
            _fn(auto2, 62, [ctypes.c_void_p, wintypes.DWORD])(auto2, _TXN_TIMEOUT_MS)
        except Exception:                            # noqa: BLE001 - 版本差异，不致命
            pass
        finally:
            _release(auto2)

    _tls.auto = ptr
    return ptr


# 属性读取（索引 = IUIAutomationElement 的 vtable 位）
def _bstr(elem, index: int) -> str:
    out = ctypes.c_void_p()
    hr = _fn(elem, index, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])(
        elem, ctypes.byref(out))
    if hr < 0 or not out.value:
        return ""
    try:
        return ctypes.wstring_at(out.value)
    finally:
        _OLEAUT32.SysFreeString(out)


def _int_prop(elem, index: int) -> int:
    out = ctypes.c_int(0)
    hr = _fn(elem, index, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)])(
        elem, ctypes.byref(out))
    return int(out.value) if hr >= 0 else 0


def _ptr_prop(elem, index: int) -> int:
    """指针宽度的属性（`get_CurrentNativeWindowHandle` = vtable[36]）。

    不能用 `_int_prop`：那是 32 位 —— 在 x64 上会把 HWND 截断。
    """
    out = ctypes.c_ssize_t(0)
    hr = _fn(elem, index, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)])(
        elem, ctypes.byref(out))
    return int(out.value) if hr >= 0 else 0


def _rect_prop(elem, index: int = 43) -> tuple[int, int, int, int]:
    r = win32.RECT()
    hr = _fn(elem, index, [ctypes.c_void_p, ctypes.POINTER(win32.RECT)])(
        elem, ctypes.byref(r))
    if hr < 0:
        return (0, 0, 0, 0)
    return (r.left, r.top, r.right, r.bottom)


def _pattern(elem, pattern_id: int):
    """拿一个模式指针；不支持时返回 `None`（这不是错误）。"""
    out = ctypes.c_void_p()
    # IUIAutomationElement::GetCurrentPattern = vtable[16]
    hr = _fn(elem, 16, [ctypes.c_void_p, ctypes.c_int,
                        ctypes.POINTER(ctypes.c_void_p)])(elem, pattern_id,
                                                          ctypes.byref(out))
    return out if hr >= 0 and out.value else None


# 树
@dataclass
class UIElement:
    index: int
    path: tuple[int, ...]
    name: str
    control_type: str
    automation_id: str
    class_name: str
    rect: tuple[int, int, int, int]
    enabled: bool
    offscreen: bool
    focusable: bool
    value: str = ""

    @property
    def depth(self) -> int:
        return len(self.path)

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    def brief(self) -> str:
        bits = ["[%d] %s" % (self.index, self.control_type or "?")]
        if self.name:
            bits.append('"%s"' % self.name.replace("\n", " ")[:120])
        if self.automation_id:
            bits.append("id=%s" % self.automation_id)
        if not self.enabled:
            bits.append("(disabled)")
        if self.value:
            bits.append("value=%r" % self.value[:80])
        return " ".join(bits)


@dataclass
class Snapshot:
    hwnd: int
    elements: list[UIElement] = field(default_factory=list)
    truncated: bool = False
    focused_index: int | None = None
    took_ms: int = 0

    def render(self, *, max_chars: int = 12000) -> str:
        lines = []
        for el in self.elements:
            lines.append("  " * el.depth + el.brief())
        text = "\n".join(lines)
        if len(text) > max_chars:
            return text[:max_chars] + "\n…（树被截断，共 %d 个元素）" % len(self.elements)
        return text


def _element_from_handle(auto, hwnd: int):
    out = ctypes.c_void_p()
    # IUIAutomation::ElementFromHandle = vtable[6]
    hr = _fn(auto, 6, [ctypes.c_void_p, wintypes.HWND,
                       ctypes.POINTER(ctypes.c_void_p)])(auto, hwnd, ctypes.byref(out))
    if hr < 0 or not out.value:
        raise UIAError("取不到这个窗口的无障碍树（窗口可能已经关了，或者是提权窗口）")
    return out


def _walker(auto):
    out = ctypes.c_void_p()
    # IUIAutomation::get_ControlViewWalker = vtable[14]
    hr = _fn(auto, 14, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])(auto,
                                                                           ctypes.byref(out))
    if hr < 0 or not out.value:
        raise UIAError("取不到 UIA 的 ControlView 遍历器")
    return out


def _first_child(walker, elem):
    out = ctypes.c_void_p()
    # IUIAutomationTreeWalker::GetFirstChildElement = vtable[4]
    # 不是 3 —— 3 是 GetParentElement（写错的话会一路往上爬，
    #    本机的症状是"桌面 1 → Program Manager → 桌面 1"无限套娃）
    hr = _fn(walker, 4, [ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p)])(walker, elem, ctypes.byref(out))
    return out if hr >= 0 and out.value else None


def _next_sibling(walker, elem):
    out = ctypes.c_void_p()
    # IUIAutomationTreeWalker::GetNextSiblingElement = vtable[6]
    hr = _fn(walker, 6, [ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p)])(walker, elem, ctypes.byref(out))
    return out if hr >= 0 and out.value else None


@win32.dpi_wrapped
def snapshot(hwnd: int, *, max_elements: int = 250, max_depth: int = 12,
             include_offscreen: bool = False) -> Snapshot:
    """把窗口的 ControlView 树 breadth-first... 实际是 depth-first 读成一张表。

    `index` 与 `path` 是一一对应的：`path` 是"从窗口根往下第几个孩子"，
      动作时用它重新解析（见模块 docstring 判据 1）。
    """
    import time
    t0 = time.monotonic()
    auto = _automation()
    root = _element_from_handle(auto, int(hwnd))
    walker = _walker(auto)
    out = Snapshot(hwnd=int(hwnd))
    try:
        def visit(elem, path, depth):
            if len(out.elements) >= max_elements:
                out.truncated = True
                return
            ctype_id = _int_prop(elem, 21)           # get_CurrentControlType
            ctype = CONTROL_TYPES.get(ctype_id, str(ctype_id) if ctype_id else "")
            name = _bstr(elem, 23)                   # get_CurrentName
            offscreen = bool(_int_prop(elem, 38))    # get_CurrentIsOffscreen
            rect = _rect_prop(elem, 43)              # get_CurrentBoundingRectangle
            enabled = bool(_int_prop(elem, 28))      # get_CurrentIsEnabled
            if offscreen and not include_offscreen:
                pass                                 # 不返回，但继续往下（容器常被标 offscreen）
            elif rect[2] > rect[0] and rect[3] > rect[1]:
                a11y_id = _bstr(elem, 29)            # get_CurrentAutomationId
                cls = _bstr(elem, 30)                # get_CurrentClassName
                focusable = bool(_int_prop(elem, 27))  # get_CurrentIsKeyboardFocusable
                value = ""
                if ctype in ("Edit", "Document"):
                    pat = _pattern(elem, VALUE_PATTERN)
                    if pat:
                        try:
                            value = _bstr(pat, 4)    # IUIAutomationValuePattern::get_CurrentValue
                        finally:
                            _release(pat)
                out.elements.append(UIElement(
                    index=len(out.elements), path=path, name=name,
                    control_type=ctype, automation_id=a11y_id, class_name=cls,
                    rect=rect, enabled=enabled, offscreen=offscreen,
                    focusable=focusable, value=value))
            if depth >= max_depth:
                out.truncated = True
                return
            child = _first_child(walker, elem)
            idx = 0
            while child:
                visit(child, path + (idx,), depth + 1)
                nxt = _next_sibling(walker, child)
                _release(child)
                child = nxt
                idx += 1
                if len(out.elements) >= max_elements:
                    out.truncated = True
                    break

        visit(root, (), 0)
    finally:
        _release(walker)
        _release(root)
    out.took_ms = int((time.monotonic() - t0) * 1000)
    return out


@win32.dpi_wrapped
def resolve(hwnd: int, path: tuple[int, ...]):
    """按路径重新解析一个元素。调用方负责 `release`。"""
    auto = _automation()
    root = _element_from_handle(auto, int(hwnd))
    walker = _walker(auto)
    cur = root
    try:
        for idx in path:
            child = _first_child(walker, cur)
            i = 0
            while child and i < idx:
                nxt = _next_sibling(walker, child)
                _release(child)
                child = nxt
                i += 1
            if not child:
                raise UIAError("元素已经不在了（窗口内容变了）—— 重新 `get_window_state` 再看一眼")
            _release(cur)
            cur = child
        return cur
    except Exception:
        _release(cur)
        raise
    finally:
        _release(walker)


@win32.dpi_wrapped
def click_point(hwnd: int, path: tuple[int, ...]) -> tuple[int, int]:
    """元素的屏幕物理坐标中心点。`input.click` 用它。"""
    el = resolve(hwnd, path)
    try:
        l, t, r, b = _rect_prop(el, 43)
    finally:
        _release(el)
    if r <= l or b <= t:
        raise UIAError("这个元素没有可见区域（可能在滚动区外面）—— 先滚动或重新观察")
    return (l + r) // 2, (t + b) // 2


@win32.dpi_wrapped
def set_value(hwnd: int, path: tuple[int, ...], text: str) -> None:
    el = resolve(hwnd, path)
    pat = None
    try:
        pat = _pattern(el, VALUE_PATTERN)
        if not pat:
            raise UIAError("这个控件不支持直接赋值 —— 先 `click` 再 `type_text`")
        b = _OLEAUT32.SysAllocString(str(text))
        try:
            # IUIAutomationValuePattern::SetValue = vtable[3]
            hr = _fn(pat, 3, [ctypes.c_void_p, ctypes.c_void_p])(pat, b)
        finally:
            _OLEAUT32.SysFreeString(b)
        if hr < 0:
            raise UIAError("这个控件现在是只读的（SetValue 被拒）—— 先 `click` 再 `type_text`")
    finally:
        if pat:
            _release(pat)
        _release(el)


@win32.dpi_wrapped
def invoke(hwnd: int, path: tuple[int, ...]) -> None:
    el = resolve(hwnd, path)
    pat = None
    try:
        pat = _pattern(el, INVOKE_PATTERN)
        if not pat:
            raise UIAError("这个控件不支持 Invoke —— 用坐标 `click` 点它")
        # IUIAutomationInvokePattern::Invoke = vtable[3]
        hr = _fn(pat, 3, [ctypes.c_void_p])(pat)
        if hr < 0:
            raise UIAError("Invoke 被拒（控件可能已禁用 / 不可见）")
    finally:
        if pat:
            _release(pat)
        _release(el)


@win32.dpi_wrapped
def focus(hwnd: int, path: tuple[int, ...]) -> None:
    el = resolve(hwnd, path)
    try:
        # IUIAutomationElement::SetFocus = vtable[3]
        hr = _fn(el, 3, [ctypes.c_void_p])(el)
        if hr < 0:
            raise UIAError("这个控件不能获得焦点（SetFocus 被拒）")
    finally:
        _release(el)


@win32.dpi_wrapped
def focused_element(hwnd: int) -> UIElement | None:
    """当前焦点元素（只读，用来在观察里说一句"焦点在哪"）。"""
    auto = _automation()
    out = ctypes.c_void_p()
    # IUIAutomation::GetFocusedElement = vtable[8]
    hr = _fn(auto, 8, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])(
        auto, ctypes.byref(out))
    if hr < 0 or not out.value:
        return None
    try:
        hwnd_owner = _ptr_prop(out, 36)              # get_CurrentNativeWindowHandle
        if hwnd_owner and int(hwnd_owner) != int(hwnd):
            return None
        return UIElement(index=-1, path=(), name=_bstr(out, 23),
                         control_type=CONTROL_TYPES.get(_int_prop(out, 21), ""),
                         automation_id=_bstr(out, 29), class_name=_bstr(out, 30),
                         rect=_rect_prop(out, 43), enabled=bool(_int_prop(out, 28)),
                         offscreen=bool(_int_prop(out, 38)),
                         focusable=bool(_int_prop(out, 27)))
    finally:
        _release(out)
