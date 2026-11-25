"""切「完全权限」前的安全提示（TUI 侧）。

与 GUI 的 `SafetyNotice` 同义：**一条命令不能静默把会话切进"不问而放"**。
形状复用审批面板（同一套键位：数字选中、回车确认），但回答归本地 ——
它没有后端的 `request_id`，发到 `/api/approval` 只会被当成一次不存在的审批。

钉住四件事：

1. `/mode full_access` 与 `/mode 完全权限` 都先弹提示，**不发给后端**；
2. 选「取消」不发、命令放回输入框（取消不是把用户的话吃掉）；
3. 选「启用」正常发出去（命令仍由后端执行，TUI 不另实现一套切换）；
4. 选「启用，且不再询问」之后，**本次运行**不再弹（重启仍会问）。
   以及已经在完全权限时不再弹（重复设置不是提权）。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui.app import CranniaApp  # noqa: E402
from crannia_tui.ui import ApprovalPanel  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


class FakeBackend:
    base = ""
    token = ""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def get_json(self, path: str) -> dict:
        if path == "/api/status":
            return {"mode_session": "auto", "mode_session_name": "自动",
                    "mode_configured": "manual", "mode_name": "手动"}
        if path == "/api/commands":
            return {"ok": True, "commands": []}
        return {}

    def post_json(self, path: str, payload: dict, timeout: float | None = None):
        with self._lock:
            self.calls.append((path, payload))
        return 200, json.dumps({"reply": "ok"})

    def stop(self) -> None:
        pass

    def chats(self) -> list[dict]:
        with self._lock:
            return [p for path, p in self.calls if path == "/api/chat"]


class _App(CranniaApp):
    CSS_PATH = str(HERE / "crannia_tui" / "app.tcss")

    def _boot(self) -> None:                      # 不连后端
        pass


async def _settle(pilot) -> None:
    await pilot.pause()
    await pilot.pause()


def test_cancel_sends_nothing_and_restores_the_line() -> None:
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            app._mode_key = "auto"
            app._submit_text("/mode full_access")
            await _settle(pilot)
            panel = app.query_one(ApprovalPanel)
            check(panel.display, "面板没弹出来")
            check(not fake.chats(), "还没确认就发出去了：%r" % (fake.chats(),))
            # 默认落在「取消」上：直接回车不该提权。
            await pilot.press("enter")
            await _settle(pilot)
            check(not panel.display, "取消后面板没收起")
            check(not fake.chats(), "取消之后仍然发出去了：%r" % (fake.chats(),))
            check(app.query_one("#composer").text == "/mode full_access",
                  "命令没放回输入框：%r" % app.query_one("#composer").text)
    asyncio.run(scenario())


def test_confirm_sends_the_command_and_skip_sticks_for_this_run() -> None:
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            app._mode_key = "auto"
            app._submit_text("/mode 完全权限")
            await _settle(pilot)
            await pilot.press("1")
            await pilot.press("enter")
            await _settle(pilot)
            chats = fake.chats()
            check(chats and chats[-1].get("text") == "/mode 完全权限",
                  "确认之后没把命令发出去：%r" % (chats,))

            # 第二次仍然要问（没选「不再询问」）。
            app._submit_text("/mode full_access")
            await _settle(pilot)
            check(app.query_one(ApprovalPanel).display, "第二次没弹提示")

            # 选 2：本次运行不再问。
            await pilot.press("2")
            await pilot.press("enter")
            await _settle(pilot)
            check(app._safety_skip, "选了不再询问但标志没记住")
            n = len(fake.chats())
            app._submit_text("/mode full_access")
            await _settle(pilot)
            check(not app.query_one(ApprovalPanel).display,
                  "选了不再询问之后还弹")
            check(len(fake.chats()) == n + 1, "跳过提示之后没发出去")
    asyncio.run(scenario())


def test_already_full_access_is_not_prompted() -> None:
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            app._mode_key = "full_access"
            app._submit_text("/mode full_access")
            await _settle(pilot)
            check(not app.query_one(ApprovalPanel).display,
                  "已经在完全权限还弹提示")
            check(len(fake.chats()) == 1, "命令没发出去")
    asyncio.run(scenario())


def main() -> int:
    test_cancel_sends_nothing_and_restores_the_line()
    test_confirm_sends_the_command_and_skip_sticks_for_this_run()
    test_already_full_access_is_not_prompted()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
