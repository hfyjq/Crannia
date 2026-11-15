"""
Excel manipulation tools for office_parser skill.

Ported from excel-mcp-server (haris-musa) and adapted to Crannia's
tool-call conventions. MIT License, Copyright (c) 2025 Haris.
License text: `references/LICENSE-excel-mcp-server.txt`.
All functions follow the pattern: accept explicit parameters, return
{"success": True/False, ...} dicts.

Dependencies: openpyxl (already required by office_parser).
"""

from __future__ import annotations

import logging
import re
import uuid
from copy import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import load_workbook, Workbook
from openpyxl.chart import (
    AreaChart, BarChart, BubbleChart, DoughnutChart, LineChart,
    PieChart, ProjectedPieChart, RadarChart, ScatterChart, StockChart,
    SurfaceChart, Reference, Series,
)
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.legend import Legend
from openpyxl.chart.axis import ChartLines
from openpyxl.drawing.spreadsheet_drawing import (
    AnchorMarker, OneCellAnchor, SpreadsheetDrawing,
)
from openpyxl.formatting.rule import (
    ColorScaleRule, DataBarRule, IconSetRule, FormulaRule, CellIsRule,
)
from openpyxl.styles import (
    PatternFill, Border, Side, Alignment, Protection, Font, Color,
)
from openpyxl.utils import get_column_letter, column_index_from_string
from openpyxl.worksheet.table import Table, TableStyleInfo

# 安全接线 + 原子写的唯一落点，全在 `_excel_io`。
# `init` 是转调口：`skill_loader` 只认识 `handler.init`，那个函数里必须
# 多一行 `excel_tools.init(security)`（已加），否则这边恒为 None，fail-closed。
from ._excel_io import guarded_resolve, init, overwrite_blocked, save_workbook  # noqa: F401  (init 供 handler 转调)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _read_excel_data_xlrd(
    path: Path, sheet_name: str, start_cell: str, end_cell: Optional[str]
) -> dict:
    """Read data from a .xls file via xlrd."""
    try:
        import xlrd
    except ImportError:
        return {"success": False, "error": "需要安装 xlrd 库来读取 .xls 文件。请运行: pip install xlrd"}

    try:
        wb = xlrd.open_workbook(str(path))
        sn = sheet_name or wb.sheet_names()[0]
        if sn not in wb.sheet_names():
            return {"success": False, "error": f"工作表 '{sn}' 不存在"}

        sh = wb.sheet_by_name(sn)
        sr, sc, er, ec = _parse_cell_range(start_cell, end_cell)
        # Convert 1-based to 0-based for xlrd
        sr0, sc0 = max(0, sr - 1), max(0, sc - 1)
        er0 = min(sh.nrows - 1, er - 1) if er else sh.nrows - 1
        ec0 = min(sh.ncols - 1, ec - 1) if ec else sh.ncols - 1

        if sr0 >= sh.nrows or sc0 >= sh.ncols:
            return {"success": True, "range": f"{start_cell}:{end_cell or start_cell}",
                    "sheet_name": sn, "cells": []}

        cells = []
        for r in range(sr0, er0 + 1):
            for c in range(sc0, ec0 + 1):
                val = sh.cell_value(r, c)
                if isinstance(val, float) and val == int(val):
                    val = int(val)
                addr = f"{get_column_letter(c + 1)}{r + 1}"
                cells.append({
                    "address": addr, "value": val,
                    "row": r + 1, "column": c + 1,
                })
        return {
            "success": True,
            "sheet_name": sn,
            "range": f"{start_cell}:{end_cell or start_cell}",
            "cells": cells,
        }
    except Exception as e:
        return {"success": False, "error": f"读取 .xls 数据失败: {type(e).__name__}: {e}"}


def _resolve_xlsx_path(file_path: str, op: str = "write") -> Path:
    """解析路径 + 过安全核 —— 全部 Excel 工具的唯一收口。

    这个函数同时是安全校验点：Excel 工具不能只拼路径后直接
    `open()` / `save()`，必须在这里先过 `validate_path`。

    `op` 的语义表、为什么不能统一写 `"write"`、以及为什么每一处都要
    显式写：全在 `_excel_io.guarded_resolve` 的 docstring 里，不在这儿重复。
    机械读者：`tests/test_office_guard.py`（按 AST 反查每一处都有显式 op）。
    """
    return guarded_resolve(file_path, op)


def _parse_cell_range(
    start_cell: str, end_cell: Optional[str] = None
) -> tuple:
    """Parse cell references into (start_row, start_col, end_row, end_col).

    Returns (start_row, start_col, end_row, end_col) where rows are 1-indexed
    ints and cols are 1-indexed ints.  If *end_cell* is None, end == start.
    """
    def _split(ref: str) -> tuple[int, int]:
        col_str = "".join(c for c in ref if c.isalpha()).upper()
        row_str = "".join(c for c in ref if c.isdigit())
        if not col_str or not row_str:
            raise ValueError(f"Invalid cell reference: {ref}")
        return int(row_str), column_index_from_string(col_str)

    sr, sc = _split(start_cell)
    if end_cell:
        er, ec = _split(end_cell)
    else:
        er, ec = sr, sc
    return sr, sc, er, ec


def _format_range_str(start_row, start_col, end_row, end_col) -> str:
    return f"{get_column_letter(start_col)}{start_row}:{get_column_letter(end_col)}{end_row}"


def _outside_range_note(ws, sr: int, sc: int, er: int, ec: int) -> str:
    """说清"你要的范围之外还有什么"。

    ## 为什么需要它

    模型读 `9月!A5:H12` 时若正好漏掉 I–L 列
    （`累计完成` / `年计划` / `完成进度` / `上年度累计`），
    它会自己把 9 个月的当月值加一遍当"累计"，而表里早就有权威的累计列。

    代价：利润总额报 850.75，表里是 951.66（差 100.91 万元），
    同比从 -8.7% 被写成 -18.4%，而它一处都没察觉 ——
    全篇结论架在错数上，语气零不确定。

    这里刻意不猜列义（不去认哪一列是"累计"）—— 只报"那边有非空的东西"。
    猜列义会另开一个会漂移的判定面（一个判定两处）。列叫什么，模型自己看。

    ## 必须同时查左边那一向

    只查右和下（`max_c > ec` / `max_r > er`）不够 —— 左边那片
    往往是行名 / 分组标签：

    模型把一张表切成四条横带读（每条 × 9 张表）：
    `A1:H30`（标签）· `I1:O62`（纯数字，没有标签） · `P1:T62` · `A31:H62`（标签）。
    而回执对 `I1:O62` 若只说「右边的 P 列里有非空数据」
    —— 一次都不提"你左边的 A–H 才是这些行的行名" ——
    模型只能靠数行号把 `I:O` 拼回 `A:B` 的标签上（61 行 × 9 张表），错了一行：

        报「交易量 1-9 月累计 99998」 —— 那是 `L33`，行标签 `2025年实际`；
        而 `L32`（`2026年实际`）才是 97396。

    措辞不猜列义（同上一段：猜列义会另开一个会漂移的判定面）——
    只说"左边那片多半是行名 / 分组标签"，以及该做的动作（先把行对上）。

    只扫"与请求同高"的那一段（`sr..er` × 多出来的列）—— 成本有界，
    不会因为表有 60 万行就变慢。
    """
    try:
        max_c = int(getattr(ws, "max_column", 0) or 0)
        max_r = int(getattr(ws, "max_row", 0) or 0)
    except Exception:                                   # noqa: BLE001
        return ""
    # `sr <= 1` 这一半不能漏 —— 漏了的话，"一小块、右边下面都没东西"的读
    # 会在这里提前返回，「上面紧邻那行」永远没机会说话。
    if sc <= 1 and sr <= 1 and max_c <= ec and max_r <= er:
        return ""

    # 用 `iter_rows` 另起一趟 —— `read_only=True` 下 `ws.cell(r,c)` 不可靠
    def _nonempty_cols(min_col: int, max_col: int) -> list:
        hit: list = []
        if max_col < min_col:
            return hit
        try:
            for row in ws.iter_rows(min_row=sr, max_row=er,
                                    min_col=min_col, max_col=max_col, values_only=True):
                for j, v in enumerate(row):
                    if v is not None and str(v).strip() != "":
                        letter = get_column_letter(min_col + j)
                        if letter not in hit:
                            hit.append(letter)
        except Exception:                               # noqa: BLE001
            return []
        return hit

    def _span(hit: list) -> str:
        return hit[0] if len(hit) == 1 else "%s–%s" % (hit[0], hit[-1])

    left = _nonempty_cols(1, sc - 1) if sc > 1 else []
    right = _nonempty_cols(ec + 1, max_c) if max_c > ec else []

    # 上面紧邻的那一行。只查"紧邻"（`sr - 1`），不查整片上方 ——
    # 否则只要不是从第 1 行开始读就恒真，提示立刻变噪声。
    # 判据：紧邻那一行在同列带里有数字，你多半是从一块数据的中间开始读的
    # （那一行通常是这块的表头或上一行数据）。
    # 若漏掉紧邻行，模型读 `A55:H61`（9 张表）而上一行 54 就是花鲢，
    # 于是花鲢只剩 1 月的值，它却反过来宣称「原表未填均价」。
    # 判据必须收窄到「那一行里有数字」，不是"有非空" ——
    # 只要不是从块边界开始读，"上一行非空"几乎恒真，
    # `A54:H60`（含花鲢、读全了）也会被报（它的上一行 53 是表头）。
    #   表头行，全是文字，不报（那是这一块的天然边界）
    #   数据行，同列带里有数字，报（你多半是从一块数据的中间开始的）
    above = False
    if sr > 1:
        try:
            _row = next(ws.iter_rows(min_row=sr - 1, max_row=sr - 1,
                                     min_col=sc, max_col=ec, values_only=True))
            above = any(isinstance(v, (int, float)) and not isinstance(v, bool)
                        for v in _row if v is not None)
        except Exception:                               # noqa: BLE001
            above = False

    # 同一把尺子用在下边：`er + 1` 也是个数据行，你在块的中间停下了。
    # 模型读 `A51:J59`（价格块 51–60 行），
    # 正好把最后一行的鲈鱼留在外面 —— 于是报告只有六类鱼，
    # 还给它起名叫「六类主要品种」，而鲈鱼均价 13.46（比它列出来的都高）。
    # 只说「下面到第 61 行还有内容」太泛：61 行是空的，60 行才是那条鱼。
    below_adjacent = False
    if er < max_r:
        try:
            _row = next(ws.iter_rows(min_row=er + 1, max_row=er + 1,
                                     min_col=sc, max_col=ec, values_only=True))
            below_adjacent = any(isinstance(v, (int, float)) and not isinstance(v, bool)
                                 for v in _row if v is not None)
        except Exception:                               # noqa: BLE001
            below_adjacent = False

    parts, advice = [], []
    if above:
        parts.append("**上面紧邻的第 %d 行**里也有内容" % (sr - 1))
    if right:
        parts.append("右边的 **%s 列**里有非空数据" % _span(right))
    if below_adjacent:
        parts.append("**下面紧邻的第 %d 行也是个数据行**（和这一块同一批）" % (er + 1))
    elif max_r > er:
        parts.append("**下面到第 %d 行**还有内容" % max_r)
    if left:
        advice.append("左边那片 `%s` 多半是**这些行的行名 / 分组标签** ⇒ "
                      "**引用手上这些数之前，先把它的行对上，别按位置数**" % _span(left))
    if above:
        advice.append("**上面紧邻那一行别落下** —— 多半是这一块的表头或上一行数据"
                      "（少一行，整块的归属就会错位）")
    if below_adjacent:
        advice.append("**下面紧邻那一行也别落下** —— 你停在了一块数据的**中间**"
                      "（少一行，这一批「品种 / 科目」的集合就不完整）")
    if parts:
        advice.append("表里若已写着 `累计` / `合计` / `小计` / `年计划` 这类列，"
                      "**就该用它那个数**（那是这张表的权威口径）；"
                      "**若你自己算了，必须跟它对一次账，对不上要说出来**")
    # "不许把样本当整表" ——
    #   错法是把"我给它的那一段"说成"原表就是这样"。
    #   光说"别下结论"没有用 —— 必须同时给它下一刀的确切参数
    #   （警告里直接把 `start_cell` / `end_cell` 写出来）。
    if max_r > er:
        advice.append('★ **别对你没拿到的那些行下任何结论**（别说「原表没有」「就这些」）'
                      '—— 要就接着读：start_cell="%s%d", end_cell="%s%d"'
                      % (get_column_letter(sc), er + 1, get_column_letter(ec),
                         min(max_r, er + max(50, er - sr + 1))))
    if max_c > ec:
        advice.append("★ **右边那几列同理**（别断言「没有」）—— 要一起读就把 `end_cell` "
                      "放到 `%s` 列" % get_column_letter(max_c))
    if not advice:
        return ""

    head = "★ 你只读了 `%s%s:%s%s`" % (get_column_letter(sc), sr,
                                       get_column_letter(ec), er)
    if parts:
        head += "，而%s" % "，".join(parts)
    return head + "。**先去看那边**：" + "；".join(advice) + "。"


