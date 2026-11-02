"""
Office document parser skill handlers: parse_docx, parse_pptx, parse_xlsx.

Hard constraints enforced here:
  1. Streaming: paragraph/slide/row iterators — no bulk reads.
  2. Security: validate_path() BEFORE any file is opened.
  3. Chunking: all text flows through core.chunker (≤1500 chars, metadata).

Dependencies:
  - python-docx  (Word)
  - python-pptx  (PowerPoint)
  - openpyxl     (Excel — read_only mode)
"""

import os
import re
import shutil
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

from core.chunker import chunk_stream
from core.security import SecurityGuard, WriteNeedsApproval
from core.workspace import find_in_roots, lookup_roots, primary_output_dir

#: 产物的版式规格。模块级导入（不是函数内）——
#: 函数内 import 若解析到旧副本，失败是静默的；装载期失败才会立刻暴露。
from . import layout_spec as _spec

# Injected by skill_loader at startup
_security: SecurityGuard | None = None


def init(security: SecurityGuard) -> None:
    """Called by skill_loader after module import.

    必须把安全核转调给 `word_tools`。
    `skill_loader` 只认识 handler 这一个模块，子模块它不认识
不转调的话 `word_tools` 拿不到 `SecurityGuard`，而它自己有
    `doc.save(...)` —— 与硬约束「`validate_path()` 在 `open()` 之前」不一致。

    漏这一行，`word_tools._security` 恒为 None，每个 Word 工具都 raise
    （fail-closed，一眼可辨）。机械读者：`tests/test_word_tools_guard.py`。
    """
    global _security
    _security = security
    from . import word_tools
    word_tools.init(security)
    # Excel 同样必须转调：`excel_tools` 的写工具依赖安全核，
    # 不转调时 `_resolve_xlsx_path` 对绝对路径原样放行，
    # `write_excel_data("/任意位置/x.xlsx", …)` 能写出去（违反硬约束
    # 「`validate_path()` 在 `open()` 之前」）。漏这一行的症状与 Word 那边
    # 相同：所有 Excel 工具全部 raise（fail-closed）。
    # 机械读者：`tests/test_office_guard.py`。
    from . import excel_tools
    excel_tools.init(security)
    # PPT 那一批没有自己的一套解析 —— `ppt_tools._resolve_path`
    # 转调 `word_tools` 那一份（见它的 docstring）。
    #这一行的 `init` 是占位：`skill_loader` 按子模块挨个调，
    # 少一个会 AttributeError。安全校验靠的是上面 `word_tools.init`。
    from . import ppt_tools
    ppt_tools.init(security)


# Excel manipulation tools (from excel_tools.py)

from .excel_tools import (  # noqa: E402, F401 — re-export for bus discovery
    get_workbook_metadata,
    read_excel_data,
    write_excel_data,
    apply_formula,
    validate_formula_syntax,
    format_excel_range,
    create_excel_chart,
    create_excel_pivot,
    create_excel_table,
    copy_worksheet,
    delete_worksheet,
    rename_worksheet,
    merge_excel_cells,
    unmerge_excel_cells,
    get_merged_cells,
    copy_excel_range,
    delete_excel_range,
    insert_excel_rows,
    insert_excel_columns,
    delete_excel_rows,
    delete_excel_columns,
    # 新工具必须加进这张显式名单 —— 它是 bus discovery 的
    # 入口（`getattr(handler_module, name)`），漏了的话工具在 manifest 里声明着、
    # 而 `get_tool_handler` 找不到，模型搜得到、一调就 `AttributeError`。
    # `test_skill_doctor` 按 manifest 反查 handler（不是读代码），会报
    # `handler_tool_not_found`。
    freeze_excel_panes,
    set_excel_conditional_format,
    add_excel_data_validation,
    # 打印设置 + 分级显示 —— 显式名单 = bus discovery 的入口
    set_excel_print_setup,
    set_excel_outline,
)

# Excel 读侧查询三件（from excel_query.py）
# 面向真实经营表设计；用法与边界见 `excel_query.py` 的模块 docstring

from .excel_query import (  # noqa: E402, F401 — re-export for bus discovery
    profile_excel_data,      # 每列画像（流式 / 唯一值封顶 / 预算）
    find_excel_value,        # 按值定位
    get_excel_comments,      # Excel 批注（Word 的批注工具管不到）
)

# Excel 写侧九件（from excel_transform.py）
# 含出图工具 `plot_excel_data`（支持跨表）

from .excel_transform import (  # noqa: E402, F401 — re-export for bus discovery
    sort_excel_range,
    filter_excel_data,
    remove_excel_duplicates,        # destructive: true（丢的行回不来）
    protect_excel_sheet,
    set_excel_hyperlink,
    export_excel_to_csv,
    merge_excel_workbooks,
    recalculate_excel_formulas,     # destructive: true（LibreOffice 往返可能丢图表）
    plot_excel_data,                # 出图（支持跨表）
)

# Word manipulation tools (from word_tools.py)

from .word_tools import (  # noqa: E402, F401 — re-export for bus discovery
    get_document_info,
    get_document_text,
    get_document_outline,
    list_available_documents,
    copy_document,
    merge_documents,
    add_word_heading,
    add_word_paragraph,
    apply_table_of_contents,
    delete_paragraph,
    search_and_replace_text,
    insert_content_near_text,
    format_paragraph_text,
    create_custom_style,
    format_word_table,
    set_table_cell_shading,
    apply_table_alternating_rows,
    highlight_table_header,
    merge_table_cells,
    merge_table_cells_horizontal,
    merge_table_cells_vertical,
    set_table_cell_alignment,
    set_table_alignment_all,
    set_table_column_width,
    set_table_width,
    auto_fit_table,
    format_table_cell_text,
    set_table_cell_padding,
    get_all_comments,
    get_comments_by_author,
    get_comments_for_paragraph,
    add_footnote,
    # 页面设置 / 分节 / 页眉页脚 / 文档属性
    set_word_page_setup,
    add_word_section,
    set_word_header_footer,
    set_document_properties,
    # 题注 + 交叉引用 —— 手写 SEQ/REF 域
    #   （`python-docx` 没有 field API；形状与"引擎认不认"的验法见 `word_tools.py`）
    add_word_caption,
    add_word_cross_reference,
    # 题注的剩余两件 ——
    #   ① 「把已有的手写编号替换成题注」
    #   ② 「图目录 / 表目录」（`TOC \c` 域，`apply_table_of_contents` 会写它）
    replace_manual_captions,
    # 表格行列增删四件（同上：显式名单 = bus discovery 的入口）
    add_table_rows,
    add_table_columns,
    delete_table_rows,
    delete_table_columns,
)


# PowerPoint 后处理工具
#
# 它们不生成幻灯片（那走 `create_svg_slide` / `svgs_to_pptx`），
#   只改已经存在的 `.pptx` —— 见 `ppt_tools.py` 的模块 docstring。
from .ppt_tools import (  # noqa: E402, F401 — re-export for bus discovery
    # 读侧两件 —— 修改 / 重排已有 .pptx 的入口
    extract_pptx_identity,
    pptx_to_outline,
    set_ppt_background,
    set_ppt_background_graphics,
    crop_ppt_picture,
    add_ppt_audio,
    set_ppt_master_text_style,
)


# ------------------------------------------------------------------
# Word (.docx) — paragraph iterator
# ------------------------------------------------------------------

def parse_docx(file_path: str) -> List[Dict]:
    """
    Stream-parse a Word (.docx) document.

    - Uses python-docx: doc.paragraphs iterator (lazy, no bulk load).
    - Tables are extracted and interleaved at their position.
    - Every 50 paragraphs = 1 pseudo-page → fed to chunk_stream().

    Args:
        file_path: Path to the .docx file.

    Returns:
        List of chunk dicts: {"page", "chunk_index", "text"}.
    """
    _require_security()
    resolved_path = _resolve_path(file_path)
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]
    _note_observed(resolved)   # 记录读态（绕过两个 _guarded 收口）

    try:
        from docx import Document
    except ImportError:
        raise ImportError(
            "python-docx is required for Word documents. "
            "Install it with: pip install python-docx"
        )

    def paragraph_iterator() -> Iterator[Tuple[int, str]]:
        doc = Document(str(resolved))
        page_num = 1
        buffer: List[str] = []
        para_count = 0

        # Iterate through the document body in order, interleaving
        # paragraphs and tables.
        # python-docx stores paragraphs and tables separately in
        # doc.paragraphs and doc.tables, but we can use iter_inner_content
        # on the body element to walk them in document order.
        from docx.oxml.ns import qn

        body = doc.element.body
        para_idx = 0
        table_idx = 0

        for child in body:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

            if tag == "p":
                # Paragraph
                if para_idx < len(doc.paragraphs):
                    para = doc.paragraphs[para_idx]
                    text = para.text
                    if text.strip():
                        # Preserve heading hierarchy in chunk text
                        style_name = (para.style.name if para.style else "")
                        if style_name.startswith("Heading"):
                            try:
                                level = int(style_name.split()[-1])
                                text = "#" * level + " " + text
                            except (ValueError, IndexError):
                                pass
                        buffer.append(text)
                        para_count += 1
                    para_idx += 1
            elif tag == "tbl":
                # Table
                #
                # 带列号、并标出合并区。
                #
                # `row.cells` 按网格列返回，一个"合并 1-4"的格子会给出
                # 4 个一模一样的值。若只做 `" | ".join(cell.text ...)`，
                # 输出里没有列号、值还重复 4 次，模型只能数格子猜列号，
                # 而它必然猜错。
                #
                # 坐标一律 0 基，与 `format_table_cell_text` 的
                # `row_index` / `col_index` 同一套编号。打印 1 基、
                # 而写侧收 0 基 = 亲手造一个新的差一错。
                if table_idx < len(doc.tables):
                    from .word_tools import iter_grid_cells, cell_merge_range
                    table = doc.tables[table_idx]
                    table_lines = []
                    for r_i, grid_row in enumerate(iter_grid_cells(table)):
                        parts = []
                        for gc in grid_row:
                            text = gc["cell"].text.strip()
                            mark = ""
                            r0, r1, c0, c1 = cell_merge_range(
                                table, r_i, gc["col"])
                            # 四元组的顺序是 (r0, r1, c0, c1) —— 没合并时
                            # 必须等于 (r, r, c, c)，不是 (r, c, r, c)。
                            # 写反的话每个普通格子都会被标成 "[合并 c5-c5]"，
                            # 把该看的信号淹掉。
                            if (r0, r1, c0, c1) != (r_i, r_i,
                                                    gc["col"], gc["col"]):
                                if r0 == r1:
                                    mark = f"[合并 c{c0}-c{c1}]"
                                elif c0 == c1:
                                    mark = f"[合并 r{r0}-r{r1}]"
                                else:
                                    mark = f"[合并 r{r0}-r{r1}×c{c0}-c{c1}]"
                            parts.append(f'c{gc["col"]}={text}{mark}')
                        table_lines.append(f"r{r_i}: " + " | ".join(parts))
                    if table_lines:
                        buffer.append(
                            f"[表格 {table_idx + 1}] 坐标 0 基"
                            f"（与 format_table_cell_text 同一套）；"
                            f"[合并 ...] 标出合并区，整区共用一个值\n"
                            + "\n".join(table_lines)
                        )
                        para_count += 1
                    table_idx += 1

            if para_count >= 50:
                yield (page_num, "\n".join(buffer))
                page_num += 1
                buffer = []
                para_count = 0

        # Flush remaining
        if buffer:
            yield (page_num, "\n".join(buffer))

    return chunk_stream(paragraph_iterator())


# ------------------------------------------------------------------
# PowerPoint (.pptx) — slide iterator
# ------------------------------------------------------------------

def parse_pptx(file_path: str) -> List[Dict]:
    """
    Extract text from a PowerPoint (.pptx) presentation.

    - Uses python-pptx: prs.slides iterator.
    - Each slide = 1 page. Text from all shapes is collected.
    - Fed to chunk_stream() for enforced ≤1500-char chunks.

    Args:
        file_path: Path to the .pptx file.

    Returns:
        List of chunk dicts: {"page", "chunk_index", "text"}.
    """
    _require_security()
    resolved_path = _resolve_path(file_path)
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]
    _note_observed(resolved)   # 记录读态（绕过两个 _guarded 收口）

    try:
        from pptx import Presentation
    except ImportError:
        raise ImportError(
            "python-pptx is required for PowerPoint files. "
            "Install it with: pip install python-pptx"
        )

    def slide_iterator() -> Iterator[Tuple[int, str]]:
        prs = Presentation(str(resolved))
        for slide_num, slide in enumerate(prs.slides, start=1):
            texts: List[str] = []

            # Prepend layout info for semantic context
            if slide.slide_layout and slide.slide_layout.name:
                texts.append(f"[Layout: {slide.slide_layout.name}]")

            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        t = para.text.strip()
                        if t:
                            texts.append(t)
                if shape.has_table:
                    table = shape.table
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        texts.append(" | ".join(cells))
            yield (slide_num, "\n".join(texts))

    return chunk_stream(slide_iterator())


# ------------------------------------------------------------------
# Excel (.xlsx) — row iterator (streaming via read_only)
# ------------------------------------------------------------------

def parse_xlsx(file_path: str, sheet_name: str = "") -> List[Dict]:
    """
    Stream-read an Excel spreadsheet (.xlsx or .xls).

    - `.xlsx`: uses openpyxl with read_only=True for true streaming.
    - `.xls` (Excel 97–2003): uses xlrd for legacy binary format.
    - Rows are read one at a time.
    - Every 100 rows = 1 pseudo-page → fed to chunk_stream().

    Args:
        file_path: Path to the .xlsx or .xls file.
        sheet_name: Optional sheet name. Defaults to the first sheet.

    Returns:
        List of chunk dicts: {"page", "chunk_index", "text"}.
    """
    _require_security()
    resolved_path = _resolve_path(file_path)
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]
    _note_observed(resolved)   # 记录读态（绕过两个 _guarded 收口）

    ext = resolved.suffix.lower()

    # .xls (Excel 97–2003 binary) → xlrd
    if ext == ".xls":
        return _parse_xls_xlrd(resolved, sheet_name)

    # .xlsx / .xlsm (OOXML) → openpyxl
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ImportError(
            "openpyxl is required for .xlsx files. "
            "Install it with: pip install openpyxl"
        )

    def row_iterator() -> Iterator[Tuple[int, str]]:
        wb = load_workbook(str(resolved), read_only=True, data_only=True)

        # Determine which sheet to read
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                wb.close()
                raise ValueError(
                    f"Sheet '{sheet_name}' not found. "
                    f"Available sheets: {wb.sheetnames}"
                )
            ws = wb[sheet_name]
        else:
            ws = wb.active

        page_num = 1
        buffer: List[str] = []
        row_count = 0

        # iter_rows yields one row at a time — true streaming
        for row in ws.iter_rows(values_only=True):
            # Convert row tuple to tab-separated string
            row_str = "\t".join(
                str(cell) if cell is not None else "" for cell in row
            )
            if row_str.strip():  # skip entirely empty rows
                buffer.append(row_str)
                row_count += 1

            if row_count >= 100:
                yield (page_num, "\n".join(buffer))
                page_num += 1
                buffer = []
                row_count = 0

        # Flush remaining
        if buffer:
            yield (page_num, "\n".join(buffer))

        wb.close()

    return chunk_stream(row_iterator())


