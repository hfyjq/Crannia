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
from contextlib import contextmanager
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


#: 单字符标点 -> 虚拟键码。快捷键里写 `ctrl+'` 比 `ctrl+quote` 自然，
#: 而 `parse_combo` 不会替它查英文名 —— 没有这张表就只有英文名能用。
#: 实测留档（2026-11-27 订正）：Photoshop 的 `Ctrl+'`（视图 > 显示 > 网格）
#: 这条快捷键人手动也按不出来（用户实测），所以"发不进去"不是注入的问题；
#: 同一条路发 `Ctrl+Shift+;` 是生效的，标点键本身没问题。这类快捷键请走菜单，
#: 别在按键上花时间。
_SYMBOL_VK: dict[str, int] = {
    "'": 0xDE, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, ";": 0xBA,
    "=": 0xBB, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD,
}


def _vk_of(token: str) -> tuple[int, bool]:
    """返回 `(虚拟键码, 是否扩展键)`。"""
    if token in _VK:
        return _VK[token], token in _EXTENDED
    if len(token) == 1:
        ch = token.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch), False
    if token in _SYMBOL_VK:
        return _SYMBOL_VK[token], False
    if token.startswith("vk_"):
        try:
            return int(token[3:], 0), False
        except ValueError:
            pass
    raise InputError("不认识这个键名：%r（用 enter / tab / ctrl / a / F5 这类写法）" % token)


# 事件构造
#: 虚拟键 -> scan code 的缓存。
#:
#: 只发 `wVk` 而不发 `wScan` 时，事件能到窗口，但接收方拿不到"是哪个键"
#: （文本控件收到的键名是空的）；修饰键状态仍然对 —— 那是按 VK 读的。
#: 所以坏的那一半只表现为"普通按键不起作用"，看起来像键盘根本没发出去。
_SCAN_CACHE: dict[int, int] = {}


def _scan_of(vk: int) -> int:
    """虚拟键码 -> scan code。查不到回 0（那时退回只发 VK）。"""
    try:
        got = _SCAN_CACHE.get(int(vk))
        if got is None:
            got = int(win32.user32.MapVirtualKeyW(int(vk), 0))    # MAPVK_VK_TO_VSC
            _SCAN_CACHE[int(vk)] = got
        return got
    except Exception:                                # noqa: BLE001
        return 0


def _key_event(vk: int, flags: int, scan: int = 0) -> "INPUT":
    ev = INPUT()
    ev.type = INPUT_KEYBOARD
    #: `KEYEVENTF_UNICODE` 那一支的 scan 是字符本身，`wVk` 必须是 0 —— 不许改写。
    if scan == 0 and vk and not (flags & KEYEVENTF_UNICODE):
        scan = _scan_of(vk)
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


def _button_flags(button: str) -> tuple[int, int, int]:
    """返回 `(down_flag, up_flag, mouseData)`。

    侧键不用（不是所有鼠标都有），但保留 mouseData 位是为了让
    left/right/middle 与 XDOWN/XUP 共用一条发送路径，将来要扩也只改这张表。
    """
    button = (button or "left").lower()
    return {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0))


def button_down(button: str) -> None:
    """只按下鼠标键，不松开。供脚本的 down/up 与多键拖用。"""
    down, _up, data = _button_flags(button)
    _send([_mouse_event(down, data=data)])


def button_up(button: str) -> None:
    _down, up, data = _button_flags(button)
    _send([_mouse_event(up, data=data)])


#: 修饰键按下后、与松开前的静置时间（秒）。
#:
#: 为什么必须有：`SendInput` 是一串独立调用，`held_keys` 的按下、
#: 动作里的鼠标事件、`finally` 里的松开之间只隔几微秒。消息循环快的
#: 程序当场就处理完；但重绘重、循环慢的程序常把鼠标事件压到下一帧
#: 再读键盘状态 —— 那时修饰键已经抬了，表现为"动作生效了，但修饰键
#: 没生效"（选中态没跟着变，只有指针挪过去）。人手按住修饰键时会保持
#: 几百毫秒，所以人工操作从不复现这一档。
KEY_SETTLE = 0.04
KEY_RELEASE_SETTLE = 0.06

