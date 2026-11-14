# -*- coding: utf-8 -*-
"""PDF 交付栈的机械读者：许可边界与功能可用性。

被钉住的三件事：

  1. 代码里不许出现 `import fitz` / `pymupdf`（扫描 `core/` `skills/`
     `bus/` `web/` `providers/` `channels/` —— 按 AST，不 grep 注释）；
  2. 依赖清单里不许出现 `PyMuPDF` / `pdf2docx`（`requirements.txt`
     与 `pyproject.toml` 两份都要看）；
  3. `parse_pdf` 与 `convert_from_pdf` 在当前 PDF 栈上真的能跑
     （合成 PDF，不依赖 `workspace/` 里那份未跟踪的大文件）。

外加一条 `raw_char_text` 的往返：pdfminer 对坏 Type0 输出 `(cid:N)`，
必须还原成 `chr(N)` 才能进文本偏移修复的判定（实测 `(cid:14062)` = `0x36EE`）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_pdf_stack.py
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _sandbox import sandbox, tmp_audit_log  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


# 1. 源级：AGPL 依赖不许回来
def test_no_agpl_pdf_imports():
    roots = ["core", "skills", "bus", "web", "providers", "channels", "mcpx"]
    hits: list = []
    for root in roots:
        for p in (_ROOT / root).rglob("*.py"):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except Exception:                          # noqa: BLE001
                continue
            for n in ast.walk(tree):
                names = []
                if isinstance(n, ast.Import):
                    names = [a.name for a in n.names]
                elif isinstance(n, ast.ImportFrom) and n.module:
                    names = [n.module]
                for name in names:
                    if name.split(".")[0].lower() in ("fitz", "pymupdf"):
                        hits.append("%s:%d" % (p.relative_to(_ROOT), n.lineno))
    check("no import fitz / pymupdf in shipped code", not hits, hits[:10])


def test_manifests_do_not_list_agpl_pdf_deps():
    for name in ("requirements.txt", "pyproject.toml"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        low = text.lower()
        check("%s: no PyMuPDF" % name, "pymupdf" not in low)
        check("%s: no pdf2docx" % name, "pdf2docx" not in low)


def test_new_stack_is_declared():
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for pkg in ("pypdf", "pypdfium2", "pdfplumber", "docvortex", "reportlab"):
        check("requirements: %s" % pkg, pkg in text)


# 2. CID 还原（pdfminer 输出 → 码点）
def test_raw_cid_becomes_codepoint():
    from core.pdf_text_repair import raw_char_text
    check("(cid:14062) -> chr(14062)",
          raw_char_text("(cid:14062)") == chr(14062),
          repr(raw_char_text("(cid:14062)")))
    check("plain text untouched", raw_char_text("中文") == "中文")
    check("bad cid untouched", raw_char_text("(cid:notanumber)") == "(cid:notanumber)")


# 3. 功能：parse_pdf + convert_from_pdf
def _mk_text_pdf(path: Path, pages: int = 2) -> None:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i in range(1, pages + 1):
        c.setFont("Helvetica", 12)
        c.drawString(72, 720, "Crannia PDF page %d" % i)
        c.showPage()
    c.save()


def _init_skills():
    from core.security import SecurityGuard
    from skills.file_parser import handler as FP
    from skills.office_parser import handler as OP
    g = SecurityGuard(allowed_dirs=["./workspace"], command_blacklist=[],
                      audit_log_path=tmp_audit_log())
    FP.init(g)
    OP.init(g)
    return FP, OP


def test_parse_pdf_on_new_stack():
    with sandbox():
        Path("workspace").mkdir(exist_ok=True)
        FP, _ = _init_skills()
        p = Path("workspace/stack.pdf")
        _mk_text_pdf(p, 2)
        chunks = FP.parse_pdf(str(p))
        text = "".join(c["text"] for c in chunks)
        check("parse_pdf returns chunks", bool(chunks))
        check("both pages present", {c["page"] for c in chunks} == {1, 2},
              sorted({c["page"] for c in chunks}))
        check("text survives", "Crannia PDF page 1" in text and
              "Crannia PDF page 2" in text, text[:120])


def test_convert_from_pdf_uses_docvortex():
    with sandbox():
        Path("workspace").mkdir(exist_ok=True)
        _, OP = _init_skills()
        p = Path("workspace/stack.pdf")
        _mk_text_pdf(p, 3)          # 多页：docvortex 会把文字放进 textbox 层
        r = OP.convert_from_pdf(str(p), "word")
        check("convert_from_pdf succeeds", r.get("success") is True, r)
        out = Path(r.get("output_path") or "")
        check("docx exists", out.is_file() and out.stat().st_size > 0, out)
        if out.is_file():
            import re
            import zipfile
            with zipfile.ZipFile(str(out)) as zf:
                xml = zf.read("word/document.xml").decode("utf-8", "ignore")
            text = re.sub(r"<[^>]+>", "", xml)
            check("docx has all pages' text",
                  "Crannia PDF page 1" in text and "Crannia PDF page 3" in text,
                  text[:160])


def test_convert_from_pdf_scanned_has_a_truthful_error():
    with sandbox():
        Path("workspace").mkdir(exist_ok=True)
        _, OP = _init_skills()
        p = Path("workspace/blank.pdf")
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(p))
        c.showPage()                                  # 空白页：没有文本层
        c.save()
        r = OP.convert_from_pdf(str(p), "word")
        check("blank PDF is not reported as success",
              r.get("success") is False, r)
        check("error explains the missing text layer",
              "文本层" in str(r.get("error") or ""), r)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print("\n-- %s --" % t.__name__)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s did not raise" % t.__name__, False, str(exc))
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
