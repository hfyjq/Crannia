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
    2026-11-27 订正：同一个 `(hwnd, pid)` 复用原 id —— "永不重用"
   指的是"一个 id 不给两个窗口"，同一个窗口重新列举不换 id。真机代价：
   PS 主窗从 `w6` 变成 `w279`，模型手里的 `w6` 直接报"不认识的窗口 id"，
   白跑两轮。（原实现每次 `list_windows` 全发新 id + 200 条上限按插入
   顺序清理，常用窗口最先被清。）

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
    #: 弹出菜单的元素 → 菜单命令 id（`win32.menu_items` 配上的那些）。
    #: 有它 → 这次观察的菜单项坐标取自菜单自己的表，不是 UIA 的估计。
    #: 点击时按 id 重取一次当下的矩形 —— 菜单滚动/换层都跟得上。
    menu_ids: dict[int, int] = field(default_factory=dict)
    #: 窗口相对观察那一刻的位移（像素）。窗口被拖动时，比例坐标仍然
    #: 相对"你看到的那张图"，所以换算时把这段位移补上 —— 不必因为
    #: 窗口挪了一下就让观察作废。由 `tools._ensure_same_rect` 填。
    drift: tuple[int, int] = (0, 0)


@dataclass
class Takeover:
    owner: str
    started: float = field(default_factory=time.time)
    authorized: set[str] = field(default_factory=set)      # exe 小写 basename
    windows: dict[str, win32.WindowInfo] = field(default_factory=dict)
    observations: dict[str, Observation] = field(default_factory=dict)
    latest_by_window: dict[str, str] = field(default_factory=dict)
    #: 每个窗口最近一次点击的实际物理点（下一张截图用来画红十字；取一次就清）。
    last_click: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: 每个窗口上一次点击的时刻（`time.monotonic`）—— 算"距上次点击多少 ms"。
    #: 两次单点落在同一个点上、间隔小于系统双击阈值时，会被合成一次双击；
    #: 回执把间隔报出来，模型才知道自己是不是在"点两下"。
    last_click_at: dict[str, float] = field(default_factory=dict)
    #: 上次 `cursor_position` 报数那一刻的光标物理点（只留给下一张图标一次）。
    #: 与 `last_click` 分开存：一个是"点落在哪"，一个是"我读到它在哪"。
    last_cursor: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: 最近一次点击"实际落点 − 请求落点"的差值（取一次就清）。
    #: 存在它是为了把"点偏了多少"变成可读的反馈，而不是让模型自己猜图有没有对。
    click_miss: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: 下一次观察要自动开网格的窗口（点偏超阈值时置，取一次就清）。
    grid_hint: set[str] = field(default_factory=set)
    #: 最近一次观察 / 激活的窗口 —— `get_window_state` / `cursor_position`
    #: 不给 window 时沿用。它只是默认值，回执里会写明"沿用 wX"。
    last_window: str = ""
    #: 每个窗口上一次观察的原始截图指纹（sha256 前 12 位）。
    #: 截图按内容命名附件，所以"同一张图"既可能是界面没变、也可能是
    #: 目标程序没重绘；指纹把这件事变成回执里的事实，让模型有依据判断。
    last_frame: dict[str, str] = field(default_factory=dict)
    #: 每个窗口上一次观察的元素树摘要（类型 / id / 名字 / 矩形）。
    #: 与 `last_frame` 配对使用：图与树各自独立，两个都相同才说明界面
    #: 真的没动；只有图相同而树变了，说明拿到的是旧帧。
    last_tree: dict[str, str] = field(default_factory=dict)
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
        #: 这条会出现在按钮旁的提示里 —— 跟着界面语言走，与别的消息同一处取。
        from core import i18n
        return False, i18n.t("接管模式只支持 Windows 桌面（当前系统 %s）") % sys.platform
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
        print("[computer] failed to detach emergency-stop hook: %s: %s" % (type(exc).__name__, exc))
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
            print("[computer] operation detection entered (model starting actions; physical input will exit takeover)")


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
            print("[computer] operation detection ended (%s)" % (reason or "turn_end"))


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
        print("[computer] reading permanent app allowlist failed (treating as not approved): %s: %s"
              % (type(exc).__name__, exc))
    with _lock:
        return bool(_state is not None and exe in _state.authorized)