#: 连按同一个键时，两次完整按下之间的距离。取值按人手连按的节奏，
#: 让重绘慢的程序有机会处理完上一次按下/松开。
#: 它不是"防丢键"的开关：实测有程序对连按次数本身有阈值，把间隔拉到
#: 300 ms 照样整批失效 —— 那不是时序能解决的，只能分成几次按。
REPEAT_GAP = 0.06


@contextmanager
def held_keys(combo: str, *, settle: float = KEY_SETTLE,
              release_settle: float = KEY_RELEASE_SETTLE):
    """按住修饰键执行一段动作，退出时逆序释放——异常也走 finally。

    修饰键跟随单次动作（不跨轮持有）：调用者把 click/drag/scroll 包在
    `with held_keys("ctrl+shift")` 里，就不会出现"模型忘了松开"的卡键。
    护栏在 tools 层过 guard.combo_decision，这里只管按下与释放。

    `settle` / `release_settle` 见上面那两个常量的注释：它们是给目标应用
    读键盘状态用的，不是给我们自己。脚本里每一步都带修饰键时可以传小一点
    （`_run_script` 就这么做——那里一次脚本最多 64 步，全按默认会白等几秒）。
    """
    tokens = parse_combo(combo)
    if not tokens:
        yield []
        return
    vks = [(_vk_of(t)) for t in tokens]
    for vk, ext in vks:
        _send([_key_event(vk, KEYEVENTF_EXTENDEDKEY if ext else 0)])
    if settle > 0:
        time.sleep(settle)
    try:
        yield tokens
    finally:
        if release_settle > 0:
            time.sleep(release_settle)
        for vk, ext in reversed(vks):
            _send([_key_event(vk, KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0))])


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
    down, up, data = _button_flags(button)
    n = max(1, min(3, int(count or 1)))
    _legacy_move_to(int(x), int(y))
    for i in range(n):
        win32.user32.mouse_event(down, 0, 0, data, 0)
        time.sleep(0.01)
        win32.user32.mouse_event(up, 0, 0, data, 0)
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
    down, up, data = _button_flags(button)
    try:
        move_to(x, y)
        for i in range(n):
            _send([_mouse_event(down, data=data), _mouse_event(up, data=data)])
            if i + 1 < n:
                time.sleep(0.05)
        return "sendinput"
    except InputError:
        if req not in ("", "auto"):
            raise
        print("[input] SendInput click failed, falling back to legacy real mouse")
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
def drag(x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3,
         button: str = "left") -> None:
    """两点间拖拽。button 换成中键/右键就是 3D 软件的平移/旋转导航。"""
    down, up, data = _button_flags(button)
    move_to(x1, y1)
    _send([_mouse_event(down, data=data)])
    try:
        steps = max(2, min(30, int(duration / 0.02)))
        for i in range(1, steps + 1):
            t = i / steps
            move_to(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
            time.sleep(max(0.0, duration / steps - 0.02))
    finally:
        # 中途异常也要松开，否则目标软件会一直以为键还按着。
        _send([_mouse_event(up, data=data)])


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
    down = [_key_event(vk, KEYEVENTF_EXTENDEDKEY if ext else 0)
            for vk, ext in vks]              # 按下：从左到右
    up = [_key_event(vk, KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0))
          for vk, ext in reversed(vks)]      # 松开：从右到左
    try:
        #: 按下与松开分两次发，中间留一段时间。挤在同一次调用里时，
        #: 重绘重的程序处理到 keydown 时 keyup 已经躺在队列里 —— 快捷键
        #: 常常因此不触发（人手按键天然有几十毫秒的持续）。
        _send(down)
        if KEY_SETTLE > 0:
            time.sleep(KEY_SETTLE)
        _send(up)
    except InputError:
        #: 部分发送时可能有 down 已经进队列、up 被拦 —— 键会卡住。
        #: 先补一次 up（对没按下的键松开无害），再决定要不要退 legacy。
        try:
            _send(up)
        except InputError:
            pass
        if req not in ("", "auto"):
            raise
        print("[input] SendInput key failed, falling back to legacy keybd_event")
        _legacy_press_key(tokens)
    return tokens