# ------------------------------------------------------------------
# Placeholder handlers for shared tools
# These are registered by file_parser; listed here only so
# skill_loader doesn't look for them in this module.
# ------------------------------------------------------------------

# search_chunks and summarize_document are NOT defined here —
# they are handled by agent_loop.py directly and registered via
# file_parser/manifest.yaml.


# ------------------------------------------------------------------
# Document structure extraction
# ------------------------------------------------------------------

def extract_document_structure(file_path: str) -> dict:
    """
    Extract document outline/structure from Word or PowerPoint files.

    Auto-detects format by file extension:
      - .docx → heading hierarchy (level, text, paragraph index)
      - .pptx → slide outline (slide number, title, layout, preview)

    Does NOT extract full text — use parse_docx / parse_pptx for that.

    Args:
        file_path: Path to the .docx or .pptx file.

    Returns:
        dict with success, doc_type, structure list, item_count.
    """
    _require_security()
    resolved_path = _resolve_path(file_path)

    try:
        resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]
        _note_observed(resolved)   # 记录读态（绕过两个 _guarded 收口）
    except Exception as e:
        return {"success": False, "error": f"Security check failed: {e}"}

    # 两道兜底：存在性检查 + 包住解析调用。
    #
    # 缺了它们时，一个不存在的文件会先过路径校验（路径本身合法），
    # 再由 python-docx 抛 `PackageNotFoundError`，异常直接穿到调用方。
    # 调用方契约要求这个函数返回错误对象，不抛异常。
    # 测试也要覆盖这一支：只测"安全校验失败"会让"文件不存在"看起来
    # 也被测到了，实际测的是另一件事。
    if not resolved.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    suffix = resolved.suffix.lower()
    try:
        if suffix == ".docx":
            return _extract_docx_structure(resolved)
        elif suffix == ".pptx":
            return _extract_pptx_structure(resolved)
        else:
            return {
                "success": False,
                "error": f"Unsupported file type '{suffix}'. "
                         f"Supports: .docx, .pptx",
            }
    except Exception as e:
        # 契约同 `excel_tools`：绝不把异常抛给调用方。
        # 损坏的 .docx / .pptx 走的就是这一支 —— 必须由工具兜住，不能抛给调用方。
        return {"success": False,
                "error": f"解析失败: {type(e).__name__}: {e}"}


def _extract_docx_structure(path: Path) -> dict:
    """Extract heading hierarchy from a Word document."""
    try:
        from docx import Document
    except ImportError:
        raise ImportError("python-docx required. Install: pip install python-docx")

    doc = Document(str(path))
    headings: list = []
    para_index = 0
    for para in doc.paragraphs:
        style_name = (para.style.name if para.style else "")
        if style_name.startswith("Heading"):
            try:
                level = int(style_name.split()[-1])
            except (ValueError, IndexError):
                level = 1
            text = para.text.strip()
            if text:
                headings.append({
                    "level": level,
                    "text": text[:200],  # truncate very long headings
                    "paragraph_index": para_index,
                })
        para_index += 1

    return {
        "success": True,
        "doc_type": "docx",
        "structure": headings,
        "item_count": len(headings),
    }


def _extract_pptx_structure(path: Path) -> dict:
    """Extract slide outline from a PowerPoint presentation."""
    try:
        from pptx import Presentation
    except ImportError:
        raise ImportError("python-pptx required. Install: pip install python-pptx")

    prs = Presentation(str(path))
    slides_outline: list = []
    for slide_num, slide in enumerate(prs.slides, start=1):
        title = ""
        layout_name = ""
        text_preview = ""

        if slide.slide_layout:
            layout_name = slide.slide_layout.name

        if slide.shapes.title:
            title = slide.shapes.title.text.strip()

        # Collect first ~150 chars of body text for preview
        preview_parts: list = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape != slide.shapes.title:
                for para in shape.text_frame.paragraphs:
                    t = para.text.strip()
                    if t:
                        preview_parts.append(t)
                        break  # one sentence per shape
                if len(" ".join(preview_parts)) > 150:
                    break
        text_preview = " ".join(preview_parts)[:150]

        slides_outline.append({
            "slide_number": slide_num,
            "title": title,
            "layout": layout_name,
            "preview": text_preview,
        })

    return {
        "success": True,
        "doc_type": "pptx",
        "structure": slides_outline,
        "item_count": len(slides_outline),
    }


# Archive extraction

def extract_archive(file_path: str) -> dict:
    """
    Extract a compressed archive (.zip, .7z, .rar) into the workspace.

    The archive is extracted to `workspace/<archive_name>/` so the
    contents stay organised and don't scatter into the workspace root.

    Only archives inside the workspace can be extracted (security
    constraint — cross-zone archives must be copied to workspace first
    via `copy_to_workspace`).

    Args:
        file_path: Path to the archive file.  Must be under workspace/.

    Returns:
        dict with `success`, `extracted_to`, `file_count`,
        `files` (top 50 names), and any `errors`.
    """
    import shutil
    import zipfile

    _require_security()
    resolved_path = _resolve_path(file_path)

    # Security: validate the archive is under a workspace
    try:
        resolved = _security.validate_path(str(resolved_path), "read")
    except WriteNeedsApproval:
        # 这一处今天是 `read`，抛不出它（审批只长在写路径上）——
        # 加这一支是防御性的：哪天有人把 op 改成 `write`，
        # `except Exception` 会把审批吞成一行错误文本，
        # 症状是"跨区写永远弹不出弹窗、只看到一串『安全限制:』"。
        raise
    except Exception as e:
        return {"success": False, "error": f"安全限制: {e}"}

    src = Path(str(resolved))
    if not src.exists():
        return {"success": False, "error": f"文件不存在: {file_path}"}

    ext = src.suffix.lower()
    if ext not in (".zip", ".7z", ".rar"):
        return {"success": False,
                "error": f"不支持的压缩格式: {ext}，仅支持 .zip / .7z / .rar"}

    # Determine output: workspace/<archive_stem>/
    stem = src.stem
    out_dir = primary_output_dir() / stem
    # If target already exists, append a suffix
    if out_dir.exists():
        import time as _time
        ts = _time.strftime("%Y%m%d_%H%M%S")
        out_dir = primary_output_dir() / f"{stem}_{ts}"

    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        if ext == ".zip":
            _extract_zip(src, out_dir)
        elif ext == ".7z":
            _extract_7z(src, out_dir)
        elif ext == ".rar":
            _extract_rar(src, out_dir)

        # Collect results
        all_files = []
        for root, dirs, files in os.walk(out_dir):
            for f in files:
                all_files.append(os.path.relpath(os.path.join(root, f), out_dir))
        all_files.sort()

        return {
            "success": True,
            "extracted_to": str(out_dir),
            "file_count": len(all_files),
            "files": all_files[:50],
            "hint": (
                f"已解压 {len(all_files)} 个文件到 {out_dir}。"
                + (f"（仅显示前50个）" if len(all_files) > 50 else "")
            ),
        }

    except Exception as e:
        # Clean up partial extraction
        if out_dir.exists():
            try:
                shutil.rmtree(out_dir)
            except Exception:
                pass
        return {"success": False,
                "error": f"解压失败: {type(e).__name__}: {e}"}


def _extract_zip(src: Path, out_dir: Path) -> None:
    """Extract a .zip archive with zip-slip protection."""
    import zipfile
    with zipfile.ZipFile(src, "r") as zf:
        for member in zf.infolist():
            # Zip-slip: prevent path traversal
            target = (out_dir / member.filename).resolve()
            if not str(target).startswith(str(out_dir.resolve())):
                raise ValueError(f"不安全的压缩包路径: {member.filename}")
        zf.extractall(out_dir)


def _extract_7z(src: Path, out_dir: Path) -> None:
    """Extract a .7z archive via py7zr (optional dependency)."""
    try:
        import py7zr
    except ImportError:
        raise ImportError(
            "需要安装 py7zr 库来解压 .7z 文件。请运行: pip install py7zr"
        )
    with py7zr.SevenZipFile(src, mode="r") as szf:
        szf.extractall(out_dir)


def _extract_rar(src: Path, out_dir: Path) -> None:
    """Extract a .rar archive via rarfile (optional dependency).

    RAR 需要一个外部工具（`unrar` / `bsdtar` / `7z` 之一）—— Python 侧
    没有纯实现，`rarfile` 只是去调其中一个。而这些工具我们一个都没打包
    （随包运行时里 `tool_setup()` 直接 `RarCannotExec`；开发机也一样，
    所以这不是"装出来才坏的"，是这条路从来就没通）。
找不到工具时给一句能兑现的路线，别把 `Cannot find working tool`
       原样丢给模型 —— 它看不出该干什么。
    """
    try:
        import rarfile
    except ImportError:
        raise ImportError(
            "需要安装 rarfile 库来解压 .rar 文件。请运行: pip install rarfile"
        )
    try:
        rarfile.tool_setup()
    except Exception:                                           # noqa: BLE001
        raise RuntimeError(
            "这台机器上没有能解 RAR 的外部工具（unrar / bsdtar / 7z 都没有）。"
            "RAR 解压需要它们之一，Python 库自己做不到。两条路："
            "(1) 用系统里的 WinRAR / 7-Zip 手动解开；"
            "(2) 让对方换 .zip 或 .7z 提供文件 —— 那两种我们自带能力，不需要外部工具。"
        ) from None
    with rarfile.RarFile(src) as rf:
        rf.extractall(out_dir)


# ------------------------------------------------------------------
# Legacy .xls support (xlrd)
# ------------------------------------------------------------------

def _parse_xls_xlrd(resolved: Path, sheet_name: str = "") -> List[Dict]:
    """Read a legacy .xls (Excel 97–2003) file via xlrd."""
    try:
        import xlrd
    except ImportError:
        raise ImportError(
            "xlrd is required for .xls (Excel 97–2003) files. "
            "Install it with: pip install xlrd"
        )

    wb = xlrd.open_workbook(str(resolved))

    # Determine sheet
    if sheet_name:
        if sheet_name not in wb.sheet_names():
            raise ValueError(
                f"Sheet '{sheet_name}' not found. "
                f"Available sheets: {wb.sheet_names()}"
            )
        sh = wb.sheet_by_name(sheet_name)
    else:
        sh = wb.sheet_by_index(0)

    def xls_row_iterator() -> Iterator[Tuple[int, str]]:
        page_num = 1
        buffer: List[str] = []
        row_count = 0
        for r in range(sh.nrows):
            row_vals = []
            for c in range(sh.ncols):
                cell = sh.cell_value(r, c)
                # Convert floats that are whole numbers to int strings
                if isinstance(cell, float) and cell == int(cell):
                    cell = int(cell)
                row_vals.append(str(cell))
            buffer.append("\t".join(row_vals))
            row_count += 1
            if row_count >= 100:
                yield (page_num, "\n".join(buffer))
                page_num += 1
                buffer = []
                row_count = 0
        if buffer:
            yield (page_num, "\n".join(buffer))

    return chunk_stream(xls_row_iterator())


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _note_observed(path) -> None:
    """产物侧的读态记账 —— 见 `core/file_tools.note_observed`。

    只服务 `parse_docx` / `parse_pptx` / `parse_xlsx` 这三条读路：它们
      **走 handler 自己的 `_split_path` + 直接调 `validate_path`**，
      不经 `word_tools._guarded` / `_excel_io._guarded` 那两个收口，
      所以得在这儿单独补一笔。
    """
    try:
        from core.file_tools import note_observed
        note_observed(path)
    except Exception as exc:                                  # noqa: BLE001
        print("[office] 读态记账失败（不影响读取）：%s: %s"
              % (type(exc).__name__, exc))


def _require_security() -> None:
    if _security is None:
        raise RuntimeError(
            "SecurityGuard not initialized. "
            "The skill_loader must call init() before any handler is invoked."
        )


def overwrite_blocked(path, *, reader: str = "get_document_text"):
    """整块覆盖一个可能已存在的文件之前的那一句问话。

    见 `core/file_tools.overwrite_guard` —— 判据是：
    不存在，放行；没读过，拒；读过但变了，拒；读过且没变，覆盖。
    返回 `None` = 放行；否则 `{"error", "hint"}`，调用方 `return {"success": False, **blk}`。

    异常一律放行并出声：这是检测不是边界 ——
      让它因为自身出错而拦住所有人的活，方向反了。
    """
    try:
        from core.file_tools import overwrite_guard
        return overwrite_guard(path, reader=reader)
    except Exception as exc:                                  # noqa: BLE001
        print("[office] 覆盖闸出错（已放行）：%s: %s" % (type(exc).__name__, exc))
        return None


def _resolve_path(file_path: str) -> Path:
    """Try file_path as-is; if not found, try under workspace/."""
    p = Path(file_path)
    if p.exists():
        return p.resolve()
    # 「查」—— 全部根都试
    alt = find_in_roots(p.name)
    if alt is None:
        alt = lookup_roots()[0] / p.name
    if alt.exists():
        return alt.resolve()
    return p.resolve()


# Write operations — document creation & editing

from skills.office_parser.session import DocSession, get_store


_RE_HEADING = re.compile(r'^(#{1,3})\s+(.*)$')
_RE_LIST = re.compile(r'^([-*+]|\d+[.)])\s+(.*)$')

#: Markdown 表格的分隔行 —— `| --- | :--: |`。
#:
#: 它是"这几行是表格"的唯一判据（`_parse_document_text` 只在前一行是
#: 表格行、且这一行是分隔行时才收表）。没有它就不猜，连续 `|` 行
#: 仍落成普通段落 —— fail-open 的方向：宁可不成表，不可乱吃文本。
_RE_TABLE_SEP = re.compile(r'^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$')