def _display_width(s: str) -> int:
    """估算一个字符串在 Excel 里占几个字符宽。

    判据：Excel 的列宽单位是"默认字体的字符数"，而一个汉字约等于两个
    西文字符宽 —— 按 `len()` 数会把中文列估窄一半，`auto_width` 之后中文
    还是被截断（而那看起来像"没生效"）。
    取法是东亚宽度的近似：CJK 区段 + 全角标点记 2，其余记 1。
    """
    n = 0
    for ch in str(s):
        o = ord(ch)
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
                or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
                or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
                or 0xFFE0 <= o <= 0xFFE6 or 0x20000 <= o <= 0x3FFFD):
            n += 2
        else:
            n += 1
    return n


# Workbook metadata

def get_workbook_metadata(
    file_path: str,
    include_ranges: bool = False,
) -> dict:
    """Get metadata about an Excel workbook (.xlsx or .xls)."""
    path = _resolve_xlsx_path(file_path, "read")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    ext = path.suffix.lower()

    # .xls → xlrd
    if ext == ".xls":
        try:
            import xlrd
            wb = xlrd.open_workbook(str(path))
            info: dict = {
                "filename": path.name,
                "sheets": wb.sheet_names(),
                "sheet_count": wb.nsheets,
                "size": path.stat().st_size,
            }
            if include_ranges:
                ranges = {}
                for sn in wb.sheet_names():
                    sh = wb.sheet_by_name(sn)
                    if sh.nrows > 0 and sh.ncols > 0:
                        ranges[sn] = f"A1:{get_column_letter(sh.ncols)}{sh.nrows}"
                info["used_ranges"] = ranges
            return {"success": True, **info}
        except ImportError:
            return {"success": False, "error": "需要安装 xlrd 库来读取 .xls 文件。请运行: pip install xlrd"}
        except Exception as e:
            return {"success": False, "error": f"读取 .xls 元数据失败: {type(e).__name__}: {e}"}

    # .xlsx → openpyxl
    try:
        wb = load_workbook(path, read_only=True)
        info: dict = {
            "filename": path.name,
            "sheets": wb.sheetnames,
            "sheet_count": len(wb.sheetnames),
            "size": path.stat().st_size,
        }
        if include_ranges:
            ranges = {}
            for sn in wb.sheetnames:
                ws = wb[sn]
                if ws.max_row > 0 and ws.max_column > 0:
                    ranges[sn] = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
            info["used_ranges"] = ranges
        wb.close()
        return {"success": True, **info}
    except Exception as e:
        return {"success": False, "error": f"读取工作簿元数据失败: {type(e).__name__}: {e}"}


# Read / Write data

