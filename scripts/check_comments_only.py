#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只许动注释：比对两个版本，任何代码层差异都报出来。

    python scripts/check_comments_only.py                 # 工作区 vs HEAD
    python scripts/check_comments_only.py --base dcdad2b  # 某个提交 vs HEAD
    python scripts/check_comments_only.py --base dcdad2b --to HEAD

## 为什么不是简单的 AST 比对

`ast.dump` 会把 docstring 当成节点，剥掉之后注释与普通字符串都看不出来：
注释不在 AST 里（所以改注释不影响 AST），而字符串字面量在（改它就是改代码）。
所以"AST 相同"只能证明 docstring 与代码没变，证明不了字符串没变 —— 那正是
上一批漏掉的那一类。

这里改用 token 流：去掉注释 token 与 docstring 那个 STRING token，剩下的逐个比。
字符串字面量因此会被看见。

退出码：0 = 只动了注释；1 = 有代码层差异（列出文件与第一处差异）。
"""

from __future__ import annotations

import argparse
import ast
import difflib
import io
import subprocess
import sys
import tokenize
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _docstring_positions(src: str) -> set:
    """所有 docstring 的起始行 —— 只剥这些 STRING，别的字符串照比。

    按 (行, 列) 匹配会漏掉带前缀的写法（以 r 开头的三引号串，token 从 r 开始，
    而 AST 给的 col_offset 是引号位置），漏掉之后整段横幅被当成"字符串被改了"。
    按行匹配没有这个问题。
    """
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(body[0].value.lineno)
    return out


def code_tokens(src: str) -> list:
    # 行尾先归一：`git show` 给的是库里存的 LF，工作区是 CRLF（autocrlf），
    # 不归一的话每个多行字符串都会被判成内容变了。
    src = src.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
    docs = _docstring_positions(src)
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.start[0] in docs:
                continue
            if tok.type in (tokenize.NL, tokenize.NEWLINE,
                            tokenize.INDENT, tokenize.DEDENT):
                continue
            out.append((tok.type, tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [(-1, "<无法解析>")]
    return out


def _git_show(rev: str, path: str) -> str:
    r = subprocess.run(["git", "show", "%s:%s" % (rev, path)],
                       capture_output=True, encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="HEAD", help="基线提交（默认 HEAD）")
    ap.add_argument("--to", default=None, help="目标提交；不给就比工作区")
    args = ap.parse_args()

    cmd = ["git", "diff", "--name-only", args.base]
    if args.to:
        cmd.append(args.to)
    changed = [f for f in subprocess.run(cmd, capture_output=True, encoding="utf-8",
                                         errors="replace").stdout.split()
               if f.endswith(".py")]

    bad = []
    for f in changed:
        old = _git_show(args.base, f)
        try:
            if args.to:
                new = _git_show(args.to, f)
            else:
                new = io.open(_ROOT / f, encoding="utf-8", newline="").read()
        except OSError:
            continue
        if code_tokens(old) != code_tokens(new):
            bad.append(f)

    print("基线 %s · 改动 .py %d 个 · 代码层有差异 %d 个"
          % (args.base, len(changed), len(bad)))
    for f in bad:
        old = code_tokens(_git_show(args.base, f))
        new = code_tokens(_git_show(args.to, f) if args.to
                          else io.open(_ROOT / f, encoding="utf-8", newline="").read())
        print("  %s" % f)
        for line in list(difflib.unified_diff([t[1] for t in old],
                                             [t[1] for t in new],
                                             lineterm="", n=0))[2:8]:
            print("      " + line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