#: 文档的统一字体。
#:
#: 不指定字体时，`document.xml` 里 `rFonts` 一次都不出现 —— 正文全交给
#: Word 的主题兜底。而主题对 ASCII 与 东亚是两套字体，同一段里
#: 数字与中文用不同字面，看起来参差不齐。
#:
#: 中文必须单独设 `w:eastAsia` —— python-docx 的 `font.name = X`
#: 只写 `w:ascii`（顺带 `w:hAnsi`），中文不受它管。只设 `font.name`
#: 是最常见的"设了没用"（改的是 A，真正生效的是 B）。
#:三个属性一律显式给，而且是同一个字体（先把"一致"拿到手）。
_DOC_FONT = "微软雅黑"

#: 行内加粗：`这样`。非贪婪 + 不跨行（跨行的 `**` 多半不是加粗意图）。
_RE_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")


#: 模板里那四个主题字体引用的属性名。设直接字体名之前必须先把它们摘掉。
#:
#: 为什么要摘：
#: 默认模板给 `Heading 1..9` / `Title` / `Subtitle` 写的 rFonts 是主题引用
#: （`w:eastAsiaTheme="majorEastAsia"` 等），而 `_apply_doc_fonts` 只加直接字体名、
#: 没删主题属性，一条 rFonts 里两套并存：
#:
#:     <w:rFonts w:asciiTheme="majorHAnsi" w:eastAsiaTheme="majorEastAsia" ...
#:               w:ascii="微软雅黑" w:hAnsi="微软雅黑" w:eastAsia="微软雅黑"/>
#:
#: 这两组在 OOXML 里是互斥的（一个指主题、一个指字体名），同写一条 Word 取哪个
#: 不确定 —— 标题跟着主题走，而主题的东亚字体是空的
#: （`<a:majorFont><a:ea typeface=""/>`），标题的中文掉回 Word 默认东亚字体，
#: 与正文（微软雅黑）不一致。
#: `Normal` 本来没有主题属性，正文一直是对的 —— 所以这个问题只在标题上现形。
_THEME_FONT_ATTRS = ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme")


def _apply_doc_fonts(doc) -> None:
    """给整篇文档定一套中英都显式指定的字体。

    范围是所有段落样式 + 字符样式 + 表格样式——
    `Title` / `Subtitle` / `Heading 1-9` 同样带主题引用，漏了它们就是同一个问题
    换个地方再发生一次。
    字符样式那一半也别漏（`Heading 1 Char` / `Title Char` …）：它们同样带主题引用，
    而 run 一旦用 `w:rStyle` 引到它们，就绕过了段落样式这一层。同一个问题的第二个门。
    表格样式是第三个门（`Medium Grid 2` 那一族）—— 表格用了它们，格子里的中文同样掉。
    碰不到的那一半，如实标：表格样式里 `w:tblStylePr`（首行/斑马纹那些条件格式）
    自带一层 rPr，本函数不动它 —— 那是表格样式自己的格子级格式，
    动它容易把样式改坏。要覆盖得单独做，且要有对照。
    """
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn
    _WANT = (WD_STYLE_TYPE.PARAGRAPH, WD_STYLE_TYPE.CHARACTER, WD_STYLE_TYPE.TABLE)
    for st in doc.styles:
        if st.type not in _WANT:
            continue
        try:
            st.font.name = _DOC_FONT
            rpr = st.element.get_or_add_rPr()
            rf = rpr.get_or_add_rFonts()
            # ① 先摘主题引用（见 `_THEME_FONT_ATTRS` 那段）：不摘就是"两套并存"。
            for attr in _THEME_FONT_ATTRS:
                key = qn(attr)
                if rf.get(key) is not None:
                    del rf.attrib[key]
            # ② 再显式给四个属性 —— 中文由 `w:eastAsia` 管，ASCII 由 `w:ascii` 管。
            for attr, val in (("w:ascii", _DOC_FONT), ("w:hAnsi", _DOC_FONT),
                              ("w:eastAsia", _DOC_FONT), ("w:cs", _DOC_FONT)):
                rf.set(qn(attr), val)
        except Exception:                                  # noqa: BLE001
            continue


# 版式 —— 规格全在 `layout_spec.py`，这里只负责"落下去"

#: `w:pPr` 里排在 `w:pBdr` 之后的那一串（OOXML 的子元素有序 ——
#: 顺序错了 Word 会报"内容有问题"）。用法见 `_add_left_bar`。
#: 这张表就是"为什么不直接 append"：append 会排到 `w:rPr` 后面，那是坏的。
_PPR_AFTER_PBDR = (
    "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
    "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
    "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
    "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
    "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
)

#: `w:tblPr` 里排在 `w:tblBorders` 之后的。
_TBLPR_AFTER_BORDERS = ("w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
                        "w:tblCaption", "w:tblDescription")

#: `w:tcPr` 里排在 `w:shd` 之后的。
_TCPR_AFTER_SHD = ("w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText",
                   "w:vAlign", "w:hideMark")


def _set_style(doc, name: str, *, size=None, color=None, bold=None,
               line=None, before=None, after=None) -> None:
    """改一个已存在的段落样式；这版模板里没有就跳过（不 raise）。

    缺样式时 `doc.styles[name]` 抛 `KeyError`，而模板版本会换，逐个护住。
    """
    from docx.shared import Pt
    try:
        st = doc.styles[name]
    except KeyError:
        return
    f = st.font
    if size is not None:
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    if color is not None:
        f.color.rgb = color
    pf = st.paragraph_format
    if line is not None:
        pf.line_spacing = line
    if before is not None:
        pf.space_before = Pt(before)
    if after is not None:
        pf.space_after = Pt(after)
    if name == "Normal":
        pf.first_line_indent = Pt(0)


def _add_left_bar(doc, style_name: str) -> None:
    """给一个段落样式加左侧竖条（`w:pBdr/w:left`）。

    这是"设计过"识别度最高的一处：章节标题左边一条 4px 竖条，
    视线一到就知道"这里换了一层"。宽度由 `layout_spec.HEADING_BAR_SZ` 给。
    """
    from docx.oxml.ns import qn
    try:
        st = doc.styles[style_name]
    except KeyError:
        return
    # 与段落级的 `_box_style` 共用同一份 XML 写法（`_set_left_bar`）
    #   —— 两个答案看着都对，是本仓反复踩过的形状。
    _set_left_bar(st.element.get_or_add_pPr(), _spec.ACCENT, _spec.HEADING_BAR_SZ)


def _apply_layout(doc) -> None:
    """把 `layout_spec` 那份规格落到这份 docx 上。

    与 `_apply_doc_fonts` 的分工：那个只管字体（18 条读者钉着），
    这个管其余全部（页面 / 字号 / 颜色 / 间距 / 竖条）。各测各的。
    别在别处再写色值/字号 —— 规格只有一个入口：`layout_spec.py`。
    """
    from docx.shared import Cm, RGBColor

    _apply_doc_fonts(doc)                        # 字体那一半，原样保留

    for sec in doc.sections:                     # A4 + 中文边距
        sec.page_width = Cm(_spec.PAGE_WIDTH_CM)
        sec.page_height = Cm(_spec.PAGE_HEIGHT_CM)
        sec.top_margin = Cm(_spec.MARGIN_TOP_CM)
        sec.bottom_margin = Cm(_spec.MARGIN_BOTTOM_CM)
        sec.left_margin = Cm(_spec.MARGIN_LEFT_CM)
        sec.right_margin = Cm(_spec.MARGIN_RIGHT_CM)

    ink = RGBColor.from_string(_spec.INK)
    accent = RGBColor.from_string(_spec.ACCENT)
    muted = RGBColor.from_string(_spec.INK_MUTED)

    for name in ("Normal", "List Paragraph", "List Bullet", "List Number",
                 "Body Text"):
        _set_style(doc, name, size=_spec.BODY_PT, color=ink,
                   line=_spec.LINE_SPACING, after=_spec.PARA_SPACE_AFTER_PT)

    # 标题阶梯：字号 / 字重 / 颜色 / 间距四级一起给，不只动字号。
    for name, (pt, before, after, bar) in _spec.LEVELS.items():
        _set_style(doc, name, size=pt, color=accent, bold=True,
                   line=_spec.HEADING_LINE_SPACING, before=before, after=after)
        if bar and _spec.HEADING_BAR:
            _add_left_bar(doc, name)

    for name, (pt, before, after) in _spec.COVER_LEVELS.items():
        _set_style(doc, name, size=pt, color=accent, bold=True,
                   before=before, after=after)

    # `bold=False` 是必须的：模板里 `Caption` 默认是粗体（表注加粗很难看），
    #   而 `_set_style` 传 `None` 就"不动它"，不显式关掉就会带着模板的粗体。
    _set_style(doc, "Caption", size=_spec.CAPTION_PT, color=muted, bold=False)


def _add_para(doc, text: str, style: str = ""):
    """加一段 —— 把 `粗体` 真的渲染成粗体，而不是把记号留在正文里。

    记号既没渲染、也没剥掉时，正文会原样出现 `锁定管理费用`
    这种字（一份报告里 32 段如此，真加粗的 run = 0）。

    `_parse_document_text` 的 docstring 说"刻意不做行内格式" —— 那是不渲染，
    不等于可以把记号原样留在正文里。两者差着一个"能不能读"。
    """
    p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    body = text or ""
    pos = 0
    for m in _RE_INLINE_BOLD.finditer(body):
        if m.start() > pos:
            p.add_run(body[pos:m.start()])
        r = p.add_run(m.group(1))
        r.bold = True
        pos = m.end()
    if pos < len(body):
        p.add_run(body[pos:])
    return p


def _is_table_row(s: str) -> bool:
    """一行是不是 Markdown 表格行 —— 判据是「以 `|` 开头且至少两个 `|`」。

    不用"含 `|`"当判据：正文里写"甲|乙"是常见的中文写法，吃进来就是
    「检测可以错，但改动内容不能错」的反面。要求行首是 `|`，把误吃的面收窄。
    """
    return s.startswith("|") and s.count("|") >= 2


def _split_table_row(s: str) -> list:
    """`| a | b |` → `["a", "b"]`（去掉首尾那两个空串，逐格 `strip`）。"""
    s = s.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _looks_numeric(text: str) -> bool:
    """这一格是不是"数" —— 是的话右对齐（一列数右对齐才比得出大小）。

    判据收得窄：只认纯数、百分比、带千分位/正负号/货币符的。
    中文里「2025 年」「第一季度」「提升」都不是数 —— 拿去右对齐更难看。
    """
    s = (text or "").strip()
    for ch in (",", "，", "¥", "$", "€", "£", "%", "+", "-", "—", "～", "~", " "):
        s = s.replace(ch, "")
    if not s:
        return False
    try:
        float(s)
    except ValueError:
        return False
    return True


def _shade_cell(cell, hex_fill: str) -> None:
    """给一格铺底纹（`w:shd`）。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.insert_element_before(shd, *_TCPR_AFTER_SHD)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)


def _prettify_table(table) -> None:
    """表头底纹 + 发丝线框 + 数字列右对齐。空表 / 单行表也要能跑。"""
    from docx.enum.table import WD_ALIGN_VERTICAL
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    rows = list(table.rows)
    if not rows:
        return

    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.insert_element_before(borders, *_TBLPR_AFTER_BORDERS)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = borders.find(qn("w:" + edge))
        if el is None:
            el = OxmlElement("w:" + edge)
            borders.append(el)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")                    # 0.5pt 发丝线
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), _spec.HAIRLINE)

    # 判"哪几列是数字列"，表头与表体用同一个答案 —— 分开判，列会歪。
    body = rows[1:]
    ncols = len(table.columns)
    numeric_col = []
    for j in range(ncols):
        vals = [r.cells[j].text.strip() for r in body if j < len(r.cells)]
        vals = [v for v in vals if v]
        hits = sum(1 for v in vals if _looks_numeric(v))
        numeric_col.append(bool(vals) and hits * 2 > len(vals))   # 过半即算

    for i, row in enumerate(rows):
        head = (i == 0)
        for j, cell in enumerate(row.cells):
            try:
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            except Exception:                      # noqa: BLE001 — 纯外观，失败不致命
                pass
            if head:
                _shade_cell(cell, _spec.TABLE_HEAD_BG)
            align = (WD_ALIGN_PARAGRAPH.RIGHT if numeric_col[j]
                     else WD_ALIGN_PARAGRAPH.LEFT)
            for par in cell.paragraphs:
                par.alignment = align
                if head:
                    for run in par.runs:
                        run.bold = True


def _add_table(doc, rows) -> None:
    """把二维列表写成一张 Word 表格。

    两处共用：`add_block(block_type="table")` 与 `_parse_document_text`
    产出的 `{"type": "table"}`。不许抄第二份：两份会漂移，
    而两个答案看着都对。
    长相是表头底纹 + 加粗 + 发丝线框 + 数字列右对齐，
    不用 `Table Grid`（纯黑框、表头与表体无分别）。
    不做隔行斑马纹 —— 中文商务表格的习惯里没有这一条，行数一多反而花。
    加它是我们发明的、不是必要的，所以不加（理由见 `layout_spec` docstring 五）。
    """
    table_data = rows or []
    if not table_data:
        doc.add_table(rows=1, cols=1)
        return
    ncols = max(len(r) for r in table_data)
    table = doc.add_table(rows=len(table_data), cols=ncols)
    try:
        table.style = "Table Grid"                 # 借它把网格建起来，框线下面换色
    except Exception:                              # noqa: BLE001
        pass
    for i, row_data in enumerate(table_data):
        for j, cell_val in enumerate(row_data):
            table.rows[i].cells[j].text = str(cell_val)
    _prettify_table(table)


# 组件 —— 让交付件不至于"只有一张表格"
#
# 全是版式，不是插图：底纹 + 边框 + 缩进拼出来，可编辑、跟着文档变。
# 规格在 `layout_spec.py` 的「组件」那一段。

#: `w:pPr` 里排在 `w:shd` 之后的（= `_PPR_AFTER_PBDR` 去掉它自己）。
_PPR_AFTER_SHD = _PPR_AFTER_PBDR[1:]


def _set_left_bar(ppr, color: str, sz: int) -> None:
    """给一个 `w:pPr` 加左侧竖条。样式级与段落级共用这一份。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.insert_element_before(pbdr, *_PPR_AFTER_PBDR)
    if pbdr.find(qn("w:left")) is not None:
        return
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), str(sz))
    left.set(qn("w:space"), "8")
    left.set(qn("w:color"), color)
    pbdr.append(left)