def read_excel_data(
    file_path: str,
    sheet_name: str = "",
    start_cell: str = "A1",
    end_cell: Optional[str] = None,
    include_validation: bool = False,
    compact: bool = False,
    columns: Optional[List[str]] = None,
    merge_mode: str = "raw",
    formula_mode: str = "auto",
    offset: int = 0,
    max_rows: Optional[int] = None,
) -> dict:
    """读取 Excel 单元格区域（.xlsx / .xls）。

    ## 三种读法（挑一个）

    | 场景 | 怎么用 |
    |---|---|
    | 看一小块（默认） | 什么都不给 —— 逐格回 `{address, value, row, column}` |
    | 大表翻页 | `compact=True` + `max_rows` + `offset` —— 回二维数组，走流式 |
    | 同一块区域 × 全簿/多表 | `sheet_name="*"` 或 `sheet_name=["1月","5月"]` |

    ## 四个关键参数

    * `formula_mode` —— `"auto"`（默认）：有缓存值就给值、没有就给公式串，
      两种都带 `formula` 标记（没值的另带 `formula_uncached`）；
      `"cached"` 只要值；`"formula"` 只要公式串。
      默认给"值"是有意的：一张经营表可能有
      2,344 个公式，而累计 / 环比 / 同比全靠跨表链（形如 `='8月'!I7+E7`）
只给公式串等于让模型一个派生指标都读不到。
      大区域会跳过公式标记（那需要第二遍加载）—— 回执里会写 `notes`。
    * `merge_mode` —— `"raw"`（默认）/ `"fill"`：
      把合并区锚点的值铺开到成员格，并标注 `merged_from: "A45"`。
      一张表 266 个合并区可能制造 1253 格"看起来是空的"（纵向合并的分组标签
      只在左上角那一格）—— 不铺开，模型看不出那些行属于哪个组。
    * `columns` —— 只留这几列（列字母，如 `["B","E","F"]`）。
      跨表读时基本是必需品：9 张表 × 全宽很容易顶穿 6000 字符那道闸。
      用字母不用列名 —— 表里可能出现 `花  鲢`（带空格）、
      `黄桑鱼` vs 备注里的 `黄颡鱼`，名字对不齐，只有位置是可靠的。
    * `sheet_name` —— `"*"` = 全部表，`[..]` = 点名那几张。
      跨表时回 `{"blocks": [{"sheet_name": …, "rows": …}, …]}`。

    ## 两条硬约束（别照直觉改）

    * 流式与合并/校验互斥：`read_only=True` 的 sheet 没有 `merged_cells`、
      也没有 `data_validations`，要 `merge_mode="fill"` 或 `include_validation=True`
      时必须全载（`compact=True` 与它们同时用时以全载为准）。
    * 不给 `end_cell` 时有行数上限（`READ_SAFETY_ROWS`）：没有它，
      10 万行的表会先建 200 万个 dict —— OOM 发生在 6000 字符那道闸之前
      （那道闸管输出，管不了我们自己的内存）。撞上限会在 `notes` 里出声。
    """
    from . import _excel_io as _IO

    path = _resolve_xlsx_path(file_path, "read")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    if formula_mode not in _IO.FORMULA_MODES:
        return {"success": False,
                "error": "formula_mode 只能是 %s，给的是 '%s'"
                         % (list(_IO.FORMULA_MODES), formula_mode)}
    if merge_mode not in ("raw", "fill"):
        return {"success": False,
                "error": "merge_mode 只能是 raw / fill，给的是 '%s'" % merge_mode}
    multi = (sheet_name == "*") or isinstance(sheet_name, (list, tuple))

    # .xls → xlrd：它没有流式、也分不出缓存值/公式，新参数一律明确拒绝
    if path.suffix.lower() == ".xls":
        blocked = (compact or bool(columns) or merge_mode != "raw" or multi
                   or formula_mode == "formula")
        if blocked:
            return {"success": False,
                    "error": ".xls（Excel 97-2003 老格式）不支持这些参数：xlrd 会把整本"
                             "读进内存，既没有流式、也分不出缓存值/公式。",
                    "hint": "用 Excel 另存为 .xlsx 再读（另存为 -> Excel 工作簿）。"}
        return _read_excel_data_xlrd(path, sheet_name, start_cell, end_cell)

    # 区域与上限
    try:
        sr, sc, er, ec = _parse_cell_range(start_cell, end_cell)
    except ValueError as e:
        return {"success": False, "error": f"单元格引用无效: {e}"}

    capped = end_cell is None
    if capped:
        er = sr + _IO.READ_SAFETY_ROWS - 1
    # **给了 end_cell 时不许再用 max_rows / offset 去动 er** ——
    #   `max_rows` 是"这次回几行"（分页），`er` 是"这块区域到哪为止"，两件事。
    #   混成一件会让 offset 与它互相抵消：`offset=4, max_rows=4` 第二页直接回空。
    #   `offset`/`max_rows` 一律交给 `read_block` 的 `offset`/`limit` 处理。
    rows_span = max(0, er - sr + 1)
    cols_span = max(0, ec - sc + 1)

    # 公式标记要第二遍加载 —— 大区域上那是"第二遍全量读"，跳过并出声。
    want_marks = (formula_mode == "auto"
                  and rows_span * cols_span <= _IO.FORMULA_SCAN_CELL_CAP)
    marks_skipped = (formula_mode == "auto" and not want_marks)

    # 流式只在不碰合并区、不读校验时可用（read_only 的 sheet 没有那两样）
    stream = (not include_validation) and merge_mode == "raw"

    wb_vals = wb_fml = None
    try:
        if formula_mode == "formula":
            wb_vals = load_workbook(path, read_only=stream, data_only=False)
            wb_fml, marks = None, False
        else:
            wb_vals = load_workbook(path, read_only=stream, data_only=True)
            if want_marks:
                wb_fml = load_workbook(path, read_only=stream, data_only=False)
                marks = True
            else:
                wb_fml, marks = None, False

        names, err = _IO.pick_sheet_names(list(wb_vals.sheetnames), sheet_name)
        if err:
            return {"success": False, "error": err}

        if include_validation:
            try:
                from .cell_validation import get_data_validation_for_cell
            except ImportError as e:
                return {"success": False,
                        "error": f"include_validation=True 需要 cell_validation 模块，"
                                 f"但导入失败: {type(e).__name__}: {e}"}

        notes = []
        if marks_skipped:
            notes.append("区域过大（%d 格），**没有**标记哪些格是公式（那要第二遍读）；"
                         "本次回的是缓存值，没缓存值的格是 null。"
                         % (rows_span * cols_span))
        # 只在真撞上限时才出声（超时/截断真的发生了才报）。
        #   给每一张普通表都加一句"上限 5000 行"是噪声 —— 噪声会让人
        #   （和模型）学会忽略这一栏，而它真要说话时就没人听了。
        hit_ceiling = False

        blocks = []
        for sn in names:
            ws_v = wb_vals[sn]
            # 非流式下必须先把上界夹到 `ws.max_row`：
            #   `read_only=False` 的 `iter_rows(max_row=5000)` 在一张 61 行的表上
            #   会造出 5000 行，而且把 `ws.max_row` 改写成 5000（openpyxl
            #   取格子会撑大 dimension）。结果不只是浪费 —— 真数据会被埋在 4939 行
            #   空行后面，而那看起来像"表就是空的"。
            #   流式那条路不用夹：read_only 的 iter_rows 停在表尾（61 行）。
            er_sheet = er
            if not stream:
                try:
                    er_sheet = min(er, int(ws_v.max_row))
                except Exception:                       # noqa: BLE001
                    er_sheet = er

            # 不给 `end_cell` 时，列也要跟着走。
            #
            # 若只把行撑大而列原地不动：`read_excel_data(f, "一季度", compact=True)`
            # 会得到 `range="A1:A5000"`、`columns=["A"]` —— 只读了 A 列。
            # 根因是 `_parse_cell_range(start, None)` 的语义是 "end == start"
            # （`er = ec = 1`）。「不传区域」的真实行为会变成
            # 「A 列一直往下读」，而它看起来完全像"读了整张表"
            # —— 于是模型会拿一列的数据去下整张表的结论。
            #判据：不给 `end_cell` = 这张表的整块（列取到这表实际用到的最右列）。
            #   行那半仍受 `READ_SAFETY_ROWS` 保护（它的理由是 OOM，见函数 docstring），
            #   所以"整块"是"整块 ∩ 安全上限"，而真边界由 `extent` 说清。
            ec_sheet = ec
            if end_cell is None:
                try:
                    ec_sheet = max(ec, int(ws_v.max_column or 0))
                except Exception:                       # noqa: BLE001
                    ec_sheet = ec

            # 真边界（used range）—— **必须在 `read_block`
            #   之前取。`iter_rows(max_row=…)` 会撑大** `ws.max_row`（本函数上面
            #   那段注释记着：61 行的表给 `max_row=5000` 之后 `max_row` 就变成 5000），
            #   取晚了读到的就是我们自己改写过的值 —— "读不到 ≠ 0，
            #   读到的也可能不是事实"。
            #
            #   它答的是模型唯一没法自己算出来的那个问题："这张表到底多大"
            #   （`actual_rows` / `actual_cols` 是可见截断，不是业务边界）。
            try:
                true_max_row = int(ws_v.max_row or 0)
                true_max_col = int(ws_v.max_column or 0)
            except Exception:                           # noqa: BLE001
                true_max_row = true_max_col = 0
            # 取真起点。`actual_range` 若写死从表格左上角起，
            #   一张数据从 C3 开始的表会被报成 `A1:…`（读数不等于事实）。
            #   取不到就落回 1。
            try:
                _min_r = int(getattr(ws_v, "min_row", 1) or 1)
                _min_c = int(getattr(ws_v, "min_column", 1) or 1)
            except Exception:                           # noqa: BLE001
                _min_r = _min_c = 1

            anchors = _IO.merge_anchors(ws_v) if merge_mode == "fill" else None
            rows, cols_kept = _IO.read_block(
                ws_v, (wb_fml[sn] if wb_fml is not None else None),
                min_row=sr, max_row=er_sheet, min_col=sc, max_col=ec_sheet,
                keep_cols=columns, merge_mode=merge_mode, anchors=anchors,
                offset=offset, limit=max_rows, formula_marks=marks)
            if capped and len(rows) >= (er_sheet - sr + 1):
                hit_ceiling = True

            if include_validation:
                ws = wb_vals[sn]
                for row in rows:
                    for cell in row:
                        try:
                            vi = get_data_validation_for_cell(ws, cell["address"])
                            cell["validation"] = vi if vi else {"has_validation": False}
                        except Exception as e:      # noqa: BLE001
                            # 读不到 != 没有：塌成 False 就是说谎
                            cell["validation"] = {
                                "has_validation": None,
                                "error": f"{type(e).__name__}: {e}"}

            # `range` 报拿到的那一块，不是"请求的矩形"。
            #   流式那条路上 `er_sheet` 不夹（`read_block` 自己停在表尾），
            #   于是不给 `end_cell` 时会报成 `A1:F5000` —— 而实际只回了 18 行。
            #   那个 5000 会被读成"这表有 5000 行"（读数不等于事实）。
            _rng_er = min(er_sheet, true_max_row) if true_max_row else er_sheet
            rng = "%s%d:%s%d" % (get_column_letter(sc), sr,
                                 get_column_letter(ec_sheet), _rng_er)

            # "这是哪一块"与"这张表多大"必须同时给（结构化，不只散文）。
            #   依据 = `actual_rows/actual_cols` + `has_more`/`shown_end_row`
            #   + 「可见截断、不是业务边界」。
            #   散文那一半仍在 `_outside_range_note`（不新增第二个渲染源）——
            #   这里给的是机器可判的那一份。
            # 返回形状与契约对齐。
            row_cut = bool(true_max_row > er_sheet)
            col_cut = bool(true_max_col > ec_sheet)
            extent = {
                "actual_range": ("%s%d:%s%d" % (get_column_letter(_min_c), _min_r,
                                                get_column_letter(true_max_col),
                                                true_max_row)
                                 if (true_max_row and true_max_col) else ""),
                # 真·表边界的数字形态（判据：端点只是
                #   可见截断，不是业务边界）。
                "actual_end_row": true_max_row or 0,
                "actual_end_col": true_max_col or 0,
                "shown_rows": len(rows),
                "has_more": bool(row_cut or col_cut),
                # `truncated` 与 `has_more` 是两个独立开关：
                #   `has_more` = 你要的那个矩形之外还有数据；
                #   `truncated` = 工具自己替你截了（行数安全上限），与你给的窗口无关。
                #   塌成一个，分不清"表就这么大"与"我们没给全"。
                "truncated": bool(hit_ceiling),
            }
            if row_cut:
                # 下一刀的确切入口（同族的 `has_more_warning` 也直接给这个）。
                # 列要取本次的起始列 —— 若写死 `A`，一次从 C 列起的读
                #   会把下一次引到 A 列去（读错地方，而且看不出来）。
                extent["next_start_cell"] = "%s%d" % (get_column_letter(sc), er_sheet + 1)
            if col_cut:
                # 列被切时也要给下一刀 —— 只给行的话，列被切时一个字都不说。
                #   语义：把 `end_cell` 放到这一列，就能把剩下的列一起读回来。
                extent["next_end_cell"] = get_column_letter(true_max_col)

            # 记下"这次读没读全"，好让写侧出声。
            #   要防的形状是「读了半块 → 拿它下结论 → 写进交付物」，而写侧
            #   对此一无所知。判据窄到只有 `has_more`（"你要的矩形之外还有
            #   数据"）—— 不拿 `_outside_range_note` 的"命中过"当判据，理由见
            #   `core/partial_read.py` 的 docstring（那一句几乎每次都命中）。
            if extent["has_more"]:
                from core.partial_read import note as _note_partial
                from core.request_snapshot import current_session_id
                _note_partial(current_session_id(),
                              "`%s` 的 `%s` 只读到 `%s`，而它实际到第 %s 行 / %s 列"
                              % (path.name, sn, rng, true_max_row or "?",
                                 get_column_letter(true_max_col) if true_max_col else "?"))

            if compact:
                # 每行带回行号：行号内联进 `rows` 的首格、
                #   `columns` 首项回一个「行」标签。
                #   依据：读文件时逐条带位置是通用做法；`compact` 若只回
                #   裸值，"读串行"在结构上就不可避免。
                #   行号取表内真行号（`c["row"]`），不是 `enumerate` 的下标 ——
                #   分页（`offset`）时下标每页从 0 起，拿它会每页都从 1 开始数。
                blocks.append({
                    "sheet_name": sn, "range": rng,
                    "columns": ["行"] + list(cols_kept),
                    "rows": [[(row[0]["row"] if row else None)]
                             + [c["value"] for c in row] for row in rows],
                    "returned_rows": len(rows), **extent,
                })
            else:
                blocks.append({
                    "sheet_name": sn, "range": rng,
                    "cells": [c for row in rows for c in row], **extent,
                })

            # 范围之外还有什么 —— 每一个读了区域的地方都要说。
            #   模型读 A5:H12 而漏掉 I–L 时，必须有人告诉它。
            # 必须在这个循环里逐表问 —— 因为每张表的
            #   "实际读到的列"可能不同（不给 `end_cell` 时取各自 `max_column`），
            #   在外面那一趟只会拿同一个 `ec` 去问每一张表，问出来的边界是错的。
            #   （同一件事不许有两个渲染源 —— 现在是一处、每表一次。）
            _n = _outside_range_note(ws_v, sr, sc, er_sheet, ec_sheet)
            if _n and _n not in notes:
                notes.append(("[%s] " % sn if multi else "") + _n)

        if hit_ceiling:
            notes.append("没给 end_cell ⇒ 读到第 %d 行就停了（安全上限 %d 行）；"
                         "**这张表可能更长** —— 要后面的行请给 end_cell，"
                         "或改用 compact + offset 分页。"
                         % (er, _IO.READ_SAFETY_ROWS))

        if multi:
            out = {"success": True, "sheets_read": len(names), "blocks": blocks}
        else:
            out = {"success": True, **blocks[0]}
        if notes:
            out["notes"] = notes
        return out
    except Exception as e:
        return {"success": False,
                "error": f"读取 Excel 数据失败: {type(e).__name__}: {e}"}
    finally:
        for wb in (wb_vals, wb_fml):
            if wb is not None:
                try:
                    wb.close()
                except Exception:
                    pass

