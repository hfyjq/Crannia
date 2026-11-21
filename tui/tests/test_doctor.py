"""三个只读命令的断言：判据能注入、输出与退出码对得上、后端没被启动。

子进程口径在这里一律用假实现顶掉 —— 测试不依赖开发机上恰好装了哪份 crannia。
测试进程的输出仍一律 ASCII；中文只出现在被捕获的 doctor 行里。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import __main__ as M  # noqa: E402
from crannia_tui import doctor as D  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def make_home(root: Path) -> Path:
    home = root / "crannia-home"
    home.mkdir()
    (home / "main.py").write_text("", encoding="utf-8")
    (home / "core").mkdir()
    (home / "web").mkdir()
    return home


class CountingBackend:
    """顶掉真 Backend：只要被构造或启动，就是命令分流错了。"""

    starts = 0
    made = 0

    def __init__(self, crannia, python, port=None) -> None:
        CountingBackend.made += 1
        self.crannia, self.python, self.port = crannia, python, port

    def start(self) -> None:
        CountingBackend.starts += 1


def test_doctor_all_pass() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = make_home(root)
        (root / "data").mkdir()
        (root / "data" / "settings.json").write_text("{}", encoding="utf-8")
        code, lines = D.doctor(home=home, python=sys.executable,
                               data_root_fn=lambda: root, port=54321,
                               port_probe=lambda p: True,
                               skill_warnings_fn=lambda: [],
                               backend_fn=lambda: None,
                               key_fn=lambda: {"id": "c1", "name": "X",
                                              "has_key": True})
        out = "\n".join(lines)
        check(code == 0, f"全通过应返回 0，实际 {code}：{out}")
        for marker in ("[ok ] crannia 根", "[ok ] 解释器", "[ok ] 数据根",
                       "[ok ] 设置文件", "[ok ] 端口：54321 空闲"):
            check(marker in out, f"缺输出行 {marker}：{out}")
        check("[FAIL]" not in out and "[warn]" not in out, "全通过不该有失败或告警")
        check(not list(root.glob(D._PROBE_PREFIX + "*")), "探针文件必须删掉")


def test_doctor_missing_settings_is_warn() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = make_home(root)
        code, lines = D.doctor(home=home, python=sys.executable,
                               data_root_fn=lambda: root,
                               skill_warnings_fn=lambda: [],
                               backend_fn=lambda: None,
                               key_fn=lambda: {"id": "c1", "name": "X",
                                              "has_key": True})
        out = "\n".join(lines)
        check(code == 0, f"尚未播种不算失败，实际 {code}：{out}")
        check("尚未播种" in out, f"应有一行 warn：{out}")
        check("[warn]" in out, f"warn 要看得出来：{out}")
        check("由系统分配" in out, f"未传 --port 要说清：{out}")


def test_doctor_warns_about_missing_skill_config() -> None:
    """技能声明了配置、却没配：只报 WARN，不挡启动、不改变退出码。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = make_home(root)
        (root / "data").mkdir()
        (root / "data" / "settings.json").write_text("{}", encoding="utf-8")
        code, lines = D.doctor(
            home=home, python=sys.executable, data_root_fn=lambda: root,
            skill_warnings_fn=lambda: [{
                "skill": "github", "scope": "github",
                "where": "data/settings.json · skill_configs.github"}],
            backend_fn=lambda: None,
            key_fn=lambda: {"id": "c1", "name": "X", "has_key": True})
        out = "\n".join(lines)
        check(code == 0, f"WARN 不该改变退出码，实际 {code}：{out}")
        check("[warn] 技能配置：github 需要 github" in out, out)
        check("skill_configs.github" in out, "要说清去哪里配：" + out)


