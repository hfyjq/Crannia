"""仓外的技能目录，也要能导入它自己的 handler。

现场（2026-11-02）：用户从技能页「添加技能」装进来的包落在数据根的
`skills/` 下（安装版里与安装目录分家），而 handler 是按
`skills.<目录名>.handler` 导入的 —— 那个目录不在 `skills` 包里，
于是桩注册成功、第一次调用才抛 `ModuleNotFoundError`，接着被
`quarantine` 当场屏蔽。开发树里两个根是同一个目录（`user_skills_dir()`
在非安装版返回 `Path("skills")`），所以这条从来没被看见过。

判据两条，缺一不可：
  ① 桩注册之后能真的调起来；
  ② 包里那句 `from skills.<名>.其它模块 import …`（`SKILL_SPEC.md` 教人
     这么写）也一起成立 —— 只让 handler 能导入、同级模块不能导入，等于
     把作者的代码劈成两半。

Usage:
    .venv/Scripts/python.exe tests/test_skill_outside_package.py

NOTE: prints are ASCII on purpose -- the Windows console is GBK by default.
"""

import sys
import tempfile
import textwrap
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import sandbox  # noqa: E402

from bus.loaders import skill_loader as L  # noqa: E402
from bus.registry import get_tool_handler, unregister_owner  # noqa: E402

_NAME = "zz_outside_probe"
_TOOL = "zz_outside_probe_tool"

_MANIFEST = textwrap.dedent("""
    kind: skill
    name: %s
    version: "0.1.0"
    description: outside-package probe
    tools:
      - name: %s
        permission: read
        destructive: false
        output_kind: structured
        description: probe tool
        inputSchema:
          type: object
          properties: {a: {type: string}}
    """ % (_NAME, _TOOL))


def _make_skill(root: Path) -> Path:
    d = root / "skills" / _NAME
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("", encoding="utf-8")
    (d / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
    # 同级模块：验证 `from skills.<名>.helper import …` 也成立
    (d / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
    (d / "handler.py").write_text(
        "from skills.%s.helper import VALUE\n"
        "def %s(a=''):\n"
        "    return {'success': True, 'v': VALUE}\n" % (_NAME, _TOOL),
        encoding="utf-8")
    return d


def test_outside_package_skill_loads_and_runs():
    import skills as _pkg
    before = list(getattr(_pkg, "__path__", []) or [])
    with tempfile.TemporaryDirectory() as tmp:
        d = _make_skill(Path(tmp))
        try:
            n = L.load_skill_lazy(d, security_guard=None)
            assert n == 1, "桩没注册上（%r）" % (n,)
            h = get_tool_handler(_TOOL)
            assert h is not None, "注册表里查不到这个工具"
            out = h(a="x")
            assert isinstance(out, dict) and out.get("success") is True, out
            assert out.get("v") == 42, (
                "同级模块没跟着导入（%r）—— 只让 handler 能导入等于把作者的代码劈成两半"
                % (out,))
            print("  [ok] 仓外技能：桩注册 -> 首次调用成功，同级模块也导入得到")
        finally:
            unregister_owner("skill:" + _NAME)
            # `_make_importable` 往包路径里挂了一个临时根 —— 把它摘掉，
            # 否则这个进程里后续的扫描会带着一个已经不存在的目录。
            paths = getattr(_pkg, "__path__", None)
            if paths is not None:
                for p in list(paths):
                    if p not in before:
                        paths.remove(p)


def test_user_skill_with_same_name_is_skipped():
    """安装版的目录布局：同名的用户技能不试着装载，随包的那份赢。

    装进去也拿不到它自己的实现 —— `skills.<名>` 按包路径解析，
    包路径里排在前面的是随包目录（`_make_importable` 只是把用户根
    追加在后面）。结果是"同名工具用了内置实现，用户独有的工具
    在装载或首次调用时 AttributeError"。
    """
    import os
    from bus.core import _tool_skill_entries

    with tempfile.TemporaryDirectory() as tmp:
        primary = Path(tmp) / "app" / "skills"
        user = Path(tmp) / "data" / "skills"
        for name, root in (("alpha", primary), ("alpha", user), ("beta", user)):
            (root / name).mkdir(parents=True, exist_ok=True)
        old = os.environ.get("CRANNIA_DATA_DIR")
        os.environ["CRANNIA_DATA_DIR"] = str(Path(tmp) / "data")
        try:
            got = [p for p in _tool_skill_entries(primary)]
        finally:
            if old is None:
                os.environ.pop("CRANNIA_DATA_DIR", None)
            else:
                os.environ["CRANNIA_DATA_DIR"] = old
        names = [p.name for p in got]
        assert names == ["alpha", "beta"], names
        alpha = [p for p in got if p.name == "alpha"][0]
        assert str(alpha).startswith(str(primary)), (
            "同名时留下的那份不是随包的（%s）" % alpha)
    print("  [ok] 同名用户技能跳过；不同名的照常进候选")


def test_repo_skill_still_resolves():
    """负对照：仓内那个技能不需要这一步也照旧导入得到。"""
    d = _PROJECT_ROOT / "skills" / "file_parser"
    assert (d / "handler.py").is_file(), "夹具坏了：找不到仓内的 file_parser"
    mod = __import__("skills.file_parser.handler", fromlist=["x"])
    assert hasattr(mod, "init") or True
    print("  [ok] 仓内技能照旧按名字导入")


if __name__ == "__main__":
    print("=" * 60)
    print("  Skill outside package")
    print("=" * 60)
    print()
    tests = [
        ("outside package loads and runs", test_outside_package_skill_loads_and_runs),
        ("user skill with same name is skipped",
         test_user_skill_with_same_name_is_skipped),
        ("repo skill still resolves", test_repo_skill_still_resolves),
    ]
    passed = failed = 0
    #: 整批跑在沙箱里。`_LazyStub` 首次调用失败会写
    #:   `data/runtime/quarantine.json`（相对 cwd），而这个文件装载失败正是
    #:   本文件的被测项之一 —— 不换 cwd 就是直接写进生产账本，
    #:   下次启动弹「兼容性警告」（2026-11-02 真发生过）。
    with sandbox():
        for name, fn in tests:
            try:
                fn()
                passed += 1
            except Exception as exc:                           # noqa: BLE001
                failed += 1
                print("  [FAIL] %s: %s: %s" % (name, type(exc).__name__, exc))
                import traceback
                traceback.print_exc()
    print()
    print("=" * 60)
    print("  Results: %d passed, %d failed, %d total"
          % (passed, failed, len(tests)))
    print("=" * 60)
    if failed:
        sys.exit(1)