def write_excel_data(
    file_path: str,
    sheet_name: str,
    data: List[List],
    start_cell: str = "A1",
    allow_formula: bool = False,
) -> dict:
    """Write data to an Excel worksheet.

    Args:
        file_path: Path to .xlsx file (created if missing).
        sheet_name: Target sheet name.
        data: List of rows, each row is a list of cell values.
        start_cell: Top-left cell to start writing.
        allow_formula: 以 `=` 开头的字符串按什么写。

    ## `=` 开头的值（`allow_formula`）

    默认（`False`）按字面文本写 —— 值一个字不变，只是不变成公式。
    理由：这个入口是"模型给数据、整块盖上去"，而模型手里的 `data`
    可能是上一轮的读数、也可能是从不可信文档 / 网页里抄来的内容。
    openpyxl 对 `=...` 会存成公式（`t="f"`），Excel 打开时求值 ——
    一行从外部内容里抄来的 `=cmd|' /C calc'!A0` 就是一次外部程序调用。

    要写真公式有两条正路：`apply_formula`（显式工具，过语法与禁形检查），
    或这里传 `allow_formula=true`。两条都走 `_formula_deny_reason`。
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not data:
        return {"success": False, "error": "没有提供要写入的数据"}

    # 本工具是模型给数据、整块盖上去那一类（逐格覆盖
    #   `start_cell` 起的一块区域，不关心那块区域里原来有什么）—— 模型手里的
    #   `data` 可能是上一轮的读数。覆盖闸挂在这里。
    _blk = overwrite_blocked(path)
    if _blk:
        return {"success": False, **_blk}

    try:
        if path.exists():
            wb = load_workbook(path)
        else:
            wb = Workbook()
            wb.active.title = "Sheet1"

        if sheet_name not in wb.sheetnames:
            wb.create_sheet(sheet_name)
        ws = wb[sheet_name]

        sr, sc, _, _ = _parse_cell_range(start_cell)
        _as_text = 0                      # 有几个 `=` 开头的值被按文本写了
        for i, row in enumerate(data):
            for j, val in enumerate(row):
                cell = ws.cell(row=sr + i, column=sc + j, value=val)
                if not (isinstance(val, str) and val.startswith("=") and len(val) > 1):
                    continue
                if allow_formula:
                    denied = _formula_deny_reason(val)
                    if denied:
                        wb.close()
                        return {"success": False,
                                "error": f"第 {i + 1} 行第 {j + 1} 列的公式命中了"
                                         f"不支持的形状：{denied}。"}
                else:
                    # 默认按字面文本写：赋完再改 `data_type`。
                    # openpyxl 的 `value` setter 已经把 `=` 开头判成 'f'（公式），
                    # 手动改回 's' 之后写盘就是字符串，值一个字都不变
                    #（读回来 `data_type='s'`、`value` 原样）。
                    cell.data_type = "s"
                    _as_text += 1

        # Set reasonable column widths (accounting for Chinese/CJK chars)
        ncols = max(len(r) for r in data) if data else 1
        for col_idx in range(sc, sc + ncols):
            cl = get_column_letter(col_idx)
            ws.column_dimensions[cl].width = 14  # wide enough for CJK + numbers

        path.parent.mkdir(parents=True, exist_ok=True)
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        _res = {"success": True,
                "message": f"已写入 {len(data)} 行数据到 '{sheet_name}'。"
                           "文件已保存，用户需要时可调用 deliver_file 发送文件。",
                "rows_written": len(data), "start_cell": start_cell}
        # 出声 —— 有值被按文本写了，模型必须知道（否则它会以为公式生效了）。
        # 判据：「做了什么」与「没做什么」都得说。
        if _as_text:
            _res["formula_as_text"] = _as_text
            _res["note"] = (
                f"有 {_as_text} 个以 = 开头的值按文本写入（它们没有变成公式）。"
                f"要写真公式：用 apply_formula，或传 allow_formula=true。")
        # 写侧出声 —— 本次会话若有过"没读全"的
        #   Excel 读，就在这里说清。与 `create_document` 走同一个函数
        #   （`core.partial_read.warning_now`），措辞不许抄第二份。
        from core.partial_read import warning_now as _pw_now
        _pr = _pw_now()
        if _pr:
            _res["warning"] = _pr
        return _res
    except Exception as e:
        return {"success": False, "error": f"写入 Excel 数据失败: {type(e).__name__}: {e}"}


# Formulas

#: 公式里确定性禁止的形状 —— 命中就拒，不解析公式。
#:
#: 判据与 `core/command_policy.py` 的 deny 档同族：判得出来的才拒，
#: 判不出来的交给后面的路。写一个公式解析器不在选项里 —— 解析器的
#: 漏判比不解析更危险。
#:
#: 只认两种「合法公式里不该出现」的形状：
#:   · DDE：`=cmd|' /C calc'!A0` —— Excel 会把它求值成一次外部程序调用。
#:     特征是一个裸 `|`（合法公式里它只出现在字符串字面量内，极罕见）。
#:   · 外部工作簿引用：`=[Book1.xlsx]Sheet1!A1` —— 拉一个不在表里的文件。
#:     特征是被 `[]` 包着、且带表格文件扩展名的那一段。
#:     结构化引用（`SUM(Table1[Sales])`）不命中 —— 方括号里没有扩展名。
_FORMULA_DENY = (
    ("DDE（动态数据交换）", re.compile(r"\|")),
    ("外部工作簿引用", re.compile(r"\[[^\]]*\.(?:xlsx|xlsm|xlsb|xls|csv)\s*\]", re.I)),
)


def _formula_deny_reason(formula: str) -> str:
    """公式命中确定性禁形就返回那个人话名字，没命中返回空串。"""
    body = str(formula or "").lstrip("=")
    for name, pat in _FORMULA_DENY:
        if pat.search(body):
            return name
    return ""


def apply_formula(
    file_path: str,
    sheet_name: str,
    cell: str,
    formula: str,
) -> dict:
    """Apply an Excel formula to a cell after validation."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        # Validate first
        v = validate_formula_syntax(file_path, sheet_name, cell, formula)
        if not v.get("success"):
            return v

        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]

        # `_parse_cell_range` 返回四元组 `(sr, sc, er, ec)`——
        # 这里原先写的是 `row, col = ...`，必然 `ValueError: too many values
        # to unpack`，被下面那个 `except Exception` 收成
        # "应用公式失败: ValueError: …"，所以这个工具从来没真正工作过。
        # 现有测试只断言"跨区时它失败"，而"因为解包崩了"同样满足那个断言
        # —— 只钉失败、不钉原因，测试就钉不住东西。
        row, col, _, _ = _parse_cell_range(cell)
        target = ws.cell(row=row, column=col)
        target.value = f"={formula.lstrip('=')}" if not formula.startswith("=") else formula
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"公式已应用到 {cell}", "cell": cell, "formula": formula}
    except Exception as e:
        return {"success": False, "error": f"应用公式失败: {type(e).__name__}: {e}"}


def validate_formula_syntax(
    file_path: str,
    sheet_name: str,
    cell: str,
    formula: str,
) -> dict:
    """验证公式的语法形状 —— 不执行、不应用。

    它不是安全边界。这里只做四件事：非空 · 拒花括号 · 括号配对 ·
    拒两种确定性禁形（DDE / 外部工作簿引用）。它不解析公式，所以判不了
    「这个公式会不会拉取远程内容 / 会不会调起外部程序」的全部形状 ——
    那要写解析器，而解析器的漏判比不解析更危险。

    真正的围栏是谁被允许写公式：`write_excel_data` 默认把 `=` 开头
    按文本写，公式只能从 `apply_formula` 或显式 `allow_formula=true` 进来。
    """
    path = _resolve_xlsx_path(file_path, "read")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            wb.close()
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}

        _parse_cell_range(cell)  # validate cell ref

        # Basic formula sanity checks
        clean = formula.lstrip("=").strip()
        if not clean:
            wb.close()
            return {"success": False, "error": "公式为空"}
        if "{" in clean or "}" in clean:
            wb.close()
            return {"success": False, "error": "不支持数组公式（花括号）"}

        # Check parenthesis balance
        if clean.count("(") != clean.count(")"):
            wb.close()
            return {"success": False, "error": "括号不匹配"}

        # 确定性禁形 —— 上面三道都是语法检查，挡不住
        #   `=cmd|' /C calc'!A0` 这种"语法完全合法、语义是外部程序调用"的东西。
        #   这一条不解析公式，只认两种形状（见 `_FORMULA_DENY`）。
        denied = _formula_deny_reason(formula)
        if denied:
            wb.close()
            return {"success": False,
                    "error": f"不支持的公式形状：{denied}。"
                             f"这里只写单元格内计算的公式，不写会拉起外部文件或程序的。"}

        wb.close()
        return {"success": True, "message": f"公式语法验证通过: {formula}"}
    except ValueError as e:
        return {"success": False, "error": f"单元格引用无效: {e}"}
    except Exception as e:
        return {"success": False, "error": f"验证公式失败: {type(e).__name__}: {e}"}


# Formatting

