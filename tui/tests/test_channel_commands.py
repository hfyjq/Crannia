"""`crannia channel`：账本的列表 / 增删改 / 默认，以及启停与扫码。

钉四件事：

1. 列表把"默认那条"、凭据指纹与接入警告的短标记都画出来。
2. 引用解析：id、名字、撞名、找不到。
3. 配置类动作走一次子进程（TUI 不 import core），参数里带的是 id；
   接入警告要先确认，非交互环境不给确认就不动账本。
4. 启停与扫码要求后端在跑；扫码只印一次 ASCII，按相位收尾。

子进程用一个假的 `query_fn` 顶掉，不碰真的账本。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import channel as C  # noqa: E402

_n = 0

LEDGER = {
    "feishu": {
        "fields": ["app_id", "app_secret"],
        "secret": ["app_secret"],
        "default": "c11111111",
        "notice": {"key": "group_unfinished", "text": "群聊那一半还没做完。"},
        "entries": [
            {"id": "c11111111", "name": "默认", "is_default": True,
             "fields": {"app_id": "aa****bb"}, "notice": "group_unfinished"},
            {"id": "c22222222", "name": "测试", "is_default": False,
             "fields": {"app_id": "cc****dd"}, "notice": "group_unfinished"},
        ],
    },
    "telegram": {
        "fields": ["bot_token"],
        "secret": ["bot_token"],
        "default": "",
        "notice": {"key": "group_unfinished", "text": "群聊那一半还没做完。"},
        "entries": [],
    },
}


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def run_capture(fn) -> str:
    buf = io.StringIO()
    # stdin 必须换成非 TTY：真机终端和 WSL 的伪终端都会让 `isatty()` 为真，
    # 那时确认提示会停下来等键盘，这个文件就挂在这儿。
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        with contextlib.redirect_stdout(buf):
            fn()
    finally:
        sys.stdin = old_stdin
    return buf.getvalue()


def fake_query(seen: list[str], result: dict | None = None):
    """按代码里的动作分流；不认识的动作直接报错（免得断言的靶子打偏）。"""
    def run(code: str) -> str:
        seen.append(code)
        if "cc.entries_for" in code:
            return json.dumps(LEDGER, ensure_ascii=False)
        if "cc.create(" in code:
            return json.dumps({"id": "c99999999", "name": "默认",
                               "is_default": True}, ensure_ascii=False)
        if "cc.update(" in code:
            return json.dumps({"id": "c11111111", "platform": "feishu",
                               "name": "默认"}, ensure_ascii=False)
        if "cc.delete(" in code:
            return json.dumps({"ok": True}, ensure_ascii=False)
        if "cc.set_default(" in code:
            return json.dumps({"ok": True, "platform": "feishu"},
                              ensure_ascii=False)
        raise AssertionError("没有预料到的子进程调用：" + code)
    return run


def test_list_marks_default_notice_and_running() -> None:
    out = run_capture(lambda: C.channel_command(
        ["list"], python=Path("py"), home=Path("."),
        query_fn=fake_query([]), ep=None))
    check("飞书" in out or "feishu" in out, out)
    check("[群聊未完善]" in out, "平台行要挂接入警告的短标记：" + out)
    check("* 默认" in out, "默认那条要有星：" + out)
    check("c11111111" in out, "条目 id 要看得见：" + out)
    check("Telegram" in out or "telegram" in out, "没有条目的通道也要列出来")
    check("（未配置）" in out, "空通道要说明还没配置")


def test_render_running_marker() -> None:
    lines = C.render(LEDGER, {"feishu_channel": True})
    joined = "\n".join(lines)
    check("运行中" in joined, joined)


def test_resolve() -> None:
    hit, why = C._resolve(LEDGER, "c22222222")
    check(hit is not None and hit[1]["name"] == "测试", why)
    hit, why = C._resolve(LEDGER, "测试")
    check(hit is not None and hit[1]["id"] == "c22222222", why)
    hit, why = C._resolve(LEDGER, "没有这条")
    check(hit is None and "没有" in why, why)


def test_add_confirms_notice_before_touching_ledger() -> None:
    seen: list[str] = []
    out = run_capture(lambda: C.channel_command(
        ["add", "feishu"], python=Path("py"), home=Path("."),
        query_fn=fake_query(seen), ep=None))
    check("群聊那一半还没做完" in out, "配置前要把警告摆出来：" + out)
    check(not any("cc.create(" in c for c in seen),
          "没确认就不许写账本：" + repr(seen))


def test_config_and_remove_use_ids() -> None:
    seen: list[str] = []
    old_ask, old_getpass = C._ask, C.getpass.getpass
    C._ask = lambda prompt: ""
    C.getpass.getpass = lambda prompt: ""
    try:
        run_capture(lambda: C.channel_command(
            ["config", "测试", "--yes"], python=Path("py"), home=Path("."),
            query_fn=fake_query(seen), ep=None))
    finally:
        C._ask, C.getpass.getpass = old_ask, old_getpass
    calls = [c for c in seen if "cc.update(" in c]
    check(bool(calls), "配置要落一次 update：" + repr(seen))
    # 名字那格留空 = 不改：生成的是 `('' or None)`，核心看到的是 None。
    check("name=('' or None)" in calls[0], calls[0])
    check("c22222222" in calls[0], "要按 id 改，不按名字：" + calls[0])

    seen = []
    run_capture(lambda: C.channel_command(
        ["remove", "测试", "--yes"], python=Path("py"), home=Path("."),
        query_fn=fake_query(seen), ep=None))
    deletes = [c for c in seen if "cc.delete(" in c]
    check(bool(deletes), "删除要落一次 delete")
    check("c22222222" in deletes[0], deletes[0])


def test_default_and_unknown_platform() -> None:
    seen: list[str] = []
    out = run_capture(lambda: C.channel_command(
        ["default", "默认"], python=Path("py"), home=Path("."),
        query_fn=fake_query(seen), ep=None))
    check(any("cc.set_default(" in c for c in seen), repr(seen))
    check("已设为默认" in out, out)

    out = run_capture(lambda: C.channel_command(
        ["add", "没有这个平台", "--yes"], python=Path("py"), home=Path("."),
        query_fn=fake_query([]), ep=None))
    check("未知平台" in out, out)


def test_start_requires_backend() -> None:
    old = C.endpoint
    C.endpoint = lambda python, home: None
    try:
        out = run_capture(lambda: C.channel_command(
            ["start", "feishu"], python=Path("py"), home=Path("."),
            query_fn=fake_query([])))
        check("后端未启动" in out, out)
    finally:
        C.endpoint = old


def test_start_posts_channel_id() -> None:
    calls: list[tuple[str, dict]] = []
    old_ep, old_post = C.endpoint, C._post
    C.endpoint = lambda python, home: {"port": 1, "token": "t"}
    C._post = lambda ep, path, payload: (calls.append((path, payload))
                                         or (True, "", {"status": "started"}))
    try:
        out = run_capture(lambda: C.channel_command(
            ["start", "feishu"], python=Path("py"), home=Path("."),
            query_fn=fake_query([])))
        check(calls == [("/api/channel/start",
                         {"channel_id": "feishu_channel"})], repr(calls))
        check("started" in out or "已启动" in out, out)
    finally:
        C.endpoint, C._post = old_ep, old_post


def test_login_prints_ascii_once_and_finishes() -> None:
    phases = [
        {"phase": "qr_ready", "ascii": "QR-ART\n"},
        {"phase": "qr_ready", "ascii": "QR-ART\n"},
        {"phase": "scanned"},
        {"phase": "confirmed"},
    ]
    old_post, old_get, old_sleep = C._post, C._get, C.time.sleep
    C._post = lambda ep, path, payload: (True, "", {})
    C._get = lambda ep, path: phases.pop(0) if phases else {"phase": "confirmed"}
    C.time.sleep = lambda s: None
    try:
        out = run_capture(lambda: C.channel_command(
            ["login", "wechat"], python=Path("py"), home=Path("."),
            ep={"port": 1, "token": "t"}))
        check(out.count("QR-ART") == 1, "二维码只印一次：" + out)
        check("请使用手机微信扫码继续" in out, out)
        check("微信已登录" in out, out)
    finally:
        C._post, C._get, C.time.sleep = old_post, old_get, old_sleep


def main() -> int:
    for fn in [test_list_marks_default_notice_and_running,
               test_render_running_marker,
               test_resolve,
               test_add_confirms_notice_before_touching_ledger,
               test_config_and_remove_use_ids,
               test_default_and_unknown_platform,
               test_start_requires_backend,
               test_start_posts_channel_id,
               test_login_prints_ascii_once_and_finishes]:
        fn()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
