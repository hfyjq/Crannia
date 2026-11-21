"""命令分派的锁。

这一层没有 UI 也没有网络，纯字符串判断 —— 所以断言可以写得很死。
分派错了不会报错，只会"把一句命令当成聊天发出去"，表现是一次莫名其妙的回答，
在交互界面里几乎不可能被发现。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.cells import cell_len

from crannia_tui.commands import parse_command, short_path

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


def test_exit() -> None:
    print("\n退出：带不带斜杠都认")
    for s in ("/exit", "/quit", "exit", "quit", "EXIT", "  /exit  "):
        check(f"{s!r} -> exit", parse_command(s)[0] == "exit", str(parse_command(s)))


def test_local_commands() -> None:
    print("\n本地命令：不发给后端")
    check("/help 已取消（走聊天）", parse_command("/help")[0] == "chat")
    check("/cls -> cls", parse_command("/cls")[0] == "cls")
    check("/new -> new", parse_command("/new")[0] == "new")
    check("大写也认", parse_command("/EXIT")[0] == "exit")


def test_commands_with_arguments_go_to_chat() -> None:
    print("\n命令后面带东西的，当聊天内容发出去（不吞掉）")
    action, payload = parse_command("/help 怎么用")
    check("不判成 help", action == "chat", action)
    check("原文保留", payload == "/help 怎么用", payload)


def test_plain_text() -> None:
    print("\n普通输入：原样当 payload")
    action, payload = parse_command("帮我看下这个报错")
    check("action 是 chat", action == "chat")
    check("payload 保留原文", payload == "帮我看下这个报错")
    check("首尾空白被裁", parse_command("  你好  ")[1] == "你好")


def test_empty() -> None:
    print("\n空输入：判成 chat、payload 为空，由调用方跳过")
    check("空串", parse_command("") == ("chat", ""))
    check("全空格", parse_command("   ") == ("chat", ""))


def test_similar_but_not_commands() -> None:
    print("\n形似命令但不是的，不许被吞")
    for s in ("/exitnow", "exit please", "/help!", "/hello"):
        check(f"{s!r} -> chat", parse_command(s)[0] == "chat", str(parse_command(s)))


def test_short_path_never_overflows() -> None:
    """抬头里的路径必须压进给定列数。

    按显示列算而不是字符数：中文目录名一个字符占两列，按字符数切出来的
    字符串会超宽、折行，抬头那三行的缩进就全歪了。
    """
    print("\n路径截断")
    cases = [
        r"D:\work\crannia",
        r"D:\work\some-project\src\deeply\nested\package",
        r"D:\中文目录\另一个中文目录\项目",
        r"C:\a",
        r"D:\one",
    ]
    for p in cases:
        for limit in (80, 40, 24, 12):
            out = short_path(p, limit)
            check(f"{limit} 列内 {p[-14:]!r}", cell_len(out) <= limit,
                  f"{out!r} 宽 {cell_len(out)}")

    # 真截断过的要留省略号，否则看不出"这里被我掐了"
    long_p = r"D:\work\some-project\src\deeply\nested\package"
    check("掐过的带省略号", "…" in short_path(long_p, 20))
    # 没超限的原样返回，不要无缘无故改写用户看得懂的路径
    check("不超限就原样", short_path(r"D:\a\b", 80) == r"D:\a\b")


def main() -> int:
    test_exit()
    test_local_commands()
    test_commands_with_arguments_go_to_chat()
    test_plain_text()
    test_empty()
    test_similar_but_not_commands()
    test_short_path_never_overflows()

    print("\n" + "=" * 54)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  -", f)
    print(f"{_PASS + _FAIL} checks: {_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