def format_excel_range(
    file_path: str,
    sheet_name: str,
    start_cell: str,
    end_cell: Optional[str] = None,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    font_size: Optional[int] = None,
    font_color: Optional[str] = None,
    bg_color: Optional[str] = None,
    border_style: Optional[str] = None,
    border_color: Optional[str] = None,
    number_format: Optional[str] = None,
    alignment: Optional[str] = None,
    wrap_text: bool = False,
    merge_cells: bool = False,
    conditional_format: Optional[Dict[str, Any]] = None,
    column_width: Optional[float] = None,
    row_height: Optional[float] = None,
    auto_width: bool = False,
) -> dict:
    """Apply formatting to a range of cells.

    Supports font styling, background fill, borders, number format,
    text alignment, cell merging, conditional formatting, **and column widths /
    row heights** (the Excel counterpart of Word's
    `set_table_column_width`; without it a wide table is unreadable).

    * `column_width` / `row_height` -- applied to **every column / row the range
      touches**. Setting a width is a column-level property: a range spanning
      `B:D` sets widths for B, C and D.
    * `auto_width` -- estimate the width from the longest content in each
      column (CJK counts as ~2 units, since Excel column widths are in
      "characters of the default font", and a Chinese glyph is about twice as
      wide). Explicit `column_width` wins if both are given.
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]

        sr, sc, er, ec = _parse_cell_range(start_cell, end_cell)

        # Build font
        font_kwargs: dict = {"bold": bold, "italic": italic,
                              "underline": "single" if underline else None}
        if font_size is not None:
            font_kwargs["size"] = font_size
        if font_color is not None:
            fc = font_color if font_color.startswith("FF") else f"FF{font_color}"
            font_kwargs["color"] = Color(rgb=fc)
        font = Font(**{k: v for k, v in font_kwargs.items() if v is not None or k in ("bold", "italic")})

        # Build fill
        fill = None
        if bg_color is not None:
            bc = bg_color if bg_color.startswith("FF") else f"FF{bg_color}"
            fill = PatternFill(start_color=Color(rgb=bc), end_color=Color(rgb=bc), fill_type="solid")

        # Build border
        border = None
        if border_style is not None:
            bc2 = (border_color or "000000")
            bc2 = bc2 if bc2.startswith("FF") else f"FF{bc2}"
            side = Side(style=border_style, color=Color(rgb=bc2))
            border = Border(left=side, right=side, top=side, bottom=side)

        # Build alignment
        align = None
        if alignment is not None or wrap_text:
            align = Alignment(horizontal=alignment, vertical="center", wrap_text=wrap_text)

        # Apply to range
        for row in range(sr, er + 1):
            for col in range(sc, ec + 1):
                cell = ws.cell(row=row, column=col)
                cell.font = font
                if fill:
                    cell.fill = fill
                if border:
                    cell.border = border
                if align:
                    cell.alignment = align
                if number_format:
                    cell.number_format = number_format

        # 列宽 / 行高
        # 宽度是列级属性：区域跨 B:D 就是给 B、C、D 三列都设一遍。
        # `auto_width` 按最长内容估：Excel 的列宽单位是"默认字体的字符数"，
        #   而一个汉字≈两个字符宽，CJK 记 2、其余记 1。估出来的宽度**夹在
        #   6..60 之间**（太窄挤成一团、太宽一列占满屏，都没用）。
        if auto_width and column_width is None:
            widths = {}
            for row in range(sr, er + 1):
                for col in range(sc, ec + 1):
                    v = ws.cell(row=row, column=col).value
                    if v is None:
                        continue
                    n = _display_width(str(v))
                    widths[col] = max(widths.get(col, 0), n)
            for col, n in widths.items():
                ws.column_dimensions[get_column_letter(col)].width = min(60, max(6, n + 2))

        if column_width is not None:
            if column_width <= 0:
                wb.close()
                return {"success": False, "error": "column_width 必须 > 0"}
            for col in range(sc, ec + 1):
                ws.column_dimensions[get_column_letter(col)].width = float(column_width)

        if row_height is not None:
            if row_height <= 0:
                wb.close()
                return {"success": False, "error": "row_height 必须 > 0"}
            for row in range(sr, er + 1):
                ws.row_dimensions[row].height = float(row_height)

        # Merge if requested
        if merge_cells and end_cell:
            ws.merge_cells(f"{start_cell}:{end_cell}")

        # Conditional formatting
        if conditional_format:
            rule_type = conditional_format.get("type")
            params = conditional_format.get("params", {})
            range_str = f"{start_cell}:{end_cell}" if end_cell else start_cell

            rule_map = {
                "color_scale": ColorScaleRule,
                "data_bar": DataBarRule,
                "icon_set": IconSetRule,
                "formula": FormulaRule,
                "cell_is": CellIsRule,
            }
            RuleClass = rule_map.get(rule_type)
            if RuleClass:
                if rule_type == "cell_is" and "fill" in params:
                    fc3 = params["fill"].get("fgColor", "FFC7CE")
                    fc3 = fc3 if fc3.startswith("FF") else f"FF{fc3}"
                    params["fill"] = PatternFill(
                        start_color=Color(rgb=fc3),
                        end_color=Color(rgb=fc3), fill_type="solid")
                # 这条是间接公式入口：`params` 是模型给的自由 dict，
                # `FormulaRule(**params)` 的 `formula` 字段可以是任意公式。
                # 与 `set_excel_conditional_format` 走同一个确定性禁形。
                if rule_type == "formula":
                    _cf_formula = params.get("formula") or []
                    _cf_first = (_cf_formula[0]
                                 if isinstance(_cf_formula, (list, tuple)) and _cf_formula
                                 else _cf_formula)
                    _cf_denied = _formula_deny_reason(str(_cf_first or ""))
                    if _cf_denied:
                        wb.close()
                        return {"success": False,
                                "error": f"不支持的公式形状：{_cf_denied}。"}
                rule = RuleClass(**params)
                ws.conditional_formatting.add(range_str, rule)

        # Re-fit columns: number formats need more width than raw values
        for col_idx in range(sc, ec + 1):
            cl = get_column_letter(col_idx)
            existing = ws.column_dimensions[cl].width or 8
            if number_format and ('￥' in number_format or '$' in number_format or '#' in number_format):
                # Currency/number format — need at least 15 chars
                ws.column_dimensions[cl].width = max(existing, 15)
            else:
                ws.column_dimensions[cl].width = max(existing, 10)

        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        rng = f"{start_cell}:{end_cell}" if end_cell else start_cell
        return {"success": True, "message": f"格式化已应用到 {rng}", "range": rng}
    except Exception as e:
        return {"success": False, "error": f"格式化失败: {type(e).__name__}: {e}"}


# Charts

#: 图表类型表 —— 覆盖 `openpyxl.chart` 里全部 11 个 `*Chart` 类。
#:
#: 只有 5 个键时，组合图（簇状柱形 + 堆积折线）
#: 与 股价图（`StockChart`，开盘-盘高-盘低-收盘）一个都做不了，
#: 而那不是"库做不到"，是这张表没写全。
#:
#: 判据是一个类名一个键，别名只加在真会用的那几个上（`projectedpie`
#: 同时收 `projected_pie`，因为模型两种写法都会打）。
#: `scatter` 与其他几种的取数方式不同（XY 各自一对），见下面的分支 ——
#: 加新类型时别只看这张表，`_build_chart` 里还有一条按类型的分派。
_CHART_TYPES = {
    "line": LineChart, "bar": BarChart, "pie": PieChart,
    "scatter": ScatterChart, "area": AreaChart,
    # 下面这几个是 `openpyxl.chart` 里本来就有的
    "doughnut": DoughnutChart, "radar": RadarChart, "bubble": BubbleChart,
    "surface": SurfaceChart,
    # 股价图 —— 「开盘-盘高-盘低-收盘」就是它。
    #   它吃4 条序列（少一条 openpyxl 会抛），所以 data_range 得给够列。
    "stock": StockChart,
    "projectedpie": ProjectedPieChart, "projected_pie": ProjectedPieChart,
}

#: `bar` / `line` / `area` 的分组方式（"堆积折线图"就是 `stacked`）。
_GROUPINGS = {"clustered", "standard", "stacked", "percentStacked"}

#: 图例位置（OOXML 的 `ST_LegendPos`）。"图例在顶部" = `t`。
_LEGEND_POSITIONS = {"r", "l", "t", "b", "tr"}


def _set_data_markers(chart) -> None:
    """折线/散点每条序列都带数据标记（"带数据标记的折线图"）。

    openpyxl 里这是每个 Series 的属性，不是图表级的 —— 所以得逐条设，
    而"逐条"意味着在 add_data 之后才能做（序列那时才存在）。
    """
    from openpyxl.chart.marker import Marker
    for s in chart.series or ():
        s.marker = Marker(symbol="circle", size=6)


def _build_chart(ChartClass: type, ct: str, ws, cell_range: str, *,
                 grouping: str = "", bar_dir: str = "", marker: bool = False,
                 title: str = "", x_axis: str = "", y_axis: str = ""):
    """按类型 + 子类型造一个图表对象。

    组合图的两条腿都走这里 —— 取数逻辑只有这一处（一个判定两处会漂）。
    组合图那条腿若自己再写一遍 `Reference`，早晚会漏掉 `scatter` 那个分支。
    """
    sc, ec = cell_range.split(":")
    sr, sc_col, er, ec_col = _parse_cell_range(sc, ec)

    chart = ChartClass()
    chart.title = title
    if hasattr(chart, "x_axis"):
        chart.x_axis.title = x_axis
    if hasattr(chart, "y_axis"):
        chart.y_axis.title = y_axis

    # 取数：三种形态（加新类型时先看这里，别只看 `_CHART_TYPES`）
    if ct == "bubble":
        # 气泡图：前 3 列 = X 值 / Y 值 / 大小
        xv = Reference(ws, min_row=sr + 1, max_row=er, min_col=sc_col)
        yv = Reference(ws, min_row=sr + 1, max_row=er, min_col=sc_col + 1)
        zv = Reference(ws, min_row=sr + 1, max_row=er, min_col=sc_col + 2)
        chart.series.append(Series(yv, xv, zv, title_from_data=True))
    elif ct == "scatter":
        for col in range(sc_col + 1, ec_col + 1):
            xv = Reference(ws, min_row=sr + 1, max_row=er, min_col=sc_col)
            yv = Reference(ws, min_row=sr + 1, max_row=er, min_col=col)
            chart.series.append(Series(yv, xv, title_from_data=True))
    elif ct in ("pie", "doughnut", "projectedpie", "projected_pie"):
        data_ref = Reference(ws, min_row=sr, max_row=er,
                             min_col=sc_col, max_col=ec_col)
        cats_ref = Reference(ws, min_row=sr + 1, max_row=er, min_col=sc_col)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
    else:
        # 通用形态：首列当类别，其余列各一条序列。
        #   `stock`（股价图）走的就是这条 —— 日期 + 开/高/低/收 4 列，
        #   少一条序列 openpyxl 会抛（它要 4 条），所以 data_range 得给够。
        data_ref = Reference(ws, min_row=sr, max_row=er,
                             min_col=sc_col + 1, max_col=ec_col)
        cats_ref = Reference(ws, min_row=sr + 1, max_row=er, min_col=sc_col)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)

    # 子类型
    if grouping:
        if grouping not in _GROUPINGS:
            raise ValueError(f"grouping 只认 {sorted(_GROUPINGS)}，收到 {grouping!r}")
        if hasattr(chart, "grouping"):
            chart.grouping = grouping
    if bar_dir:
        if bar_dir not in ("col", "bar"):
            raise ValueError(f"bar_dir 只认 'col'（柱）/ 'bar'（条），收到 {bar_dir!r}")
        if hasattr(chart, "type"):
            chart.type = bar_dir
    if marker:
        _set_data_markers(chart)
    return chart


def create_excel_chart(
    file_path: str,
    sheet_name: str,
    data_range: str,
    chart_type: str,
    target_cell: str,
    title: str = "",
    x_axis: str = "",
    y_axis: str = "",
    show_data_labels: bool = True,
    show_legend: bool = True,
    # ── 下面六个参数是 `openpyxl` 本来就有、而这张 schema 没有接的口。
    #    全部可选，旧调用一个字都不用改。
    grouping: str = "",
    bar_dir: str = "",
    marker: bool = False,
    style: int = 0,
    legend_position: str = "",
    gridlines: bool = True,
    combo: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """Create a chart in an Excel worksheet.

    `chart_type` 取 `_CHART_TYPES` 的键（line/bar/pie/scatter/area/doughnut/
    radar/bubble/surface/stock/projectedpie）。

    组合图：`combo=[{"chart_type":"line","data_range":"C13",
    "grouping":"stacked","marker":true,"y_axis":"市盈率"}]` —— 主图 + N 条腿
    叠在同一张图上（`openpyxl` 的 `chart1 += chart2`）。
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    ct = chart_type.lower()
    ChartClass = _CHART_TYPES.get(ct)
    if ChartClass is None:
        return {"success": False,
                "error": f"不支持的图表类型 '{chart_type}'。支持: {', '.join(_CHART_TYPES)}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]

        # Init drawing containers
        if not hasattr(ws, "_drawings"):
            ws._drawings = []
        if not hasattr(ws, "_charts"):
            ws._charts = []

        # Parse data range
        if "!" in data_range:
            _, cell_range = data_range.split("!")
        else:
            cell_range = data_range

        try:
            chart = _build_chart(ChartClass, ct, ws, cell_range,
                                 grouping=grouping, bar_dir=bar_dir, marker=marker,
                                 title=title, x_axis=x_axis, y_axis=y_axis)
        except ValueError as ve:
            # 子类型参数写错不是"创建图表失败"（那句话会把"你写错了"和
            #    "库抛了"压成同一句）。单独说清。
            return {"success": False, "error": str(ve)}

        # 组合图：主图 += 每条腿
        # 「收盘价簇状柱形 + 市盈率堆积折线」就是它。
        # 每条腿自己带 data_range（组合图的腿不可能和主图同一块数据）。
        for i, leg in enumerate(combo or ()):
            if not isinstance(leg, dict):
                return {"success": False, "error": f"combo[{i}] 要是对象，收到 {type(leg).__name__}"}
            leg_ct = str(leg.get("chart_type") or "").lower()
            LegClass = _CHART_TYPES.get(leg_ct)
            if LegClass is None:
                return {"success": False,
                        "error": f"combo[{i}] 的 chart_type {leg_ct!r} 不支持；"
                                 f"可选 {', '.join(sorted(_CHART_TYPES))}"}
            leg_range = str(leg.get("data_range") or "")
            if "!" in leg_range:
                leg_range = leg_range.split("!")[1]
            if not leg_range:
                return {"success": False, "error": f"combo[{i}] 缺 data_range"}
            try:
                leg_chart = _build_chart(
                    LegClass, leg_ct, ws, leg_range,
                    grouping=str(leg.get("grouping") or ""),
                    bar_dir=str(leg.get("bar_dir") or ""),
                    marker=bool(leg.get("marker")),
                    title="", x_axis="", y_axis=str(leg.get("y_axis") or ""))
            except ValueError as ve:
                return {"success": False, "error": f"combo[{i}]: {ve}"}
            leg_chart.legend = None          # 整张图共用一个图例
            chart += leg_chart               # openpyxl 的组合图就是这一行

        # Legend
        if show_legend:
            chart.legend = Legend()
            chart.legend.position = legend_position or "r"
        else:
            chart.legend = None

        # Data labels
        if show_data_labels:
            dl = DataLabelList()
            dl.showVal = True
            chart.dataLabels = dl

        # 预设样式（1..48）· 网格线开关
        if style:
            if not (1 <= int(style) <= 48):
                return {"success": False, "error": f"style 要 1~48，收到 {style}"}
            chart.style = int(style)
        if not gridlines and hasattr(chart, "y_axis"):
            # "删除主要网格线" = 把这个轴上的网格线对象拿掉
            chart.y_axis.majorGridlines = None

        chart.width = 15
        chart.height = 7.5

        # Anchor
        tc_col = column_index_from_string("".join(c for c in target_cell if c.isalpha())) - 1
        tc_row = int("".join(c for c in target_cell if c.isdigit())) - 1
        drawing = SpreadsheetDrawing()
        drawing.chart = chart
        anchor = OneCellAnchor()
        anchor._from = AnchorMarker(col=tc_col, row=tc_row)
        drawing.anchor = anchor
        ws._drawings.append(drawing)
        ws._charts.append(chart)

        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True,
                "message": f"{chart_type.capitalize()} 图表已创建于 {target_cell}",
                "chart_type": chart_type, "location": target_cell}
    except Exception as e:
        return {"success": False, "error": f"创建图表失败: {type(e).__name__}: {e}"}


