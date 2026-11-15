#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把界面文案的字面量包成 `tr("…")` —— 接线批 1（命令回话 + 通知/报错）。

    # 先看会发生什么（默认 dry-run，不改文件）
    .venv/Scripts/python.exe scripts/i18n_wire.py

    # 真写
    .venv/Scripts/python.exe scripts/i18n_wire.py --apply

## 它包哪些（判据与扫描器同源，不是第二套白名单）

直接复用 `scripts/i18n_scan.py` 的块定义与分类：

* 只碰块 01 / 02（命令回话 · 通知/报错）的 Python 来源；
* 只碰 `_keep(...)==1` 的（`保留=0` 的别名 / 码位比较 / 日志串不碰）；
* 跳过 f-string（`f"…{x}…"`）：键里是 `{x}`，而 `tr()` 在格式化之后查不到 ——
  这一批先留中文，等单独一轮把它们改成 `%` 写法（脚本末尾会报数）；
* 跳过 `locales/worksheet/文案待改清单.xlsx` 里的句子 —— 用户正在改原文，
  现在包上去等于让他们的改动落在一个马上要换掉的键上。等改完再跑一次本脚本即可。

## 为什么是 `tr()` 不是 `t()`

`pre_process.py` 与 `web/server.py` 里都有局部变量叫 `t`
（`t = (text or "").strip()` / `t = threading.Thread(...)`）——
`from core.i18n import t` 会在那些函数里被局部名遮住，症状是
`UnboundLocalError` 或"翻译偶尔不生效"。导入成 `tr`（全仓无同名）。

## 幂等

已经是 `tr(...)` 包着的不再包第二层 —— 所以改完原文之后可以直接再跑一遍。
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("i18n_scan", _ROOT / "scripts" / "i18n_scan.py")
SCAN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SCAN)

#: 本批只接这两块（其余块是界面，走前端 `t()`，是另一轮）。
BLOCKS = ("01-commands", "02-api-messages", "04-chat")

#: 不接的源文件：`prompt_line()` 是给模型看的系统提示词，
#: 它由「回答语言」管，换界面语言不许动它。
SKIP_FILES = {"core/response_language.py"}

#: 导入写成 `tr`（理由见模块 docstring）。
IMPORT_LINE = "from core.i18n import t as tr\n"


def _excluded_texts() -> set[str]:
    """`文案待改清单.xlsx` 里的句子 —— 用户正在改原文，先不接。

    读不到就照常接并打一行：那张表是临时施工单，不是长期真相源；
    但它一旦存在，跳过它就是有意的（列在 `--apply` 的输出里）。
    """
    p = _ROOT / "locales" / "worksheet" / "文案待改清单.xlsx"
    if not p.exists():
        print("[wire] 没有文案待改清单.xlsx —— 全部照接")
        return set()
    try:
        import openpyxl
    except ImportError:
        print("[wire] 装不了 openpyxl —— **全部照接**（这会让待改句子的键马上作废）")
        return set()
    out: set[str] = set()
    wb = openpyxl.load_workbook(p, data_only=True)
    for ws in wb.worksheets:
        # 表头在第 3 行：序号 / 类别 / 块 / 位置 / 原文 / …
        head = [str(c or "").strip() for c in next(ws.iter_rows(min_row=3, max_row=3, values_only=True))]
        if "原文" not in head:
            continue
        col = head.index("原文")
        for row in ws.iter_rows(min_row=4, values_only=True):
            val = row[col] if col < len(row) else None
            if isinstance(val, str) and val.strip():
                out.add(val.replace("\r\n", "\n").strip())
    print("[wire] 待改清单里的句子 %d 条：**跳过不接**（等改完原文再跑一次）" % len(out))
    return out


def _targets() -> list[dict]:
    """块 01/02 里的 Python 来源（JS 走前端，不在这批）。"""
    out = []
    for name in BLOCKS:
        for src in SCAN.BLOCKS[name]["sources"]:
            if src.get("js") or src["file"] in SKIP_FILES:
                continue
            out.append(dict(src, block=name))
    return out


def _already_wrapped(data: bytes, start: int) -> bool:
    head = data[max(0, start - 16):start].decode("utf-8", "ignore")
    hs = head.rstrip()
    return hs.endswith("tr(") or hs.endswith("t(") or hs.endswith("i18n.t(")


