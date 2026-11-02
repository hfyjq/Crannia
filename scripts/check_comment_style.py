#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""注释规范的机械读者。

    .venv/Scripts/python.exe scripts/check_comment_style.py            # 看总数
    .venv/Scripts/python.exe scripts/check_comment_style.py --list     # 逐条列
    .venv/Scripts/python.exe scripts/check_comment_style.py --top 15   # 只列最脏的文件

## 范围

只扫**注释与 docstring**：`.py` 的模块 / 类 / 函数 docstring，以及 `#` 注释。
不碰字符串字面量 —— 那些可能是喂给模型的回执、用户可见的界面文案，
它们的记号规则是另一套（是否 Markdown 由呈现层决定）。

前端（`.js` / `.jsx` / `.ts` / `.tsx`）由 `comment_style_js.py` 扫，判据同一张表。
那一半依赖 `pygments`、且**不随产品发布** —— 它缺席时本脚本只扫 `.py`，
一行提示后照常结束。`count_all()` 因此只数 `.py`，前端走 `count_all_frontend()`。

排除：`# noqa` / `# type:` / `# pylint:` / `# pragma:` 这类工具指令。

## 判据

| 项 | 例子 |
|---|---|
| 强调记号 | 强调星、警告三角、`**粗体**` |
| 双反引号 | ` ``name`` `（单反引号只许包**代码标识符**） |
| 箭头符 | 推出符号（写「所以」「即」） |
| 装饰分隔线 | 用制表符画的长横线、小节横线 |
| 工号 / 条目号 | `W109` `M10` `C1a` `D11` `G19` `H5` `N-24` |

散落在句子里的「参照某产品做的」这一类，这个脚本不列产品名单 ——
它自己就会变成一份名单。要机器挡的那一类（带路径、带项目名）由一份
独立的来源棘轮负责，它同样只在开发树里跑。
"""

from __future__ import annotations

import argparse
import ast
import importlib
import io
import re
import sys
import tokenize
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("core", "bus", "providers", "channels", "mcpx", "web", "scripts",
             "skills", "tests")
SKIP_DIRS = {"__pycache__", "node_modules", "dist", "release", "runtime",
             ".venv", ".git", ".runtime-cache", "vendor"}

#: 每条判据：名字 → 正则。
#: 这几个符号在 GBK 控制台上会崩，所以用 chr() 拼出来 ——
#: 本文件因此保持 ASCII 安全，判据本身不受影响。
STAR = chr(0x2605)          # 强调星
WARN = chr(0x26A0)          # 警告三角
ARROW = chr(0x21D2)         # 推出符号
BOX = "".join(chr(c) for c in (0x2550, 0x2500, 0x2501, 0x2551))

RULES = {
    "强调记号": re.compile(STAR + "|" + WARN),
    "粗体": re.compile(r"\*\*[^*\n]{1,40}\*\*"),
    "双反引号": re.compile(r"``[^`\n]{1,60}``"),
    "箭头符": re.compile(ARROW),
    "装饰线": re.compile(r"^#\s*[" + BOX + r"=]{6,}"),
    # N 族一律两位（N-01..N-36）—— 一位的 N-1 是数学记号，不是工号。
    "工号": re.compile(r"\b(?:[WMCDGH]\d{1,3}[a-z]?|N-\d{2})\b"),
}

#: 工具指令行整行跳过。
_TOOL_DIRECTIVE = re.compile(r"#\s*(noqa|type:|pylint:|pragma:|mypy:|fmt:|isort:|ruff:)")

#: 按形状打，就有几种形状与记号同形、其实不是记号。
#: 每一条 (相对路径, 判据) -> (次数, 理由)。用量是精确值，两头都钉：
#: 少一次说明那条形状没了（豁免该摘），多一次说明它在放水。
#:
#: 判据是「把这一处改掉会不会把意思改坏」：
#: Excel 的列引用、型号名、Word 的标题级别、docstring 里的 Python 运算符，
#: 改掉就没有别的写法能表达同一个意思 —— 那不是欠账。
_KNOWN_FALSE_POSITIVE = {
    ("skills/office_parser/excel_tools.py", "工号"):
        (8, "Excel 单元格区间（`A5:H12` 这类）与示例地址"),
    ("tests/test_excel_read.py", "工号"): (6, "同上"),
    ("providers/minimax/__init__.py", "工号"): (2, "型号名 `M2.7` / `M3`"),
    ("providers/minimax/client.py", "工号"): (2, "同上"),
    ("scripts/check_docx_structure.py", "工号"): (2, "Word 标题级别 `H1` / `H2`"),
    ("scripts/make_dirty_fixture.py", "工号"): (2, "公式 `=D6/C6` 里的单元格引用"),
    ("skills/office_parser/cell_validation.py", "工号"): (1, "示例单元格地址 `C2`"),
    ("core/session_workspace.py", "粗体"): (1, "docstring 里的函数签名 `**tool_args`"),
    ("tests/test_loop_health.py", "粗体"): (1, "乘方运算符"),
    ("tests/test_mcp_loop.py", "粗体"): (1, "dict 解包 `**`"),
}

#: 每轮重新计数 —— `count_all()` 会清。
_FP_USED: dict = {}


def reset_false_positive_usage() -> None:
    _FP_USED.clear()


def false_positive_usage() -> dict:
    return dict(_FP_USED)

#: 只在注释里判、不在 docstring 里判的判据（装饰线是整行注释的形状）。
_COMMENT_ONLY = {"装饰线"}


def _docstrings(tree: ast.AST):
    """(节点名, docstring) —— 模块 / 类 / 函数。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                name = getattr(node, "name", "<module>")
                yield name, doc


