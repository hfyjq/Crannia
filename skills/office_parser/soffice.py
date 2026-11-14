"""LibreOffice 的发现与调用参数 —— 候选路径只许一份。

## 为什么单独一个文件

`convert_to_pdf` 与 `excel_transform.recalculate_excel_formulas` 都要用它。
两条路各留一份 = 两份会漂移，而两个答案看着都对。
`excel_transform` 不能 import `handler` —— `handler.py` 那一大片
re-export 会绕回来，循环 import。所以共用件必须住在第三方位置，就是这里。

## 可达性

`C:\\Program Files\\LibreOffice\\program\\soffice.exe` 是 Windows 默认安装路径；
该文件存在时，公式重算那条路才能跑。不存在时由 `missing_hint()` 给出替代路线。
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: 候选路径，顺序即优先级。
#: 前面是绝对路径、后面才是命令名 —— 与 `core/shell.py::_BASH_CANDIDATES`
#: 同一条判据：绝对路径优先，因为 `soffice` 在 Windows 上不落 PATH 是常态。
SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/libreoffice",
    "/usr/bin/soffice",
    "soffice",
    "libreoffice",
)

#: 转换超时（秒）。必须给：LibreOffice 无头模式卡住是常态，
#: 而一个挂住的进程比一条报错难查得多（`core/shell.py` 那条线记过同族的事）。
CONVERT_TIMEOUT_S = 120


def find_soffice():
    """找 LibreOffice 的可执行文件。找不到回 `None`（不抛）。"""
    for cand in SOFFICE_CANDIDATES:
        try:
            if shutil.which(cand):
                return cand
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if Path(cand).is_file():
                return cand
        except OSError:
            continue
    return None


def missing_hint(purpose: str = "这个操作") -> dict:
    """找不到 LibreOffice 时的带路线回执（拒绝要给得兑现的路线）。

    不许只说"失败了" —— 要告诉模型还能怎么办。
    """
    return {
        "success": False,
        "error": f"{purpose}需要 LibreOffice，但这台机器上没找到"
                 f"（试过 {len(SOFFICE_CANDIDATES)} 个候选路径）。",
        "hint": ("两条路：(1) 装一个 LibreOffice（libreoffice.org，免费）后重试；"
                 "(2) 改用 read_excel_data(formula_mode='cached') 读**缓存值** —— "
                 "那不需要 LibreOffice，只是读不到从没在 Excel 里打开过的文件里"
                 "的公式结果。"),
    }
