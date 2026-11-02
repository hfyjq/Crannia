#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把翻译工作表回填成运行时语言包（`locales/<lang>.json`）。

    .venv/Scripts/python.exe scripts/i18n_apply.py            # 英文
    .venv/Scripts/python.exe scripts/i18n_apply.py --lang ja  # 以后加的语言

## 产出

    locales/en.json               { 中文原文: English }   ← 运行时读这一份
    locales/_apply_report.txt     明细（占位符不一致 / 冲突 / 孤儿 / 超长）

## 四条规矩（与 `locales/README.md` 一致）

1. 缺译文，不进包，不报错（回落中文，永不显示裸键）。
2. 占位符必须逐个对得上（`%s` / `%(name)s` / `{…}`）—— 对不上的那一行
   不进包，写进报告。宁可回落中文，也不要在运行时抛 `KeyError`。
3. 同一句在不同块翻了两个样子，报冲突，取先出现的那个（块按编号序）。
   这是"台词只有一份"的代价：表是按块分的，而键是原文。
4. 旧译文变孤儿（原文改了，键变了），报出来，不静默丢。
   孤儿不进 `en.json`，但报告里逐条列出，由人决定复用还是重翻。

输出只打 ASCII 计数（Windows 控制台是 GBK，而文案里满是 emoji）；
   逐条明细写进报告文件（UTF-8），用编辑器看。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
OUT = _ROOT / "locales"
WS = OUT / "worksheet"

#: 占位符：Python 的 `%s` / `%(name)s` + `{…}`（JS 表达式也算）。
_PH = re.compile(r"%\([A-Za-z_]\w*\)[sdrf]|%[sdrf]|\{[^{}]*\}")

#: 占位符里引号内的文案允许翻译 —— 它本身就是给人看的字：
#:   `{ask.row.button || "执行"}` => `{ask.row.button || "Run"}` 是对的。
#:   比"占位符结构"时必须先把它们抹成 `""`，否则这 7 条会被误判成占位符不一致。
_PH_STR = re.compile(r'"[^"]*"|\'[^\']*\'')


def _ph_key(ph: str) -> str:
    return _PH_STR.sub('""', ph)

#: 写成 `\u` 转义而不是原字：右端点那个字不在 GBK 里，
#: 写成原字会被 `scripts/check_gbk_safe.py` 记一笔（它只管"会不会崩"，而这行不打印
#: —— 但"不增长"的判据是按文件数的，何必让它多一行）。
CN = re.compile("[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]")   # ASCII-only source

#: 英文超过中文字数的这个倍数，只提示（按钮/标签会截断），不拦。
LEN_RATIO = 1.8


def _blocks() -> list[Path]:
    """块表 = `NN-name.csv`，不含 `.skipped.csv` 与 `00-all.csv`。"""
    out = []
    for p in sorted(WS.glob("[0-9][0-9]-*.csv")):
        name = p.name
        if ".skipped" in name or name.startswith("00-"):
            continue
        out.append(p)
    return out


def _rows(path: Path, lang: str) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f)]


def apply_lang(lang: str) -> int:
    src_path = OUT / "source.json"
    if not src_path.exists():
        print("[apply] 没有 source.json —— 先跑 scripts/i18n_scan.py")
        return 2
    source = json.loads(src_path.read_text(encoding="utf-8"))

    pack: dict[str, str] = {}
    where: dict[str, str] = {}
    conflicts: list[str] = []
    bad_ph: list[str] = []
    too_long: list[str] = []
    covered: dict[str, list[int]] = {}
    unseen: set[str] = set()

    col = "译文_%s" % lang
    for path in _blocks():
        block = path.name[:-4]
        n_keep = n_done = 0
        for r in _rows(path, lang):
            text = (r.get("原文") or "").strip()
            if not text:
                continue
            unseen.add(text)
            keep = (r.get("保留") or "").strip()
            if keep != "1":
                continue
            n_keep += 1
            val = (r.get(col) or "").strip()
            if not val:
                continue
            n_done += 1
            a = sorted(_ph_key(x) for x in _PH.findall(text))
            b = sorted(_ph_key(x) for x in _PH.findall(val))
            if a != b:
                bad_ph.append("%s  [%s]  %s\n    ZH: %s\n    EN: %s"
                              % (r.get("id"), block, r.get("位置"), text, val))
                continue
            prev = pack.get(text)
            if prev is None:
                pack[text] = val
                where[text] = block
            elif prev != val:
                conflicts.append("%s\n    先（%s）：%s\n    又（%s）：%s"
                                 % (text, where[text], prev, block, val))
            cn = len(CN.findall(text)) or 1
            if len(val) > LEN_RATIO * cn:
                too_long.append("%s  %d 字 -> %d 字符\n    ZH: %s\n    EN: %s"
                                % (block, cn, len(val), text, val))
        covered[block] = [n_done, n_keep]

    # 孤儿：上一次的包里、这次的表里一个都找不到（原文被改过，键变了）
    old_path = OUT / ("%s.json" % lang)
    orphans: list[str] = []
    if old_path.exists():
        try:
            old = json.loads(old_path.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001
            print("[apply] 旧包读不出来（忽略）：%s" % type(exc).__name__)
            old = {}
        orphans = sorted(k for k in old if k not in unseen)

    # 写盘：先临时文件、后替换（写一半不会留下半个包）
    tmp = old_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(sorted(pack.items())), ensure_ascii=False,
                              indent=2) + "\n", encoding="utf-8")
    tmp.replace(old_path)

    # 报告（UTF-8；控制台只打计数）
    lines = ["# i18n apply report — lang=%s" % lang, ""]
    lines.append("## 覆盖率（已填 / 保留=1）")
    for block, (done, keep) in covered.items():
        lines.append("  %-18s %4d / %4d  %s"
                     % (block, done, keep, "" if keep == done else "  <-- 未填完"))
    total_done = sum(v[0] for v in covered.values())
    total_keep = sum(v[1] for v in covered.values())
    lines.append("")
    lines.append("TOTAL %d / %d" % (total_done, total_keep))
    for title, items in (("占位符不一致（未进包）", bad_ph),
                         ("同一句两个译法（取先出现的）", conflicts),
                         ("新旧原文孤儿（未进包）", orphans),
                         ("超过 %.1fx 长度（仅提示）" % LEN_RATIO, too_long)):
        lines.append("")
        lines.append("## %s：%d" % (title, len(items)))
        lines.extend("  " + x.replace("\n", "\n  ") for x in items)
    (OUT / "_apply_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[apply] %s: %d/%d 条已填 -> %s"
          % (lang, total_done, total_keep, old_path.name))
    print("[apply] 占位符不一致 %d · 冲突 %d · 孤儿 %d · 超长提示 %d"
          % (len(bad_ph), len(conflicts), len(orphans), len(too_long)))
    print("[apply] 明细 -> locales/_apply_report.txt")
    return 1 if (bad_ph or conflicts) else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en")
    args = ap.parse_args()
    return apply_lang(str(args.lang).strip() or "en")


if __name__ == "__main__":
    sys.exit(main())
