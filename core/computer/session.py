r"""接管会话状态 —— 全进程只有一份。

## 为什么是单例，不是"每会话一份"

Windows 一个登录会话只有一个输入桌面（`Winsta0\Default`），`SendInput`
也没有目标窗口参数 —— 两个 agent 同时接管，事件会交织。同一时刻
只允许一条会话持有接管；第二个人来要，拒绝并说清，不是排队。

## 三条判据

1. `/clear` 有意不清（同 `mode`）：一次启用整个会话有效，
   除非关闭软件或再次点击接管按钮 —— `/clear` 不在那两条里。
   登记在 `state_registry` 的 `SESSION_SCOPED_BY_OTHER`，
   purge=None，理由写在那一格。
2. 观察快照存"路径"不存 COM 指针 —— 动作时重新解析（`uia.resolve`），
   窗口变了就失败，这正是"一次观察 → 一个动作"要的失败方向。
3. 窗口 id / 观察 id 单调递增、永不重用 —— 重用一个 id 会让一条
   迟到的点击打到另一个窗口上（比报错坏得多）。

## 与"物理输入即退出"的接口

`watch.py` 检测到物理输入后调 `disable("physical_input")`；
`subscribe()` 的监听者在锁外被调用（监听者可能 emit 事件 / 碰 IO）。
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import win32


class TakeoverError(RuntimeError):
    pass


@dataclass
class Observation:
    obs_id: str
    window_id: str
    hwnd: int
    exe: str
    rect: tuple[int, int, int, int]     # 窗口外框（物理像素）—— 过期检测用
    elements: list = field(default_factory=list)   # list[uia.UIElement]
    created: float = 0.0
    consumed: bool = False
    #: 这一张图对应的物理矩形。
    #: 全窗截图 = `rect`；局部放大 = 裁剪出来的那一块。模型的 `x`/`y`
    #: 是 0–1000 比例，永远相对它看到的那张图，这里就是映射的底。
    coord_rect: tuple[int, int, int, int] | None = None


@dataclass
class Takeover:
    owner: str
    started: float = field(default_factory=time.time)
    authorized: set[str] = field(default_factory=set)      # exe 小写 basename
    windows: dict[str, win32.WindowInfo] = field(default_factory=dict)
    observations: dict[str, Observation] = field(default_factory=dict)
    latest_by_window: dict[str, str] = field(default_factory=dict)
    #: 每个窗口最近一次点击的物理点（下一张截图用来画红十字；取一次就清）。
    last_click: dict[str, tuple[int, int]] = field(default_factory=dict)
    seq: int = 0
    stop_reason: str = ""
    #: 「接管」与「正在对抗检测」是两态。
    #: 启用之后用户还要打字下指令 / 去打开目标应用——那些输入不该算干预。
    #:模型第一次动电脑那一刻（`tools.computer()` 被调）才 `armed=True`；
    #: 一轮结束（`PostProcess` 收尾）自动 `armed=False`。
    #: `Esc` 不受这一格影响 —— 它是显式急停，启用期间永远有效。
    armed: bool = False
    armed_at: float = 0.0
    #: 启用那一刻算好的"我们自己"的 pid（Crannia 后端 + 桌面壳）。
    #: 用途：`list_windows` 把它们剔出去，`window_decision` 也据此拒绝 ——
    #: 用户绝不该把"控制 Crannia 自己"授权出去（那等于把急停按钮交出去）。
    our_pids: set[int] = field(default_factory=set)


_lock = threading.RLock()
_state: Takeover | None = None
_listeners: list[Callable[[str, str], None]] = []   # (owner, reason)
#: 最近一次为什么关的 —— 界面要能说一句人话（"检测到你的键盘输入"）。
#: 它是进程级的史（同 `_recent_commands`），`/clear` 不清。
_last_stop: dict = {"owner": "", "reason": "", "at": 0.0}


# 生命周期
_enable_lock = threading.Lock()


def enable(owner: str) -> tuple[bool, str]:
    """开启接管。模式校验在调用方（这里不 import `core.modes`，避免循环）。"""
    global _state
    if not win32.IS_WINDOWS:
        return False, "接管模式只支持 Windows 桌面（当前系统 %s）" % sys.platform
    if not owner:
        return False, "拿不到当前会话"
    if win32.is_locked():
        return False, "桌面已锁屏 —— 先解锁再启用接管"
    # 整段串行：两个通道同时按「启用」不能装出两套钩子 / 两半状态。
    with _enable_lock:
        with _lock:
            if _state is not None:
                if _state.owner == owner:
                    return True, "接管已经开着"
                return False, "另一个对话正在接管这台电脑 —— 先在那里退出再启用"
        # 急停钩子是启用的一部分，不是可选项：装不起来就不许启用
        #   （否则"用户一碰键鼠就退出"这条承诺是空的）。
        #   在发布状态之前装 —— 不能出现"已经算开着、但钩子还没起来"的窗口。
        try:
            from . import watch
            ours = win32.our_pids()
            ok, why = watch.start(ours)
            if not ok:
                return False, why
        except Exception as exc:                     # noqa: BLE001
            return False, "急停钩子装不起来（%s: %s）—— 拒绝启用接管" % (
                type(exc).__name__, exc)
        with _lock:
            _state = Takeover(owner=owner, our_pids=set(ours))
        return True, ""


def disable(reason: str = "") -> str:
    """关闭接管，返回之前的所有者（没有就返回空串）。"""
    global _state
    with _lock:
        if _state is None:
            return ""
        owner = _state.owner
        _state.stop_reason = reason or ""
        _state = None
        _last_stop.update({"owner": owner, "reason": reason or "",
                           "at": time.time()})
        listeners = list(_listeners)
    try:
        from . import watch
        watch.stop()
    except Exception as exc:                         # noqa: BLE001
        print("[computer] 摘急停钩子失败：%s: %s" % (type(exc).__name__, exc))
    for fn in listeners:
        try:
            fn(owner, reason or "")
        except Exception as exc:                     # noqa: BLE001 - 监听者坏了不能反噬
            print("[computer] stop listener failed: %s: %s" % (type(exc).__name__, exc))
    return owner


def disable_for_session(session_id: str, reason: str = "") -> bool:
    """只有这条会话是持有者时才关（模式切走 / 会话清理）。返回是否真的关了。"""
    with _lock:
        if _state is None or not session_id or _state.owner != session_id:
            return False
    disable(reason)
    return True


def subscribe(fn: Callable[[str, str], None]) -> None:
    """注册一个"接管被关掉了"的监听者（锁外调用）。"""
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def arm() -> None:
    """模型开始动电脑了，打开对抗检测。"""
    with _lock:
        if _state is not None and not _state.armed:
            _state.armed = True
            _state.armed_at = time.time()
            print("[computer] 已进入操作检测（模型开始动作；物理输入将退出接管）")


def disarm_for(session_id: str, reason: str = "") -> None:
    """一轮结束，关掉对抗检测（只关持有者的那一轮，六个通道并行）。

    不传 `session_id` 的调用会关掉任何人的 —— 只给 `disable()` 用。
    """
    with _lock:
        if _state is None:
            return
        if session_id and _state.owner != session_id:
            return
        if _state.armed:
            _state.armed = False
            print("[computer] 操作检测结束（%s）" % (reason or "turn_end"))


def is_armed() -> bool:
    """只有这一格为真时，物理键鼠才算"干预"（`Esc` 例外）。"""
    with _lock:
        return bool(_state is not None and _state.armed)


def last_stop() -> dict:
    """最近一次"为什么退出接管"（进程级的史，与哪条会话无关）。

    模型侧要用它区分两种长得一样的状态：
    「用户从没启用」 vs 「刚刚因为用户干预被退出」。
    """
    with _lock:
        return dict(_last_stop)


def our_pids() -> set[int]:
    """启用时算好的"我们自己"（后端 + 桌面壳）。没启用，空集。"""
    with _lock:
        return set(_state.our_pids) if _state else set()


def is_enabled() -> bool:
    with _lock:
        return _state is not None


def enabled_for(session_id: str) -> bool:
    """这条会话自己是不是接管者 —— `tools=` 只给它的持有者发 `computer`。"""
    with _lock:
        return bool(_state is not None and session_id and _state.owner == session_id)


def status() -> dict:
    with _lock:
        if _state is None:
            return {"enabled": False, "owner": "", "started_at": 0,
                    "authorized": [], "stop_reason": "", "armed": False,
                    "last_stop": dict(_last_stop)}
        return {"enabled": True, "owner": _state.owner,
                "started_at": _state.started, "authorized": sorted(_state.authorized),
                "stop_reason": _state.stop_reason, "armed": bool(_state.armed),
                "armed_at": _state.armed_at, "last_stop": dict(_last_stop)}


def _require(session_id: str) -> Takeover:
    with _lock:
        if _state is None:
            raise TakeoverError("接管模式没有启用 —— 请让用户点击输入框右侧的「接管」按钮")
        if session_id and _state.owner != session_id:
            raise TakeoverError("接管由另一个对话持有 —— 这里不能操作")
        return _state


# 应用授权（逐应用 · 每会话一次）
def authorize(exe: str) -> None:
    exe = (exe or "").lower()
    if not exe:
        return
    with _lock:
        if _state is None:
            raise TakeoverError("接管模式没有启用")
        _state.authorized.add(exe)


def is_authorized(exe: str) -> bool:
    """这个应用现在能不能操作。

    两层：永久白名单（`data/allowlist.json` 的
    `computer_app` 格，设置页可加可删）或 本会话的临时授权。
    文件那条每轮现读（`core/allowlist.py` 判据 ①：文件即真相）——
      用户在设置页删掉一条，下一轮就该重新问。
    """
    exe = (exe or "").lower()
    if not exe:
        return False
    try:
        from core import allowlist as _al
        if _al.matches_any(_al.list_for("computer_app"), exe):
            return True
    except Exception as exc:                         # noqa: BLE001 - 读不到就按"没批准"
        print("[computer] 读永久应用白名单失败（按未批准处理）：%s: %s"
              % (type(exc).__name__, exc))
    with _lock:
        return bool(_state is not None and exe in _state.authorized)


def authorized() -> set[str]:
    with _lock:
        return set(_state.authorized) if _state else set()


# 窗口登记
def set_windows(infos: list[win32.WindowInfo]) -> dict[str, win32.WindowInfo]:
    """给这一批窗口发新的 id（`w12` 这种），返回 id → info。

    每次 `list_windows` 都发新 id，旧 id 作废（不是复用）——
      见模块 docstring 判据 3。
    """
    with _lock:
        st = _state
        if st is None:
            raise TakeoverError("接管模式没有启用")
        out: dict[str, win32.WindowInfo] = {}
        for info in infos:
            st.seq += 1
            wid = "w%d" % st.seq
            st.windows[wid] = info
            out[wid] = info
        # 只保留最近 200 个窗口登记（长会话不无限涨）
        if len(st.windows) > 200:
            for wid in list(st.windows)[:-200]:
                st.windows.pop(wid, None)
        return out


def get_window(window_id: str) -> win32.WindowInfo | None:
    with _lock:
        return _state.windows.get(window_id) if _state else None


# 观察快照
def record_observation(window_id: str, info: win32.WindowInfo,
                       elements: list, rect: tuple[int, int, int, int],
                       coord_rect: tuple[int, int, int, int] | None = None) -> str:
    with _lock:
        st = _state
        if st is None:
            raise TakeoverError("接管模式没有启用")
        st.seq += 1
        oid = "obs%d" % st.seq
        st.observations[oid] = Observation(
            obs_id=oid, window_id=window_id, hwnd=info.hwnd,
            exe=info.exe, rect=rect, elements=list(elements or []),
            created=time.time(), coord_rect=coord_rect or rect)
        st.latest_by_window[window_id] = oid
        # 每个窗口只留最近 5 次观察
        olds = [k for k, v in st.observations.items() if v.window_id == window_id]
        for k in olds[:-5]:
            st.observations.pop(k, None)
        return oid


def usable_observation(obs_id: str) -> Observation:
    """取一个还能用的观察：存在、没被消费、且是那个窗口最新的一次。"""
    with _lock:
        st = _state
        if st is None:
            raise TakeoverError("接管模式没有启用")
        obs = st.observations.get(obs_id)
        if obs is None:
            raise TakeoverError("观察快照不存在（或已过期）—— 重新 `get_window_state`")
        if obs.consumed:
            raise TakeoverError("这次观察已经用过了 —— 重新 `get_window_state` 再看一眼")
        if st.latest_by_window.get(obs.window_id) != obs_id:
            raise TakeoverError("这不是该窗口最新的一次观察 —— 重新 `get_window_state`")
        return obs


def consume(obs_id: str) -> None:
    with _lock:
        if _state and obs_id in _state.observations:
            _state.observations[obs_id].consumed = True


def note_click(window_id: str, x: int, y: int) -> None:
    """记下实际点到的物理点（下一张截图在它上面画红十字）。"""
    with _lock:
        if _state is not None:
            _state.last_click[window_id] = (int(x), int(y))


def take_last_click(window_id: str) -> tuple[int, int] | None:
    """取走那个点（只标一次，免得每张图都糊一个旧红十字）。"""
    with _lock:
        if _state is None:
            return None
        return _state.last_click.pop(window_id, None)


def reset_for_tests() -> None:
    """只给测试用（`purge` 不走它 —— `/clear` 有意不清）。

    连 `_last_stop` 一起清：否则上一条用例的"刚刚因键盘退出"会漏进下一条，
      让「从没启用」那条用例拿到一句"接管刚刚退出了"（两种状态又塌回一种）。
    """
    global _state
    with _lock:
        _state = None
        _last_stop.update({"owner": "", "reason": "", "at": 0.0})