def _box_style(par, fill: str, bar: str, sz: int = 18) -> None:
    """把一段做框：底纹 + 左强调条 + 缩进（段落没有 padding，只能靠缩进顶）。

    用段落而不是单格表格：少一个 `doc.tables` 里的对象，
    版式体检就不会把提示框当成"表头没底纹的表"。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt
    ppr = par._p.get_or_add_pPr()
    shd = ppr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        ppr.insert_element_before(shd, *_PPR_AFTER_SHD)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    _set_left_bar(ppr, bar, sz)
    par.paragraph_format.left_indent = Pt(_spec.BOX_INDENT_PT)
    par.paragraph_format.right_indent = Pt(_spec.BOX_INDENT_PT)
    par.paragraph_format.space_before = Pt(_spec.BOX_SPACE_PT)
    par.paragraph_format.space_after = Pt(_spec.BOX_SPACE_PT)


def _add_divider(doc) -> None:
    """一条横向分隔线（`---`）—— 段落的下边框。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt
    p = doc.add_paragraph()
    ppr = p._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(_spec.DIVIDER_SZ))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), _spec.HAIRLINE)
    pbdr.append(bottom)
    ppr.insert_element_before(pbdr, *_PPR_AFTER_PBDR)
    p.paragraph_format.space_before = Pt(6.0)
    p.paragraph_format.space_after = Pt(6.0)


def _add_callout(doc, lines, kind: str) -> None:
    """提示框（`> [!NOTE]` 一族）/ 引用块（普通 `>`）：逐段铺底与竖条。

    逐段而不是整块一次：每段各自带底纹与左条，中间不会断开
    （整块一次做不到——段落底纹不能跨段）。
    """
    fill, bar = (_spec.CALLOUT.get(kind) or (_spec.QUOTE_BG, _spec.QUOTE_BAR))
    for ln in (lines or [""]):
        p = _add_para(doc, ln)
        _box_style(p, fill, bar)


def _add_kpi(doc, rows) -> None:
    """一排指标卡：`标签 | 数值 | 增减`（增减可省）。

    用无框表排（要横排只能如此），并打上 `COMPONENT_MARK`
    —— 版式体检据此把"组件表"与"数据表"分开（数据表必须有表头底纹，组件表不适用）。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    data = [r for r in (rows or []) if r]
    if not data:
        return
    ncols = min(len(data), 4)                      # 一行最多 4 张（再多就挤了）
    chunks = [data[i:i + ncols] for i in range(0, len(data), ncols)]
    for chunk in chunks:
        table = doc.add_table(rows=1, cols=len(chunk))
        # 打标记：这是组件，不是数据表
        cap = OxmlElement("w:tblCaption")
        cap.set(qn("w:val"), _spec.COMPONENT_MARK)
        table._tbl.tblPr.append(cap)
        for j, item in enumerate(chunk):
            label = item[0] if len(item) > 0 else ""
            value = item[1] if len(item) > 1 else ""
            change = item[2] if len(item) > 2 else ""
            cell = table.rows[0].cells[j]
            cell.text = ""
            p1 = cell.paragraphs[0]
            p1.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r1 = p1.add_run(str(value))
            r1.bold = True
            r1.font.size = Pt(_spec.KPI_VALUE_PT)
            r1.font.color.rgb = RGBColor.from_string(_spec.ACCENT)
            p2 = cell.add_paragraph()
            p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r2 = p2.add_run(str(label))
            r2.font.size = Pt(_spec.KPI_LABEL_PT)
            r2.font.color.rgb = RGBColor.from_string(_spec.INK_MUTED)
            if change:
                p3 = cell.add_paragraph()
                p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
                r3 = p3.add_run(str(change))
                r3.font.size = Pt(_spec.KPI_CHANGE_PT)
                r3.font.color.rgb = RGBColor.from_string(_spec.INK_MUTED)
            _shade_cell(cell, _spec.KPI_BG)


#: 提示框标记：`> [!NOTE]`（GitHub alert 的写法，模型本来就会）＋几个中文别名。
#: 允许标记后面同一行还跟正文（`> [!NOTE] 这段话…`）—— 模型会那么写。
_RE_CALLOUT = re.compile(
    r"^\[!\s*(NOTE|TIP|IMPORTANT|WARNING|CAUTION|DANGER|"
    r"说明|建议|注意|警告|重要|严禁)\s*\]\s*(.*)$", re.I)

#: 标记名 → 四型之一。四个色只有四个，所以多个名字会归到同一型（有意的）。
_ALERT_KIND = {
    "note": "info", "说明": "info",
    "tip": "tip", "建议": "tip",
    "important": "warning", "重要": "warning",
    "warning": "warning", "警告": "warning", "注意": "warning",
    "caution": "danger", "danger": "danger", "严禁": "danger",
}

#: `:::kpi` 开 / `:::` 闭（fenced div，pandoc 与 markdown-it 都有这个写法）。
_RE_FENCE_OPEN = re.compile(r"^:::\s*([A-Za-z]+)\s*$")
_RE_FENCE_CLOSE = re.compile(r"^:::\s*$")

#: 分隔线：单独一行的三个以上 `-`。
#: 不认 `- - -`（带空格的）：那种写法在中文里少见，多认一种就多一种误判。
_RE_DIVIDER = re.compile(r"^-{3,}$")


def _parse_document_text(text: str) -> list:
    """把纯文本 / Markdown 解析成块列表。

    支持的最小子集（与 `add_block` 的 `block_type` 对齐）：

    ==============================  ==========================
    `#` / `##` / `###`        heading（level 1-3）
    `-` / `*` / `1.`          list（连续行合并成一块）
    | a | b | + | --- |     table
    空行分隔                        paragraph
    ==============================  ==========================

    表格必须真的建成表格：整篇带表的 Markdown 若把管道表
    原样落成竖线文本，工具仍会返回 success、零警告 —— 回执与事实不符。
    图片 / 行内格式仍然不做 —— 那两格继续走 `add_block`。

    收表的判据只有一个：某行以 `|` 开头、且下一行是分隔行（`_RE_TABLE_SEP`）。
    没有分隔行，不猜，仍按普通段落走（fail-open）。

    动机见 :func:`create_document` 的 `content` 参数。
    """
    blocks: list = []
    para: list = []
    items: list = []
    ordered = False

    def flush_para() -> None:
        nonlocal para
        if para:
            txt = "\n".join(para).strip()
            if txt:
                blocks.append({"type": "paragraph", "content": txt})
            para = []

    def flush_list() -> None:
        nonlocal items
        if items:
            blocks.append({"type": "list",
                           "content": "\n".join(items),
                           "ordered": ordered})
        items = []

    # 用带回看的索引循环 —— 收表要在当前行决定时就看下一行
    #   是不是分隔行，所以不能只做一次顺序扫描。
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            flush_para()
            flush_list()
            i += 1
            continue

        m = _RE_HEADING.match(stripped)
        if m:
            flush_para()
            flush_list()
            blocks.append({"type": "heading",
                           "content": m.group(2).strip(),
                           "level": len(m.group(1))})
            i += 1
            continue

        m = _RE_LIST.match(stripped)
        if m:
            flush_para()
            if not items:
                ordered = m.group(1)[0].isdigit()
            items.append(m.group(2).strip())
            i += 1
            continue

        # 表格：本行是表格行、且下一行是分隔行，收一整块
        # 判据不放宽：没有分隔行就不认，连续 `|` 行照旧落 paragraph。
        if (_is_table_row(stripped) and i + 1 < len(lines)
                and _RE_TABLE_SEP.match(lines[i + 1].strip())):
            flush_para()
            flush_list()
            rows = [_split_table_row(stripped)]
            i += 2                      # 跨过表头 + 分隔行（分隔行不进 rows）
            while i < len(lines) and _is_table_row(lines[i].strip()):
                rows.append(_split_table_row(lines[i].strip()))
                i += 1
            blocks.append({"type": "table", "rows": rows})
            continue

        # 分隔线（`---` 单独一行）
        if _RE_DIVIDER.match(stripped):
            flush_para()
            flush_list()
            blocks.append({"type": "divider"})
            i += 1
            continue

        # 提示框 / 引用块（连续的 `>` 行收成一块）
        # 判据：首行是 `[!TYPE]`，提示框（按 TYPE 上色）；否则，普通引用块。
        if stripped.startswith(">"):
            flush_para()
            flush_list()
            quoted = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quoted.append(lines[i].strip()[1:].strip())
                i += 1
            kind = "quote"
            if quoted:
                m = _RE_CALLOUT.match(quoted[0])
                if m:
                    kind = _ALERT_KIND.get(m.group(1).lower(), "info")
                    rest = m.group(2).strip()
                    quoted = ([rest] if rest else []) + quoted[1:]
            blocks.append({"type": "callout", "kind": kind,
                           "lines": quoted or [""]})
            continue

        # 指标卡（`:::kpi` … `:::`；行内 `标签 | 数值 | 增减`）
        m = _RE_FENCE_OPEN.match(stripped)
        if m and m.group(1).lower() == "kpi":
            flush_para()
            flush_list()
            i += 1
            rows = []
            while i < len(lines) and not _RE_FENCE_CLOSE.match(lines[i].strip()):
                row = lines[i].strip()
                if row:
                    rows.append([c.strip() for c in row.split("|")])
                i += 1
            i += 1                                  # 跳过闭合的 `:::`
            if rows:
                blocks.append({"type": "kpi", "rows": rows})
            continue

        flush_list()
        para.append(stripped)
        i += 1

    flush_para()
    flush_list()
    return blocks


def create_document(
    doc_type: str,
    title: str = "",
    content: str = "",
    output_name: str = "",
    source_file: str = "",
) -> dict:
    """
    Create a new document file on disk immediately.

    For Word: creates an empty .docx at workspace/<id>.docx.
    Each add_block() call immediately writes to this file — no
    finalize/export step needed.  Call export_document() to rename
    to the final output name when done.

    For PPT: creates a session for slide accumulation (SVG-based).

    For Excel: use write_excel_data directly — no session needed.

    Args:
        doc_type: "word", "ppt", or "ppt_svg"
        title: Optional document title
        content: 整篇正文。给了它就不需要再逐段 add_block ——
            内容会被解析成 heading / paragraph / list，一次写盘。
        source_file: 长报告走这条 —— 内容引用一个文件（`.md` / `.txt`），
            不必内联进这次调用。见下面那段「为什么」。
        output_name: 给了它就在同一次调用里直接导出成最终文件名，
            连 export_document 也省掉。

    Returns:
        dict with session_id (= file path for Word), doc_type, hint

    为什么要有 `source_file`：
    长报告的 `content` 可能超出一次 tool-call 的 JSON 长度上限而被截断
    （症状是 JSON 解析失败）。退回逐块 `add_block` 又会撞 20 块上限
后半部分被压成无标题的大段落、小标题底下全空、内容丢归属。

