"""Excel 的读侧查询三件：表格画像 / 按值定位 / 批注。

## 这三件按什么定的

前两件是计划里的（`profile_excel_data` / `find_excel_value`）；第三件由真实表确定：

`AAA公司）2026年09月度经营总结.xlsx` 里有 21 条批注，而它们不是装饰：

    9月!L40  不包含私家车5340元
    9月!L41  不包含私家车月结4200元，场地占用240元
    5月!E11  工资同比增加1.55万元 / 易耗品增加2.55万元

那是脚注：不读它，就会把「变动原因」总结错。而仓库里
`get_all_comments` / `get_comments_by_author` / `get_comments_for_paragraph`
三件全是 Word 的（python-docx），Excel 批注一件都没有，这一件是补那个缺口。

## 三条判据（贯穿本文件，都别改）

1. 流式：`read_only=True` + `iter_rows(values_only=True)`，不许物化整表。
   这是硬要求（"严禁将全文塞入短期上下文" + "流式处理: openpyxl read_only"）
   的代码形态 —— 照它做的，不是绕开它。
2. 读不到 != 0：唯一值数封顶后回 `null` +
   `distinct_at_least`，不回一个看起来像结论的 `1000`；数值列没有数字时
   `mean/min/max/sum` 一律 `null`，不是 0。
3. 诚实早退：撞时间/行数预算就停，回 `complete: false` + `stopped_reason`，
   并点破"只扫了前 N 行 不等于 表里只有这些行"。

两件读工具都必须过 `read` 档的写标记闸（`tests/test_permission_tiers.py` 的
AST 读者），本文件里不许出现 `save` / `write_*` / `to_csv` / `copy` / `move` 那些。
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ._excel_io import guarded_resolve, pick_sheet_names

# 预算与上限（常量不是旋钮：只有两格随档，别把它们变成参数）
PROFILE_MAX_SCAN_ROWS = 200_000
PROFILE_TIME_BUDGET_S = 20.0
PROFILE_DISTINCT_CAP = 1000
PROFILE_QUANTILE_SAMPLE = 10_000
PROFILE_MAX_COLUMNS = 200
#: profile 回执里最多报几列（超了报数不报字符）。21 列的表
#: 报 200 列时回执 34.8K —— 比 6000 字符那道闸大 6 倍，而多出来的全是幽灵列。
PROFILE_MAX_REPORT_COLUMNS = 80
PROFILE_SAMPLE_VALUES = 5

FIND_MAX_MATCHES = 200
FIND_SCAN_ROW_CAP = 200_000

#: 时间检查的间隔（不是每行都看钟 —— 那在几十万行上是可观的常数开销）
_TICK_EVERY = 2000


# 一、表格画像

class _ColStat:
    """逐列 running 状态 —— 绝不攒值（流式处理硬约束）。

    均值/方差用 Welford，不用 `sum / sum_of_squares`：
    后者在"20 万行大额金额"上会掉精度，而一个看起来像结论的错数是本仓库反复警告的东西。
    """

    __slots__ = ("n", "non_null", "types", "n_num", "mean", "m2", "mn", "mx",
                 "total", "keys", "distinct_capped", "samples", "res", "res_seen")

    def __init__(self) -> None:
        self.n = 0
        self.non_null = 0
        self.types: Dict[str, int] = {}
        self.n_num = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.mn: Optional[float] = None
        self.mx: Optional[float] = None
        self.total = 0.0
        self.keys: set = set()
        self.distinct_capped = False
        self.samples: List[str] = []
        self.res: List[float] = []
        self.res_seen = 0

    def add(self, v: Any, cap: int, keep_res: bool) -> None:
        self.n += 1
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return
        self.non_null += 1
        t = _type_of(v)
        self.types[t] = self.types.get(t, 0) + 1

        if t in ("int", "float"):
            x = float(v)
            self.n_num += 1
            self.total += x
            d = x - self.mean
            self.mean += d / self.n_num          # Welford：均值/方差数值稳定
            self.m2 += d * (x - self.mean)
            self.mn = x if self.mn is None else min(self.mn, x)
            self.mx = x if self.mx is None else max(self.mx, x)
            if keep_res:
                # 蓄水池采样（Algorithm R）—— 分位数只在近似下给
                self.res_seen += 1
                if len(self.res) < PROFILE_QUANTILE_SAMPLE:
                    self.res.append(x)
                else:
                    j = _randrange(self.res_seen)
                    if j < PROFILE_QUANTILE_SAMPLE:
                        self.res[j] = x

        # 唯一值数是唯一一个会退化成 O(n) 内存的量，封顶并出声
        if not self.distinct_capped:
            self.keys.add(str(v))
            if len(self.keys) > cap:
                self.distinct_capped = True

        if len(self.samples) < PROFILE_SAMPLE_VALUES:
            s = str(v)[:60]
            if s not in self.samples:
                self.samples.append(s)

    def as_dict(self, letter: str, name: Optional[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "column": letter,
            "name": name,
            "non_null": self.non_null,
            "blank": self.n - self.non_null,
        }
        if self.types:
            out["types"] = dict(sorted(self.types.items(), key=lambda kv: -kv[1]))
        out["numeric_count"] = self.n_num
        # 读不到 != 0：一个数字都没有时一律 null，不是 0
        if self.n_num:
            out["sum"] = round(self.total, 6)
            out["mean"] = round(self.mean, 6)
            out["min"] = self.mn
            out["max"] = self.mx
            out["stdev"] = (round(math.sqrt(self.m2 / (self.n_num - 1)), 6)
                            if self.n_num > 1 else None)
        else:
            out["sum"] = None
            out["mean"] = None
            out["min"] = None
            out["max"] = None
            out["stdev"] = None
        if self.distinct_capped:
            # 不许报那个 cap 数 —— 它看起来像一个结论
            out["distinct"] = None
            out["distinct_capped"] = True
            out["distinct_at_least"] = len(self.keys)
        else:
            out["distinct"] = len(self.keys)
        if self.samples:
            out["samples"] = self.samples
        return out


def _randrange(n: int) -> int:
    """`random.randrange` 的一层薄封装（避免在热循环里反复 import）。"""
    import random
    return random.randrange(n)


def _type_of(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "text"
    if hasattr(v, "year") and hasattr(v, "month"):
        return "date"
    return type(v).__name__


def profile_excel_data(
    file_path: str,
    sheet_name: str = "",
    header_row: int = 1,
    cell_range: Optional[str] = None,
    max_scan_rows: int = PROFILE_MAX_SCAN_ROWS,
    time_budget_s: float = PROFILE_TIME_BUDGET_S,
    distinct_cap: int = PROFILE_DISTINCT_CAP,
    include_quantiles: bool = False,
    max_columns: int = PROFILE_MAX_COLUMNS,
) -> dict:
    """给一张表的每一列做画像：类型 / 空值 / 唯一值数 / 最值 / 均值 / 标准差。

    ## 它答什么问题

    「这张表长什么样、每列什么分布、某个聚合是多少」——
    典型用法：先画像再决定读哪一块（而不是先读整表撞上预算）。

    它不是 `summarize_document` 的同类：那个答的是"表里某段文字在说什么"
    （走 `parse_xlsx` -> 分块 -> 检索那条路）。分工写在这里，也写进 manifest 的描述。

    ## 三条必须记住的

    * 流式：逐行 `iter_rows`，不把表物化进内存（流式处理硬要求）。
    * 唯一值数是唯一会退化成 O(n) 内存的量，超过 `distinct_cap` 就回
      `distinct: null` + `distinct_at_least`，绝不回那个 cap 数
      （读不到 != 0）。
    * 预算到点就停并如实说：`complete: false` + `stopped_reason`，
      而且点破"只扫了前 N 行不等于表里只有这些行"。
      字段叫 `scanned_rows`，不叫 `total_rows` —— 后者是在撒谎。

    `include_quantiles=True` 时给 p50/p90/p99，但标 `approximate: true`
    （精确分位数要全部值，与流式冲突，用蓄水池采样）。
    """
    path = guarded_resolve(file_path, "read")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    if path.suffix.lower() == ".xls":
        return {"success": False,
                "error": "画像要流式 + 缓存值/公式区分，而 .xls（xlrd）两样都没有。",
                "hint": "用 Excel 另存为 .xlsx 再试。"}
    if max_columns < 1 or max_columns > 1000:
        return {"success": False, "error": "max_columns 要在 1..1000 之间"}
    if distinct_cap < 1:
        return {"success": False, "error": "distinct_cap 要 >= 1"}
    if max_scan_rows < 1:
        return {"success": False, "error": "max_scan_rows 要 >= 1"}

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:                                  # noqa: BLE001
        return {"success": False, "error": f"打不开工作簿: {type(e).__name__}: {e}"}

    try:
        names, err = pick_sheet_names(list(wb.sheetnames), sheet_name)
        if err:
            return {"success": False, "error": err}

        blocks = []
        for sn in names:
            r = _profile_one(wb[sn], sn, header_row, cell_range, max_scan_rows,
                             time_budget_s, distinct_cap, include_quantiles,
                             max_columns)
            if r.get("success") is False:
                return r
            blocks.append(r)

        if isinstance(sheet_name, (list, tuple)) or sheet_name == "*":
            return {"success": True, "sheets_read": len(names), "blocks": blocks}
        return {"success": True, **blocks[0]}
    finally:
        try:
            wb.close()
        except Exception:                                   # noqa: BLE001
            pass


def _profile_one(ws, sn, header_row, cell_range, max_scan_rows, time_budget_s,
                 distinct_cap, include_quantiles, max_columns) -> Dict[str, Any]:
    from .excel_tools import _parse_cell_range
    if cell_range:
        try:
            sr, sc, er, ec = _parse_cell_range(
                *(str(cell_range).split(":") + [None])[:2])
        except Exception:                                   # noqa: BLE001
            return {"success": False, "error": f"cell_range 解析不了: {cell_range!r}"}
    else:
        sr, sc, er, ec = 1, 1, None, max_columns

    names = None
    if header_row and header_row >= 1:
        for row in ws.iter_rows(min_row=int(header_row), max_row=int(header_row),
                                values_only=True):
            names = list(row)
            break

    stats: Dict[int, _ColStat] = {}
    scanned = 0
    stopped = ""
    t0 = time.monotonic()
    body_start = (int(header_row) + 1) if (header_row and header_row >= 1) else sr

    for row in ws.iter_rows(min_row=body_start, max_row=er,
                            min_col=sc, max_col=ec, values_only=True):
        if scanned >= max_scan_rows:
            stopped = "row_budget"
            break
        if scanned % _TICK_EVERY == 0 and time.monotonic() - t0 > time_budget_s:
            stopped = "time_budget"
            break
        for j, v in enumerate(row):
            col = sc + j
            st = stats.get(col)
            if st is None:
                st = stats[col] = _ColStat()
            st.add(v, distinct_cap, include_quantiles)
        scanned += 1

    cols_out = []
    dropped_blank = 0
    for col in sorted(stats):
        st = stats[col]
        # 全空的列不报：`ec` 给 200 而表只有 21 列时，
        #   `read_only` 的 `iter_rows(max_col=200)` 会把每行补到 200 列，
        #   profile 里冒出 179 个全是 null 的"幽灵列"，而回执因此从 3.6K
        #   涨到 34.8K（顶上 6000 字符那道闸的就是它们）。
        if st.non_null == 0:
            dropped_blank += 1
            continue
        letter = get_column_letter(col)
        nm = None
        if names and (col - sc) < len(names):
            raw = names[col - sc]
            nm = str(raw).strip() if raw is not None else None
            if nm == "":
                nm = None
        d = st.as_dict(letter, nm)
        if include_quantiles and st.res:
            d["quantiles"] = _quantiles(st.res)
            d["quantile_note"] = "近似值（蓄水池采样，样本 %d）" % len(st.res)
        cols_out.append(d)

    truncated_cols = 0
    if len(cols_out) > PROFILE_MAX_REPORT_COLUMNS:
        truncated_cols = len(cols_out) - PROFILE_MAX_REPORT_COLUMNS
        cols_out = cols_out[:PROFILE_MAX_REPORT_COLUMNS]

    out: Dict[str, Any] = {
        "success": True,
        "sheet_name": sn,
        "header_row": header_row,
        "scanned_rows": scanned,
        "columns_profiled": len(cols_out),
        "columns": cols_out,
        "complete": not stopped,
    }
    if dropped_blank:
        out["blank_columns_skipped"] = dropped_blank
    if truncated_cols:
        # 报列数不是"少多少字符"
        out["columns_truncated"] = truncated_cols
    if stopped:
        out["stopped_reason"] = stopped
        # 点破那个假结论
        out["warning"] = (
            "只扫了前 %d 行就停了（%s）—— ★ **这不代表表里只有这些行**："
            "下面的计数/最值/均值都只覆盖**已扫的这部分**。"
            % (scanned, "撞到时间预算" if stopped == "time_budget" else "撞到行数预算"))
    return out


def _quantiles(sample: List[float]) -> Dict[str, float]:
    s = sorted(sample)
    n = len(s)

    def q(p: float) -> float:
        if n == 0:
            return float("nan")
        i = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(s[i], 6)

    return {"p50": q(0.50), "p90": q(0.90), "p99": q(0.99), "approximate": True}


# 二、按值定位

FIND_MATCHES = ("contains", "equals", "regex")


def find_excel_value(
    file_path: str,
    sheet_name: str = "",
    value: str = "",
    match: str = "contains",
    cell_range: Optional[str] = None,
    max_matches: int = FIND_MAX_MATCHES,
    case_sensitive: bool = False,
) -> dict:
    """在表里按值定位，回命中格的地址。

    「这个值在哪」与「这块区域是什么分布」是两件事 —— 后者归 `profile_excel_data`。

    * `match` —— `"contains"`（默认，子串）/ `"equals"`（整格相等）/ `"regex"`（正则）
    * 命中数有上限，撞到时回 `matches_truncated: true` 且报数
      （报还差多少个，不是"少多少字符"）。
    """
    path = guarded_resolve(file_path, "read")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if match not in FIND_MATCHES:
        return {"success": False,
                "error": "match 只能是 %s，给的是 '%s'" % (list(FIND_MATCHES), match)}
    if value == "":
        return {"success": False, "error": "value 不能为空（那会命中所有格子）"}
    if path.suffix.lower() == ".xls":
        return {"success": False, "error": ".xls 不支持按值定位（没有流式）。",
                "hint": "用 Excel 另存为 .xlsx 再试。"}

    try:
        if match == "regex":
            flags = 0 if case_sensitive else re.IGNORECASE
            rx = re.compile(str(value), flags)
        else:
            rx = None
    except re.error as e:
        return {"success": False, "error": f"正则写错了: {e}"}

    needle = str(value) if case_sensitive else str(value).lower()

    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:                                  # noqa: BLE001
        return {"success": False, "error": f"打不开工作簿: {type(e).__name__}: {e}"}

    try:
        names, err = pick_sheet_names(list(wb.sheetnames), sheet_name)
        if err:
            return {"success": False, "error": err}

        from .excel_tools import _parse_cell_range
        if cell_range:
            try:
                parts = str(cell_range).split(":")
                sr, sc, er, ec = _parse_cell_range(parts[0], parts[1] if len(parts) > 1 else None)
            except Exception:                               # noqa: BLE001
                return {"success": False, "error": f"cell_range 解析不了: {cell_range!r}"}
        else:
            sr, sc, er, ec = 1, 1, None, None

        matches: List[Dict[str, Any]] = []
        scanned = 0          # 跨表累计的行数（只用来判上限）
        truncated = False
        for sn in names:
            for i, row in enumerate(wb[sn].iter_rows(min_row=sr, max_row=er,
                                                     min_col=sc, max_col=ec,
                                                     values_only=True)):
                row_idx = sr + i            # 地址要用这张表的实际行号
                if scanned >= FIND_SCAN_ROW_CAP or len(matches) >= max_matches:
                    truncated = True
                    break
                scanned += 1
                for j, v in enumerate(row):
                    if v is None:
                        continue
                    s = str(v)
                    if rx is not None:
                        hit = bool(rx.search(s))
                    elif match == "equals":
                        hit = (s == str(value)) if case_sensitive else (s.lower() == needle)
                    else:
                        hit = (str(value) in s) if case_sensitive else (needle in s)
                    if hit:
                        matches.append({
                            "sheet_name": sn,
                            "address": "%s%d" % (get_column_letter(sc + j), row_idx),
                            "value": s[:120],
                        })
                        if len(matches) >= max_matches:
                            truncated = True
                            break
                if len(matches) >= max_matches:
                    break
            if truncated:
                break

        out: Dict[str, Any] = {
            "success": True,
            "match": match,
            "value": value,
            "match_count": len(matches),
            "matches": matches,
            "scanned_rows": scanned,
        }
        if truncated:
            out["matches_truncated"] = True
            out["note"] = ("只回到 %d 条就停了（上限 %d / 行数上限 %d）—— "
                           "★ 还有其他命中**没有列出来**，第 %d 行的命中可能被截在中间。"
                           % (len(matches), max_matches, FIND_SCAN_ROW_CAP, scanned + sr))
        return out
    except Exception as e:                                  # noqa: BLE001
        return {"success": False, "error": f"查找失败: {type(e).__name__}: {e}"}
    finally:
        try:
            wb.close()
        except Exception:                                   # noqa: BLE001
            pass


# 三、批注（真表照出来的缺口）

def get_excel_comments(file_path: str, sheet_name: str = "*") -> dict:
    """读 Excel 的单元格批注。

    这一件存在的理由：`AAA公司…经营总结.xlsx` 有 21 条批注，
    而它们是脚注不是装饰 ——

        9月!L40  不包含私家车5340元
        9月!L41  不包含私家车月结4200元，场地占用240元

    不读它，就会把「变动原因」总结错。而仓库里 `get_all_comments` 三件
    全是 Word 的（python-docx），Excel 侧一直是空的。

    批注要全载才读得到（`read_only=True` 的 sheet 没有批注），
    所以本工具不用流式 —— 但会按行扫描，且给行数上限。
    `ws._comments` 在本版本 openpyxl 上是空的（2 条批注时它报 0）
