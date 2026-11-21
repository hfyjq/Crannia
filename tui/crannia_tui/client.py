"""事件流客户端。

为什么用线程而不是 asyncio：crannia 的管线是同步的（`provider.chat()` 阻塞），
TUI 也保持同步模型。两套并发模型混在一起时，出问题的地方永远在交界处，
而这里唯一的并发需求就是"一边等输入、一边收事件"——一个线程足够。

断线要自己重连：浏览器的 `EventSource` 自带这条（断了 3 秒一次地试），
手写的 `urllib` 没有这份运气 —— 不重连的话，界面就是"卡死"本身：
横幅继续转、卡片停在 `running`，而实际上一条事件都收不到了。
"""

from __future__ import annotations

import threading
import urllib.parse
import urllib.request
from typing import Callable

from . import events as E
from .i18n import t

#: 重连的退避：1s、2s、4s…封顶 10s。连接恢复后从头计。
_BACKOFF_CAP = 10.0


class EventStream:
    """订阅 `/api/events`，把每条事件交给回调；断了自动重连。

    回调在读取线程里执行 —— 调用方自己保证线程安全（往终端写要加锁，
    因为输入行随时可能在被重绘）。
    """

    def __init__(self, base: str, token: str,
                 on_event: Callable[[E.Event], None],
                 on_state: Callable[[str], None] | None = None,
                 session: str = "") -> None:
        self.base = base
        self.token = token
        #: 只订阅这一条会话的事件。空 = 全部会话 —— 那会把 IM 群聊与
        #: 别的地方的一轮也画进这个界面。
        self.session = str(session or "")
        self.on_event = on_event
        #: 连接状态变化："connected" / "disconnected" / 一句人话的失败原因。
        #: TUI 用它更新状态栏，也用它决定"还要不要相信屏幕上那个绿灯"。
        self.on_state = on_state or (lambda _s: None)
        self._stop = threading.Event()
        self._resp = None
        self._thread: threading.Thread | None = None
        #: 收到的最后一条事件的 seq。重连时作为 `last_id` 传回去，
        #: 服务端会把错过的事件里可重放的那部分补发（与浏览器的
        #: `Last-Event-ID` 同一行为）。
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="crannia-sse")
        self._thread.start()

    def stop(self) -> None:
        """停止订阅。

        先关响应对象再等线程：读取循环阻塞在 socket 上，只设标志位的话
        它要等到下一个心跳才会返回 —— 退出时卡十几秒，用户会以为挂了。
        """
        self._stop.set()
        resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            url = f"{self.base}/api/events?token={self.token}"
            if self.session:
                url += "&session=" + urllib.parse.quote(self.session)
            if self._last_seq:
                url += f"&last_id={self._last_seq}"
            reason = "disconnected"
            try:
                req = urllib.request.Request(
                    url, headers={"Accept": "text/event-stream"})
                # 不设短超时：SSE 是长连接，短超时会把正常的静默当成断线。
                resp = urllib.request.urlopen(req, timeout=None)
                self._resp = resp
                if getattr(resp, "status", 200) != 200:
                    reason = t("事件流被拒绝（HTTP %s）") % resp.status
                else:
                    self.on_state("connected")
                    attempt = 0
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        line = raw.decode("utf-8", "ignore").rstrip("\r\n")
                        if not line or line.startswith(":"):
                            # 心跳或 connected 注释帧。它们的存在本身是信息：
                            # 读到这里说明连接还活着。
                            continue
                        if not line.startswith("data:"):
                            continue
                        ev = E.parse(line[5:].strip())
                        if ev is None:
                            continue
                        if ev.seq:
                            self._last_seq = ev.seq
                        self.on_event(ev)
            except Exception as exc:
                if self._stop.is_set():
                    return
                reason = t("事件流已断开：%s") % type(exc).__name__
            if self._stop.is_set():
                return
            attempt += 1
            self.on_state(reason)
            # 退避放在"报过状态"之后：用户先看到"断了"，再等下一次尝试。
            self._stop.wait(min(2.0 ** (attempt - 1), _BACKOFF_CAP))
