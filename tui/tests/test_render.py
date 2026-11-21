"""渲染层的行为锁。

不引入 pytest：这个包对外只有 rich 一个运行依赖，测试再拉一套框架
会让"clone 下来就能验"变成"先装测试依赖"。所以自带一个极小的
`check()`，`python tests/test_render.py` 直接跑。

断言一律按**逻辑行**比对，不按物理行：rich 会把长行按终端宽度折成多行，
按物理行断言的话，换一个宽度就跑红 —— 而那条失败跟代码对不对无关。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

from crannia_tui import events as E
from crannia_tui.render import ScrollbackRenderer

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [ok] {label}")
    else:
        _FAIL += 1
        _FAILED.append(label)
        print(f"  [!!] {label}" + (f"  —— {detail}" if detail else ""))


def render(items) -> tuple[str, ScrollbackRenderer]:
    """喂一串 (type, data)，拿回纯文本输出。"""
    buf = io.StringIO()
    console = Console(file=buf, width=88, force_terminal=False, legacy_windows=False)
    r = ScrollbackRenderer(console)
    for etype, data in items:
        r.feed(E.Event(type=etype, data=data))
    r.flush_unclosed()
    return buf.getvalue(), r


def lines_of(text: str) -> list[str]:
    """去掉行尾空白与空行，只留有内容的逻辑行。"""
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------- 解析

def test_parse() -> None:
    print("\nparse：坏输入不许炸")
    check("合法 JSON -> Event", E.parse('{"type":"say","seq":1,"data":{"text":"hi"}}') is not None)
    ev = E.parse('{"type":"say","seq":7,"session":"cli:private:local","data":{"text":"hi"}}')
    check("seq 保留", ev is not None and ev.seq == 7)
    check("session 保留", ev is not None and ev.session == "cli:private:local")
    check("data.text 取得到", ev is not None and ev.s("text") == "hi")

    check("非 JSON -> None", E.parse("not json") is None)
    check("JSON 但不是对象 -> None", E.parse("[1,2]") is None)
    check("缺 type -> None", E.parse('{"seq":1}') is None)
    check("type 非字符串 -> None", E.parse('{"type":123}') is None)


def test_value_helpers() -> None:
    print("\n取值助手：类型不对时给默认值，不抛")
    ev = E.Event(type="x", data={"n": 3, "s": "a", "b": True})
    check("i() 取整数", ev.i("n") == 3)
    check("i() 缺字段给默认", ev.i("nope", -1) == -1)
    check("s() 取字符串", ev.s("s") == "a")
    check("s() 类型不符给默认", ev.s("n") == "")
    check("b() 取布尔", ev.b("b") is True)
    check("b() 类型不符给默认", ev.b("s") is False)


# ---------------------------------------------------------------- 工具卡片

def test_tool_card_pairs_start_and_end() -> None:
    print("\n工具卡片：activity 来自 start，结果来自 end")
    out, _ = render([
        (E.TOOL_START, {"call_id": "c1", "name": "read_file",
                        "activity": "读 logs/app.log"}),
        (E.TOOL_END, {"call_id": "c1", "name": "read_file", "ok": True,
                      "duration_ms": 120, "result_chars": 4820}),
    ])
    ls = lines_of(out)
    check("有且只有两行（标题 + 结果）", len(ls) == 2, str(ls))
    check("标题含工具名", "read_file" in ls[0])
    check("标题含 activity", "读 logs/app.log" in ls[0])
    check("结果含千分位字符数", "4,820" in ls[1], ls[1])
    check("结果含耗时", "120ms" in ls[1], ls[1])


def test_tool_card_failure() -> None:
    print("\n工具卡片：失败时给一行错误，不外溢")
    out, _ = render([
        (E.TOOL_START, {"call_id": "c2", "name": "run_command", "activity": "跑测试"}),
        (E.TOOL_END, {"call_id": "c2", "name": "run_command", "ok": False,
                      "duration_ms": 3210, "result_chars": 1240,
                      "error": "exit 1：3 failed\n第二行 traceback 不该出现"}),
    ])
    ls = lines_of(out)
    check("错误只占一行", len(ls) == 2, str(ls))
    check("显示错误首行", "3 failed" in ls[1], ls[1])
    check("第二行被截掉", "traceback 不该出现" not in out)


def test_tool_card_without_activity() -> None:
    print("\n工具卡片：start 丢了也要能画")
    out, _ = render([
        (E.TOOL_END, {"call_id": "c3", "name": "glob", "ok": True,
                      "duration_ms": 40, "result_chars": 12}),
    ])
    ls = lines_of(out)
    check("没有 activity 时不留双空格残留", len(ls) == 2 and ls[0].strip().startswith("⏺"), str(ls))
    check("仍显示工具名", "glob" in ls[0])


def test_unclosed_tool_is_reported() -> None:
    print("\n没等到 end 的工具不许静默丢掉")
    out, _ = render([
        (E.TOOL_START, {"call_id": "c4", "name": "fetch", "activity": "拉取"}),
    ])
    check("打出一行提示", "没有收到结束事件" in out, out)
    check("仍带工具名", "fetch" in out)


# ---------------------------------------------------------------- 回复

def test_say_then_task_end_does_not_duplicate() -> None:
    print("\nsay 之后 task.end 不重复同一段话")
    text = "3 处失败都指向同一个原因。"
    out, _ = render([
        (E.SAY, {"turn": 1, "text": text}),
        (E.TASK_END, {"ok": True, "response": text}),
    ])
    check("只出现一次", out.count("3 处失败都指向同一个原因") == 1, out)


def test_task_end_falls_back_when_no_say() -> None:
    print("\n没有 say 时 task.end 的 response 要兜底打出来")
    out, _ = render([
        (E.TASK_END, {"ok": False, "response": "api 密钥错误，请检查配置"}),
    ])
    check("兜底生效", "api 密钥错误" in out, out)


def test_empty_say_is_silent() -> None:
    print("\n空 say 不产生输出")
    out, _ = render([(E.SAY, {"turn": 1, "text": "   "})])
    check("输出为空", not out.strip(), out)


def test_turn_start_resets_fallback() -> None:
    print("\n新一轮开始时，上一轮的 say 不再挡住兜底")
    out, _ = render([
        (E.SAY, {"turn": 1, "text": "第一轮的话"}),
        (E.TURN_START, {"turn": 2}),
        (E.TASK_END, {"ok": False, "response": "第二轮的错误说明"}),
    ])
    check("第二轮兜底被打出", "第二轮的错误说明" in out, out)


# ---------------------------------------------------------------- 错误

def test_error_line() -> None:
    print("\n错误事件：一行红字，带 stage")
    out, _ = render([
        (E.ERROR, {"stage": "AgentLoopStage", "message": "AuthenticationError: 401\n第二行"}),
    ])
    ls = lines_of(out)
    check("只占一行", len(ls) == 1, str(ls))
    check("带 stage", "AgentLoopStage" in ls[0])
    check("不带第二行", "第二行" not in out)


def test_unknown_event_is_ignored() -> None:
    print("\n不认识的事件忽略，不报错")
    out, _ = render([("brand.new.event", {"whatever": 1})])
    check("无输出", not out.strip())


# ---------------------------------------------------------------- 主

def test_http_fallback_and_task_end_do_not_duplicate() -> None:
    """HTTP 返回值与 task.end 是并发两条路，谁先到不固定。

    线上出现过的形态：`--once` 里 HTTP 先返回，于是兜底打一次；随后事件流送来
    `task.end.response`，又打一次。两个入口各判各的标记就挡不住 ——
    必须共用 `said()` 那一份"上一次说过什么"。
    """
    print("\n-- HTTP 兜底与 task.end 只出一份")
    buf = io.StringIO()
    console = Console(file=buf, width=88, force_terminal=False, legacy_windows=False)
    r = ScrollbackRenderer(console)

    # HTTP 先到（模拟 `_send` 里的兜底）
    r.feed(E.Event(type=E.TURN_START, data={}))
    first = r.said("好的，已经改好了。")
    # 事件流后到，文本相同
    second = r.feed(E.Event(type=E.TASK_END, data={"response": "好的，已经改好了。"}))

    text = buf.getvalue()
    check("HTTP 兜底先到时应当输出", first is True)
    check("同一段文本的 task.end 不再输出", second is False)
    check("正文只出现一次", text.count("已经改好了") == 1, f"实际 {text.count('已经改好了')} 次")

    # 反过来：事件流先到，HTTP 后到
    print("-- 反过来也一样")
    buf2 = io.StringIO()
    console2 = Console(file=buf2, width=88, force_terminal=False, legacy_windows=False)
    r2 = ScrollbackRenderer(console2)
    r2.feed(E.Event(type=E.TURN_START, data={}))
    r2.feed(E.Event(type=E.SAY, data={"text": "换个方向再试一次。"}))
    again = r2.said("换个方向再试一次。")
    check("say 之后的 HTTP 兜底不再输出", again is False)
    check("正文只出现一次（反序）", buf2.getvalue().count("换个方向再试一次") == 1)


def test_http_fallback_still_works_alone() -> None:
    """事件流一条都没到时，兜底仍要能打出来。"""
    print("\n-- 只有 HTTP 返回值时兜底照常")
    buf = io.StringIO()
    console = Console(file=buf, width=88, force_terminal=False, legacy_windows=False)
    r = ScrollbackRenderer(console)
    r.feed(E.Event(type=E.TURN_START, data={}))
    check("say 缺席时兜底输出", r.said("只有这一段。") is True)
    check("内容确实写出来了", "只有这一段" in buf.getvalue())
    check("空文本不输出", r.said("   ") is False)


def main() -> int:
    test_parse()
    test_value_helpers()
    test_tool_card_pairs_start_and_end()
    test_tool_card_failure()
    test_tool_card_without_activity()
    test_unclosed_tool_is_reported()
    test_say_then_task_end_does_not_duplicate()
    test_task_end_falls_back_when_no_say()
    test_empty_say_is_silent()
    test_turn_start_resets_fallback()
    test_http_fallback_and_task_end_do_not_duplicate()
    test_http_fallback_still_works_alone()
    test_error_line()
    test_unknown_event_is_ignored()

    print("\n" + "=" * 54)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  -", f)
    print(f"{_PASS + _FAIL} checks: {_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