def test_doctor_hard_failures() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = make_home(root)
        (root / "data").mkdir()
        (root / "data" / "settings.json").write_text("{坏 JSON",
                                                      encoding="utf-8")
        # 1) 数据根不存在：硬失败，别项不再查
        code, lines = D.doctor(home=home, python=sys.executable,
                               data_root_fn=lambda: root / "没有这个目录",
                               skill_warnings_fn=lambda: [],
                               backend_fn=lambda: None,
                               key_fn=lambda: {"id": "c1", "name": "X",
                                              "has_key": True})
        out = "\n".join(lines)
        check(code == 1, f"数据根不存在应返回 1，实际 {code}：{out}")
        check("[FAIL] 数据根" in out, f"应红在数据根：{out}")

        # 2) 设置文件坏 JSON：硬失败
        (root / "data" / "settings.json").write_text("{坏 JSON",
                                                      encoding="utf-8")
        code, lines = D.doctor(home=home, python=sys.executable,
                               data_root_fn=lambda: root,
                               skill_warnings_fn=lambda: [],
                               backend_fn=lambda: None,
                               key_fn=lambda: {"id": "c1", "name": "X",
                                              "has_key": True})
        out = "\n".join(lines)
        check(code == 1, f"坏 JSON 应返回 1，实际 {code}：{out}")
        check("[FAIL] 设置文件" in out, f"应红在设置文件：{out}")

        # 3) 端口被占：只有端口红，别项照常通过
        (root / "data" / "settings.json").write_text("{}", encoding="utf-8")
        code, lines = D.doctor(home=home, python=sys.executable,
                               data_root_fn=lambda: root, port=1234,
                               port_probe=lambda p: False,
                               skill_warnings_fn=lambda: [],
                               backend_fn=lambda: None,
                               key_fn=lambda: {"id": "c1", "name": "X",
                                              "has_key": True})
        out = "\n".join(lines)
        check(code == 1, f"端口占用应返回 1，实际 {code}：{out}")
        check("[FAIL] 端口" in out, f"应红在端口：{out}")
        check("[ok ] 数据根" in out and "[ok ] 设置文件" in out,
              f"端口独占失败，别项应正常：{out}")


def test_query_contracts() -> None:
    """两个子进程查询各问各的模块，版本查不到也不算失败。"""
    codes: list[str] = []

    def fake_query(code: str) -> str:
        codes.append(code)
        return "9.8.7"

    check(D.app_version(query_fn=fake_query) == "crannia 9.8.7",
          "版本号应拼成一行")
    check(len(codes) == 1 and "core.version" in codes[0],
          f"版本要从 core.version 查：{codes}")
    check(D.app_version(query_fn=lambda c: "") == "crannia (版本未知)",
          "查不到版本应给占位文案")
    check(D.app_version(query_fn=lambda c: (_ for _ in ()).throw(OSError()))
          == "crannia (版本未知)", "子进程报错也不能让 --version 失败")

    with tempfile.TemporaryDirectory() as td:
        p = D.settings_path(data_root_fn=lambda: Path(td))
        check(p.is_absolute(), f"设置文件路径应是绝对路径：{p}")
        check(str(p).endswith("data" + os_sep() + "settings.json"),
              f"口径应是 data_root()/data/settings.json：{p}")


def os_sep() -> str:
    return "\\" if sys.platform == "win32" else "/"


