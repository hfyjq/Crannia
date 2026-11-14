"""`SendInput` 封装 —— 接管里唯一真正"动"的地方。

四条纪律：

1. 坐标是虚拟屏幕的绝对坐标（`MOUSEEVENTF_ABSOLUTE | VIRTUALDESK`）——
   多显示器下也不能用主屏宽度归一化，否则第二块屏上的点击全部偏移。
2. 注入失败要出声 —— `SendInput` 被 UIPI 挡住时返回的插入数少于事件数，
   而 `GetLastError` 不会告诉你原因（官方逐字）。这里按"少一个就抛"处理。
3. 文本走 `KEYEVENTF_UNICODE`，不走虚拟键 —— 否则中文 / emoji 打不出来。
   `\n` / `\t` 例外：转成 `Return` / `Tab`（文本框不认裸 0x0A）。
4. 每个公开动作都不做权限判断 —— 硬拒绝在 `guard.py`，调用方先问再调。
   这一层只管"按住键、松开键"。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from . import win32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


if win32.IS_WINDOWS:                                 # pragma: no cover - 平台分支
    win32.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    win32.user32.SendInput.restype = wintypes.UINT


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

#: 长文本注入的分批大小（字符数）与批间停顿。
#:
#: 316 字一次性 `SendInput`，Windows 11 记事本只正确
#: 收到前几个字，其余退化成"重复首字"（`最后一班船` 之后是一长串 `夜夜夜…`）。
#: 同一份事件序列在 Tk 里逐字正确，不是事件构造错，是 WinUI/RichEdit
#: 一类的输入队列消化不了大批注入。每批 16 字 + 4ms，给目标留出处理时间。
#: 这不是"性能优化"：去掉它会让长文本在记事本里静默损坏。
_TEXT_BATCH_CHARS = 16
_TEXT_BATCH_PAUSE = 0.004


class InputError(RuntimeError):
    """注入失败 —— `brief` 是给模型/用户看的那一句。"""

    def __init__(self, brief: str, detail: str = ""):
        super().__init__(brief)
        self.brief = brief
        self.detail = detail


# 两条后端 / 两种输入方式
#
# （微信）：`SendInput` 的绝对坐标点击
#   落点对、红十字对、界面毫无反应；同一位置改 `SetCursorPos` +
#   `mouse_event`（DPI-aware）就生效。两条路并存，由调用方选。
POINTER_BACKENDS = ("sendinput", "legacy")
INPUT_METHODS = ("auto", "type", "paste")


def resolve_pointer_backend(requested: str = "auto") -> str:
    """`auto` → `sendinput`（失败由 `click` 再退 legacy）；否则原样校验。"""
    v = str(requested or "auto").strip().lower()
    if v in ("", "auto"):
        return "sendinput"
    if v not in POINTER_BACKENDS:
        raise InputError("pointer 只认 auto / sendinput / legacy（收到 %r）"
                         % (requested,))
    return v


def choose_input_method(text, requested: str = "auto") -> str:
    """`auto`，含非 ASCII 就走剪贴板粘贴（中文/输入法场景），否则逐字。

    禁粘贴的站点（PTA 那类）由调用方显式传 `type`；`paste` 也可以强制。
    """
    v = str(requested or "auto").strip().lower()
    if v not in INPUT_METHODS:
        raise InputError("input_method 只认 auto / type / paste（收到 %r）"
                         % (requested,))
    if v != "auto":
        return v
    return "paste" if any(ord(ch) > 0x7F for ch in str(text or "")) else "type"


# 键名表
_VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "meta": 0x5B, "super": 0x5B,
    "numlock": 0x90, "scrolllock": 0x91,
    "semicolon": 0xBA, "equals": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "slash": 0xBF, "backtick": 0xC0,
    "bracketleft": 0xDB, "backslash": 0xDC, "bracketright": 0xDD,
    "quote": 0xDE,
    # F1=0x70 … F24=0x87
    **{"f%d" % i: 0x6F + i for i in range(1, 25)},
}

#: 需要 `KEYEVENTF_EXTENDEDKEY` 的键（方向键 / 编辑键 / 小键盘 / 右侧修饰键）
_EXTENDED = frozenset({
    "insert", "delete", "del", "home", "end", "pageup", "pagedown",
    "left", "right", "up", "down", "printscreen", "numlock", "scrolllock",
    "divide", "multiply", "subtract", "add", "decimal",
})

_ALIASES = {
    "control_l": "ctrl", "control_r": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "shift_l": "shift", "shift_r": "shift",
    "alt_l": "alt", "alt_r": "alt",
    "return": "enter", "escape": "esc", "del": "delete",
    "super_l": "win", "super_r": "win", "meta_l": "win", "meta_r": "win",
    "arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down",
    "page_up": "pageup", "page_down": "pagedown", "caps_lock": "capslock",
}


def parse_combo(combo: str) -> list[str]:
    """`"Control_L+a"` → `["ctrl", "a"]`；`"Return"` → `["enter"]`。

    只做归一化，不做权限判断 —— 权限在 `guard.combo_decision`。
    """
    out: list[str] = []
    for raw in str(combo or "").split("+"):
        token = raw.strip().lower().replace(" ", "_")
        if not token:
            continue
        token = _ALIASES.get(token, token)
        out.append(token)
    return out


def _vk_of(token: str) -> tuple[int, bool]:
    """返回 `(虚拟键码, 是否扩展键)`。"""
    if token in _VK:
        return _VK[token], token in _EXTENDED
    if len(token) == 1:
        ch = token.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch), False
    if token.startswith("vk_"):
        try:
            return int(token[3:], 0), False
        except ValueError:
            pass
    raise InputError("不认识这个键名：%r（用 enter / tab / ctrl / a / F5 这类写法）" % token)


# 事件构造
def _key_event(vk: int, flags: int, scan: int = 0) -> "INPUT":
    ev = INPUT()
    ev.type = INPUT_KEYBOARD
    ev.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return ev


def _mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> "INPUT":
    ev = INPUT()
    ev.type = INPUT_MOUSE
    ev.u.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data,
                         dwFlags=flags, time=0, dwExtraInfo=0)
    return ev


def _send(events: list["INPUT"]) -> None:
    if not win32.IS_WINDOWS:
        raise InputError("接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform)
    if not events:
        return
    arr = (INPUT * len(events))(*events)
    sent = win32.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        err = ctypes.get_last_error()
        raise InputError(
            "按键/鼠标注入被系统挡掉了（%d/%d 条进入队列）—— 多半是目标权限更高（UIPI），"
            "或者安全桌面正在前台" % (sent, len(events)),
            detail="SendInput sent=%d expected=%d GetLastError=%d" % (sent, len(events), err))


def _abs_xy(x: int, y: int) -> tuple[int, int]:
    vx, vy, vw, vh = win32.virtual_screen()
    if vw <= 1 or vh <= 1:
        raise InputError("取不到虚拟屏幕尺寸（多显示器句柄异常）")
    nx = int(round((x - vx) * 65535 / (vw - 1)))
    ny = int(round((y - vy) * 65535 / (vh - 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


# 公开动作
@win32.dpi_wrapped
def move_to(x: int, y: int) -> None:
    nx, ny = _abs_xy(int(x), int(y))
    _send([_mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                        nx, ny)])
    time.sleep(0.02)


def _button_flags(button: str) -> tuple[int, int]:
    button = (button or "left").lower()
    return {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))


@win32.dpi_wrapped
def _legacy_move_to(x: int, y: int) -> None:
    """真实鼠标移动（`SetCursorPos`）—— 微信/Qt 那条路。

    必须在 `dpi_aware` 里：进来的 (x, y) 是物理像素，DPI 不感知的
    线程会让系统再缩放一次（症状与 `SendInput` 坐标偏移同形）。
    """
    if not win32.IS_WINDOWS:
        raise InputError("接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform)
    if not win32.user32.SetCursorPos(int(x), int(y)):
        err = ctypes.get_last_error()
        raise InputError("真实鼠标移动到 (%d,%d) 失败" % (int(x), int(y)),
                         detail="SetCursorPos GetLastError=%d" % err)
    time.sleep(0.02)


@win32.dpi_wrapped
def _legacy_click(x: int, y: int, *, button: str = "left", count: int = 1) -> None:
    """`SetCursorPos` + `mouse_event` —— 与 SendInput 并存的那条路。"""
    if not win32.IS_WINDOWS:
        raise InputError("接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform)
    down, up = _button_flags(button)
    n = max(1, min(3, int(count or 1)))
    _legacy_move_to(int(x), int(y))
    for i in range(n):
        win32.user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.01)
        win32.user32.mouse_event(up, 0, 0, 0, 0)
        if i + 1 < n:
            time.sleep(0.05)


@win32.dpi_wrapped
def click(x: int, y: int, *, button: str = "left", count: int = 1,
          backend: str = "auto") -> str:
    """在屏幕物理坐标 (x, y) 点击。`button` = left / right / middle。

    *backend*：`auto`（默认，SendInput；抛错时退 legacy）· `sendinput` ·
    `legacy`（`SetCursorPos` + `mouse_event`；微信这类 Qt 窗口用这条）。

    `auto` 检测不到"SendInput 成功但界面不理"—— 那正是微信的病
    （注入返回 success，应用自己丢掉了）。模型看到"点击没反应"时要显式
    传 `legacy` 再试一次。

    返回实际用的后端（`sendinput` / `legacy`）—— 回执要把这件事说出去。
    """
    req = str(backend or "auto").strip().lower()
    if req not in ("", "auto") + POINTER_BACKENDS:
        raise InputError("pointer 只认 auto / sendinput / legacy（收到 %r）"
                         % (backend,))
    n = max(1, min(3, int(count or 1)))
    if req == "legacy":
        _legacy_click(x, y, button=button, count=n)
        return "legacy"
    down, up = _button_flags(button)
    try:
        move_to(x, y)
        for i in range(n):
            _send([_mouse_event(down), _mouse_event(up)])
            if i + 1 < n:
                time.sleep(0.05)
        return "sendinput"
    except InputError:
        if req not in ("", "auto"):
            raise
        print("[input] SendInput 点击失败，退到 legacy 真实鼠标")
        _legacy_click(x, y, button=button, count=n)
        return "legacy"


@win32.dpi_wrapped
def scroll(x: int, y: int, *, vertical: int = 0, horizontal: int = 0) -> None:
    """在 (x, y) 滚动。正数 = 向下/向右（内部换算成 Windows 的负 wheel）。"""
    move_to(x, y)
    events: list["INPUT"] = []
    steps = int(vertical or 0)
    if steps:
        events.append(_mouse_event(MOUSEEVENTF_WHEEL, data=-steps * WHEEL_DELTA))
    steps_h = int(horizontal or 0)
    if steps_h:
        events.append(_mouse_event(MOUSEEVENTF_HWHEEL, data=-steps_h * WHEEL_DELTA))
    _send(events)


@win32.dpi_wrapped
def drag(x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3) -> None:
    move_to(x1, y1)
    _send([_mouse_event(MOUSEEVENTF_LEFTDOWN)])
    steps = max(2, min(30, int(duration / 0.02)))
    for i in range(1, steps + 1):
        t = i / steps
        move_to(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
        time.sleep(max(0.0, duration / steps - 0.02))
    _send([_mouse_event(MOUSEEVENTF_LEFTUP)])


def type_text(text: str) -> None:
    """把 `text` 打进当前焦点。中文 / emoji 走 Unicode 注入。

    焦点由调用方负责（先 `click` 或 UIA `set_value`）。本函数不抢焦点。
    """
    if not text:
        return
    events: list[INPUT] = []
    text = str(text)
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
            i += 2
            events.extend([_key_event(0x0D, 0), _key_event(0x0D, KEYEVENTF_KEYUP)])
            continue
        if ch in ("\n", "\r"):
            i += 1
            events.extend([_key_event(0x0D, 0), _key_event(0x0D, KEYEVENTF_KEYUP)])
            continue
        if ch == "\t":
            i += 1
            events.extend([_key_event(0x09, 0), _key_event(0x09, KEYEVENTF_KEYUP)])
            continue
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
        else:
            units = [code]
        for unit in units:
            events.append(_key_event(0, KEYEVENTF_UNICODE, unit))
            events.append(_key_event(0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, unit))
        i += 1
    # 分批发送（见上面 `_TEXT_BATCH_CHARS` 的说明）——
    #   一次性把 300+ 字的 600+ 条事件塞进去，记事本会丢字并重复。
    step = max(2, _TEXT_BATCH_CHARS * 2)   # 每个字符 = down + up 两条
    for start in range(0, len(events), step):
        _send(events[start:start + step])
        if start + step < len(events):
            time.sleep(_TEXT_BATCH_PAUSE)


# 剪贴板 + Ctrl+V
#
# 中文 / 输入法场景：逐字 `KEYEVENTF_UNICODE` 会被当前输入法吞掉；实际
#   （微信搜索框）只有"写剪贴板 + Ctrl+V"这条路通。ASCII 仍可走逐字。
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _set_clipboard_text(text: str) -> None:
    """把 *text* 写进 Windows 剪贴板（CF_UNICODETEXT）。"""
    if not win32.IS_WINDOWS:
        raise InputError("接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform)
    data = str(text)
    size = (len(data) + 1) * ctypes.sizeof(ctypes.c_wchar)
    h = win32.kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not h:
        raise InputError("剪贴板：分配内存失败")
    p = win32.kernel32.GlobalLock(h)
    if not p:
        win32.kernel32.GlobalFree(h)
        raise InputError("剪贴板：锁定内存失败")
    try:
        ctypes.memmove(p, ctypes.create_unicode_buffer(data), size)
    finally:
        win32.kernel32.GlobalUnlock(h)

    opened = False
    for _ in range(10):                      # 别的程序可能正占着剪贴板
        if win32.user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.02)
    if not opened:
        win32.kernel32.GlobalFree(h)
        raise InputError("剪贴板被别的程序占着，打不开 —— 等一会再试")
    try:
        if not win32.user32.EmptyClipboard():
            raise InputError("剪贴板：清空失败")
        if not win32.user32.SetClipboardData(CF_UNICODETEXT, h):
            raise InputError("剪贴板：写入失败")
        h = None                             # 成功，所有权归系统，不许再 free
    finally:
        win32.user32.CloseClipboard()
        if h:
            win32.kernel32.GlobalFree(h)


@win32.dpi_wrapped
def _legacy_press_key(tokens: list[str]) -> None:
    """`keybd_event` 那条路（与 `SendInput` 并存）。"""
    if not win32.IS_WINDOWS:
        raise InputError("接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform)
    vks = [_vk_of(t) for t in tokens]
    for vk, ext in vks:
        win32.user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY if ext else 0, 0)
    for vk, ext in reversed(vks):
        win32.user32.keybd_event(
            vk, 0, KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0), 0)


def press_key(combo: str, *, backend: str = "auto") -> list[str]:
    """按一个组合键，例如 `"Control_L+a"` / `"Return"` / `"ctrl+shift+t"`。

    返回归一化后的键名（给审计用）。*backend* 与 `click` 同一套：
    `auto` 走 SendInput（抛错时退 legacy）；`legacy` 直接用 `keybd_event`。
    """
    tokens = parse_combo(combo)
    if not tokens:
        raise InputError("没有按键")
    req = str(backend or "auto").strip().lower()
    if req not in ("", "auto") + POINTER_BACKENDS:
        raise InputError("pointer 只认 auto / sendinput / legacy（收到 %r）"
                         % (backend,))
    if req == "legacy":
        _legacy_press_key(tokens)
        return tokens
    vks = [(_vk_of(t)) for t in tokens]
    events: list["INPUT"] = []
    for vk, ext in vks:                      # 按下：从左到右
        events.append(_key_event(vk, KEYEVENTF_EXTENDEDKEY if ext else 0))
    for vk, ext in reversed(vks):            # 松开：从右到左
        events.append(_key_event(vk, KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0)))
    try:
        _send(events)
    except InputError:
        if req not in ("", "auto"):
            raise
        print("[input] SendInput 按键失败，退到 legacy keybd_event")
        _legacy_press_key(tokens)
    return tokens


def paste_text(text: str, *, backend: str = "auto") -> None:
    """写剪贴板 + Ctrl+V（中文 / 输入法场景）。"""
    if not text:
        return
    _set_clipboard_text(str(text))
    time.sleep(0.05)                         # 给目标一点时间看到剪贴板变化
    press_key("ctrl+v", backend=backend)


def enter_text(text: str, *, method: str = "auto", backend: str = "auto") -> str:
    """按 *method* 选 `type_text` 或 `paste_text`，返回实际用的那个。"""
    chosen = choose_input_method(text, method)
    if chosen == "paste":
        paste_text(text, backend=backend)
    else:
        type_text(text)
    return chosen