VK_CAPITAL = 0x14

#: 中文输入法的 LANGID（简/繁/香港/新加坡）。这些布局下字母键可能被 IME
#: 截获成候选框；英文布局不会，所以不必碰大写锁定。
_CJK_LANGIDS = frozenset({0x0804, 0x0404, 0x0C04, 0x1004})
#: 英语（美国）—— 临时切换的目标布局，只认已安装的那份。
EN_US_LANGID = 0x0409


def caps_lock_on() -> bool | None:
    """大写锁定现在开着吗？读不到返回 `None`（不猜）。

    低 bit 是开关态（caps / num / scroll），这是 `GetKeyState` 的文档语义；
    `GetAsyncKeyState` 的低 bit 是"上次查询之后按过"，两者不是一件事，
    别替换。代价是它按线程的消息队列取值 —— 后台线程可能读到旧值，所以
    读数里拿不准时一律写"读不到"，不猜成"关"。
    """
    if not win32.IS_WINDOWS:
        return None
    try:
        return bool(int(win32.user32.GetKeyState(VK_CAPITAL)) & 0x0001)
    except Exception:                                # noqa: BLE001
        return None


def needs_ime_bypass(tokens: list[str]) -> bool:
    """这个组合键会不会被输入法截获（=值不值得为它临时开大写锁定）。

    只认"没有修饰键的单字母"。实测：`Ctrl+1` / `Ctrl+0` / `Alt+F` 都原样
    到达目标窗口，`Tab` / `F10` 也一样 —— 输入法截获的是裸字母（拿去拼
    候选框）。给别的键也开大写锁定，只会让用户的指示灯闪一下、外加两次
    多余的注入。
    """
    if len(tokens) != 1:
        return False
    tok = tokens[0]
    return len(tok) == 1 and "a" <= tok <= "z"


#: 常见键盘布局的 LANGID -> 给人看的名字。只列会进"读数"的那几类；
#: 其余按 `0xXXXX` 原样报出 —— 不猜语言。
_LANG_NAMES = {
    0x0409: "英语（美国）",
    0x0804: "中文（简体）",
    0x0404: "中文（繁体）",
    0x0C04: "中文（香港）",
    0x1004: "中文（新加坡）",
    0x0411: "日语",
    0x0412: "韩语",
}


def keyboard_layout(hwnd: int | None = None) -> tuple[int, str] | None:
    """目标窗口线程的键盘布局 `(langid, 名字)`；读不到返回 None。

    必须按目标窗口的线程取。`GetKeyboardLayout(0)` 问的是调用线程，
    而框架跑在后台工作线程里 —— 那个线程的布局可能一直停在进程启动时，
    与用户此刻在目标应用里用的输入法无关（症状：中文输入法在吃键，框架
    却判成英文布局，大写锁定旁路因此从不触发）。
    """
    if not win32.IS_WINDOWS:
        return None
    hkl = None
    try:
        if hwnd:
            hkl = win32.keyboard_layout_of(hwnd)
        else:
            hkl = int(win32.user32.GetKeyboardLayout(0) or 0)
    except Exception:                                # noqa: BLE001
        return None
    langid = int(hkl or 0) & 0xFFFF
    if not langid:
        return None
    return langid, _LANG_NAMES.get(langid, "0x%04X" % langid)


def ime_may_intercept(hwnd: int | None = None) -> bool:
    """目标窗口的布局是不是中文（IME 可能把字母键转成候选框）。

    *hwnd* 不给时退回调用线程的布局 —— 只为兼容旧调用点与测试；
    真实动作一律传目标窗口。
    """
    lay = keyboard_layout(hwnd)
    return bool(lay and lay[0] in _CJK_LANGIDS)