def test_main_routes_without_backend() -> None:
    """三个命令都不构造、不启动后端；老用法逐字不变。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = make_home(root)
        # `__main__` 在 import 期就把这两个名字绑进了自己的命名空间，只改
        # `paths` 模块里的不够：构造 Backend 时求值的仍是 M 的那两份。
        # 不钉住它们，这个用例会依赖本机磁盘上恰好有没有 `.env` / `.venv`。
        old = (D.paths.crannia_home, D.paths.python_exe,
               D.make_data_root_query, D.app_version, D._live_endpoint,
               M.crannia_home, M.python_exe,
               M.Backend, M.run_once)
        CountingBackend.made = CountingBackend.starts = 0
        seen: dict = {}

        def fake_once(backend, text):
            seen["text"], seen["backend"] = text, backend
            return 7

        try:
            D.paths.crannia_home = lambda: home
            D.paths.python_exe = lambda: Path(sys.executable)
            M.crannia_home = lambda: home
            M.python_exe = lambda: Path(sys.executable)
            D.make_data_root_query = lambda py, h: (lambda: root)
            D.app_version = lambda: "crannia 9.8.7"
            D._live_endpoint = lambda *_a: None
            M.Backend = CountingBackend
            M.run_once = fake_once

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code_doctor = M.main(["doctor"])
                path_lines = out.getvalue().splitlines()
                out.seek(0)
                out.truncate(0)
                code_config = M.main(["config", "path"])
                path_out = out.getvalue().strip()
                out.seek(0)
                out.truncate(0)
                code_version = M.main(["--version"])
                version_out = out.getvalue().strip()
                out.seek(0)
                out.truncate(0)
                code_once = M.main(["--once", "原来的话", "--port", "1234"])
                out.seek(0)
                out.truncate(0)
                help_out = ""
                try:
                    M.main(["--help"])
                except SystemExit as exc:
                    check(exc.code == 0, "--help 应正常退出")
                help_out = out.getvalue()
        finally:
            (D.paths.crannia_home, D.paths.python_exe,
             D.make_data_root_query, D.app_version, D._live_endpoint,
             M.crannia_home, M.python_exe,
             M.Backend, M.run_once) = old

        check(code_doctor == 0, f"doctor 应全过：{path_lines}")
        check(code_config == 0, "config path 应返回 0")
        check(code_version == 0, "--version 应返回 0")
        check(code_once == 7, "--once 的返回值应原样透传")
        check(seen.get("text") == "原来的话", "原文要一个字节不变地送到 run_once")
        check(seen.get("backend") is not None and seen["backend"].port == 1234,
              "--port 仍要传进 Backend")
        check(path_out and Path(path_out).is_absolute(),
              f"config path 只该打印一行绝对路径：{path_out!r}")
        check(len(path_out.splitlines()) == 1, "config path 不该有第二行")
        check(version_out == "crannia 9.8.7", f"版本行不对：{version_out!r}")
        check(CountingBackend.made == 1 and CountingBackend.starts == 0,
              f"三个只读命令不许碰后端：made={CountingBackend.made}, "
              f"starts={CountingBackend.starts}")
        for token in ("doctor", "config path", "--version"):
            check(token in help_out, f"--help 里应看得到 {token}")


def test_doctor_reports_backend_and_key() -> None:
    """D11 里那两项：后端可达 / 密钥存在。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = make_home(root)
        (root / "data").mkdir()
        (root / "data" / "settings.json").write_text("{}", encoding="utf-8")

        def run(backend, key):
            return D.doctor(home=home, python=sys.executable,
                            data_root_fn=lambda: root,
                            skill_warnings_fn=lambda: [],
                            backend_fn=backend, key_fn=lambda: key)

        code, lines = run(lambda: {"port": 5000},
                          {"id": "c1", "name": "Deepseek",
                           "base_url": "https://api.deepseek.com/v1",
                           "has_key": True})
        out = "\n".join(lines)
        check(code == 0, f"全通过应返回 0：{out}")
        check("[ok ] 后端：已有一个在跑（端口 5000）" in out, out)
        check("[ok ] 密钥：默认配置「Deepseek」已填" in out, out)

        code, lines = run(lambda: None,
                          {"id": "c2", "name": "Local",
                           "base_url": "http://127.0.0.1:11434/v1",
                           "has_key": False})
        out = "\n".join(lines)
        check(code == 0, f"本机模型无密钥不算失败：{out}")
        check("[ok ] 后端：未运行" in out, out)
        check("[ok ] 密钥：本机模型，无需密钥" in out, out)

        code, lines = run(lambda: None,
                          {"id": "c3", "name": "Remote",
                           "base_url": "https://api.example.com/v1",
                           "has_key": False})
        out = "\n".join(lines)
        check(code == 1, f"远端的默认配置没有密钥应硬失败：{out}")
        check("[FAIL] 密钥：默认配置「Remote」没有 API Key" in out, out)

        code, lines = run(lambda: None, {"id": ""})
        out = "\n".join(lines)
        check(code == 0, f"还没配模型只是提醒：{out}")
        check("还没有默认模型配置" in out, out)


def main() -> int:
    test_doctor_all_pass()
    test_doctor_missing_settings_is_warn()
    test_doctor_warns_about_missing_skill_config()
    test_doctor_hard_failures()
    test_doctor_reports_backend_and_key()
    test_query_contracts()
    test_main_routes_without_backend()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
