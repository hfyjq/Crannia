"""事件流的重连：断了要自己接上，断点要续上。

没有重连的话，界面就是"卡死"本身 —— 横幅继续转、卡片停在 `running`，
而实际上一条事件都收不到了。这类故障用户报上来永远是"它不动了"，
所以这里值得钉两条断言：**会重连**、**重连带着断点**。
"""

from __future__ import annotations

import sys
import time
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import client as C  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


class _Resp:
    """假响应：给几行就吐几行，吐完即视为连接结束。"""

    def __init__(self, lines: list[bytes]) -> None:
        self.status = 200
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        pass


def test_reconnect_and_catch_up() -> None:
    states: list[str] = []
    events: list = []
    urls: list[str] = []
    attempts = {"n": 0}
    event_line = (b'data: {"type": "context.state", "seq": 7, "ts": 0, '
                  b'"data": {"pct": 3}}\n')

    def fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        attempts["n"] += 1
        if attempts["n"] == 1:
            return _Resp([event_line])            # 给一条就断
        if attempts["n"] == 2:
            raise urllib.error.URLError("connection refused")
        return _Resp([b": hb\n"])                 # 第三次连上，只发心跳

    original = C.urllib.request.urlopen
    C.urllib.request.urlopen = fake_urlopen
    try:
        stream = C.EventStream("http://x", "t", events.append, states.append)
        stream.start()
        waited = 0.0
        while attempts["n"] < 3 and waited < 5.0:
            time.sleep(0.05)
            waited += 0.05
        time.sleep(0.2)                           # 让第三次的 connected 报出来
        stream.stop()
    finally:
        C.urllib.request.urlopen = original

    check(attempts["n"] >= 3, f"断线后应当继续重连，实际 {attempts['n']} 次")
    check(len(events) == 1 and events[0].seq == 7,
          f"断线前那条事件要送到，实际 {events}")
    # 第一次连接收到过 seq=7：第二次请求必须把断点带回去
    check("last_id=7" in urls[1], f"重连应带 last_id=7：{urls[1]}")
    check("connected" in states and
          any(s != "connected" for s in states),
          f"状态里既有连上也有断开：{states}")
    # 事件流线程要在 stop 后很快退出（退避等待必须可中断）
    check(stream._thread is None, "stop 之后读取线程应当已被收走")


def test_stop_interrupts_backoff() -> None:
    """退避等待必须能被 stop 打断 —— 否则退出时要白等一轮退避。"""
    states: list[str] = []

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("refused")

    original = C.urllib.request.urlopen
    C.urllib.request.urlopen = fake_urlopen
    try:
        stream = C.EventStream("http://x", "t", lambda _e: None,
                               states.append)
        stream.start()
        time.sleep(0.3)                           # 够它失败两三次、进到退避里
        t0 = time.time()
        stream.stop()
        elapsed = time.time() - t0
        check(stream._thread is None, "线程应退出")
        check(elapsed < 2.0,
              f"stop 应立刻打断退避等待，实际等了 {elapsed:.1f}s")
    finally:
        C.urllib.request.urlopen = original


if __name__ == "__main__":
    test_reconnect_and_catch_up()
    test_stop_interrupts_backoff()
    print(f"{_n} checks: {_n} passed, 0 failed")
