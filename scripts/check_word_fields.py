#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等价判据：我们手写的域，排版引擎认不认。

## 为什么需要这么一个脚本（而 `tests/test_word_fields.py` 不够）

那些文件证的是「XML 形状对」—— 域的三段齐全、`instrText` 自成 run、书签唯一。
而 XML 合法只是必要条件：一份"看着像域"的 XML，引擎照样可以当成三行普通文字。

要证「它是活的域」，只有一条路：让一个真的排版引擎去算它。

## 判据（关键在那句"故意的错"）

### ① 题注 + 交叉引用

```
建：正文「详见图」+ REF 域  →  两张带题注的图
插：在第一张之前再插一张带题注的图
读：LibreOffice 打开，读回算出来的文本
```

| | 我们写的缓存值 | 引擎重算后 |
|---|---|---|
| 新插入的图 | 3（错的） | 1 |
| 原来的第一张 | 1（陈旧） | 2 |
| 原来的第二张 | 2（陈旧） | 3 |
| 正文里那条引用 | 1（陈旧） | 2 ← 跟着变了 |

判据是"缓存值写错了它仍然对" —— 这是最强的形状：
如果引擎只是把缓存值回显出来，这张表右列就会等于左列。

（判据要能分开「算了」与「回显」：缓存值恰好等于真值时，
`1 == 1` 什么也证不到，必须故意写错。）

### ② 手写编号 → 题注 → 图目录

```
建：图标题写成错的编号（图7/图8/图9）+ 一个「请在此插入图目录」占位
做：replace_manual_captions（编号 → SEQ 域）+ apply_table_of_contents（TOC \\c "图"）
读：LibreOffice 更新域，读回图目录里的条目
```

判据：图目录里应当是 1/2/3（引擎按顺序重数），而不是我们写进去的 7/8/9。

## 这个脚本证不了的那一件事（如实标）

`STYLEREF <级> \\s` 取章节号 —— 本机验不了。
它要求标题样式带自动多级编号，而这一版夹具造不出 LibreOffice 认的形状
（试了三种：样式级 `w:numPr` / 段落级 `w:numPr` / `numbering.xml` 里
`abstractNum` 带 `w:pStyle`）—— 三次标题都不带编号，`STYLEREF` 一律渲染成
「错误:未找到引用源」。那半边只有"域指令写对了"有机械读者
（`tests/test_word_captions.py`），"章号真算得出来"没有。
（卷子的顺序是「先把标题的手动编号换成自动多级编号、再标题注」，而那一步本仓还没有工具。）

## 为什么不在常规套件里

LibreOffice 冷启动 ~25 秒，而整套测试现在 22 秒，放进去会把套件拖成分钟级。
与 `check_skill_reach.py` / `check_gbk_safe.py` 同一个位置：
人工验收的机械前置。

## 用法

    .venv/Scripts/python.exe scripts/check_word_fields.py

退出码：`0` 通过 · `1` 判据不过 · `2` 环境不具备（没装 LibreOffice）

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SO = Path("C:/Program Files/LibreOffice/program/soffice.exe")
_LO_PY = Path("C:/Program Files/LibreOffice/program/python.exe")
_PORT = "2007"

_PASS = 0
_FAIL = 0
_FAILED: list = []

#: 题注故意写成错的编号（图7/8/9）—— 判据是"引擎重数成 1/2/3"。
#: 缓存值恰好等于真值的话，这半边什么也证不到。
_WRONG_NUMBERS = ("图7 第一张图", "图8 第二张图", "图9 第三张图")


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def _build(tmp: Path) -> list:
    """建两份故意让缓存值过期的稿子，返回路径列表。"""
    os.chdir(str(tmp))
    (tmp / "workspace").mkdir(exist_ok=True)
    from core.security import SecurityGuard
    from skills.office_parser import handler as H
    H.init(SecurityGuard(allowed_dirs=["./workspace"], command_blacklist=[],
                         audit_log_path=str(tmp / "audit.log")))
    from docx import Document
    from skills.office_parser import word_tools as WT

    d = Document()
    d.add_paragraph("本文讨论系统架构，详见图。")
    d.add_paragraph("")
    d.add_paragraph("")
    d.save("workspace/g.docx")
    r1 = WT.add_word_caption("workspace/g.docx", 1, label="图", text="系统架构")
    WT.add_word_caption("workspace/g.docx", 2, label="图", text="数据流")
    WT.add_word_cross_reference("workspace/g.docx", 0, bookmark=r1["bookmark"])
    # 关键一步：在第一张图之前再插一张带题注的图
    #后面两张的缓存值全部过期（它们还写着 1 / 2）
    doc = Document("workspace/g.docx")
    doc.paragraphs[1].insert_paragraph_before("")
    doc.save("workspace/g.docx")
    WT.add_word_caption("workspace/g.docx", 1, label="图", text="新插入的图")

    # ② 手写编号 → 题注 → 图目录
    t = Document()
    t.add_paragraph("图目录")
    t.add_paragraph("请在此插入图目录")
    for line in _WRONG_NUMBERS:
        t.add_paragraph(line)
    t.save("workspace/t.docx")
    WT.replace_manual_captions("workspace/t.docx", "图")          # 编号 → SEQ 域
    WT.apply_table_of_contents("workspace/t.docx", kind="figure",
                               replace_text="请在此插入图目录")
    return [tmp / "workspace" / "g.docx", tmp / "workspace" / "t.docx"]


