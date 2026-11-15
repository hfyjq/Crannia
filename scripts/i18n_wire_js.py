#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把界面文案包成 `t("…")` —— 前端接线。

    .venv/Scripts/python.exe scripts/i18n_wire_js.py           # dry-run
    .venv/Scripts/python.exe scripts/i18n_wire_js.py --apply

## 它包什么（判据与扫描器同源）

* 只碰 `BLOCKS` 里登记过的 `.js/.jsx` 文件，且只碰工作表里 `保留=1` 的句子；
* 只包静态句（原文里没有 `{…}`）—— 带占位符的要人工决定值怎么传
  （`t("共 {n} 条", { n })`），脚本乱包会把值塞错；
* 四种载体：`"…"` · `'…'` · 反引号（不含 `${}`）· JSX 裸文本（`>…<`）；
* 已在 `t(…)` 里的不重复包（幂等）。

## 没做的两件事

1. 模块级数据表（`settings-view.js` 的 `label`/`note`、`provider-icons.js` 的 `note`）
   不在这里包 —— `t()` 在 import 时求值会定死语言（后端 `_VERBS` 同一个坑）。
   它们由渲染处翻（`t(row.label)`）。
2. 带 `{…}` 的句子一律跳过并报数。

ASCII-only 输出（Windows 控制台是 GBK）。
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("i18n_scan", _ROOT / "scripts" / "i18n_scan.py")
SCAN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SCAN)

IMPORT_LINE = 'import { t } from "./i18n.js";\n'

#: 这些文件是数据表（模块级求值），不包，交给渲染处翻。
SKIP_FILES = {
    "packages/ui/src/settings-view.js",
    "packages/ui/src/provider-icons.js",
    "packages/ui/src/loadingPhrases.js",
    "packages/ui/src/mode.js",
    "packages/ui/src/thread-list.js",
    "packages/ui/src/format.js",
    "packages/ui/src/skills-view.js",
    "packages/ui/src/onboarding-model.js",
    "packages/ui/src/channels-model.js",
    "packages/ui/src/endpoint-form.js",
    "packages/ui/src/group-form.js",
    "packages/ui/src/approval-model.js",
    "packages/ui/src/ask-user-model.js",
    "packages/ui/src/takeover-model.js",
    "packages/ui/src/plugin-notice-view.js",
    "packages/ui/src/trace-view.js",
    "packages/ui/src/i18n.js",
}


def _texts_by_file() -> dict[str, set[str]]:
    """块 → 文件 → 该文件所在块的静态句子集合。"""
    out: dict[str, set[str]] = {}
    ws = _ROOT / "locales" / "worksheet"
    for name, spec in SCAN.BLOCKS.items():
        texts: set[str] = set()
        csv_path = ws / ("%s.csv" % name)
        if csv_path.exists():
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if (r.get("保留") or "").strip() != "1":
                        continue
                    t = (r.get("原文") or "").strip()
                    if t and len(t) >= 2:
                        texts.add(t)
        for src in spec["sources"]:
            rel = src.get("js")
            if rel and rel not in SKIP_FILES:
                out.setdefault(rel, set()).update(texts)
    # 键是全局的（中文原文），每个前端文件都拿全量文本去对，
    #   否则「端配置」这种在别的块里登记的句子永远接不上。
    everything = set()
    for v in out.values():
        everything |= v
    for k in list(out):
        out[k] = set(everything)
    return out


def _module_ranges(src: str) -> list[tuple[int, int]]:
    """模块级常量块的字符区间（`const X = [...]` / `= {...}`，括号配平）。

    这些块在 import 时求值，里面包 `t()` 会在那一刻把语言定死
    （与后端 `_VERBS` 同一个坑）。它们的文案要在渲染处翻（`t(row.label)`）。
    """
    ranges = []
    for m in re.finditer(r"(?m)^(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*",
                         src):
        i = m.end()
        if i >= len(src):
            continue
        if src[i] not in "[{":
            # 标量常量（`const THINKING = "…";`）同样在 import 时求值，也跳过
            end = src.find(";", i)
            ranges.append((i, len(src) if end < 0 else end + 1))
            continue
        depth, j = 0, i
        while j < len(src):
            ch = src[j]
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        ranges.append((i, j))
    return ranges


def _in_ranges(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in ranges)


def _line_of(src: str, pos: int) -> str:
    return src[src.rfind("\n", 0, pos) + 1:pos]


def _in_comment(src: str, pos: int) -> bool:
    line = _line_of(src, pos)
    if line.lstrip().startswith("//"):
        return True
    head = src[:pos]
    return head.rfind("/*") > head.rfind("*/")