# Pivot table

def create_excel_pivot(
    file_path: str,
    sheet_name: str,
    data_range: str,
    rows: List[str],
    values: List[str],
    columns: Optional[List[str]] = None,
    agg_func: str = "sum",
) -> dict:
    """Create a pivot-table-like summary on a new sheet.

    Reads source data, groups by *rows* fields, aggregates *values*
    fields using *agg_func* (sum/average/count/min/max).
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    valid_agg = {"sum", "average", "count", "min", "max"}
    if agg_func.lower() not in valid_agg:
        return {"success": False, "error": f"无效的聚合函数 '{agg_func}'。支持: {', '.join(sorted(valid_agg))}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}

        sc, ec = data_range.split(":")
        sr, sc_col, er, ec_col = _parse_cell_range(sc, ec)

        # Read source data
        ws = wb[sheet_name]
        src_data = []
        for row in range(sr, er + 1):
            row_vals = [ws.cell(row=row, column=c).value for c in range(sc_col, ec_col + 1)]
            src_data.append(row_vals)

        if len(src_data) < 2:
            return {"success": False, "error": "源数据至少需要标题行和一行数据"}

        headers = [str(h) for h in src_data[0]]
        records = [dict(zip(headers, row)) for row in src_data[1:]]

        # Validate fields
        avail = {h.lower() for h in headers}
        for f in rows + values + (columns or []):
            if str(f).lower() not in avail:
                return {"success": False, "error": f"字段 '{f}' 不存在。可用字段: {', '.join(headers)}"}

        # Create pivot sheet
        pivot_name = f"{sheet_name}_pivot"
        if pivot_name in wb.sheetnames:
            wb.remove(wb[pivot_name])
        pws = wb.create_sheet(pivot_name)

        # Header row
        c = 1
        for f in rows:
            pws.cell(row=1, column=c, value=f).font = Font(bold=True)
            c += 1
        for f in values:
            pws.cell(row=1, column=c, value=f"{f} ({agg_func})").font = Font(bold=True)
            c += 1

        # Group and aggregate
        groups: dict = {}
        for rec in records:
            key = tuple(str(rec.get(f, "")) for f in rows)
            if key not in groups:
                groups[key] = []
            groups[key].append(rec)

        r = 2
        for key, group_recs in sorted(groups.items()):
            c = 1
            for val in key:
                pws.cell(row=r, column=c, value=val)
                c += 1
            for vf in values:
                nums = [rec[vf] for rec in group_recs
                        if isinstance(rec.get(vf), (int, float))]
                if agg_func == "sum":
                    result = sum(nums)
                elif agg_func == "average":
                    result = sum(nums) / len(nums) if nums else 0
                elif agg_func == "count":
                    result = len(nums)
                elif agg_func == "min":
                    result = min(nums) if nums else 0
                elif agg_func == "max":
                    result = max(nums) if nums else 0
                else:
                    result = sum(nums)
                pws.cell(row=r, column=c, value=result)
                c += 1
            r += 1

        # Add table style
        if r > 2:
            tbl = Table(displayName=f"Pivot_{uuid.uuid4().hex[:8]}",
                        ref=f"A1:{get_column_letter(c - 1)}{r - 1}")
            tbl.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium9", showRowStripes=True)
            pws.add_table(tbl)

        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True,
                "message": f"透视表已创建于工作表 '{pivot_name}'",
                "pivot_sheet": pivot_name,
                "rows_grouped": len(groups),
                "aggregation": agg_func}
    except Exception as e:
        return {"success": False, "error": f"创建透视表失败: {type(e).__name__}: {e}"}


# Excel table (native)

def create_excel_table(
    file_path: str,
    sheet_name: str,
    data_range: str,
    table_name: Optional[str] = None,
    table_style: str = "TableStyleMedium9",
) -> dict:
    """Create a native Excel table from a data range with styling."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]

        tn = table_name or f"Table_{uuid.uuid4().hex[:8]}"
        tbl = Table(displayName=tn, ref=data_range)
        style = TableStyleInfo(name=table_style, showRowStripes=True)
        tbl.tableStyleInfo = style
        ws.add_table(tbl)

        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"表格 '{tn}' 已创建", "table_name": tn, "range": data_range}
    except Exception as e:
        return {"success": False, "error": f"创建表格失败: {type(e).__name__}: {e}"}


# Sheet management

def copy_worksheet(file_path: str, source_sheet: str, target_sheet: str) -> dict:
    """Copy a worksheet within the same workbook."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if source_sheet not in wb.sheetnames:
            return {"success": False, "error": f"源工作表 '{source_sheet}' 不存在"}
        if target_sheet in wb.sheetnames:
            return {"success": False, "error": f"目标工作表 '{target_sheet}' 已存在"}
        source = wb[source_sheet]
        target = wb.copy_worksheet(source)
        target.title = target_sheet
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"'{source_sheet}' 已复制为 '{target_sheet}'"}
    except Exception as e:
        return {"success": False, "error": f"复制工作表失败: {type(e).__name__}: {e}"}


def delete_worksheet(file_path: str, sheet_name: str) -> dict:
    """Delete a worksheet from the workbook."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        if len(wb.sheetnames) == 1:
            return {"success": False, "error": "不能删除工作簿中唯一的工作表"}
        del wb[sheet_name]
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"工作表 '{sheet_name}' 已删除"}
    except Exception as e:
        return {"success": False, "error": f"删除工作表失败: {type(e).__name__}: {e}"}