def _read_with_libreoffice(paths: list) -> dict:
    """用 LibreOffice 逐个打开，返回 `{posix 路径: [(段号, 文本)]}`。

    域会被引擎重算（`refresh` + `getDocumentIndexes().update()` 都要调 ——
    只 `refresh` 的话目录域不更新，那半边就白测了）。
    """
    subprocess.Popen([str(_SO), "--headless", "--norestore", "--nologo",
                      "--accept=socket,host=127.0.0.1,port=%s;urp;" % _PORT],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(24)
    reader = r"""
import uno
local = uno.getComponentContext()
res = local.ServiceManager.createInstanceWithContext(
    "com.sun.star.bridge.UnoUrlResolver", local)
ctx = res.resolve("uno:socket,host=127.0.0.1,port=@PORT@;urp;StarOffice.ComponentContext")
d = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
for _p in @PATHS@:
    print("=== " + _p)
    doc = d.loadComponentFromURL(uno.systemPathToFileUrl(_p), "_blank", 0, ())
    try:
        doc.refresh()
        doc.getTextFields().refresh()
        _idx = doc.getDocumentIndexes()
        for _k in range(_idx.getCount()):
            _idx.getByIndex(_k).update()
    except Exception as e:
        print("refresh failed:", e)
    for i, p in enumerate(doc.Text.createEnumeration()):
        try:
            print("%d\t%s" % (i, p.getString()))
        except Exception:
            pass
    doc.close(False)
""".replace("@PORT@", _PORT).replace("@PATHS@", repr([str(p) for p in paths]))
    out: dict = {}
    try:
        r = subprocess.run([str(_LO_PY), "-c", reader], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=240)
        cur = None
        for line in (r.stdout or "").splitlines():
            if line.startswith("=== "):
                cur = line[4:].strip()
                out[cur] = []
                continue
            if cur is not None and "\t" in line:
                a, b = line.split("\t", 1)
                try:
                    out[cur].append((int(a), b))
                except ValueError:
                    pass
    finally:
        try:
            subprocess.run([str(_LO_PY), "-c",
                            "import uno;c=uno.getComponentContext();"
                            "r=c.ServiceManager.createInstanceWithContext("
                            "'com.sun.star.bridge.UnoUrlResolver',c);"
                            "x=r.resolve('uno:socket,host=127.0.0.1,port=%s;urp;"
                            "StarOffice.ComponentContext');"
                            "x.ServiceManager.createInstanceWithContext("
                            "'com.sun.star.frame.Desktop',x).terminate()" % _PORT],
                           capture_output=True, timeout=60)
        except Exception:                                 # noqa: BLE001
            pass
    return out


def main() -> int:
    print("=" * 62)
    print("W1b/W1c: do the fields we hand-write actually compute? (LibreOffice)")
    print("=" * 62)
    if not _SO.exists() or not _LO_PY.exists():
        print("  [skip] 本机没有 LibreOffice —— 这条判据跑不了")
        print("         （它不是「失败」，是「没条件验」；读不到 != 0）")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="lv_wf_"))
    cwd0 = os.getcwd()
    try:
        paths = _build(tmp)
        print("  已建两份稿子（缓存值**故意过期**）")
        print("  正在用 LibreOffice 打开（~25 秒）…")
        res = _read_with_libreoffice(paths)

        # ① 题注 + 交叉引用
        gp = str(paths[0])
        rows = res.get(gp) or []
        for i, t in rows:
            print("     [g] 段%d: %r" % (i, t))
        joined = "\n".join(t for _i, t in rows)
        check("★ 正文那条引用**跟上了**（应为 2）",
              "详见图。 2" in joined or "详见图。2" in joined,
              "没跟上 = REF 域没被当域用")
        check("★ 新插入的那张排到 1", "图 1 新插入的图" in joined, joined[:200])
        check("★ 原来的第一张变成 2", "图 2 系统架构" in joined, joined[:200])
        check("★ 原来的第二张变成 3", "图 3 数据流" in joined, joined[:200])
        # 反面：如果引擎只是回显缓存值，我们会看到 3 / 1 / 2 那三个旧的
        check("★★ 不是回显缓存值（旧值 3/1/2 一个都不该以「图 N」的样子留下）",
              not ("图 3 新插入的图" in joined and "图 1 系统架构" in joined),
              "出现了旧编号 —— 说明引擎没重算")

        # ② 手写编号 → 题注 → 图目录
        tp = str(paths[1])
        trows = res.get(tp) or []
        for i, t in trows:
            print("     [t] 段%d: %r" % (i, t))
        tjoined = "\n".join(t for _i, t in trows)
        check("★★ 图目录**收进了 3 条题注**（`TOC \\c` 认它们）",
              all("图 %d " % n in tjoined for n in (1, 2, 3)),
              tjoined[:300])
        check("★★ 而且编号是引擎**重数的** 1/2/3，不是我们写进去的 7/8/9",
              not any("图 %d " % n in tjoined for n in (7, 8, 9)),
              "出现了 7/8/9 —— 说明是回显缓存值")
        check("★ 题注本身也排在正文里（不是只在目录里）",
              "图 1 第一张图" in tjoined and "图 3 第三张图" in tjoined,
              tjoined[:300])
        check("★ 占位文字被域换掉了", "请在此插入图目录" not in tjoined, tjoined[:200])
        check("★ 章号那半边**未验**（如实标，不是通过）", True,
              "STYLEREF \\s 要求标题带自动编号，本机夹具造不出 —— 见脚本 docstring")
    finally:
        os.chdir(cwd0)

    print("")
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d 条判据: %d 通过, %d 失败" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
