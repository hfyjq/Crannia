#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 `requirements.txt` 里每一条钉成今天实际装着的那个版本，写进 `requirements.lock`。

    .venv/Scripts/python.exe scripts/make_requirements_lock.py

## 为什么不是 `pip freeze`

`pip freeze` 导的是整个环境，而环境里有两样东西不该进仓：

- 装着但已经没用的包（开发环境里常有——装过、后来不用了，`pip freeze` 照样导出来）；
- 本地可编辑安装（`-e D:\\...`）—— 那是这台机器的路径，发出去既泄漏又装不上。

所以这里只处理 `requirements.txt` 里点名的那些，逐条查出当前版本。

## 两个文件各自的职责

| 文件 | 说什么 |
|---|---|
| `requirements.txt` | 要什么 —— 下界（`>=`），给的是"最低能用的版本" |
| `requirements.lock` | 我们测过的直接依赖是哪一版 —— 精确钉死 |

装最新能用的：`pip install -r requirements.txt`
按我们测过的直接依赖版本装：`pip install -r requirements.lock`

注意：两个文件都不钉传递依赖：锁把 41 条直接依赖钉死了，而那 94 个传递依赖
仍按安装当天的 PyPI 解析 —— 所以「装出来的组合」与「我们测过的组合」
并不完全是同一套。真正的完全复现要 `pip freeze` 出一份平台专属清单（含
Windows 专有包），那是另一件事，本仓库不做，也别把锁改成那样。
这份锁解决的是"半年后直接装，直接依赖漂到哪去了"。

## 改了 requirements.txt 之后

重新跑本脚本，`tests/test_requirements_lock.py` 会在你忘记时变红。

ASCII-only 输出（Windows 控制台 GBK）。
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_REQ = _ROOT / "requirements.txt"
_LOCK = _ROOT / "requirements.lock"

#: 一行 `name>=1.2,<2.0   # 注释` —— 只取名字那一段。
_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")

_HEADER = """\
# Crannia 依赖锁 —— 直接依赖的版本。
#
# 用法：
#   装最新能用的                     pip install -r requirements.txt
#   按我们测过的直接依赖版本装        pip install -r requirements.lock
#
# 改 requirements.txt 之后重新生成：
#   .venv/Scripts/python.exe scripts/make_requirements_lock.py
# 忘了改的话，tests/test_requirements_lock.py 会红。
#
# 注意：它钉的是**直接依赖**，**不含**传递依赖 —— 那些仍按安装当天的 PyPI
#    解析，所以装出来的组合不保证与我们测过的完全一致。完全复现要一份
#    平台专属的 `pip freeze` 清单，本仓库有意不做。
#    也不含本机的可编辑安装 —— 那些是这台机器的路径，不该进仓。
"""


def _names(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def main() -> int:
    if not _REQ.is_file():
        print("[lock] 找不到 %s" % _REQ)
        return 2
    names = _names(_REQ.read_text(encoding="utf-8"))
    if not names:
        print("[lock] requirements.txt 里一条依赖都没解析出来 —— 解析器坏了？")
        return 2

    rows, missing = [], []
    for name in names:
        try:
            rows.append((name, metadata.version(name)))
        except metadata.PackageNotFoundError:
            missing.append(name)

    if missing:
        print("[lock] 这些没装，先 pip install：%s" % ", ".join(missing))
        return 1

    body = "".join("%s==%s\n" % (n, v)
                   for n, v in sorted(rows, key=lambda r: r[0].lower()))
    _LOCK.write_text(_HEADER + body, encoding="utf-8", newline="\n")
    print("[lock] %d 条钉好，写到 %s" % (len(rows), _LOCK.name))
    print("[lock] 解释器：%s" % sys.version.split()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
