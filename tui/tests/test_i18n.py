"""`crannia_tui.i18n`：TUI 自己的界面文案表。

钉六件事：

1. 查不到回落原文；源码语言就是原文；首尾空白原样保留。
2. 两张表：基（crannia 根的 `locales/`）+ 补（`crannia_tui/locales/`）。
   补集不许重复基里的键，同一句话只能有一个家。
3. 界面里用到的每一句都有译文；占位符个数一致。
4. 数据表（加载短语 / 结局 / 故障 / 错误分类）每一格也都有译文。
5. 五个界面模块里的中文字面量，要么包在 `t()` 里，要么在具名表里。
   新增一句没进表，这里当场红。
6. 语言包读不出来不抛，回落原文。

`__main__.py` 也在扫描表里：入口在构造 argparse 之前先问一次语言，
所以 `--help` 也跟着当前界面语言走（`--version` 那条路跳过）。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent / "crannia_tui"
ROOT = HERE.parent.parent

sys.path.insert(0, str(HERE.parent))

from crannia_tui import i18n as I  # noqa: E402
from crannia_tui import outcome as O  # noqa: E402
from crannia_tui import setup as S  # noqa: E402
from crannia_tui import ui as U  # noqa: E402

_n = 0

#: 包在 `t()` 里的模块，以及它们里面"值是中文、显示时才过 `t()`"的具名表。
_SCAN = {
    "ui.py": {"LOADING_PHRASES", "_RETRY_LABELS"},
    "app.py": {"BINDINGS"},
    "setup.py": {"_ERROR_HINTS"},
    "outcome.py": {"_LABELS"},
    "render.py": set(),
    "doctor.py": set(),
    "channel.py": {"_USAGE"},
    "profile.py": set(),
    "update.py": set(),
    "paths.py": set(),
    "term.py": set(),
    "approval.py": set(),
    "commands.py": set(),
    "events.py": set(),
    "i18n.py": set(),
    "backend.py": set(),
    "headless.py": set(),
    "client.py": set(),
    "clipboard.py": set(),
    "probe.py": {"BINDINGS"},
    "settings.py": set(),
    "__main__.py": set(),
}

#: 不翻译、也不该被改的单个字面量：
#: `正在初始化…` 是模块级常量，用时才过 `t()`；
#: `是` 是用户输入的匹配词，不是显示文案；
#: `【图片` 是插进输入框的占位标记，提交时按它认图。
_ALLOW = {"app.py": {"正在初始化…", "【图片"},
          "setup.py": {"是"},
          "channel.py": {"是"}}

#: 没有界面文案、不进扫描表的模块。`__init__.py` 只有包级说明。
_NOT_SCANNED = {"__init__.py"}

_PH = re.compile(r"%\([A-Za-z_]\w*\)[sdrf]|%[sdrf]|\{[A-Za-z_][\w.]*\}")
_CJK = re.compile("[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]")


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def _read_pack(path: Path, *, strip_values: bool) -> dict:
    """读一张表；键 strip，基表的译文也 strip（与 `i18n._pack` 同一条）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        if isinstance(value, str) and str(key).strip():
            out[str(key).strip()] = value.strip() if strip_values else value
    return out


def _base() -> dict:
    return _read_pack(ROOT / "locales" / "en.json", strip_values=True)


def _supplement() -> dict:
    return _read_pack(PKG / "locales" / "en.json", strip_values=False)


def _calls(tree: ast.AST) -> list[str]:
    """`t("...")` 的字面量实参（拼接后的常量也算一条）。"""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "t"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append(arg.value)
    return out


