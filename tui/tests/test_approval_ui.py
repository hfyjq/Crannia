"""审批面板（全屏形态）的断言。

审批是"答错也没人报错"的重灾区：

· `GET` 拉不到详情却拿事件里的字段硬画 —— 界面上是一个空壳面板；
· 数字键只是挪了光标却没确认 —— 用户以为答了，那一轮其实还挂着；
· 答完不撤面板 / 撤了不还输入框 —— 后面整个会话都没法打字；
· 文本态空回车 —— 后端没有默认值，静默发空串等于替用户放弃。

所以用真 App 喂事件，把每条路都走一遍。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import events as E  # noqa: E402
from crannia_tui.app import CranniaApp  # noqa: E402
from crannia_tui.ui import ApprovalPanel, HeaderBar  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


class FakeBackend:
    """`get_json` 给详情、`post_json` 记下收到的载荷 —— 送到后端的东西
    必须能从测试里拿回来看，"送出去了大概没问题"不算验证。"""

    base = ""
    token = ""

    def __init__(self, pending: dict | None = None) -> None:
        self.pending = pending
        self.calls: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self._lock = threading.Lock()

    def get_json(self, path: str) -> dict:
        self.gets.append(path)
        return {"pending": self.pending}

    def post_json(self, path: str, payload: dict, timeout: float | None = None):
        with self._lock:
            self.calls.append((path, payload))
        return 200, "{}"

    def stop(self) -> None:
        pass


class _App(CranniaApp):
    # 样式表按"定义 App 的那个模块"解析，子类在 tests/ 下，所以要给绝对路径。
    CSS_PATH = str(HERE / "crannia_tui" / "app.tcss")

    def _boot(self) -> None:                      # 不连后端
        pass


PENDING = {
    "request_id": "r1",
    "kind": "command",
    "body": "Crannia 想执行 rm -rf build/",
    "options": [
        {"key": "allow", "label": "同意"},
        {"key": "allow_session", "label": "同意，本会话不再询问"},
        {"key": "deny", "label": "拒绝"},
        {"key": "other", "label": "我有别的话说..."},
    ],
    "default_key": "allow",
}

TEXT_PENDING = {"request_id": "r2", "kind": "command", "expect": "text",
                "question": "有什么需要告诉 Crannia 的？", "options": []}


def ev(_type: str, **data) -> E.Event:
    # 第一个参数带下划线：事件的载荷里也有个 `kind`，不避开就和它撞名。
    return E.Event(type=_type, data=data)


def mount(app: CranniaApp, pending: dict) -> None:
    """把一次审批挂上（同步喂事件 —— 事件线程就是这么进来的）。"""
    app._on_event(ev(E.APPROVAL_REQUEST, request_id=pending.get("request_id")))


async def wait_for(fake: FakeBackend, count: int, limit: float = 3.0) -> None:
    """提交在后台线程里发，等它落账。超时就让下面的断言去报。"""
    waited = 0.0
    while len(fake.calls) < count and waited < limit:
        await asyncio.sleep(0.05)
        waited += 0.05


def test_choice_flow() -> None:
    async def scenario() -> None:
        fake = FakeBackend(PENDING)
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            mount(app, PENDING)
            await pilot.pause()
            panel = app.query_one(ApprovalPanel)

            # 1) 面板占的正是输入框那个槽
            check(panel.display, "面板应当显示")
            check(not app.query_one("#composer").display, "输入框应当让位")
            check(app._approval is not None and app._approval.request_id == "r1",
                  "应当记着这次审批")

            # 2) 数字键只挪选中，不提交 —— 按 2 之后不该有任何请求
            await pilot.press("2")
            await pilot.pause()
            check(not fake.calls, "数字键不该直接提交")
            check(panel._picked == 1, f"按 2 应选中第二项，实际 {panel._picked}")

            # 3) 回车确认 → 送回去的是后端给的 key，不是序号
            await pilot.press("enter")
            await wait_for(fake, 1)
            check(len(fake.calls) == 1, f"应提交一次，实际 {len(fake.calls)}")
            path, payload = fake.calls[0]
            check(path == "/api/approval", f"应发到 /api/approval，实际 {path}")
            check(payload.get("request_id") == "r1", "request_id 要带上")
            check(payload.get("choice") == "allow_session",
                  f"选中项要换成 key，实际 {payload.get('choice')!r}")

            # 4) 答完 → 面板收起、输入框回来
            check(not panel.display, "答完面板应收起")
            check(app._approval is None, "应当清掉挂着的审批")
            check(app.query_one("#composer").display, "输入框应当回来")

    asyncio.run(scenario())


def test_arrows_and_default() -> None:
    async def scenario() -> None:
        fake = FakeBackend(PENDING)
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            mount(app, PENDING)
            await pilot.pause()
            panel = app.query_one(ApprovalPanel)
            n = len(PENDING["options"])

            # 初始落在默认项上（第 0 项）—— "永不默认选中升级项"由后端保证。
            check(panel._picked == 0, f"初始应选中默认项，实际 {panel._picked}")

            # 到底再往下要绕回第一项；在顶上往上要绕到最后一项。
            # 越界要么崩要么卡住，两种都过不了这一条。
            for _ in range(n):
                await pilot.press("down")
            check(panel._picked == 0, f"绕一圈应回到起点，实际 {panel._picked}")
            await pilot.press("up")
            check(panel._picked == n - 1, f"顶上往上应到最后一项，实际 {panel._picked}")

            # 回车确认当前高亮项（最后一项是"我有别的话说"，key=other）
            await pilot.press("enter")
            await wait_for(fake, 1)
            check(fake.calls[-1][1].get("choice") == "other",
                  f"回车应确认高亮项，实际 {fake.calls[-1][1]!r}")

    asyncio.run(scenario())


def test_text_mode() -> None:
    async def scenario() -> None:
        # "我有别的话说"提交后，后端进入文本子状态：expect=text、无选项
        fake = FakeBackend(TEXT_PENDING)
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            mount(app, TEXT_PENDING)
            await pilot.pause()
            panel = app.query_one(ApprovalPanel)
            composer = app.query_one("#composer")
            inp = app.query_one("#approval-input")

            check(not composer.display, "文本态输入框也要让位")
            check(inp.display, "面板里应当有一行可输入")
            body = panel.query_one("#approval-body")
            check("告诉" in str(body.content), "正文应有问题文本")

            # 空回车不提交：后端没有默认值，静默发空串等于替用户放弃
            await pilot.press("enter")
            await pilot.pause()
            check(not fake.calls, "空回车不应提交")

            await pilot.press(*"改用 PowerShell 跑")
            await pilot.press("enter")
            await wait_for(fake, 1)
            path, payload = fake.calls[-1]
            check(payload.get("text") == "改用 PowerShell 跑",
                  f"应送文本，实际 {payload!r}")
            check(not panel.display, "答完面板应收起")
            check(composer.display, "输入框应当回来")

    asyncio.run(scenario())


def test_stale_paths() -> None:
    async def scenario() -> None:
        fake = FakeBackend(PENDING)
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            mount(app, PENDING)
            await pilot.pause()
            panel = app.query_one(ApprovalPanel)
            check(panel.display, "面板应当显示")

            # 别处答掉了：resolved 事件把面板撤下来
            app._on_event(ev(E.APPROVAL_RESOLVED, request_id="r1", kind="command"))
            await pilot.pause()
            check(not panel.display, "resolved 应撤面板")
            check(app._approval is None, "resolved 应清状态")
            check(app.query_one("#composer").display, "输入框应当回来")

            # GET 说没有待审批了：不动界面，也不该崩
            fake.pending = None
            app._on_event(ev(E.APPROVAL_REQUEST, request_id="r9"))
            await pilot.pause()
            check(app._approval is None, "没有待审批时不应挂起")

    asyncio.run(scenario())


def test_clear_keeps_header() -> None:
    async def scenario() -> None:
        fake = FakeBackend()
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            check(len(app.query(HeaderBar)) == 1, "启动后应有一个抬头")

            app.action_clear()
            await pilot.pause()
            check(len(app.query(HeaderBar)) == 1, "clear 之后抬头应还在")

            # 状态刷新挂在定时回调上，抛了界面上也不显，这里直接调出来验。
            app._apply_status({"model": "another-model", "model_config_name": "x"})
            await pilot.pause()
            info = app.query_one(HeaderBar).query_one("#info")
            check("another-model" in str(info.content),
                  f"抬头应显示新模型名，实际 {str(info.content)!r}")

    asyncio.run(scenario())


def test_boot_picks_up_an_already_pending_approval() -> None:
    """接入一个已经在跑的后端时，先挂着的审批要补读出来。

    SSE 从 `last_seq=0` 起、不回放历史 —— 光订阅会漏掉"我来之前就挂着"的那条。
    """
    import crannia_tui.app as APP

    class _StubStream:
        def __init__(self, *args, **kwargs) -> None:
            self.started = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            pass

    async def scenario() -> None:
        fake = FakeBackend(PENDING)
        app = _App(backend=fake)                  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            app._boot_ready({"session": "workspace:private",
                             "version": "1.1.0", "model": "m",
                             "effort_name": "high"}, [])
            await pilot.pause()
            panel = app.query_one(ApprovalPanel)
            check(app._approval is not None
                  and app._approval.request_id == "r1",
                  "启动时就挂着的审批应当直接显示")
            check(panel.display, "面板应当显示")
            check("/api/approval" in fake.gets, "应当补读一次 /api/approval")
            #: 实时事件与补读撞在一起：同一个 request_id 只画一次。
            app._approval_requested()
            await pilot.pause()
            check(app._approval is not None
                  and app._approval.request_id == "r1", "重复到达不该换掉它")
            check(panel.display, "重复到达不该撤掉面板")

    original = APP.EventStream
    APP.EventStream = _StubStream
    try:
        asyncio.run(scenario())
    finally:
        APP.EventStream = original


def main() -> int:
    test_choice_flow()
    test_arrows_and_default()
    test_text_mode()
    test_stale_paths()
    test_clear_keeps_header()
    test_boot_picks_up_an_already_pending_approval()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