def ime_open(hwnd: int) -> bool | None:
    """目标窗口的输入法此刻开着吗？读不到返回 None（不猜）。

    TSF 系输入法（如搜狗）不归 IMM32 管：`ImmGetContext` 可能拿不到
    上下文，或拿得到但开关状态与用户看到的候选框无关 —— 那种情况如实
    报"读不到"，不拿它当"输入法是关的"。
    """
    if not win32.IS_WINDOWS:
        return None
    himc = None
    try:
        imm = ctypes.windll.imm32
        himc = imm.ImmGetContext(int(hwnd))
        if not himc:
            return None
        return bool(imm.ImmGetOpenStatus(himc))
    except Exception:                                # noqa: BLE001
        return None
    finally:
        if himc:
            try:
                ctypes.windll.imm32.ImmReleaseContext(int(hwnd), himc)
            except Exception:                        # noqa: BLE001
                pass


#: 会"卡住"的修饰键 / 鼠标键 —— 读数里报出来，让模型知道有没有残留的
#: 按下状态（框架自己的 finally 会松，但崩溃 / 外部注入可能留下）。
_HELD_VKS = ((0x11, "ctrl"), (0x10, "shift"), (0x12, "alt"),
             (0x5B, "win"), (0x5C, "win"), (0x01, "鼠标左键"),
             (0x02, "鼠标右键"), (0x04, "鼠标中键"))


def pressed_keys() -> list[str] | None:
    """此刻还按着的修饰键 / 鼠标键；读不到返回 None（不猜成"没有"）。"""
    if not win32.IS_WINDOWS:
        return None
    try:
        out: list[str] = []
        for vk, name in _HELD_VKS:
            down = win32.key_is_down(vk)
            if down is None:                     # 读不到 ≠ 没按着
                return None
            if down and name not in out:
                out.append(name)
        return out
    except Exception:                                # noqa: BLE001
        return None


def keyboard_focus(hwnd: int) -> dict | None:
    """目标线程此刻的键盘焦点是不是"在目标窗口里"。

    返回 `{"active": "类名「标题」", "within": bool}`；读不到返回 None。
    判据是活动窗口的根窗口 == 目标窗口。活动窗口是另一个顶层窗口
    （例如 Photoshop 的浮动操作栏 `OS_ViewContainer`）时，键盘事件不会到
    目标窗口 —— 而"前台"仍可能是目标窗口，两者不是一件事。
    """
    if not win32.IS_WINDOWS:
        return None
    tid = win32.thread_id_of(hwnd)
    active, _focus = win32.thread_focus(tid)
    if not active:
        return None
    try:
        within = win32.root_of(active) == int(hwnd)
    except Exception:                                # noqa: BLE001
        return None
    return {"active": win32.window_brief(active),
            "class": win32.class_name_of(active),
            "within": bool(within)}


def input_context(hwnd: int) -> dict:
    """按目标窗口读一组事实（回执的「框架读数」段用它；从不抛）。

    五个格：`layout` / `ime_open` / `caps` / `held` / `focus`。读不到的格
    是 None —— 调用方必须把 None 渲染成"读不到"，不许当成 False。
    """
    return {
        "layout": keyboard_layout(hwnd),
        "ime_open": ime_open(hwnd),
        "caps": caps_lock_on(),
        "held": pressed_keys(),
        "focus": keyboard_focus(hwnd),
    }


@contextmanager
def caps_lock_guard(active: bool):
    """必要时临时打开大写锁定，退出时按原状态还原。

    为什么有用：中文输入法在大写模式下不把字母键转成候选框，而目标程序的
    快捷键不区分大小写 —— 这是"不用换输入法"的一条旁路。

    什么时候动它：调用方说需要（中文布局）且现在确实关着。读不到状态
    （`None`）时不动 —— 不知道用户原本什么状态，就不该改。
    """
    if not active:
        yield False
        return
    was = caps_lock_on()
    if was is None or was:
        yield False
        return
    try:
        press_key("capslock")
        yield True
    finally:
        try:
            press_key("capslock")
        except Exception:                            # noqa: BLE001
            pass


