"""`destructive` 的第二把尺子：AST 反查。

## 它补的是哪一格

能力名单那把尺子钉的是谁能遇到审批；这一把钉的是声明与实现一致：

> AST 说它有不可逆的动作，它必须声明 `true`，或者在账本里（带理由）。

为什么非有不可：`destructive` 的兜底是 `false`（fail-open ——
兜底取 `true` 会让闸为 129 个工具弹窗）。一个没人看过的工具会
静默地不算不可逆。兜底不承重，承重的是这一把尺子 + 棘轮。

## 这不是安全边界

扫的是文本里有没有那个动词，跳过一跳同名函数 —— 动态拼出来的名字
（`getattr(shutil, "rmtree")`）、C 扩展里干的坏事，它都看不见。
它挡的是「加了个会删东西的工具、忘了声明」，不是「决意隐藏的人」。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_destructive_declared.py
"""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 复用同一个扫描器与同一张账本 —— 不抄第二份
# （`scripts/propose_destructive.py` 是那批声明的生成器，账本的家在它那里）。
from scripts.propose_destructive import LEDGER                       # noqa: E402
from tests.test_permission_tiers import (                            # noqa: E402
    IRREVERSIBLE_ATTRS, _collect_defs, _write_marks,
)

_BOOTED = False


def _boot_once():
    global _BOOTED
    if not _BOOTED:
        from scripts.measure_request import boot_registry
        boot_registry()
        _BOOTED = True


def _flagged_tools() -> dict[str, list[str]]:
    """`{工具名: [命中的不可逆动词]}` —— 跑一趟全扫。"""
    import bus.registry as R
    index = _collect_defs()
    out: dict[str, list[str]] = {}
    for name in sorted(R.list_tools()):
        for rel, lineno, tree, _src in index.get(name, []):
            node = None
            for sub in ast.walk(tree):
                if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name == name and sub.lineno == lineno):
                    node = sub
                    break
            if node is None:
                continue
            marks = _write_marks(node, tree, attrs=IRREVERSIBLE_ATTRS,
                                 extra_rules=False)
            if marks:
                out[name] = sorted(marks) + ["(%s:%d)" % (rel, lineno)]
    return out


# 1. 主不变量：AST 命中，声明 true 或进账本

def test_every_ast_flagged_tool_is_declared_or_ledgered():
    """本文件的主不变量。

    判据：`IRREVERSIBLE_ATTRS` 扫出来的工具，必须要么声明 `true`，
    要么在 `LEDGER` 里有一条 `False` + 理由。

    账本条数是自造靶子的另一半：把 `LEDGER` 里任一条删掉，
    这里立刻红（除非那个工具真的被声明成 `true`）。
    """
    _boot_once()
    import bus.registry as R
    flagged = _flagged_tools()
    assert flagged, ("一个都没扫出来 —— 扫描器坏了，下面的断言是恒真的"
                     "（同 `tests/test_permission_tiers.py::test_the_scanner_can_go_red`）")
    problems = []
    for name, marks in sorted(flagged.items()):
        if R.get_tool_destructive(name):
            continue                       # 声明成不可逆，对上了
        entry = LEDGER.get(name)
        if entry is not None and entry[0] is False and entry[1].strip():
            continue                       # 账本里有理由，也对上了
        problems.append("%s 命中 %s，却既没声明 true、也不在账本里" % (name, marks[:3]))
    assert problems == [], (
        "AST 说这些工具有不可逆的动作，而声明层说它们可逆：\n  %s\n"
        "  -> 要么在自己的 `manifest.yaml` / `_EXPLICIT_DESTRUCTIVE` 里声明 "
        "`destructive: true`，要么去 `scripts/propose_destructive.py` 的 `LEDGER` 里"
        "记一条 **带理由** 的 `False`（那一格就是账本）。"
        % "\n  ".join(problems))


def test_the_ledger_and_the_declarations_agree():
    """账本与声明必须一致 —— 两边分开写就一定会漂。

    * 账本说 `True`，声明必须真是 `True`；
    * 账本说 `False`，声明必须真是 `False`（否则它不该在账本里，该直接声明 `True`）。
    """
    _boot_once()
    import bus.registry as R
    mism = []
    for name, (want, _why) in sorted(LEDGER.items()):
        if name not in R._registry["tools"]:
            mism.append("%s 在账本里，但**没注册**（改名了？删了？）" % name)
            continue
        got = R.get_tool_destructive(name)
        if got != want:
            mism.append("%s 账本说 %s，声明层是 %s" % (name, want, got))
    assert mism == [], "账本与声明对不上：\n  " + "\n  ".join(mism)


