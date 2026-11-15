"""
Word document manipulation tools for office_parser skill.

Ported from Office-Word-MCP-Server and adapted to Crannia conventions.
MIT License, Copyright (c) 2025 GongRzhe.
License text: `references/LICENSE-Office-Word-MCP-Server.txt`.
All functions return {"success": True/False, ...} dicts.

Dependencies: python-docx (already required by office_parser).
"""

from __future__ import annotations

import json
import logging
import os
from copy import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 落点口径只有一处 —— 见 `core/workspace.py` 那段。
from core.workspace import primary_output_dir
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml
from docx.shared import Inches, Pt, RGBColor

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

#: 只读语义的 op —— 与 `SecurityGuard.validate_path` 的 Level 1 白名单同一份
#: （那里写着"只读路径只放 read 和 copy"）。这份集合决定两件事：过不过核、
#: 以及要不要建输出目录。
_READ_OPS = ("read", "copy")


def _resolve_path(file_path: str, op: str = "write") -> Path:
    """把工具参数里的路径解析成真实路径。

    前缀守卫必须拿同一种形态比：拿相对路径 `p` 去和
    绝对路径 `ws` 比前缀时，条件恒为 False，守卫形同虚设，
    `workspace/x.docx` 会被拼成 `workspace/workspace/x.docx` → "文档不存在"。
    模型从 `list_workspace_files` 拿到 `workspace/xxx.docx` 原样回传时，
    就会得到这种"文档不存在"。

    这个函数同时是安全校验点，`op` 必填语义见下。

    ## `op` —— 每个调用点都要显式写出来

    这个函数是 36 个读写点的共同收口，而它们要的围栏不是同一道：

    | 语义 | 处数 | 例 | op |
    |---|---|---|---|
    | 纯读 | 6 | `get_document_text` · `get_document_outline` · `get_all_comments` … | `"read"` |
    | 混合的源 | 3 | `copy_document.source_file` · `merge_documents.sf` ×2 | `"read"` |
    | 其余（写） | 27 | `add_word_*` · `set_table_*` · `format_*` · `delete_*` … | `"write"` |

    不能在这一个地方统一写成 `"write"` —— 那会把
    「读桌面上那份 docx」也拒掉，而那是个正当用法（`get_document_text`
    读跨区文件）。三类语义，不能一把 sed 全换。

    每一处都显式写、不靠默认值 —— 将来多一个读文本的工具时，
    必须有人动手分类，而不是被自动放行。默认值 `"write"` 只是给
    将来新加的函数兜底，方向是 fail-closed：漏写，多拒（看得见），
    不会 少拒（看不见）。
    机械读者：`tests/test_word_tools_guard.py`（按 AST 反查每一处都有显式 op）。

    ## `op` 还决定"要不要建输出目录"

    相对路径要先拼到输出根上，而那个根可能还不存在。以前这一行无条件建目录，
    于是 12 个声明 `read` 的工具第一次被调用时会创建一个空目录 —— 改文件内容
    一个字节都没改，但它确实是写副作用，而 `read` 那一档的契约正是
    "可以并发跑、可以被只读子 agent 放行"。

    现在只在 `op` 不属于 `_READ_OPS` 时建。读一个还不存在的路径，
    结果应该是"找不到"，不是"顺手把输出目录建出来"。
    """
    p = Path(file_path)
    if p.is_absolute():
        resolved = p
    else:
        # 已经是 workspace/... 开头 → 不要再拼一次（只剥前导 "./"）
        norm = str(p).replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        if norm == "workspace" or norm.startswith("workspace/"):
            resolved = p.resolve()
        else:
            # 写那一类 —— 落点只有一个口径
            workspace = primary_output_dir()
            if op not in _READ_OPS:
                workspace.mkdir(parents=True, exist_ok=True)
            resolved = (workspace / p).resolve()
    return _guarded(resolved, op)


#: 安全核 —— 由 `handler.init()` 转调 `init()` 注入。
#: `skill_loader` 调的是 handler 的 `init`（子模块它不认识），
#: 所以那个 `init` 里必须多一行 `word_tools.init(security)`，否则这里恒为 None
#:下面 `_guarded` 每次调用都会 raise（fail-closed，症状是"所有 Word 工具全挂"，
#: 一眼可辨 —— 比静默放行好）。
_security = None


def init(security) -> None:
    """注入安全核（由 `handler.init()` 转调）。"""
    global _security
    _security = security


def _guarded(resolved: Path, op: str) -> Path:
    """按 *op* 过安全核。

    没注入安全核就 raise，与 `handler._require_security()` 同一条规矩
    （fail-closed）。不要"没核就放行、保持兼容"—— 那是坏形状：
    放行是静默的，拒绝才看得见。
    """
    if _security is None:
        raise RuntimeError(
            "SecurityGuard not initialized for word_tools. "
            "skill_loader must call handler.init() before any Word tool runs.")
    guarded = Path(_security.validate_path(str(resolved), op))
    if op == "read":
        _note_observed(guarded)
    return guarded


def _note_observed(path: Path) -> None:
    """产物侧的读态记账 —— 见 `core/file_tools.note_observed`。

    挂在这一处（路径解析收口）而不是逐个读工具里：`_guarded` 是
      Word + PPT 全族的共同收口（`ppt_tools._resolve_path` 转调本模块），
      一处覆盖 36 个调用点。
    已知取舍：记的是"解析过这个路径"而不是"内容真交出去了"
极少量过度记账（解析成功但随后读失败）。方向偏松（少拦一次），
      这是有意选的：宁可少拦一次，也不要因为记账时机太窄而漏掉整族工具。
    """
    try:
        from core.file_tools import note_observed
        note_observed(path)
    except Exception as exc:                                  # noqa: BLE001
        # 记账失败不许影响读取（同 `core/security._note_touched` 的规矩）
        print("[office] 读态记账失败（不影响读取）：%s: %s"
              % (type(exc).__name__, exc))


def _ensure_docx(filename: str) -> str:
    if not filename.endswith(".docx"):
        return filename + ".docx"
    return filename


def _overwrite_blocked(path: Path) -> Optional[dict]:
    """整块覆盖一个可能已存在的 .docx 之前，问一句「你这个读数还新鲜吗」。

    见 `core/file_tools.overwrite_guard` —— 判据是：
    目标不存在，放行 · 没读过，拒 · 读过但变了，拒 · 读过且没变，覆盖。

    只挂"整块覆盖"类工具（`merge_documents` / `copy_document`）——
      `add_word_paragraph` 这类自己 load 新鲜版本再改的工具不要求先读。
    异常一律放行并出声：这是检测不是边界，
      而且它唯一的产出只是一次拒绝 —— 让它因为自身出错而拦住所有人的活，
      方向反了（安全机制不许妨碍把事做完）。
    """
    try:
        from core.file_tools import overwrite_guard
        return overwrite_guard(path, reader="get_document_text")
    except Exception as exc:                                  # noqa: BLE001
        print("[office] 覆盖闸出错（已放行）：%s: %s" % (type(exc).__name__, exc))
        return None


def _check_writable(path: Path) -> tuple[bool, str]:
    if path.exists():
        try:
            with open(path, "ab") as f:
                pass
            return True, ""
        except Exception as e:
            return False, f"文件不可写: {e}"
    return True, ""


# Document Management

def get_document_info(file_path: str) -> dict:
    """Get metadata about a Word document: title, author, paragraphs, tables, size."""
    path = _resolve_path(file_path, "read")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        props = doc.core_properties
        stats = {
            "filename": path.name,
            "size_kb": round(path.stat().st_size / 1024, 1),
            "title": props.title or "",
            "author": props.author or "",
            "paragraphs": len(doc.paragraphs),
            "tables": len(doc.tables),
            "sections": len(doc.sections),
        }
        return {"success": True, **stats}
    except Exception as e:
        return {"success": False, "error": f"读取文档信息失败: {type(e).__name__}: {e}"}


def get_document_text(file_path: str) -> dict:
    """Extract all text from a Word document."""
    path = _resolve_path(file_path, "read")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        texts = [p.text for p in doc.paragraphs if p.text.strip()]
        return {"success": True, "paragraphs": len(texts), "text": "\n\n".join(texts)}
    except Exception as e:
        return {"success": False, "error": f"提取文本失败: {type(e).__name__}: {e}"}


def get_document_outline(file_path: str) -> dict:
    """Get heading structure of a Word document."""
    path = _resolve_path(file_path, "read")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        outline = []
        for i, p in enumerate(doc.paragraphs):
            if p.style and p.style.name.startswith("Heading "):
                try:
                    level = int(p.style.name.split(" ")[1])
                except (ValueError, IndexError):
                    level = 1
                outline.append({"level": level, "text": p.text, "paragraph_index": i})
        return {"success": True, "headings": outline, "total": len(outline)}
    except Exception as e:
        return {"success": False, "error": f"提取大纲失败: {type(e).__name__}: {e}"}