@contextmanager
def ime_muted(hwnd: int):
    """注入按键期间临时关掉目标窗口的输入法；退出时按原状态恢复。

    中文输入法会截获字母键、转成候选框，目标程序收不到原始按键 ——
    症状是"键盘完全没反应，而鼠标一切正常"，且换到英文输入法就恢复。
    这里只动这个窗口的 IME 上下文，不改用户全局的输入法。

    产物是"这次有没有真的关过"（True/False）：没关成也不报错 ——
    TSF 系的输入法不归 IMM32 管，那种情况只能如实告诉调用方。
    """
    if not win32.IS_WINDOWS:
        yield False
        return
    himc = None
    imm = None
    was_open = False
    try:
        imm = ctypes.windll.imm32
        himc = imm.ImmGetContext(int(hwnd))
        if himc:
            was_open = bool(imm.ImmGetOpenStatus(himc))
            if was_open:
                imm.ImmSetOpenStatus(himc, False)
    except Exception:                                # noqa: BLE001
        himc = None
    try:
        yield was_open
    finally:
        if himc is not None:
            try:
                if was_open:
                    imm.ImmSetOpenStatus(himc, True)
                imm.ImmReleaseContext(int(hwnd), himc)
            except Exception:                        # noqa: BLE001
                pass


def _pick_english_layout(installed) -> int | None:
    """从 `[(HKL, langid)]` 里挑英语（美国）；没有返回 None（不替用户装）。"""
    for hkl, langid in installed or ():
        if langid == EN_US_LANGID:
            return int(hkl)
    return None


def english_layout_hkl() -> int | None:
    """已安装的英语（美国）布局 HKL；没装返回 None。"""
    return _pick_english_layout(win32.installed_layouts())


def _wait_layout(hwnd: int, langid: int, timeout: float = 0.4) -> bool:
    """等目标线程的布局变成 *langid*；超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = keyboard_layout(hwnd)
        if now and now[0] == langid:
            return True
        time.sleep(0.02)
    return False


@contextmanager
def english_layout_guard(hwnd: int, active: bool):
    """为这次注入临时把目标窗口切到英语布局，退出时还原。

    为什么用 `WM_INPUTLANGCHANGEREQUEST`：它是系统给"窗口级输入语言切换"
    的官方消息，作用在目标线程上；注入 Win+Space 既被护栏禁止（Win 键），
    真机也按不出来。实测中文 0x0804 <-> 英语 0x0409 都切得动、读得回。

    只在"中文布局 + 这个键会被 IME 吃掉"时动（判据与 caps_lock_guard
    同源）。产物是 `{"switched": bool, "restored": bool}`：没切成功就
    如实报 swapped=False；切成功但还原失败时 restored=False —— 调用方
    必须把这件事告诉模型（用户的状态被留在英语了）。
    """
    res = {"switched": False, "restored": True}
    if not active:
        yield res
        return
    lay = keyboard_layout(hwnd)
    if not lay or lay[0] not in _CJK_LANGIDS:
        yield res
        return
    hkl = english_layout_hkl()
    if hkl is None:
        yield res
        return
    before = win32.keyboard_layout_of(hwnd)
    if not before or not win32.post_input_language_change(hwnd, hkl):
        yield res
        return
    if not _wait_layout(hwnd, EN_US_LANGID):
        # 消息可能晚到：走之前先试着还原，不能把用户留在英语。
        win32.post_input_language_change(hwnd, before)
        _wait_layout(hwnd, lay[0], timeout=0.2)
        yield res
        return
    res["switched"] = True
    try:
        yield res
    finally:
        win32.post_input_language_change(hwnd, before)
        res["restored"] = _wait_layout(hwnd, lay[0])


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