内容可以来自文件。那份文件是分段写出来的
    （`write_file` / `edit_file`，不受 tool-call 长度限制），
    而本函数走的是整篇写入那条路 —— 已豁免 20 块上限。
    """
    valid_types = ("word", "ppt", "ppt_svg", "txt")
    if doc_type not in valid_types:
        return {
            "success": False,
            "error": f"Unknown doc_type '{doc_type}'. Choose: {', '.join(valid_types)}. "
                     f"For Excel, use write_excel_data directly.",
        }

    # 内容也可以来自一个文件
    # 判据：长报告走这条，短内容仍可内联。
    # 读那个文件要过围栏（`validate_path(..., "read")`）—— 与别的读同一道。
    # `content` 与 `source_file` 都给了，`content` 优先（显式压过引用）。
    if source_file and not content:
        _require_security()
        try:
            src = _resolve_path(source_file)
            resolved_src = Path(
                _security.validate_path(str(src), "read"))  # type: ignore[union-attr]
            content = resolved_src.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return {"success": False,
                    "error": f"读不了 source_file='{source_file}'（{type(exc).__name__}: {exc}）。"
                             f"先用 write_file 把内容写成一个 .md / .txt 再来。"}

    # 纯文本
    # "输出一份摘要到 txt" 是自然请求；没有这个出口时模型会空转。
    #
    # 刻意不做 wip/export 两段式：纯文本没有格式问题，也不需要
    # 后续 format 工具，一次落盘最直接。
    if doc_type == "txt":
        _require_security()
        name = (output_name or title or "").strip() or "document.txt"
        # 只取 basename —— 防止模型传 ../../ 之类越权
        name = Path(name).name
        if not name.lower().endswith(".txt"):
            name += ".txt"
        target = primary_output_dir() / name
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved = Path(_security.validate_path(str(target), "write"))  # type: ignore[union-attr]
        text = content or ""
        resolved.write_text(text, encoding="utf-8")
        return {
            "success": True,
            "session_id": str(target),   # 与 word 路径一致：文件路径即 session_id
            "doc_type": "txt",
            "file_path": str(target),
            "chars_written": len(text),
            "size_bytes": resolved.stat().st_size,
            "hint": "纯文本已写入工作区。用 deliver_file 可以把它发出去。",
        }

    store = get_store()

    if doc_type == "word":
        # Word: create file immediately on disk
        from docx import Document as DocxDoc
        import uuid as _uuid
        file_id = _uuid.uuid4().hex[:12]
        out_path = store.output_dir / f"_wip_{file_id}.docx"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        doc = DocxDoc()
        _apply_layout(doc)         # 字体 + 页面/字号/配色/竖条
        if title:
            doc.core_properties.title = title

        # 整篇写入
        # 若模型必须 create_document → add_block × N → export_document，
        # 每个段落 = 一次工具调用 = 一次 LLM 往返，而每次往返都要把整个
        # 消息历史重发一遍。一次"写摘要"任务因此产生 ~40 次 add_block
        # 调用、232.9K tokens、近两分钟。
        #
        #提供一次调用写整篇（整文件写入 / 补丁写入）的路径。
        # 结构化块（表格/图片）仍然走 add_block。
        blocks = _parse_document_text(content) if content else []
        written = 0
        for blk in blocks:
            bt = blk["type"]
            if bt == "heading":
                doc.add_heading(blk["content"],
                                level=max(1, min(int(blk.get("level", 1)), 3)))
            elif bt == "paragraph":
                # 走 `_add_para` —— `粗体` 真渲染，不留记号
                _add_para(doc, blk["content"])
            elif bt == "list":
                style = "List Number" if blk.get("ordered") else "List Bullet"
                for item in blk["content"].split("\n"):
                    if item.strip():
                        _add_para(doc, item.strip(), style=style)
            elif bt == "table":
                # Markdown 管道表真的建成 Word 表格。
                #   未知块类型必须显式处理：若静默跳过而 `written += 1`
                #   照加，会变成块数报对了、内容没了 —— "回执比事实更自信"。
                _add_table(doc, blk.get("rows") or [])
            elif bt == "divider":
                _add_divider(doc)
            elif bt == "callout":
                _add_callout(doc, blk.get("lines") or [], blk.get("kind") or "quote")
            elif bt == "kpi":
                _add_kpi(doc, blk.get("rows") or [])
            written += 1

        doc.save(str(out_path))

        # Register a lightweight session (just for tracking)
        session = store.create(doc_type="word", title=title)
        session.output_path = str(out_path)
        session.status = "DRAFT"
        # 让块计数与 add_block 路径一致（add_block 的 20 块上限读的就是它）
        for blk in blocks:
            session.blocks.append({"type": blk["type"]})
        # 整篇写入不算进那个 20 块上限 —— 上限的本意是防"交互式无限追加"，
        #   而这一次是另一次操作（内容长度本来就受模型输出限制）。记下基线，
        #   `add_block` 从它往上数。否则一篇 30 块的长报告永远加不进一张图。
        session.append_base = len(session.blocks)

        result = {
            "success": True,
            "session_id": str(out_path),  # file path IS the session_id
            "doc_type": "word",
            "title": title,
            "file_path": str(out_path),
            "blocks_written": written,
            "hint": (
                "整篇内容已一次写盘。用 export_document 重命名为最终文件名。"
                if written else
                "文件已创建（空的）。**推荐**直接传 content 一次写完整篇"
                "（标题 / 段落 / 列表 / **表格**都认）；"
                "图片这类结构化块才需要逐次 add_block。"
            ),
        }

        # 写侧出声：本次会话里若有过"没读全"的 Excel 读，
        #    就在回执上挂一句 —— 否则「读了半块 → 拿它下结论 → 写进交付物」
        #    这条链上，写侧一个字都不知道。
        from core.partial_read import warning_now as _pw_now
        _pr = _pw_now()
        if _pr:
            result["warning"] = _pr

        # output_name 给了就在同一次调用里导出 —— 连 export_document 也省掉，
        # 整个"创建 → 写入 → 导出"流程压缩成 1 次工具调用。
        if output_name and written:
            try:
                exported = export_document(session_id=str(out_path),
                                           output_name=output_name)
                if isinstance(exported, dict) and exported.get("success"):
                    result["exported_to"] = exported.get("file_path") or exported.get("output_path")
                    if result["exported_to"]:
                        result["file_path"] = result["exported_to"]
                        result["session_id"] = result["exported_to"]
                else:
                    result["export_error"] = (exported or {}).get("error", "unknown")
            except Exception as exc:                       # noqa: BLE001
                result["export_error"] = f"{type(exc).__name__}: {exc}"

        return result

    # PPT / PPT SVG: traditional session mode
    session = store.create(doc_type=doc_type, title=title)

    if doc_type == "ppt_svg":
        svg_dir = store.output_dir / "svg_sessions" / session.session_id
        svg_dir.mkdir(parents=True, exist_ok=True)
        session.svg_dir = str(svg_dir)
        session.canvas_format = "ppt169"

    next_hints = {
        "ppt": "Use add_slide to add slides, then export_document.",
        "ppt_svg": "Use set_slide_design to define colors/fonts/layout, "
                   "then create_svg_slide for each slide. "
                   "For >5 slides, generate in segments of 3-4. "
                   "Use mock_image for placeholder images. "
                   "Call export_document when done.",
    }

    return {
        "success": True,
        "session_id": session.session_id,
        "doc_type": doc_type,
        "status": session.status,
        "title": title,
        "next_step": next_hints.get(doc_type, ""),
    }


def add_block(
    session_id: str,
    block_type: str,
    content: str = "",
    level: int = 1,
    rows: list | None = None,
    image_path: str = "",
    ordered: bool = False,
) -> dict:
    """
    Append a content block to a Word document and write to disk immediately.

    The file is saved after every block — no finalize step needed.
    All Word format tools (format_paragraph_text, format_word_table, etc.)
    can operate on the same file between add_block calls.

    Args:
        session_id: File path from create_document(word) — the .docx on disk.
        block_type: One of: heading, paragraph, table, list, image, page_break
        content: Text content
        level: Heading level 1-3
        rows: List of lists for table block_type
        image_path: Path to image file
        ordered: True for ordered (numbered) list

    Returns:
        dict with success, block_count, file_path
    """
    valid_blocks = ("heading", "paragraph", "table", "list", "image", "page_break")
    if block_type not in valid_blocks:
        return {
            "success": False,
            "error": f"Unknown block_type '{block_type}'. Choose: {', '.join(valid_blocks)}",
        }

    _require_security()
    file_path = Path(session_id)

    # detect if this looks like a file path (from new Word flow) vs old session ID
    if not file_path.suffix == ".docx":
        # Fallback: old session-id based flow — lookup in store
        store = get_store()
        session = store.get(session_id)
        if session is None:
            return {"success": False, "error": f"Session '{session_id}' not found. "
                     "For Word, use create_document(word) to get a file path."}
        if session.doc_type != "word":
            return {"success": False, "error": f"Session is type '{session.doc_type}', not 'word'."}
        if session.output_path:
            file_path = Path(session.output_path)
        else:
            return {"success": False, "error": "Session exists but has no file path."}
        # Update session_id to use file path going forward
        session_id = str(file_path)

    # 按"写"校验，不是按"读"。
    #
    # 这下面执行的是写（`doc.save(str(file_path))`）。`validate_path` 的
    # READ_ONLY 分支是「`read`/`copy` 放行、其余 raise」，若用默认的
    # `required_op="read"`，一个已存在、落在跨区 READ_ONLY 区的 `.docx`
    # 会过读校验、然后被写进去 —— 围栏在门上，门开着。
    #
    # 两处必须一起：
    #   ① 校验放在"路径定下来之后" —— 放在最上面时，走 store 兜底那条路
    #      拿到的 `session.output_path` 会一次校验都没有；
    #   ② 显式 `"write"` —— 默认值是 `read`。
    #
    # 正常路径看不出差别：`session_id` 来自 `create_document`，落点在工作区
    # （READ_WRITE），两档都放行。只有跨区那种情况会变 —— 而那正是要拦的。
    file_path = Path(_security.validate_path(str(file_path), "write"))  # type: ignore[union-attr]

    if not file_path.exists():
        return {"success": False, "error": f"文档文件不存在: {file_path}. 请先调用 create_document(word)"}

    # Hard cap
    store = get_store()
    session = store.get(session_id)
    block_index = 0
    if session is not None:
        # 从 `append_base` 往上数 —— `create_document` 一次写进去的那些
        #   不算（见 `DocSession.append_base`）。数全额的话，一篇 30 块的长报告
        #   会把 `add_block` 永久锁死，而模型只能自己写 python-docx 绕过去。
        block_index = len(session.blocks) - int(getattr(session, "append_base", 0) or 0)
        if block_index >= 20:
            return {"success": False, "error": "MAXIMUM 20 blocks reached."}

    try:
        from docx import Document as DocxDoc
        from docx.shared import Inches, Pt
        doc = DocxDoc(str(file_path))
        _apply_layout(doc)         # 与 `create_document` 同一条版式

        if block_type == "heading":
            h = doc.add_heading(content, level=max(1, min(level, 3)))
        elif block_type == "paragraph":
            _add_para(doc, content)
        elif block_type == "table":
            # Accept both `rows` list and `content` CSV-like string
            table_data = rows if rows else []
            if not table_data and content:
                # Parse CSV-like content: "A,B,C\n1,2,3\n4,5,6"
                table_data = [[cell.strip() for cell in line.split(",")]
                              for line in content.split("\n") if line.strip()]
            # 建表代码走 `_add_table` —— 两处共用一份。另一处是
            #   `create_document(content=…)` 里认出来的 Markdown 表格。抄第二份的
            #   代价不是多写几行，是两份会漂移、而两个答案看着都对。
            _add_table(doc, table_data)
        elif block_type == "list":
            for item in (content.split("\n") if content else []):
                item = item.strip()
                if item:
                    _add_para(doc, item,
                              style="List Bullet" if not ordered else "List Number")
        elif block_type == "image":
            img_path = Path(image_path)
            if not img_path.is_absolute():
                # 「查」—— 图片可能在任何一个工作组里
                img_path = find_in_roots(img_path) or (lookup_roots()[0] / img_path)
            if img_path.exists():
                doc.add_picture(str(img_path))
            else:
                return {"success": False, "error": f"图片文件不存在: {image_path}"}
        elif block_type == "page_break":
            doc.add_page_break()

        doc.save(str(file_path))

        # Track in session for block count
        if session is not None:
            session.blocks.append({"type": block_type})
            # 回执里的块号也用同一个口径（追加计数），
            #   否则模型看到的 "Block 31/20" 会自相矛盾
            block_index = len(session.blocks) - int(
                getattr(session, "append_base", 0) or 0)

        return {
            "success": True,
            "session_id": str(file_path),
            "block_count": block_index,
            "block_type": block_type,
            "file_path": str(file_path),
            "hint": f"Block {block_index}/20 added and saved. "
                    "You can now call format tools (format_paragraph_text, format_word_table, etc.) "
                    "on this file. Use add_block again for more content, "
                    "or export_document to rename to final name.",
        }
    except Exception as e:
        return {"success": False, "error": f"添加内容块失败: {type(e).__name__}: {e}"}


def add_slide(
    session_id: str,
    slide_type: str,
    title: str = "",
    bullets: list | None = None,
    image_path: str = "",
    rows: list | None = None,
) -> dict:
    """
    Append a slide to a PowerPoint presentation under construction.

    Args:
        session_id: From create_document()
        slide_type: One of: title, content, image, table
        title: Slide title text
        bullets: List of bullet point strings (for content slide)
        image_path: Path to image (for image slide)
        rows: List of lists (for table slide)

    Returns:
        dict with success, slide_count, hint
    """
    valid_slides = ("title", "content", "image", "table")
    if slide_type not in valid_slides:
        return {
            "success": False,
            "error": f"Unknown slide_type '{slide_type}'. Choose: {', '.join(valid_slides)}",
        }

    store = get_store()
    session = store.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session '{session_id}' not found or expired."}
    if session.doc_type != "ppt":
        return {"success": False, "error": f"Session is type '{session.doc_type}', not 'ppt'."}
    if session.status != "DRAFT":
        return {"success": False, "error": f"Session is {session.status}, no more edits allowed."}

    slide: dict = {"type": slide_type, "title": title}

    if slide_type == "content":
        slide["bullets"] = bullets or []
    elif slide_type == "image":
        slide["image_path"] = image_path
    elif slide_type == "table":
        slide["rows"] = rows or []

    session.blocks.append(slide)

    return {
        "success": True,
        "session_id": session_id,
        "slide_count": len(session.blocks),
        "hint": "Continue calling add_slide for more slides, "
                "or call export_document when done.",
    }


# write_sheet 不提供：直接用 write_excel_data（不需要 session）。

# finalize_document 不提供：add_block 立即写盘，不需要加锁收尾。

def export_document(
    session_id: str,
    output_name: str = "",
) -> dict:
    """
    Finalize and rename a Word or PPT document.

    For Word: the file already exists on disk (add_block writes immediately).
    This renames the working file to the final output name.

    For PPT/PPT_SVG: renders slides to disk.
    """
    from pathlib import Path as _Path
    store = get_store()
    _require_security()

    # Word: file-path based session_id
    candidate = _Path(session_id)
    if candidate.suffix == ".docx" and candidate.exists():
        # 重命名必须校验：重命名 = 在旧路径上删、在新路径上写
        # —— 两个端点都要问。
        # 走 `"write"` 而不是 `"rename"`/`"delete"`：一次操作只用一个 op，
        # 少一个"这个工作区配了 rename 没配 write 会怎样"的分支要想。
        # READ_ONLY 区三个 op 一视同仁地拦（`validate_path` 只放 `read`/`copy`）。
        try:
            candidate = _Path(_security.validate_path(str(candidate), "write"))
        except WriteNeedsApproval:
            # 区外写是一道审批，不是错误。必须让它冒到派发层
            # （`agent_loop` 那两处 `except WriteNeedsApproval` 才接得住）——
            # 被下面那个 `except Exception` 接住的话，用户看到的是一行
            # "安全限制: …"，而审批弹窗永远不出现。
            raise
        except Exception as e:
            return {"success": False, "error": f"安全限制: {e}"}
        fname = output_name or f"document_{candidate.stem}.docx"
        # 只取 basename —— `output_name` 是模型给的，而它下面直接拼进
        # `target` 且 `target.unlink()` 在前，不剥的话 `"../../evil.docx"`
        # 就是一个逃出工作区的任意删除/写入原语。
        # 与 `create_document` 的 txt 分支同一条规则：只取 basename。
        fname = _Path(fname).name
        if not fname.endswith(".docx"):
            fname += ".docx"
        target = candidate.parent / fname
        # 新端点同样要问（`candidate.parent` 已校验过，剥完 basename 之后
        # `target` 必然同区 —— 这一句是纵深的第二道，防的是上面那句
        # 哪天被改回去）。
        try:
            target = _Path(_security.validate_path(str(target), "write"))
        except WriteNeedsApproval:
            raise                      # 同上：审批不许被吞成错误文本
        except Exception as e:
            return {"success": False, "error": f"安全限制: {e}"}
        # 下面那句 `target.unlink()` + `rename` 是整本盲覆盖 ——
        #   目标名撞上另一个对话正在改的 .docx，它整份就没了（连回收站都没有）。
        _blk = overwrite_blocked(target)
        if _blk:
            return {"success": False, **_blk}
        try:
            if target.exists():
                target.unlink()
            candidate.rename(target)
            _note_observed(target)      # 写成功之后回记
            session = store.get(session_id)
            if session is not None:
                # 重命名之后句柄必须跟着走。
                # `session.get()` 也认文件路径（见 `session.py` 的 `_by_output_path`），
                # 所以 `output_path` 从这一刻起就是"这份文件现在的路径"。
                # 不更新的话，`create_document(output_name=…)` 那条一次成型的路
                # （它内部就调这个函数、并把导出后的路径当 session_id 返回）
                # 会让模型手里的路径查不到自己的 session —— 同一个 20 块上限
                # 和块计数会静默归零。
                # 顺序：先 `get`（用旧路径找到），再改。反了就得靠旧值兜底。
                session.output_path = str(target)
            block_count = len(session.blocks) if session else 0
            return {
                "success": True,
                "file_path": str(target),
                "doc_type": "word",
                "block_count": block_count,
                "hint": f"文档已导出至 {target}。用户需要文件时调用 deliver_file('{target}') 发送。",
            }
        except Exception as e:
            return {"success": False, "error": f"重命名失败: {type(e).__name__}: {e}"}

    # PPT / PPT_SVG: traditional render
    session = store.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session not found. For Word, pass file path."}
    if session.doc_type not in ("ppt", "ppt_svg"):
        return {"success": False, "error": f"export_document handles word/ppt/ppt_svg only."}
    if session.status == "EXPORTED":
        return {"success": False, "error": "Document already exported."}
    if session.status == "DRAFT":
        session.status = "FINALIZED"

    ext = ".pptx"
    fname = output_name or f"{session.title or 'presentation'}{ext}"
    if not fname.endswith(ext):
        fname += ext
    raw_path = store.output_dir / fname
    out_path = _Path(_security.validate_path(str(raw_path)))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _font_notes: list = []                      # 字体替换记录
    if session.doc_type == "ppt":
        _render_ppt(session, out_path)
    elif session.doc_type == "ppt_svg":
        _font_notes = _render_ppt_svg(session, out_path)

    session.status = "EXPORTED"
    session.output_path = str(out_path)
    out = {
        "success": True,
        "file_path": str(out_path),
        "doc_type": session.doc_type,
        "block_count": len(session.blocks),
        "hint": f"Document exported to {out_path}.",
    }
    if _font_notes:
        out["notes"] = _font_notes
    return out

# export_workbook removed — Excel uses write_excel_data directly


# Office -> PDF
#
# 本工具依赖 `manifest.yaml` 里的声明与本文件的具名 import 两处同步：
# 漏掉任何一处，模型会搜得到、调得动，第一次调用却抛
# `AttributeError: ... not found`。`core/skill_doctor.py` 按 manifest
# 反查 handler，能抓到这一类漏登记。
#
# 转换依赖本机安装 LibreOffice：没有安装时本工具只返回一句
# 可执行的错误说明，不会产出 PDF。

# 候选路径与查找函数在 `soffice.py` 里 ——
# `excel_transform.recalculate_excel_formulas` 也要用它，各留一份会漂移。
# 这里保持 `_SOFFICE_CANDIDATES` / `_find_soffice` 这两个名字可用，
# 免得动到本文件里已有的调用点与任何按名字找它们的读者。
from .soffice import (  # noqa: E402
    CONVERT_TIMEOUT_S as _CONVERT_TIMEOUT_S,
    SOFFICE_CANDIDATES as _SOFFICE_CANDIDATES,
    find_soffice as _find_soffice,
)

#: 能吃进来的后缀（与本文件 `convert_to_pdf` 的 docstring 逐字一致）
_CONVERTIBLE_SUFFIXES = (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt")

#: 幻灯片目检的回执附言。
#:
#: 三渲染器差异（同一份产物逐页比 PDF 文字层）：
#: **PowerPoint 176 行 · WPS 177 行 · LibreOffice 192 行** —— LibreOffice 多出 16 行，
#: 全是单行文本框被折出来的（`NVIDIA` 折成 `NVIDI` + 换行的 `A`）。首行坐标还能看出
#: 机理：`NVDA` 在 LibreOffice 里 x=740.6，在 PowerPoint 里 x=720.5
#: —— 框被撑宽了。
#:
#: 根因是转换器给单行文本框写的是 `wrap="none"` + `spAutoFit`
#: （`pptx_export/svg_to_pptx/drawingml/elements.py:1585`）：
#: PowerPoint / WPS 认这个语义、按文字把框撑宽；LibreOffice 忽略 `wrap="none"`、
#: 按框宽折行。折行是渲染器的差异，不是产物缺陷。
#:
#: 而目检这条路用的正是 LibreOffice（转 PDF → 出图 → 看图）
#:模型每一次都会看见那 16 行。写在回执里 = 事实在用到它的那一刻到达，
#: 不靠它每次想起来 —— 能给它「事实」，就别给它「规则」。
_DECK_RENDER_NOTE = (
    " 提示：此 PDF 由 LibreOffice 渲染，它会把幻灯片单行文本框折成两行；"
    "PowerPoint / WPS 不折（改成把框撑宽）。"
    "断行是渲染器差异，不是产物缺陷，别据此改产物。"
)


def _deck_render_note(suffix: str) -> str:
    """幻灯片才附那句话 —— `docx`/`xlsx` 转出来的 PDF 没有这个现象。

    自带 `lower()`：调用方（`convert_to_pdf`）已经先 `lower()` 过一次，
    但一个只在小写输入下才对的助手，换个调用点就静默失效
    （`.PPTX` 落进 `else` 分支，附言消失，而不会有任何东西红）。
    """
    return _DECK_RENDER_NOTE if suffix.lower() in (".pptx", ".ppt") else ""


def _drain_font_substitutions() -> None:
    """清场 —— 一个转换周期开始时调一次。

    为什么需要它：那份记录是模块级的，而一次转换中途抛异常时会留下残留
