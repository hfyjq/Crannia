"""剪贴板取图：Linux 那条路的选择与解析。

WSL 与服务器上通常没装 `wl-clipboard` / `xclip`，所以这里不碰真剪贴板 ——
把 `shutil.which` 与 `subprocess.run` 换掉，钉四件事：

1. 会话类型决定用哪个工具，且参数里带 `--no-newline`（少了它 PNG 结尾多一个换行）；
2. 有图时给的是 `data:image/png;base64,`，且 base64 能原样解回那些字节；
3. "剪贴板里没有图"不是错误（工具那几种说法 + 静默退出 1）；
4. 真失败（一个工具都没装、超时、协议错误）必须抛，且消息能指路。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import base64
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import clipboard as C  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


PNG = b"\x89PNG\r\n\x1a\n" + b"fake-body"


class patched:
    """临时换掉模块级依赖：平台、会话变量、which、run。

    会话变量整份换掉（而不是只 set 几个），这样"没有 WAYLAND_DISPLAY"这种
    情形才测得准 —— 本机可能真的有那个变量。
    """

    def __init__(self, *, platform: str, env: dict, which, run) -> None:
        self.platform = platform
        self.env = env
        self.which = which
        self.run = run

    def __enter__(self):
        self._saved = (C.sys.platform, dict(C.os.environ), C.shutil.which,
                       C.subprocess.run)
        C.sys.platform = self.platform
        C.os.environ.clear()
        C.os.environ.update(self.env)
        C.shutil.which = self.which
        C.subprocess.run = self.run
        return self

    def __exit__(self, *_exc) -> None:
        plat, env, which, run = self._saved
        C.sys.platform = plat
        C.os.environ.clear()
        C.os.environ.update(env)
        C.shutil.which = which
        C.subprocess.run = run


def have(*names):
    """只认这几个可执行文件，别的都当没装。"""
    return lambda name: ("/usr/bin/" + name) if name in names else None


def responder(stdout: bytes, returncode: int = 0, stderr: bytes = b""):
    """假 run：记下收到的命令，回一份固定结果。"""
    seen: list[list[str]] = []

    def run(cmd, **_kwargs):
        seen.append(list(cmd))
        return types.SimpleNamespace(stdout=stdout, stderr=stderr,
                                     returncode=returncode)

    return run, seen


def test_wayland_picks_wl_paste() -> None:
    run, seen = responder(PNG)
    with patched(platform="linux", env={"WAYLAND_DISPLAY": "wayland-0"},
                 which=have("wl-paste", "xclip"), run=run):
        got = C.read_image()
    check(seen and seen[0][0] == "wl-paste", f"Wayland 应选 wl-paste：{seen}")
    check("--no-newline" in seen[0],
          f"必须带 --no-newline（否则二进制尾巴多个换行）：{seen[0]}")
    check("image/png" in seen[0], f"类型应是 image/png：{seen[0]}")
    check(got is not None and got.startswith("data:image/png;base64,"),
          f"应是 data URL：{str(got)[:40]}")


def test_x11_picks_xclip() -> None:
    run, seen = responder(PNG)
    with patched(platform="linux", env={"DISPLAY": ":0"}, which=have("xclip"),
                 run=run):
        got = C.read_image()
    check(seen and seen[0][0] == "xclip", f"X11 应选 xclip：{seen}")
    check("image/png" in seen[0], f"类型应是 image/png：{seen[0]}")
    check(got is not None, "有图不该返回 None")


def test_base64_round_trip() -> None:
    run, _seen = responder(PNG)
    with patched(platform="linux", env={"WAYLAND_DISPLAY": "wayland-0"},
                 which=have("wl-paste"), run=run):
        got = C.read_image()
    payload = got.split(",", 1)[1]
    check(base64.b64decode(payload) == PNG, "base64 应能原样解回那些字节")


def test_no_image_is_not_an_error() -> None:
    run, _seen = responder(b"", 1, b"No suitable type of content copied")
    with patched(platform="linux", env={"WAYLAND_DISPLAY": "wayland-0"},
                 which=have("wl-paste"), run=run):
        got = C.read_image()
    check(got is None, f"剪贴板里没图应给 None，实际 {got!r}")


def test_silent_exit_1_is_not_an_error() -> None:
    run, _seen = responder(b"", 1, b"")
    with patched(platform="linux", env={"WAYLAND_DISPLAY": "wayland-0"},
                 which=have("wl-paste"), run=run):
        got = C.read_image()
    check(got is None, f"静默退出 1 也当没图，实际 {got!r}")


def test_missing_tools_says_what_to_install() -> None:
    run, seen = responder(PNG)
    with patched(platform="linux", env={}, which=have(), run=run):
        try:
            C.read_image()
        except C.ClipboardError as exc:
            msg = str(exc)
            check("wl-clipboard" in msg, f"消息要提到 wl-clipboard：{msg}")
            check("xclip" in msg, f"消息要提到 xclip：{msg}")
        else:
            check(False, "一个工具都没装时必须抛")
    check(not seen, "没工具时不该真去起子进程")


def test_timeout_is_reported() -> None:
    def run(_cmd, **_kwargs):
        raise C.subprocess.TimeoutExpired(cmd="wl-paste", timeout=20)

    with patched(platform="linux", env={"WAYLAND_DISPLAY": "wayland-0"},
                 which=have("wl-paste"), run=run):
        try:
            C.read_image()
        except C.ClipboardError as exc:
            check("超时" in str(exc), f"应说是超时：{exc}")
        else:
            check(False, "超时必须抛")


def test_protocol_error_is_reported() -> None:
    run, _seen = responder(b"", 1, b"Error: Cannot open display :0")
    with patched(platform="linux", env={"DISPLAY": ":0"}, which=have("xclip"),
                 run=run):
        try:
            C.read_image()
        except C.ClipboardError as exc:
            check("display" in str(exc).lower(), f"应把工具原话带出来：{exc}")
        else:
            check(False, "协议错误必须抛，不许当成没图")


def test_other_platform_is_refused() -> None:
    run, seen = responder(PNG)
    with patched(platform="darwin", env={}, which=have("wl-paste"), run=run):
        try:
            C.read_image()
        except C.ClipboardError as exc:
            check("Windows" in str(exc) and "Linux" in str(exc),
                  f"消息要说清支持哪些平台：{exc}")
        else:
            check(False, "别的平台必须抛")
    check(not seen, "别的平台不该起子进程")


def main() -> int:
    for fn in (test_wayland_picks_wl_paste,
               test_x11_picks_xclip,
               test_base64_round_trip,
               test_no_image_is_not_an_error,
               test_silent_exit_1_is_not_an_error,
               test_missing_tools_says_what_to_install,
               test_timeout_is_reported,
               test_protocol_error_is_reported,
               test_other_platform_is_refused):
        fn()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