def _table_ids(tree: ast.AST, names: set[str]) -> set[int]:
    """具名表里的字面量 —— 它们是表的值，显示时才过 `t()`。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in names:
                for sub in ast.walk(value):
                    ids.add(id(sub))
    return ids


def _doc_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def test_source_language_is_the_original() -> None:
    I.configure(None)
    check(I.current() == "zh", I.current())
    check(I.t("") == "", "空串原样返回")
    check(I.t("已完成") == "已完成", "源码语言不查表")
    check(I.t("这一句不在表里") == "这一句不在表里", "查不到回落原文")


def test_whitespace_is_restored() -> None:
    I.configure("en", home=ROOT)
    check(I.t(" · 接近触发") == " · near trigger",
          repr(I.t(" · 接近触发")))
    check(I.t("  ↑↓ 或数字选择，回车确认")
          == "  ↑↓ or a number to choose, Enter to confirm",
          repr(I.t("  ↑↓ 或数字选择，回车确认")))
    check(I.t("上下文：") == "Context: ", repr(I.t("上下文：")))


def test_base_and_supplement() -> None:
    I.configure("en", home=ROOT)
    check(I.t("已完成") == "Completed", "基表要有共用术语")
    check(I.t("待审批") == "Awaiting approval", "补集要有 TUI 自己的话")
    with tempfile.TemporaryDirectory() as td:
        I.configure("en", home=Path(td))
        check(I.t("已完成") == "已完成", "没有基表就回落原文")
        check(I.t("待审批") == "Awaiting approval", "补集不依赖基表")


def test_broken_pack_does_not_throw() -> None:
    with tempfile.TemporaryDirectory() as td:
        loc = Path(td) / "locales"
        loc.mkdir()
        (loc / "en.json").write_text("{not json", encoding="utf-8")
        I.configure("en", home=Path(td))
        check(I.t("待审批") == "Awaiting approval", "坏包不影响补集")
        check(I.t("已完成") == "已完成", "坏包整体回落原文")
        (loc / "en.json").write_text(json.dumps(
            {"好的": 123, "坏的": "Good"}, ensure_ascii=False),
            encoding="utf-8")
        I.configure("en", home=Path(td))
        check(I.t("好的") == "好的", "表里不是字符串就回落原文")
        check(I.t("坏的") == "Good", "字符串照常取用")


def test_supplement_does_not_duplicate_base() -> None:
    base, sup = _base(), _supplement()
    check(bool(base), "找不到基表 —— 这道检查会空转")
    dupes = sorted(set(sup) & set(base))
    check(not dupes, "补集与基表重复（同一句话两个家）：%r" % (dupes[:8],))


def test_every_used_string_has_a_translation() -> None:
    pack = dict(_base())
    pack.update(_supplement())
    used: dict[str, str] = {}
    for name in _SCAN:
        tree = ast.parse((PKG / name).read_text(encoding="utf-8"))
        for text in _calls(tree):
            used.setdefault(text.strip(), name)
    check(len(used) > 60, "只扫到 %d 句 —— 扫描器自己坏了？" % len(used))
    missing = sorted(k for k in used if k not in pack)
    check(not missing, "%d 句没有译文：%r" % (len(missing), missing[:8]))


def test_table_values_have_translations() -> None:
    pack = dict(_base())
    pack.update(_supplement())
    values = list(U.LOADING_PHRASES)
    values += list(U._RETRY_LABELS.values())
    values += [label for label, _item in S._ERROR_HINTS.values()]
    values += list(O._LABELS.values())
    check(len(values) > 30, "表里只取到 %d 格" % len(values))
    missing = sorted({v.strip() for v in values if v.strip() not in pack})
    check(not missing, "表里有 %d 格没有译文：%r" % (len(missing), missing[:8]))


def test_every_module_is_scanned() -> None:
    """扫描表要跟着包里的文件走 —— 新加一个模块而没登记，等于它从没被查过。

    这一条是给上面两张表配的读者：表是手抄的，漏登记不会有任何东西变红。
    """
    on_disk = {p.name for p in PKG.glob("*.py")}
    missing = sorted(on_disk - set(_SCAN) - _NOT_SCANNED)
    check(not missing, "这些模块不在 i18n 扫描表里：%r" % (missing,))
    stale = sorted(set(_SCAN) - on_disk)
    check(not stale, "扫描表里有点不存在的文件：%r" % (stale,))


def test_placeholders_are_preserved() -> None:
    bad = []
    for key, value in _supplement().items():
        if sorted(_PH.findall(key)) != sorted(_PH.findall(value)):
            bad.append((key, value))
    check(not bad, "占位符个数对不上：%r" % (bad[:6],))


def test_every_chinese_literal_is_wrapped_or_in_a_table() -> None:
    bad: list[tuple[str, int, str]] = []
    calls_seen = 0
    for name, tables in _SCAN.items():
        tree = ast.parse((PKG / name).read_text(encoding="utf-8"))
        # `t()` 的实参子树整块都算"进表" —— `t(name or "默认那句")`
        # 里那句默认值同样会过 `t()`。
        wrapped: set[int] = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "t"):
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Constant):
                            wrapped.add(id(sub))
        calls_seen += len(wrapped)
        docs = _doc_ids(tree)
        table_ids = _table_ids(tree, tables)
        allow = _ALLOW.get(name, set())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            if id(node) in docs or id(node) in wrapped or id(node) in table_ids:
                continue
            if not _CJK.search(node.value):
                continue
            if node.value.strip() in allow:
                continue
            bad.append((name, node.lineno, node.value[:60]))
    check(calls_seen > 60, "只看到 %d 处 t() 调用 —— 扫描器没扫到东西？"
          % calls_seen)
    check(not bad, "%d 处中文没进表：%r" % (len(bad), bad[:8]))


def test_available_lists_the_supplement() -> None:
    langs = I.available()
    check("zh" in langs and "en" in langs, repr(langs))


def test_help_is_built_after_the_language_is_known() -> None:
    """`--help` 的文案在解析参数之前就拼好了，所以语言要先定。

    把入口那一步换成一个只记一笔的函数：它必须被调到，且 help 真是英文。
    """
    import contextlib
    import io

    from crannia_tui import __main__ as M

    I.configure("en", home=ROOT)
    called: list[bool] = []
    orig = M._configure_language
    M._configure_language = lambda: called.append(True)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            try:
                M.main(["--help"])
            except SystemExit:
                pass
    finally:
        M._configure_language = orig
    out = buf.getvalue()
    check(bool(called), "--help 之前要先定语言")
    check("Crannia's terminal interface" in out, out[:300])
    check("crannia 的终端界面" not in out, "界面文案还是中文")


def test_language_probe_prepares_the_data_root() -> None:
    """问语言那个子进程动的是 `data/settings.json`，要先 prepare 数据根。

    安装版里路径是相对的，靠入口那一下 chdir 才指到用户数据根；不 prepare
    就读到安装目录里的模板（开发树两者重合，所以别处看不出来）。
    """
    from crannia_tui import __main__ as M

    check(M._LANG_CODE.startswith(
        "from core import paths as _paths; _paths.prepare()"), M._LANG_CODE)


def main() -> int:
    for fn in [test_source_language_is_the_original,
               test_whitespace_is_restored,
               test_base_and_supplement,
               test_broken_pack_does_not_throw,
               test_supplement_does_not_duplicate_base,
               test_every_used_string_has_a_translation,
               test_every_module_is_scanned,
               test_table_values_have_translations,
               test_placeholders_are_preserved,
               test_every_chinese_literal_is_wrapped_or_in_a_table,
               test_available_lists_the_supplement,
               test_help_is_built_after_the_language_is_known,
               test_language_probe_prepares_the_data_root]:
        fn()
    I.configure(None)
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
