# -*- coding: utf-8 -*-
"""`core/tool_program.py` 的机械读者。

钉住：

1. 子进程 + 桥真的跑起来：打印内容与 `result` 变量都能回来。
2. `tool()` 调用经父进程执行；结果回给脚本。
3. 只读/非破坏才能调；写入被拒且给路线。
4. 超时杀进程并如实说；输出有上限。
5. 工具抛的异常变成脚本里可捕获的错误。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_tool_program.py
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import tool_program as TP  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


def test_plain_program():
    out = TP.run_program("print('hello')\nresult = 1 + 1")
    check("打印内容回来了", "hello" in out, out)
    check("result 回来了", "result = 2" in out, out)
    check("调用次数如实", "工具调用 0 次" in out, out)


def test_tool_call_round_trip():
    old_allowed, old_call = TP._tool_allowed, TP._call_tool
    old_exists = TP._tool_exists
    try:
        TP._tool_exists = lambda name: True
        TP._tool_allowed = lambda name: True
        TP._call_tool = lambda name, args: "GOT:%s:%s" % (name, args.get("x"))
        out = TP.run_program("v = tool('fake', x=7)\nprint(v)")
    finally:
        TP._tool_allowed, TP._call_tool = old_allowed, old_call
        TP._tool_exists = old_exists
    check("★ tool() 经父进程执行并回给脚本", "GOT:fake:7" in out, out)
    check("调用次数计数", "工具调用 1 次" in out, out)


def test_write_tool_is_refused():
    old_allowed, old_call = TP._tool_allowed, TP._call_tool
    old_exists = TP._tool_exists
    try:
        TP._tool_exists = lambda name: True
        TP._tool_allowed = lambda name: name != "write_file"
        TP._call_tool = lambda name, args: "SHOULD-NOT-RUN"
        out = TP.run_program("tool('write_file', file_path='x', content='y')")
    finally:
        TP._tool_allowed, TP._call_tool = old_allowed, old_call
        TP._tool_exists = old_exists
    check("★ 写入被拒并说明路线",
          "不能在脚本里调" in out and "写入请直接调用" in out, out)
    check("被拒的调用没有执行", "SHOULD-NOT-RUN" not in out, out)


def test_tool_error_is_catchable():
    old_allowed, old_call = TP._tool_allowed, TP._call_tool
    old_exists = TP._tool_exists
    try:
        TP._tool_exists = lambda name: True
        TP._tool_allowed = lambda name: True

        def boom(name, args):
            raise RuntimeError("炸了")
        TP._call_tool = boom
        out = TP.run_program(
            "try:\n    tool('x')\nexcept Exception as e:\n    print('caught:' + str(e))")
    finally:
        TP._tool_allowed, TP._call_tool = old_allowed, old_call
        TP._tool_exists = old_exists
    check("工具异常在脚本里可捕获", "caught:" in out and "炸了" in out, out)


def test_unknown_tool_says_so():
    """不存在的工具与"存在但不能调"是两句话 —— 合并就是对模型撒谎。"""
    old_allowed, old_call = TP._tool_allowed, TP._call_tool
    old_exists = TP._tool_exists
    try:
        TP._tool_exists = lambda name: False
        TP._tool_allowed = lambda name: False
        TP._call_tool = lambda name, args: "SHOULD-NOT-RUN"
        out = TP.run_program("tool('typo_name')")
    finally:
        TP._tool_allowed, TP._call_tool = old_allowed, old_call
        TP._tool_exists = old_exists
    check("不存在的工具说'没有这个工具'", "没有这个工具" in out, out)
    check("不存在的工具不说'不能在脚本里调'", "不能在脚本里调" not in out, out)
    check("不存在的工具给搜索路线", "search_tools" in out, out)


def test_non_ascii_round_trip():
    """非 ASCII 结果不能被两端编码拆坏。

    子进程按 `-I` 跑，`PYTHONIOENCODING` 被忽略，不显式钉 UTF-8 时，
    中文结果会按控制台编码（cp936）解错，而症状是 `JSONDecodeError`。
    """
    old_allowed, old_call = TP._tool_allowed, TP._call_tool
    old_exists = TP._tool_exists
    try:
        TP._tool_exists = lambda name: True
        TP._tool_allowed = lambda name: True
        # 这条载荷的形状是实测里真正把 JSON 拆坏的那一条：中文、反引号、
        # 全角括号混排，按控制台编码解码时会把后面的 ASCII 字符吞进双字节。
        payload = ('{"note": "根是 `D:/x/副本/workspace`（扫了 461 个条目）。"}')
        TP._call_tool = lambda name, args: payload
        out = TP.run_program("v = tool('fake')\nprint(v)")
    finally:
        TP._tool_allowed, TP._call_tool = old_allowed, old_call
        TP._tool_exists = old_exists
    check("★ 非 ASCII 结果原样回来", payload in out, "len=%d" % len(out))


def test_tools_lists_what_can_be_called():
    """`tools()` 的清单必须与执行判定一致 —— 两处各算一次就会漂。"""
    old_all, old_names = TP._tool_allowed, TP._all_tool_names
    try:
        TP._all_tool_names = lambda: ["read_file", "grep", "write_file"]
        TP._tool_allowed = lambda n: n != "write_file"
        out = TP.run_program("print(tools())")
    finally:
        TP._tool_allowed, TP._all_tool_names = old_all, old_names
    check("★ tools() 列出可调的工具名",
          "read_file" in out and "grep" in out, out)
    check("★★ 写工具不在清单里（清单与执行同一个判定）",
          "write_file" not in out, out)
    check("★ 查询不算一次工具调用", "工具调用 0 次" in out, out)


def test_tools_pattern_filters():
    old_all, old_names = TP._tool_allowed, TP._all_tool_names
    try:
        TP._all_tool_names = lambda: ["grep", "read_file", "memory_read"]
        TP._tool_allowed = lambda n: True
        out = TP.run_program("print(tools('mem'))")
    finally:
        TP._tool_allowed, TP._all_tool_names = old_all, old_names
    check("★ tools('mem') 只回匹配的",
          "memory_read" in out and "read_file" not in out, out)


def test_blocked_owner_is_not_callable():
    """被隔离 / 被停用的归属，脚本里也调不动（`run_program` 绕过呈现闸）。"""
    from bus import registry as R
    R.register_tool("read_zz_probe", lambda **kw: "x",
                    {"name": "read_zz_probe", "description": "",
                     "inputSchema": {"type": "object", "properties": {}}},
                    owner="skill:zz_probe")
    old = R.quarantined_owners()
    try:
        check("登记之后是可调的", TP._tool_allowed("read_zz_probe") is True, "")
        R.set_quarantined({"skill:zz_probe"})
        check("★★ 归属被隔离 -> 不可调", TP._tool_allowed("read_zz_probe") is False, "")
    finally:
        R.set_quarantined(old)
        R.unregister_owner("skill:zz_probe")


def test_timeout_and_limits():
    out = TP.run_program("import time\ntime.sleep(5)", timeout_ms=1000)
    check("★ 超时杀进程并出声", "超时" in out, out)
    check("空脚本被拒", TP.run_program("").startswith("错误：code 是空的"))

    old = TP._MAX_OUTPUT_CHARS
    TP._MAX_OUTPUT_CHARS = 100
    try:
        out = TP.run_program("print('x' * 500)")
    finally:
        TP._MAX_OUTPUT_CHARS = old
    check("输出超过上限被截断且说明", "输出被截断" in out, out[:200])


def test_child_env_is_whitelisted():
    """凭据不进入子进程的环境（`SECURITY.md` 的那句承诺）。

    真实子进程验：父进程里塞进三个典型凭据名，脚本里 `os.environ` 读它们。
    `-I` 是隔离运行选项，不是环境清洗器 —— 这条测试钉的就是这个区别。
    """
    import os

    keys = {
        "DEEPSEEK_API_KEY": "sk-test-should-not-leak",
        "FEISHU_APP_SECRET": "secret-should-not-leak",
        "HTTPS_PROXY": "http://user:pass@127.0.0.1:9",
    }
    old = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update(keys)
        out = TP.run_program(
            "import os\n"
            "print('K1=' + repr(os.environ.get('DEEPSEEK_API_KEY')))\n"
            "print('K2=' + repr(os.environ.get('FEISHU_APP_SECRET')))\n"
            "print('K3=' + repr(os.environ.get('HTTPS_PROXY')))\n"
            "print('PATHOK=' + str(bool(os.environ.get('PATH') or os.environ.get('Path'))))\n"
            "print('TEMPOK=' + str(bool(os.environ.get('TEMP') or os.environ.get('TMP'))))\n")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    check("DEEPSEEK_API_KEY 没进子进程", "K1=None" in out, out)
    check("FEISHU_APP_SECRET 没进子进程", "K2=None" in out, out)
    check("HTTPS_PROXY 没进子进程", "K3=None" in out, out)
    check("PATH 仍然可用", "PATHOK=True" in out, out)
    check("TEMP 仍然可用", "TEMPOK=True" in out, out)


def main():
    for fn in (test_plain_program,
               test_tool_call_round_trip,
               test_write_tool_is_refused,
               test_tool_error_is_catchable,
               test_unknown_tool_says_so,
               test_non_ascii_round_trip,
               test_tools_lists_what_can_be_called,
               test_tools_pattern_filters,
               test_blocked_owner_is_not_callable,
               test_child_env_is_whitelisted,
               test_timeout_and_limits):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