def _wrap_file(path: Path, texts: set[str], apply: bool) -> tuple[int, int]:
    src = path.read_text(encoding="utf-8")
    ranges = _module_ranges(src)
    n = 0
    for text in sorted(texts, key=len, reverse=True):
        if "{" in text:          # 动态的（`{n}`）交给 `_wrap_dynamic`
            continue
        # ① 双引号 / 单引号 / 反引号字面量
        for q in ('"', "'", "`"):
            lit = q + text + q
            start = 0
            while True:
                i = src.find(lit, start)
                if i < 0:
                    break
                start = i + len(lit)
                if _in_comment(src, i):
                    continue
                before = src[max(0, i - 3):i]
                if before.endswith("t("):
                    continue
                prev = src[max(0, i - 12):i].rstrip()
                if prev.endswith(("===", "!==", "==", "!=", "case")):
                    continue
                if "import " in _line_of(src, i)[:40]:
                    continue
                if _in_ranges(i, ranges):
                    continue
                src = src[:i] + "t(" + lit + ")" + src[i + len(lit):]
                start = i + len(lit) + 3
                n += 1
        # ② JSX 裸文本（`>…<` 之间），保留两侧空白
        pat = re.compile(r">(\s*)" + re.escape(text) + r"(\s*)<")
        def _sub(m):
            nonlocal n
            if _in_ranges(m.start(), ranges):
                return m.group(0)
            n += 1
            return ">%s{t(%s)}%s<" % (m.group(1), _js(text), m.group(2))
        src = pat.sub(_sub, src)
    if n and apply:
        if 'from "./i18n.js"' not in src:
            lines = src.splitlines(keepends=True)
            idx = 0
            for i, ln in enumerate(lines[:60]):
                if ln.startswith(("import ", "export ")) or ln.startswith("} from"):
                    idx = i + 1
            lines.insert(idx, IMPORT_LINE)
            src = "".join(lines)
        path.write_text(src, encoding="utf-8")
    return n, 0


def _vars_obj(names: list[str]) -> str:
    """把占位符名字做成 `{ "a": a, "b.c": b.c }`（名字就是表达式原文）。"""
    import json as _json
    parts = []
    for n in names:
        n = n.strip()
        key = _json.dumps(n, ensure_ascii=False)
        parts.append("%s: %s" % (key, n))
    return "{ " + ", ".join(parts) + " }"


def _wrap_dynamic(path: Path, texts: set[str], apply: bool) -> int:
    """带 `{…}` 的模板也接上：`t("…{n}…", { n })`。

    判据：只认工作表里那条原文（把 `{expr}` 展开回源表达式后逐字相等），
      所以不会误伤别的字符串。名字就是源码里的表达式原文（`g.members.length`）。
    """
    src = path.read_text(encoding="utf-8")
    ranges = _module_ranges(src)
    n = 0
    for text in sorted(texts, key=len, reverse=True):
        if "{" not in text:
            continue
        names = re.findall(r"\{([^{}]*)\}", text)
        # ① JSX 裸文本（含 `{…}`）
        pat = re.compile(r">(\s*)" + re.escape(text) + r"(\s*)<")
        def _sub(m):
            nonlocal n
            if _in_ranges(m.start(), ranges):
                return m.group(0)
            n += 1
            return ">%s{t(%s, %s)}%s<" % (m.group(1), _js(text), _vars_obj(names), m.group(2))
        src = pat.sub(_sub, src)
        # ② 模板字面量：把 `{x}` 换成 `${x}`
        tpl = "`" + re.sub(r"\{([^{}]*)\}", lambda m: "${" + m.group(1) + "}", text) + "`"
        if tpl in src:
            i = 0
            while True:
                i = src.find(tpl, i)
                if i < 0:
                    break
                if _in_ranges(i, ranges) or _in_comment(src, i):
                    i += len(tpl)
                    continue
                before = src[max(0, i - 3):i]
                if before.endswith("t("):
                    i += len(tpl)
                    continue
                repl = "t(%s, %s)" % (_js(text), _vars_obj(names))
                src = src[:i] + repl + src[i + len(tpl):]
                i += len(repl)
                n += 1
    if n and apply:
        if 'from "./i18n.js"' not in src:
            lines = src.splitlines(keepends=True)
            at = next((i for i, ln in enumerate(lines)
                       if re.match(r"^(export\s+)?(default\s+)?(async\s+)?(function|const|let|var|class)", ln)),
                      len(lines))
            lines.insert(at, IMPORT_LINE)
            src = "".join(lines)
        path.write_text(src, encoding="utf-8")
    return n


def _js(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    total = 0
    for rel, texts in sorted(_texts_by_file().items()):
        p = _ROOT / rel
        if not p.exists():
            print("[wire-js] 缺文件：%s" % rel)
            continue
        n, _ = _wrap_file(p, texts, args.apply)
        n2 = _wrap_dynamic(p, texts, args.apply)
        if n or n2:
            print("[wire-js] %-46s 静态 %3d · 动态 %3d%s"
                  % (rel, n, n2, "" if args.apply else "（dry-run）"))
        total += n + n2
    print("[wire-js] 合计 %d 处%s" % (total, "" if args.apply else "（dry-run，加 --apply 才写）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
