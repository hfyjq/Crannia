# -*- coding: utf-8 -*-
"""`run_program` 的子进程侧：读一段 Python，暴露 `tool()` 与 `tools()`。

这个文件不 import 本仓库任何模块（`-I` 隔离运行）。
协议：stdout 每一行一个 JSON 对象（`call` / `done`），stdin 收每个调用的回复。
用户代码的 `print` 被收进缓冲区，最后随 `done` 一起回给父进程。
"""

import io
import json
import sys


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        code = fh.read()

    # 协议两端都是 UTF-8。子进程按 `-I` 跑，`PYTHONIOENCODING` 被忽略，
    # 控制台默认编码（中文 Windows 是 cp936）会把两端都解错：一个中文字符串
    # 在 cp936 下可能吞掉紧跟其后的 ASCII 引号或括号，报出来的是
    # `JSONDecodeError: Extra data`，而真正的原因与 JSON 无关。
    real = sys.stdout
    for stream in (sys.stdin, real, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    def send(obj) -> None:
        real.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real.flush()

    def call(name=None, **kwargs):
        if not name:
            raise ValueError("tool() 需要一个工具名")
        send({"type": "call", "name": str(name), "args": kwargs})
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("框架连接断了")
        msg = json.loads(line)
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error") or "工具调用失败")
        return msg.get("result")

    def tools(pattern=""):
        """脚本里能调的工具名（只读与 MCP）；`pattern` 按子串过滤。"""
        send({"type": "list", "pattern": str(pattern or "")})
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("框架连接断了")
        msg = json.loads(line)
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error") or "取工具清单失败")
        return list(msg.get("result") or [])

    buf = io.StringIO()
    sys.stdout = buf
    sys.stderr = buf
    ns = {"__name__": "__main__", "tool": call, "tools": tools}
    ok, err, result = True, "", None
    try:
        exec(compile(code, "<program>", "exec"), ns)
        result = ns.get("result")
    except Exception as exc:                           # noqa: BLE001
        ok = False
        err = "%s: %s" % (type(exc).__name__, exc)
    finally:
        sys.stdout = real
        sys.stderr = sys.__stderr__
    send({"type": "done", "ok": ok, "error": err,
          "output": buf.getvalue(), "result": repr(result)[:4000]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
