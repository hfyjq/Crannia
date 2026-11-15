"""Excel 的写侧：排序 / 筛选 / 去重 / 保护 / 超链接 / 导出 CSV / 合并 / 重算 / 画图。

## 九件，各答什么

| 工具 | 一句话 |
|---|---|
| `sort_excel_range` | 按列排序（多键、升降序） |
| `filter_excel_data` | 两模式：`extract` 抽到新表 / `auto_filter` 挂原生筛选器 |
| `remove_excel_duplicates` | 按列去重（不可逆 —— 丢的行回不来） |
| `protect_excel_sheet` | 加/解工作表保护 |
| `set_excel_hyperlink` | 单元格超链接 |
| `export_excel_to_csv` | 流式导出 CSV（UTF-8 BOM） |
| `merge_excel_workbooks` | 多簿合并（追加表 / 叠行） |
| `recalculate_excel_formulas` | LibreOffice 重算公式（不可逆 —— 往返会丢图表/条件格式） |
| `plot_excel_data` | 出图（折线/柱状/饼图/散点/面积），支持跨表 |

## `plot_excel_data` 是从哪来的

它要答的一个基准需求：「主要鱼类 1~9月 价格走势图」。
而仓库里只有 `create_excel_chart` —— 它只画单表内的一个区域，
而那个需求的数横跨 9 张表（每张月表一段）。新增这一件，两条读法：

* 单表：`cell_range` 是一块矩形 —— 首列 = X 标签，其余每列一条线，首行 = 线名
* 跨表（`sheet_name="*"`）：X 轴 = 工作表顺序，`cell_range` 里的每一行 = 一条线，
  线名取自 `label_column` 那一列（同一批行）。那张经营表：
  `plot_excel_data(f, sheet_name="*", cell_range="E54:E60", label_column="B")`
  = 7 条鱼 × 9 个月的价格走势图，一次调用。

中文字体是这条路上最容易静默失败的地方（默认字体画中文=豆腐块）。
本文件按候选表挑字体，挑不到会明确说明，而不是给一张满是方块的图。

## 一条贯穿的规矩

所有落盘都走 `_excel_io.save_workbook`（原子写 + 回读校验）；
所有路径都过 `guarded_resolve`（输入 `read`、输出 `write` —— 混合源的工具两处分别声明）。
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import load_workbook
from openpyxl.styles import PatternFill

from . import soffice as _soffice
from ._excel_io import guarded_resolve, overwrite_blocked, pick_sheet_names, save_workbook

#: 画图用到的 CJK 字体候选，顺序即优先级（Windows 在前，其次 mac/linux）。
CJK_FONT_CANDIDATES = (
    "Microsoft YaHei", "SimHei", "SimSun", "DengXian", "KaiTi",
    "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC",
    "Source Han Sans SC", "WenQuanYi Micro Hei", "Arial Unicode MS",
)

CHART_TYPES = ("line", "bar", "pie", "scatter", "area")

FILTER_MODES = ("extract", "auto_filter")
FILTER_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "contains", "not_contains")


def _err(msg: str, hint: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {"success": False, "error": msg}
    if hint:
        out["hint"] = hint
    return out


def _parse_range(cell_range: str):
    from .excel_tools import _parse_cell_range
    parts = str(cell_range or "").split(":")
    if not parts or not parts[0]:
        raise ValueError("cell_range 不能为空")
    return _parse_cell_range(parts[0], parts[1] if len(parts) > 1 else None)


def _rows_of(ws, sr, sc, er, ec) -> List[List[Any]]:
    return [list(r) for r in ws.iter_rows(min_row=sr, max_row=er,
                                          min_col=sc, max_col=ec, values_only=True)]


def _write_rows(ws, sr, sc, rows) -> None:
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            ws.cell(row=sr + i, column=sc + j, value=v)


# 排序 / 筛选 / 去重

def sort_excel_range(file_path: str, sheet_name: str, cell_range: str,
                     sort_keys: Optional[List[Dict[str, Any]]] = None,
                     has_header: bool = True,
                     key_column: Optional[str] = None,
                     descending: bool = False) -> dict:
    """按一列或多列排序一块区域（原地）。

    * `sort_keys` —— `[{"column": "B"|2, "order": "asc"|"desc"}, ...]`，多键按序比较
    * 兼容简写：只给 `key_column` + `descending`

    只动这块区域内的行，区域外的行一个字节不动（免得把表头/合计行卷进来）。
    """
    path = guarded_resolve(file_path, "write")
    if not path.exists():
        return _err(f"文件不存在: {file_path}")

    keys = []
    if sort_keys:
        for k in sort_keys:
            if not isinstance(k, dict) or "column" not in k:
                return _err("sort_keys 每一项都要形如 {'column': 'B', 'order': 'asc'}")
            order = str(k.get("order", "asc")).lower()
            if order not in ("asc", "desc"):
                return _err("order 只能是 asc / desc，给的是 '%s'" % order)
            keys.append((k["column"], order))
    elif key_column is not None:
        keys.append((key_column, "desc" if descending else "asc"))
    if not keys:
        return _err("要么给 sort_keys，要么给 key_column")

    try:
        sr, sc, er, ec = _parse_range(cell_range)
    except ValueError as e:
        return _err(f"cell_range 无效: {e}")

    try:
        wb = load_workbook(path)
    except Exception as e:                                      # noqa: BLE001
        return _err(f"打不开工作簿: {type(e).__name__}: {e}")
    try:
        if sheet_name not in wb.sheetnames:
            return _err(f"工作表 '{sheet_name}' 不存在")
        ws = wb[sheet_name]
        rows = _rows_of(ws, sr, sc, er, ec)

        idx = []
        for col, order in keys:
            if isinstance(col, int):
                j = col - sc
            else:
                j = _col_index(str(col)) - sc
            if j < 0 or j >= (len(rows[0]) if rows else 0):
                wb.close()
                return _err(f"排序列 {col!r} 不在区域 {cell_range} 里")
            idx.append((j, order == "desc"))

        head, body = (rows[0], rows[1:]) if (has_header and rows) else (None, rows)

        def _key(row):
            out = []
            for j, desc in idx:
                v = row[j] if j < len(row) else None
                out.append(_SortKey(v, desc))
            return out

        body = sorted(body, key=_key)
        _write_rows(ws, sr, sc, ([head] if head else []) + body)
        err = save_workbook(wb, path)
        if err:
            return _err(f"保存失败: {err}")
    except Exception as e:                                      # noqa: BLE001
        try:
            wb.close()
        except Exception:                                       # noqa: BLE001
            pass
        return _err(f"排序失败: {type(e).__name__}: {e}")

    return {"success": True,
            "message": f"已按 {len(keys)} 个键排序 {sheet_name}!{cell_range}"
                       f"（{len(body)} 行{'，含表头' if head else ''}）"}


class _SortKey:
    """让 None 永远排在最后，且数字与文字不比较大小（免得 TypeError）。"""
    __slots__ = ("v", "desc")

    def __init__(self, v, desc: bool):
        self.v = v
        self.desc = desc

    def __lt__(self, other):
        a, b = self.v, other.v
        if a is None and b is None:
            return False
        if a is None:
            return False            # None 永远靠后（不随 desc 翻转）
        if b is None:
            return True
        try:
            if a == b:
                return False        # 相等必须显式说"不小于"
            r = a < b
        except TypeError:
            s_a, s_b = str(a), str(b)
            if s_a == s_b:
                return False
            r = s_a < s_b
        return (not r) if self.desc else r


def _col_index(letter: str) -> int:
    from openpyxl.utils import column_index_from_string
    return column_index_from_string(str(letter).strip().upper())


def filter_excel_data(file_path: str, sheet_name: str, cell_range: str,
                      mode: str = "extract",
                      criteria: Optional[List[Dict[str, Any]]] = None,
                      output_sheet: str = "",
                      has_header: bool = True,
                      include_header: bool = True) -> dict:
    """筛选。两种模式用途完全不同：

    * `"extract"` —— 按 `criteria` 把命中的行抄到新表（原地一个字节不动）
    * `"auto_filter"` —— 在原区域挂原生自动筛选器（下拉箭头，不删任何行）

    `criteria` —— `[{"column": "C", "op": "gt", "value": 60}, ...]`，多个条件全部满足。
    `op` 取 `eq/ne/gt/ge/lt/le/contains/not_contains`。
    """
    if mode not in FILTER_MODES:
        return _err("mode 只能是 %s，给的是 '%s'" % (list(FILTER_MODES), mode))
    for c in criteria or []:
        if str(c.get("op", "")) not in FILTER_OPS:
            return _err("op 只能是 %s，给的是 '%s'" % (list(FILTER_OPS), c.get("op")))

    path = guarded_resolve(file_path, "write")
    if not path.exists():
        return _err(f"文件不存在: {file_path}")
    try:
        sr, sc, er, ec = _parse_range(cell_range)
    except ValueError as e:
        return _err(f"cell_range 无效: {e}")

    try:
        wb = load_workbook(path)
    except Exception as e:                                      # noqa: BLE001
        return _err(f"打不开工作簿: {type(e).__name__}: {e}")
    try:
        if sheet_name not in wb.sheetnames:
            return _err(f"工作表 '{sheet_name}' 不存在")
        ws = wb[sheet_name]

        if mode == "auto_filter":
            ws.auto_filter.ref = "%s%d:%s%d" % (
                _col_letter(sc), sr, _col_letter(ec), er)
            err = save_workbook(wb, path)
            if err:
                return _err(f"保存失败: {err}")
            return {"success": True,
                    "message": f"已给 {sheet_name}!{cell_range} 挂上自动筛选器"
                               f"（行数一个没动）"}

        if not criteria:
            wb.close()
            return _err("mode='extract' 必须给 criteria（否则要抄哪些行？）")

        rows = _rows_of(ws, sr, sc, er, ec)
        head, body = (rows[0], rows[1:]) if (has_header and rows) else ([], rows)
        jidx = []
        for c in criteria:
            j = (_col_index(str(c["column"])) - sc) if not isinstance(c["column"], int) \
                else (c["column"] - sc)
            jidx.append((j, str(c["op"]), c.get("value")))

        kept = [r for r in body if all(_match(r, j, op, v) for j, op, v in jidx)]

        out_name = output_sheet or (sheet_name + "_筛选")
        if out_name in wb.sheetnames:
            del wb[out_name]
        target = wb.create_sheet(out_name)
        blob = ([head] if (head and include_header) else []) + kept
        _write_rows(target, 1, 1, blob)
        err = save_workbook(wb, path)
        if err:
            return _err(f"保存失败: {err}")
        return {"success": True,
                "message": f"从 {sheet_name}!{cell_range} 里筛出 {len(kept)} 行"
                           f"（原表 {len(body)} 行），已写到新表 '{out_name}'",
                "output_sheet": out_name, "matched_rows": len(kept),
                "source_rows": len(body)}
    except Exception as e:                                      # noqa: BLE001
        try:
            wb.close()
        except Exception:                                       # noqa: BLE001
            pass
        return _err(f"筛选失败: {type(e).__name__}: {e}")


def _match(row, j, op, want) -> bool:
    v = row[j] if 0 <= j < len(row) else None
    if op in ("contains", "not_contains"):
        hit = (want is not None) and (str(want) in ("" if v is None else str(v)))
        return hit if op == "contains" else not hit
    if op in ("eq", "ne"):
        hit = _loose_eq(v, want)
        return hit if op == "eq" else not hit
    # 数值比较：比不了就判不中（fail-closed，宁可少筛不可错筛）
    try:
        a, b = float(v), float(want)
    except (TypeError, ValueError):
        return False
    return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]


def _loose_eq(v, want) -> bool:
    if v is None:
        return want in (None, "")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return float(v) == float(want)
        except (TypeError, ValueError):
            return False
    return str(v) == str(want)


def _col_letter(n: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(n)


def remove_excel_duplicates(file_path: str, sheet_name: str, cell_range: str,
                            key_columns: Optional[List[Any]] = None,
                            has_header: bool = True) -> dict:
    """按 `key_columns` 去重（保留第一次出现的那行），把结果写回原区域。

    ## 这一件声明 `destructive: true` —— 理由是诚实的

    丢掉的列回不来（没有"撤销"）。所以它在派发前会走兜底闸，
    弹一次审批浮窗 —— 在用户自己的真数据上，那是对的。

    `key_columns` —— 列字母或列号；省略 = 整行比较。
    """
    path = guarded_resolve(file_path, "write")
    if not path.exists():
        return _err(f"文件不存在: {file_path}")
    try:
        sr, sc, er, ec = _parse_range(cell_range)
    except ValueError as e:
        return _err(f"cell_range 无效: {e}")

    try:
        wb = load_workbook(path)
    except Exception as e:                                      # noqa: BLE001
        return _err(f"打不开工作簿: {type(e).__name__}: {e}")
    try:
        if sheet_name not in wb.sheetnames:
            return _err(f"工作表 '{sheet_name}' 不存在")
        ws = wb[sheet_name]
        rows = _rows_of(ws, sr, sc, er, ec)
        head, body = (rows[0], rows[1:]) if (has_header and rows) else (None, rows)

        if key_columns:
            cols = [(_col_index(str(c)) - sc) if not isinstance(c, int) else (c - sc)
                    for c in key_columns]
        else:
            cols = list(range(len(body[0]))) if body else []

        seen = set()
        kept = []
        for r in body:
            k = tuple(_hashable(r[j]) if 0 <= j < len(r) else None for j in cols)
            if k in seen:
                continue
            seen.add(k)
            kept.append(r)

        removed = len(body) - len(kept)
        # 清掉原区域的多余行（否则旧的尾部还留着）
        # **不能写 `ws.cell(row=r, column=c, value=None)`** —— openpyxl 的
        #   `Worksheet.cell()` 是 `if value is not None: cell.value = value`
        #传 None 是个空操作，清不掉任何东西。踩到过：
        #   去重后前 4 行对了，尾部两行原样留着（而回执说删了 2 行）。
        for r in range(sr, er + 1):
            for c in range(sc, ec + 1):
                ws.cell(row=r, column=c).value = None
        _write_rows(ws, sr, sc, ([head] if head else []) + kept)
        err = save_workbook(wb, path)
        if err:
            return _err(f"保存失败: {err}")
    except Exception as e:                                      # noqa: BLE001
        try:
            wb.close()
        except Exception:                                       # noqa: BLE001
            pass
        return _err(f"去重失败: {type(e).__name__}: {e}")

    return {"success": True,
            "message": f"已按 {'整行' if not key_columns else key_columns} 去重："
                       f"{len(body)} 行 -> {len(kept)} 行（删了 {removed} 行重复）",
            "removed_rows": removed, "kept_rows": len(kept)}


def _hashable(v):
    return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)


# 保护 / 超链接

def protect_excel_sheet(file_path: str, sheet_name: str,
                        protect: bool = True, password: str = "") -> dict:
    """给工作表加/解保护。

    密码只写不读 —— openpyxl 的"保护"是防误改，不是加密。
    解保护要 `protect=False` + 同一个密码。
    """
    path = guarded_resolve(file_path, "write")
    if not path.exists():
        return _err(f"文件不存在: {file_path}")
    try:
        wb = load_workbook(path)
    except Exception as e:                                      # noqa: BLE001
        return _err(f"打不开工作簿: {type(e).__name__}: {e}")
    try:
        if sheet_name not in wb.sheetnames:
            return _err(f"工作表 '{sheet_name}' 不存在")
        prot = wb[sheet_name].protection
        prot.sheet = bool(protect)
        if protect and password:
            prot.password = str(password)
        err = save_workbook(wb, path)
        if err:
            return _err(f"保存失败: {err}")
    except Exception as e:                                      # noqa: BLE001
        try:
            wb.close()
        except Exception:                                       # noqa: BLE001
            pass
        return _err(f"设置保护失败: {type(e).__name__}: {e}")

    return {"success": True,
            "message": ("已给 %s 加上保护" if protect else "已解除 %s 的保护") % sheet_name}


def set_excel_hyperlink(file_path: str, sheet_name: str, cell: str,
                        target: str, display: str = "") -> dict:
    """给某个单元格挂外部链接（网址 / 文件 / 邮件）。

    `display` 省略时显示 target 本身（而不是留空 —— 空格子点了才知道有链接）。
    """
    if not str(target or "").strip():
        return _err("target 不能为空")
    path = guarded_resolve(file_path, "write")
    if not path.exists():
        return _err(f"文件不存在: {file_path}")
    try:
        wb = load_workbook(path)
    except Exception as e:                                      # noqa: BLE001
        return _err(f"打不开工作簿: {type(e).__name__}: {e}")
    try:
        if sheet_name not in wb.sheetnames:
            return _err(f"工作表 '{sheet_name}' 不存在")
        ws = wb[sheet_name]
        ws[cell].hyperlink = str(target)
        if display:
            ws[cell].value = str(display)
        elif ws[cell].value in (None, ""):
            ws[cell].value = str(target)
        # 链接默认是蓝的、带下划线 —— 但不覆盖用户已经设过的样式
        if not ws[cell].font or not ws[cell].font.underline:
            from openpyxl.styles import Font
            ws[cell].font = Font(color="0563C1", underline="single")
        err = save_workbook(wb, path)
        if err:
            return _err(f"保存失败: {err}")
    except Exception as e:                                      # noqa: BLE001
        try:
            wb.close()
        except Exception:                                       # noqa: BLE001
            pass
        return _err(f"设置超链接失败: {type(e).__name__}: {e}")

    return {"success": True, "message": f"已给 {sheet_name}!{cell} 挂上链接: {target}"}


# 导出 CSV / 合并多簿

def export_excel_to_csv(file_path: str, sheet_name: str = "",
                        output_name: str = "", delimiter: str = ",",
                        encoding: str = "utf-8-sig") -> dict:
    """把一张工作表流式导出成 CSV。

    * 默认 `utf-8-sig`（带 BOM）—— 不带的话 Excel 打开中文会乱码，
      而"导出给 Excel 用"正是这个工具最常见的用途。
    * 只导第一张或点名的那张表；要全套请逐张调（或多簿合并另说）。
    """
    src = guarded_resolve(file_path, "read")
    if not src.exists():
        return _err(f"文件不存在: {file_path}")
    if len(str(delimiter)) != 1:
        return _err("delimiter 要正好一个字符")

    from ._excel_io import pick_sheet_names as _pick
    try:
        wb = load_workbook(src, read_only=True, data_only=True)
    except Exception as e:                                      # noqa: BLE001
        return _err(f"打不开工作簿: {type(e).__name__}: {e}")
    try:
        names, err = _pick(list(wb.sheetnames), sheet_name)
        if err:
            return _err(err)
        sn = names[0]
        out_name = output_name or (Path(file_path).stem + "_" + sn + ".csv")
        if not out_name.lower().endswith(".csv"):
            out_name += ".csv"
        out_path = guarded_resolve(out_name, "write")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(out_path, "w", newline="", encoding=encoding) as fh:
            w = csv.writer(fh, delimiter=delimiter)
            for row in wb[sn].iter_rows(values_only=True):
                w.writerow(["" if v is None else v for v in row])
                n += 1
        return {"success": True, "output_path": str(out_path).replace("\\", "/"),
                "sheet_name": sn, "rows_written": n, "encoding": encoding,
                "message": f"已把 {sn} 的 {n} 行导出到 {out_path.name}"
                           f"（{encoding}，用 Excel 打开不会乱码）"}
    except Exception as e:                                      # noqa: BLE001
        return _err(f"导出 CSV 失败: {type(e).__name__}: {e}")
    finally:
        try:
            wb.close()
        except Exception:                                       # noqa: BLE001
            pass


def merge_excel_workbooks(file_paths: Optional[List[str]] = None,
                          output_name: str = "merged.xlsx",
                          mode: str = "sheets",
                          has_header: bool = True) -> dict:
    """把多个工作簿合并成一个。

    * `mode="sheets"` —— 每个来源原样搬成一张表（表名重复就自动加后缀）
    * `mode="append_rows"` —— 同名表叠行（每张只取第一张表），第一份的表头保留
    """
    if mode not in ("sheets", "append_rows"):
        return _err("mode 只能是 sheets / append_rows，给的是 '%s'" % mode)
    srcs = [str(p) for p in (file_paths or [])]
    if len(srcs) < 1:
        return _err("file_paths 至少要给一个")
    if not output_name.lower().endswith(".xlsx"):
        output_name += ".xlsx"

    read_paths = [guarded_resolve(p, "read") for p in srcs]
    missing = [str(p) for p in read_paths if not p.exists()]
    if missing:
        return _err(f"这些文件不存在: {missing}")
    out_path = guarded_resolve(output_name, "write")

    # 本工具只校验源存在（上面那三句）、必须另外检查输出路径
    #否则输出名撞上另一个对话正在编辑的 xlsx，整本会被一个全新工作簿无声覆盖。
    _blk = overwrite_blocked(out_path)
    if _blk:
        return _err(_blk["error"], _blk.get("hint", ""))

    from openpyxl import Workbook
    try:
        if mode == "sheets":
            out = Workbook()
            out.remove(out.active)
            used = set()
            total = 0
            for p in read_paths:
                wb = load_workbook(p)
                for sn in wb.sheetnames:
                    name = sn
                    i = 2
                    while name in used:
                        name = "%s_%d" % (sn, i)
                        i += 1
                    used.add(name)
                    dst = out.create_sheet(name)
                    for row in wb[sn].iter_rows(values_only=True):
                        dst.append(list(row))
                        total += 1
                wb.close()
            if not out.sheetnames:
                return _err("来源里一张表都没有")
            err = save_workbook(out, out_path)
            if err:
                return _err(f"保存失败: {err}")
            return {"success": True, "output_path": str(out_path).replace("\\", "/"),
                    "sheets": list(out.sheetnames), "rows_written": total,
                    "message": f"已把 {len(read_paths)} 个工作簿搬成 {len(out.sheetnames)} 张表"}

        # append_rows
        out = Workbook()
        ws = out.active
        ws.title = "merged"
        first = True
        total = 0
        for p in read_paths:
            wb = load_workbook(p, read_only=True, data_only=True)
            sn = wb.sheetnames[0]
            for i, row in enumerate(wb[sn].iter_rows(values_only=True)):
                if i == 0 and has_header:
                    if not first:
                        continue          # 表头只留第一份
                ws.append(list(row))
                total += 1
            wb.close()
            first = False
        err = save_workbook(out, out_path)
        if err:
            return _err(f"保存失败: {err}")
        return {"success": True, "output_path": str(out_path).replace("\\", "/"),
                "rows_written": total,
                "message": f"已把 {len(read_paths)} 个字簿的第一张表叠成 {total} 行"}
    except Exception as e:                                      # noqa: BLE001
        return _err(f"合并失败: {type(e).__name__}: {e}")


# 公式重算（LibreOffice）

def recalculate_excel_formulas(file_path: str, sheet_name: str = "") -> dict:
    """用 LibreOffice 无头模式重算公式，把缓存值写回。

    ## 什么时候需要它

    `openpyxl` 只写公式不算值。所以我们自己写进去的公式，
    读回来永远是"没有缓存值"（`read_excel_data(formula_mode='cached')` 回 `null`）。
    真 Excel 存过的文件本来就有缓存值，不需要这一步。

    ## `destructive: true` —— 理由是诚实的

    LibreOffice 往返可能丢掉图表 / 条件格式 / 数据验证 / 透视表
    （它按自己的理解重新序列化整个文件）。派发前会走兜底闸弹一次浮窗。

    ## 做法（与 `convert_to_pdf` 同一套）

    临时目录里转一次 → 校验表数没变 → 原子替换回原路径。
    找不到 LibreOffice 时出声 + 带路线，不静默失败。
    """
    path = guarded_resolve(file_path, "write")
    if not path.exists():
        return _err(f"文件不存在: {file_path}")
    soffice = _soffice.find_soffice()
    if soffice is None:
        out = _soffice.missing_hint("重算公式")
        return out

    try:
        before = load_workbook(path, read_only=True)
        sheets_before = list(before.sheetnames)
        before.close()
    except Exception as e:                                      # noqa: BLE001
        return _err(f"打不开工作簿: {type(e).__name__}: {e}")

    with tempfile.TemporaryDirectory(prefix="lv_recalc_") as td:
        try:
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "xlsx",
                 "--outdir", td, str(path)],
                capture_output=True, timeout=_soffice.CONVERT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _err("LibreOffice 重算超时（%d 秒）—— 已放弃，原文件未动。"
                        % _soffice.CONVERT_TIMEOUT_S)
        except Exception as e:                                  # noqa: BLE001
            return _err(f"调用 LibreOffice 失败: {type(e).__name__}: {e}")

        produced = list(Path(td).glob("*.xlsx"))
        if proc.returncode != 0 or not produced:
            tail = (proc.stderr or b"")[-300:].decode("utf-8", "replace")
            return _err(f"LibreOffice 没能重算出结果（rc={proc.returncode}）: {tail}")

        try:
            chk = load_workbook(produced[0], read_only=True)
            sheets_after = list(chk.sheetnames)
            chk.close()
        except Exception as e:                                  # noqa: BLE001
            return _err(f"LibreOffice 产出的文件打不开: {type(e).__name__}: {e}")

        if sheets_after != sheets_before:
            return _err("★ 重算前后**工作表对不上**（%r -> %r）—— 拒绝替换原文件。"
                        % (sheets_before, sheets_after))

        # 数一下有多少公式格现在有缓存值了（给回执一个数，而不是一句"成功了"）
        filled = 0
        try:
            wv = load_workbook(produced[0], read_only=True, data_only=True)
            wf = load_workbook(produced[0], read_only=True, data_only=False)
            for sn in sheets_after:
                for vr, fr in zip(wv[sn].iter_rows(values_only=True),
                                  wf[sn].iter_rows(values_only=True)):
                    for a, b in zip(vr, fr):
                        if isinstance(b, str) and b.startswith("=") and a is not None:
                            filled += 1
            wv.close()
            wf.close()
        except Exception:                                       # noqa: BLE001
            filled = -1                                        # 读不到 != 0

        # 原子替换（`_excel_io` 里那条判据：同目录 + os.replace）
        tmp = path.with_name(path.name + ".lvtmp")
        shutil.copyfile(str(produced[0]), str(tmp))
        os.replace(str(tmp), str(path))

    out: Dict[str, Any] = {
        "success": True,
        "sheets": sheets_after,
        "message": "已用 LibreOffice 重算公式并写回（表数校验通过）",
        "warning": ("★ LibreOffice 往返**可能丢掉图表 / 条件格式 / 数据验证 / 透视表** "
                    "—— 它按自己的理解重新序列化整个文件。若这张表有那些东西，"
                    "请先备份再比对。"),
    }
    if filled >= 0:
        out["cells_with_cached_values"] = filled
    else:
        out["cells_with_cached_values"] = None
        out["note"] = "数不出重算后有多少格带缓存值（读不到 != 0，故回 null）"
    return out


# 画图（用户需求 3：主要鱼类 1~9月 价格走势图）

def _pick_cjk_font() -> str:
    """挑一个能画中文的字体。挑不到回空串（调用方要出声）。"""
    try:
        from matplotlib import font_manager as fm
    except Exception:                                           # noqa: BLE001
        return ""
    have = {f.name for f in fm.fontManager.ttflist}
    for c in CJK_FONT_CANDIDATES:
        if c in have:
            return c
    return ""


def plot_excel_data(file_path: str, sheet_name: str = "", cell_range: str = "",
                    chart_type: str = "line",
                    value_column: str = "", label_column: str = "",
                    header_row: int = 1, series_names: Optional[List[str]] = None,
                    title: str = "", x_label: str = "", y_label: str = "",
                    output_name: str = "") -> dict:
    """把表格数据画成图（PNG），存进工作区。

    ## 两种读法（`sheet_name` 决定）

    * 单表（`sheet_name` 是具体表名）——
      `cell_range` 是一块矩形：首行 = 各条线的名字（`header_row`），
      首列 = X 轴标签，其余每列一条线。
    * 跨表（`sheet_name="*"`）——
      X 轴 = 工作表顺序（1月/2月/…），`cell_range` 里的每一行 = 一条线，
      线名取自 `label_column` 那一列（同一批行）。用来画"逐月走势"。

    ## 用例（真表）

        plot_excel_data(f, sheet_name="*", cell_range="E54:E60",
                        label_column="B", chart_type="line",
                        title="主要鱼类 1-9月价格走势")
        -> 7 条鱼 × 9 个月，一次调用出图

    中文字体：挑了候选表里的第一个可用字体；一个都没有时会在回执里明确说明
    （图上中文会变方块）—— 不许静默给一张废图。
    """
    if chart_type not in CHART_TYPES:
        return _err("chart_type 只能是 %s，给的是 '%s'"
                    % (list(CHART_TYPES), chart_type))
    if not str(cell_range or "").strip():
        return _err("必须给 cell_range（要画哪一块数据）")

    src = guarded_resolve(file_path, "read")
    if not src.exists():
        return _err(f"文件不存在: {file_path}")
    if src.suffix.lower() == ".xls":
        return _err("画图不支持 .xls（没有流式）—— 另存为 .xlsx 再试。")

    try:
        sr, sc, er, ec = _parse_range(cell_range)
    except ValueError as e:
        return _err(f"cell_range 无效: {e}")

    x_labels: List[Any] = []
    series: List[Dict[str, Any]] = []

    try:
        wb = load_workbook(src, read_only=True, data_only=True)
    except Exception as e:                                      # noqa: BLE001
        return _err(f"打不开工作簿: {type(e).__name__}: {e}")
    try:
        names, err = pick_sheet_names(list(wb.sheetnames), sheet_name)
        if err:
            return _err(err)

        if isinstance(sheet_name, (list, tuple)) or sheet_name == "*":
            # 跨表：X = 工作表名，range 里每一行一条线
            lc = _col_index(label_column) if label_column else 0
            rows_by_sheet = []
            labels = None
            for sn in names:
                rows = [list(r) for r in wb[sn].iter_rows(
                    min_row=sr, max_row=er, min_col=sc, max_col=ec,
                    values_only=True)]
                rows_by_sheet.append(rows)
                if labels is None and lc:
                    labels = [wb[sn].cell(row=r, column=lc).value
                              for r in range(sr, er + 1)]
            x_labels = list(names)
            nrows = len(rows_by_sheet[0]) if rows_by_sheet else 0
            for i in range(nrows):
                vals = []
                for rows in rows_by_sheet:
                    v = rows[i][0] if i < len(rows) and rows[i] else None
                    vals.append(_num(v))
                nm = (str(labels[i]) if labels and i < len(labels) and labels[i] is not None
                      else ("第%d行" % (i + 1)))
                series.append({"name": nm.strip(), "values": vals})
        else:
            # 单表：首行线名，首列 X，其余每列一条线
            grid = [list(r) for r in wb[names[0]].iter_rows(
                min_row=sr, max_row=er, min_col=sc, max_col=ec, values_only=True)]
            if not grid:
                return _err(f"{cell_range} 里没有数据")
            head = grid[0] if header_row and header_row >= 1 else None
            body = grid[1:] if head else grid
            x_labels = [(str(r[0]) if r and r[0] is not None else "") for r in body]
            ncol = max((len(r) for r in grid), default=0)
            for j in range(1, ncol):
                nm = ""
                if head and j < len(head) and head[j] is not None:
                    nm = str(head[j]).strip()
                if not nm:
                    nm = _col_letter(sc + j)
                series.append({"name": nm,
                               "values": [_num(r[j]) if j < len(r) else None for r in body]})
    finally:
        try:
            wb.close()
        except Exception:                                       # noqa: BLE001
            pass

    if series_names:
        for i, s in enumerate(series):
            if i < len(series_names) and series_names[i]:
                s["name"] = str(series_names[i])
    series = [s for s in series if any(v is not None for v in s["values"])]
    if not series:
        return _err("这块区域里没有可画的数值（全是空的？）")

    return _render_chart(series, x_labels, chart_type, title, x_label, y_label,
                         output_name, src)


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _render_chart(series, x_labels, chart_type, title, x_label, y_label,
                  output_name, src: Path) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = _pick_cjk_font()
    if font:
        plt.rcParams["font.sans-serif"] = [font] + list(plt.rcParams["font.sans-serif"])
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(11, 5.6), dpi=140)
    xs = list(range(len(x_labels)))
    try:
        if chart_type == "pie":
            vals = [v for v in series[0]["values"] if v is not None]
            labs = [str(x_labels[i]) for i, v in enumerate(series[0]["values"])
                    if v is not None]
            ax.pie(vals, labels=labs, autopct="%1.1f%%")
        else:
            for s in series:
                ys = [float("nan") if v is None else v for v in s["values"]]
                if chart_type == "bar":
                    ax.bar(xs[:len(ys)], ys, label=s["name"])
                elif chart_type == "scatter":
                    ax.scatter(xs[:len(ys)], ys, label=s["name"], s=28)
                elif chart_type == "area":
                    ax.fill_between(xs[:len(ys)], ys, alpha=0.35, label=s["name"])
                    ax.plot(xs[:len(ys)], ys, linewidth=1.4)
                else:
                    ax.plot(xs[:len(ys)], ys, marker="o", markersize=4,
                            linewidth=1.8, label=s["name"])
            ax.set_xticks(xs)
            ax.set_xticklabels([str(x) for x in x_labels], rotation=0)
            ax.grid(True, alpha=0.28, linestyle="--")
            if len(series) > 1:
                ax.legend(loc="best", fontsize=9)
        if title:
            ax.set_title(title, fontsize=13)
        if x_label:
            ax.set_xlabel(x_label)
        if y_label:
            ax.set_ylabel(y_label)
        fig.tight_layout()

        out_name = output_name or (src.stem + "_图.png")
        if not out_name.lower().endswith(".png"):
            out_name += ".png"
        out_path = guarded_resolve(out_name, "write")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换 —— 半截的 PNG 比没有更难查
        tmp = out_path.with_name(out_path.name + ".lvtmp")
        # `format="png"` 必须显式给：临时文件叫 `xxx.png.lvtmp`，
        #   而 matplotlib 按后缀猜格式，它会去找一个叫 `lvtmp` 的格式然后报错
        #   （踩到过：`Format 'lvtmp' is not supported`）。
        fig.savefig(str(tmp), format="png")
    finally:
        plt.close(fig)

    os.replace(str(tmp), str(out_path))
    out: Dict[str, Any] = {
        "success": True,
        "output_path": str(out_path).replace("\\", "/"),
        # 给界面看的产物。`core/artifacts.py` 会把它
        #   收进 `data/attachments/` 并从模型可见的回执里删掉（模型有
        #   `output_path` 就够了）—— 聊天区靠它把图直接画出来。
        "artifacts": [{"path": str(out_path).replace("\\", "/"),
                       "name": out_path.name}],
        "chart_type": chart_type,
        "series_count": len(series),
        "series_names": [s["name"] for s in series],
        "x_count": len(x_labels),
        "message": f"已画出 {len(series)} 条线 / {len(x_labels)} 个点，存到 "
                   f"{out_path.name} —— 用 deliver_file 发给用户。",
    }
    if not font:
        out["warning"] = ("★ 这台机器上**没找到任何中文字体**（试过 %d 个候选）—— "
                          "图上的中文会显示成方块。" % len(CJK_FONT_CANDIDATES))
    return out