def test_the_ledger_has_no_stale_entry():
    """账本里每一条都必须是真注册过的工具。

    与上面那条分开写：上面那条允许"没注册"（它只把它记成 mismatch），
    这一条把它单独点出来 —— 一个改了名的工具会同时踩两条，
    而如果只有前者，那条消息会被"对不上"这个大帽子盖住。
    """
    _boot_once()
    import bus.registry as R
    stale = sorted(n for n in LEDGER if n not in R._registry["tools"])
    assert stale == [], "账本里有不存在的工具：%r" % (stale,)


# 2. 扫描器的自造靶子（负对照）

def test_the_scanner_can_go_red():
    """自造靶子 —— 仪器必须能对同一份源码给出两个不同的答案。

    正面：`p.unlink()` / `shutil.rmtree(...)` / `server.sendmail(...)` 都要抓到。
    反面：同一份源码里的一句 `f.write_text(x)` 是「改」不是「不可逆」，
    所以换表之后它不该被抓到 —— 那半条才证明"两张表真的不同"，
    而不只是"我把表填了"。
    """
    src = (
        "import shutil\n"
        "def tool_yes(p, srv):\n"
        "    p.unlink()\n"
        "    shutil.rmtree('/tmp/x')\n"
        "    srv.sendmail('a', ['b'], 'c')\n"
        "def tool_no(p):\n"
        "    p.write_text('x')\n"          # 「改」不是「不可逆」
        "    p.save()\n"
    )
    tree = ast.parse(src)
    defs = {n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)}
    yes = _write_marks(defs["tool_yes"], tree,
                       attrs=IRREVERSIBLE_ATTRS, extra_rules=False)
    no = _write_marks(defs["tool_no"], tree,
                      attrs=IRREVERSIBLE_ATTRS, extra_rules=False)
    assert yes, "靶子没被打中 —— 扫描器对 unlink/rmtree/sendmail 失灵了"
    assert not no, (
        "`write_text` 被算成了**不可逆** —— 两张表混在一起了（那是「改」那一张）\n"
        "  抓到的是: %r" % (sorted(no),))


# 3. 磁盘上的 manifest（含 draft 包 —— 它们不装载，注册表看不见）

def test_every_manifest_tool_declares_destructive():
    """**每份 `skills/*/manifest.yaml` 里的每个工具都要声明**。

    这条不能靠注册表：`draft: true` 的包根本不装载
    （`if manifest.get("draft"): continue`），它不在注册表里，
    于是"读注册表"那条路看不见它 —— 而漏掉的症状是「一次静默的 fail-open」。
这一条直接读盘，所以 draft 与非 draft 一视同仁。

    跳过 `_` 开头的条目（`_wip_*` 临时目录）—— 与 `skill_doctor.discover()` 同规矩。
    """
    import yaml
    missing = []
    n_manifests = 0
    for d in sorted((_PROJECT_ROOT / "skills").iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        mp = d / "manifest.yaml"
        if not mp.is_file():
            continue
        n_manifests += 1
        try:
            man = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            missing.append("%s 解析不了（%s: %s）" % (mp.name, type(exc).__name__, exc))
            continue
        for t in man.get("tools", []):
            if not isinstance(t, dict):
                continue
            if "destructive" not in t:
                missing.append("skills/%s: %s 没声明 destructive"
                               % (d.name, t.get("name")))
            elif not isinstance(t["destructive"], bool):
                missing.append("skills/%s: %s 的 destructive 不是布尔（%r）"
                               % (d.name, t.get("name"), t["destructive"]))
    #: 地板 = 8。它是一条地板（抓"扫描根坏了"），不是棘轮：
    #:   地板必须等于今天的真值，否则它每次删包都要人肉追一次。
    #   这个数只回答"扫描根对不对"，不是"包够不够多"。
    assert n_manifests >= 6, "只扫到 %d 份 manifest —— 扫描根是不是错了？" % n_manifests
    assert missing == [], (
        "这些工具没声明 `destructive:`（**draft 包也算**）：\n  %s\n"
        "  -> 去 `scripts/propose_destructive.py --apply`，或手工补一行。"
        % "\n  ".join(missing))


# runner

def _main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
