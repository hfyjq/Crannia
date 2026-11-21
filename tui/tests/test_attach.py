"""接入已有后端：端点文件、判活、版本握手。

钉四件事：

1. 端点文件的读法与 `core/endpoint.py` 同形（坏的一律 None）。
2. 有活端点就接上去，不 spawn 第二个后端（同一数据根只许一个）。
3. 版本不一致是硬错，不"退回自己起" —— 那会在同一个数据根上加第二个后端。
4. 端口关了 / 没有文件就当作没人，走原有启动路径。

用本机回环上的临时 HTTP 服务造真实应答，不依赖真后端。

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

from crannia_tui.backend import Backend, BackendError  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


class _Status(BaseHTTPRequestHandler):
    token = "tok"
    version = "1.0.0"

    def do_GET(self):                       # noqa: N802 (http.server 约定)
        if self.path != "/api/status":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("X-Crannia-Token") != self.token:
            self.send_response(403)
            self.end_headers()
            return
        body = json.dumps({"version": self.version, "framework_running": True})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


def _serve() -> tuple:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Status)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _write_endpoint(root: Path, port: int, token: str = "tok",
                    version: str = "1.0.0") -> None:
    path = root / "data" / "runtime" / "endpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"port": port, "token": token,
                                "pid": 4242, "version": version}),
                    encoding="utf-8")


def _backend(root: Path, expect: str = "1.0.0") -> Backend:
    return Backend(Path("."), Path(sys.executable),
                   data_root=root, expect_version=expect)


def test_attach_to_live_backend():
    srv, port = _serve()
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_endpoint(root, port)
            b = _backend(root)
            b.start()
            check(b._proc is None, "接上去时不该有子进程")
            check(b.port == port, f"端口应取自端点文件，实际 {b.port}")
            check(b.token == "tok", f"令牌应取自端点文件，实际 {b.token!r}")
            check(b.base == f"http://127.0.0.1:{port}", b.base)
    finally:
        srv.shutdown()
        srv.server_close()


def test_version_mismatch_is_hard_error():
    srv, port = _serve()
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_endpoint(root, port)
            b = _backend(root, expect="9.9.9")
            try:
                b.start()
            except BackendError as exc:
                check("升级" in str(exc), f"报错要说清怎么办，实际 {exc}")
                check("9.9.9" in str(exc) and "1.0.0" in str(exc),
                      f"报错要带两边的版本，实际 {exc}")
            else:
                raise AssertionError("版本不一致必须拒绝，不能静默接上")
            check(b._proc is None, "拒绝之后也不许偷偷 spawn")
    finally:
        srv.shutdown()
        srv.server_close()


def test_no_endpoint_means_none():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check(_backend(root)._attach() is None, "没有端点文件 -> None")
        _write_endpoint(root, 1, token="")   # 字段不像样
        check(_backend(root)._attach() is None, "空令牌 -> None")
        (root / "data" / "runtime" / "endpoint.json").write_text(
            "{not json", encoding="utf-8")
        check(_backend(root)._attach() is None, "坏 JSON -> None")


def test_dead_endpoint_means_none():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_endpoint(root, 9)             # 9 号端口上没人
        check(_backend(root)._attach() is None, "端口关了 -> None")


def main() -> int:
    for fn in (test_attach_to_live_backend, test_version_mismatch_is_hard_error,
               test_no_endpoint_means_none, test_dead_endpoint_means_none):
        fn()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
