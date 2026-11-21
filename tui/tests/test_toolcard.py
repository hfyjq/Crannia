"""工具卡（步骤流的一格）的断言。

这一块有两个失手点，都不会报错：

· **画**：圆点颜色、状态文字、右上角对不齐 —— 只在真终端里看得出来；
· **配对与结局**：`start`/`end` 靠 `call_id` 对上；结局取自事件里的
  `outcome` / `reason` —— 认错了就是把"没跑"画成"跑完了"。

渲染断言读的是卡片的内部状态（`_label`/`_meta`）—— 颜色与对齐那种
"长什么样"的部分靠 `tools/pty_drive.py` 的整屏还原来看，两边各管一半。
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
from crannia_tui.ui import ToolCard  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def plain(renderable, width: int = 72) -> str:
    from rich.console import Console
    con = Console(width=width, force_terminal=False, legacy_windows=False)
    with con.capture() as cap:
        con.print(renderable)
    return cap.get()


def test_running_card() -> None:
    card = ToolCard(activity="正在写入 slide_007_v2.svg",
                    arg="slide_007_v2.svg", name="write_file")
    out = plain(card._build())
    check(card.state == "run", "刚调起应是运行中")
    check("正在写入 slide_007_v2.svg" in out, "运行中应显示进行时描述（原样带'正在'）")
    check("> slide_007_v2.svg" in out, "参数应单独一行（> 行）")
    # 不到一秒不显示计时：每个快工具都闪一下"0s"没有信息量
    check("0s" not in out, "不满一秒不该显示 0s")


def test_done_card() -> None:
    card = ToolCard(activity="正在读取 app.py", arg="app.py", name="read_file")
    card.done(outcome="completed", chars=4820, ms=41)
    check(card.state == "completed", "应落到完成态")
    check(card._label == "read_file（已完成）",
          f"完成态标题应是工具名+结局，实际 {card._label!r}")
    out = plain(card._build())
    check("（已完成）" in out and "app.py" in out, "完成态要能看到结局与参数")
    check("4,820 字符" in out and "41ms" in out, "体积与耗时都要在")


def test_error_card() -> None:
    """失败与"未执行"都是白点；错误正文不上屏，体积耗时也不报。"""
    card = ToolCard(activity="正在执行命令", arg="pytest -x", name="run_command")
    card.done(outcome="error", reason="failed", chars=2140, ms=1230)
    check(card.state == "error", "应落到 error")
    check("（失败）" in card._label, f"实际 {card._label!r}")
    out = plain(card._build())
    check("2,140" not in out and "1.2s" not in out, f"没跑成的不报体积耗时：{out!r}")
    check("Traceback" not in out, "错误正文不该上屏")

    refused = ToolCard(activity="正在执行命令", name="run_command")
    refused.done(outcome="error", reason="refused")
    check("（未执行）" in refused._label, f"实际 {refused._label!r}")


def test_outcome_labels_and_dots() -> None:
    """十种（结局, 细分）的文案与圆点 —— 两个白点例外在这里钉死。"""
    from crannia_tui import outcome as O
    table = [
        ("completed", "", "已完成", "green"),
        ("waiting", "", "待审批", "yellow"),
        ("error", "failed", "失败", "white"),
        ("error", "refused", "未执行", "white"),
        ("denied", "auto-review-blocked", "自动审查拦截", "red"),
        ("rejected", "cancelled", "未采纳", "white"),
        ("rejected", "recused", "未执行", "white"),
        ("rejected", "user", "被拒绝", "red"),
        ("rejected", "unavailable", "被拒绝", "red"),
        ("rejected", "auto-review-unavailable", "被拒绝", "red"),
    ]
    for outcome, reason, text, colour in table:
        check(O.label(outcome, reason) == text,
              f"{outcome}/{reason} 应说 {text!r}，实际 {O.label(outcome, reason)!r}")
        check(O.dot(outcome, reason) == colour,
              f"{outcome}/{reason} 应是 {colour}，实际 {O.dot(outcome, reason)}")


def test_waiting_card() -> None:
    """挂起等审批：黄点、不报体积（那格字数其实是审批载荷自己的长度）。"""
    card = ToolCard(activity="正在执行命令", name="run_command")
    card.done(outcome="waiting")
    check(card.state == "waiting", "应落到 waiting")
    check("（待审批）" in card._label, f"实际 {card._label!r}")
    out = plain(card._build())
    check("字符" not in out, f"等审批不该报体积：{out!r}")


def test_orphan() -> None:
    card = ToolCard(activity="正在执行命令", name="run_command")
    card.orphan()
    check(card.state == "unknown", "没收尾的卡应是 unknown")
    check("未检测到结束事件" in card._meta, "要说清为什么停在那儿")


# -- 接线：事件 → 卡片 → 结局 -------------------------------------------

def ev(_type: str, **data) -> E.Event:
    return E.Event(type=_type, data=data)


class FakeBackend:
    base = ""
    token = ""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def get_json(self, path: str) -> dict:
        with self._lock:
            self.calls.append(path)
        return {}

    def post_json(self, path: str, payload: dict, timeout: float | None = None):
        return 200, "{}"

    def stop(self) -> None:
        pass


class _App(CranniaApp):
    # 样式表按"定义 App 的那个模块"解析，子类在 tests/ 下，所以要给绝对路径。
    CSS_PATH = str(HERE / "crannia_tui" / "app.tcss")

    def _boot(self) -> None:                      # 不连后端
        pass


async def wait_for(cond, limit: float = 3.0) -> None:
    waited = 0.0
    while not cond() and waited < limit:
        await asyncio.sleep(0.05)
        waited += 0.05


def test_wiring_outcomes() -> None:
    """事件 → 卡片：结局取自事件，不再去轨迹里猜。

    第三格是审批：`waiting` 之后这一轮的 `task.end` 是常态（后端停在那儿
    等人拍板）—— 卡片不许被标成"没收到结束事件"；结算事件用**同一个
    `call_id`** 回来时，原地改那张卡，不许补第二张。
    """
    async def scenario() -> None:
        app = _App(backend=FakeBackend())         # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()

            app._on_event(ev(E.TOOL_START, call_id="c1", name="run_command",
                             activity="正在执行命令", sub_hint="pytest -x"))
            app._on_event(ev(E.TOOL_END, call_id="c1", name="run_command",
                             outcome="error", reason="failed", ok=False))
            app._on_event(ev(E.TOOL_START, call_id="c2", name="run_command",
                             activity="正在执行命令", sub_hint="rm -rf build/"))
            app._on_event(ev(E.TOOL_END, call_id="c2", name="run_command",
                             outcome="denied", reason="auto-review-blocked"))
            app._on_event(ev(E.TOOL_START, call_id="c3", name="run_command",
                             activity="正在执行命令", sub_hint="git status"))
            app._on_event(ev(E.TOOL_END, call_id="c3", name="run_command",
                             outcome="waiting", reason=""))
            cards = list(app.query(ToolCard))
            check([c.state for c in cards] == ["error", "denied", "waiting"],
                  f"三张卡的结局：{[c.state for c in cards]}")
            check("c3" in app._pending_cards, "等审批的卡要记在 _pending_cards")

            # 挂审批时这一轮会正常结束 —— 不许把等待中的卡标成没收尾。
            app._on_event(ev(E.TASK_END, response=""))
            check(cards[2].state == "waiting",
                  f"task.end 之后仍应是 waiting：{cards[2].state}")

            # 用户拒绝：同一个 call_id 的结算事件 → 原地改。
            app._on_event(ev(E.TOOL_END, call_id="c3", name="run_command",
                             outcome="rejected", reason="user"))
            await pilot.pause()
            cards = list(app.query(ToolCard))
            check(len(cards) == 3, f"结算事件不许再补一张卡：{len(cards)}")
            check(cards[2].state == "rejected", cards[2].state)
            check(not app._pending_cards, "_pending_cards 应已清空")

    asyncio.run(scenario())


def test_running_marker_and_command_lines() -> None:
    """运行中：命令折行但**最多四行**（多的报剩余行数），右上角 `running 1.23s`。

    单行截断会把命令的尾巴藏掉（"到底对哪个目录做的事"常常在那半截），
    而不封顶的折行会把对话挤成走马灯 —— 四行 + 报数是两头的折中。
    """
    short = 'pwd && echo "---" && ls -la | head -20'
    card = ToolCard(activity="正在执行命令", arg=short, name="run_command")
    out = plain(card._build(64), width=64)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    check("running…" in lines[0], f"还没到第一拍应显示 running…：{lines[0]!r}")
    check(len(lines) == 2, f"短命令应只占一行，实际 {lines}")
    check("还有" not in out, "短命令不该报剩余行数")

    # 很长的命令：四行封顶 + 一行剩余计数
    long_cmd = " && ".join(f"echo step{i}" for i in range(1, 30))
    card2 = ToolCard(activity="正在执行命令", arg=long_cmd, name="run_command")
    out = plain(card2._build(60), width=60)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    check(len(lines) <= 7, f"卡片不该无限长：{lines}")
    check("还有" in out and "行" in out, f"超长应报剩余行数：{out!r}")
    # 每行都不超过给定宽度（按显示列算，中文两格）
    check(all(len(ln) <= 60 for ln in lines), f"行宽溢出：{max(lines, key=len)!r}")

    # 第一拍之后走字：0.5s -> 1.23s，而且一直是 running 开头
    card.tick(0.5)
    check("running 500ms" in plain(card._build(64)), "半秒应给毫秒")
    card.tick(1.23)
    check("running 1.23s" in plain(card._build(64)), "过一秒应换成秒（两位小数）")

    # 跑完之后右上角换成体积与耗时，不再有 running
    card.done(outcome="completed", chars=873, ms=1230)
    out = plain(card._build(64))
    check("running" not in out, "完成态不该再出现 running")
    check("873 字符 · 1.2s" in out, f"完成态应给体积与耗时：{out!r}")


def test_dot_colour_does_not_bleed() -> None:
    """颜色只归最前面那颗圆点，正文白、次要信息灰。

    曾经 `Text("● ", style=colour)` 的 base style 会传染给之后 append 的
    每一段 —— 整张卡跟着圆点变绿/变红，主次全被颜色喊哑。
    """
    card = ToolCard(activity="正在执行命令", arg="pytest -x", name="run_command")
    card.done(outcome="completed", chars=100, ms=12)
    from rich.console import Console
    con = Console(width=72, force_terminal=True, record=True,
                  color_system="truecolor", legacy_windows=False)
    con.print(card._build())
    out = con.export_text(styles=True)
    check("\x1b[32m● \x1b[0m" in out, "圆点应单独染色（绿）")
    check("run_command（已完成）\x1b[0m" not in out or
          "\x1b[32mrun_command" not in out,
          "正文不应被圆点的颜色包住")
    # 更直接的说法：正文行里除圆点外没有 32（绿）
    line = next(ln for ln in out.splitlines() if "run_command" in ln)
    check("\x1b[32m" not in line.replace("\x1b[32m● \x1b[0m", ""),
          f"正文行不应带圆点的绿色：{line!r}")
    check("\x1b[2m" in out, "次要信息（参数/体积耗时）应是灰（dim）")


def main() -> int:
    test_running_card()
    test_done_card()
    test_error_card()
    test_outcome_labels_and_dots()
    test_waiting_card()
    test_orphan()
    test_wiring_outcomes()
    test_dot_colour_does_not_bleed()
    test_running_marker_and_command_lines()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    test_dot_colour_does_not_bleed()
    sys.exit(main())
