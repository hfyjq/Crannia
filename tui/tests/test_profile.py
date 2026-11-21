"""`crannia profile`：列出与切换。

钉四件事：

1. 列表里默认那条有星，三列（名字 / 模型 / id）都在。
2. 引用解析：id、名字、大小写；撞名与找不到都要给出能照做的下一句。
3. 没有后端时走一次子进程查询 / 调用（参数里带的是 id，不是名字）。
4. 有后端时走它的接口，切换结果照后端的话回。

本机回环上起一个假后端，不碰真的配置。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import profile  # noqa: E402

_n = 0

ENTRIES = [
    {"id": "m11111111", "name": "deepseek", "model": "deepseek-flash",
     "is_default": True},
    {"id": "m22222222", "name": "备用", "model": "deepseek-pro",
     "is_default": False},
]


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


class _Handler(BaseHTTPRequestHandler):
    token = "tok"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                       # noqa: N802 (http.server 约定)
        if self.headers.get("X-Crannia-Token") != self.token:
            return self._send(403, {"error": "unauthorized"})
        if self.path != "/api/model-configs":
            return self._send(404, {})
        self._send(200, {
            "default": "m11111111",
            "cards": [{"id": "deepseek", "configs": [
                {"id": "m11111111", "name": "deepseek",
                 "model": "deepseek-flash", "is_default": True},
                {"id": "m22222222", "name": "备用",
                 "model": "deepseek-pro", "is_default": False},
            ]}],
        })

    def do_POST(self):                      # noqa: N802
        if self.headers.get("X-Crannia-Token") != self.token:
            return self._send(403, {"error": "unauthorized"})
        if self.path != "/api/model-configs/default":
            return self._send(404, {})
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        if data.get("id") != "m22222222":
            return self._send(400, {"ok": False, "error": "没有这条配置"})
        self._send(200, {"ok": True, "default": "m22222222",
                         "message": "已设为默认：「备用」。新对话生效；当前这个需重启。"})

    def log_message(self, *args):
        pass


def _serve() -> tuple:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, {"port": srv.server_address[1], "token": "tok"}


def test_render():
    lines = profile.render(ENTRIES)
    check(len(lines) == 2, lines)
    check(lines[0].startswith("*"), f"默认那条要有星：{lines[0]!r}")
    check("deepseek" in lines[0] and "deepseek-flash" in lines[0]
          and "m11111111" in lines[0], lines[0])
    check(lines[1].startswith(" "), f"非默认不留星：{lines[1]!r}")
    check("尚未拥有模型配置" in profile.render([])[0], profile.render([]))


def test_resolve():
    hit, why = profile._resolve(ENTRIES, "m22222222")
    check(hit is not None and hit["name"] == "备用", (hit, why))
    hit, _ = profile._resolve(ENTRIES, "DeepSeek")
    check(hit is not None and hit["id"] == "m11111111", hit)
    hit, why = profile._resolve(ENTRIES, "没有这个")
    check(hit is None and "现有" in why, why)
    clash = ENTRIES + [{"id": "m33333333", "name": "备用",
                        "model": "x", "is_default": False}]
    hit, why = profile._resolve(clash, "备用")
    check(hit is None and "撞名" in why, why)


def test_subprocess_paths():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)

        listing = json.dumps({"default": "m11111111", "entries": [
            {"id": "m11111111", "name": "deepseek", "model": "deepseek-flash"},
            {"id": "m22222222", "name": "备用", "model": "deepseek-pro"}]})

        def run_list(code):
            return listing

        entries, default = profile.read(Path("python"), home,
                                        query_fn=run_list)
        check(default == "m11111111", default)
        check(entries[0]["is_default"] is True, entries)

        seen = {}

        def run_switch(code):
            if "names_payload" in code:
                return listing
            seen["code"] = code
            return json.dumps({"reason": ""})

        ok, msg = profile.switch(Path("python"), home, "备用",
                                 query_fn=run_switch)
        check(ok and "已设为默认" in msg, msg)
        check("m22222222" in seen["code"], "切换要按 id 落盘，不是名字")

        def run_refuse(code):
            if "names_payload" in code:
                return listing
            return json.dumps({"reason": "缺依赖 socksio"})

        ok, msg = profile.switch(Path("python"), home, "备用",
                                 query_fn=run_refuse)
        check(not ok and "缺依赖" in msg, msg)


def test_live_backend_paths():
    srv, ep = _serve()
    try:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            entries, default = profile.read(Path("python"), home, ep=ep)
            check(default == "m11111111", default)
            check(any(e["name"] == "备用" for e in entries), entries)

            ok, msg = profile.switch(Path("python"), home, "备用", ep=ep)
            check(ok and "已设为默认" in msg, msg)

            ok, msg = profile.switch(Path("python"), home, "deepseek", ep=ep)
            check(ok and "已经是默认" in msg, msg)
    finally:
        srv.shutdown()
        srv.server_close()


def test_profile_command_exit_codes():
    srv, ep = _serve()
    try:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            check(profile.profile_command(None, python=Path("python"),
                                          home=home, ep=ep) == 0,
                  "列列表应当成功")
            check(profile.profile_command("没有这个", python=Path("python"),
                                          home=home, ep=ep) == 1,
                  "找不到应当回 1")
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> int:
    for fn in (test_render, test_resolve, test_subprocess_paths,
               test_live_backend_paths, test_profile_command_exit_codes):
        fn()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