def list_available_documents(directory: str = ".") -> dict:
    """List all .docx files in a directory."""
    try:
        d = Path(directory)
        if not d.exists():
            # 默认目录走唯一口径（不写字面量 `workspace`）
            d = primary_output_dir()
        docx_files = sorted(d.glob("*.docx"))
        files = [{"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1)} for f in docx_files]
        return {"success": True, "directory": str(d), "count": len(files), "files": files}
    except Exception as e:
        return {"success": False, "error": f"列出文档失败: {type(e).__name__}: {e}"}


def copy_document(source_file: str, destination_file: Optional[str] = None) -> dict:
    """Create a copy of a Word document."""
    src = _resolve_path(source_file, "read")
    src = Path(_ensure_docx(str(src)))
    if not src.exists():
        return {"success": False, "error": f"源文档不存在: {source_file}"}

    if destination_file:
        dst = _resolve_path(destination_file, "write")
        dst = Path(_ensure_docx(str(dst)))
    else:
        # 默认目标 = 源文件同目录的 `<名>_copy.docx`。
        #
        # 它同样要过写入闸 —— 默认目标和显式目标必须是同一道围栏。
        #   修前这里直接 `src.parent / ...` 拼一个路径就去 `shutil.copy2`，
        #   `_guarded` 一次都没调：源文件读得到（`classify_path` 兜底 READ_ONLY
        #   允许 `read`），但那个目录写不得 —— 于是「读桌面上的 docx、
        #   默认复制一份」就成了一条绕开写语义的路。
        #   READ_ONLY 区的写现在是一条审批（`WriteNeedsApproval`），不是硬拒；
        #   过这道闸之后，它要么被批过、要么弹一次 —— 与显式目标完全一致。
        dst = _resolve_path(str(src.parent / f"{src.stem}_copy{src.suffix}"), "write")

    # `shutil.copy2` 对已存在目标是无条件覆盖。
    blocked = _overwrite_blocked(dst)
    if blocked:
        return {"success": False, **blocked}

    try:
        import shutil
        shutil.copy2(str(src), str(dst))
        _note_observed(dst)          # 写成功之后回记
        return {"success": True, "source": str(src), "destination": str(dst)}
    except Exception as e:
        return {"success": False, "error": f"复制文档失败: {type(e).__name__}: {e}"}


def merge_documents(
    target_file: str,
    source_files: List[str],
    add_page_breaks: bool = True,
) -> dict:
    """Merge multiple Word documents into one.

    Args:
        target_file: Output filename.
        source_files: List of source .docx files.
        add_page_breaks: Insert page breaks between documents.
    """
    target = _resolve_path(target_file, "write")
    target = Path(_ensure_docx(str(target)))

    # 本工具是整本盲覆盖 —— `Document()` 从头新建（见下）、
    #   从不 load 已存的 target、只校验 source 存在。目标名撞上另一个对话
    #   正在改的文档，就会把它整本换掉且不报错。闸挂在这里。
    blocked = _overwrite_blocked(target)
    if blocked:
        return {"success": False, **blocked}

    # Validate sources
    missing = []
    for sf in source_files:
        sp = _resolve_path(sf, "read")
        sp = Path(_ensure_docx(str(sp)))
        if not sp.exists():
            missing.append(sf)

    if missing:
        return {"success": False, "error": f"以下文件不存在: {', '.join(missing)}"}

    try:
        target_doc = Document()

        for i, sf in enumerate(source_files):
            sp = _resolve_path(sf, "read")
            sp = Path(_ensure_docx(str(sp)))
            src_doc = Document(str(sp))

            if add_page_breaks and i > 0:
                target_doc.add_page_break()

            for paragraph in src_doc.paragraphs:
                new_p = target_doc.add_paragraph(paragraph.text)
                try:
                    if paragraph.style and paragraph.style.name in target_doc.styles:
                        new_p.style = target_doc.styles[paragraph.style.name]
                except Exception:
                    pass
                for run, new_run in zip(paragraph.runs, new_p.runs):
                    new_run.bold = run.bold
                    new_run.italic = run.italic
                    new_run.underline = run.underline

            # Copy tables
            for table in src_doc.tables:
                new_table = target_doc.add_table(rows=len(table.rows), cols=len(table.columns))
                try:
                    if table.style:
                        new_table.style = table.style
                except Exception:
                    pass
                for ri, row in enumerate(table.rows):
                    for ci, cell in enumerate(row.cells):
                        new_table.cell(ri, ci).text = cell.text

        target_doc.save(str(target))
        # 写成功之后回记一笔 —— 否则模型自己刚产出的这份
        #   会被下一次写当成"没读过的新面孔"或"版本对不上"。
        _note_observed(target)
        return {"success": True, "merged": len(source_files), "output": str(target)}
    except Exception as e:
        return {"success": False, "error": f"合并文档失败: {type(e).__name__}: {e}"}


# Content Creation (enhanced)

def add_word_heading(
    file_path: str,
    text: str,
    level: int = 1,
    font_name: Optional[str] = None,
    font_size: Optional[int] = None,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    border_bottom: bool = False,
) -> dict:
    """Add a formatted heading to a Word document."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    if level < 1 or level > 9:
        return {"success": False, "error": "标题级别必须在 1-9 之间"}

    writable, err = _check_writable(path)
    if not writable:
        return {"success": False, "error": err}

    try:
        doc = Document(str(path))
        try:
            heading = doc.add_heading(text, level=level)
        except Exception:
            heading = doc.add_paragraph(text)
            heading.style = doc.styles["Normal"]
            if heading.runs:
                run = heading.runs[0]
                run.bold = True
                sizes = {1: 16, 2: 14, 3: 12}
                run.font.size = Pt(sizes.get(level, 12))

        if any([font_name, font_size, bold is not None, italic is not None]):
            for run in heading.runs:
                if font_name:
                    run.font.name = font_name
                if font_size:
                    run.font.size = Pt(font_size)
                if bold is not None:
                    run.font.bold = bold
                if italic is not None:
                    run.font.italic = italic

        if border_bottom:
            pPr = heading._element.get_or_add_pPr()
            pBdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "4")
            bottom.set(qn("w:space"), "0")
            bottom.set(qn("w:color"), "000000")
            pBdr.append(bottom)
            pPr.append(pBdr)

        doc.save(str(path))
        return {"success": True, "message": f"标题 '{text}' (级别 {level}) 已添加"}
    except Exception as e:
        return {"success": False, "error": f"添加标题失败: {type(e).__name__}: {e}"}


def add_word_paragraph(
    file_path: str,
    text: str,
    style: Optional[str] = None,
    font_name: Optional[str] = None,
    font_size: Optional[int] = None,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    color: Optional[str] = None,
) -> dict:
    """Add a formatted paragraph to a Word document."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        paragraph = doc.add_paragraph(text)
        if style:
            try:
                paragraph.style = style
            except KeyError:
                paragraph.style = doc.styles["Normal"]

        if any([font_name, font_size, bold is not None, italic is not None, color]):
            for run in paragraph.runs:
                if font_name:
                    run.font.name = font_name
                if font_size:
                    run.font.size = Pt(font_size)
                if bold is not None:
                    run.font.bold = bold
                if italic is not None:
                    run.font.italic = italic
                if color:
                    run.font.color.rgb = _parse_color(color)

        doc.save(str(path))
        return {"success": True, "message": "段落已添加"}
    except Exception as e:
        return {"success": False, "error": f"添加段落失败: {type(e).__name__}: {e}"}


def apply_table_of_contents(
    file_path: str,
    kind: str = "toc",
    label: str = "",
    paragraph_index: Optional[int] = None,
    replace_text: str = "",
    max_level: int = 3,
    hyperlink: bool = True,
    title: str = "",
) -> dict:
    """插入目录 / 图目录 / 表目录 —— 写进去的是真域（`TOC`），不是静态清单。

    它做的是「可以更新」那一半：支持替换占位文字、指定段落位置、
    显示级别与超链接开关。

    | `kind` | 域指令 | 收什么 |
    |---|---|---|
    | `toc` | `TOC \\o "1-3" \\h \\z \\u` | 标题样式（`\\o` 管显示级别） |
    | `figure` | `TOC \\h \\z \\c "图"` | 题注（`\\c` 按题注标签收） |
    | `table` | `TOC \\h \\z \\c "表"` | 同上 |

    `\\c` 与 `\\o` 不许混：`\\c "图"` 收的是题注，混进 `\\o` 会把标题也一起收进来。
    所以图目录的前提是那些图标题已经是题注 —— 手写编号
    `图1 系统架构` 不会进图目录，得先 `replace_manual_captions`。

    域放在哪儿（三支按序判）：
      · `replace_text` 非空，找含这段文字的段落，就地把它换成一个域
        （「替换『请在此插入目录』的文字」就是这一支）
      · `paragraph_index` 给了，在那一段之后另起一段放域
        （「在『目录』文字后插入目录」是这一支）
      · 都不给，插在正文最前

    不要退回到"静态清单"：把全文抄进新文档、再把标题文字拼成清单，
    会丢掉图片、分节与页眉页脚，而且那张清单永远不会更新。

    域要排版引擎算过才有内容：Word/WPS 打开时会提示更新（我们顺手开了
    `updateFields`），LibreOffice 自己重算；没有引擎的地方看到的是占位符
    （占位符必须非空 —— 留空会让那处看起来像「写丢了」）。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    _alias = {"目录": "toc", "正文目录": "toc",
              "图目录": "figure", "图表目录": "figure", "图": "figure",
              "表目录": "table", "表格目录": "table", "表": "table"}
    k = str(kind or "toc").strip().lower()
    k = _alias.get(k, k)
    if k not in ("toc", "figure", "table"):
        return {"success": False,
                "error": "kind 只认 toc / figure / table（也收中文 目录 / 图目录 / 表目录），"
                         f"收到 {kind!r}"}

    lab = label.strip() or ("图" if k == "figure" else ("表" if k == "table" else ""))
    try:
        lvl = int(max_level)
    except (TypeError, ValueError):
        return {"success": False, "error": f"max_level 要是整数，收到 {max_level!r}"}
    if not (1 <= lvl <= 9):
        return {"success": False, "error": f"max_level 要在 1-9 之间，收到 {lvl}"}

    _h = " \\h" if hyperlink else ""
    if k == "toc":
        instr = ' TOC \\o "1-%d"%s \\z \\u ' % (lvl, _h)
        what = "目录"
    else:
        instr = ' TOC%s \\z \\c "%s" ' % (_h, lab)
        what = f"{lab}目录"

    try:
        doc = Document(str(path))
        from docx.text.paragraph import Paragraph as _P
        target = None
        span = None
        where_idx = None

        if replace_text:
            for i, para in enumerate(doc.paragraphs):
                pos = (para.text or "").find(replace_text)
                if pos >= 0:
                    target, span, where_idx = para, (pos, pos + len(replace_text)), i
                    break
            if target is None:
                return {"success": False,
                        "error": f"文档里找不到 {replace_text!r} —— 要么改用 "
                                 f"paragraph_index，要么先确认那段占位文字确实在文档里"}
        elif paragraph_index is not None:
            try:
                pi = int(paragraph_index)
            except (TypeError, ValueError):
                return {"success": False,
                        "error": f"paragraph_index 要是整数，收到 {paragraph_index!r}"}
            if not (0 <= pi < len(doc.paragraphs)):
                return {"success": False,
                        "error": f"段落索引无效（文档有 {len(doc.paragraphs)} 段，0 起）"}
            src = doc.paragraphs[pi]
            holder = OxmlElement("w:p")
            src._p.addnext(holder)
            target = _P(holder, src._parent)
            where_idx = pi + 1
        else:
            if not doc.paragraphs:
                return {"success": False, "error": "文档里一个段落都没有，没地方放目录"}
            holder = OxmlElement("w:p")
            first = doc.element.body.find(qn("w:p"))
            if first is not None:
                first.addprevious(holder)
            else:
                doc.element.body.append(holder)
            target = _P(holder, doc.paragraphs[0]._parent)
            where_idx = 0

        rpr = _rpr_copy(target.runs[0]) if target.runs else None
        field = _field_runs(instr, _TOC_PLACEHOLDER, rpr)
        if span is not None:
            _splice_span(target, span[0], span[1], field)
        else:
            for r in field:
                target._p.append(r)

        if title.strip():
            tp = OxmlElement("w:p")
            target._p.addprevious(tp)
            _P(tp, target._parent).add_run(title.strip())

        _enable_update_fields(doc)
        doc.save(str(path))
        where = (f"已用域**替换**了第 {where_idx} 段里的 {replace_text!r}"
                 if replace_text else f"已在第 {where_idx} 段放好域段落")
        return {"success": True, "kind": k, "label": lab, "instruction": instr.strip(),
                "replace_text": replace_text,
                "message": (f"{where}：{what}（域指令 `{instr.strip()}`）。"
                            + ("" if k == "toc" else
                               f"⚠️ 只有**题注**才收得进来 —— 手写编号要先过 "
                               f"`replace_manual_captions`。")
                            + "⚠️ 域要有内容得让引擎算一次：Word/WPS 打开时会提示更新"
                              "（我们开了 `updateFields`），LibreOffice 自己重算；"
                              "在那之前那一处显示的是占位符。")}
    except Exception as e:
        return {"success": False, "error": f"插入目录失败: {type(e).__name__}: {e}"}


def delete_paragraph(file_path: str, paragraph_index: int) -> dict:
    """Delete a paragraph by index (0-based)."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if paragraph_index < 0 or paragraph_index >= len(doc.paragraphs):
            return {"success": False, "error": f"段落索引无效。文档有 {len(doc.paragraphs)} 个段落 (0-{len(doc.paragraphs) - 1})"}
        p = doc.paragraphs[paragraph_index]._p
        p.getparent().remove(p)
        doc.save(str(path))
        return {"success": True, "message": f"已删除段落 #{paragraph_index}"}
    except Exception as e:
        return {"success": False, "error": f"删除段落失败: {type(e).__name__}: {e}"}


def search_and_replace_text(
    file_path: str,
    find_text: str,
    replace_text: str,
) -> dict:
    """Search and replace text throughout a document."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        count = 0
        for paragraph in doc.paragraphs:
            if find_text in paragraph.text:
                for run in paragraph.runs:
                    if find_text in run.text:
                        run.text = run.text.replace(find_text, replace_text)
                        count += 1

        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        if find_text in paragraph.text:
                            for run in paragraph.runs:
                                if find_text in run.text:
                                    run.text = run.text.replace(find_text, replace_text)
                                    count += 1
        doc.save(str(path))
        if count > 0:
            return {"success": True, "message": f"已将 {count} 处 '{find_text}' 替换为 '{replace_text}'"}
        return {"success": True, "message": f"未找到 '{find_text}'"}
    except Exception as e:
        return {"success": False, "error": f"搜索替换失败: {type(e).__name__}: {e}"}


def insert_content_near_text(
    file_path: str,
    target_text: str,
    content: str,
    position: str = "after",
    style: Optional[str] = None,
) -> dict:
    """Insert a paragraph before or after a paragraph containing target text.

    Args:
        file_path: Path to .docx file.
        target_text: Text to locate the insertion point.
        content: Content to insert.
        position: 'before' or 'after' the target paragraph.
        style: Optional Word style name for the new paragraph.
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    if position not in ("before", "after"):
        return {"success": False, "error": "position 必须是 'before' 或 'after'"}

    try:
        doc = Document(str(path))
        # Find target paragraph
        target_idx = None
        for i, p in enumerate(doc.paragraphs):
            if target_text in p.text:
                target_idx = i
                break

        if target_idx is None:
            return {"success": False, "error": f"未找到包含 '{target_text}' 的段落"}

        # Insert the new paragraph by manipulating the XML
        new_para = doc.add_paragraph(content)
        if style:
            try:
                new_para.style = style
            except KeyError:
                pass

        target_element = doc.paragraphs[target_idx]._p
        new_element = new_para._p
        parent = target_element.getparent()

        if position == "before":
            parent.insert(list(parent).index(target_element), new_element)
        else:
            parent.insert(list(parent).index(target_element) + 1, new_element)

        doc.save(str(path))
        return {"success": True, "message": f"已在 '{target_text}' {position} 处插入内容"}
    except Exception as e:
        return {"success": False, "error": f"插入内容失败: {type(e).__name__}: {e}"}


# Rich Text Formatting

_COLOR_MAP = {
    "red": RGBColor(255, 0, 0), "blue": RGBColor(0, 0, 255),
    "green": RGBColor(0, 128, 0), "yellow": RGBColor(255, 255, 0),
    "black": RGBColor(0, 0, 0), "gray": RGBColor(128, 128, 128),
    "white": RGBColor(255, 255, 255), "purple": RGBColor(128, 0, 128),
    "orange": RGBColor(255, 165, 0),
}


def _parse_color(color: str) -> RGBColor:
    if color.lower() in _COLOR_MAP:
        return _COLOR_MAP[color.lower()]
    c = color.lstrip("#")
    if len(c) == 6:
        return RGBColor(int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    return RGBColor(0, 0, 0)


def format_paragraph_text(
    file_path: str,
    paragraph_index: int,
    start_pos: int,
    end_pos: int,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    underline: Optional[bool] = None,
    color: Optional[str] = None,
    font_size: Optional[int] = None,
    font_name: Optional[str] = None,
) -> dict:
    """Format a specific range of text within a paragraph.

    Args:
        file_path: Path to .docx file.
        paragraph_index: Index of the paragraph (0-based).
        start_pos: Start character position within the paragraph text.
        end_pos: End character position (exclusive).
        bold/italic/underline: Toggle formatting.
        color: Hex color or name (red, blue, etc.).
        font_size: Size in points.
        font_name: Font family name.
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if paragraph_index < 0 or paragraph_index >= len(doc.paragraphs):
            return {"success": False, "error": f"段落索引无效。文档有 {len(doc.paragraphs)} 个段落"}

        paragraph = doc.paragraphs[paragraph_index]
        text = paragraph.text
        if start_pos < 0 or end_pos > len(text) or start_pos >= end_pos:
            return {"success": False, "error": f"文本范围无效。段落有 {len(text)} 个字符"}

        target = text[start_pos:end_pos]

        # Rebuild runs: before + formatted target + after
        for run in paragraph.runs:
            run.clear()

        if start_pos > 0:
            paragraph.add_run(text[:start_pos])

        run_t = paragraph.add_run(target)
        if bold is not None:
            run_t.bold = bold
        if italic is not None:
            run_t.italic = italic
        if underline is not None:
            run_t.underline = underline
        if color:
            run_t.font.color.rgb = _parse_color(color)
        if font_size:
            run_t.font.size = Pt(font_size)
        if font_name:
            run_t.font.name = font_name

        if end_pos < len(text):
            paragraph.add_run(text[end_pos:])

        doc.save(str(path))
        return {"success": True, "message": f"已格式化段落 #{paragraph_index} 中 '{target[:30]}...'"}
    except Exception as e:
        return {"success": False, "error": f"格式化文本失败: {type(e).__name__}: {e}"}


def create_custom_style(
    file_path: str,
    style_name: str,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    font_size: Optional[int] = None,
    font_name: Optional[str] = None,
    color: Optional[str] = None,
    base_style: Optional[str] = None,
) -> dict:
    """Create a custom paragraph style in the document."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if base_style and base_style in doc.styles:
            base = doc.styles[base_style]
        else:
            base = doc.styles["Normal"]
        new_style = doc.styles.add_style(style_name, WD_STYLE_TYPE.PARAGRAPH)
        new_style.base_style = base
        font = new_style.font
        if bold is not None:
            font.bold = bold
        if italic is not None:
            font.italic = italic
        if font_size:
            font.size = Pt(font_size)
        if font_name:
            font.name = font_name
        if color:
            font.color.rgb = _parse_color(color)

        doc.save(str(path))
        return {"success": True, "message": f"样式 '{style_name}' 已创建"}
    except Exception as e:
        return {"success": False, "error": f"创建样式失败: {type(e).__name__}: {e}"}


# Table Formatting (Word)

def _set_cell_border(cell, val="single", sz="4", color="000000"):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcBorders = tcPr.first_child_found_in("w:tcBorders")
    if tcBorders is None:
        tcBorders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), val)
        el.set(qn("w:sz"), sz)
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        tcBorders.append(el)
    tcPr.append(tcBorders)


def _set_cell_shading(cell, fill_hex: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    existing = tcPr.find(qn("w:shd"))
    if existing is not None:
        tcPr.remove(existing)
    c = fill_hex.lstrip("#").upper()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{c}"/>')
    tcPr.append(shd)


def _set_cell_alignment(cell, horizontal="left", vertical="top"):
    for paragraph in cell.paragraphs:
        a = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT,
             "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(horizontal.lower(), WD_ALIGN_PARAGRAPH.LEFT)
        paragraph.alignment = a
    tcPr = cell._tc.get_or_add_tcPr()
    existing = tcPr.find(qn("w:vAlign"))
    if existing is not None:
        tcPr.remove(existing)
    ve = OxmlElement("w:vAlign")
    ve.set(qn("w:val"), {"center": "center", "bottom": "bottom"}.get(vertical.lower(), "top"))
    tcPr.append(ve)


def format_word_table(
    file_path: str,
    table_index: int,
    has_header_row: bool = False,
    border_style: Optional[str] = None,
) -> dict:
    """Apply formatting to a Word table (borders + optional header row).

    Args:
        file_path: Path to .docx file.
        table_index: Index of the table (0-based).
        has_header_row: Bold the first row.
        border_style: 'single', 'double', 'thick', 'none'.
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": f"表格索引无效。文档有 {len(doc.tables)} 个表格"}
        table = doc.tables[table_index]

        if has_header_row and table.rows:
            for cell in table.rows[0].cells:
                for p in cell.paragraphs:
                    for run in p.runs:
                        run.bold = True

        if border_style:
            val_map = {"none": "nil", "single": "single", "double": "double", "thick": "thick"}
            val = val_map.get(border_style.lower(), "single")
            for row in table.rows:
                for cell in row.cells:
                    _set_cell_border(cell, val=val)

        doc.save(str(path))
        return {"success": True, "message": f"表格 #{table_index} 已格式化"}
    except Exception as e:
        return {"success": False, "error": f"格式化表格失败: {type(e).__name__}: {e}"}


def set_table_cell_shading(
    file_path: str,
    table_index: int,
    row_index: int,
    col_index: int,
    fill_color: str,
) -> dict:
    """Apply background color to a specific table cell."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]
        if row_index < 0 or row_index >= len(table.rows):
            return {"success": False, "error": "行索引无效"}
        if col_index < 0 or col_index >= len(table.rows[row_index].cells):
            return {"success": False, "error": "列索引无效"}

        _set_cell_shading(table.rows[row_index].cells[col_index], fill_color)
        doc.save(str(path))
        return {"success": True, "message": f"单元格 ({row_index},{col_index}) 背景色已设置"}
    except Exception as e:
        return {"success": False, "error": f"设置底纹失败: {type(e).__name__}: {e}"}


def apply_table_alternating_rows(
    file_path: str,
    table_index: int,
    color1: str = "FFFFFF",
    color2: str = "F2F2F2",
) -> dict:
    """Apply alternating row colors to a Word table."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]

        for i, row in enumerate(table.rows):
            c = color1 if i % 2 == 0 else color2
            for cell in row.cells:
                _set_cell_shading(cell, c)

        doc.save(str(path))
        return {"success": True, "message": f"表格 #{table_index} 交替行颜色已应用"}
    except Exception as e:
        return {"success": False, "error": f"设置交替行失败: {type(e).__name__}: {e}"}


def highlight_table_header(
    file_path: str,
    table_index: int,
    header_color: str = "4472C4",
    text_color: str = "FFFFFF",
) -> dict:
    """Highlight the header row with background and white text."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]
        if not table.rows:
            return {"success": False, "error": "表格没有行"}

        for cell in table.rows[0].cells:
            _set_cell_shading(cell, header_color)
            for p in cell.paragraphs:
                for run in p.runs:
                    run.bold = True
                    if text_color != "auto":
                        run.font.color.rgb = _parse_color(text_color)

        doc.save(str(path))
        return {"success": True, "message": f"表格 #{table_index} 表头已高亮"}
    except Exception as e:
        return {"success": False, "error": f"高亮表头失败: {type(e).__name__}: {e}"}


def merge_table_cells(
    file_path: str,
    table_index: int,
    start_row: int,
    start_col: int,
    end_row: int,
    end_col: int,
) -> dict:
    """Merge cells in a rectangular area of a Word table."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]

        start_cell = table.cell(start_row, start_col)
        end_cell = table.cell(end_row, end_col)
        start_cell.merge(end_cell)

        doc.save(str(path))
        return {"success": True, "message": f"已合并 ({start_row},{start_col}) → ({end_row},{end_col})"}
    except Exception as e:
        return {"success": False, "error": f"合并单元格失败: {type(e).__name__}: {e}"}


def merge_table_cells_horizontal(
    file_path: str,
    table_index: int,
    row_index: int,
    start_col: int,
    end_col: int,
) -> dict:
    """Merge cells horizontally in a single row."""
    return merge_table_cells(file_path, table_index, row_index, start_col, row_index, end_col)


def merge_table_cells_vertical(
    file_path: str,
    table_index: int,
    col_index: int,
    start_row: int,
    end_row: int,
) -> dict:
    """Merge cells vertically in a single column."""
    return merge_table_cells(file_path, table_index, start_row, col_index, end_row, col_index)


def set_table_cell_alignment(
    file_path: str,
    table_index: int,
    row_index: int,
    col_index: int,
    horizontal: str = "left",
    vertical: str = "top",
) -> dict:
    """Set text alignment for a specific table cell."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]
        if row_index < 0 or row_index >= len(table.rows):
            return {"success": False, "error": "行索引无效"}
        if col_index < 0 or col_index >= len(table.rows[row_index].cells):
            return {"success": False, "error": "列索引无效"}

        _set_cell_alignment(table.rows[row_index].cells[col_index], horizontal, vertical)
        doc.save(str(path))
        return {"success": True, "message": f"单元格 ({row_index},{col_index}) 对齐已设置"}
    except Exception as e:
        return {"success": False, "error": f"设置对齐失败: {type(e).__name__}: {e}"}


def set_table_alignment_all(
    file_path: str,
    table_index: int,
    horizontal: str = "left",
    vertical: str = "top",
) -> dict:
    """Set text alignment for all cells in a table."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]

        for row in table.rows:
            for cell in row.cells:
                _set_cell_alignment(cell, horizontal, vertical)
        doc.save(str(path))
        return {"success": True, "message": f"表格 #{table_index} 全局对齐已设置"}
    except Exception as e:
        return {"success": False, "error": f"设置对齐失败: {type(e).__name__}: {e}"}


def set_table_column_width(
    file_path: str,
    table_index: int,
    col_index: int,
    width: float,
    width_type: str = "points",
) -> dict:
    """Set width of a specific table column.

    width_type: 'points', 'inches', 'cm', 'percent', 'auto'
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    valid_types = {"points", "inches", "cm", "percent", "auto"}
    if width_type not in valid_types:
        return {"success": False, "error": f"width_type 无效。支持: {', '.join(sorted(valid_types))}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]
        if col_index < 0 or col_index >= len(table.columns):
            return {"success": False, "error": "列索引无效"}

        # Convert to DXA (twentieths of a point)
        conversions = {"inches": 72, "cm": 28.35, "points": 1}
        if width_type == "auto":
            word_w, word_t = 0, "auto"
        elif width_type == "percent":
            word_w, word_t = width, "pct"
        else:
            word_w = int(width * conversions.get(width_type, 1) * 20)
            word_t = "dxa"

        for row in table.rows:
            if col_index < len(row.cells):
                cell = row.cells[col_index]
                tcPr = cell._tc.get_or_add_tcPr()
                existing = tcPr.find(qn("w:tcW"))
                if existing is not None:
                    tcPr.remove(existing)
                we = OxmlElement("w:tcW")
                we.set(qn("w:w"), str(word_w))
                we.set(qn("w:type"), word_t)
                tcPr.append(we)

        doc.save(str(path))
        return {"success": True, "message": f"列 #{col_index} 宽度已设置为 {width}{width_type}"}
    except Exception as e:
        return {"success": False, "error": f"设置列宽失败: {type(e).__name__}: {e}"}


def set_table_width(
    file_path: str,
    table_index: int,
    width: float,
    width_type: str = "percent",
) -> dict:
    """Set the overall width of a Word table."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]

        conversions = {"inches": 72, "cm": 28.35, "points": 1}
        if width_type == "percent":
            word_w, word_t = int(width * 50), "pct"
        else:
            word_w = int(width * conversions.get(width_type, 1) * 20)
            word_t = "dxa"

        tbl = table._tbl
        tblPr = tbl.find(qn("w:tblPr"))
        if tblPr is None:
            tblPr = OxmlElement("w:tblPr")
            tbl.insert(0, tblPr)
        existing = tblPr.find(qn("w:tblW"))
        if existing is not None:
            tblPr.remove(existing)
        we = OxmlElement("w:tblW")
        we.set(qn("w:w"), str(word_w))
        we.set(qn("w:type"), word_t)
        tblPr.append(we)

        doc.save(str(path))
        return {"success": True, "message": f"表格宽度已设置为 {width}{width_type}"}
    except Exception as e:
        return {"success": False, "error": f"设置表格宽度失败: {type(e).__name__}: {e}"}


def auto_fit_table(file_path: str, table_index: int) -> dict:
    """Set table columns to auto-fit based on content."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]

        tbl = table._tbl
        tblPr = tbl.find(qn("w:tblPr"))
        if tblPr is None:
            tblPr = OxmlElement("w:tblPr")
            tbl.insert(0, tblPr)
        existing = tblPr.find(qn("w:tblLayout"))
        if existing is not None:
            tblPr.remove(existing)
        le = OxmlElement("w:tblLayout")
        le.set(qn("w:type"), "autofit")
        tblPr.append(le)

        for ci in range(len(table.columns)):
            for row in table.rows:
                if ci < len(row.cells):
                    tcPr = row.cells[ci]._tc.get_or_add_tcPr()
                    ew = tcPr.find(qn("w:tcW"))
                    if ew is not None:
                        tcPr.remove(ew)
                    we = OxmlElement("w:tcW")
                    we.set(qn("w:w"), "0")
                    we.set(qn("w:type"), "auto")
                    tcPr.append(we)

        doc.save(str(path))
        return {"success": True, "message": f"表格 #{table_index} 列宽已设为自适应"}
    except Exception as e:
        return {"success": False, "error": f"自适应列宽失败: {type(e).__name__}: {e}"}


# ------------------------------------------------------------------
# 合并单元格 —— 「一个判据，两处用」
# ------------------------------------------------------------------
#
# 三层根因叠在一起：
#
#   1. 读侧不给坐标 —— 若 `parse_docx` 把表格拼成一根字符串
#      （`学院 | 材料与新能源学院 | ... | 班级 | 新材25-01`），
#      没有任何列号，而合并区的值重复出现 4 次，模型只能数格子猜列号。
#   2. 写侧要坐标，而落在合并区内部 = 改写整个合并区 ——
#      往 `r0c2`（合并区 1-4 的内部列）写 `""`，`r0c1` 也变空。
#   3."把残留旧值删掉"这种正常诉求在这套 API 上会做坏：
#      清理只能往合并区内部列写，而那是擦掉真值。
#      结果可能是三个标签被覆盖、两个值被抹掉、三个该填的格子是空的，
#      而模型写值的位置全偏、方向一致。
#
# 所以这两处要的是同一件事：这个格子在网格上的真起点与跨度。
# 一个函数，读写共用（不写两份会漂移的东西）。


def iter_grid_cells(table) -> List[List[dict]]:
    """把一个 Word 表格摊成「网格坐标 + 真格子」。

    返回 [[{"col": 起始网格列(0基), "span": 跨列数, "cell": _Cell}, ...], ...]
    每个内层列表 = 表格的一行，只含真实格子（合并区只出现一次）。

    为什么不能直接用 `row.cells`：python-docx 的 `row.cells` 按网格列
    返回 —— 一个"合并 1-4"的格子会返回 4 个一模一样的 `_Cell`。
    读侧照它拼字符串就得到"值重复 4 次且没有列号"，而那正是模型猜错列号的入口。

    跨度取自 XML 的 `w:gridSpan`。
    """
    rows: List[List[dict]] = []
    for row in table.rows:
        cells = row.cells                      # 按网格列展开（合并格会重复）
        out: List[dict] = []
        col = 0
        for idx, tc in enumerate(row._tr.tc_lst):
            span = 1
            tcPr = tc.tcPr
            if tcPr is not None:
                raw = tcPr.find(qn("w:gridSpan"))
                if raw is not None:
                    try:
                        span = max(1, int(raw.get(qn("w:val")) or 1))
                    except (TypeError, ValueError):
                        span = 1
            # `cells` 是按网格列的，所以起点列就是索引；越界时退回最后一个
            if col < len(cells):
                out.append({"col": col, "span": span, "cell": cells[col]})
            col += span
        rows.append(out)
    return rows


def cell_merge_range(table, row_index: int, col_index: int) -> tuple:
    """这个格所在的合并区 —— (r0, r1, c0, c1)，0 基、闭区间。

    没合并就是 (row_index, row_index, col_index, col_index)。

    判据是 `_tc` 同一性（同一个 `w:tc` 元素 = 同一个真实格子）——
    那正是"往这里写会连带改掉哪些格"的定义。
    与 `iter_grid_cells` 分工不同：那个回答"第几列、跨几列"（给读侧排版），
    这个回答"我这一笔会改到哪儿"（给写侧判定）。两者判的不是同一个问题。
    """
    n_rows = len(table.rows)
    if not (0 <= row_index < n_rows):
        return (row_index, row_index, col_index, col_index)
    try:
        target = table.cell(row_index, col_index)._tc
    except (IndexError, ValueError):
        return (row_index, row_index, col_index, col_index)

    def same(r: int, c: int) -> bool:
        try:
            return table.cell(r, c)._tc is target
        except (IndexError, ValueError):
            return False

    r0 = r1 = row_index
    while r0 > 0 and same(r0 - 1, col_index):
        r0 -= 1
    while r1 < n_rows - 1 and same(r1 + 1, col_index):
        r1 += 1
    c0 = c1 = col_index
    while c0 > 0 and same(r0, c0 - 1):
        c0 -= 1
    while c1 < len(table.rows[r0].cells) - 1 and same(r0, c1 + 1):
        c1 += 1
    return (r0, r1, c0, c1)


def format_table_cell_text(
    file_path: str,
    table_index: int,
    row_index: int,
    col_index: int,
    text_content: Optional[str] = None,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    underline: Optional[bool] = None,
    color: Optional[str] = None,
    font_size: Optional[int] = None,
    font_name: Optional[str] = None,
) -> dict:
    """Format text within a specific table cell."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]
        if row_index < 0 or row_index >= len(table.rows):
            return {"success": False, "error": "行索引无效"}
        if col_index < 0 or col_index >= len(table.rows[row_index].cells):
            return {"success": False, "error": "列索引无效"}

        cell = table.rows[row_index].cells[col_index]

        # 落在合并区内部列，拒，并说出它属于哪个区。
        #
        # 一个"合并 1-4"的格子，`row.cells` 返回 **4 个一模一样的
        # `_Cell`**，往 `r0c2` 写 `""`（"清理残留旧值"）把 r0c1 也擦了。
        # 而那是真值那一格 —— 先写对了 `r0c1=管理学院`，随后的清理擦掉了它。
        #
        # 判据不是"这一格在不在合并区里"（写起点是唯一正确的写法），
        # 而是"你给的这个坐标是不是起点"。
        r0, r1, c0, c1 = cell_merge_range(table, row_index, col_index)
        merged = (r0, r1, c0, c1) != (row_index, row_index, col_index, col_index)
        if merged and (row_index, col_index) != (r0, c0):
            return {
                "success": False,
                "merged_cell": {
                    "target": [row_index, col_index],
                    "region": [r0, r1, c0, c1],
                    "origin": [r0, c0],
                },
                "error": (
                    f"单元格 ({row_index},{col_index}) 在一个合并区内部 —— "
                    f"该合并区是 **行 {r0}-{r1} × 列 {c0}-{c1}**，整区共用一个值。"
                    f"写它的**内部列**会连带改掉整个合并区（会把起点那一格的"
                    f"真值一起擦掉）。要改这个格子，请把坐标写成它的**起点** "
                    f"({r0},{c0})；要「清空」它，同样写起点。"
                ),
                "hint": f"改用 row_index={r0}, col_index={c0} 重试一次即可。",
            }

        if text_content is not None:
            cell.text = str(text_content)

        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                if bold is not None:
                    run.bold = bold
                if italic is not None:
                    run.italic = italic
                if underline is not None:
                    run.underline = underline
                if color:
                    run.font.color.rgb = _parse_color(color)
                if font_size:
                    run.font.size = Pt(font_size)
                if font_name:
                    run.font.name = font_name

        doc.save(str(path))

        # 存盘之后回读。
        #
        # 治的是"工具说成功了、而文件其实是别的样子"那一半。
        # 模型即使回读过、看见了"学院 / 姓名 首列为空"，也可能把这个观测
        # 理论化掉（"这是合并单元格的读取顺序问题……实际文件里都在"）。
        #观测到了、却给了个自信的错解释。工具坏看得见，
        # 报告说"没问题"看不见。所以"回读"必须是工具做的，不能靠模型自觉。
        #
        # 回读的是合并区起点 (`r0,c0`) —— 那才是这一笔真正写到的地方。
        if text_content is not None:
            try:
                back = Document(str(path))
                got = back.tables[table_index].cell(r0, c0).text
                if got.strip() != str(text_content).strip():
                    return {
                        "success": False,
                        "verify_failed": True,
                        "error": (
                            f"写入后回读不一致：写进去的是 {str(text_content)!r}，"
                            f"而盘上 ({r0},{c0}) 读到的是 {got!r}。"
                            f"文件**已经保存**了，但**不要**告诉用户已完成 —— "
                            f"先回读确认再报。"
                        ),
                    }
            except Exception as e:
                return {"success": False,
                        "error": f"写入后回读失败（无法确认是否写进去了）: "
                                 f"{type(e).__name__}: {e}"}

        return {"success": True, "verified": True,
                "message": f"单元格 ({row_index},{col_index}) 文本已格式化"
                           f"（回读确认写入的是 ({r0},{c0})）"}
    except Exception as e:
        return {"success": False, "error": f"格式化单元格文本失败: {type(e).__name__}: {e}"}


def set_table_cell_padding(
    file_path: str,
    table_index: int,
    row_index: int,
    col_index: int,
    top: Optional[float] = None,
    bottom: Optional[float] = None,
    left: Optional[float] = None,
    right: Optional[float] = None,
    unit: str = "points",
) -> dict:
    """Set padding/margins for a specific table cell."""
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    if unit not in ("points", "percent"):
        return {"success": False, "error": "unit 必须是 'points' 或 'percent'"}

    try:
        doc = Document(str(path))
        if table_index < 0 or table_index >= len(doc.tables):
            return {"success": False, "error": "表格索引无效"}
        table = doc.tables[table_index]
        if row_index < 0 or row_index >= len(table.rows):
            return {"success": False, "error": "行索引无效"}
        if col_index < 0 or col_index >= len(table.rows[row_index].cells):
            return {"success": False, "error": "列索引无效"}

        cell = table.rows[row_index].cells[col_index]
        tcPr = cell._tc.get_or_add_tcPr()
        existing = tcPr.find(qn("w:tcMar"))
        if existing is not None:
            tcPr.remove(existing)

        sides = {"w:top": top, "w:bottom": bottom, "w:left": left, "w:right": right}
        if any(v is not None for v in sides.values()):
            margins = OxmlElement("w:tcMar")
            for side, value in sides.items():
                if value is not None:
                    me = OxmlElement(side)
                    if unit == "points":
                        me.set(qn("w:w"), str(int(value * 20)))
                        me.set(qn("w:type"), "dxa")
                    else:
                        me.set(qn("w:w"), str(int(value * 50)))
                        me.set(qn("w:type"), "pct")
                    margins.append(me)
            tcPr.append(margins)

        doc.save(str(path))
        return {"success": True, "message": f"单元格 ({row_index},{col_index}) 内边距已设置"}
    except Exception as e:
        return {"success": False, "error": f"设置内边距失败: {type(e).__name__}: {e}"}


# Comments

def get_all_comments(file_path: str) -> dict:
    """Extract all comments from a Word document."""
    path = _resolve_path(file_path, "read")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        comments = []
        try:
            for comment in doc.comments:
                comments.append({
                    "id": comment._element.get(qn("w:id"), ""),
                    "author": comment.author,
                    "date": str(comment.date) if comment.date else "",
                    "text": comment.text,
                    "initials": comment.initials,
                })
        except Exception:
            # python-docx comments API may vary; attempt XML fallback
            for c in doc._part._element.findall(".//" + qn("w:comment")):
                author = c.get(qn("w:author"), "")
                text_els = c.findall(qn("w:t"))
                text = "".join(t.text or "" for t in text_els)
                comments.append({"id": c.get(qn("w:id"), ""), "author": author, "text": text})

        return {"success": True, "comments": comments, "total": len(comments)}
    except Exception as e:
        return {"success": False, "error": f"提取评论失败: {type(e).__name__}: {e}"}


def get_comments_by_author(file_path: str, author: str) -> dict:
    """Filter comments by author name."""
    path = _resolve_path(file_path, "read")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    result = get_all_comments(str(path))
    if not result.get("success"):
        return result

    filtered = [c for c in result["comments"] if c.get("author", "").lower() == author.lower()]
    return {"success": True, "author": author, "comments": filtered, "total": len(filtered)}


def get_comments_for_paragraph(file_path: str, paragraph_index: int) -> dict:
    """Get comments for a specific paragraph."""
    path = _resolve_path(file_path, "read")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if paragraph_index < 0 or paragraph_index >= len(doc.paragraphs):
            return {"success": False, "error": f"段落索引无效。文档有 {len(doc.paragraphs)} 个段落"}
        para_text = doc.paragraphs[paragraph_index].text

        result = get_all_comments(str(path))
        if not result.get("success"):
            return result

        # Filter comments referencing this paragraph
        para_ref = str(paragraph_index)
        filtered = []
        for c in result["comments"]:
            cpara = c.get("paragraph_index", c.get("para", ""))
            if str(cpara) == para_ref or para_text[:30] in c.get("text", ""):
                filtered.append(c)

        return {"success": True, "paragraph_index": paragraph_index,
                "paragraph_text": para_text[:100], "comments": filtered, "total": len(filtered)}
    except Exception as e:
        return {"success": False, "error": f"获取段落评论失败: {type(e).__name__}: {e}"}


# Footnotes

def add_footnote(file_path: str, paragraph_index: int, footnote_text: str) -> dict:
    """Add a footnote to a specific paragraph.

    Uses direct XML manipulation since python-docx has limited footnote API.
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}

    try:
        doc = Document(str(path))
        if paragraph_index < 0 or paragraph_index >= len(doc.paragraphs):
            return {"success": False, "error": f"段落索引无效。文档有 {len(doc.paragraphs)} 个段落"}

        para = doc.paragraphs[paragraph_index]

        # Access or create footnotes part
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        footnotes_part = None
        for rel in doc.part.rels.values():
            if "footnotes" in rel.reltype:
                footnotes_part = rel.target_part
                break

        if footnotes_part is None:
            return {"success": False, "error": "文档不支持脚注（footnotes part 未找到）"}

        # Count existing footnotes
        footnotes_xml = footnotes_part._element
        existing = footnotes_xml.findall(qn("w:footnote"))
        fn_id = len(existing) + 1

        # Add footnote reference in paragraph
        run = para.add_run()
        run_element = run._r
        footnote_ref = OxmlElement("w:footnoteReference")
        footnote_ref.set(qn("w:id"), str(fn_id))
        run_element.append(footnote_ref)

        # Add footnote content
        footnote_el = OxmlElement("w:footnote")
        footnote_el.set(qn("w:id"), str(fn_id))
        p_el = OxmlElement("w:p")
        r_el = OxmlElement("w:r")
        t_el = OxmlElement("w:t")
        t_el.set(qn("xml:space"), "preserve")
        t_el.text = footnote_text
        r_el.append(t_el)
        p_el.append(r_el)
        footnote_el.append(p_el)
        footnotes_xml.append(footnote_el)

        doc.save(str(path))
        return {"success": True, "message": f"脚注已添加到段落 #{paragraph_index}", "footnote_id": fn_id}
    except Exception as e:
        return {"success": False, "error": f"添加脚注失败: {type(e).__name__}: {e}"}


# Word 表格的行列增删
#
# 既有的一堆表格工具（合并/着色/对齐/列宽/斑马纹…）
#   都能改已有格子长什么样，唯独改不了有几行几列 ——
#   而那是最常见的收尾动作（"再加一行"）。
#
# 两条纪律（都与合并单元格"你以为改一格、其实改一片"同源）：
#   ① 行列操作的坐标是「网格列」，与 `iter_grid_cells` / `parse_docx`
#      打出来的 `cN` 是同一套编号（0 基）—— 读侧给什么、写侧就收什么。
#   ② 碰到合并区就 fail-closed：加/删列在横向合并下会产出结构坏掉的表
#      （gridSpan 与 tblGrid 对不上），删行在纵向合并下会删掉别人的格子。
#      这些情况拒，并把原因说出来 —— 静默产出一份坏 docx 比报错贵得多。

def _table_or_error(path: Path, table_index: int):
    """打开文档并取表；取不到就返回 (None, 错误 dict)。"""
    doc = Document(str(path))
    if table_index < 0 or table_index >= len(doc.tables):
        return None, {"success": False,
                      "error": f"表格索引 {table_index} 无效（共 {len(doc.tables)} 个表）"}
    return doc, None


def _table_has_horizontal_merge(table) -> bool:
    """表里有没有横向合并（任一真实格子的 `gridSpan` > 1）。"""
    for row in iter_grid_cells(table):
        for gc in row:
            if gc["span"] > 1:
                return True
    return False


def _row_touches_vertical_merge(table, row_index: int) -> bool:
    """这一行是否属于某个纵跨多行的合并区。"""
    for col in range(len(table.rows[row_index].cells)):
        r0, r1, _c0, _c1 = cell_merge_range(table, row_index, col)
        if r1 > r0:
            return True
    return False


def add_table_rows(file_path: str, table_index: int,
                   count: int = 1) -> dict:
    """给 Word 表格追加 `count` 行（加在最后）。

    新行按表的网格列数建，没有内容。要填内容请接着用
    `format_table_cell_text`（注意合并区的坐标规则）。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    count = int(count or 1)
    if count < 1 or count > 100:
        return {"success": False, "error": f"count 要在 1-100 之间，给的是 {count}"}
    try:
        doc, err = _table_or_error(path, table_index)
        if err:
            return err
        table = doc.tables[table_index]
        for _ in range(count):
            table.add_row()
        doc.save(str(path))
        return {"success": True, "row_count": len(table.rows),
                "message": f"已追加 {count} 行，现在共 {len(table.rows)} 行"}
    except Exception as e:
        return {"success": False, "error": f"追加行失败: {type(e).__name__}: {e}"}


def delete_table_rows(file_path: str, table_index: int,
                      row_index: int, count: int = 1) -> dict:
    """从 `row_index`（0 基）开始删 `count` 行。

    被删的行若属于纵跨多行的合并区，拒（那会连带删掉别行的格子）。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    count = int(count or 1)
    try:
        doc, err = _table_or_error(path, table_index)
        if err:
            return err
        table = doc.tables[table_index]
        n = len(table.rows)
        if row_index < 0 or row_index >= n:
            return {"success": False, "error": f"行索引 {row_index} 无效（共 {n} 行）"}
        if row_index + count > n:
            return {"success": False,
                    "error": f"要删 {count} 行但只剩 {n - row_index} 行 —— "
                             f"表格至少要留一行。"}
        for r in range(row_index, row_index + count):
            if _row_touches_vertical_merge(table, r):
                return {"success": False,
                        "error": f"第 {r} 行属于一个**纵跨多行**的合并区，"
                                 f"删它会连带删掉别的行的格子。"
                                 f"请先取消那个合并（或改用整表替换）。"}
        # 按 XML 元素删（python-docx 没有 delete_row）
        trs = table._tbl.findall(qn("w:tr"))
        for tr in trs[row_index:row_index + count]:
            table._tbl.remove(tr)
        doc.save(str(path))
        return {"success": True, "row_count": len(doc.tables[table_index].rows),
                "message": f"已删除第 {row_index} 起的 {count} 行"}
    except Exception as e:
        return {"success": False, "error": f"删除行失败: {type(e).__name__}: {e}"}


def add_table_columns(file_path: str, table_index: int,
                      count: int = 1) -> dict:
    """给 Word 表格追加 `count` 列（加在最后）。

    表里有横向合并时拒 —— 加列要同时动 `w:tblGrid` 与每一行的
    `w:tc`，而合并区里的格子跨多列，补出来的结构会对不上
    （Word 打开会报"表格已损坏"）。避开合并再做。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    count = int(count or 1)
    if count < 1 or count > 50:
        return {"success": False, "error": f"count 要在 1-50 之间，给的是 {count}"}
    try:
        doc, err = _table_or_error(path, table_index)
        if err:
            return err
        table = doc.tables[table_index]
        if _table_has_horizontal_merge(table):
            return {"success": False,
                    "error": "这张表里有**横向合并**的单元格，加列会让表格结构"
                             "对不上（Word 会报表格损坏）。先取消合并再加列。"}
        tbl = table._tbl
        grid = tbl.find(qn("w:tblGrid"))
        if grid is None:
            return {"success": False, "error": "表格缺少 w:tblGrid，无法加列"}
        trs = tbl.findall(qn("w:tr"))
        width = "1440"                       # 1 英寸（twips），与常见默认一致
        for _ in range(count):
            gc = OxmlElement("w:gridCol")
            gc.set(qn("w:w"), width)
            grid.append(gc)
            for tr in trs:
                tc = OxmlElement("w:tc")
                tcPr = OxmlElement("w:tcPr")
                tcW = OxmlElement("w:tcW")
                tcW.set(qn("w:w"), width)
                tcW.set(qn("w:type"), "dxa")
                tcPr.append(tcW)
                tc.append(tcPr)
                tc.append(OxmlElement("w:p"))      # 空格子也要有一个段落
                tr.append(tc)                      # trPr 在最前，追加在末尾是对的
        doc.save(str(path))
        cols = len(doc.tables[table_index].columns)
        return {"success": True, "column_count": cols,
                "message": f"已追加 {count} 列，现在共 {cols} 列"}
    except Exception as e:
        return {"success": False, "error": f"追加列失败: {type(e).__name__}: {e}"}


def delete_table_columns(file_path: str, table_index: int,
                         col_index: int, count: int = 1) -> dict:
    """从 `col_index`（0 基网格列）开始删 `count` 列。

    与 `add_table_columns` 同一条：有横向合并就拒。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    count = int(count or 1)
    try:
        doc, err = _table_or_error(path, table_index)
        if err:
            return err
        table = doc.tables[table_index]
        if _table_has_horizontal_merge(table):
            return {"success": False,
                    "error": "这张表里有**横向合并**的单元格，删列会让表格结构"
                             "对不上。先取消合并再删。"}
        n_cols = len(table.columns)
        if col_index < 0 or col_index >= n_cols:
            return {"success": False,
                    "error": f"列索引 {col_index} 无效（共 {n_cols} 列）"}
        if col_index + count > n_cols:
            return {"success": False,
                    "error": f"要删 {count} 列但只剩 {n_cols - col_index} 列 —— "
                             f"表格至少要留一列。"}
        tbl = table._tbl
        grid = tbl.find(qn("w:tblGrid"))
        grid_cols = grid.findall(qn("w:gridCol")) if grid is not None else []
        for tr in tbl.findall(qn("w:tr")):
            tcs = tr.findall(qn("w:tc"))
            for tc in tcs[col_index:col_index + count]:
                tr.remove(tc)
        for gc in grid_cols[col_index:col_index + count]:
            grid.remove(gc)
        doc.save(str(path))
        cols = len(doc.tables[table_index].columns)
        return {"success": True, "column_count": cols,
                "message": f"已删除第 {col_index} 列起的 {count} 列，现在共 {cols} 列"}
    except Exception as e:
        return {"success": False, "error": f"删除列失败: {type(e).__name__}: {e}"}


# 页面设置 / 分节 / 页眉页脚 / 文档属性
#
# 这一批属于"库本来就有、接口没接"：`python-docx` 本来就有它们
#    （`sections[].*` / `add_section()` / `settings.odd_and_even_pages_header_footer` /
#     `core_properties`），而这里一条工具都没接。
#与图表类型补齐是同一件事：不是做不到，是没接。
#
# 这一批覆盖：分节 · 页面设置 · 页眉页脚 · 奇偶不同 · 页码域。
#   多级编号 / 题注 / 交叉引用 / 水印 / 首字下沉 / 艺术字 / 分栏 / 图片环绕
#   需要手写 OOXML（`python-docx` 没有对应 API）。

#: 纸张尺寸（厘米）—— 只放考试与办公里会用到的那几张。
#: 与 Excel 那张表同一个取向：不枚举全套，要别的就直接给 cm。
_PAGE_SIZES_CM = {
    "a4": (21.0, 29.7),
    "a3": (29.7, 42.0),
    "a5": (14.8, 21.0),
    "b5": (17.6, 25.0),
    "letter": (21.59, 27.94),
    "legal": (21.59, 35.56),
}


def _cm(value: float):
    """厘米 -> `docx.shared` 的长度对象。负数不许（会把版面撑坏）。"""
    from docx.shared import Cm
    v = float(value)
    if v < 0:
        raise ValueError("长度不能是负数：%r" % value)
    return Cm(v)


def set_word_page_setup(
    file_path: str,
    section_index: int = 0,
    orientation: str = "",
    page_size: str = "",
    page_width_cm: float = 0,
    page_height_cm: float = 0,
    top_cm: Optional[float] = None,
    bottom_cm: Optional[float] = None,
    left_cm: Optional[float] = None,
    right_cm: Optional[float] = None,
    gutter_cm: Optional[float] = None,
    header_distance_cm: Optional[float] = None,
    footer_distance_cm: Optional[float] = None,
) -> dict:
    """Word 页面设置（按节）—— 纸张 / 方向 / 边距 / 装订线 / 页眉页脚距边界。

    边距那几个不给 = 不动它。
    给了负数要报错，不许当成"没给" —— 若写成 `val < 0: continue`，
    `top_cm=-2` 会回「一个参数都没给 —— 页面设置要改什么就说什么」，
    一句和病因无关的话。漏了参数说漏了、写错了说写错了。
    横向：既设 `orientation` 也把宽高对调 —— 只设前者，Word/WPS 里
    纸张还是竖的、只是内容横过来（那是用户看到"设了没反应"的常见原因）。

    纸张也可以直接给 `page_width_cm` / `page_height_cm`
    （宣传册那类 21 × 14.8 的非标尺寸就靠它俩）。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    try:
        from docx.enum.section import WD_ORIENT
        doc = Document(str(path))
        secs = doc.sections
        if not (0 <= section_index < len(secs)):
            return {"success": False,
                    "error": f"节号无效（文档有 {len(secs)} 节，0 起）"}
        sec = secs[section_index]
        done = []

        if page_size:
            key = str(page_size).strip().lower()
            wh = _PAGE_SIZES_CM.get(key)
            if wh is None:
                return {"success": False,
                        "error": f"page_size 认 {sorted(_PAGE_SIZES_CM)}，收到 {page_size!r}"}
            sec.page_width, sec.page_height = _cm(wh[0]), _cm(wh[1])
            done.append("纸型 %s" % page_size)
        if page_width_cm:
            sec.page_width = _cm(page_width_cm)
            done.append("宽 %.2fcm" % page_width_cm)
        if page_height_cm:
            sec.page_height = _cm(page_height_cm)
            done.append("高 %.2fcm" % page_height_cm)

        if orientation:
            o = str(orientation).strip().lower()
            if o not in ("portrait", "landscape"):
                return {"success": False,
                        "error": f"orientation 只认 portrait / landscape，收到 {orientation!r}"}
            sec.orientation = (WD_ORIENT.LANDSCAPE if o == "landscape"
                               else WD_ORIENT.PORTRAIT)
            # 只设 orientation 不改宽高，纸还是竖的 —— 见 docstring
            if (o == "landscape") == (sec.page_width < sec.page_height):
                sec.page_width, sec.page_height = sec.page_height, sec.page_width
            done.append("横向" if o == "landscape" else "纵向")

        for label, val, attr in (("上", top_cm, "top_margin"),
                                 ("下", bottom_cm, "bottom_margin"),
                                 ("左", left_cm, "left_margin"),
                                 ("右", right_cm, "right_margin"),
                                 ("装订线", gutter_cm, "gutter"),
                                 ("页眉距边界", header_distance_cm, "header_distance"),
                                 ("页脚距边界", footer_distance_cm, "footer_distance")):
            if val is None:
                continue
            if float(val) < 0:
                return {"success": False,
                        "error": f"{label}边距不能是负数（收到 {val!r}）—— "
                                 f"不想改它就**别传这个参数**"}
            setattr(sec, attr, _cm(val))
            done.append("%s %.2fcm" % (label, float(val)))

        if not done:
            return {"success": False, "error": "一个参数都没给 —— 页面设置要改什么就说什么"}
        doc.save(str(path))
        return {"success": True,
                "message": "已设置第 %d 节的页面：%s" % (section_index, "；".join(done))}
    except Exception as e:
        return {"success": False, "error": f"页面设置失败: {type(e).__name__}: {e}"}


def add_word_section(
    file_path: str,
    start_type: str = "new_page",
    orientation: str = "",
) -> dict:
    """在文档末尾加一节。`start_type` = `new_page` / `continuous` / `even_page` / `odd_page`。

    新节继承上一节的页面设置（`python-docx` 的 `add_section` 就是这么做的）
    —— 所以"从新的一页开始"只要这一句；要把新节设成横向，紧接着调
    `set_word_page_setup(section_index=<新节号>)`（或在这里直接给 `orientation`）。

    它是"加一节"，不是"把文档切成若干节" —— 现有的分节符在正文里，
    本工具不碰它们。要"每个部分独立成节"，得按顺序逐节追加。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    try:
        from docx.enum.section import WD_SECTION, WD_ORIENT
        kinds = {"new_page": WD_SECTION.NEW_PAGE,
                 "continuous": WD_SECTION.CONTINUOUS,
                 "even_page": WD_SECTION.EVEN_PAGE,
                 "odd_page": WD_SECTION.ODD_PAGE}
        st = str(start_type).strip().lower()
        if st not in kinds:
            return {"success": False,
                    "error": f"start_type 认 {sorted(kinds)}，收到 {start_type!r}"}
        doc = Document(str(path))
        sec = doc.add_section(kinds[st])
        if orientation:
            o = str(orientation).strip().lower()
            if o not in ("portrait", "landscape"):
                return {"success": False,
                        "error": f"orientation 只认 portrait / landscape，收到 {orientation!r}"}
            sec.orientation = (WD_ORIENT.LANDSCAPE if o == "landscape"
                               else WD_ORIENT.PORTRAIT)
            if (o == "landscape") == (sec.page_width < sec.page_height):
                sec.page_width, sec.page_height = sec.page_height, sec.page_width
        doc.save(str(path))
        return {"success": True, "section_index": len(doc.sections) - 1,
                "message": "已加第 %d 节（起点 %s%s）"
                           % (len(doc.sections) - 1, st,
                              "，横向" if orientation else "")}
    except Exception as e:
        return {"success": False, "error": f"加节失败: {type(e).__name__}: {e}"}


def set_word_header_footer(
    file_path: str,
    section_index: int = 0,
    header: str = "",
    footer: str = "",
    header_page_number: bool = False,
    footer_page_number: bool = False,
    page_number_format: str = "第 &P 页",
    different_first_page: bool = False,
    different_odd_even: bool = False,
    clear: bool = False,
) -> dict:
    """某一节的页眉 / 页脚（含页码域）· 首页不同 · 奇偶不同。

    页码是域（`&P` 当前页 / `&N` 总页数）—— 由 Word/WPS 打开时渲染。
    我们不算页数（那是排版引擎的事）。
    `footer_page_number=True` + `page_number_format="第 &P 页"` 就是"页脚正中插页码"。

    `different_odd_even=True` 开的是文档级开关
    （`settings.odd_and_even_pages_header_footer`），它对整篇生效 ——
    而"哪一节显示什么"仍由每节的 `header` / `footer` 决定。
    奇偶不同的文档里，`header` 写进的是奇数页那一支；
    偶数页要另设（本工具暂不做，`even_page_header` 走 `1b` 的 XML 那一批）。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    try:
        doc = Document(str(path))
        secs = doc.sections
        if not (0 <= section_index < len(secs)):
            return {"success": False,
                    "error": f"节号无效（文档有 {len(secs)} 节，0 起）"}
        sec = secs[section_index]
        done = []

        if different_first_page:
            sec.different_first_page_header_footer = True
            done.append("首页页眉页脚不同")
        if different_odd_even:
            doc.settings.odd_and_even_pages_header_footer = True
            done.append("奇偶页不同（文档级）")

        def _fill(container, text, want_page):
            """把文字 + 可选页码域写进页眉/页脚。清空靠 clear，不靠空串。"""
            if clear:
                for p in list(container.paragraphs):
                    p._element.getparent().remove(p._element)
                container.add_paragraph()
            para = container.paragraphs[0] if container.paragraphs else container.add_paragraph()
            if text:
                for r in list(para.runs):
                    r._element.getparent().remove(r._element)
                para.add_run(text)
            if want_page:
                para.add_run(("\t" if text else "") + str(page_number_format))

        if header or clear or header_page_number:
            sec.header.is_linked_to_previous = False
            _fill(sec.header, header, header_page_number)
            done.append("页眉")
        if footer or clear or footer_page_number:
            sec.footer.is_linked_to_previous = False
            _fill(sec.footer, footer, footer_page_number)
            done.append("页脚")

        if not done:
            return {"success": False, "error": "一个参数都没给 —— 页眉页脚要改什么就说什么"}
        doc.save(str(path))
        return {"success": True,
                "message": "已设置第 %d 节：%s" % (section_index, "；".join(done))}
    except Exception as e:
        return {"success": False, "error": f"页眉页脚失败: {type(e).__name__}: {e}"}


def set_document_properties(file_path: str, title: str = "", author: str = "",
                            subject: str = "", keywords: str = "",
                            category: str = "", comments: str = "") -> dict:
    """文档属性（标题 / 作者 / 主题 / 关键词 / 类别 / 备注）。

    `word3` 第 1 步"设置文档属性的摘要信息：标题为…，作者为…"就是它。
    「单位 / 公司」不在标准属性里 —— 它在 OOXML 的 `app.xml`（扩展属性），
    `python-docx` 没有口。如实说，不假装写进去了。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    try:
        doc = Document(str(path))
        cp = doc.core_properties
        pairs = (("title", title), ("author", author), ("subject", subject),
                 ("keywords", keywords), ("category", category), ("comments", comments))
        done = []
        for attr, val in pairs:
            if val:
                setattr(cp, attr, val)
                done.append(attr)
        if not done:
            return {"success": False, "error": "一个属性都没给"}
        doc.save(str(path))
        msg = "已设置文档属性：%s" % "、".join(done)
        if subject and subject.strip():
            msg += "（⚠️『单位/公司』不在标准属性里，本工具写不了 —— 那是 app.xml）"
        return {"success": True, "message": msg}
    except Exception as e:
        return {"success": False, "error": f"设置文档属性失败: {type(e).__name__}: {e}"}


# 题注 / 交叉引用
#
# `python-docx` 没有域（field）的 API —— `Run.add_field` 不存在，
# `caption` / `bookmark` / `SEQ` 都没有现成口。
#这一件是手写 OOXML（`w:fldChar` + `w:instrText` + `w:bookmarkStart`），
# 与"库本来就有、只是没接"的那批不是同一类活。
#
# ## 怎么确认"引擎真的认这些域"
#
# 手写域最大的风险是「看着像域，其实引擎不认」—— 而 XML 合法
# 只是必要条件。用 LibreOffice 当等价判据：
#
#   写一份 docx（`REF _Ref100 \h` + `SEQ 图 \* ARABIC`）→ LibreOffice 打开
#   → 读回算出来的文本 → 得到 `见图 1 与表 1` / `图 1 系统架构`。
#
# 缓存值恰好等于期望值时，这个结果不可信：分不出"引擎算了"
# 与"它只是把缓存值回显出来"（没红 ≠ 跑到了）。
#把缓存值故意改成 99 / 88 再跑一次 —— 读回来仍然是 1
#引擎真的重算了。这一条才是判据。
#
# ## 域的形状（三段，缺一不可）
#
#     run1: <w:fldChar w:fldCharType="begin"/>
#     run2: <w:instrText xml:space="preserve"> REF _Ref100 \h </w:instrText>
#     run3: <w:fldChar w:fldCharType="separate"/>
#     run4: 缓存值（纯文本 run）—— 引擎打开时会重算，但它在"没打开"时是
#           唯一看得见的东西
#     run5: <w:fldChar w:fldCharType="end"/>
#
# `instrText` 必须自成一段 run（它不能和普通文字挤在一个 run 里），
#   而 begin/separate/end 也各是一个 run —— 挤在一起 Word 认不出。
# 缓存值要给：不给的话，在没有排版引擎的地方（预览器、我们自己的
#   `get_document_text`）那处是空白，看起来像"写丢了"。
#   它是近似值（我们按文档里已有的同 label 题注数推），
#   打开时会被引擎的重算覆盖 —— 这一点如实说。

#: 题注 labels 的兜底。不是白名单 —— 卷子上写"图"/"表"，
#: 而英文文档写 "Figure"/"Table"；这里只用来算缓存值，不限制用户写什么。
_SEQ_LABELS = ("图", "表", "Figure", "Table")


def _w_el(tag: str, **attrs):
    """造一个 `w:` 元素（属性名写 `w:xxx` 的形式，这里转成 `qn`）。"""
    e = OxmlElement(tag)
    for k, v in attrs.items():
        e.set(qn(k.replace("w:", "w:")), v)
    return e


def _append_field(paragraph, instr: str, cached: str) -> None:
    """在段落末尾追加一个完整的域（begin/instrText/separate/缓存/end）。"""
    r1 = paragraph.add_run()._r
    r1.append(_w_el("w:fldChar", **{"w:fldCharType": "begin"}))
    r2 = paragraph.add_run()._r
    it = _w_el("w:instrText", **{"xml:space": "preserve"})
    it.text = instr
    r2.append(it)
    r3 = paragraph.add_run()._r
    r3.append(_w_el("w:fldChar", **{"w:fldCharType": "separate"}))
    paragraph.add_run(cached)
    r5 = paragraph.add_run()._r
    r5.append(_w_el("w:fldChar", **{"w:fldCharType": "end"}))


def _enable_update_fields(doc) -> None:
    """在 `settings.xml` 里打开 `w:updateFields`，Word/WPS 打开时会提示更新域。

    为什么必须加：我们写的缓存值是近似的。不加这一项，
    Word 打开时不会主动重算，用户看到的就是那个近似值 —— 而它看起来是真的。
    LibreOffice 不看这一项（它自己会重算），
    而 Word 看 —— 两个引擎的行为不一样，两个都要照顾。
    """
    try:
        st = doc.settings.element
        if st.find(qn("w:updateFields")) is None:
            st.append(_w_el("w:updateFields", **{"w:val": "true"}))
    except Exception as exc:                              # noqa: BLE001
        print("[word] 打开 updateFields 失败（域仍然写得进去）：%s: %s"
              % (type(exc).__name__, exc))


def _next_bookmark_name(doc) -> str:
    """下一个书签名 `_Ref<n>`。

    唯一性是判据：书签重名时 Word 打开会报"书签已定义"，
    而那个报错不会在写的时候出现（我们看不见）。扫一遍取最大值 +1。

    书签名的限制（OOXML）：不能含空格、不能以数字开头、≤40 字符。
    `_Ref<n>` 三条都满足。
    """
    import re as _re
    mx = 0
    for el in doc.element.body.iter(qn("w:bookmarkStart")):
        m = _re.fullmatch(r"_Ref(\d+)", el.get(qn("w:name")) or "")
        if m:
            mx = max(mx, int(m.group(1)))
    return "_Ref%d" % (mx + 1)


def _count_seq_fields(doc, label: str) -> int:
    """文档里已有几个 `SEQ <label>` 域 —— 用来算缓存值（不是权威值）。"""
    n = 0
    for el in doc.element.body.iter(qn("w:instrText")):
        t = (el.text or "").strip().upper()
        if t.startswith("SEQ"):
            rest = t[3:].strip().split()
            if rest and rest[0] in (label.upper(), label):
                n += 1
    return n


def add_word_caption(file_path: str, paragraph_index: int, label: str = "图",
                     text: str = "", at_end: bool = True) -> dict:
    """给某个段落加一个题注（`SEQ` 域 + 书签），返回 `bookmark` 给交叉引用用。

    形状：`图 1 系统架构` —— 其中 `1` 是 `SEQ 图 \\* ARABIC` 域，
    被一个书签（`_Ref<n>`）包着，后面可以用 `add_word_cross_reference` 引用它。

    它做的是"编号自动排序和更新"那一半 —— 手写的"图 1"改成域之后，
    中间插一张图，后面的编号自己会变（引擎重算）。

    `at_end=False` 就插在段落开头。

    插入的是域，不是死文本：
      · Word/WPS 打开时会重算（我们顺手打开了 `updateFields`，它会提示一下）
      · LibreOffice 会自己重算（缓存值故意写错，它也会算回对的）
      · 而在没有排版引擎的地方（我们自己的 `get_document_text`、
        某些预览器）看到的是我们写的缓存值，那是近似值
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    if not label.strip():
        return {"success": False, "error": "label 不能是空的（如「图」「表」）"}
    try:
        doc = Document(str(path))
        if not (0 <= paragraph_index < len(doc.paragraphs)):
            return {"success": False,
                    "error": f"段落索引无效（文档有 {len(doc.paragraphs)} 段，0 起）"}
        para = doc.paragraphs[paragraph_index]

        n = _count_seq_fields(doc, label.strip()) + 1
        name = _next_bookmark_name(doc)
        bid = str(abs(hash(name)) % 90000 + 1000)

        start = _w_el("w:bookmarkStart", **{"w:id": bid, "w:name": name})
        end = _w_el("w:bookmarkEnd", **{"w:id": bid})

        if at_end:
            para.add_run(" " + label.strip() + " ")
            para._p.append(start)
            _append_field(para, " SEQ %s \\* ARABIC " % label.strip(), str(n))
            para._p.append(end)
            if text:
                para.add_run(" " + text)
        else:
            # 插到开头：python-docx 没有 insert_run(0)，得自己拼
            body = para._p
            first = body.find(qn("w:r"))
            holder = OxmlElement("w:p")
            hp = holder  # 借一个段落来拼，再搬元素过去
            from docx.text.paragraph import Paragraph as _P
            tmp = _P(holder, para._parent)
            tmp.add_run(label.strip() + " ")
            tmp._p.append(start)
            _append_field(tmp, " SEQ %s \\* ARABIC " % label.strip(), str(n))
            tmp._p.append(end)
            if text:
                tmp.add_run(" " + text)
            kids = [c for c in list(tmp._p)]
            if first is not None:
                for c in reversed(kids):
                    first.addprevious(c)
            else:
                for c in kids:
                    body.append(c)

        _enable_update_fields(doc)
        doc.save(str(path))
        return {"success": True, "bookmark": name, "number": n,
                "message": ("已在第 %d 段%s插入题注「%s %d%s」"
                            "（`SEQ` 域 + 书签 `%s`）—— 引用它请把 `%s` 交给 "
                            "`add_word_cross_reference`。⚠️ 缓存值是**近似值**，"
                            "Word/WPS 打开时会重算"
                            % (paragraph_index, "末尾" if at_end else "开头",
                               label.strip(), n, (" " + text) if text else "", name, name))}
    except Exception as e:
        return {"success": False, "error": f"加题注失败: {type(e).__name__}: {e}"}


def add_word_cross_reference(file_path: str, paragraph_index: int,
                             bookmark: str = "", kind: str = "number",
                             hyperlink: bool = True) -> dict:
    """在某个段落末尾加一个交叉引用（`REF` 域），指向 `add_word_caption` 给的书签。

    `kind`：
      · `number` —— 只要编号（`REF _Ref1 \\h`）
      · `text`   —— 只要文字（`REF _Ref1 \\n \\h`）
      · `full`   —— 整条题注（`REF _Ref1 \\w \\h`）

    它做的是"引用能在编号变化时自动更新"那一半 —— 插一张图之后，
    正文里那句"见图 1"自己会变。

    与题注同一套边界：写进去的是域；Word/WPS 打开时重算（我们开了
    `updateFields` 会提示），LibreOffice 自己重算；没有引擎的地方看到的是缓存值。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    if not bookmark.strip():
        return {"success": False,
                "error": "要给 bookmark —— 它是 add_word_caption 回执里的那个名字"}
    bm = bookmark.strip()
    if " " in bm or bm[0].isdigit() or len(bm) > 40:
        return {"success": False,
                "error": f"书签名不合法（不能有空格、不能数字开头、≤40 字符）：{bm!r}"}
    k = str(kind or "number").strip().lower()
    switch = {"number": "", "text": " \\n", "full": " \\w"}.get(k)
    if switch is None:
        return {"success": False,
                "error": f"kind 只认 number / text / full，收到 {kind!r}"}
    try:
        doc = Document(str(path))
        if not (0 <= paragraph_index < len(doc.paragraphs)):
            return {"success": False,
                    "error": f"段落索引无效（文档有 {len(doc.paragraphs)} 段，0 起）"}
        # 书签必须真的存在 —— 指向空气的引用在 Word 里是「错误！未找到引用源。」
        names = {el.get(qn("w:name")) for el in doc.element.body.iter(qn("w:bookmarkStart"))}
        if bm not in names:
            return {"success": False,
                    "error": f"文档里没有书签 {bm!r}（现有：{sorted(n for n in names if n)[:8]}）"
                             f" —— 先用 add_word_caption 建题注"}

        para = doc.paragraphs[paragraph_index]
        cached = _cached_ref_text(doc, bm, k)
        instr = " REF %s%s%s " % (bm, switch, " \\h" if hyperlink else "")
        if paragraph_index == len(doc.paragraphs) - 1 or not para.text.strip():
            _append_field(para, instr, cached)
        else:
            para.add_run(" ")
            _append_field(para, instr, cached)

        _enable_update_fields(doc)
        doc.save(str(path))
        return {"success": True, "bookmark": bm, "kind": k, "cached": cached,
                "message": ("已在第 %d 段插入交叉引用（`REF %s`，%s）—— "
                            "缓存值「%s」是**近似值**，编号变了它会自己跟着变"
                            % (paragraph_index, bm, k, cached))}
    except Exception as e:
        return {"success": False, "error": f"加交叉引用失败: {type(e).__name__}: {e}"}


def _cached_ref_text(doc, bookmark: str, kind: str) -> str:
    """给引用算一个近似的缓存值 —— 从被引用书签里那个 `SEQ` 域的缓存值抄。

    找不到就回 `"1"`（不是空串 —— 空串在预览里看起来像"写丢了"）。
    它一定会被引擎的重算覆盖；这里只是"没打开时看得见一个数字"。
    """
    try:
        body = doc.element.body
        target = None
        for el in body.iter(qn("w:bookmarkStart")):
            if el.get(qn("w:name")) == bookmark:
                target = el
                break
        if target is None:
            return "1"
        # 从书签起点往后扫，找第一个 SEQ 域的缓存值（separate 之后那一段文本）
        seen_begin = False
        seen_separate = False
        cur = target                      # lxml 元素：往后走用 `getnext()`
        for _ in range(60):
            cur = cur.getnext()
            if cur is None:
                break
            if cur.tag == qn("w:fldChar"):
                t = cur.get(qn("w:fldCharType"))
                if t == "separate":
                    seen_separate = True
                elif t == "end" and seen_begin:
                    break
            elif cur.tag == qn("w:instrText"):
                if "SEQ" in (cur.text or "").upper():
                    seen_begin = True
            elif cur.tag == qn("w:t") and seen_separate:
                v = (cur.text or "").strip()
                if v:
                    return v
        return "1"
    except Exception:                                     # noqa: BLE001
        return "1"

# 把已有的手写编号换成题注域
#
# ## 它与 `add_word_caption` 不是一件事
#
# `add_word_caption` 是插入（那一段原本什么都没有）；
# 这一件是替换 —— 把已有图片下方图标题里的手写编号就地换成域。
#它必须先定位文本，再就地换掉那一小截。
#
# ## 为什么"定位"在这里是危险的
#
# 形如 `^[空格]*(图|表)[空格]*\d+` 的判据可能命中正文里的引用，例如：
#
#     表4-2所示的就是参与测试的实训室的软件和硬件设备的详细列表。
#
# —— 那是正文里的一句引用，不是题注。天真地"全部替换"会毁掉正文。
# 这是「检测可以错，打码不能错」的镜像 —— 区别在于
# 这里没有"打码"那一半兜底：改错了就是用户的文档被改坏。
#
#三条防线，缺一不可：
#   ① 判据保守（有句读 / 标题太长，不当题注）；
#   ② 跳过要说出来（`skipped` 带理由与段号，不静默丢）；
#   ③ `dry_run` 先看一眼；`paragraph_indices` 显式点名可越过启发式
#      （判据不是"我猜得准"，是"猜不准时有没有一条明说的路"）。
#
# ## 那条编号经常跨 run
#
# 编号经常跨 run：`图1-` / `1` / `  IDS结构框架` 是三个 run，
# `图4-1` / `0` / … 也是。
#按 run 找编号一定找错。**先按段落文本定位 `[start, end)`，再落到 run 上
# 只 splice 那一段**（同 `edit_file` 纪律：其余字节一个不动）。
#
# ## 域的形状（带章节号时）
#
#     图 <STYLEREF 1 \s> - <SEQ 图 \* ARABIC \s 1> 标题
#
# 「包含章节号」在 Word 里就是两个域：章号来自标题样式（`STYLEREF`），
#   序号带 `\s <级>`，每一章重新开始数。分隔符是两者之间那段普通文字。

#: 题注编号的形状：`1` / `1-1` / `1.2` / `1 - 3`
_CAPTION_NUM = r"\d+(?:[-–—.．\s]*\d+)*"

#: 段落以这些收尾，多半是正文里的一句引用，不是题注。
_SENTENCE_END = "。！？；，.!?;,"

#: 编号后面那截标题最多多长（超了当正文）。真题注最长 17 字，
#: 正文引用可达 28 字，24 落在两者之间。
_CAPTION_REST_MAX = 24

#: `TOC` 域的缓存值（没打开排版引擎时唯一看得见的东西 —— 不许留空）。
_TOC_PLACEHOLDER = "（打开文档后更新域即可显示）"


def _caption_matcher(label: str):
    """`label` + 编号 + 标题 的形状。只用半角空格与制表符做间隔，
    不用 `\\s` —— 后者会把段落分隔符也吞进来。"""
    import re as _re
    return _re.compile(r"^[ \t　]*%s[ \t　]*(?P<num>%s)[ \t　]*(?P<rest>.*)$"
                       % (_re.escape(label), _CAPTION_NUM), _re.IGNORECASE)


def _para_has_seq(para) -> bool:
    """这一段里已经有 `SEQ` 域了吗。

    幂等的判据：本工具跑第二遍时，那些段落的文本已经变成
    `图 1 标题`（缓存值）而编号那截已经不是手写数字了 —— 没有这条，
    第二遍会把缓存值再包一层，产出 `图 1 1 标题` 还把书签弄重。
    """
    for el in para._p.iter(qn("w:instrText")):
        if "SEQ" in (el.text or "").upper():
            return True
    return False


def _rpr_copy(run):
    """把某个 run 的字符格式（`w:rPr`）抄一份 —— 新造的 run 靠它不丢格式。"""
    from copy import deepcopy as _d
    if run is None:
        return None
    rpr = run._r.find(qn("w:rPr"))
    return _d(rpr) if rpr is not None else None


def _mk_run(text: str, rpr):
    r = OxmlElement("w:r")
    if rpr is not None:
        from copy import deepcopy as _d
        r.append(_d(rpr))
    t = _w_el("w:t", **{"xml:space": "preserve"})
    t.text = text
    r.append(t)
    return r


def _mk_fld(kind: str, rpr):
    r = OxmlElement("w:r")
    if rpr is not None:
        from copy import deepcopy as _d
        r.append(_d(rpr))
    r.append(_w_el("w:fldChar", **{"w:fldCharType": kind}))
    return r


def _mk_instr(instr: str, rpr):
    r = OxmlElement("w:r")
    if rpr is not None:
        from copy import deepcopy as _d
        r.append(_d(rpr))
    it = _w_el("w:instrText", **{"xml:space": "preserve"})
    it.text = instr
    r.append(it)
    return r


def _field_runs(instr: str, cached: str, rpr) -> list:
    """一个完整的域 = begin / instrText / separate / 缓存值 / end（五个 run）。

    缓存值必须非空：空串在没有排版引擎的地方（我们自己的
    `get_document_text`、某些预览器）看起来像「写丢了」。
    """
    return [_mk_fld("begin", rpr), _mk_instr(instr, rpr),
            _mk_fld("separate", rpr), _mk_run(cached, rpr), _mk_fld("end", rpr)]


def _splice_span(para, start: int, end: int, new_runs: list,
                 head: Optional[str] = None,
                 tail: Optional[str] = None) -> None:
    """把段落文本的 `[start, end)` 换成 `new_runs`，其余文字与格式逐字保留。

    判据同 `edit_file`：只 splice 命中那一段。编号经常跨 run
    （`图4-1` / `0` / `  …`），按 run 找一定找错。

    `head` / `tail` 给了就覆盖原文那两截（`None` = 原样保留）——
    覆盖是整段一侧的：给了 `head` 就把编号之前的 run 全吃掉、
    给了 `tail` 就把编号之后的 run 全吃掉。题注的文字经常跨 run
    （`图6年龄与送餐需求的` + `分析`），只换命中那个 run 会让标题重复一遍。
    题注那一趟用它把 `图1 标题` 规范成 `图 1 标题` —— 原始文档里
    `图1` / `图 1` / `图1-1  ` 三种写法混着来，而题注的标准形状是一个空格。
    """
    from copy import deepcopy as _d
    runs = list(para.runs)
    pos, spans = 0, []
    for r in runs:
        t = r.text or ""
        spans.append((pos, pos + len(t), r))
        pos += len(t)

    si = ei = None
    for k, (a, b, _r) in enumerate(spans):
        if si is None and start < b:
            si = k
        if end <= b:
            ei = k
            break
    if si is None:                      # 起点在段末，直接追加
        for r in new_runs:
            para._p.append(r)
        return
    if ei is None:
        ei = len(spans) - 1
    # 调用者要重写整段首/尾，把那一侧的 run 全吃掉（见上面那段 docstring）
    if head is not None:
        si = 0
    if tail is not None:
        ei = len(spans) - 1

    rs, re_ = spans[si][2], spans[ei][2]
    if head is None:
        head = (rs.text or "")[: start - spans[si][0]]
    if tail is None:
        tail = (re_.text or "")[end - spans[ei][0]:]
    tail_rpr = _rpr_copy(re_)

    anchor = rs._r
    for k in range(si + 1, ei + 1):     # 先摘掉中间那些 run（含 ei —— 它被 tail 取代）
        el = spans[k][2]._r
        el.getparent().remove(el)
    rs.text = head
    prev = anchor
    for r in new_runs:
        prev.addnext(r)
        prev = r
    if tail:
        prev.addnext(_mk_run(tail, tail_rpr))


def _scan_manual_captions(doc, label: str):
    """扫出「形状像题注」的段落。返回 `(hits, rejected)`。

    `hits` 里每条都带 `looks_like_body`，而**被判据否掉的那些不从
    这里剔掉** —— `paragraph_indices` 是越过判据的那条路（判据不是"我猜得准"，
    是"猜不准时有没有一条明说的路"）。在这里就剔掉，那条路就断了：
    回执还在说「确实要改就把它放进 `paragraph_indices`」，而放进去也没用。

    """
    import re as _re
    rx = _caption_matcher(label)
    hits, rejected = [], []
    for i, para in enumerate(doc.paragraphs):
        text = para.text or ""
        m = rx.match(text)
        if not m:
            continue
        if _para_has_seq(para):
            rejected.append({"paragraph_index": i, "text": text[:60],
                             "reason": "已经有 SEQ 域了 —— 不重复替换"})
            continue
        rest = (m.group("rest") or "").strip()
        hits.append({"paragraph_index": i, "text": text[:80],
                     "number": _re.sub(r"\s+", "", m.group("num")),
                     "title": rest,
                     "looks_like_body": (rest[:1] in tuple(_SENTENCE_END)
                                         or rest[-1:] in tuple(_SENTENCE_END)
                                         or len(rest) > _CAPTION_REST_MAX),
                     "start": m.start("num"), "end": m.end("num")})
    return hits, rejected


def _heading_has_numbering(doc, level: int) -> bool:
    """标题样式（或第一个用它的段落）有没有自动编号。

    它是 `STYLEREF <级> \\s` 能不能取到章号的唯一前提：
    那个域读的是标题样式的编号定义，不是标题的文字 ——
    `第一章 绪论` 这种手写的字面量，它一个字都看不见。
    """
    names = (f"Heading {level}", f"标题 {level}")
    for nm in names:
        try:
            el = doc.styles[nm].element
        except Exception:                                  # noqa: BLE001
            continue
        ppr = el.find(qn("w:pPr"))
        if ppr is not None and ppr.find(qn("w:numPr")) is not None:
            return True
        break
    for p in doc.paragraphs:                     # ② 直接挂在段落上也算
        try:
            if p.style is not None and p.style.name in names:
                ppr = p._p.find(qn("w:pPr"))
                return ppr is not None and ppr.find(qn("w:numPr")) is not None
        except Exception:                                  # noqa: BLE001
            return False
    # ③ Word 存盘的规范形状：`numbering.xml` 的 `w:lvl` 里带 `w:pStyle`
    #    （没有任何 w:numId 引用它，靠 pStyle 反向绑定）。
    #    漏了这一条方向是危险的：本该说「章号取不到」的却说没事。
    try:
        np_el = doc.part.numbering_part.element
    except Exception:                                      # noqa: BLE001
        return False
    want = {("Heading %d" % level), ("Heading%d" % level), ("标题 %d" % level)}
    for lvl in np_el.iter(qn("w:lvl")):
        ps = lvl.find(qn("w:pStyle"))
        if ps is not None and (ps.get(qn("w:val")) or "") in want:
            return True
    return False


def _caption_number_runs(label: str, number: str, chapter_level: int,
                         separator: str, rpr) -> list:
    """编号那一截要换成什么 —— 返回一串 run。

    · `chapter_level=0`：`SEQ 图 \\* ARABIC`
    · `chapter_level=1`：`STYLEREF 1 \\s` + 分隔符 + `SEQ 图 \\* ARABIC \\s 1`
    """
    import re as _re
    lab = label.strip()
    if not chapter_level:
        return _field_runs(" SEQ %s \\* ARABIC " % lab, number, rpr)
    bits = [b for b in _re.split(r"[-–—.．]", number) if b]
    chap = bits[0] if len(bits) > 1 else "1"
    seqv = bits[-1] if len(bits) > 1 else number
    return (_field_runs(" STYLEREF %d \\s " % chapter_level, chap, rpr)
            + [_mk_run(separator or "-", rpr)]
            + _field_runs(" SEQ %s \\* ARABIC \\s %d " % (lab, chapter_level),
                          seqv, rpr))


def replace_manual_captions(file_path: str, label: str = "图",
                            chapter_level: int = 0, separator: str = "-",
                            paragraph_indices: Optional[List[int]] = None,
                            dry_run: bool = False) -> dict:
    """把文档里已有的手写编号就地换成题注域（`SEQ`），并给每个加书签。

    它做的是「按顺序全部替换」那一半 ——
    换完之后，中间插一张图，后面的编号自己会重排。

    `chapter_level`：`0` 不带章节号（`图 1`）；`1` 包含章节号、用
    `separator` 分隔（`图 1-1`，满足"编号包含章节编号、用连字符分隔"的要求）。

    `paragraph_indices`：显式点名要改哪几段（越过自动判据）；
    不给就按启发式扫全部。

    这个工具会改文档里已有的文字，而判据是启发式的，
    拿不准时先 `dry_run=true` 看一眼（回执会把「要改的」与
    「跳过并给了理由的」两张表都列出来）。`skipped` 里那些不会被静默丢掉。
    """
    path = _resolve_path(file_path, "write")
    path = Path(_ensure_docx(str(path)))
    if not path.exists():
        return {"success": False, "error": f"文档不存在: {path}"}
    if not label.strip():
        return {"success": False, "error": "label 不能是空的（如「图」「表」）"}
    try:
        lvl = int(chapter_level)
    except (TypeError, ValueError):
        return {"success": False, "error": f"chapter_level 要是整数，收到 {chapter_level!r}"}
    if not (0 <= lvl <= 9):
        return {"success": False,
                "error": f"chapter_level 要在 0-9 之间（0 = 不带章节号），收到 {lvl}"}

    try:
        doc = Document(str(path))
        hits, skipped = _scan_manual_captions(doc, label.strip())

        if paragraph_indices is not None:
            want = []
            for v in paragraph_indices:
                try:
                    want.append(int(v))
                except (TypeError, ValueError):
                    return {"success": False,
                            "error": f"paragraph_indices 里有非整数：{v!r}"}
            by_idx = {h["paragraph_index"]: h for h in hits}
            chosen = []
            for i in want:
                if i in by_idx:
                    # 点名 = 越过 `looks_like_body` 那道判据
                    chosen.append(by_idx[i])
                elif 0 <= i < len(doc.paragraphs):
                    skipped.append({"paragraph_index": i,
                                    "text": (doc.paragraphs[i].text or "")[:60],
                                    "reason": "点名了，但这一段里找不到手写编号"})
                else:
                    skipped.append({"paragraph_index": i, "text": "",
                                    "reason": "段号越界"})
        else:
            chosen = [h for h in hits if not h["looks_like_body"]]
            for h in hits:
                if h["looks_like_body"]:
                    skipped.append({"paragraph_index": h["paragraph_index"],
                                    "text": h["text"],
                                    "reason": "看着像正文里的一句引用（带句读或太长）"
                                              "—— 确实要改就把它放进 paragraph_indices"})

        report = {"success": True, "label": label.strip(), "dry_run": bool(dry_run),
                  "found": len(chosen), "skipped": skipped,
                  "items": [{"paragraph_index": p["paragraph_index"],
                             "text": p["text"], "number": p["number"]}
                            for p in chosen]}
        if dry_run:
            report["message"] = (
                "dry_run：扫到 %d 段像「%s」的手写编号（**一个字节都没改**）。"
                "确认无误就把 dry_run 去掉重跑一次。%s"
                % (len(chosen), label.strip(),
                   ("；另有 %d 段被跳过，理由见 skipped" % len(skipped)) if skipped else ""))
            return report

        changed, failed = [], []
        for p in chosen:
            para = doc.paragraphs[p["paragraph_index"]]
            rpr = _rpr_copy(para.runs[0]) if para.runs else None
            runs = _caption_number_runs(label.strip(), p["number"], lvl,
                                        separator, rpr)
            name = _next_bookmark_name(doc)
            bid = str(abs(hash(name)) % 90000 + 1000)
            try:
                _splice_span(para, p["start"], p["end"], runs,
                             head=label.strip() + " ",
                             tail=(" " + p["title"]) if p["title"] else "")
                first = para._p.find(qn("w:r"))
                start_el = _w_el("w:bookmarkStart", **{"w:id": bid, "w:name": name})
                if first is not None:
                    first.addprevious(start_el)
                else:
                    para._p.append(start_el)
                para._p.append(_w_el("w:bookmarkEnd", **{"w:id": bid}))
                changed.append({"paragraph_index": p["paragraph_index"],
                                "number": p["number"], "bookmark": name})
            except Exception as exc:                      # noqa: BLE001
                failed.append({"paragraph_index": p["paragraph_index"],
                               "error": "%s: %s" % (type(exc).__name__, exc)})

        if changed:
            _enable_update_fields(doc)
            doc.save(str(path))

        warn_num = ""
        if lvl and changed and not _heading_has_numbering(doc, lvl):
            warn_num = ("；⚠️ 文档里的「Heading %d / 标题 %d」样式**没有自动编号** ⇒ "
                        "章号（`STYLEREF %d \\s`）现在取不到（Word 显空白、"
                        "LibreOffice 显「错误:未找到引用源」）—— 要章号就得先把标题的"
                        "手动编号换成**自动多级编号**" % (lvl, lvl, lvl))
        report["replaced"] = len(changed)
        report["bookmarks"] = [c["bookmark"] for c in changed]
        report["failed"] = failed
        if failed:
            report["success"] = False
            report["error"] = ("有 %d 段没换成（见 failed）—— 其余的已经改了，"
                               "别当整批失败" % len(failed))
        report["message"] = (
            "已把 %d 段「%s」的手写编号换成题注域%s，书签：%s。"
            "⚠️ 域里的数字是**近似值**，Word/WPS 打开时重算（会提示一下），"
            "LibreOffice 自己重算 —— 到时编号会按顺序**自动重排**。"
            "引用它们请把书签名交给 `add_word_cross_reference`。%s%s"
            % (len(changed), label.strip(),
               ("（含章节号、用「%s」分隔）" % (separator or "-")) if lvl else "",
               ", ".join(report["bookmarks"][:6]) or "—",
               ("；另有 %d 段被跳过，理由见 skipped" % len(skipped)) if skipped else "",
               warn_num))
        return report
    except Exception as e:
        return {"success": False, "error": f"替换手写编号失败: {type(e).__name__}: {e}"}
