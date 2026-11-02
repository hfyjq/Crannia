#!/usr/bin/env python3
"""
GBK 安全自查 —— 找出用 GBK 编不出来的字符串字面量。

Windows 控制台默认 GBK。`print` 里出现 GBK 编不出的字符会让整个
测试/脚本崩掉，而崩掉的输出看起来像测试失败 —— 所以这是个 bug 探针，
不是洁癖检查。本仓已经栽过两次：三个测试文件用 U+274C（勾叉类符号），以及
`scripts/check_prompt_leak.py` 自己的 FAIL 分支用 U+21D2（右箭头）（正常路径不崩，
发现泄漏时才崩 —— 最坏的时机）。

用法::

    # 默认扫 tests/ 与 scripts/（就是会直接跑的那些）
    .venv/Scripts/python.exe scripts/check_gbk_safe.py

    # 指定文件
    .venv/Scripts/python.exe scripts/check_gbk_safe.py tests/test_compressor.py

    # 连生产代码一起（core/ bus/ providers/ web/ channels/）—— 输出居多，
    # 但生产代码里 print 的中文如果不在 GBK 里，同样会崩
    .venv/Scripts/python.exe scripts/check_gbk_safe.py --all

退出码: `0` 全部安全 · `1` 有会崩的字面量 · `2` 路径问题

判定分两档（这个区分很重要，否则全是噪声）:

* 会崩 —— 出现在非 docstring 的字符串里（`print`、`raise`、日志……）。
  这些真的会在 GBK 控制台上抛 `UnicodeEncodeError`。退出码只看这一档。
* 仅 docstring —— 只出现在模块/类/函数的 docstring 里。不参与打印，
  所以无害；列出来只是提醒（比如有人 `print(__doc__)` 就会崩）。

中文本身不需要检查 —— GBK 覆盖常用汉字。编不出来的是 emoji，以及
箭头/星形/圈字/勾叉这几类符号（U+2192、U+21D2、U+26A0、U+24D8、
U+2705、U+274C …）。

（本段特意不写这些符号本身 —— 写了的话这个脚本会把自己报成"仅 docstring、
无害"，虽说无害，但每次跑都多一行噪声。想认字形就照码点去查。）
"""

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TARGETS = ["tests", "scripts"]
ALL_TARGETS = DEFAULT_TARGETS + ["core", "bus", "providers", "web", "channels", "mcpx"]


def _unencodable(text: str):
    """GBK 编不出来的字符，去重后按码点排序。"""
    bad = {}
    for ch in text:
        try:
            ch.encode("gbk")
        except UnicodeEncodeError:
            bad[ch] = ord(ch)
    return sorted(bad.items(), key=lambda kv: kv[1])


def _docstring_nodes(tree: ast.AST) -> set:
    """收集所有 docstring 的 Constant 节点 id。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                            ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def scan(path: Path):
    """-> (will_crash, docstring_only)；每项是 [(line, char, codepoint)]"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None
    docs = _docstring_nodes(tree)
    crash, doc = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for ch, cp in _unencodable(node.value):
                (doc if id(node) in docs else crash).append((node.lineno, ch, cp))
    return crash, doc


def _collect(targets):
    files = []
    for t in targets:
        p = ROOT / t
        if p.is_file() and p.suffix == ".py":
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
    return [f for f in files if "__pycache__" not in f.parts]


def main() -> int:
    ap = argparse.ArgumentParser(description="GBK 安全自查")
    ap.add_argument("paths", nargs="*",
                    help="要扫的文件或目录；默认 tests/ + scripts/")
    ap.add_argument("--all", action="store_true",
                    help="连生产代码一起扫（core/ bus/ providers/ web/ ...）")
    args = ap.parse_args()

    targets = args.paths or (ALL_TARGETS if args.all else DEFAULT_TARGETS)
    files = _collect(targets)
    if not files:
        print(f"[gbk] 没找到要扫的 .py：{targets}")
        return 2

    n_crash = n_doc = 0
    print("=" * 66)
    print(f"GBK 安全自查 —— {len(files)} 个文件")
    print("=" * 66)

    for f in files:
        r = scan(f)
        if r is None:
            print(f"  (跳过，读/解析失败) {f.relative_to(ROOT)}")
            continue
        crash, doc = r
        if not crash and not doc:
            continue
        rel = f.relative_to(ROOT)
        if crash:
            n_crash += len(crash)
            print(f"\n[会崩] {rel}")
            for ln, ch, cp in crash:
                print(f"    line {ln:<5} {ascii(ch)}  U+{cp:04X}")
        if doc:
            n_doc += len(doc)
            print(f"\n[仅 docstring，无害] {rel}")
            for ln, ch, cp in doc:
                print(f"    line {ln:<5} {ascii(ch)}  U+{cp:04X}")

    print()
    print("=" * 66)
    if n_crash:
        print(f"结论: FAIL —— {n_crash} 处**会崩**（GBK 控制台上 UnicodeEncodeError）")
        print("怎么办: 把 print/raise/日志里的这些符号换成 ASCII ——")
        print("        U+2705 -> OK   U+274C -> FAIL   U+26A0 -> [!]")
        print("        U+21D2 -> ->   U+2194 -> <->    U+24D8 -> note:")
        print("        或者在跑之前设 PYTHONIOENCODING=utf-8 ——")
        print("        但那只治你的终端：别人跑同一个测试仍会崩。")
        print("=" * 66)
        return 1
    print(f"结论: PASS —— 没有会崩的字面量"
          + (f"（{n_doc} 处仅在 docstring，无害）" if n_doc else ""))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