def authorized() -> set[str]:
    with _lock:
        return set(_state.authorized) if _state else set()


# 窗口登记
def set_windows(infos: list[win32.WindowInfo]) -> dict[str, win32.WindowInfo]:
    """给这一批窗口发 id（`w12` 这种），返回 id → info。

    同一个 `(hwnd, pid)` 复用原来的 id —— 模型手里的 id 不该因为
      "又列了一次窗口"就作废（真机：PS 主窗从 w6 变 w279，w6 直接报错）。
      判据 3 是"一个 id 不给两个窗口"，同一个窗口复用不违反它；
      hwnd 被系统回收给新窗口时 pid 会变 → 发新 id。
    清理按"最久没用"（这一批出现的移到末尾），不再按"最早登记"。
    """
    with _lock:
        st = _state
        if st is None:
            raise TakeoverError("接管模式没有启用")
        by_key = {(info.hwnd, info.pid): wid
                  for wid, info in st.windows.items()}
        out: dict[str, win32.WindowInfo] = {}
        for info in infos:
            key = (info.hwnd, info.pid)
            wid = by_key.get(key)
            if wid is None:
                st.seq += 1
                wid = "w%d" % st.seq
                by_key[key] = wid
            # 移到最后 = 最近使用；200 条上限按这个顺序清最老的。
            if wid in st.windows:
                st.windows.pop(wid)
            st.windows[wid] = info
            out[wid] = info
        if len(st.windows) > 200:
            for wid in list(st.windows)[:-200]:
                st.windows.pop(wid, None)
        return out


def get_window(window_id: str) -> win32.WindowInfo | None:
    with _lock:
        return _state.windows.get(window_id) if _state else None


def window_id_for(hwnd: int) -> str:
    """某个顶层窗口已登记的 id（没有就空串）。"""
    with _lock:
        if _state is None:
            return ""
        for wid, info in _state.windows.items():
            if int(info.hwnd) == int(hwnd):
                return wid
        return ""


def last_window() -> str:
    """最近一次观察 / 激活的窗口 id（没有就空串）。"""
    with _lock:
        return _state.last_window if _state else ""


def set_last_window(window_id: str) -> None:
    with _lock:
        if _state is not None and window_id:
            _state.last_window = window_id


# 观察快照
def record_observation(window_id: str, info: win32.WindowInfo,
                       elements: list, rect: tuple[int, int, int, int],
                       coord_rect: tuple[int, int, int, int] | None = None,
                       menu_ids: dict[int, int] | None = None) -> str:
    with _lock:
        st = _state
        if st is None:
            raise TakeoverError("接管模式没有启用")
        st.seq += 1
        oid = "obs%d" % st.seq
        st.observations[oid] = Observation(
            obs_id=oid, window_id=window_id, hwnd=info.hwnd,
            exe=info.exe, rect=rect, elements=list(elements or []),
            created=time.time(), coord_rect=coord_rect or rect,
            menu_ids=dict(menu_ids or {}))
        st.latest_by_window[window_id] = oid
        st.last_window = window_id
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
            raise TakeoverError("[STALE_OBSERVATION] 观察快照不存在（或已过期）—— "
                                "重新 `get_window_state`")
        if obs.consumed:
            raise TakeoverError("[STALE_OBSERVATION] 这次观察已经用过了 —— "
                                "重新 `get_window_state` 再看一眼")
        if st.latest_by_window.get(obs.window_id) != obs_id:
            raise TakeoverError("[STALE_OBSERVATION] 这不是该窗口最新的一次观察 —— "
                                "重新 `get_window_state`")
        return obs


def consume(obs_id: str) -> None:
    with _lock:
        if _state and obs_id in _state.observations:
            _state.observations[obs_id].consumed = True


def note_frame(window_id: str, digest: str) -> str | None:
    """记下这一帧的指纹，返回该窗口上一帧的指纹（第一次为 `None`）。

    `digest` 取的是原始窗口截图的哈希，与观察参数（网格 / 放大 /
    标记）无关 —— 它回答的是"窗口内容有没有变"，不是"这张图长什么样"。
    """
    with _lock:
        st = _state
        if st is None:
            return None
        prev = st.last_frame.get(window_id)
        st.last_frame[window_id] = digest
        return prev