def rename_worksheet(file_path: str, old_name: str, new_name: str) -> dict:
    """Rename a worksheet and fix chart references in the ZIP archive."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if old_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{old_name}' 不存在"}
        if new_name in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{new_name}' 已存在"}
        wb[old_name].title = new_name
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}

        # Patch chart XML directly in the ZIP — most reliable method.
        # openpyxl serializes chart references with sheet names that
        # become stale after rename.
        import zipfile, io, re, shutil
        tmp_path = path.with_suffix(".xlsx.tmp")
        with zipfile.ZipFile(path, 'r') as zin, \
             zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if 'charts/' in item.filename and item.filename.endswith('.xml'):
                    text = data.decode('utf-8')
                    # Replace all forms of sheet references in chart XML
                    text = text.replace(f"'{old_name}'!", f"'{new_name}'!")  # 'Sheet1'!
                    text = text.replace(f">{old_name}!", f">'{new_name}'!")  # >Sheet1!
                    text = text.replace(f" {old_name}!", f" '{new_name}'!")  #  Sheet1!
                    data = text.encode('utf-8')
                zout.writestr(item, data)
        shutil.move(str(tmp_path), str(path))

        return {"success": True, "message": f"'{old_name}' 已重命名为 '{new_name}'"}
    except Exception as e:
        return {"success": False, "error": f"重命名失败: {type(e).__name__}: {e}"}
    except Exception as e:
        return {"success": False, "error": f"重命名工作表失败: {type(e).__name__}: {e}"}


# Merge / Unmerge

def merge_excel_cells(file_path: str, sheet_name: str, start_cell: str, end_cell: str) -> dict:
    """Merge a range of cells."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        ws.merge_cells(f"{start_cell}:{end_cell}")
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"已合并 {start_cell}:{end_cell}"}
    except Exception as e:
        return {"success": False, "error": f"合并单元格失败: {type(e).__name__}: {e}"}


def unmerge_excel_cells(file_path: str, sheet_name: str, start_cell: str, end_cell: str) -> dict:
    """Unmerge a range of cells."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        ws.unmerge_cells(f"{start_cell}:{end_cell}")
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"已取消合并 {start_cell}:{end_cell}"}
    except Exception as e:
        return {"success": False, "error": f"取消合并失败: {type(e).__name__}: {e}"}


def get_merged_cells(file_path: str, sheet_name: str) -> dict:
    """Get all merged cell ranges in a worksheet."""
    path = _resolve_xlsx_path(file_path, "read")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        merged = [str(m) for m in ws.merged_cells.ranges]
        wb.close()
        return {"success": True, "sheet_name": sheet_name, "merged_ranges": merged}
    except Exception as e:
        return {"success": False, "error": f"获取合并单元格失败: {type(e).__name__}: {e}"}


# Range copy / delete

def copy_excel_range(
    file_path: str,
    sheet_name: str,
    source_start: str,
    source_end: str,
    target_start: str,
    target_sheet: Optional[str] = None,
) -> dict:
    """Copy a range of cells to another location (same or different sheet)."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ts = target_sheet or sheet_name
        if ts not in wb.sheetnames:
            return {"success": False, "error": f"目标工作表 '{ts}' 不存在"}

        src_ws = wb[sheet_name]
        dst_ws = wb[ts]
        sr, sc, er, ec = _parse_cell_range(source_start, source_end)
        tr, tc, _, _ = _parse_cell_range(target_start)
        row_off, col_off = tr - sr, tc - sc

        for i in range(sr, er + 1):
            for j in range(sc, ec + 1):
                src = src_ws.cell(row=i, column=j)
                dst = dst_ws.cell(row=i + row_off, column=j + col_off)
                dst.value = src.value
                if src.has_style:
                    dst._style = copy(src._style)

        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"已复制 {source_start}:{source_end} → {target_start}"}
    except Exception as e:
        return {"success": False, "error": f"复制区域失败: {type(e).__name__}: {e}"}


def delete_excel_range(
    file_path: str,
    sheet_name: str,
    start_cell: str,
    end_cell: str,
    shift_direction: str = "up",
) -> dict:
    """Delete a range of cells and shift remaining cells."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if shift_direction not in ("up", "left"):
        return {"success": False, "error": "shift_direction 只能是 'up' 或 'left'"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        sr, sc, er, ec = _parse_cell_range(start_cell, end_cell)

        # Shift
        if shift_direction == "up":
            ws.delete_rows(sr, er - sr + 1)
        else:
            ws.delete_cols(sc, ec - sc + 1)

        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"已删除 {start_cell}:{end_cell}（{shift_direction} 方向移动）"}
    except Exception as e:
        return {"success": False, "error": f"删除区域失败: {type(e).__name__}: {e}"}


# Row / Column insert / delete

def insert_excel_rows(
    file_path: str,
    sheet_name: str,
    start_row: int,
    count: int = 1,
) -> dict:
    """Insert one or more rows starting at the specified row."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if start_row < 1:
        return {"success": False, "error": "起始行号必须 >= 1"}
    if count < 1:
        return {"success": False, "error": "行数必须 >= 1"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        ws.insert_rows(start_row, count)
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"已在第 {start_row} 行插入 {count} 行"}
    except Exception as e:
        return {"success": False, "error": f"插入行失败: {type(e).__name__}: {e}"}


def insert_excel_columns(
    file_path: str,
    sheet_name: str,
    start_col: int,
    count: int = 1,
) -> dict:
    """Insert one or more columns starting at the specified column."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if start_col < 1:
        return {"success": False, "error": "起始列号必须 >= 1"}
    if count < 1:
        return {"success": False, "error": "列数必须 >= 1"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        ws.insert_cols(start_col, count)
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"已在第 {start_col} 列插入 {count} 列"}
    except Exception as e:
        return {"success": False, "error": f"插入列失败: {type(e).__name__}: {e}"}


def delete_excel_rows(
    file_path: str,
    sheet_name: str,
    start_row: int,
    count: int = 1,
) -> dict:
    """Delete one or more rows starting at the specified row."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if start_row < 1:
        return {"success": False, "error": "起始行号必须 >= 1"}
    if count < 1:
        return {"success": False, "error": "行数必须 >= 1"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        if start_row > ws.max_row:
            return {"success": False, "error": f"起始行 {start_row} 超出工作表范围"}
        ws.delete_rows(start_row, count)
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"已从第 {start_row} 行删除 {count} 行"}
    except Exception as e:
        return {"success": False, "error": f"删除行失败: {type(e).__name__}: {e}"}


def delete_excel_columns(
    file_path: str,
    sheet_name: str,
    start_col: int,
    count: int = 1,
) -> dict:
    """Delete one or more columns starting at the specified column."""
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if start_col < 1:
        return {"success": False, "error": "起始列号必须 >= 1"}
    if count < 1:
        return {"success": False, "error": "列数必须 >= 1"}

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        if start_col > ws.max_column:
            return {"success": False, "error": f"起始列 {start_col} 超出工作表范围"}
        ws.delete_cols(start_col, count)
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": f"已从第 {start_col} 列删除 {count} 列"}
    except Exception as e:
        return {"success": False, "error": f"删除列失败: {type(e).__name__}: {e}"}


# 冻结窗格 / 条件格式 / 数据验证
#
# 它们是做表的收尾动作。缺了这三类，模型只能去写
#   openpyxl 脚本走 `run_command`（多一次审批、多一轮往返）。

def freeze_excel_panes(
    file_path: str,
    sheet_name: str,
    rows: int = 1,
    cols: int = 0,
) -> dict:
    """冻结前 `rows` 行 / 前 `cols` 列（滚动时它们不动）。

    `rows=1, cols=0`（默认）= 冻结首行（表头）。两个都给则同时冻结左上角区域。
    `rows=0, cols=0` = 取消冻结。
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    rows, cols = max(0, int(rows or 0)), max(0, int(cols or 0))

    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]
        if rows == 0 and cols == 0:
            ws.freeze_panes = None
            msg = "已取消冻结"
        else:
            # openpyxl 的约定：freeze_panes = 未冻结区域的左上角单元格
            #冻 1 行 1 列，"B2"。
            ws.freeze_panes = f"{get_column_letter(cols + 1)}{rows + 1}"
            msg = f"已冻结前 {rows} 行 / 前 {cols} 列"
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True, "message": msg,
                "freeze_panes": ws.freeze_panes}
    except Exception as e:
        return {"success": False, "error": f"冻结窗格失败: {type(e).__name__}: {e}"}


#: 条件格式支持的三类规则 → 需要哪些参数。写在模块级是刻意的：
#: 校验与建规则读同一份，不写两处会漂移的东西。
_CF_RULES = ("cellIs", "formula", "colorScale")

#: `cellIs` 允许的运算符（openpyxl 的 `CellIsRule` 直接收这些字符串）。
_CF_OPERATORS = (
    "greaterThan", "lessThan", "greaterThanOrEqual", "lessThanOrEqual",
    "equal", "notEqual", "between", "notBetween",
)


def set_excel_conditional_format(
    file_path: str,
    sheet_name: str,
    cell_range: str,
    rule_type: str = "cellIs",
    operator: str = "greaterThan",
    formula: Optional[List[str]] = None,
    fill_color: str = "FFC7CE",
    font_color: str = "",
) -> dict:
    """给单元格区域加条件格式（数据条/色阶之外最常见的那三类）。

    - `rule_type="cellIs"` —— 按数值比较。`operator` 取 `_CF_OPERATORS` 之一，
      `formula` 给 1 个值（`between`/`notBetween` 要给 2 个）。
    - `rule_type="formula"` —— 任意公式，`formula[0]` 形如 `$A1>100`。
    - `rule_type="colorScale"` —— 色阶（`formula` 给 2~3 个边界值，可留空用默认）。

    颜色是 6 位或 8 位十六进制（不带 `#`），例如 `"FFC7CE"` 或 `"FFFFC7CE"`。
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if rule_type not in _CF_RULES:
        return {"success": False,
                "error": f"rule_type 只能是 {_CF_RULES}，给的是 '{rule_type}'"}
    formula = [str(f) for f in (formula or [])]

    def _hex(c: str, default: str) -> str:
        v = str(c or "").strip().lstrip("#")
        if not v:
            return default
        if len(v) == 6:
            return "FF" + v.upper()
        if len(v) == 8:
            return v.upper()
        raise ValueError(f"颜色要 6 位或 8 位十六进制，给的是 '{c}'")

    try:
        fill = _hex(fill_color, "FFFFC7CE")
        font = _hex(font_color, "") if font_color else ""

        if rule_type == "cellIs":
            if operator not in _CF_OPERATORS:
                return {"success": False,
                        "error": f"operator 只能是 {_CF_OPERATORS}，给的是 '{operator}'"}
            need = 2 if operator in ("between", "notBetween") else 1
            if len(formula) < need:
                return {"success": False,
                        "error": f"operator='{operator}' 需要 {need} 个值，"
                                 f"formula 给了 {len(formula)} 个"}
            rule = CellIsRule(
                operator=operator, formula=formula[:need],
                fill=PatternFill(start_color=fill, end_color=fill,
                                 fill_type="solid"),
                font=Font(color=font) if font else None,
            )
        elif rule_type == "formula":
            if not formula:
                return {"success": False, "error": "rule_type='formula' 必须给 formula[0]"}
            # 条件格式的公式同样是公式入口 —— 同一个确定性禁形。
            denied = _formula_deny_reason(formula[0])
            if denied:
                return {"success": False,
                        "error": f"不支持的公式形状：{denied}。"}
            rule = FormulaRule(
                formula=formula[:1],
                fill=PatternFill(start_color=fill, end_color=fill,
                                 fill_type="solid"),
            )
        else:   # colorScale
            rule = ColorScaleRule(
                start_type="min", start_color="FFF8696B",
                mid_type="percentile", mid_value=50, mid_color="FFFFEB84",
                end_type="max", end_color="FF63BE7B",
            )

        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        wb[sheet_name].conditional_formatting.add(cell_range, rule)
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True,
                "message": f"已给 {sheet_name}!{cell_range} 加上 {rule_type} 条件格式"}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"条件格式失败: {type(e).__name__}: {e}"}


