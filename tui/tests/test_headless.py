"""--once 遇到审批时的断言。

真等 600 秒才能发现"没人能批"是没有意义的测试：这里用阻塞的假
`/api/chat` + 可推事件的注入流，把"审批先到"这件事直接摆到驱动面前。
测试进程的输出一律 ASCII；中文提示语只进捕获缓冲区，不打到控制台。
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path
from rich.console import Console

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import events as E  # noqa: E402
from crannia_tui import headless  # noqa: E402
from crannia_tui.backend import BackendError  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


class FakeBackend:
    """按路径记账；`/api/chat` 可以一直阻塞，直到测试放行。"""

    base = "http://127.0.0.1:1"
    token = "token"

    def __init__(self, reply: tuple[int, str] | None = None,
                 block_chat: bool = True, chat_error: Exception | None = None,
                 start_error: bool = False,
                 status_session: str = "workspace:private") -> None:
        self.reply = reply or (200, json.dumps({"reply": "已经完成"}))
        self.block_chat = block_chat
        self.chat_error = chat_error
        self.start_error = start_error
        self.status_session = status_session
        self.calls: list[tuple[str, dict | str]] = []
        self._release = threading.Event()

    def start(self) -> None:
        self.calls.append(("start", {}))
        if self.start_error:
            raise BackendError("假后端起不来")

    def wait_ready(self) -> dict:
        self.calls.append(("wait_ready", {}))
        return {}

    def start_framework(self) -> None:
        self.calls.append(("start_framework", {}))

    def get_json(self, path: str) -> dict:
        self.calls.append(("get_json", path))
        if path == "/api/status":
            return {"session": self.status_session}
        return {}

    def post_json(self, path: str, payload: dict,
                  timeout: float | None = None):
        self.calls.append((path, payload))
        if path == "/api/chat":
            if self.block_chat:
                self._release.wait(5)
            if self.chat_error is not None:
                raise self.chat_error
        return self.reply

    def stop(self) -> None:
        self.calls.append(("stop", {}))

    def release(self) -> None:
        self._release.set()

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


class FakeStream:
    """签名照 EventStream；测试从外部推事件，不依赖真 SSE。"""

    instances: list["FakeStream"] = []

    def __init__(self, base: str, token: str, on_event,
                 on_state=None, session: str = "") -> None:
        self.session = session
        self.on_event = on_event
        self.started = False
        self.stopped = False
        FakeStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def push(self, ev: E.Event) -> None:
        self.on_event(ev)


def capture_console(buf: io.StringIO):
    """把 run_once 自己 new 的 Console 指到内存，中文不进测试输出。"""
    return lambda: Console(file=buf, width=200, force_terminal=False,
                           legacy_windows=False)


def wait_stream(limit: float = 3.0) -> FakeStream:
    """等 run_once 建好注入的流。"""
    deadline = time.monotonic() + limit
    while not FakeStream.instances and time.monotonic() < deadline:
        time.sleep(0.01)
    check(bool(FakeStream.instances), "注入的流应当被创建")
    return FakeStream.instances[0]


def test_approval_exits_three() -> None:
    """审批一到就中断后端、出提示、返回 3；已经打出来的卡片还在。"""
    buf = io.StringIO()
    original = headless.Console
    headless.Console = capture_console(buf)
    fake = FakeBackend()
    FakeStream.instances.clear()
    result: dict[str, int] = {}

    def run() -> None:
        result["code"] = headless.run_once(
            fake, "请做一件需要批准的事", stream_factory=FakeStream)

    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
        stream = wait_stream()
        check(stream.started, "流应当启动")
        check(stream.session == "workspace:private",
              "★ 订阅必须带会话键（空 = 全部会话，会把别处的审批当成自己的）")
        check(("get_json", "/api/status") in fake.calls,
              "会话键应当从 /api/status 取")
        # 先渲染一张工具卡，再推审批：收尾不能清掉它。
        stream.push(E.Event(type=E.TOOL_START,
                            data={"call_id": "c1", "name": "read_file"}))
        stream.push(E.Event(type=E.TOOL_END,
                            data={"call_id": "c1", "ok": True,
                                  "chars": 12, "duration_ms": 3}))
        stream.push(E.Event(type=E.APPROVAL_REQUEST,
                            data={"request_id": "r1"}))
        worker.join(timeout=5)
    finally:
        fake.release()                       # 放行阻塞的 /api/chat
        headless.Console = original

    check(not worker.is_alive(), "审批后应当立刻收尾，而不是干等")
    check(result.get("code") == 3,
          f"审批应当返回 3，实际 {result.get('code')}")
    stops = [p for p, payload in fake.calls
             if p == "/api/stop" and payload == {}]
    check(len(stops) == 1, f"/api/stop 应当以 {{}} 调一次，实际 {stops}")
    check(fake.paths().index("/api/stop") < fake.paths().index("stop"),
          "先中断回合，再停后端进程")
    out = buf.getvalue()
    check("--once" in out and "请改用交互界面" in out,
          f"输出里应有中文说明，实际 {out!r}")
    check("read_file" in out, f"已渲染的工具卡应保留，实际 {out!r}")
    check(stream.stopped, "流应当被收掉")


def test_normal_reply_exits_zero() -> None:
    """没有审批：正常回一条仍返回 0，不调 /api/stop。"""
    buf = io.StringIO()
    original = headless.Console
    headless.Console = capture_console(buf)
    fake = FakeBackend(block_chat=False)
    FakeStream.instances.clear()
    try:
        code = headless.run_once(fake, "你好", stream_factory=FakeStream)
    finally:
        headless.Console = original

    check(code == 0, f"正常完成应返回 0，实际 {code}")
    check("/api/chat" in fake.paths(), "应当发过 /api/chat")
    check("/api/stop" not in fake.paths(), "没有审批不该中断")
    check("已经完成" in buf.getvalue(), "后端给的正文应当上屏")


def test_failures_still_exit_one() -> None:
    """后端起不来、请求失败仍是 1 —— 新退出码不许改掉老约定。"""
    original = headless.Console
    headless.Console = capture_console(io.StringIO())
    try:
        bad_start = FakeBackend(start_error=True)
        code_start = headless.run_once(
            bad_start, "x", stream_factory=FakeStream)
        bad_chat = FakeBackend(block_chat=False,
                               chat_error=OSError("连接已断开"))
        code_chat = headless.run_once(
            bad_chat, "x", stream_factory=FakeStream)
    finally:
        headless.Console = original

    check(code_start == 1, f"后端起不来应返回 1，实际 {code_start}")
    check(code_chat == 1, f"请求失败应返回 1，实际 {code_chat}")


def test_missing_session_refuses_instead_of_subscribing_all() -> None:
    """`/api/status` 没给会话键时宁可拒绝，也不退回"订阅全部会话"。"""
    buf = io.StringIO()
    original = headless.Console
    headless.Console = capture_console(buf)
    fake = FakeBackend(block_chat=False, status_session="")
    FakeStream.instances.clear()
    try:
        code = headless.run_once(fake, "你好", stream_factory=FakeStream)
    finally:
        headless.Console = original

    check(code == 1, f"拿不到会话应返回 1，实际 {code}")
    check(not FakeStream.instances, "拿不到会话时不该建事件流")
    check("/api/chat" not in fake.paths(), "拿不到会话时不该发消息")


def main() -> int:
    test_approval_exits_three()
    test_normal_reply_exits_zero()
    test_missing_session_refuses_instead_of_subscribing_all()
    test_failures_still_exit_one()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
