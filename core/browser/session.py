"""浏览器接管的会话状态。

一条连接、一个活动标签、一次观察一份 ref。与桌面接管共享同一个
`core.computer.session` 的持有者 / 授权 / 两态 `armed` / 急停 —— 浏览器不是
第二个"接管"，它只是同一次接管里的另一条通道。

兜底是硬要求：新工具不行就退化回来。
  这里所有连不上/动作失败的出口都带一句 `fallback_hint()`，指向已经验证过的
  桌面路径（`computer` + 截图 + 0–1000 比例）。别只回一个异常名。
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field

from core.computer import input as desk_input
from core.computer import session as desk_session

from . import discover
from .cdp import CDP, CDPError


class BrowserError(RuntimeError):
    """给模型看的一句话。"""


class NeedAppApproval(BrowserError):
    """这个浏览器 exe 还没被逐应用授权 —— 调用方转成 `computer_app` 挂起载荷。"""

    def __init__(self, exe: str, label: str):
        super().__init__("浏览器 %s 还没有被授权" % exe)
        self.exe = exe
        self.label = label


def fallback_hint() -> str:
    return ("可以直接**改用 `computer` 操作浏览器窗口**（桌面接管已验证："
            "截图 + 0–1000 比例 + 点击红十字 + 网格/放大）。"
            "`computer` 的 `list_windows` 里找 msedge/chrome 那个窗口即可。")


@dataclass
class Tab:
    tid: str
    target_id: str
    session_id: str = ""
    title: str = ""
    url: str = ""


@dataclass
class Ref:
    ref: str
    backend_id: int
    role: str
    name: str
    x: float = 0.0          # CSS 像素（点击用）
    y: float = 0.0


@dataclass
class Snapshot:
    obs_id: str
    tid: str
    refs: dict[str, Ref] = field(default_factory=dict)
    text: str = ""
    viewport: tuple[int, int] = (0, 0)
    created: float = 0.0
    consumed: bool = False


@dataclass
class State:
    owner: str
    product: str
    exe: str
    label: str
    cdp: CDP
    tabs: dict[str, Tab] = field(default_factory=dict)
    active: str = ""
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    seq: int = 0
    last_click: tuple[float, float] | None = None
    version: str = ""


_state: State | None = None
_lock = threading.RLock()
_listener_done = False


# 生命周期
def _ensure_listener() -> None:
    global _listener_done
    if _listener_done:
        return
    _listener_done = True
    try:
        desk_session.subscribe(lambda _owner, reason: close(
            "接管关闭（%s）" % (reason or "未知")))
    except Exception as exc:                         # noqa: BLE001
        print("[browser] 订阅接管关闭失败：%s: %s" % (type(exc).__name__, exc))


def connect(owner: str) -> State:
    """连上用户的浏览器（需要先勾选审批式开关）。失败必带兜底路线。"""
    global _state
    with _lock:
        if _state is not None:
            if _state.owner == owner:
                return _state
            raise BrowserError("另一个对话正在控制浏览器 —— 先在那里退出。\n"
                               + fallback_hint())
        if not desk_session.enabled_for(owner):
            raise BrowserError("接管模式没有启用（或不是这个对话持有的）。\n"
                               + fallback_hint())
        cands = discover.candidates()
        if not cands:
            raise BrowserError(discover.setup_hint())
        last = ""
        for cand in cands:
            if not desk_session.is_authorized(cand.exe):
                raise NeedAppApproval(cand.exe, cand.label)
            cdp = CDP()
            try:
                ver = cdp.connect(cand.ws_url, timeout=45)
            except CDPError as exc:
                last = str(exc)
                cdp.close()
                continue
            st = State(owner=owner, product=cand.product, exe=cand.exe,
                       label=cand.label, cdp=cdp,
                       version=str(ver.get("product") or ""))
            _state = st
            _ensure_listener()
            _refresh_tabs(st)
            print("[browser] 已连接 %s %s" % (cand.label, st.version))
            return st
        raise BrowserError(
            "连不上浏览器调试端口（%s）。\n%s\n%s"
            % (last or "端口拒绝连接", discover.setup_hint(), fallback_hint()))


def close(reason: str = "") -> None:
    global _state
    with _lock:
        if _state is None:
            return
        cdp = _state.cdp
        _state = None
    try:
        cdp.close()
    except Exception:                                # noqa: BLE001
        pass
    if reason:
        print("[browser] 连接已关闭：%s" % reason)


def enabled_for(owner: str) -> bool:
    with _lock:
        return bool(_state is not None and _state.owner == owner)


def status() -> dict:
    with _lock:
        if _state is None:
            return {"enabled": False}
        return {"enabled": True, "owner": _state.owner,
                "product": _state.product, "exe": _state.exe,
                "version": _state.version,
                "tabs": len(_state.tabs), "active": _state.active}


# 标签
def _refresh_tabs(st: State) -> None:
    res = st.cdp.send("Target.getTargets")
    pages = [t for t in (res.get("targetInfos") or [])
             if t.get("type") == "page"
             and not str(t.get("url") or "").startswith("devtools://")]
    by_target = {t.target_id: t for t in st.tabs.values()}
    tabs: dict[str, Tab] = {}
    for info in pages:
        tid = info.get("targetId") or ""
        if not tid:
            continue
        old = by_target.get(tid)
        if old is None:
            tid_name = "t%d" % (len(tabs) + 1)
            while tid_name in st.tabs or tid_name in tabs:
                tid_name = "t%d" % (int(tid_name[1:]) + 1)
            old = Tab(tid=tid_name, target_id=tid)
        old.title = str(info.get("title") or "")[:120]
        old.url = str(info.get("url") or "")[:300]
        tabs[old.tid] = old
    st.tabs = tabs
    if st.active not in tabs:
        st.active = next(iter(tabs), "")


def list_tabs(owner: str) -> list[Tab]:
    st = connect(owner)
    with _lock:
        _refresh_tabs(st)
        return list(st.tabs.values())


def open_tab(owner: str, url: str) -> Tab:
    st = connect(owner)
    _check_url(url)
    with _lock:
        res = st.cdp.send("Target.createTarget", {"url": url})
        target_id = res.get("targetId") or ""
        if not target_id:
            raise BrowserError("浏览器没有返回新标签页 id。\n" + fallback_hint())
        _refresh_tabs(st)
        tab = next((t for t in st.tabs.values() if t.target_id == target_id), None)
        if tab is None:
            raise BrowserError("新标签页没出现在列表里。\n" + fallback_hint())
        st.active = tab.tid
        _attach(st, tab)
        return tab


def switch_tab(owner: str, tid: str) -> Tab:
    st = connect(owner)
    with _lock:
        _refresh_tabs(st)
        tab = st.tabs.get(tid)
        if tab is None:
            raise BrowserError("没有标签页 %s（用 list_tabs 看当前的）。" % tid)
        st.active = tid
        _attach(st, tab)
        return tab


def _attach(st: State, tab: Tab) -> None:
    if not tab.session_id:
        res = st.cdp.send("Target.attachToTarget",
                          {"targetId": tab.target_id, "flatten": True})
        tab.session_id = res.get("sessionId") or ""
        if not tab.session_id:
            raise BrowserError("attach 失败：浏览器没给 sessionId。\n" + fallback_hint())
    try:
        st.cdp.send("Page.enable", session_id=tab.session_id)
    except CDPError:
        pass
    try:
        st.cdp.send("Accessibility.enable", session_id=tab.session_id)
    except CDPError:
        pass
    try:
        st.cdp.send("Target.activateTarget", {"targetId": tab.target_id})
    except CDPError:
        pass


def active_tab(st: State) -> Tab:
    _refresh_tabs(st)
    tab = st.tabs.get(st.active)
    if tab is None:
        raise BrowserError("没有可操作的标签页 —— `open_tab` 开一个。")
    _attach(st, tab)
    return tab


# 导航 / 等待
_ALLOWED_SCHEMES = ("http://", "https://", "about:blank")


def _check_url(url: str) -> None:
    u = str(url or "").strip()
    if not u:
        raise BrowserError("url 是空的")
    if u == "about:blank" or u.lower().startswith(_ALLOWED_SCHEMES):
        return
    raise BrowserError("browser 只允许 http(s) / about:blank（拒绝 %r）。"
                       "本地文件/协议链接请用 `computer` 或 `run_command`。" % u[:40])


def navigate(owner: str, url: str) -> tuple[Tab, bool]:
    st = connect(owner)
    _check_url(url)
    with _lock:
        tab = active_tab(st)
        st.cdp.send("Page.navigate", {"url": url}, session_id=tab.session_id)
        ready = _wait_ready(st, tab, timeout=12.0)
        _refresh_tabs(st)
        return tab, ready


def _wait_ready(st: State, tab: Tab, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res = st.cdp.send("Runtime.evaluate",
                              {"expression": "document.readyState",
                               "returnByValue": True},
                              session_id=tab.session_id, timeout=4)
            val = (res.get("result") or {}).get("value")
            if val == "complete":
                return True
        except CDPError:
            pass
        time.sleep(0.25)
    return False


def wait(owner: str, seconds: float = 1.0) -> bool:
    st = connect(owner)
    with _lock:
        tab = active_tab(st)
        time.sleep(max(0.0, min(10.0, float(seconds or 0))))
        return _wait_ready(st, tab, timeout=8.0)


# 快照
_INTERACTIVE = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio",
    "combobox", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio",
    "option", "tab", "switch", "slider", "spinbutton", "treeitem",
})


def snapshot(owner: str, *, max_refs: int = 80, max_chars: int = 12000) -> Snapshot:
    st = connect(owner)
    with _lock:
        tab = active_tab(st)
        metrics = st.cdp.send("Page.getLayoutMetrics", session_id=tab.session_id)
        vp = metrics.get("cssLayoutViewport") or {}
        vw = int(vp.get("clientWidth") or 0)
        vh = int(vp.get("clientHeight") or 0)
        tree = st.cdp.send("Accessibility.getFullAXTree", {},
                           session_id=tab.session_id, timeout=20)
        nodes = tree.get("nodes") or []
        st.seq += 1
        obs = Snapshot(obs_id="b%d" % st.seq, tid=tab.tid,
                       viewport=(vw, vh), created=time.time())
        lines = []
        used = 0
        for node in nodes:
            if node.get("ignored"):
                continue
            role = ((node.get("role") or {}).get("value")) or ""
            name = ((node.get("name") or {}).get("value")) or ""
            value = ((node.get("value") or {}).get("value")) or ""
            role_l = str(role).lower()
            line = "%s %s" % (role or "?", str(name).replace("\n", " ")[:120])
            if value:
                line += " = %r" % str(value)[:80]
            ref = ""
            backend = node.get("backendDOMNodeId") or 0
            if role_l in _INTERACTIVE and backend and len(obs.refs) < max_refs:
                st.seq += 1
                ref = "e%d" % st.seq
                x, y = _center_of(st, tab, int(backend))
                if x is not None:
                    obs.refs[ref] = Ref(ref=ref, backend_id=int(backend),
                                        role=role_l, name=str(name)[:120],
                                        x=x, y=y)
                    line += " [%s]" % ref
                else:
                    line += " [跨域 iframe / 无布局，暂不能按 ref 点]"
            lines.append(line)
            used += len(line) + 1
            if used >= max_chars:
                lines.append("…（快照截断）")
                break
        obs.text = "\n".join(lines)
        st.snapshots[obs.obs_id] = obs
        olds = list(st.snapshots.keys())
        for old in olds[:-5]:
            st.snapshots.pop(old, None)
        return obs


def usable(owner: str, obs_id: str) -> tuple[State, Snapshot]:
    st = connect(owner)
    with _lock:
        snap = st.snapshots.get(obs_id)
        if snap is None:
            raise BrowserError("观察快照不存在（或已过期）—— 重新 `snapshot`")
        if snap.consumed:
            raise BrowserError("这次观察已经用过了 —— 重新 `snapshot` 再看一眼")
        if snap.tid != st.active:
            raise BrowserError("这个观察是另一个标签页的 —— 先 switch_tab 回去，"
                               "或重新 snapshot")
        return st, snap


def consume(obs: Snapshot) -> None:
    with _lock:
        obs.consumed = True


def _center_of(st: State, tab: Tab, backend_id: int):
    try:
        res = st.cdp.send("DOM.getBoxModel", {"backendNodeId": backend_id},
                          session_id=tab.session_id, timeout=8)
        quad = ((res.get("model") or {}).get("border") or
                (res.get("model") or {}).get("content") or [])
        if len(quad) < 8:
            return None, None
        xs = quad[0::2]
        ys = quad[1::2]
        return (sum(xs) / 4.0, sum(ys) / 4.0)
    except CDPError:
        return None, None


# 动作
def _click_at(st: State, tab: Tab, cx: float, cy: float, *,
              button: str = "left", count: int = 1) -> None:
    for i in range(max(1, count)):
        st.cdp.send("Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": cx, "y": cy},
                    session_id=tab.session_id)
        st.cdp.send("Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": cx, "y": cy,
                     "button": button, "clickCount": i + 1},
                    session_id=tab.session_id)
        st.cdp.send("Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": cx, "y": cy,
                     "button": button, "clickCount": i + 1},
                    session_id=tab.session_id)


def click(owner: str, obs_id: str, *, ref: str = "", x=None, y=None,
          button: str = "left", count: int = 1) -> str:
    st, snap = usable(owner, obs_id)
    with _lock:
        tab = active_tab(st)
        if ref:
            r = snap.refs.get(ref)
            if r is None:
                raise BrowserError("没有 ref %s（快照里的 ref 只对那一次有效）" % ref)
            cx, cy = _center_of(st, tab, r.backend_id)
            if cx is None:
                raise BrowserError(
                    "这个元素拿不到位置（可能在跨域 iframe 里）—— 用 `snapshot` "
                    "看别的元素，或改用 `computer` 操作窗口。\n" + fallback_hint())
            what = "元素 %s「%s」" % (ref, r.name[:40])
        elif x is not None and y is not None:
            try:
                nx, ny = int(x), int(y)
            except (TypeError, ValueError):
                raise BrowserError("x/y 要给 0–1000 的整数比例")
            if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
                raise BrowserError("x/y 是 0–1000 比例（不是像素）")
            cx = nx / 1000.0 * max(1, snap.viewport[0])
            cy = ny / 1000.0 * max(1, snap.viewport[1])
            what = "比例 (%d,%d)" % (nx, ny)
        else:
            raise BrowserError("click 要给 ref，或者 x + y（0–1000 比例）")
        n = max(1, min(3, int(count or 1)))
        b = (button or "left").lower()
        _click_at(st, tab, cx, cy, button=b, count=n)
        st.last_click = (cx, cy)
        consume(snap)
        print("[browser] click %s -> css (%.0f,%.0f)" % (what, cx, cy))
        return "已点击 %s。位置可能已经变了 —— **重新 `snapshot`** 看结果。" % what


def type_text(owner: str, obs_id: str, *, ref: str = "", text: str = "") -> str:
    if not str(text or ""):
        raise BrowserError("text 是空的")
    st, snap = usable(owner, obs_id)
    with _lock:
        tab = active_tab(st)
        if ref:
            r = snap.refs.get(ref)
            if r is None:
                raise BrowserError("没有 ref %s（快照里的 ref 只对那一次有效）" % ref)
            try:
                st.cdp.send("DOM.focus", {"backendNodeId": r.backend_id},
                            session_id=tab.session_id)
            except CDPError:
                # 有些自绘控件不响应 DOM.focus —— 点一下它的中心再输入。
                cx, cy = _center_of(st, tab, r.backend_id)
                if cx is None:
                    raise BrowserError("这个元素既 focus 不了也拿不到位置。\n"
                                       + fallback_hint())
                _click_at(st, tab, cx, cy)
        st.cdp.send("Input.insertText", {"text": str(text)},
                    session_id=tab.session_id, timeout=25)
        consume(snap)
        print("[browser] type_text %d chars (ref=%s)" % (len(str(text)), ref or "-"))
        return ("已输入 %d 个字符。**重新 `snapshot`** 确认它真的进去了"
                "（别假设；有些编辑器会改写/自动缩进）。" % len(str(text)))


#: CDP 的 `Input.dispatchKeyEvent` 要 key/code/vk 三样；这里只覆盖常用键。
_KEYS = {
    "enter": ("Enter", "Enter", 13), "return": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9), "esc": ("Escape", "Escape", 27),
    "escape": ("Escape", "Escape", 27),
    "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46),
    "space": (" ", "Space", 32),
    "left": ("ArrowLeft", "ArrowLeft", 37), "right": ("ArrowRight", "ArrowRight", 39),
    "up": ("ArrowUp", "ArrowUp", 38), "down": ("ArrowDown", "ArrowDown", 40),
    "home": ("Home", "Home", 36), "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33), "pagedown": ("PageDown", "PageDown", 34),
}
_MODS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "win": 4,
         "shift": 8}


def press_key(owner: str, obs_id: str, key: str) -> str:
    st, snap = usable(owner, obs_id)
    try:
        tokens = desk_input.parse_combo(key)
    except Exception as exc:                         # noqa: BLE001
        raise BrowserError("按键格式不对：%s" % (getattr(exc, "brief", "") or exc))
    if not tokens:
        raise BrowserError("组合键是空的")
    mods = 0
    main = None
    for t in tokens:
        if t in _MODS:
            mods |= _MODS[t]
        else:
            main = t
    if main is None:
        raise BrowserError("这个组合键里没有主键：%s" % key)
    if main in _KEYS:
        k, code, vk = _KEYS[main]
    elif len(main) == 1:
        k = main
        if main.isalpha():
            code, vk = "Key" + main.upper(), ord(main.upper())
        elif main.isdigit():
            code, vk = "Digit" + main, ord(main)
        else:
            code, vk = "", 0
    else:
        raise BrowserError("不认识的键：%s" % main)
    text = k if (len(k) == 1 and not mods) else ""
    macmd = {"type": "keyDown", "key": k, "code": code,
             "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
             "modifiers": mods}
    if text:
        macmd["text"] = text
    with _lock:
        tab = active_tab(st)
        st.cdp.send("Input.dispatchKeyEvent", macmd, session_id=tab.session_id)
        up = dict(macmd)
        up["type"] = "keyUp"
        up.pop("text", None)
        st.cdp.send("Input.dispatchKeyEvent", up, session_id=tab.session_id)
        consume(snap)
    print("[browser] press_key %s" % key)
    return "已按下 %s。**重新 `snapshot`** 看结果。" % key


def scroll(owner: str, obs_id: str, *, x=None, y=None,
           dx: int = 0, dy: int = 0) -> str:
    st, snap = usable(owner, obs_id)
    with _lock:
        tab = active_tab(st)
        vw, vh = max(1, snap.viewport[0]), max(1, snap.viewport[1])
        cx = (int(x) / 1000.0 * vw) if x is not None else vw / 2.0
        cy = (int(y) / 1000.0 * vh) if y is not None else vh / 2.0
        st.cdp.send("Input.dispatchMouseEvent",
                    {"type": "mouseWheel", "x": cx, "y": cy,
                     "deltaX": int(dx or 0) * 120, "deltaY": int(dy or 0) * 120},
                    session_id=tab.session_id)
        consume(snap)
    print("[browser] scroll at (%.0f,%.0f) dy=%s dx=%s" % (cx, cy, dy, dx))
    return "已滚动（纵向 %s，横向 %s）。**重新 `snapshot`** 看结果。" % (dy, dx)


def screenshot(owner: str) -> tuple[bytes, tuple[int, int]]:
    """返回 `(png, css_viewport)`。"""
    st = connect(owner)
    with _lock:
        tab = active_tab(st)
        metrics = st.cdp.send("Page.getLayoutMetrics", session_id=tab.session_id)
        vp = metrics.get("cssLayoutViewport") or {}
        css = (int(vp.get("clientWidth") or 0), int(vp.get("clientHeight") or 0))
        res = st.cdp.send("Page.captureScreenshot",
                          {"format": "png", "fromSurface": True},
                          session_id=tab.session_id, timeout=25)
        data = res.get("data") or ""
        if not data:
            raise BrowserError("截图失败：浏览器没返回数据。\n" + fallback_hint())
        png = base64.b64decode(data)
        return png, css


def last_click(owner: str):
    with _lock:
        return _state.last_click if _state and _state.owner == owner else None
