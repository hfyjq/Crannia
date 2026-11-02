"""Office 产物的「整块覆盖」闸。

钉住什么：`core/file_tools.overwrite_guard` 的那条判据，被挂到 5 个
"整块覆盖"类的 office 工具上之后，行为对不对。

判据：

    | 磁盘上   | 本会话读过它吗 | 读过之后它变了吗 | 结果 |
    |---------|--------------|----------------|------|
    | 不存在   | —            | —              | 放行（新建） |
    | 存在     | 没读过        | —              | 拒 +「先读一遍」 |
    | 存在     | 读过          | 变了            | 拒 +「重读一遍」 |
    | 存在     | 读过          | 没变            | 放行（覆盖） |

每条都是行为断言（调真工具、看它真返回什么），不是"某个函数被调过"。

用法：
    export PYTHONIOENCODING=utf-8
    .venv/Scripts/python.exe tests/test_office_overwrite_guard.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sandbox import (  # noqa: E402
    sandbox as _sandbox_ctx,
    tmp_audit_log as _tmp_audit_log,
    prod_snapshot as _prod_snapshot,
    prod_diff as _prod_diff,
)

sandbox = _sandbox_ctx


def _setup_security():
    """注入一个最小的安全核（与其它 office 测试同一形状）。"""
    from core.security import SecurityGuard
    from skills.office_parser.handler import init
    guard = SecurityGuard(
        allowed_dirs=["./workspace"],
        command_blacklist=[],
        audit_log_path=_tmp_audit_log(),
    )
    init(guard)
    return guard


def _reset_ledger():
    """清掉读态账本 —— 它按会话键（本进程里是空串）存在模块级 dict 里。

    不清的话，用例之间会互相喂"熟脸"：前一条读过的路径，后一条就
       不再是"陌生目标"了。（这正是 `_reads` 的语义，不是 bug。）
    """
    from core import file_tools as FT
    FT.forget_reads(FT._sid())


def _make_docx(name: str, text: str = "正文") -> None:
    """就地造一份 docx（不依赖生产工作区里手放的夹具）。"""
    from docx import Document
    p = Path("workspace") / name
    p.parent.mkdir(parents=True, exist_ok=True)
    d = Document()
    d.add_paragraph(text)
    d.save(str(p))


def _make_xlsx(name: str, rows=(("a", 1),)) -> None:
    from openpyxl import Workbook
    p = Path("workspace") / name
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for r in rows:
        ws.append(list(r))
    wb.save(str(p))


# ------------------------------------------------------------------
# 1. 陌生目标 -> 拒（这条是整批存在的理由）
# ------------------------------------------------------------------

def test_a_stranger_target_is_refused():
    """目标已存在、本会话没读过它，`merge_documents` 必须拒，且带路线。"""
    from skills.office_parser import word_tools as W
    _setup_security()
    _reset_ledger()
    _make_docx("src1.docx")
    _make_docx("target1.docx", "这是另一个会话的成果")

    out = W.merge_documents("target1.docx", ["src1.docx"])

    assert out.get("success") is False, "陌生目标竟然放行了: %r" % (out,)
    assert "target1.docx" in out.get("error", ""), \
        "拒绝理由里没点出是哪个文件: %r" % (out,)
    assert "还没读过" in out.get("error", ""), \
        "拒绝理由没有说清是「没读过」这一支: %r" % (out,)
    hint = out.get("hint", "")
    assert "get_document_text" in hint, \
 "：路线必须兑现得了 —— hint 要给一个**真存在**的读工具: %r" % (out,)


# ------------------------------------------------------------------
# 2. 读过且没变 -> 放行（负对照：少了它，"拒"与"永远拒"分不开）
# ------------------------------------------------------------------

def test_a_read_unchanged_target_is_allowed():
    """先读过、此后没变，放行，且内容真的被替换了。"""
    from skills.office_parser import word_tools as W
    _setup_security()
    _reset_ledger()
    _make_docx("src2.docx", "合并进来的正文")
    _make_docx("target2.docx", "旧内容")

    read = W.get_document_text("target2.docx")
    assert read.get("success") is True, "前置条件没成立（读都没读成）: %r" % (read,)

    out = W.merge_documents("target2.docx", ["src2.docx"])
    assert out.get("success") is True, "读过且没变的目标竟然被拒了: %r" % (out,)

    after = W.get_document_text("target2.docx")
    assert "合并进来的正文" in after.get("text", ""), \
        "放行了但内容没真的写进去（那这条测的是另一件事）: %r" % (after,)


# ------------------------------------------------------------------
# 3. 读过之后被改过 -> 拒
# ------------------------------------------------------------------

def test_a_target_changed_after_the_read_is_refused():
    """读过 → 之后被改（= 另一个对话写了它），拒，且理由是「被改过」。"""
    from skills.office_parser import word_tools as W
    _setup_security()
    _reset_ledger()
    _make_docx("src3.docx")
    _make_docx("target3.docx", "第一版")

    W.get_document_text("target3.docx")
    _make_docx("target3.docx", "别人改过的一版")       # 外部改动

    out = W.merge_documents("target3.docx", ["src3.docx"])

    assert out.get("success") is False, "被改过的目标竟然放行了: %r" % (out,)
    assert "被改过" in out.get("error", ""), \
        "拒绝理由没有说清是「读过之后被改过」这一支: %r" % (out,)


# ------------------------------------------------------------------
# 4. 目标不存在 -> 放行（负对照）
# ------------------------------------------------------------------

def test_a_missing_target_is_created():
    """目标不存在，不要求先读，直接建（这条守住"新建不受影响"）。"""
    from skills.office_parser import word_tools as W
    _setup_security()
    _reset_ledger()
    _make_docx("src4.docx", "内容")
    assert not (Path("workspace") / "brand_new4.docx").exists()

    out = W.merge_documents("brand_new4.docx", ["src4.docx"])

    assert out.get("success") is True, "目标不存在竟然也被拒了: %r" % (out,)
    assert (Path("workspace") / "brand_new4.docx").exists(), \
        "放行了但文件没建出来"


# ------------------------------------------------------------------
# 5. 写成功之后它就是「熟脸」了（连写两次不该被误伤）
# ------------------------------------------------------------------

def test_a_target_we_just_wrote_is_a_familiar_face():
    """同一目标连写两次：第二次不该被当成"没读过的新面孔"。

    这是"写成功后回记"的唯一行为读者 —— 少了它，模型每写一次
      都得先读一次，而那是纯粹的税。
    """
    from skills.office_parser import word_tools as W
    _setup_security()
    _reset_ledger()
    _make_docx("src5.docx", "A")
    _make_docx("src5b.docx", "B")

    first = W.merge_documents("target5.docx", ["src5.docx"])
    assert first.get("success") is True, "第一次写就失败了: %r" % (first,)

    second = W.merge_documents("target5.docx", ["src5b.docx"])
    assert second.get("success") is True, \
        "刚写过的目标被当成陌生面孔了（纪律② 没生效）: %r" % (second,)


# ------------------------------------------------------------------
# 6. Excel 侧同形
# ------------------------------------------------------------------

def test_excel_merge_refuses_a_stranger_output():
    """`merge_excel_workbooks` 只查源、从不查输出 —— 输出是陌生面孔时必须拒。"""
    from skills.office_parser import excel_transform as X
    _setup_security()
    _reset_ledger()
    _make_xlsx("s6.xlsx")
    _make_xlsx("out6.xlsx", (("别人的", 1),))

    out = X.merge_excel_workbooks(["s6.xlsx"], output_name="out6.xlsx")

    assert out.get("success") is False, "陌生的输出目标竟然放行了: %r" % (out,)
    assert "out6.xlsx" in out.get("error", ""), \
        "拒绝理由里没点出是哪个文件: %r" % (out,)
    assert "read_excel_data" in out.get("hint", ""), \
 "：hint 要给一个**真存在**的 Excel 读工具: %r" % (out,)


def test_excel_write_data_refuses_a_stranger_target():
    """`write_excel_data`（模型给数据、整块盖）同形；且拒绝时一个字节都没写。"""
    from skills.office_parser import excel_tools as E
    _setup_security()
    _reset_ledger()
    _make_xlsx("t6.xlsx", (("原本就在这儿的", 42),))
    before = (Path("workspace") / "t6.xlsx").read_bytes()

    out = E.write_excel_data("t6.xlsx", "Sheet1", [["盖上去的", 9]])

    assert out.get("success") is False, "陌生目标竟然放行了: %r" % (out,)
    after = (Path("workspace") / "t6.xlsx").read_bytes()
    assert before == after, "拒了但文件被改了（拒绝必须是原子的）"


def test_excel_write_data_allows_after_reading():
    """Excel 侧的正向：读过之后再写，放行（负对照，同第 2 条）。"""
    from skills.office_parser import excel_tools as E
    _setup_security()
    _reset_ledger()
    _make_xlsx("t7.xlsx", (("旧的", 1),))

    read = E.read_excel_data("t7.xlsx", "Sheet1")
    assert read.get("success") is True, "前置条件没成立: %r" % (read,)

    out = E.write_excel_data("t7.xlsx", "Sheet1", [["新的", 2]])
    assert out.get("success") is True, "读过且没变的目标竟然被拒了: %r" % (out,)


# ------------------------------------------------------------------
# runner（本仓没有 pytest，每个文件自带）
# ------------------------------------------------------------------

def main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    passed = failed = 0
    before = _prod_snapshot()
    with sandbox():
        for name, fn in tests:
            try:
                fn()
                passed += 1
                print("  PASS  " + name)
            except AssertionError as exc:
                failed += 1
                print("  FAIL  " + name + ": " + str(exc))
            except Exception as exc:                          # noqa: BLE001
                failed += 1
                print("  ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))
    leaked = _prod_diff(before, _prod_snapshot())
    if leaked:
        failed += 1
        print("  FAIL  production-data-pollution: " + str(leaked))
    print("")
    print("%d tests: %d passed, %d failed" % (len(tests), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