下一轮的产物会替上一轮背黑锅（回执上写着"字体被换了"，而这一轮一次都没换）。
    """
    try:
        from .pptx_export.svg_to_pptx.drawingml.utils import take_font_substitutions
    except Exception:                     # noqa: BLE001 —— 出不了声也不该挡住导出
        return
    take_font_substitutions()


def _font_substitution_notes() -> list:
    """把这次转换里被换掉的字体族取回来 —— 取走即清空。

    判据（条目逐字）：「产物里不出现它，且转换器出声说了它换成了什么。
    只看『没崩』不算过」。
    控制台那几行不算出声 —— WebUI 看不见，那句话必须进回执。
    读不到那份记录，空表（不做任何断言，也不假装说过）。
    """
    try:
        from .pptx_export.svg_to_pptx.drawingml.utils import take_font_substitutions
    except Exception:                     # noqa: BLE001
        return []
    subs = take_font_substitutions()
    if not subs:
        return []
    detail = "；".join("%s -> %s" % (a, b) for a, b in subs[:5])
    if len(subs) > 5:
        detail += "；另有 %d 处" % (len(subs) - 5)
    return ["产物里有 %d 个字体族本机没装，已换成栈里后面的字体：%s。"
            "（对方机器上打开时的替换是另一回事，本条只保证**我们这台**不出错行）"
            % (len(subs), detail)]


def convert_to_pdf(filename: str = "") -> dict:
    """
    Convert an Office file to PDF via LibreOffice headless mode.

    Supports .docx/.doc, .xlsx/.xls, .pptx/.ppt.
    Output saved under workspace/.

    Args:
        filename: The Office file to convert, e.g. "report.docx".

    Returns:
        dict with success, output_path (or error message).
    """
    _require_security()
    if not filename:
        return {"success": False, "error": "请提供要转换的 Office 文件名"}

    resolved = _resolve_path(filename)
    try:
        validated = _security.validate_path(str(resolved))  # type: ignore[union-attr]
    except Exception as e:
        return {"success": False, "error": f"Security check failed: {e}"}

    source = Path(validated)
    if not source.exists():
        return {"success": False, "error": f"文件不存在: {filename}"}

    suffix = source.suffix.lower()
    if suffix not in _CONVERTIBLE_SUFFIXES:
        return {
            "success": False,
            "error": (f"不支持的文件格式 '{suffix}'。支持: "
                      f"{', '.join(_CONVERTIBLE_SUFFIXES)}"),
        }

    soffice = _find_soffice()
    if soffice is None:
        # 这条路给得出下一步（拒绝要带路线）：
        # 只说"不支持"会让模型换个写法重试一遍，再失败一次。
        return {
            "success": False,
            "error": ("LibreOffice 未找到 —— Office→PDF 转换依赖它的无头模式。"
                      "当前机器上 %d 个候选路径都不存在（含 PATH）。"
                      % len(_SOFFICE_CANDIDATES)),
            "hint": ("装一个 LibreOffice（libreoffice.org，免费）即可；"
                     "或者改用 `run_command` 自己跑转换命令。"
                     "★ 若这台机器上装了 Microsoft Office，"
                     "也可以用 `docx2pdf`（它走 Word COM，需要另装）。"),
        }

    import subprocess

    # 落点唯一口径：`primary_output_dir()`
    out_dir = primary_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_name = source.stem + ".pdf"
    out_path = out_dir / output_name

    # 用列表传参，不拼字符串 —— 文件名里有空格/中文是常态
    #（`core/shell.py` 那条线记过：拼字符串就要面对引号，而引号有无数种写法）。
    cmd = [soffice, "--headless", "--convert-to", "pdf",
           "--outdir", str(out_dir.resolve()), str(source.resolve())]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_CONVERT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"LibreOffice 转换超时（{_CONVERT_TIMEOUT_S} 秒）—— 已放弃。",
            "hint": "文件太大或 LibreOffice 卡住了；可以先用小文件试一次。",
        }
    except OSError as e:
        return {"success": False,
                "error": f"启动 LibreOffice 失败: {type(e).__name__}: {e}"}

    if not out_path.exists():
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
        return {
            "success": False,
            "error": (f"LibreOffice 跑完了但没产出 {output_name}"
                      f"（退出码 {proc.returncode}）。"),
            "detail": tail.strip() or "(stderr 是空的)",
            "hint": "常见原因：**另有一个 LibreOffice 实例**占着用户配置目录"
                    "（可能就是用户自己正开着的那个）。\n"
                    "★ **别去按名字杀进程**（`taskkill /IM soffice.bin`、`killall` 那种）—— "
                    "那会连带关掉用户自己的窗口，而里面可能有没保存的东西。\n"
                    "可以走的两条：① 用**独立的用户配置目录**再跑一次"
                    "（soffice 支持 `-env:UserInstallation=file:///<一个临时目录>`，"
                    "两边互不干扰）；② 或者把这个情况**告诉用户**，"
                    "由他决定要不要关掉那个实例。",
        }

    return {
        "success": True,
        "output_path": str(out_path.resolve()),
        "original": filename,
        # 幻灯片附一句渲染器差异（`spAutoFit` 那条，见上面 `_DECK_RENDER_NOTE`）
        "hint": (f"已将 {filename} 转换为 PDF: {output_name}"
                 + _deck_render_note(suffix)),
    }


def convert_from_pdf(filename: str = "", target_format: str = "word") -> dict:
    """
    Convert a PDF file to Word (.docx) or Excel (.xlsx).

    PDF→Word 用 `docvortex`（MIT，零 PyMuPDF）；
    PDF→Excel 用 `pdfplumber`（MIT）。
    Output saved under workspace/.

    Args:
        filename: The PDF file to convert, e.g. "document.pdf".
        target_format: Target format — "word" or "excel". Default: "word".

    Returns:
        dict with success, output_path (or error message).
    """
    _require_security()
    if not filename:
        return {"success": False, "error": "请提供要转换的 PDF 文件名"}

    resolved = _resolve_path(filename)
    try:
        validated = _security.validate_path(str(resolved))  # type: ignore[union-attr]
    except Exception as e:
        return {"success": False, "error": f"Security check failed: {e}"}

    source = Path(validated)
    if not source.exists():
        return {"success": False, "error": f"文件不存在: {filename}"}

    suffix = source.suffix.lower()
    if suffix != ".pdf":
        return {"success": False, "error": f"需要 PDF 文件，但提供了 '{suffix}'"}

    target = target_format.lower().strip()
    if target not in ("word", "excel"):
        return {"success": False, "error": f"不支持的目标格式 '{target_format}'。可选: word, excel"}

    out_dir = primary_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    if target == "word":
        try:
            import docvortex as _docvortex
        except ImportError:
            return {
                "success": False,
                "error": "docvortex 未安装。请执行: pip install docvortex",
            }

        output_name = source.stem + "_converted.docx"
        out_path = out_dir / output_name

        try:
            # `parse_mode="txt"`：只吃文本层，不触发 OCR 模型下载。
            # 扫描件会得到空结果，下面那支如实说，别假装转换成功。
            result = _docvortex.parse(str(source.resolve()), parse_mode="txt")
            result.export(str(out_path.resolve()), output_format="docx",
                          overwrite=True)
        except Exception as e:
            return {"success": False, "error": f"PDF→Word 转换失败: {type(e).__name__}: {e}"}

        # docvortex 对空白/扫描件也会写出一个空 docx（文件非零字节）
        #不能只看文件大小，要真的读回来看有没有字。
        #   `doc.paragraphs` 读不到 textbox 层，所以直接看 `word/document.xml`。
        import re as _re
        import zipfile as _zip

        def _docx_text(p: Path) -> str:
            try:
                if p.is_file() and p.stat().st_size > 0:
                    with _zip.ZipFile(str(p)) as zf:
                        xml = zf.read("word/document.xml").decode("utf-8", "ignore")
                    return _re.sub(r"<[^>]+>", "", xml).strip()
            except Exception:                          # noqa: BLE001
                pass
            return ""

        # **docvortex 0.5.16 的已知问题：多页 PDF 导出的 docx 正文是空的**
        #   （1 页正常；2 页起 `document.xml` 里一个文本节点都没有，文件仍有 36 KB）。
        #   这是上游的 bug，不是路径/权限。多页时退回「逐页转换 + DOCX 合并」：
        #   每页单独 parse/export（单页路径是好的），再把 body 元素深拷贝进主文档。
        if not _docx_text(out_path):
            try:
                from pypdf import PdfReader as _R, PdfWriter as _W
                page_count = len(_R(str(source.resolve())).pages)
            except Exception:                          # noqa: BLE001
                page_count = 0
            if page_count > 1:
                import tempfile
                from copy import deepcopy
                from docx import Document as _Doc
                from pypdf import PdfReader as _R2, PdfWriter as _W2

                reader = _R2(str(source.resolve()))
                with tempfile.TemporaryDirectory(prefix="pdf2word-") as td:
                    tdp = Path(td)
                    master = _Doc()
                    first = True
                    for i, page in enumerate(reader.pages):
                        part_pdf = tdp / ("p%04d.pdf" % i)
                        w = _W2()
                        w.add_page(page)
                        with open(part_pdf, "wb") as fh:
                            w.write(fh)
                        part = _docvortex.parse(str(part_pdf), parse_mode="txt")
                        part_docx = tdp / ("p%04d.docx" % i)
                        part.export(str(part_docx), output_format="docx",
                                    overwrite=True)
                        if not first:
                            master.add_page_break()
                        first = False
                        src_doc = _Doc(str(part_docx))
                        for child in src_doc.element.body.iterchildren():
                            if str(child.tag).endswith("}sectPr"):
                                continue
                            master.element.body.append(deepcopy(child))
                    master.save(str(out_path))

        if not _docx_text(out_path):
            return {
                "success": False,
                "error": ("这份 PDF 没有可用的文本层（可能是扫描件）—— "
                          "文字版转换不会产出内容。请先用 `pdf_to_images` 渲出页面，"
                          "或让用户提供带文本层的 PDF。"),
            }

        return {
            "success": True,
            "output_path": str(out_path.resolve()),
            "original": filename,
            "target_format": "word",
            "hint": f"已将 {filename} 转换为 Word: {output_name}",
        }

    elif target == "excel":
        try:
            import pdfplumber
        except ImportError:
            return {
                "success": False,
                "error": "pdfplumber 未安装。请执行: pip install pdfplumber",
            }
        try:
            from openpyxl import Workbook
        except ImportError:
            return {
                "success": False,
                "error": "openpyxl 未安装。请执行: pip install openpyxl",
            }

        output_name = source.stem + "_converted.xlsx"
        out_path = out_dir / output_name

        try:
            wb = Workbook()
            ws = wb.active
            ws.title = "Sheet1"
            sheet_idx = 0
            with pdfplumber.open(str(source.resolve())) as pdf:
                for page_num, page in enumerate(pdf.pages, start=1):
                    table = page.extract_table()
                    if table:
                        if sheet_idx > 0:
                            ws = wb.create_sheet(title=f"Page{page_num}")
                        for row in table:
                            ws.append([str(c) if c is not None else "" for c in row])
                        sheet_idx += 1
                    else:
                        # No table found — extract plain text as a single row
                        text = page.extract_text()
                        if text:
                            if sheet_idx > 0:
                                ws = wb.create_sheet(title=f"Page{page_num}")
                            for line in text.split("\n"):
                                ws.append([line])
                            sheet_idx += 1
            wb.save(str(out_path))
        except Exception as e:
            return {"success": False, "error": f"PDF→Excel 转换失败: {type(e).__name__}: {e}"}

        return {
            "success": True,
            "output_path": str(out_path.resolve()),
            "original": filename,
            "target_format": "excel",
            "hint": f"已将 {filename} 转换为 Excel: {output_name}",
        }


# AI-driven PPT generation via SVG → native DrawingML

def _svg_validate(svg_content: str) -> tuple:
    """
    Validate SVG content for safety and PPTX compatibility.

    Returns:
        (is_valid: bool, error_message: str)
    """
    import xml.etree.ElementTree as ET

    # 1. Size check: max 100KB
    if len(svg_content.encode('utf-8')) > 102400:
        return False, "SVG content exceeds 100KB limit"

    # 2. XML parse
    try:
        root = ET.fromstring(svg_content)
    except ET.ParseError as e:
        return False, f"XML parse error: {e}"

    # 3. Check root tag
    tag = root.tag.split('}')[-1] if '}' in root.tag else root.tag
    if tag != 'svg':
        return False, f"Root element must be <svg>, got <{tag}>"

    # 4. Check viewBox or width+height
    if not root.get('viewBox') and not (root.get('width') and root.get('height')):
        return False, "SVG must have viewBox or width+height attributes"

    # 5. Tag whitelist + blacklist
    ALLOWED = {
        'svg', 'rect', 'circle', 'ellipse', 'line', 'path',
        'polygon', 'polyline', 'text', 'tspan', 'image', 'g',
        'defs', 'use', 'linearGradient', 'radialGradient', 'stop',
        'title', 'desc', 'style',
    }
    FORBIDDEN = {'script', 'foreignObject', 'iframe', 'object', 'embed'}
    FORBIDDEN_ATTR_PREFIXES = ('on',)  # onclick, onload, etc.

    for elem in root.iter():
        local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        if local in FORBIDDEN:
            return False, f"Forbidden SVG element: <{local}>"
        if local not in ALLOWED:
            return False, f"Unsupported SVG element: <{local}>"
        for attr in elem.attrib:
            if any(attr.lower().startswith(p) for p in FORBIDDEN_ATTR_PREFIXES):
                return False, f"Forbidden attribute: {attr}"

    return True, ""


def mock_image(
    width: int = 800,
    height: int = 600,
    color_hex: str = "#CCCCCC",
    label: str = "",
    output_name: str = "",
) -> dict:
    """
    Generate a placeholder image file (solid color + optional text label).

    Uses Pillow for PNG output when available; falls back to raw BMP.
    """
    import re

    # --- Validate color ---
    color_hex = color_hex.lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{3}|[0-9a-fA-F]{6}', color_hex):
        return {"success": False, "error": f"Invalid color hex: #{color_hex}"}
    if len(color_hex) == 3:
        color_hex = ''.join(c * 2 for c in color_hex)
    r, g, b = int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16)

    # --- Cap dimensions ---
    width = min(max(width, 16), 3840)
    height = min(max(height, 16), 2160)

    # --- Validate label ---
    if len(label) > 100:
        label = label[:97] + "..."

    # --- Resolve output path ---
    _require_security()
    store = get_store()
    if not output_name:
        return {"success": False, "error": "output_name is required"}
    raw_path = store.output_dir / output_name
    # 写盘的东西按 `"write"` 校验。
    # 默认是 `"read"`，而它下面 `img.save(str(out_path))` —— 读的围栏挡不住写。
    out_path = _security.validate_path(str(raw_path), "write")  # type: ignore[union-attr]

    # --- Try Pillow ---
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGB', (width, height), color=(r, g, b))
        if label:
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("arial.ttf", size=min(height // 10, 48))
            except (OSError, IOError):
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = max(0, (width - tw) // 2)
            y = max(0, (height - th) // 2)
            text_color = (255, 255, 255) if (r + g + b) / 3 < 128 else (0, 0, 0)
            draw.text((x, y), label, fill=text_color, font=font)
        img.save(str(out_path), format='PNG')
        return {
            "success": True, "file_path": str(out_path),
            "width": width, "height": height, "format": "png",
            "hint": f"Placeholder image saved. For SVG <image> tags, "
                    f"reference as href=\"../../{output_name}\" "
                    f"(SVG files are in svg_sessions/<id>/).",
        }
    except ImportError:
        pass

    # --- Fallback: raw BMP ---
    row_size = (width * 3 + 3) & ~3
    pixel_data_size = row_size * height
    file_size = 14 + 40 + pixel_data_size

    header = bytearray(14)
    header[0:2] = b'BM'
    header[2:6] = file_size.to_bytes(4, 'little')
    header[10:14] = (14 + 40).to_bytes(4, 'little')

    dib = bytearray(40)
    dib[0:4] = (40).to_bytes(4, 'little')
    dib[4:8] = width.to_bytes(4, 'little', signed=True)
    dib[8:12] = height.to_bytes(4, 'little', signed=True)
    dib[12:14] = (1).to_bytes(2, 'little')
    dib[14:16] = (24).to_bytes(2, 'little')
    dib[20:24] = pixel_data_size.to_bytes(4, 'little')

    pixels = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            ratio = 0.7 + 0.3 * ((x / max(width, 1)) * (y / max(height, 1)))
            row.extend([
                min(255, int(b * ratio)),
                min(255, int(g * ratio)),
                min(255, int(r * ratio)),
            ])
        row.extend(b'\x00' * (row_size - width * 3))
        pixels.extend(row)

    if not output_name.lower().endswith('.bmp'):
        out_path = out_path.with_suffix('.bmp')

    with open(out_path, 'wb') as f:
        f.write(bytes(header))
        f.write(bytes(dib))
        f.write(bytes(pixels))

    return {
        "success": True, "file_path": str(out_path),
        "width": width, "height": height, "format": "bmp",
        "hint": f"Placeholder (BMP) saved. For SVG <image> tags, "
                f"reference as href=\"../../{out_path.name}\" "
                f"(SVG files are in svg_sessions/<id>/).",
    }


def create_svg_slide(
    session_id: str,
    svg_content: str = "",
    page_number: int = 1,
    segment_summary: str = "",
) -> dict:
    """
    Write a validated SVG slide to the session's SVG directory.

    The SVG is validated for XML well-formedness, viewBox presence,
    and safe tag usage before being written.
    """
    import json

    store = get_store()
    session = store.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session '{session_id}' not found or expired."}
    if session.doc_type != "ppt_svg":
        return {
            "success": False,
            "error": f"Session is type '{session.doc_type}', not 'ppt_svg'.",
        }
    if session.status != "DRAFT":
        return {"success": False, "error": "Session is already finalized/exported."}
    if not svg_content.strip():
        return {"success": False, "error": "svg_content is required"}

    # Validate SVG
    is_valid, error_msg = _svg_validate(svg_content)
    if not is_valid:
        return {"success": False, "error": f"SVG validation failed: {error_msg}"}

    # Check for duplicate page_number
    if page_number in session.svg_files:
        return {
            "success": False,
            "error": f"Page {page_number} already exists. "
                     f"Use a different page_number or svgs_to_pptx to finalize.",
        }

    # Write SVG file
    svg_dir = Path(session.svg_dir)
    svg_name = f"slide_{page_number:03d}.svg"
    svg_path = svg_dir / svg_name
    svg_path.write_text(svg_content, encoding='utf-8')

    session.svg_files[page_number] = str(svg_path)

    # Optionally record segment summary for tiered context
    if segment_summary:
        try:
            summary_data = json.loads(segment_summary)
            session.segment_history.append(summary_data)
        except json.JSONDecodeError:
            pass  # silently ignore invalid JSON

    return {
        "success": True,
        "slide_count": len(session.svg_files),
        "page": page_number,
        "file_name": svg_name,
        "hint": f"Slide {page_number} written. "
                f"Accumulated: {len(session.svg_files)} slide(s).",
    }


def set_slide_design(
    session_id: str,
    colors_json: str = "",
    fonts_json: str = "",
    layout_json: str = "",
    canvas_format: str = "",
    target_pages: int = 0,
) -> dict:
    """
    Set or update the design specification (spec_lock) for a PPT SVG session.

    Partial updates are supported — only provided fields are changed.
    """
    import json

    store = get_store()
    session = store.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session '{session_id}' not found or expired."}
    if session.doc_type != "ppt_svg":
        return {
            "success": False,
            "error": f"Session is type '{session.doc_type}', not 'ppt_svg'.",
        }
    if session.status != "DRAFT":
        return {"success": False, "error": "Session is already finalized/exported."}

    # Parse and merge each JSON field
    KNOWN_COLOR_KEYS = {'primary', 'secondary', 'accent', 'background', 'text', 'text_light'}
    KNOWN_FONT_KEYS = {'title', 'body', 'title_size', 'body_size'}
    KNOWN_LAYOUT_KEYS = {'margins', 'title_area', 'content_flow'}

    if colors_json:
        try:
            colors = json.loads(colors_json)
            if not isinstance(colors, dict):
                return {"success": False, "error": "colors_json must be a JSON object"}
            unknown = set(colors) - KNOWN_COLOR_KEYS
            if unknown:
                return {"success": False, "error": f"Unknown color keys: {unknown}"}
            session.spec_lock.setdefault('colors', {}).update(colors)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid colors_json: {e}"}

    if fonts_json:
        try:
            fonts = json.loads(fonts_json)
            if not isinstance(fonts, dict):
                return {"success": False, "error": "fonts_json must be a JSON object"}
            unknown = set(fonts) - KNOWN_FONT_KEYS
            if unknown:
                return {"success": False, "error": f"Unknown font keys: {unknown}"}
            session.spec_lock.setdefault('fonts', {}).update(fonts)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid fonts_json: {e}"}

    if layout_json:
        try:
            layout = json.loads(layout_json)
            if not isinstance(layout, dict):
                return {"success": False, "error": "layout_json must be a JSON object"}
            unknown = set(layout) - KNOWN_LAYOUT_KEYS
            if unknown:
                return {"success": False, "error": f"Unknown layout keys: {unknown}"}
            session.spec_lock.setdefault('layout', {}).update(layout)
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid layout_json: {e}"}

    if canvas_format:
        valid_formats = ("ppt169", "ppt43", "ppt1610")
        if canvas_format not in valid_formats:
            return {
                "success": False,
                "error": f"Unknown canvas_format '{canvas_format}'. "
                         f"Choose: {', '.join(valid_formats)}",
            }
        session.canvas_format = canvas_format

    if target_pages > 0:
        session.spec_lock['meta'] = session.spec_lock.get('meta', {})
        session.spec_lock['meta']['target_pages'] = target_pages

    return {
        "success": True,
        "spec_lock": session.spec_lock,
        "canvas_format": session.canvas_format,
        "hint": "Design spec updated. Call get_design_context() to get "
                "formatted text for LLM prompt injection.",
    }


def get_design_context(session_id: str) -> dict:
    """
    Return the current design specification as formatted text ready for
    injection into the LLM's system prompt at the start of each generation segment.
    """
    store = get_store()
    session = store.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session '{session_id}' not found or expired."}
    if session.doc_type != "ppt_svg":
        return {
            "success": False,
            "error": f"Session is type '{session.doc_type}', not 'ppt_svg'.",
        }

    spec = session.spec_lock
    lines = ["## Design Specification (spec_lock)\n"]

    # Colors
    colors = spec.get('colors', {})
    if colors:
        lines.append("### Colors")
        for key in ('primary', 'secondary', 'accent', 'background', 'text', 'text_light'):
            if key in colors:
                lines.append(f"- {key}: {colors[key]}")
        lines.append("")

    # Fonts
    fonts = spec.get('fonts', {})
    if fonts:
        lines.append("### Fonts")
        for key in ('title', 'body', 'title_size', 'body_size'):
            if key in fonts:
                lines.append(f"- {key}: {fonts[key]}")
        lines.append("")

    # Layout
    layout = spec.get('layout', {})
    if layout:
        lines.append("### Layout")
        for key in ('margins', 'title_area', 'content_flow'):
            if key in layout:
                lines.append(f"- {key}: {layout[key]}")
        lines.append("")

    # Canvas
    fmt_dims = {
        "ppt169": "1280x720 (16:9)",
        "ppt43": "1024x768 (4:3)",
        "ppt1610": "1280x800 (16:10)",
    }
    lines.append(f"### Canvas")
    lines.append(f"- format: {session.canvas_format} ({fmt_dims.get(session.canvas_format, 'unknown')})")
    lines.append("")

    # Target pages
    meta = spec.get('meta', {})
    if meta.get('target_pages'):
        lines.append(f"### Target")
        lines.append(f"- target_pages: {meta['target_pages']} slides")
        lines.append(f"- Plan your slide outline to hit exactly this number.")
        lines.append("")

    # Image path hint
    lines.append("- Use `mock_image(w=800, h=500, color_hex=\"#palette\", label=\"desc\", output_name=\"img.png\")` to generate placeholder images.")
    lines.append("- Reference in SVG: `<image href=\"../../img.png\" x=\"..\" y=\"..\" w=\"..\" h=\"..\"/>`")
    lines.append("")

    # Segment history (for segments 2+)
    if session.segment_history:
        lines.append("## Previous Segments Summary\n")
        for seg in session.segment_history:
            seg_id = seg.get('segment', '?')
            pages = seg.get('pages', '?')
            lines.append(f"### Segment {seg_id} (slides {pages})")
            for slide in seg.get('slides', []):
                p = slide.get('page', '?')
                fp = slide.get('fingerprint', '')
                lines.append(f"- Slide {p}: {fp}")
            lines.append("")

    context = "\n".join(lines)

    return {
        "success": True,
        "design_context": context,
        "spec_lock": session.spec_lock,
        "canvas_format": session.canvas_format,
        "slide_count": len(session.svg_files),
        "segment_count": len(session.segment_history),
        "hint": "Inject the design_context text into your system prompt "
                "before generating SVG slides for the current segment.",
    }


def svgs_to_pptx(
    session_id: str,
    output_name: str = "",
) -> dict:
    """
    Convert all accumulated SVG slides to a native PPTX file.

    Each SVG element becomes a native PowerPoint shape (DrawingML).
    This auto-finalizes and exports the session in one step.
    """
    store = get_store()
    session = store.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session '{session_id}' not found or expired."}
    if session.doc_type != "ppt_svg":
        return {
            "success": False,
            "error": f"Session is type '{session.doc_type}', not 'ppt_svg'.",
        }
    if session.status == "EXPORTED":
        return {"success": False, "error": "Document already exported."}
    if not session.svg_files:
        return {"success": False, "error": "No SVG slides created. Use create_svg_slide first."}

    # Auto-finalize
    if session.status == "DRAFT":
        session.status = "FINALIZED"

    # Determine output path
    ext = ".pptx"
    fname = output_name or f"{session.title or 'presentation'}{ext}"
    if not fname.endswith(ext):
        fname += ext

    _require_security()
    raw_path = store.output_dir / fname
    # 按 `"write"` 校验 —— 下面就是 `save()`。
    out_path = _security.validate_path(str(raw_path), "write")  # type: ignore[union-attr]

    # Lazy-import the vendored converter
    try:
        from .pptx_export import create_pptx_with_native_svg
    except ImportError as e:
        return {
            "success": False,
            "error": f"PPTX export module not available: {e}. "
                     "Ensure python-pptx is installed: pip install python-pptx",
        }

    # Collect sorted SVG paths
    svg_paths = [
        Path(session.svg_dir) / f"slide_{n:03d}.svg"
        for n in sorted(session.svg_files)
    ]

    try:
        _drain_font_substitutions()      # 清场，只数这一轮
        success = create_pptx_with_native_svg(
            svg_files=svg_paths,
            output_path=out_path,
            canvas_format=session.canvas_format,
            use_native_shapes=True,
            use_compat_mode=False,
            verbose=False,
        )
    except Exception as e:
        return {
            "success": False,
            "error": f"PPTX conversion failed: {type(e).__name__}: {e}",
        }

    if not success:
        return {"success": False, "error": "PPTX conversion returned failure status."}

    session.status = "EXPORTED"
    session.output_path = str(out_path)

    out = {
        "success": True,
        "file_path": str(out_path),
        "slide_count": len(session.svg_files),
        "hint": f"PPTX exported with {len(session.svg_files)} native-shape slides. "
                "Tell the user the file path.",
    }
    _notes = _font_substitution_notes()          # 字体替换记录
    if _notes:
        out["notes"] = _notes
    return out


# 幻灯片版式 —— 规格全在 `layout_spec.py` 的「幻灯片」段

#: `a:rPr` 子元素的顺序（DrawingML 的 schema 是有序的）。
#: 这就是"为什么不直接 append"：append 会排到 `a:hlinkClick` 后面，那是坏的。
#: （`a:sz` 那一类是属性，不在这个序列里。）
_RPR_ORDER = (
    "a:ln", "a:noFill", "a:solidFill", "a:gradFill", "a:blipFill",
    "a:pattFill", "a:grpFill", "a:effectLst", "a:effectDag", "a:highlight",
    "a:uLnTx", "a:uLn", "a:uFillTx", "a:uFill", "a:latin", "a:ea", "a:cs",
    "a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst",
)


def _ensure_rpr_child(rPr, tag: str):
    """在 `a:rPr` 里拿/建一个有序的子元素。"""
    from pptx.oxml.ns import qn
    el = rPr.find(qn(tag))
    if el is None:
        el = rPr.makeelement(qn(tag), {})
        idx = _RPR_ORDER.index(tag)
        rPr.insert_element_before(el, *_RPR_ORDER[idx + 1:])
    return el


def _deck_run(run, size_pt: float, color_hex: str) -> None:
    """一处把字号 / 颜色 / 字体族全落下去（latin + ea + cs 三处同值）。

    与 Word 侧 `_apply_doc_fonts` 是同一条判据：只设 `font.name` 不够 ——
    `python-pptx` 只写 `<a:latin>`，而汉字归 `<a:ea>` 管；
    只写 latin 的症状是"西文变了、汉字没变"，看着像没生效。
    `python-pptx`（1.0.2）没有 `a:ea` 的属性（只建模了 `a:latin`）