def note_tree(window_id: str, digest: str) -> str | None:
    """记下这一份元素树的摘要，返回上一份（第一次为 `None`）。"""
    with _lock:
        st = _state
        if st is None:
            return None
        prev = st.last_tree.get(window_id)
        st.last_tree[window_id] = digest
        return prev


def note_click(window_id: str, x: int, y: int, *,
               requested: tuple[int, int] | None = None,
               miss_limit: int = 0) -> float | None:
    """记下实际点到的物理点，并顺手算出它偏离请求点多少。

    `requested` 是"我们让它去的点"，`x`/`y` 是回读到的实际点。两者不一样时，
    偏了多少必须留给下一张图/下一句回执 —— 只画一个红十字而不说差值，
    模型得自己从图上量，那又变成一次目测。

    `miss_limit` 是"算不算偏"的阈值（调用方按窗口尺寸算）。超过就置一次
    网格提示：点偏之后最缺的是位置依据，网格正好补它。

    返回距上一次点击的秒数（没有上一次则 None）—— 两次单点贴得太近
    会被系统合成双击，回执要能说清这件事。
    """
    with _lock:
        if _state is None:
            return None
        now = time.monotonic()
        prev = _state.last_click_at.get(window_id)
        _state.last_click_at[window_id] = now
        gap = (now - prev) if prev is not None else None
        _state.last_click[window_id] = (int(x), int(y))
        if requested is not None:
            dx = int(x) - int(requested[0])
            dy = int(y) - int(requested[1])
            _state.click_miss[window_id] = (dx, dy)
            if miss_limit > 0 and max(abs(dx), abs(dy)) > miss_limit:
                _state.grid_hint.add(window_id)
        return gap


def take_last_click(window_id: str) -> tuple[int, int] | None:
    """取走那个点（只标一次，免得每张图都糊一个旧红十字）。"""
    with _lock:
        if _state is None:
            return None
        return _state.last_click.pop(window_id, None)


def note_cursor(window_id: str, x: int, y: int) -> None:
    """记下 `cursor_position` 报数那一刻的光标物理点，留给下一张图标出来。

    为什么不在读数当下就截图：`cursor_position` 是一条轻量读数（模型会连问
    几次确认），每次都 PrintWindow 太贵。留给下一张 `get_window_state` 标 ——
    但回执必须说清"这是读数那一刻的位置"，中间的移动归用户。
    """
    with _lock:
        if _state is None:
            return
        _state.last_cursor[window_id] = (int(x), int(y))


def take_cursor(window_id: str) -> tuple[int, int] | None:
    """取走那个点（只标一次，同 `take_last_click`）。"""
    with _lock:
        if _state is None:
            return None
        return _state.last_cursor.pop(window_id, None)


def take_click_miss(window_id: str) -> tuple[int, int] | None:
    """取走"上次点偏了多少"（同上，只报一次）。"""
    with _lock:
        if _state is None:
            return None
        return _state.click_miss.pop(window_id, None)


def take_grid_hint(window_id: str) -> bool:
    """上一次点偏了没有（取了就清，只影响紧接的那一张图）。"""
    with _lock:
        if _state is None:
            return False
        if window_id in _state.grid_hint:
            _state.grid_hint.discard(window_id)
            return True
        return False


def enable_for_tests(owner: str = "test") -> "Takeover":
    """测试专用：跳过平台闸装一个接管态，不装系统钩子。

    真机上的平台闸在 `enable()` 里（非 Windows 明确拒绝）；但这组测试注入的
    全是假窗口 / 假输入，验的是落点回读、编号、盲模型降级那套纯逻辑 ——
    在 Linux CI 上跳过它们等于放弃一半判据。
    """
    global _state
    with _lock:
        _state = Takeover(owner=owner)
        return _state


def reset_for_tests() -> None:
    """只给测试用（`purge` 不走它 —— `/clear` 有意不清）。

    连 `_last_stop` 一起清：否则上一条用例的"刚刚因键盘退出"会漏进下一条，
      让「从没启用」那条用例拿到一句"接管刚刚退出了"（两种状态又塌回一种）。
    """
    global _state
    with _lock:
        _state = None
        _last_stop.update({"owner": "", "reason": "", "at": 0.0})