def _comments(path: Path):
    """(行号, 注释文本) —— 用 tokenize，所以字符串里的 `#` 不会被当成注释。"""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                if _TOOL_DIRECTIVE.match(tok.string):
                    continue
                yield tok.start[0], tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return


def _is_known_false_positive(path: Path, rule: str) -> bool:
    """这一处豁免掉吗。豁免额度用完就不再豁免 —— 多出来的照常报。"""
    try:
        rel = path.relative_to(_ROOT).as_posix()
    except ValueError:
        return False
    key = (rel, rule)
    cap = _KNOWN_FALSE_POSITIVE.get(key, (0, ""))[0]
    if _FP_USED.get(key, 0) >= cap:
        return False
    _FP_USED[key] = _FP_USED.get(key, 0) + 1
    return True


def scan_file(path: Path) -> list[tuple]:
    """[(行号或 0, 位置, 判据名, 命中片段)]"""
    out = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        tree = None

    if tree is not None:
        for where, doc in _docstrings(tree):
            for rule, rx in RULES.items():
                if rule in _COMMENT_ONLY:
                    continue
                for m in rx.finditer(doc):
                    if _is_known_false_positive(path, rule):
                        continue
                    out.append((0, "docstring:" + where, rule, m.group(0)[:40]))

    for lineno, line in _comments(path):
        for rule, rx in RULES.items():
            for m in rx.finditer(line):
                if _is_known_false_positive(path, rule):
                    continue
                out.append((lineno, "comment", rule, m.group(0)[:40]))
    return out


def iter_files():
    """要扫的 .py —— 目录白名单 + 名字黑名单。测试也走这一处。"""
    for d in SCAN_DIRS:
        root = _ROOT / d
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if p.name == "check_comment_style.py":
                continue      # 它自己的判据表就写着那些名字，扫自己没有意义
            yield p


def count_all() -> tuple[int, int, dict, dict]:
    """返回 (文件数, 命中总数, 每文件命中, 每判据命中)。只数 `.py`。"""
    per_file = {}
    by_rule = {}
    files = 0
    reset_false_positive_usage()
    for p in sorted(iter_files()):
        files += 1
        hits = scan_file(p)
        if not hits:
            continue
        per_file[p.relative_to(_ROOT)] = hits
        for _, _, rule, _ in hits:
            by_rule[rule] = by_rule.get(rule, 0) + 1
    return files, sum(len(v) for v in per_file.values()), per_file, by_rule


def count_all_frontend():
    """前端那一半。返回 (文件数, 总数, 每文件, 每判据)，或 None（模块缺席）。

    单独一个函数而不是并进 `count_all()`：那一半是 dev-only，
    并进来会让随产品发布的那份脚本依赖一个不发的模块。
    """
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        mod = importlib.import_module("comment_style_js")
    except ImportError:
        return None
    return mod.count_all()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="逐条列出")
    ap.add_argument("--top", type=int, default=0, help="只列最脏的前 N 个文件")
    ap.add_argument("--under", type=int, default=0,
                    help="只输出命中数 <= N 的文件路径（一行一个，给批量改分批用）")
    ap.add_argument("--over", type=int, default=0,
                    help="只输出命中数 > N 的文件路径")
    ap.add_argument("--between", type=int, nargs=2, metavar=("MIN", "MAX"),
                    help="只输出 MIN <= 命中数 <= MAX 的文件路径")
    args = ap.parse_args()

    files, total, per_file, by_rule = count_all()

    if args.under or args.over or args.between:
        lo, hi = (0, 10 ** 9)
        if args.under:
            hi = args.under
        if args.over:
            lo = args.over + 1
        if args.between:
            lo, hi = args.between
        for rel, hits in sorted(per_file.items()):
            if lo <= len(hits) <= hi:
                print(rel)
        return 0

    print("扫描 %d 个 .py 文件，命中 %d 处，分布在 %d 个文件"
          % (files, total, len(per_file)))
    for rule, n in sorted(by_rule.items(), key=lambda x: -x[1]):
        print("  %-10s %5d" % (rule, n))

    front = count_all_frontend()
    if front is None:
        print()
        print("前端：跳过 —— comment_style_js 不在（dev-only，不随产品发布）")
    else:
        f_files, f_total, f_per_file, f_by_rule = front
        print()
        print("扫描 %d 个前端文件，命中 %d 处，分布在 %d 个文件"
              % (f_files, f_total, len(f_per_file)))
        for rule, n in sorted(f_by_rule.items(), key=lambda x: -x[1]):
            print("  %-10s %5d" % (rule, n))
        if args.top or args.list:
            for rel, hits in sorted(f_per_file.items()):
                print("%4d  %s" % (len(hits), rel))
                if args.list:
                    for lineno, where, rule, frag in hits[:50]:
                        print("        %5s %-10s %-22s %s"
                              % (lineno or "-", where, rule, frag))

    ranked = sorted(per_file.items(), key=lambda kv: -len(kv[1]))
    shown = ranked[:args.top] if args.top else ranked
    if args.top or args.list:
        print()
        for rel, hits in shown:
            print("%4d  %s" % (len(hits), rel))
            if args.list:
                for lineno, where, rule, frag in hits[:50]:
                    print("        %5s %-10s %-22s %s"
                          % (lineno or "-", where, rule, frag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