def _wrap_positions(path: Path, spec: dict, excluded: set[str]):
    """→ `(found, skipped)`；`found` 是 `[(start, end, text, line)]`（字节偏移）。

    `ast` 的 `col_offset` 是 UTF-8 字节偏移（Python 3.8+），而本仓库源码里全是中文
必须按字节切，按字符切会切到半个汉子（`UnicodeDecodeError`）。
    """
    src = path.read_text(encoding="utf-8")
    data = src.encode("utf-8")
    tree = ast.parse(src)
    skip = SCAN._docstring_ids(tree) | SCAN._print_ids(tree)
    found, skipped = [], []

    # 只能按 0x0A 切：bytes.splitlines() 把 0x85（NEL）也当换行，
    #    而 `0x85` 是 UTF-8 里 CJK 字符的续字节 —— 一出现就多切一刀，
    #    行首偏移整体错位，最后切在半个汉字上（`UnicodeDecodeError`）。
    base_of: dict[int, int] = {}
    acc = 0
    for i, ln in enumerate(data.split(bytes([10])), 1):
        base_of[i] = acc
        acc += len(ln) + 1

    def consider(node, span, special):
        """`node` 用来取文本/分类；`span` 是要包起来的那一段（`%` 写法包左侧）。"""
        if span is None and not isinstance(node, ast.BinOp):
            span = node
        if isinstance(node, ast.JoinedStr):
            text = SCAN._text_of(node, src) or ""
            if SCAN.CN.search(text):
                skipped.append((node.lineno, "f-string", text[:40]))
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            text = SCAN._text_of(node, src) or ""
            if not SCAN.CN.search(text):
                return
            left = node.left
            if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
                skipped.append((node.lineno, "非字面量左侧", text[:40]))
                return
            span = left
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                return
            if not SCAN.CN.search(node.value):
                return
        else:
            return
        text = span.value
        kind = special.get(id(node)) or special.get(id(span)) or "display"
        if kind != "display" or SCAN._keep({"原文": text, "kinds": {kind}})[0] == 0:
            skipped.append((node.lineno, "keep=0(%s)" % kind, text[:40]))
            return
        if text.replace("\r\n", "\n").strip() in excluded:
            skipped.append((node.lineno, "在待改清单里", text[:40]))
            return
        if getattr(span, "end_lineno", None) != span.lineno or span.end_col_offset is None:
            skipped.append((node.lineno, "多行字面量", text[:40]))
            return
        base = base_of[span.lineno]
        found.append((base + span.col_offset, base + span.end_col_offset, text, node.lineno))

    def walk_fn(fn):
        special = SCAN._non_display_ids(fn)
        composed = SCAN._composed_child_ids(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and id(node) in composed:
                continue
            consider(node, None, special)

    for prefix in spec.get("func_prefixes", []):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith(prefix):
                walk_fn(n)
    # `names`（模块级常量）一律不包 —— 它们是数据，`tr()` 写在值里
    #    会在 import 那一刻求值，语言改了要重启才生效，而同一个屏幕上
    #    命令回话（调用时翻）已经变了，两套语言并存。
    #    正解：数据保持中文，在取用的函数里翻（`activity.describe_tool` 就是这么做的）。
    for fname in spec.get("funcs", []):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fname:
                walk_fn(n)

    # ② `payload` —— `web/server.py` 的 API 出口（dict 里那几个键的值）
    keys = set(spec.get("payload") or [])
    if keys:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in keys:
                    consider(v, None, {})
    return found, skipped


def _convert_fstrings(path: Path, spec: dict, excluded: set[str], apply: bool) -> int:
    """把显示用的 f-string 换成 `trf("模板", 值…)` —— 键保持不变。

    f-string 求值之后只剩结果，而键是模板，`tr()` 永远查不到。
    `trf` 先拿模板翻、再按占位符名字填值（语序可变），见 `core/i18n.py`。

    带格式说明/转换（`{x:.2f}` / `{x!r}`）或 `{{` 转义的一律跳过并打印 ——
    那些要人工处理（`trf` 只做 `str()`）。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    skip = SCAN._docstring_ids(tree) | SCAN._print_ids(tree)
    edits: list[tuple[str, str]] = []

    def consider(node, special):
        if not isinstance(node, ast.JoinedStr) or id(node) in skip:
            return
        text = "".join(
            str(v.value) if isinstance(v, ast.Constant)
            else "{%s}" % (ast.get_source_segment(src, v.value) or "")
            for v in node.values)
        if not SCAN.CN.search(text):
            return
        kind = special.get(id(node), "display")
        row = {"原文": text.strip(), "kinds": {kind}}
        if kind != "display" or SCAN._keep(row)[0] == 0:
            return
        if text.strip() in excluded:
            return
        seg = ast.get_source_segment(src, node)
        exprs = [ast.get_source_segment(src, v.value) for v in node.values
                 if not isinstance(v, ast.Constant)]
        if not seg or any(((":" in e) or ("!" in e)) for e in exprs) or "{{" in seg:
            print("    SKIP-FSTR %s:%d %r" % (path.name, node.lineno, text.strip()[:36]))
            return
        edits.append((seg, "trf(%s%s)" % (json.dumps(text, ensure_ascii=False),
                                          "".join(", " + e for e in exprs))))

    def walk_fn(fn):
        special = SCAN._non_display_ids(fn)
        for node in ast.walk(fn):
            consider(node, special)

    for prefix in spec.get("func_prefixes", []):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith(prefix):
                walk_fn(n)
    for fname in spec.get("funcs", []):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fname:
                walk_fn(n)
    keys = set(spec.get("payload") or [])
    if keys:
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value in keys:
                        consider(v, {})

    # 去重（同一段被 walk 两次）
    seen, uniq = set(), []
    for seg, repl in edits:
        if seg in seen:
            continue
        seen.add(seg)
        uniq.append((seg, repl))
    for seg, repl in uniq:
        src = src.replace(seg, repl)
    if uniq:
        if "tr_fmt as trf" not in src:
            src = src.replace("from core.i18n import t as tr",
                              "from core.i18n import t as tr, tr_fmt as trf")
        if apply:
            path.write_text(src, encoding="utf-8")
    return len(uniq)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写文件（默认只报数）")
    args = ap.parse_args()

    excluded = _excluded_texts()
    total_wrapped = total_skip = 0
    for spec in _targets():
        path = _ROOT / spec["file"]
        if not path.exists():
            print("[wire] 缺文件：%s" % spec["file"])
            continue
        hits, skipped = _wrap_positions(path, spec, excluded)
        if hits:
            # 必须和 `_wrap_positions` 同一套字节：那边先 `read_text()`
            # （通用换行把 CRLF 折成 LF）再 encode，这里若直接 `read_bytes()`
            # 拿到的还是 CRLF，偏移整体错位、切在半个汉字上（`UnicodeDecodeError`）。
            raw = path.read_bytes()
            crlf = bytes([13, 10]) in raw
            data = raw.replace(bytes([13, 10]), bytes([10]))
            hits = [h for h in hits if not _already_wrapped(data, h[0])]
            # 同一段只包一次（dict 值被 walk 两次时会重复）
            seen, uniq = set(), []
            for h in hits:
                if (h[0], h[1]) in seen:
                    continue
                seen.add((h[0], h[1]))
                uniq.append(h)
            hits = uniq
            for start, end, _text, _ln in sorted(hits, key=lambda h: h[0], reverse=True):
                data = data[:start] + b"tr(" + data[start:end] + b")" + data[end:]
            if hits:
                text = data.decode("utf-8")
                if IMPORT_LINE not in text:
                    lines = text.splitlines(keepends=True)
                    # 插在最后一个顶层 import 之后（`from __future__` 必须留在最前）
                    idx = 0
                    for i, ln in enumerate(lines[:80]):
                        if ln.startswith(("import ", "from ")):
                            idx = i + 1
                    lines.insert(idx, IMPORT_LINE)
                    text = "".join(lines)
                if args.apply:
                    out = text.replace(chr(10), chr(13) + chr(10)) if crlf else text
                    path.write_bytes(out.encode("utf-8"))
        n_f = _convert_fstrings(path, spec, excluded, args.apply)
        print("[wire] %-38s 包 %3d · 跳过 %3d · f-string %2d%s"
              % (spec["file"], len(hits), len(skipped), n_f,
                 "" if args.apply else "（dry-run）"))
        for ln, why, txt in skipped[:3]:
            print("         skip %-14s line %-5d %s" % (why, ln, txt.replace("\n", " ")))
        total_wrapped += len(hits)
        total_skip += len(skipped)

    print("[wire] 合计：包 %d · 跳过 %d%s"
          % (total_wrapped, total_skip, "" if args.apply else "（dry-run，加 --apply 才写）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