#: 数据验证允许的类型（与 openpyxl 的 `DataValidation.type` 同名）。
_DV_TYPES = ("list", "whole", "decimal", "date", "time", "textLength", "custom")


def add_excel_data_validation(
    file_path: str,
    sheet_name: str,
    cell_range: str,
    validation_type: str = "list",
    formula1: str = "",
    formula2: str = "",
    allow_blank: bool = True,
    error_message: str = "",
) -> dict:
    """给单元格区域加数据验证（下拉列表 / 数值范围 / 文本长度上限…）。

    - `validation_type="list"` 时 `formula1` 是逗号分隔的选项
      （如 `"男,女"`）或一个区域（如 `"=$E$1:$E$5"`）。
    - 其余类型 `formula1` / `formula2` 是边界值（`between` 那类要给两个）。
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if validation_type not in _DV_TYPES:
        return {"success": False,
                "error": f"validation_type 只能是 {_DV_TYPES}，给的是 '{validation_type}'"}
    if not str(formula1).strip():
        return {"success": False, "error": "formula1 不能为空"}

    try:
        from openpyxl.worksheet.datavalidation import DataValidation

        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]

        f1 = str(formula1)
        # list 类型：openpyxl 要求字面选项带引号（`"男,女"`）。
        # 模型经常直接给 `男,女`，这里替它补上，别让它猜格式。
        if validation_type == "list" and not f1.startswith(("=", '"')):
            f1 = f'"{f1}"'

        dv = DataValidation(
            type=validation_type,
            formula1=f1,
            formula2=str(formula2) if str(formula2).strip() else None,
            allow_blank=bool(allow_blank),
            showErrorMessage=True,
            error=error_message or "输入值不符合该单元格的验证规则",
        )
        ws.add_data_validation(dv)
        dv.add(cell_range)
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True,
                "message": f"已给 {sheet_name}!{cell_range} 加上数据验证（{validation_type}）"}
    except Exception as e:
        return {"success": False, "error": f"数据验证失败: {type(e).__name__}: {e}"}


# 打印设置 / 分级显示
#
# 这两件属于"库本来就有、接口没接"：`openpyxl` 本来就有它们
#    （`page_setup` / `print_title_rows` / `oddHeader` / `oddFooter` /
#     `row_dimensions[..].outlineLevel`），而这里一条工具都没接。
#与图表类型补齐是同一件事：不是做不到，是没接。

#: 常规纸型（OOXML 的 `ST_PaperSize` 里最常用的几个）。
#: 不枚举全套 —— 枚举会烂（同"分类，不枚举"那条）：要别的就写数字，
#: 数字是 OOXML 的原生口径。
_PAPER_SIZES = {"a3": 8, "a4": 9, "a5": 11, "b4": 12, "b5": 13,
                "letter": 1, "legal": 5, "tabloid": 3}


def set_excel_print_setup(
    file_path: str,
    sheet_name: str,
    orientation: str = "",
    paper_size: str = "",
    print_title_rows: str = "",
    print_title_cols: str = "",
    header_left: str = "",
    header_center: str = "",
    header_right: str = "",
    footer_left: str = "",
    footer_center: str = "",
    footer_right: str = "",
    print_area: str = "",
    fit_to_width: int = 0,
) -> dict:
    """工作表打印设置 —— 纸张 / 方向 / 打印标题行 / 页眉页脚 / 打印区域。

    页码域照 OOXML 的原生写法：`第 &[Page] 页，共 &[Pages] 页`
    （`&[Page]` / `&[Pages]` / `&D` 日期 / `&F` 文件名 —— 由打开它的那个软件渲染）。
    我们不替你算页数 —— 那是排版引擎的事，`openpyxl` 里没有那个概念。

    `print_title_rows="1:1"` = 每页都打印第 1 行（表头）；`"1:2"` 是前两行。
    "每页都打印表标题和列名称"这类要求就是它 + `print_title_cols`。
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]

        done = []
        if orientation:
            o = orientation.strip().lower()
            if o not in ("portrait", "landscape"):
                return {"success": False,
                        "error": f"orientation 只认 portrait / landscape，收到 {orientation!r}"}
            ws.page_setup.orientation = o
            done.append("横向" if o == "landscape" else "纵向")
        if paper_size:
            key = str(paper_size).strip().lower()
            num = _PAPER_SIZES.get(key)
            if num is None:
                try:
                    num = int(key)
                except ValueError:
                    return {"success": False,
                            "error": f"paper_size 认 {sorted(_PAPER_SIZES)} 或 OOXML 数字，"
                                     f"收到 {paper_size!r}"}
            ws.page_setup.paperSize = num
            done.append("纸型 %s" % paper_size)
        if print_title_rows:
            ws.print_title_rows = print_title_rows
            done.append("每页打印行 %s" % print_title_rows)
        if print_title_cols:
            ws.print_title_cols = print_title_cols
            done.append("每页打印列 %s" % print_title_cols)
        if print_area:
            ws.print_area = print_area
            done.append("打印区域 %s" % print_area)
        if fit_to_width:
            ws.page_setup.fitToWidth = int(fit_to_width)
            ws.sheet_properties.pageSetUpPr.fitToPage = True
            done.append("按 %d 页宽缩放" % int(fit_to_width))

        # 页眉页脚三栏各自可空。
        # 必须落到 `.text` 上：`HeaderFooterItem.left/center/right` 期望的是
        #    `_HeaderFooterPart` 对象，直接赋字符串会 `TypeError`
        #    （报的是 `HeaderFooterItem.right should be ...` —— 一句话里
        #     没说"该给 .text"，所以这里把坑写下来）。
        # 三栏本来就都在（`HeaderFooterItem.__init__` 会把没给的建成空的）
        #不需要"没有就建一个"那一支 —— 建了反而把"没有页眉"变成空白页眉。
        for obj_name, part, val in (("oddHeader", "left", header_left),
                                    ("oddHeader", "center", header_center),
                                    ("oddHeader", "right", header_right),
                                    ("oddFooter", "left", footer_left),
                                    ("oddFooter", "center", footer_center),
                                    ("oddFooter", "right", footer_right)):
            if not val:
                continue
            item = getattr(ws, obj_name, None)
            if item is None:
                return {"success": False,
                        "error": f"这个工作簿没有 {obj_name}（openpyxl 版本差异）"}
            getattr(item, part).text = val
            done.append("%s.%s" % (obj_name, part))

        if not done:
            return {"success": False,
                    "error": "一个参数都没给 —— 打印设置要改什么就说什么"}
        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True,
                "message": "已设置 %s 的打印选项：%s" % (sheet_name, "；".join(done))}
    except Exception as e:
        return {"success": False, "error": f"打印设置失败: {type(e).__name__}: {e}"}


def set_excel_outline(
    file_path: str,
    sheet_name: str,
    rows: Optional[List[Dict[str, Any]]] = None,
    cols: Optional[List[Dict[str, Any]]] = None,
    summary_below: bool = True,
    summary_right: bool = False,
) -> dict:
    """给工作表设分级显示（`outlineLevel`）—— 分类汇总/折叠那套。

    `rows=[{"start": 2, "end": 10, "level": 1}]`（单行写 `{"row": 5, "level": 1}`）；
    `cols` 同形（`col` / `start_col` / `end_col`）。

    它只设"哪一级" —— 不替你把明细行变成汇总行。Excel 的"分类汇总"
    是引擎算的（那一半要走 LibreOffice UNO）；
    这一件管的是"级别标对了、按级别能展开折叠"那一半。
    `level=0` = 取消这一行的分组（不是"第 0 级"）。
    级别要一层层加（外层 1、内层 2）—— 跳级 Excel 会打不开。
    """
    path = _resolve_xlsx_path(file_path, "write")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if not rows and not cols:
        return {"success": False, "error": "rows / cols 至少给一个"}
    try:
        wb = load_workbook(path)
        if sheet_name not in wb.sheetnames:
            return {"success": False, "error": f"工作表 '{sheet_name}' 不存在"}
        ws = wb[sheet_name]

        def _spans(items, single_keys, start_keys, end_keys):
            out = []
            for i, it in enumerate(items or ()):
                if not isinstance(it, dict):
                    raise ValueError("第 %d 项要是对象" % i)
                lvl = int(it.get("level", 1))
                if lvl < 0 or lvl > 7:
                    raise ValueError("第 %d 项的 level 要 0~7（0 = 取消分组），收到 %r"
                                     % (i, it.get("level")))
                one = next((it[k] for k in single_keys if k in it), None)
                if one is not None:
                    out.append((int(one), int(one), lvl))
                    continue
                a = next((it[k] for k in start_keys if k in it), None)
                b = next((it[k] for k in end_keys if k in it), None)
                if a is None or b is None:
                    raise ValueError("第 %d 项要写 %s 之一，或 %s + %s"
                                     % (i, "/".join(single_keys), start_keys[0], end_keys[0]))
                out.append((int(a), int(b), lvl))
            return out

        try:
            row_spans = _spans(rows, ("row",), ("start", "start_row"), ("end", "end_row"))
            col_spans = _spans(cols, ("col", "column"), ("start_col", "start"),
                               ("end_col", "end"))
        except ValueError as ve:
            return {"success": False, "error": str(ve)}

        touched = 0
        for a, b, lvl in row_spans:
            for r in range(min(a, b), max(a, b) + 1):
                ws.row_dimensions[r].outlineLevel = lvl
                touched += 1
        for a, b, lvl in col_spans:
            for c in range(min(a, b), max(a, b) + 1):
                ws.column_dimensions[get_column_letter(c)].outlineLevel = lvl
                touched += 1

        ws.sheet_properties.outlinePr.summaryBelow = bool(summary_below)
        ws.sheet_properties.outlinePr.summaryRight = bool(summary_right)

        _save_err = save_workbook(wb, path)
        if _save_err:
            return {"success": False, "error": f"保存失败: {_save_err}"}
        return {"success": True,
                "message": ("已给 %s 设了分级显示：%d 个行段 / %d 个列段，共 %d 格；"
                            "汇总在%s"
                            % (sheet_name, len(row_spans), len(col_spans), touched,
                               "下方" if summary_below else "上方"))}
    except Exception as e:
        return {"success": False, "error": f"分级显示失败: {type(e).__name__}: {e}"}
