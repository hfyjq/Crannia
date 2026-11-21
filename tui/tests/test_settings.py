"""TUI 设置白名单与启动处置的断言。

钉五件事：

1. 只读白名单那一列 —— 其余键存在但不读。
2. 坏值回默认、并**报出来**；`true` 不许被当成整数（`isinstance(True, int)`）。
3. 文件读不出来时 `read()` 保持安静 —— 文件层面的处置归 `ensure()`，不重复报。
4. `ensure()` 三种报告（播种 / 备份重建 / 失败）都变成一行人话；
   数据根查不出来也要继续启动。
5. 启动路径**真的调用** `read()`（否则第 2 条只是注释里的一句话）。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import settings as S  # noqa: E402

_n = 0

BASELINE = {
    "_comment": "说明文字，不是键",
    "effort_level": 3,
    "default_mode": "manual",
    "approval_timeout_seconds": 120,
    "auto_approve": False,
    "cross_zone_enabled": True,
    "relax_command_tier": False,
    "no_ask_readonly": False,
    "full_access_unlock": False,
    "interface_language": None,
    "response_language": None,
    "trace_full": False,
    "workspace": "workspace",
}


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def _write(root: Path, data) -> Path:
    p = root / "data" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str)
                 else json.dumps(data, ensure_ascii=False),
                 encoding="utf-8")
    return p


def test_read_good_values():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = _write(root, {"effort_level": 5, "trace_full": True,
                          "workspace": "D:/work", "web_token": "secret"})
        values, problems = S.read(p, BASELINE)
        check(problems == [], f"好值不该报问题：{problems}")
        check(values["effort_level"] == 5, values)
        check(values["trace_full"] is True, values)
        check("web_token" not in values, "白名单之外的键不许读出来")
        check("default_mode" not in values, "文件里没写的键不该凭空出现")


def test_bad_values_fall_back_and_speak():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = _write(root, {"effort_level": True,          # bool 不是整数
                          "auto_approve": 1,             # int 不是 bool
                          "trace_full": "yes",
                          "workspace": 5})
        values, problems = S.read(p, BASELINE)
        check(values["effort_level"] == 3, values)
        check(values["auto_approve"] is False, values)
        check(values["trace_full"] is False, values)
        check(values["workspace"] == "workspace", values)
        check(len(problems) == 4, f"四格都该报：{problems}")
        check(all("按默认" in x for x in problems), problems)


def test_unreadable_file_reads_quietly():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check(S.read(root / "nope.json", BASELINE) == ({}, []), "缺文件 -> 空")
        p = _write(root, "{broken")
        check(S.read(p, BASELINE) == ({}, []), "坏 JSON -> 空（ensure 管这一层）")
        p.write_text("[1,2]", encoding="utf-8")
        check(S.read(p, BASELINE) == ({}, []), "顶层不是对象 -> 空")


def test_baseline_loader():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data").mkdir()
        (root / "data" / "settings.example.json").write_text(
            json.dumps(BASELINE), encoding="utf-8")
        base = S.load_baseline(root)
        check("_comment" not in base, "下划线键不该进默认值")
        check(base["effort_level"] == 3, base)


def test_ensure_reports_actions():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root, {"effort_level": True})
        home = root

        def run(code):
            return json.dumps({"action": "seeded", "backup": "", "error": ""})

        lines = S.ensure(Path("python"), home, query_fn=run,
                         root_fn=lambda: root)
        check(any("首次启动" in x for x in lines), lines)
        check(any("effort_level" in x for x in lines), f"坏值要出声：{lines}")

        def run_backup(code):
            return json.dumps({"action": "backup",
                               "backup": "C:/x/settings.json.bad-1",
                               "error": ""})

        lines = S.ensure(Path("python"), home, query_fn=run_backup,
                         root_fn=lambda: root)
        check(any("已备份到" in x for x in lines), lines)

        def run_error(code):
            return json.dumps({"action": "error", "backup": "",
                               "error": "写入失败：Permission denied"})

        lines = S.ensure(Path("python"), home, query_fn=run_error,
                         root_fn=lambda: root)
        check(any("继续启动" in x for x in lines), lines)

        def root_fail():
            raise RuntimeError("查不到数据根")

        lines = S.ensure(Path("python"), home, query_fn=run,
                         root_fn=root_fail)
        check(any("跳过设置检查" in x for x in lines), lines)


def test_startup_actually_reads_the_whitelist() -> None:
    """启动要真的调用 `read()` —— 否则「坏值出声」只是注释里的一句话。"""
    src = (HERE / "crannia_tui" / "__main__.py").read_text(encoding="utf-8")
    check("_settings.read(" in src, "__main__ 没调用 settings.read")
    check("_settings.load_baseline(" in src, "读白名单要带出厂基准")


def main() -> int:
    for fn in (test_read_good_values, test_bad_values_fall_back_and_speak,
               test_unreadable_file_reads_quietly, test_baseline_loader,
               test_ensure_reports_actions,
               test_startup_actually_reads_the_whitelist):
        fn()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"FAILED: {exc}")
        sys.exit(1)
