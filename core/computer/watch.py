r"""接管期间的输入监视 —— 急停与"物理输入即退出"。

## 两件事，同一个低级钩子

| 事件 | 判据 | 结果 |
|---|---|---|
| 物理键 / 点击 / 滚轮 / 拖拽 | `LLKHF_INJECTED` / `LLMHF_INJECTED` 未置位 且 `armed` | 立即退出接管 |
| 注入的 `Esc` | 置位了 | 吞掉（`return 1`），不退出 |
| 物理 `Esc` | 未置位 | 退出（它就是急停，不看 `armed`） |
| 任何事件落在我们自己的窗口上 | 前台 pid ∈ `our_pids` | 不算干预 |

`armed` 解决一个矛盾：启用要点击、要打字下指令，
而那些输入会退出，改成两态：启用 ≠ 开始对抗检测；**模型第一次动电脑
那一刻**（`session.arm()`）才 `armed=True`，一轮结束自动 `armed=False`。
启用之后你可以从容地打开目标应用、在聊天框里下指令，那些都不算干预。

**为什么必须用 `WH_KEYBOARD_LL` 而不是 `RegisterHotKey`**：
`SendInput` 注入的键会被 `RegisterHotKey` 匹配到，agent 自己就能"按"出急停。
低级钩子的 `flags` 里带着这个事件是不是注入的，这是 Windows 上唯一
确定性的分界线。

钩子回调里只做三件便宜的事（`GetForegroundWindow` · `GetWindowThread
ProcessId` · 集合查一下）—— 低级钩子有 `LowLevelHooksTimeout`，回调慢了会被
系统静默摘掉。`our_pids` 在启用那一刻算一次（psutil 扫进程表很贵），
之后不再重算（窗口不会换进程）。

急停键被输入层硬拒（`guard.combo_decision` 拒 `esc`）—— 那是纵深：
即使这个钩子被摘掉，agent 也按不出 `Esc`。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

from . import guard, win32

IS_WINDOWS = sys.platform == "win32"

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_XBUTTONDOWN = 0x020B
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E

LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002
LLMHF_INJECTED = 0x00000001

VK_ESCAPE = 0x1B

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", WPARAM), ("lParam", LPARAM),
                ("time", wintypes.DWORD), ("pt", POINT),
                ("lPrivate", wintypes.DWORD)]


if IS_WINDOWS:                                       # pragma: no cover - 平台分支
    _u = win32.user32
    _k = win32.kernel32
    _u.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                     wintypes.HINSTANCE, wintypes.DWORD]
    _u.SetWindowsHookExW.restype = ctypes.c_void_p
    _u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, WPARAM, LPARAM]
    _u.CallNextHookEx.restype = LRESULT
    _u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    _u.UnhookWindowsHookEx.restype = wintypes.BOOL
    _u.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
    _u.GetMessageW.restype = ctypes.c_int
    _u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, WPARAM, LPARAM]
    _u.PostThreadMessageW.restype = wintypes.BOOL
    _k.GetCurrentThreadId.restype = wintypes.DWORD
    _k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _k.GetModuleHandleW.restype = wintypes.HMODULE


#: 钩子回调的类型（必须是模块级的，否则会被 GC 掉 —— 这是一个经典崩溃）
if IS_WINDOWS:                                       # pragma: no cover - 平台分支
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)
else:
    HOOKPROC = None


class _Watch:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.thread_id = 0
        self.our_pids: set[int] = set()
        self.reason = ""
        self._lock = threading.RLock()

    # 回调（每个都要快）
    def _foreground_is_ours(self) -> bool:
        try:
            hwnd = win32.foreground_hwnd()
            if not hwnd:
                return False
            pid = wintypes.DWORD(0)
            win32.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
            return int(pid.value) in self.our_pids
        except Exception:                            # noqa: BLE001
            return False

    def _is_armed(self) -> bool:
        """模型正在动电脑吗？（只有这时物理输入才算干预 —— 见模块头）。"""
        try:
            from . import session
            return session.is_armed()
        except Exception:                            # noqa: BLE001 - 钩子不许抛
            return False

    def _stop(self, reason: str) -> None:
        if self.reason:
            return
        self.reason = reason
        # 不在钩子里直接 disable：那样会在输入线程里做 IO / emit 事件。
        threading.Thread(target=_do_disable, args=(reason,), daemon=True).start()

    def _key_cb(self, ncode, wparam, lparam):
        try:
            if ncode >= 0:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                injected = bool(kb.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
                is_down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                # 注入的 Esc 放行（送到目标应用）。
                #   吞掉它的理由是"模型不能用它关掉用户对话框"，但模型**永远
                #   关不掉搜索框/弹窗**（微信搜索会卡住），而它点「取消」按钮
                #   一样关得掉，那条防御不成立。
                #   物理 Esc 仍然是急停（下面那支，`not injected`）。
                if is_down and not injected:
                    # 物理 `Esc` = 显式急停：不看 `armed`、也不看落在哪个窗口
                    #    （横幅上写着"esc退出"，它必须随时有效）。
                    if kb.vkCode == VK_ESCAPE:
                        self._stop("esc")
                    elif self._is_armed() and not self._foreground_is_ours():
                        self._stop("physical_key")
        except Exception:                            # noqa: BLE001 - 钩子不许抛
            pass
        return win32.user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _mouse_cb(self, ncode, wparam, lparam):
        try:
            if ncode >= 0:
                ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                injected = bool(ms.flags & LLMHF_INJECTED)
                is_action = wparam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN,
                                       WM_MBUTTONDOWN, WM_XBUTTONDOWN,
                                       WM_MOUSEWHEEL, WM_MOUSEHWHEEL)
                if (is_action and not injected
                        and self._is_armed() and not self._foreground_is_ours()):
                    self._stop("physical_mouse")
        except Exception:                            # noqa: BLE001
            pass
        return win32.user32.CallNextHookEx(None, ncode, wparam, lparam)

    # 生命周期
    def start(self, our_pids: set[int]) -> tuple[bool, str]:
        with self._lock:
            if self.thread and self.thread.is_alive():
                return True, ""
            if not IS_WINDOWS:
                return False, "接管模式只支持 Windows 桌面"
            self.our_pids = set(our_pids or ())
            self.reason = ""
            ready = threading.Event()
            result = {"ok": False, "why": ""}

            def _run():
                self.thread_id = _k.GetCurrentThreadId()
                self._kb_proc = HOOKPROC(self._key_cb)
                self._ms_proc = HOOKPROC(self._mouse_cb)
                hmod = _k.GetModuleHandleW(None)
                hk = _u.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
                hm = _u.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
                if not hk or not hm:
                    err = ctypes.get_last_error()
                    if hk:
                        _u.UnhookWindowsHookEx(hk)
                    if hm:
                        _u.UnhookWindowsHookEx(hm)
                    result["why"] = ("急停钩子装不起来（SetWindowsHookEx 失败，"
                                     "GetLastError=%d）—— 拒绝启用接管" % err)
                    ready.set()
                    return
                result["ok"] = True
                ready.set()
                msg = MSG()
                try:
                    while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                        _u.TranslateMessage(ctypes.byref(msg))
                        _u.DispatchMessageW(ctypes.byref(msg))
                finally:
                    _u.UnhookWindowsHookEx(hk)
                    _u.UnhookWindowsHookEx(hm)

            self.thread = threading.Thread(target=_run, name="computer-watch",
                                           daemon=True)
            self.thread.start()
            ready.wait(timeout=3.0)
            if not result["ok"]:
                self.thread = None
                return False, result["why"] or "急停钩子没起来（超时）"
            return True, ""

    def stop(self) -> None:
        with self._lock:
            t, tid = self.thread, self.thread_id
            self.thread = None
            self.thread_id = 0
        if t and t.is_alive() and tid:
            try:
                _u.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:                        # noqa: BLE001
                pass
            t.join(timeout=2.0)


_w = _Watch()


def _do_disable(reason: str) -> None:
    from . import session
    try:
        session.disable(reason)
    except Exception as exc:                         # noqa: BLE001
        print("[computer] 停止接管失败（%s）：%s: %s"
              % (reason, type(exc).__name__, exc))


def start(our_pids: set[int]) -> tuple[bool, str]:
    return _w.start(our_pids)


def stop() -> None:
    _w.stop()


def running() -> bool:
    return bool(_w.thread and _w.thread.is_alive())
