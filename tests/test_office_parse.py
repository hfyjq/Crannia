"""
Tests for office_parser extract_document_structure.

Usage:
    cd <仓库根>
    python tests/test_office_parse.py
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
)

#: 短别名 —— 正文里读起来短一点。
sandbox = _sandbox_ctx


def _setup_security():
    """Initialize a minimal SecurityGuard for testing."""
    from core.security import SecurityGuard
    from skills.office_parser.handler import init
    guard = SecurityGuard(
        allowed_dirs=["./workspace"],
        command_blacklist=[],
        audit_log_path=_tmp_audit_log(),
    )
    init(guard)
    return guard


# ------------------------------------------------------------------
# extract_document_structure — docx
# ------------------------------------------------------------------

def _make_docx(name: str, headings=()) -> None:
    """造一份 docx（不依赖生产工作区里手放的夹具）。

    夹具由用例自己造，并且进 `sandbox()`（不往生产目录写）。
    依赖手放夹具的写法会在清理后变成常年红，而"夹具没了"看起来像环境问题。
    """
    from docx import Document
    p = Path("workspace") / name
    p.parent.mkdir(parents=True, exist_ok=True)
    d = Document()
    for level, text in headings:
        d.add_heading(text, level=level)
        d.add_paragraph("正文")
    d.save(str(p))


def test_structure_docx():
    """Extract heading structure from a Word document."""
    with sandbox():
        _setup_security()
        from skills.office_parser.handler import extract_document_structure

        _make_docx("test.docx")           # 无标题的文档：空结构也是合法的
        result = extract_document_structure("workspace/test.docx")
        assert result["success"], f"Expected success, got: {result}"
        assert result["doc_type"] == "docx"
        assert "structure" in result
        assert "item_count" in result
        # Structure list may be empty if doc has no headings — that's valid
        assert isinstance(result["structure"], list)


def test_structure_docx_report():
    """Extract structure from a docx with likely headings."""
    with sandbox():
        _setup_security()
        from skills.office_parser.handler import extract_document_structure

        _make_docx("test_report.docx",
                   [(1, "一、概况"), (2, "1.1 背景"), (1, "二、结论")])
        result = extract_document_structure("workspace/test_report.docx")
        assert result["success"], result
        assert result["doc_type"] == "docx"
        assert isinstance(result["item_count"], int)
        assert result["item_count"] >= 3, f"三个标题应当都被抽到: {result}"


# ------------------------------------------------------------------
# extract_document_structure — pptx
# ------------------------------------------------------------------

def test_structure_pptx():
    """Extract slide structure from a PowerPoint presentation."""
    with sandbox():
        _setup_security()
        from pptx import Presentation
        from skills.office_parser.handler import extract_document_structure

        prs = Presentation()
        for title in ("封面", "结论"):
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = title
        Path("workspace").mkdir(parents=True, exist_ok=True)
        prs.save("workspace/test_slides.pptx")

        result = extract_document_structure("workspace/test_slides.pptx")
        assert result["success"], f"Expected success, got: {result}"
        assert result["doc_type"] == "pptx"
        assert result["item_count"] > 0, "Should have at least one slide"
        assert isinstance(result["structure"], list)

        # Each slide entry should have required keys
        for slide in result["structure"]:
            assert "slide_number" in slide
            assert "title" in slide
            assert "layout" in slide
            assert "preview" in slide


# ------------------------------------------------------------------
# extract_document_structure — error cases
# ------------------------------------------------------------------

def test_structure_unsupported_type():
    """extract_document_structure returns error for unsupported file type."""
    with sandbox():
        _setup_security()
        from skills.office_parser.handler import extract_document_structure

        # 夹具要自己造：指向一个不存在的文件时，`.txt` 会先走到
        # "Unsupported" 那一支而仍然绿；加上存在性检查（见
        # `extract_document_structure`）后，「不存在」会先打中。
        # 那样测的就不是"不支持的类型"，而是"绿了但没测到"。
        Path("workspace").mkdir(parents=True, exist_ok=True)
        Path("workspace/te1.txt").write_text("plain text\n", encoding="utf-8")

        result = extract_document_structure("workspace/te1.txt")
        assert not result["success"]
        assert "Unsupported" in result["error"], result


def test_structure_nonexistent():
    """extract_document_structure returns error for nonexistent file."""
    _setup_security()
    from skills.office_parser.handler import extract_document_structure

    result = extract_document_structure("nonexistent.docx")
    assert not result["success"]


# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        ("test_structure_docx", test_structure_docx),
        ("test_structure_docx_report", test_structure_docx_report),
        ("test_structure_pptx", test_structure_pptx),
        ("test_structure_unsupported_type", test_structure_unsupported_type),
        ("test_structure_nonexistent", test_structure_nonexistent),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")

    print(f"\n{len(tests)} tests: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
