"""最小的 CDP 客户端 —— 零新增依赖。

`aiohttp` 自带 WebSocket 客户端（本仓库已有依赖）。后台线程跑一条 event loop，
同步侧用 `send()` 发命令 —— `computer` 的 handler 是同步的，不该为了浏览器
把它改成 async。

为什么不是 Playwright / Selenium：它们要新依赖 + 自带浏览器；
   而 Edge 145+ 的审批式调试端口已经把"接真实登录态"这件事做掉了。
   这里只做一件事：一条持久 WS（审批是每条连接一次，断线会再弹）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time


class CDPError(RuntimeError):
    """给模型/用户看的一句话（带路线）。"""


#: 浏览器「允许远程调试?」窗口的标题标记（中/英）。
#: 只用来把窗口切到前台，绝不点按钮 —— 那一下必须是人点的。
_CONSENT_MARKERS = ("允许远程调试", "allow remote debugging")


def is_consent_title(title: str) -> bool:
    """这个窗口标题是不是浏览器的远程调试同意框（纯函数，测试直接打）。"""
    t = str(title or "").lower()
    return any(m in t for m in _CONSENT_MARKERS)


class CDP:
    """一条浏览器级 WebSocket；`sessionId` 用来打到某个标签页。"""

    def __init__(self):
        self._loop = None
        self._thread = None
        self._ws = None
        self._pending: dict[int, asyncio.Future] = {}
        self._id = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._fail: BaseException | None = None
        self._closing = False
        self.url = ""

    # 生命周期
    def connect(self, url: str, timeout: float = 45.0) -> dict:
        """连上并握手。**Edge 会弹「Allow remote debugging?」—— 等用户点允许。**

        `timeout` 要留够人点按钮的时间（默认 45 秒）；点拒绝/超时都变成
        一句能照着做的错，而不是一个 traceback。
        """
        self.url = str(url or "")
        if not self.url.startswith(("ws://", "wss://")):
            raise CDPError("CDP 地址不是 WebSocket：%r" % self.url[:80])
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name="browser-cdp")
        self._thread.start()
        # 弹窗常常被聊天窗口挡在后面 —— 自动把它切到前台（不点）。
        stop_focus = threading.Event()
        threading.Thread(target=self._focus_consent, args=(stop_focus,),
                         daemon=True, name="browser-consent").start()
        try:
            ready = self._ready.wait(timeout)
        finally:
            stop_focus.set()
        if not ready:
            raise CDPError("连接超时（%d 秒）—— 浏览器会弹「Allow remote "
                           "debugging?」：**先把 Edge/Chrome 窗口切到前台**"
                           "（弹窗可能被挡在后面）再点「允许」；没有弹就重新勾选 "
                           "edge://inspect/#remote-debugging。\n"
                           "⚠️ **不要连续重试**；不想处理这个弹窗就直接用 "
                           "`computer` 操作浏览器窗口。"
                           % int(timeout))
        if self._fail is not None:
            raise CDPError("连不上浏览器调试端口：%s" % self._fail)
        result = self.send("Browser.getVersion", timeout=15)
        return result

    def _focus_consent(self, stop: threading.Event) -> None:
        """在等待用户点「允许」期间，把那个弹窗切到前台（只切，不点）。

        判据是标题标记 + 浏览器进程；找不到就安静地退。失败一律吞掉 ——
        聚焦只是帮助，不该把连接弄坏。
        """
        try:
            from core.computer import win32
        except Exception:                            # noqa: BLE001
            return
        while not stop.is_set():
            try:
                for w in win32.enum_windows():
                    if (w.exe or "").lower() not in ("msedge.exe", "chrome.exe"):
                        continue
                    if not is_consent_title(w.title):
                        continue
                    if not win32.is_foreground(w.hwnd):
                        ok, _why = win32.activate(w.hwnd)
                        if ok:
                            print("[browser] 把「允许远程调试」弹窗切到前台（等你点）")
                    break
            except Exception:                        # noqa: BLE001
                pass
            stop.wait(1.5)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:                 # noqa: BLE001
            self._fail = exc
        finally:
            self._ready.set()

    async def _run(self) -> None:
        import aiohttp
        self._loop = asyncio.get_running_loop()
        try:
            async with aiohttp.ClientSession() as session:
                self._ws = await session.ws_connect(
                    self.url, timeout=45, heartbeat=30)
                self._ready.set()
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._dispatch(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.ERROR):
                        break
        except BaseException as exc:                 # noqa: BLE001
            self._fail = exc
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(CDPError("浏览器连接已断开"))
            self._pending.clear()

    def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:                            # noqa: BLE001
            return
        mid = data.get("id")
        if mid is None:
            return                                  # 事件（Page.loadEventFired…）
        fut = self._pending.pop(mid, None)
        if fut is not None and not fut.done():
            fut.set_result(data)

    # 同步发送
    def send(self, method: str, params: dict | None = None, *,
             session_id: str | None = None, timeout: float = 15.0) -> dict:
        if self._fail is not None:
            raise CDPError("浏览器连接已断开：%s" % self._fail)
        if self._loop is None or self._ws is None:
            raise CDPError("浏览器还没连上")
        fut = asyncio.run_coroutine_threadsafe(
            self._rpc(method, params or {}, session_id, timeout), self._loop)
        try:
            data = fut.result(timeout + 5)
        except TimeoutError:
            raise CDPError("CDP 命令 %s 超时（%d 秒）" % (method, int(timeout)))
        if "error" in data:
            err = data.get("error") or {}
            raise CDPError("%s 失败：%s" % (method, err.get("message") or err))
        return data.get("result") or {}

    async def _rpc(self, method, params, session_id, timeout):
        with self._lock:
            self._id += 1
            mid = self._id
        fut = self._loop.create_future()
        self._pending[mid] = fut
        msg = {"id": mid, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        await self._ws.send_json(msg)
        return await asyncio.wait_for(fut, timeout)

    def close(self) -> None:
        if self._loop is None:
            return
        async def _close():
            try:
                if self._ws is not None:
                    await self._ws.close()
            except Exception:                        # noqa: BLE001
                pass
        try:
            asyncio.run_coroutine_threadsafe(_close(), self._loop).result(5)
        except Exception:                            # noqa: BLE001
            pass


def now() -> float:
    return time.time()