自己插，且按 schema 顺序插。
    """
    from pptx.util import Pt
    from pptx.dml.color import RGBColor
    run.font.size = Pt(size_pt)
    if color_hex:
        run.font.color.rgb = RGBColor.from_string(color_hex)
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:latin", "a:ea", "a:cs"):
        _ensure_rpr_child(rPr, tag).set("typeface", _spec.FONT)


def _deck_geometry(prs, slide) -> None:
    """把这一页的每个元素收进安全边距里。

    判据：投影会裁边、有的会议室还在用 4:3 的机器放 16:9 的片子 ——
    元素贴到页边就会被切掉一条。
    这件事必须由生产者做，不能只写在规格里：`Presentation()` 的默认
    版式是 4:3 时代留下的，标题框在 0.3in（0.76cm） 处，比规格的
    0.5in 安全边距更靠上 —— 只写规格不落下去，体检器就会每页都报红，
    而一个每次都报红的体检器很快就不会再有人看。
    先缩（装不下就缩到装得下）再挪（挪进边界内），两步都要有：
    只挪不缩的话，一个比安全区还大的框会被推到负坐标去。
    """
    from pptx.util import Cm as _PptCm
    lo = _PptCm(_spec.DECK_MARGIN_CM)
    hi_x = prs.slide_width - lo
    hi_y = prs.slide_height - lo
    max_w, max_h = hi_x - lo, hi_y - lo
    for shape in slide.shapes:
        left, top = shape.left, shape.top
        wid, hei = shape.width, shape.height
        if None in (left, top, wid, hei):
            continue                     # 读不到尺寸的形状（少见）不猜
        if wid > max_w:
            wid = max_w
        if hei > max_h:
            hei = max_h
        if left < lo:
            left = lo
        if top < lo:
            top = lo
        if left + wid > hi_x:
            left = hi_x - wid
        if top + hei > hi_y:
            top = hi_y - hei
        shape.left, shape.top = left, top
        shape.width, shape.height = wid, hei


def _apply_deck_spec(prs, slide) -> None:
    """把 `layout_spec` 的「幻灯片」段落到这一页上。

    调用时机是"文字都填完之后" —— `tf.text = "..."` 是整段替换，
    先上样式再填字会把刚设好的 run 属性一起冲掉（症状是"设了没用"）。
    表格不在 `has_text_frame` 里（它是 GraphicFrame），单独一遍。
    """
    _deck_geometry(prs, slide)
    title = slide.shapes.title
    # 比 `_element`，不比包装对象 —— `slide.shapes.title` **每次访问
    #   都新建一个 `SlidePlaceholder`**（python-pptx 的代理是现造的），
    #   所以 `shape is title` 恒为假：症状是"标题全部按正文上了样式"
    #   （症状：四页都报"最大的字号只有 18.0pt"）。
    #   两个包装对象指向的是同一个 XML 元素，那才是这一页里唯一的那个。
    title_el = None if title is None else title._element
    for shape in slide.shapes:
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                for cell in row.cells:
                    for para in cell.text_frame.paragraphs:
                        for run in para.runs:
                            _deck_run(run, _spec.DECK_BODY_PT, _spec.INK)
            continue
        if not shape.has_text_frame:
            continue
        is_title = title_el is not None and shape._element is title_el
        size = _spec.DECK_TITLE_PT if is_title else _spec.DECK_BODY_PT
        color = _spec.ACCENT if is_title else _spec.INK
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                _deck_run(run, size, color)


def _render_ppt(session: DocSession, out_path: Path) -> None:
    """把累积的 `add_slide` 块渲染成 .pptx（`doc_type="ppt"` 那条路）。

    本函数必须存在且可运行：`export_document` 的
    `elif session.doc_type == "ppt": _render_ppt(...)` 分支会直接调用它，
    缺失时那条分支每次调用都抛 `NameError`。

    manifest 声明了、模型搜得到、`create_document` 的 enum 收录了 `"ppt"`，
    这些都不能证明这条渲染路能跑。`skill_doctor` 的 `ast.parse` 也抓不到
    "调用了一个不存在的名字"，唯一能确认的是实际跑一次。
    机械读者：`tests/test_office_write.py::test_export_document_renders_ppt`。
    """
    from pptx import Presentation
    from pptx.util import Cm as _PptCm

    prs = Presentation()

    # 画布来自 `layout_spec` 的「幻灯片」段。
    #   `Presentation()` 的默认模板是 4:3（25.4 × 19.05） ——
    #   不显式设这两行，产出的每一份都是 4:3，
    #   投到 16:9 的幕布上左右各留一条黑边。
    prs.slide_width = _PptCm(_spec.DECK_WIDTH_CM)
    prs.slide_height = _PptCm(_spec.DECK_HEIGHT_CM)

    for slide_block in session.blocks:
        st = slide_block.get("type", "content")
        title = slide_block.get("title", "")

        # 版式：标题页用 0，其余用 1（Title and Content）
        layout = prs.slide_layouts[0] if st == "title" else prs.slide_layouts[1]
        slide = prs.slides.add_slide(layout)

        if title and slide.shapes.title:
            slide.shapes.title.text = title

        if st == "content":
            bullets = slide_block.get("bullets", [])
            if bullets and len(slide.placeholders) > 1:
                tf = slide.placeholders[1].text_frame
                tf.clear()
                for i, bullet in enumerate(bullets):
                    if i == 0:
                        tf.text = bullet
                    else:
                        p = tf.add_paragraph()
                        p.text = bullet
            elif not title and len(slide.placeholders) > 1:
                # `add_slide(type="content", title="")` 是合法的 —— 不加这句的话
                # 空标题页会留一个写着 "Click to add title" 的占位符，导出即带出去。
                slide.placeholders[1].text_frame.text = ""

        elif st == "image":
            img_path = slide_block.get("image_path", "")
            if img_path:
                resolved = Path(img_path)
                if not resolved.is_absolute():
                    # 与 `add_block` 的图片分支逐字同款：
                    # 相对路径一律落 `workspace/`，不写死某个具体目录。
                    resolved = find_in_roots(resolved.name) or (lookup_roots()[0] / resolved.name)
                if resolved.exists() and len(slide.placeholders) > 1:
                    ph = slide.placeholders[1]
                    try:
                        ph.insert_picture(str(resolved))
                    except Exception:
                        # 版式 1 的 idx=1 是 body 而不是 picture placeholder，
                        # `insert_picture` 会 AttributeError。退回 `add_picture`，
                        # 自己按版心定位 —— 图片进得去比"用哪一版版式"重要。
                        slide.shapes.add_picture(
                            str(resolved), prs.slide_width // 6,
                            prs.slide_height // 4,
                            width=prs.slide_width * 2 // 3)

        elif st == "table":
            rows = slide_block.get("rows", [])
            if rows:
                nrows = len(rows)
                ncols = max(len(r) for r in rows)
                # 不能用 `placeholders[1].insert_table` ——
                # `SlidePlaceholder` 没有这个方法（会抛 `AttributeError`）。
                # 表格走自己的形状 API，且要自己给矩形。
                table = slide.shapes.add_table(
                    nrows, ncols,
                    prs.slide_width // 8, prs.slide_height // 3,
                    prs.slide_width * 3 // 4, prs.slide_height // 2,
                ).table
                for i, row_data in enumerate(rows):
                    for j, cell_val in enumerate(row_data):
                        if j < ncols:
                            table.cell(i, j).text = str(cell_val)

        _apply_deck_spec(prs, slide)

    prs.save(str(out_path))


def _render_ppt_svg(session: DocSession, out_path: Path) -> list:
    """Render a ppt_svg session to native PPTX via the vendored converter.

    返回这一轮被换掉的字体族（给调用方放进回执）——
    只往控制台打的话 WebUI 那边一个字都看不到，
    而「跑过了」与「没跑过」必须分得开。
    """
    from .pptx_export import create_pptx_with_native_svg

    svg_paths = [
        Path(session.svg_dir) / f"slide_{n:03d}.svg"
        for n in sorted(session.svg_files)
    ]

    if not svg_paths:
        raise ValueError("No SVG slides in session")

    _drain_font_substitutions()          # 清场，只数这一轮
    ok = create_pptx_with_native_svg(
        svg_files=svg_paths,
        output_path=out_path,
        canvas_format=session.canvas_format,
        use_native_shapes=True,
        use_compat_mode=False,
        verbose=False,
    )
    if not ok:
        raise RuntimeError("PPTX conversion failed")
    return _font_substitution_notes()
