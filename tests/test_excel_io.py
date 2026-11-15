"""Excel 的 `_excel_io` 层：原子写 + 回读校验。

## 为什么要有这个文件

`excel_tools.py` 里 20 处写盘原来都是裸 `wb.save(str(path))`：

* 大表保存要好几秒 —— 中途失败，盘上是一份写到一半的新内容，
  而旧的原件已经没了；
* 保存"看起来成功了"而盘上是半截的 —— 没有任何东西会告诉你。

统一走 `_excel_io.save_workbook`：同目录兄弟临时文件 → 回读校验 → `os.replace`。
判据：改数据这件事不许静默地做错 —— 要么成功落地，要么原文件一个字节不变。

## 五条

| # | 判据 |
|---|---|
| 1 | 存成功之后不留临时文件，且文件是有效的 xlsx |
| 2 | 保存抛异常时，原文件一个字节没变，且不留临时文件 |
| 3 | 写出来的东西坏了（不是有效 zip），报错、不替换原文件 |
| 4 | `_verify_xlsx` 不许 `load_workbook` —— 那在大表上等于第二遍全量读 |
| 5 | 源级棘轮：`excel_tools.py` 里不许再出现裸 `wb.save(` |
| 6 | `os.replace` 的瞬时占用要重试；一直失败，回错误串、原文件不动 |

用法::

    cd <仓库根>
    .venv/Scripts/python.exe tests/test_excel_io.py

输出一律 ASCII（Windows 控制台 GBK）。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

EXCEL_TOOLS = _PROJECT_ROOT / "skills" / "office_parser" / "excel_tools.py"
EXCEL_IO = _PROJECT_ROOT / "skills" / "office_parser" / "_excel_io.py"

_WB_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<sheets>%s</sheets></workbook>"
)


def _sheet_xml(names) -> str:
    return _WB_XML % "".join('<sheet name="%s" sheetId="%d"/>' % (n, i + 1)
                             for i, n in enumerate(names))


class _FakeWB:
    """最小替身：只实现 `save` / `close` / `sheetnames`。

    必须是会坏的替身 —— 一个永远写对的假对象会让
    「校验生效」和「恰好没坏」看起来一模一样（恒真的断言）。
    """

    def __init__(self, sheets=("Sheet",), mode="ok"):
        self.sheetnames = list(sheets)
        self._mode = mode
        self.closed = False

    def save(self, p):
        p = Path(p)
        if self._mode == "raise":
            raise OSError("磁盘满了（模拟）")
        if self._mode == "garbage":
            p.write_bytes(b"NOT-A-ZIP")
            return
        if self._mode == "fewer_sheets":
            names = self.sheetnames[:-1] or ["Only"]
        elif self._mode == "more_sheets":
            names = list(self.sheetnames) + ["Extra"]
        else:
            names = self.sheetnames
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/workbook.xml", _sheet_xml(names))
            z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")

    def close(self):
        self.closed = True


# ------------------------------------------------------------------

def test_a_good_save_leaves_no_temp_and_a_valid_file():
    from skills.office_parser._excel_io import save_workbook
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tgt = Path(td) / "out.xlsx"
        wb = _FakeWB(("Sheet1", "Sheet2"))
        err = save_workbook(wb, tgt)
        assert err == "", "好的保存不该报错：%r" % err
        assert wb.closed, "save 之后必须 close（Windows 上被打开的文件不许替换）"
        assert tgt.exists(), "目标文件没出现"
        assert not list(Path(td).glob("*.lvtmp")), "留了临时文件"
        with zipfile.ZipFile(tgt) as z:
            assert "xl/workbook.xml" in z.namelist()
    print("  [ok] 存成功：不留 .lvtmp、close 已调、文件是有效 xlsx")


def test_a_raising_save_does_not_touch_the_original():
    """保存抛异常，原文件一个字节没变，且不留临时文件。

    这正是原子写要买的东西：没有它，失败那一刻盘上就是半截内容，
    而旧的原件已经没了。
    """
    from skills.office_parser._excel_io import save_workbook
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tgt = Path(td) / "out.xlsx"
        _FakeWB(("Sheet1",)).save(str(tgt))        # 先造一个"原件"
        before = tgt.read_bytes()

        try:
            save_workbook(_FakeWB(("Sheet1",), mode="raise"), tgt)
            raise AssertionError("保存抛异常时 save_workbook 竟然没把异常放出来")
        except OSError:
            pass

        assert tgt.read_bytes() == before, "★ 保存失败却动了原文件 —— 原子写没生效"
        assert not list(Path(td).glob("*.lvtmp")), "失败后留了临时文件"
    print("  [ok] 保存抛异常：原文件逐字节未变、不留临时文件、异常照抛")


def test_a_broken_output_is_refused_and_the_original_survives():
    """写出来的东西坏了，报错、不替换。

    这是"保存看起来成功了、而盘上是半截的"那条 ——
    回读校验唯一存在的理由。负对照：去掉 `_verify_xlsx` 的调用，本条立刻红。
    """
    from skills.office_parser._excel_io import save_workbook
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tgt = Path(td) / "out.xlsx"
        _FakeWB(("Sheet1",)).save(str(tgt))
        before = tgt.read_bytes()

        err = save_workbook(_FakeWB(("Sheet1",), mode="garbage"), tgt)
        assert err, "写坏了却报成功 —— 回读校验没生效"
        assert "不是有效 xlsx" in err or "打不开" in err, err
        assert tgt.read_bytes() == before, "坏产物替换了原文件"
        assert not list(Path(td).glob("*.lvtmp")), "失败后留了临时文件"
    print("  [ok] 坏产物被拒（%r），原文件完好" % err)


def test_sheet_count_mismatch_is_caught():
    """张数对不上也要抓 —— 那是"写了一半就停"的另一种形状。"""
    from skills.office_parser._excel_io import save_workbook
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tgt = Path(td) / "out.xlsx"
        _FakeWB(("Sheet1",)).save(str(tgt))
        before = tgt.read_bytes()
        for mode in ("fewer_sheets", "more_sheets"):
            err = save_workbook(_FakeWB(("A", "B"), mode=mode), tgt)
            assert "表数对不上" in err, "%s: %r" % (mode, err)
            assert tgt.read_bytes() == before
    print("  [ok] 表数与写出内容对不上 -> 拒（少一张 / 多一张都抓）")


def test_verify_does_not_load_the_workbook():
    """大表判据：`_verify_xlsx` 不许 `load_workbook`。

    那在大表上等于第二遍全量读（几十万行的表会被完整再读一遍）。
    校验必须是 O(1)：只读 zip 中央目录 + 一个小成员。
    负对照：把 `zipfile` 换成 `load_workbook`，本条立刻红。
    """
    src = EXCEL_IO.read_text(encoding="utf-8")
    body = src.split("def _verify_xlsx")[1].split("\ndef ")[0]
    assert "zipfile" in body, "`_verify_xlsx` 不是用 zipfile 读的？"
    assert "load_workbook" not in body, (
        "`_verify_xlsx` 里出现了 load_workbook —— 校验变成了第二遍全量读")
    print("  [ok] 校验走 zipfile（只读中央目录 + 一个小成员），不 load_workbook")


def test_no_bare_save_remains_in_excel_tools():
    """源级棘轮：`excel_tools.py` 里不许再出现裸 `wb.save(`。

    这条挡的是"顺手写回去" —— 裸 save 不会让任何别的测试红，
    而它正是"大表保存到一半失败就把原件写坏"的那个形状。
    """
    src = EXCEL_TOOLS.read_text(encoding="utf-8")
    assert "wb.save(" not in src, (
        "excel_tools.py 里还有裸 wb.save( —— 请改走 _excel_io.save_workbook")
    assert "save_workbook(" in src, "一处 save_workbook 都没有？那保存全没了"
    print("  [ok] excel_tools.py 里 0 处裸 wb.save（20 处全走 save_workbook）")


def test_a_transient_replace_failure_is_retried():
    """`os.replace` 的瞬时占用要重试。

    Windows 上目标被 AV / 索引器瞬时占用时，`os.replace` 会以
    `PermissionError [WinError 5]` 间歇性失败。这不是"偶发噪声"那么轻：
    `save_workbook` 是全部 20 处 Excel 写盘的收口 —— 这里不重试，
    用户的保存就会随机失败。

    负对照：把 `_replace_with_retry` 换回裸 `os.replace`，本条立刻红。
    """
    from skills.office_parser import _excel_io as M
    orig_replace = os.replace
    orig_retries, orig_backoff = M._REPLACE_RETRIES, M._REPLACE_BACKOFF_S
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:                      # 第一次被占用，第二次就好
            raise PermissionError(13, "被占用（模拟）")
        return orig_replace(src, dst)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tgt = Path(td) / "out.xlsx"
        _FakeWB(("Sheet1",)).save(str(tgt))
        try:
            M._REPLACE_RETRIES, M._REPLACE_BACKOFF_S = 3, 0.0   # 别让测试真的睡
            os.replace = flaky
            err = M.save_workbook(_FakeWB(("Sheet1", "Sheet2")), tgt)
        finally:
            os.replace = orig_replace
            M._REPLACE_RETRIES, M._REPLACE_BACKOFF_S = orig_retries, orig_backoff
        assert err == "", "瞬时占用重试之后应当成功：%r" % err
        assert calls["n"] == 2, "重试次数不对：%r" % calls["n"]
        assert not list(Path(td).glob("*.lvtmp")), "成功之后留了临时文件"
        with zipfile.ZipFile(tgt) as z:
            assert "xl/workbook.xml" in z.namelist()
    print("  [ok] os.replace 瞬时占用 -> 重试成功（试了 2 次）")


def test_a_persistent_replace_failure_reports_and_leaves_no_temp():
    """一直占用，回错误串（不是抛、也不是静默），且不留临时文件、原文件不动。

    方向不许放宽成"重试到成功为止" —— 那会在真写不进去时永远转下去。
    回错误串而不是抛，是本函数的既有契约（docstring 逐字：
    *"调用方拿它当 `{"success": False, "error": ...}` 用"*）—— 20 处调用点全是
    `if _save_err: return {"success": False, ...}` 那种接法。
    """
    from skills.office_parser import _excel_io as M
    orig_replace = os.replace
    orig_retries, orig_backoff = M._REPLACE_RETRIES, M._REPLACE_BACKOFF_S

    def always(src, dst):
        raise PermissionError(13, "被占用（模拟）")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tgt = Path(td) / "out.xlsx"
        _FakeWB(("Sheet1",)).save(str(tgt))
        before = tgt.read_bytes()
        try:
            M._REPLACE_RETRIES, M._REPLACE_BACKOFF_S = 2, 0.0
            os.replace = always
            err = M.save_workbook(_FakeWB(("Sheet1",)), tgt)
        finally:
            os.replace = orig_replace
            M._REPLACE_RETRIES, M._REPLACE_BACKOFF_S = orig_retries, orig_backoff
        assert err, "一直占用却报成功"
        assert "换名失败" in err, err
        assert before == tgt.read_bytes(), "换名失败却动了原文件"
        assert not list(Path(td).glob("*.lvtmp")), "失败后留了临时文件"
    print("  [ok] os.replace 一直占用 -> 报错串、原文件没动、不留临时文件")


# ------------------------------------------------------------------

def main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            failed += 1
            print("  FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))
    print("")
    print("%d tests: %d passed, %d failed" % (len(tests), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
