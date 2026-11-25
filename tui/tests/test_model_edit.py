"""`/model set` 的编辑面板（TUI 侧）。

    cd tui && .venv/Scripts/python.exe tests/test_model_edit.py

## 它钉什么

1. `/model set <编号>` 不发聊天、弹面板；编号按后端清单的顺序解析。
2. 三个输入依次回车：空 = 跳过（不提交那一格），有值才进请求。
3. 密钥只从面板收，请求体里只有改过的键；命令正文不进聊天。
4. 全空 = 未做更改，不调后端，也不假装改过。
5. 成功后 1.25 秒自动关闭并恢复输入框；Esc 随时退出。
6. 后端拒绝时留在框里报错，不自动关掉一个报错。

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
from crannia_tui.ui import ModelEditPanel  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


CARDS = {"default": "m11111111", "cards": [{"configs": [
    {"id": "m11111111", "name": "deepseek", "model": "deepseek-chat",
     "is_default": True},
    {"id": "m22222222", "name": "备用", "model": "deepseek-pro",
     "is_default": False},
]}]}


class FakeBackend:
    base = ""
    token = ""

    def __init__(self, *, code: int = 200, body: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.code = code
        self.body = body if body is not None else {"ok": True}
        self._lock = threading.Lock()

    def get_json(self, path: str, timeout: float = 10.0) -> dict:
        if path == "/api/model-configs":
            return CARDS
        if path == "/api/status":
            return {}
        if path == "/api/commands":
            return {"ok": True, "commands": []}
        return {}

    def post_json(self, path: str, payload: dict, timeout: float | None = None):
        with self._lock:
            self.calls.append((path, payload))
        if path == "/api/model-configs/update":
            return self.code, json.dumps(self.body)
        return 200, json.dumps({"reply": "ok"})

    def stop(self) -> None:
        pass

    def updates(self) -> list[dict]:
        with self._lock:
            return [p for path, p in self.calls
                    if path == "/api/model-configs/update"]


class _App(CranniaApp):
    CSS_PATH = str(HERE / "crannia_tui" / "app.tcss")

    def _boot(self) -> None:
        pass


async def _settle(pilot) -> None:
    await pilot.pause()
    await pilot.pause()


def _body(app) -> str:
    widget = app.query_one("#model-edit-body")
    renderable = widget.render()
    return str(getattr(renderable, "plain", renderable))


def test_set_opens_the_panel_and_collects_fields() -> None:
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                      # type: ignore[arg-type]
        async with app.run_test() as pilot:
            app._submit_text("/model set 2")
            await _settle(pilot)
            panel = app.query_one(ModelEditPanel)
            check(panel.is_open, "面板没弹出来")
            check(not app.query_one("#composer").display, "输入框没让位")
            check(not fake.calls, "还没填就发了请求：%r" % (fake.calls,))

            # 第一下回车从"当前条目"进到第一个输入。
            await pilot.press("enter")
            await _settle(pilot)
            name = app.query_one("#model-edit-name")
            model = app.query_one("#model-edit-model")
            api = app.query_one("#model-edit-api")
            check(getattr(api, "password", False) is True,
                  "API key 那一格没有设成掩码输入")
            check(not getattr(name, "password", False),
                  "条目名不该被掩码")

            name.value = "新名字"
            await pilot.press("enter")
            await _settle(pilot)
            # 模型名直接回车跳过。
            await pilot.press("enter")
            await _settle(pilot)
            api.value = "sk-test"
            await pilot.press("enter")
            await _settle(pilot)

            ups = fake.updates()
            check(len(ups) == 1, "应当只发一次更新：%r" % (ups,))
            check(ups[0]["id"] == "m22222222", repr(ups[0]))
            check(ups[0].get("name") == "新名字", repr(ups[0]))
            check("model" not in ups[0], "跳过的模型名进了请求：%r" % (ups[0],))
            check(ups[0].get("api") == "sk-test", repr(ups[0]))
            check(not any(path == "/api/chat" for path, _ in fake.calls),
                  "命令被当成聊天发出去了")
            check(api.value == "", "密钥还留在输入框里")
            check("配置更改完成" in _body(app), _body(app))

            await pilot.pause(1.4)
            await _settle(pilot)
            check(not panel.is_open, "1.25 秒后没有自动关闭")
            check(app.query_one("#composer").display, "输入框没恢复")
    asyncio.run(scenario())


def test_all_skipped_is_not_success() -> None:
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                      # type: ignore[arg-type]
        async with app.run_test() as pilot:
            app._submit_text("/model set 1")
            await _settle(pilot)
            await pilot.press("enter")                # 进入第一格
            for _ in range(3):
                await pilot.press("enter")            # 三格全跳过
                await _settle(pilot)
            check(not fake.updates(), "全空却调了后端：%r" % (fake.updates(),))
            check("未做更改" in _body(app), _body(app))
            await pilot.pause(1.4)
            await _settle(pilot)
            check(not app.query_one(ModelEditPanel).is_open, "没有自动关")
    asyncio.run(scenario())


def test_failure_stays_open_until_escape() -> None:
    async def scenario() -> None:
        fake = FakeBackend(code=400, body={"ok": False, "error": "名字撞了"})
        app = _App(backend=fake)                      # type: ignore[arg-type]
        async with app.run_test() as pilot:
            app._submit_text("/model set 1")
            await _settle(pilot)
            await pilot.press("enter")
            name = app.query_one("#model-edit-name")
            name.value = "x"
            await pilot.press("enter")
            await pilot.press("enter")
            await pilot.press("enter")
            await _settle(pilot)
            panel = app.query_one(ModelEditPanel)
            check(panel.is_open, "失败后自己关了")
            check("更改失败" in _body(app), _body(app))
            await pilot.press("escape")
            await _settle(pilot)
            check(not panel.is_open, "Esc 没关掉")
            check(app.query_one("#composer").display, "输入框没恢复")
    asyncio.run(scenario())


def test_bad_reference_does_not_open_the_panel() -> None:
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                      # type: ignore[arg-type]
        async with app.run_test() as pilot:
            app._submit_text("/model set 9")
            await _settle(pilot)
            check(not app.query_one(ModelEditPanel).is_open, "越界编号也弹了面板")
            check(not fake.updates(), "越界编号发了请求")
    asyncio.run(scenario())


def main() -> int:
    test_set_opens_the_panel_and_collects_fields()
    test_all_skipped_is_not_success()
    test_failure_stays_open_until_escape()
    test_bad_reference_does_not_open_the_panel()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