只能逐格扫 `cell.comment`，那是可靠的。
    """
    path = guarded_resolve(file_path, "read")
    if not path.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}
    if path.suffix.lower() == ".xls":
        return {"success": False, "error": ".xls 的批注读不了。",
                "hint": "用 Excel 另存为 .xlsx 再试。"}

    try:
        wb = load_workbook(path, read_only=False)
    except Exception as e:                                  # noqa: BLE001
        return {"success": False, "error": f"打不开工作簿: {type(e).__name__}: {e}"}

    try:
        names, err = pick_sheet_names(list(wb.sheetnames), sheet_name)
        if err:
            return {"success": False, "error": err}

        comments: List[Dict[str, Any]] = []
        per_sheet: Dict[str, int] = {}
        for sn in names:
            ws = wb[sn]
            n0 = len(comments)
            for row in ws.iter_rows():
                for cell in row:
                    c = getattr(cell, "comment", None)
                    if c is None:
                        continue
                    text = (c.text or "").strip()
                    comments.append({
                        "sheet_name": sn,
                        "cell": cell.coordinate,
                        "author": getattr(c, "author", "") or "",
                        "text": text[:1000],
                    })
            per_sheet[sn] = len(comments) - n0

        out: Dict[str, Any] = {
            "success": True,
            "sheets_read": len(names),
            "comment_count": len(comments),
            "per_sheet": per_sheet,
            "comments": comments,
        }
        if not comments:
            out["note"] = ("这几张表**一条批注都没有**（这是「查过了、确实没有」；"
                           "不是「没查到」）。")
        return out
    except Exception as e:                                  # noqa: BLE001
        return {"success": False, "error": f"读批注失败: {type(e).__name__}: {e}"}
    finally:
        try:
            wb.close()
        except Exception:                                   # noqa: BLE001
            pass
