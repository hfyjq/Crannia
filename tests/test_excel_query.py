# -*- coding: utf-8 -*-
"""Excel 读侧查询三件：`profile_excel_data` / `find_excel_value` / `get_excel_comments`。

判据来自一张 9 张月表的经营总结，见 `excel_query.py` 的模块 docstring。
本文件只钉三件最容易被写错、而错了不会红的东西：

| # | 判据 | 为什么 |
|---|---|---|
| 1 | 读不到 != 0 | 文本列没有数字时，`sum/mean/min/max` 必须是 `None` 不是 0 |
| 2 | 唯一值封顶后不许报那个 cap 数 | 报 `1000` 看起来像一个结论（而它是"至少这么多"） |
| 3 | 预算到点必须点破 | 只说"扫了前 N 行"会被读成"表就这么长" |
| 4 | 全空列不报 | 一张 21 列的表按 200 列扫会报出 179 个幽灵列，回执 34.8K（闸是 6000） |
| 5 | 命中被截断要报数 | 报"还差多少条"，不是"少多少字符" |

跑法::

    .venv/Scripts/python.exe tests/test_excel_query.py

输出一律 ASCII（Windows 控制台 GBK）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import sandbox  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check_(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def _init_handler():
    from core.security import SecurityGuard
    from skills.office_parser import handler as H
    H.init(SecurityGuard(allowed_dirs=["./workspace"], command_blacklist=[],
                         audit_log_path=str(Path(tempfile.gettempdir())
                                             / "lv_excel_query_audit.log")))


def _mk(name: str, rows, *, header=None, comments=None) -> Path:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    p = Path("workspace") / name
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    if header:
        ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    for addr, (text, author) in (comments or {}).items():
        ws[addr].comment = Comment(text, author)
    wb.save(str(p))
    wb.close()
    return p


def _col(r, letter):
    return next((c for c in r["columns"] if c["column"] == letter), None)


# 一、画像

def test_profile_basic_stats() -> None:
    print("\n-- 1. 画像：基本统计量")
    from skills.office_parser.excel_query import profile_excel_data as pf
    _init_handler()
    p = _mk("q_basic.xlsx", [(1, "甲"), (2, "乙"), (3, "甲"), (4, "丙")],
            header=["数值", "名称"])
    r = pf(str(p), "S1", header_row=1)
    check_("读成功", r.get("success") is True, str(r)[:200])
    check_("扫了 4 行", r.get("scanned_rows") == 4, repr(r.get("scanned_rows")))
    check_("complete=True（没撞预算）", r.get("complete") is True, str(r))
    a = _col(r, "A")
    check_("A 列求和 = 10", a["sum"] == 10, repr(a))
    check_("A 列均值 = 2.5", a["mean"] == 2.5, repr(a))
    check_("A 列 min/max = 1/4", (a["min"], a["max"]) == (1.0, 4.0), repr(a))
    check_("列名取自表头", a["name"] == "数值", repr(a.get("name")))
    b = _col(r, "B")
    check_("B 列唯一值 = 3", b["distinct"] == 3, repr(b))


def test_profile_text_column_reports_none_not_zero() -> None:
    """读不到 != 0。

    一整列全是文字，统计量必须是 `None`。写成 `0` 的话，
    模型会当成"这一列的和是 0"，而那是另一个结论。
    负对照：把 `else` 分支的 `None` 改成 `0`，本条立刻红。
    """
    print("\n-- 2. ★★ 文本列的统计量是 None，不是 0")
    from skills.office_parser.excel_query import profile_excel_data as pf
    _init_handler()
    p = _mk("q_text.xlsx", [("甲",), ("乙",), ("丙",)], header=["名称"])
    r = pf(str(p), "S1", header_row=1)
    a = _col(r, "A")
    for k in ("sum", "mean", "min", "max", "stdev"):
        check_("%s 是 None（不是 0）" % k, a[k] is None, "%s=%r" % (k, a[k]))
    check_("numeric_count = 0（这是个明确的结论）", a["numeric_count"] == 0, repr(a))
    check_("而唯一值照常数得出", a["distinct"] == 3, repr(a))


def test_profile_distinct_cap_is_null_not_the_cap() -> None:
    """唯一值封顶后不许报那个 cap 数 —— 它看起来像结论。

    回 `distinct: null` + `distinct_at_least: N` + `distinct_capped: true`。
    """
    print("\n-- 3. ★★ 唯一值封顶：回 null + 至少多少")
    from skills.office_parser.excel_query import profile_excel_data as pf
    _init_handler()
    p = _mk("q_cap.xlsx", [(i,) for i in range(10)], header=["编号"])
    r = pf(str(p), "S1", header_row=1, distinct_cap=3)
    a = _col(r, "A")
    check_("distinct 是 None（不是 3）", a["distinct"] is None, repr(a))
    check_("distinct_capped = True", a.get("distinct_capped") is True, repr(a))
    check_("distinct_at_least 说得出来", a.get("distinct_at_least", 0) >= 3, repr(a))
    r2 = pf(str(p), "S1", header_row=1, distinct_cap=100)
    check_("没封顶时照常数得出", _col(r2, "A")["distinct"] == 10, repr(_col(r2, "A")))


def test_profile_budget_stops_and_says_it_is_not_the_whole_table() -> None:
    """预算到点必须点破。

    只说"扫了前 N 行"会被读成"表就这么长" —— 那正是这类工具最容易制造的假结论。
    """
    print("\n-- 4. ★ 撞预算：停 + 说明只覆盖了一部分")
    from skills.office_parser.excel_query import profile_excel_data as pf
    _init_handler()
    p = _mk("q_cap2.xlsx", [(i,) for i in range(10)], header=["编号"])
    r = pf(str(p), "S1", header_row=1, max_scan_rows=3)
    check_("complete=False", r.get("complete") is False, str(r)[:200])
    check_("stopped_reason 说得出是哪一种",
           r.get("stopped_reason") == "row_budget", repr(r.get("stopped_reason")))
    check_("scanned_rows = 3（字段名不叫 total_rows）",
           r.get("scanned_rows") == 3, repr(r.get("scanned_rows")))
    check_("★ warning 点破了那个假结论",
           "不代表表里只有" in (r.get("warning") or ""), repr(r.get("warning")))
    check_("没撞预算时不带 stopped_reason",
           "stopped_reason" not in pf(str(p), "S1", header_row=1))


def test_profile_drops_all_blank_columns() -> None:
    """把 21 列的表当 200 列扫，会报出 179 个幽灵列，回执 34.8K（闸是 6000）。"""
    print("\n-- 5. 全空列不报（否则回执会被幽灵列顶穿）")
    from skills.office_parser.excel_query import profile_excel_data as pf
    _init_handler()
    p = _mk("q_blank.xlsx", [(1,), (2,)], header=["数值"])
    r = pf(str(p), "S1", header_row=1, max_columns=50)
    check_("只报有内容的列", r.get("columns_profiled") == 1,
          repr(r.get("columns_profiled")))
    check_("并把跳过的列数说出来", r.get("blank_columns_skipped", 0) > 0,
          repr(r.get("blank_columns_skipped")))


# 二、按值定位

def test_find_by_contains_equals_regex() -> None:
    print("\n-- 6. 按值定位：三种 match")
    from skills.office_parser.excel_query import find_excel_value as fv
    _init_handler()
    p = _mk("q_find.xlsx", [("花鲢", 8.68), ("白鲢", 4.01), ("鲈鱼", 13.03)])
    r = fv(str(p), "S1", "鲢", match="contains")
    check_("contains 命中 2 条", r["match_count"] == 2, repr(r.get("match_count")))
    check_("地址是对的（行号按表算）",
          sorted(m["address"] for m in r["matches"]) == ["A1", "A2"],
          repr([m["address"] for m in r["matches"]]))
    r2 = fv(str(p), "S1", "鲈鱼", match="equals")
    check_("equals 只中整格相等的那一个", r2["match_count"] == 1, repr(r2.get("matches")))
    check_("返回值带着内容", r2["matches"][0]["value"] == "鲈鱼", repr(r2["matches"]))
    r3 = fv(str(p), "S1", r"^花|^白", match="regex")
    check_("regex 命中 2 条", r3["match_count"] == 2, repr(r3.get("matches")))


def test_find_validates_inputs() -> None:
    print("\n-- 7. 参数坏了要明说（不静默回退）")
    from skills.office_parser.excel_query import find_excel_value as fv
    _init_handler()
    p = _mk("q_find2.xlsx", [("x",)])
    check_("空 value -> 拒", fv(str(p), "S1", "")["success"] is False)
    check_("坏 match -> 拒", fv(str(p), "S1", "x", match="bogus")["success"] is False)
    r = fv(str(p), "S1", "([", match="regex")
    check_("坏正则 -> 拒且说明", r["success"] is False and "正则" in r["error"],
          str(r)[:120])


def test_find_truncation_reports_counts_not_chars() -> None:
    """命中被截断时报还差多少条（报数不报字符）。"""
    print("\n-- 8. 命中截断：回原上限 + 明说还有没列出来的")
    from skills.office_parser.excel_query import find_excel_value as fv
    _init_handler()
    p = _mk("q_trunc.xlsx", [("命中%d" % i,) for i in range(20)])
    r = fv(str(p), "S1", "命中", match="contains", max_matches=5)
    check_("只回 5 条", r["match_count"] == 5, repr(r.get("match_count")))
    check_("matches_truncated = True", r.get("matches_truncated") is True, str(r)[:150])
    check_("★ note 明说还有其他命中没列出来",
           "其他命中" in (r.get("note") or ""), repr(r.get("note")))


# 三、批注

def test_excel_comments_are_readable() -> None:
    """Excel 的批注要读得回来。

    批注是脚注形式的信息，Word 的批注工具管不到 Excel 工作簿。
    这条测试钉的是能力，不是某个具体条数 —— 换一张表，条数就会变。
    """
    print("\n-- 9. ★ Excel 批注读得回来（Word 那三件管不到）")
    from skills.office_parser.excel_query import get_excel_comments as gc
    _init_handler()
    p = _mk("q_cm.xlsx", [("x",), ("y",)],
            comments={"A1": ("不包含私家车5340元", "Administrator"),
                      "A2": ("工资同比增加1.55万元", "张三")})
    r = gc(str(p), "*")
    check_("读成功", r.get("success") is True, str(r)[:200])
    check_("★ 两条批注都回来了", r.get("comment_count") == 2,
          repr(r.get("comment_count")))
    by = {c["cell"]: c for c in r["comments"]}
    check_("地址对", set(by) == {"A1", "A2"}, repr(list(by)))
    check_("正文对", by["A1"]["text"] == "不包含私家车5340元", repr(by["A1"]))
    check_("作者也带上了", by["A2"]["author"] == "张三", repr(by["A2"]))
    check_("分表计数说得出来", r.get("per_sheet") == {"S1": 2}, repr(r.get("per_sheet")))


def test_no_comments_is_a_conclusion_not_a_failure() -> None:
    """「查过了、确实没有」与「没查到」是两件事。"""
    print("\n-- 10. 没有批注：如实说『查过了没有』")
    from skills.office_parser.excel_query import get_excel_comments as gc
    _init_handler()
    p = _mk("q_cm2.xlsx", [("x",)])
    r = gc(str(p), "*")
    check_("成功（不是失败）", r.get("success") is True, str(r)[:150])
    check_("条数是 0", r.get("comment_count") == 0, repr(r.get("comment_count")))
    check_("★ note 说的是『查过了、确实没有』",
           "确实没有" in (r.get("note") or ""), repr(r.get("note")))


def main() -> int:
    print("=" * 64)
    print("Excel 读侧查询三件")
    print("=" * 64)
    with sandbox():
        for fn in (test_profile_basic_stats,
                   test_profile_text_column_reports_none_not_zero,
                   test_profile_distinct_cap_is_null_not_the_cap,
                   test_profile_budget_stops_and_says_it_is_not_the_whole_table,
                   test_profile_drops_all_blank_columns,
                   test_find_by_contains_equals_regex,
                   test_find_validates_inputs,
                   test_find_truncation_reports_counts_not_chars,
                   test_excel_comments_are_readable,
                   test_no_comments_is_a_conclusion_not_a_failure):
            try:
                fn()
            except AssertionError as exc:
                check_("%s 抛断言" % fn.__name__, False, str(exc))
            except Exception as exc:  # noqa: BLE001
                check_("%s 抛异常" % fn.__name__, False,
                       "%s: %s" % (type(exc).__name__, exc))
    print("\n" + "=" * 64)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
