"""Office skill write operations.

Covers:
  - DocSession / DocSessionStore (create, get, finalize, TTL eviction)
  - create_document handler (word/ppt/excel, invalid type)
  - add_block handler (all 6 block types, invalid session)
  - add_slide handler (all 4 slide types)
  - write_excel_data handler
  - export_document / export_workbook (render to file)

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_office_write.py
"""

import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# `tests/` 自己 —— 跑单个文件时它已经在 `sys.path[0]`，这里显式写一次
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sandbox import sandbox as _sandbox  # noqa: E402
from _sandbox import prod_diff, prod_snapshot  # noqa: E402


# ------------------------------------------------------------------
# DocSession / DocSessionStore tests
# ------------------------------------------------------------------

def test_doc_session_create():
    """DocSession is created with correct defaults."""
    from skills.office_parser.session import DocSession
    ds = DocSession(session_id="test1", doc_type="word", title="My Doc")
    assert ds.doc_type == "word"
    assert ds.status == "DRAFT"
    assert ds.title == "My Doc"
    assert ds.blocks == []


def test_doc_session_touch():
    """Touch updates last_access."""
    from skills.office_parser.session import DocSession
    ds = DocSession(session_id="t1", doc_type="ppt")
    old_access = ds.last_access
    time.sleep(0.01)
    ds.touch()
    assert ds.last_access > old_access


def test_doc_session_expiry():
    """is_expired works correctly with TTL."""
    from skills.office_parser.session import DocSession
    ds = DocSession(session_id="t1", doc_type="word")
    assert not ds.is_expired(60.0)
    # Short TTL — should expire quickly
    ds2 = DocSession(session_id="t2", doc_type="word")
    time.sleep(0.15)
    assert ds2.is_expired(0.1)


def test_store_create_and_get():
    """Store can create and retrieve sessions."""
    from skills.office_parser.session import DocSessionStore, reset_store
    reset_store()
    store = DocSessionStore(ttl_seconds=60, max_sessions=10)
    session = store.create("word", "Test")
    assert session.session_id
    retrieved = store.get(session.session_id)
    assert retrieved is not None
    assert retrieved.title == "Test"
    assert retrieved.doc_type == "word"


def test_store_ttl_eviction():
    """Sessions expire after TTL."""
    from skills.office_parser.session import DocSessionStore, reset_store
    reset_store()
    store = DocSessionStore(ttl_seconds=0.1, max_sessions=10)
    session = store.create("word")
    sid = session.session_id
    time.sleep(0.2)
    assert store.get(sid) is None


def test_store_finalize():
    """Finalize locks session status."""
    from skills.office_parser.session import DocSessionStore, reset_store
    reset_store()
    store = DocSessionStore(ttl_seconds=60, max_sessions=10)
    session = store.create("word")
    assert store.finalize(session.session_id)
    s2 = store.get(session.session_id)
    assert s2.status == "FINALIZED"
    # Second finalize should fail
    assert not store.finalize(session.session_id)


def test_store_max_sessions():
    """Oldest session is evicted when at capacity."""
    from skills.office_parser.session import DocSessionStore, reset_store
    reset_store()
    store = DocSessionStore(ttl_seconds=600, max_sessions=3)
    s1 = store.create("word")
    s2 = store.create("ppt")
    s3 = store.create("excel")
    s4 = store.create("word")  # should evict oldest (s1)
    assert store.active_count == 3
    assert store.get(s1.session_id) is None  # evicted
    assert store.get(s2.session_id) is not None
    assert store.get(s3.session_id) is not None
    assert store.get(s4.session_id) is not None


# ------------------------------------------------------------------
# create_document handler tests
# ------------------------------------------------------------------

def test_create_document_word():
    """create_document(word): the file path is the session_id.

    Word documents use "file as session": the returned `session_id` equals
    `file_path`, ends with `.docx`, and the reply carries a `hint` field.
    The handler must return the file path, not an internal store key --
    callers pass that handle back on the next turn.
    """
    from skills.office_parser.handler import create_document
    from skills.office_parser.session import reset_store
    reset_store()
    _init_handler()
    result = create_document("word", "My Report")
    assert result["success"], result
    assert result["doc_type"] == "word"
    assert result["session_id"].endswith(".docx"), result["session_id"]
    assert result["session_id"] == result["file_path"], result
    assert Path(result["file_path"]).exists(), result
    assert "hint" in result
    Path(result["file_path"]).unlink(missing_ok=True)


def test_create_document_ppt():
    """create_document with doc_type='ppt' returns session_id."""
    from skills.office_parser.handler import create_document
    from skills.office_parser.session import reset_store
    reset_store()
    result = create_document("ppt", "Slides")
    assert result["success"]
    assert result["doc_type"] == "ppt"


def test_create_document_excel():
    """create_document("excel") refuses and names the route.

    Excel has no session flow: the only write path is
    `write_excel_data(file_path, ...)`, which creates the file when absent.
    So the contract is "refuse + point at the right tool", not "return a
    session_id".

    The assertion checks the tool name in the error text -- refusal alone
    is not enough; without a route the model goes looking elsewhere.
    """
    from skills.office_parser.handler import create_document
    from skills.office_parser.session import reset_store
    reset_store()
    result = create_document("excel", "Data")
    assert not result["success"], result
    assert "write_excel_data" in result["error"], result["error"]


def _init_handler():
    """Initialize the handler with a SecurityGuard.

    Most tests here do not touch the guard; those that do call this first,
    otherwise the handler raises "SecurityGuard not initialized".
    """
    from core.security import SecurityGuard
    from skills.office_parser import handler as H
    sec = SecurityGuard(allowed_dirs=["./workspace"], command_blacklist=[],
                        audit_log_path="./logs/audit.log")
    H.init(sec)


def test_create_document_txt():
    """doc_type='txt' writes plain text directly.

    Plain text has no session and no Markdown parsing: the body goes to disk
    byte for byte. Without this path a natural "write a summary to txt"
    request has no valid tool, and the model loops looking for one.
    """
    from skills.office_parser import handler as H
    from skills.office_parser.session import reset_store

    reset_store()
    _init_handler()

    body = "《专业认知实习》摘要\n\n一、概况\n先后在图书馆、菜鸟驿站实训。\n\n二、结论\n三流合一。\n"
    out = Path("workspace/_test_txt_writer.txt")
    out.unlink(missing_ok=True)
    try:
        r = H.create_document("txt", content=body, output_name=out.name)
        assert r["success"], r
        assert r["doc_type"] == "txt"
        assert r["chars_written"] == len(body)
        written = Path(r["file_path"])
        assert written.exists(), r
        # 纯文本必须原样写入，不做 Markdown 解析
        assert written.read_text(encoding="utf-8") == body
    finally:
        out.unlink(missing_ok=True)


def test_create_document_txt_rejects_path_traversal():
    """output_name 里的 ../ 必须被剥掉 —— 只能落到 workspace 下。"""
    from skills.office_parser import handler as H
    from skills.office_parser.session import reset_store

    reset_store()
    _init_handler()

    r = H.create_document("txt", content="x", output_name="../../evil.txt")
    assert r["success"], r
    assert ".." not in r["file_path"], f"路径穿越没防住: {r['file_path']}"
    assert Path(r["file_path"]).parent.name == "workspace", r["file_path"]
    Path(r["file_path"]).unlink(missing_ok=True)


def test_create_document_invalid_type():
    """Unknown doc_type returns error."""
    from skills.office_parser.handler import create_document
    result = create_document("pdf")
    assert not result["success"]
    assert "error" in result


# ------------------------------------------------------------------
# add_block handler tests
# ------------------------------------------------------------------

def test_add_block_heading():
    """Add a heading block to a word document."""
    from skills.office_parser.handler import create_document, add_block
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("word", "Test")
    sid = doc["session_id"]
    result = add_block(sid, "heading", content="Chapter 1", level=2)
    assert result["success"]
    assert result["block_count"] == 1


def test_add_block_paragraph():
    """Add a paragraph block."""
    from skills.office_parser.handler import create_document, add_block
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("word", "Test")
    result = add_block(doc["session_id"], "paragraph", content="Hello world")
    assert result["success"]


def test_add_block_table():
    """Add a table block with rows."""
    from skills.office_parser.handler import create_document, add_block
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("word", "Test")
    rows = [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
    result = add_block(doc["session_id"], "table", rows=rows)
    assert result["success"]


def test_add_block_list():
    """Add a list block."""
    from skills.office_parser.handler import create_document, add_block
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("word", "Test")
    result = add_block(doc["session_id"], "list",
                       content="Item 1\nItem 2\nItem 3", ordered=True)
    assert result["success"]


def test_add_block_page_break():
    """Add a page break."""
    from skills.office_parser.handler import create_document, add_block
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("word", "Test")
    result = add_block(doc["session_id"], "page_break")
    assert result["success"]


def test_add_block_invalid_session():
    """Invalid session_id returns error."""
    from skills.office_parser.handler import add_block
    result = add_block("bad_session", "heading", content="test")
    assert not result["success"]


def test_add_block_invalid_type():
    """Unknown block_type returns error."""
    from skills.office_parser.handler import create_document, add_block
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("word")
    result = add_block(doc["session_id"], "invalid_block")
    assert not result["success"]


def test_add_block_session_reachable_by_file_path():
    """The returned handle must resolve to its own session.

    Both the 20-block cap and the block counter depend on it.
    `create_document("word")` mints an internal key `doc_<hex>` via
    `store.create()`, but returns the file path to the caller. If `store.get`
    only knows the internal key, the next turn's path lookup misses, and:
      - `block_count` stays 0 -- the receipt says `Block 0/20 added`
      - the missing session short-circuits the counter, so the 20-block cap
        never rejects anything
    """
    from skills.office_parser.handler import create_document, add_block
    from skills.office_parser.session import reset_store
    reset_store()
    _init_handler()
    doc = create_document("word", "Test")
    sid = doc["session_id"]
    try:
        result = add_block(sid, "heading", content="Chapter 1", level=2)
        assert result["success"], result
        assert result["block_count"] == 1, result
        result = add_block(sid, "paragraph", content="body")
        assert result["block_count"] == 2, result
    finally:
        Path(sid).unlink(missing_ok=True)


def test_add_block_cap_is_enforced():
    """The 20-block cap actually rejects the 21st block.

    The limit lives in `add_block`; it only works if session lookup succeeds.
    This test is the guard that keeps the cap effective: 20 blocks succeed,
    the 21st fails and its error mentions the limit.
    """
    from skills.office_parser.handler import create_document, add_block
    from skills.office_parser.session import reset_store
    reset_store()
    _init_handler()
    doc = create_document("word", "Cap")
    sid = doc["session_id"]
    try:
        for i in range(20):
            r = add_block(sid, "paragraph", content=f"p{i}")
            assert r["success"], f"第 {i+1} 块就被据了: {r}"
        over = add_block(sid, "paragraph", content="p20")
        assert not over["success"], f"第 21 块本该被拒: {over}"
        assert "20" in over["error"], over["error"]
    finally:
        Path(sid).unlink(missing_ok=True)


# ------------------------------------------------------------------
# add_slide handler tests
# ------------------------------------------------------------------

def test_add_slide_title():
    """Add a title slide."""
    from skills.office_parser.handler import create_document, add_slide
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("ppt", "My Presentation")
    result = add_slide(doc["session_id"], "title", title="Hello World")
    assert result["success"]
    assert result["slide_count"] == 1


def test_add_slide_content():
    """Add a content slide with bullets."""
    from skills.office_parser.handler import create_document, add_slide
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("ppt")
    result = add_slide(doc["session_id"], "content",
                       title="Key Points",
                       bullets=["Point A", "Point B", "Point C"])
    assert result["success"]


def test_add_slide_invalid_type():
    """Unknown slide_type returns error."""
    from skills.office_parser.handler import create_document, add_slide
    from skills.office_parser.session import reset_store
    reset_store()
    doc = create_document("ppt")
    result = add_slide(doc["session_id"], "video")
    assert not result["success"]


# ------------------------------------------------------------------
# write_excel_data handler tests (the only Excel write entry point)
# ------------------------------------------------------------------

def test_write_excel_data_creates_file():
    """`write_excel_data` is the only Excel write entry: it creates the file.

    No session is involved: one path plus one 2-D table is the entire input.
    The assertion reads the file back, so "the tool said it wrote" is not
    enough.
    """
    from skills.office_parser.excel_tools import write_excel_data
    out = Path("workspace/_test_write_excel.xlsx")
    out.unlink(missing_ok=True)
    try:
        result = write_excel_data(out.name, "Sheet1",
                                 [["A", "B"], [1, 2], [3, 4]])
        assert result["success"], result
        assert result["rows_written"] == 3, result
        assert out.exists(), "工具说写成功了，磁盘上没有"
        # 回读核对：不是"它说写了"就算数
        from openpyxl import load_workbook
        wb = load_workbook(out)
        ws = wb["Sheet1"]
        assert ws["A2"].value == 1 and ws["B2"].value == 2, "回读内容不符"
        assert ws["A3"].value == 3 and ws["B3"].value == 4, "回读内容不符"
        wb.close()
    finally:
        out.unlink(missing_ok=True)


def test_write_excel_data_appends_rows():
    """第二次写同一个文件的不同区域时，先前的内容要还在。

    这里断言的是"文件里的数据"而不是"工具报的行数" ——
    `write_excel_data` 每次都是读进来改完再存，所以真正的风险是
    "第二次把第一次覆盖掉"，而那只有回读才看得出来。
    """
    from skills.office_parser.excel_tools import write_excel_data
    out = Path("workspace/_test_write_excel2.xlsx")
    out.unlink(missing_ok=True)
    try:
        assert write_excel_data(out.name, "Data", [[1, 2]])["success"]
        r2 = write_excel_data(out.name, "Data", [[3, 4]], start_cell="A2")
        assert r2["success"], r2
        from openpyxl import load_workbook
        wb = load_workbook(out)
        ws = wb["Data"]
        assert ws["A1"].value == 1 and ws["B1"].value == 2, "第一行被覆盖了"
        assert ws["A2"].value == 3 and ws["B2"].value == 4, "第二行没写进去"
        wb.close()
    finally:
        out.unlink(missing_ok=True)


# ------------------------------------------------------------------
# export_document handler tests
# ------------------------------------------------------------------

def test_export_document_renames_word_file():
    """`export_document` 对 Word 是重命名（不是渲染），并带上块计数。"""
    from skills.office_parser.handler import (
        create_document, add_block, export_document,
    )
    from skills.office_parser.session import reset_store
    reset_store()
    _init_handler()
    doc = create_document("word", "Test")
    sid = doc["session_id"]
    assert add_block(sid, "paragraph", content="hi")["success"]
    target = Path("workspace/_test_export_renamed.docx")
    target.unlink(missing_ok=True)
    try:
        result = export_document(sid, target.name)
        assert result["success"], result
        # 比 `resolve()`：导出回的是绝对路径（与 `add_block` 一致），
        # and `target` is relative -- comparing raw strings yields a false
        # mismatch.
        assert Path(result["file_path"]).resolve() == target.resolve(), result["file_path"]
        assert target.exists(), "导出说成功了，磁盘上没有"
        assert not Path(sid).exists(), "旧文件名还在 —— 是复制不是重命名？"
        # 块计数来自 session：它同时证明"重命名后句柄还查得到自己的 session"
        assert result["block_count"] == 1, result
    finally:
        target.unlink(missing_ok=True)


def test_export_document_renders_ppt():
    """`add_slide` -> `export_document` must produce an openable .pptx.

    All four slide types are exercised: `table` goes through `insert_table`,
    and `image` goes through `insert_picture` (layout 1's idx=1 is not a
    picture placeholder and raises AttributeError, so there is an
    `add_picture` fallback). Without this test those two branches are
    effectively untested.

    A page with only a title is treated as "not rendered": every slide must
    carry at least two shapes.
    """
    from skills.office_parser.handler import (
        create_document, add_slide, export_document,
    )
    from skills.office_parser.session import reset_store
    reset_store()
    _init_handler()

    img = Path("workspace/_test_slide_img.png")
    img.unlink(missing_ok=True)
    from PIL import Image as _Image
    _Image.new("RGB", (64, 48), (200, 30, 30)).save(str(img))

    target = Path("workspace/_test_deck.pptx")
    target.unlink(missing_ok=True)
    try:
        sid = create_document("ppt", "Deck")["session_id"]
        assert add_slide(sid, "title", title="Hello")["success"]
        assert add_slide(sid, "content", title="Points",
                         bullets=["a", "b"])["success"]
        assert add_slide(sid, "table", title="T",
                         rows=[["h1", "h2"], ["v1", "v2"]])["success"]
        assert add_slide(sid, "image", title="Pic",
                         image_path=img.name)["success"]

        result = export_document(sid, target.name)
        assert result["success"], result
        assert target.exists(), "导出说成功了，磁盘上没有"

        from pptx import Presentation
        prs = Presentation(str(target))
        slides = list(prs.slides)
        assert len(slides) == 4, f"应有 4 页，实得 {len(slides)}"
        titles = [s.shapes.title.text if s.shapes.title else ""
                  for s in slides]
        assert titles[0] == "Hello", titles
        assert titles[1] == "Points", titles
        # 每页至少要有内容形状 —— 只有标题的空壳也是"没渲染"
        for i, s in enumerate(slides):
            assert len(s.shapes) >= 2, f"第 {i+1} 页只有 {len(s.shapes)} 个形状"
    finally:
        target.unlink(missing_ok=True)
        img.unlink(missing_ok=True)


# ------------------------------------------------------------------
# Merged cells: the read side reports coordinates, the write side warns,
# and writes are read back
# ------------------------------------------------------------------

def _make_merged_docx(name: str) -> Path:
    """Build a table with merged cells (leave-request form layout).

    r0: `学院` | `材料与新能源学院`(合并 c1-c4) | `班级` | `新材25-01`
    r1: `姓名`(合并 c0-c1) | `学号` | `2025444212` | 空 | 空 | 空
    """
    from docx import Document
    p = Path("workspace") / name
    p.parent.mkdir(parents=True, exist_ok=True)
    d = Document()
    t = d.add_table(rows=2, cols=7)
    t.cell(0, 0).text = "学院"
    t.cell(0, 1).merge(t.cell(0, 4))
    t.cell(0, 1).text = "材料与新能源学院"
    t.cell(0, 5).text = "班级"
    t.cell(0, 6).text = "新材25-01"
    t.cell(1, 0).merge(t.cell(1, 1))
    t.cell(1, 0).text = "姓名"
    t.cell(1, 2).text = "学号"
    t.cell(1, 3).text = "2025444212"
    d.save(str(p))
    return p


def test_merged_cell_read_side_gives_column_numbers():
    """The read side must give column numbers and not repeat merged values.

    A plain pipe-joined row with no column numbers forces the model to count
    cells to infer columns, and it gets them wrong. A merged region must
    appear once, annotated with its span:

        c1=材料与新能源学院[合并 c1-c4]
    """
    from skills.office_parser.handler import parse_docx
    p = _make_merged_docx("_test_merged_read.docx")
    try:
        text = "\n".join(c["text"] for c in parse_docx(str(p)))
        assert "c1=材料与新能源学院" in text, text[:300]
        assert "[合并 c1-c4]" in text, text[:300]
        # 值只许出现一次（旧形状是 4 次）
        assert text.count("材料与新能源学院") == 1, (
            f"合并区的值被重复输出了：{text.count('材料与新能源学院')} 次")
        # 普通格子不许被标成合并
        assert "班级[合并" not in text, "没合并的格子被标成了合并区"
        assert "c0=学院" in text and "c5=班级" in text, text[:300]
    finally:
        p.unlink(missing_ok=True)


def test_merged_cell_write_inside_region_is_refused():
    """Writing inside a merged region (not at its origin) is refused.

    Writing `""` to `r0c2` also blanks `r0c1` -- the cell that holds the real
    value -- because both map to the same underlying cell. The refusal names
    the origin (`row_index=0`, `col_index=1`) and the full region.
    """
    from skills.office_parser.word_tools import format_table_cell_text
    p = _make_merged_docx("_test_merged_write.docx")
    try:
        r = format_table_cell_text(str(p), 0, 0, 2, text_content="X")
        assert not r["success"], r
        assert r["merged_cell"]["origin"] == [0, 1], r["merged_cell"]
        assert r["merged_cell"]["region"] == [0, 0, 1, 4], r["merged_cell"]
        assert "row_index=0" in r["hint"] and "col_index=1" in r["hint"], r["hint"]
    finally:
        p.unlink(missing_ok=True)


def test_merged_cell_write_at_origin_works_and_verifies():
    """Writing at the merge origin is the only valid form, and is verified.

    `verified` pins the post-write read-back: without it the read-back could
    be removed and nothing would fail.
    """
    from skills.office_parser.word_tools import format_table_cell_text
    from skills.office_parser.handler import parse_docx
    p = _make_merged_docx("_test_merged_origin.docx")
    try:
        r = format_table_cell_text(str(p), 0, 0, 1, text_content="管理学院")
        assert r["success"], r
        assert r.get("verified") is True, r
        text = "\n".join(c["text"] for c in parse_docx(str(p)))
        assert "c1=管理学院[合并 c1-c4]" in text, text[:300]
        # 起点那一格改了、邻居没有变成 4 个值（合并区仍然是一个值）
        assert text.count("管理学院") == 1, text
    finally:
        p.unlink(missing_ok=True)


def test_merged_cell_vertical_merge_detected():
    """垂直合并同样要拦（判据是"这一笔会改到哪儿"，与方向无关）。"""
    from docx import Document
    from skills.office_parser.word_tools import format_table_cell_text
    p = Path("workspace/_test_vmerge.docx")
    d = Document()
    t = d.add_table(rows=3, cols=2)
    t.cell(0, 0).merge(t.cell(2, 0))       # 纵向合并 c0 的 r0-r2
    t.cell(0, 0).text = "纵跨"
    t.cell(1, 1).text = "x"
    d.save(str(p))
    try:
        # 落在合并区内部的行（r1 是"续行"，起点是 r0）
        # 签名是 (file_path, table_index, row_index, col_index, ...)
        r = format_table_cell_text(str(p), 0, 1, 0, text_content="Y")
        assert not r["success"], r
        assert r["merged_cell"]["origin"] == [0, 0], r["merged_cell"]
        assert r["merged_cell"]["region"] == [0, 2, 0, 0], r["merged_cell"]
    finally:
        p.unlink(missing_ok=True)


# ------------------------------------------------------------------
# Excel finishing helpers + Word table row/column operations
# ------------------------------------------------------------------

def _mk_xlsx(name: str) -> Path:
    from openpyxl import Workbook
    p = Path("workspace") / name
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["姓名", "分数"])
    ws.append(["甲", 90])
    ws.append(["乙", 50])
    wb.save(str(p))
    wb.close()
    return p


def test_freeze_excel_panes_sets_and_clears():
    from openpyxl import load_workbook
    from skills.office_parser.excel_tools import freeze_excel_panes
    _init_handler()
    p = _mk_xlsx("_test_freeze.xlsx")
    try:
        r = freeze_excel_panes(str(p), "S1", rows=1, cols=0)
        assert r["success"], r
        wb = load_workbook(str(p))
        # openpyxl convention: freeze_panes = top-left of the unfrozen area
        # => freezing one row leaves row 2, column 1 at that corner
        assert wb["S1"].freeze_panes == "A2", wb["S1"].freeze_panes
        wb.close()
        r = freeze_excel_panes(str(p), "S1", rows=0, cols=0)
        assert r["success"], r
        wb = load_workbook(str(p))
        assert wb["S1"].freeze_panes is None, wb["S1"].freeze_panes
        wb.close()
        assert not freeze_excel_panes(str(p), "无此表")["success"]
    finally:
        p.unlink(missing_ok=True)


def test_set_excel_conditional_format_validates_input():
    """坏 operator / 坏颜色必须拒（不是静默加一条没用的规则）。"""
    from openpyxl import load_workbook
    from skills.office_parser.excel_tools import set_excel_conditional_format as cf
    _init_handler()
    p = _mk_xlsx("_test_cf.xlsx")
    try:
        r = cf(str(p), "S1", "B2:B3", rule_type="cellIs",
               operator="greaterThan", formula=["60"], fill_color="C6EFCE")
        assert r["success"], r
        wb = load_workbook(str(p))
        assert len(wb["S1"].conditional_formatting._cf_rules) == 1
        wb.close()
        assert not cf(str(p), "S1", "B2:B3", operator="bogus")["success"]
        assert not cf(str(p), "S1", "B2:B3", fill_color="zzz")["success"]
        assert not cf(str(p), "S1", "B2:B3", operator="between",
                      formula=["1"])["success"], "between 只要一个值应当拒"
    finally:
        p.unlink(missing_ok=True)


def test_add_excel_data_validation_list():
    from openpyxl import load_workbook
    from skills.office_parser.excel_tools import add_excel_data_validation as dv
    _init_handler()
    p = _mk_xlsx("_test_dv.xlsx")
    try:
        r = dv(str(p), "S1", "C2:C3", validation_type="list",
               formula1="合格,不合格")
        assert r["success"], r
        wb = load_workbook(str(p))
        rules = wb["S1"].data_validations.dataValidation
        assert len(rules) == 1 and rules[0].type == "list", rules
        # 字面选项必须被引号包住（openpyxl 的硬要求，替模型补上）
        assert rules[0].formula1 == '"合格,不合格"', rules[0].formula1
        wb.close()
        assert not dv(str(p), "S1", "C2", validation_type="bogus",
                      formula1="x")["success"]
        assert not dv(str(p), "S1", "C2", formula1="")["success"]
    finally:
        p.unlink(missing_ok=True)


def test_excel_data_validation_reads_back():
    """Validation rules must be readable after they are written.

    `read_excel_data(include_validation=True)` must not silently degrade to
    `{"has_validation": False}`. If the validation lookup raises, the value
    must be "unknown" (`None`), not a definite False that looks like a
    conclusion. The write side works; the risk is a write that cannot be
    read back.

    Three cases:
      1. cell with a rule -> `has_validation: True` + type + allowed values
      2. cell without a rule -> `has_validation: False` (a real False)
      3. range reference (`=$E$1:$E$2`) -> expanded into allowed values
    """
    from skills.office_parser.excel_tools import (
        add_excel_data_validation as dv, read_excel_data as rd, write_excel_data,
    )
    _init_handler()
    p = _mk_xlsx("_test_dv_read.xlsx")
    try:
        assert dv(str(p), "S1", "C2:C3", validation_type="list",
                  formula1="合格,不合格")["success"]
        # 区域引用的那一支：候选值写在 E 列，规则指过去
        assert write_excel_data(str(p), "S1", [["优秀"], ["良好"]],
                                start_cell="E1")["success"]
        assert dv(str(p), "S1", "D2", validation_type="list",
                  formula1="=$E$1:$E$2")["success"]

        r = rd(str(p), "S1", "A1", "E3", include_validation=True)
        assert r["success"], r
        by_addr = {c["address"]: c for c in r["cells"]}

        c2 = by_addr["C2"]["validation"]
        assert c2["has_validation"] is True, (
 "写了验证却读不回来 —— 复发（模块不存在 / 异常被吞）: %r" % c2)
        assert c2["validation_type"] == "list", c2
        assert c2["allowed_values"] == ["合格", "不合格"], c2

        # 带区号范围外的格子：这才是真的"没有"
        assert by_addr["A1"]["validation"]["has_validation"] is False, by_addr["A1"]

        d2 = by_addr["D2"]["validation"]
        assert d2["has_validation"] is True and d2.get("allowed_values") == ["优秀", "良好"], d2
    finally:
        p.unlink(missing_ok=True)


def test_excel_validation_unreadable_is_unknown_not_false():
    """Unreadable is not absent: a failed lookup must not collapse to False.

    Negative control: changing `has_validation: None` back to `False` in
    `read_excel_data` makes this test fail immediately. The invariant is
    about the shape (unknown vs. absent), not one particular defect.
    """
    from skills.office_parser import cell_validation as cv
    from skills.office_parser.excel_tools import read_excel_data as rd
    _init_handler()
    p = _mk_xlsx("_test_dv_unknown.xlsx")
    real = cv.get_data_validation_for_cell
    try:
        def _boom(ws, addr):
            raise RuntimeError("模拟：规则读不出来")
        cv.get_data_validation_for_cell = _boom
        r = rd(str(p), "S1", "A1", "B1", include_validation=True)
        assert r["success"], r
        for c in r["cells"]:
            v = c["validation"]
            assert v["has_validation"] is None, (
                "查不出来却报了一个确定的 %r —— 那是个看起来像结论的数"
                % (v["has_validation"],))
            assert "模拟" in v.get("error", ""), v
    finally:
        cv.get_data_validation_for_cell = real
        p.unlink(missing_ok=True)


def test_add_table_rows_then_fill_by_coordinates():
    """加行 → 用 0 基坐标填内容（坐标体系与 `parse_docx` 打出的 `cN` 一致）。"""
    from docx import Document
    from skills.office_parser.word_tools import add_table_rows, format_table_cell_text
    _init_handler()
    p = Path("workspace/_test_addrows.docx")
    d = Document()
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "a"
    t.cell(0, 1).text = "b"
    d.save(str(p))
    try:
        r = add_table_rows(str(p), 0, count=2)
        assert r["success"] and r["row_count"] == 3, r
        fs = format_table_cell_text(str(p), 0, 2, 1, text_content="新值")
        assert fs["success"] and fs.get("verified"), fs
        doc = Document(str(p))
        assert doc.tables[0].cell(2, 1).text == "新值"
    finally:
        p.unlink(missing_ok=True)


def test_table_columns_refused_when_horizontally_merged():
    """fail-closed：有横向合并就拒加/删列（否则产出一份坏表）。

    判据不是"能不能做"，是"做出来 Word 打开会不会报表格损坏" ——
    加列要同时动 `w:tblGrid` 与每行的 `w:tc`，而合并区里的格子跨多列。
    """
    from docx import Document
    from skills.office_parser.word_tools import (
        add_table_columns, delete_table_columns,
    )
    _init_handler()
    p = Path("workspace/_test_mgcols.docx")
    d = Document()
    t = d.add_table(rows=2, cols=3)
    t.cell(0, 0).merge(t.cell(0, 1))
    d.save(str(p))
    try:
        assert not add_table_columns(str(p), 0)["success"]
        assert not delete_table_columns(str(p), 0, 0)["success"]
    finally:
        p.unlink(missing_ok=True)


def test_delete_table_rows_refuses_vertical_merge():
    from docx import Document
    from skills.office_parser.word_tools import delete_table_rows
    _init_handler()
    p = Path("workspace/_test_vmrow.docx")
    d = Document()
    t = d.add_table(rows=3, cols=2)
    t.cell(0, 0).merge(t.cell(2, 0))          # 纵跨 r0-r2
    d.save(str(p))
    try:
        r = delete_table_rows(str(p), 0, 1, 1)     # 中间那行属于纵跨区
        assert not r["success"], r
        assert "纵跨" in r["error"], r["error"]
        # 右边那一列没合并，删它所在的行也要拒（整行都在合并区里）
        assert not delete_table_rows(str(p), 0, 0, 1)["success"]
    finally:
        p.unlink(missing_ok=True)


def test_add_and_delete_table_columns_roundtrip():
    from docx import Document
    from skills.office_parser.word_tools import (
        add_table_columns, delete_table_columns,
    )
    _init_handler()
    p = Path("workspace/_test_cols.docx")
    d = Document()
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "a"
    t.cell(0, 1).text = "b"
    t.cell(1, 0).text = "c"
    t.cell(1, 1).text = "d"
    d.save(str(p))
    try:
        r = add_table_columns(str(p), 0, count=2)
        assert r["success"] and r["column_count"] == 4, r
        doc = Document(str(p))
        assert len(doc.tables[0].columns) == 4
        assert doc.tables[0].cell(0, 1).text == "b", "加列不能动已有内容"
        r2 = delete_table_columns(str(p), 0, 0, 1)
        assert r2["success"] and r2["column_count"] == 3, r2
        doc = Document(str(p))
        assert doc.tables[0].cell(0, 0).text == "b", "删列后剩下的是原来的第二列"
        assert not delete_table_columns(str(p), 0, 9)["success"], "越界应当拒"
    finally:
        p.unlink(missing_ok=True)


def test_export_document_missing_file_reports_error():
    """A missing source file is an error that names a route.

    The realistic failure on this path is "the file is no longer there"
    (deleted, wrong path, or cleaned up after the session expired). The
    error must carry more than the word "failed".
    """
    from skills.office_parser.handler import export_document
    from skills.office_parser.session import reset_store
    reset_store()
    result = export_document("workspace/_nope_does_not_exist.docx", "x.docx")
    assert not result["success"], result
    assert result.get("error"), result


def test_a_long_document_can_still_take_an_add_block():
    """A document written in one shot must not lock `add_block`.

    `create_document(content=...)` can write 30 blocks. A following
    `add_block(image)` must still succeed -- it is the first interactive
    append on that document. Otherwise the model has to write python-docx
    itself to insert the image.

    The 20-block cap is meant for interactive unlimited appends;
    `create_document` is a different operation whose length is already
    bounded by the model output. The block counter must therefore start
    from the document's existing baseline, so the receipt does not
    contradict itself (`Block 31/20`).

    Negative control: removing `- session.append_base` in `add_block`
    makes this test fail immediately.
    """
    from skills.office_parser.handler import add_block, create_document
    _init_handler()
    # 造 30 块（15 节 x 标题+正文）
    body = "\n\n".join("## 第%d节\n\n第 %d 节的正文。" % (i, i) for i in range(1, 16))
    r = create_document(doc_type="word", title="长报告", content=body)
    assert r["success"], r
    assert r["blocks_written"] >= 20, (
        "夹具不对：没造出超过 20 块的文档（blocks_written=%r）" % r.get("blocks_written"))
    out = Path(r["file_path"])
    try:
        a = add_block(session_id=r["session_id"], block_type="paragraph",
                      content="追加的一行")
        assert a.get("success") is True, (
            "★ 长文档被 20 块上限锁死（第一次 add_block 就被拒）—— "
            "模型只能自己写 python-docx 绕过，实际运行中就是这样：%r" % a)
        assert a.get("block_count", 0) >= 1, (
            "块号没从基线往上数（回执会自相矛盾地写 Block 31/20）：%r"
            % a.get("block_count"))
    finally:
        out.unlink(missing_ok=True)
    print("  [ok] %d 块的长文档仍然能 add_block（块号从基线起算）"
          % r["blocks_written"])


# ------------------------------------------------------------------
# Markdown pipe tables in `content` become real tables
# ------------------------------------------------------------------

def test_markdown_table_in_content_becomes_a_real_table():
    """Markdown pipe tables in `content` must become real tables.

    Passing a whole Markdown document with tables into `content` must not
    leave the pipes as literal text (`tables == 0`) while still returning
    `success: True`. A receipt that is more confident than the artifact is a
    defect in itself.

    The separator row is not data: header + 2 data rows = 3 rows.

    Negative control: removing the `elif bt == "table"` branch in the
    `create_document` write loop makes this test fail (block count still
    reported, table absent).
    """
    from skills.office_parser.handler import create_document
    from docx import Document
    _init_handler()
    body = (
        "## 经营情况\n\n"
        "| 指标 | 累计完成 | 年计划 |\n"
        "| --- | ---: | :---: |\n"
        "| 营业收入 | 1506.28 | 1997 |\n"
        "| 利润总额 | 951.66 | 1270 |\n"
    )
    r = create_document(doc_type="word", title="表测试", content=body)
    assert r["success"], r
    out = Path(r["file_path"])
    try:
        tables = Document(str(out)).tables
        assert len(tables) == 1, (
 "★ content 里的管道表没被建成表（tables=%d）—— 正是现场"
            % len(tables))
        t = tables[0]
        assert len(t.rows) == 3, (
            "分隔行不该进 rows（表头+2 行数据 = 3 行，实得 %d）" % len(t.rows))
        assert [c.text for c in t.rows[0].cells] == ["指标", "累计完成", "年计划"], \
            [c.text for c in t.rows[0].cells]
        assert [c.text for c in t.rows[1].cells] == ["营业收入", "1506.28", "1997"], \
            [c.text for c in t.rows[1].cells]
        assert [c.text for c in t.rows[2].cells] == ["利润总额", "951.66", "1270"], \
            [c.text for c in t.rows[2].cells]
    finally:
        out.unlink(missing_ok=True)
    print("  [ok] content 里的管道表建成了 3 行真表格")


def test_pipe_lines_without_a_separator_are_still_paragraphs():
    """Negative control: pipe lines without a separator stay paragraphs.

    The table rule has one criterion: the next line is a separator row.
    Relaxing it swallows ordinary prose such as `甲|乙`. Detection may be
    conservative, but it must never rewrite content it should not touch.
    """
    from skills.office_parser.handler import create_document
    from docx import Document
    _init_handler()
    body = "| 甲 | 乙 |\n| 丙 | 丁 |\n"
    r = create_document(doc_type="word", title="不是表", content=body)
    out = Path(r["file_path"])
    try:
        d = Document(str(out))
        assert len(d.tables) == 0, "没有分隔行却建了表（判据被放宽了）"
        joined = "\n".join(p.text for p in d.paragraphs)
        assert "| 甲 | 乙 |" in joined, repr(joined[:120])
    finally:
        out.unlink(missing_ok=True)
    print("  [ok] 无分隔行 -> 仍是段落（没乱吃文本）")


def test_table_separator_variants_and_ragged_rows():
    """分隔行的冒号对齐、缺尾竖线、参差不齐的列数都要认，且不许崩。"""
    from skills.office_parser.handler import _parse_document_text as P
    b = P("| a | b\n| :-- | --: |\n| 1 | 2 | 3 |")
    assert len(b) == 1 and b[0]["type"] == "table", b
    assert b[0]["rows"] == [["a", "b"], ["1", "2", "3"]], b[0]["rows"]
    # 单列表 + 首尾空格
    b2 = P("| 只有一列 |\n| --- |\n|  值  |")
    assert b2[0]["rows"] == [["只有一列"], ["值"]], b2[0]["rows"]
    print("  [ok] 冒号对齐 / 缺尾竖线 / 参差列数 / 单列表 都认")


def test_blocks_keep_their_order_with_a_table_in_the_middle():
    """块序不许被打乱：标题 → 表格 → 列表。"""
    from skills.office_parser.handler import _parse_document_text as P
    b = P("# 标题\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n- 甲\n- 乙\n")
    assert [x["type"] for x in b] == ["heading", "table", "list"], \
        [x["type"] for x in b]
    print("  [ok] 标题 / 表格 / 列表 的块序不变")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        # DocSession / Store (7)
        ("test_doc_session_create", test_doc_session_create),
        ("test_doc_session_touch", test_doc_session_touch),
        ("test_doc_session_expiry", test_doc_session_expiry),
        ("test_store_create_and_get", test_store_create_and_get),
        ("test_store_ttl_eviction", test_store_ttl_eviction),
        ("test_store_finalize", test_store_finalize),
        ("test_store_max_sessions", test_store_max_sessions),
        # create_document (4)
        ("test_create_document_word", test_create_document_word),
        ("test_create_document_ppt", test_create_document_ppt),
        ("test_create_document_excel", test_create_document_excel),
        # plain-text writing
        ("test_create_document_txt", test_create_document_txt),
        ("test_create_document_txt_rejects_path_traversal",
         test_create_document_txt_rejects_path_traversal),
        ("test_create_document_invalid_type", test_create_document_invalid_type),
        # add_block (8)
        ("test_add_block_heading", test_add_block_heading),
        ("test_add_block_paragraph", test_add_block_paragraph),
        ("test_add_block_table", test_add_block_table),
        ("test_add_block_list", test_add_block_list),
        ("test_add_block_page_break", test_add_block_page_break),
        ("test_add_block_invalid_session", test_add_block_invalid_session),
        ("test_add_block_invalid_type", test_add_block_invalid_type),
        # block handle reachable by file path
        ("test_add_block_session_reachable_by_file_path",
         test_add_block_session_reachable_by_file_path),
        ("test_add_block_cap_is_enforced", test_add_block_cap_is_enforced),
        # add_slide (3)
        ("test_add_slide_title", test_add_slide_title),
        ("test_add_slide_content", test_add_slide_content),
        ("test_add_slide_invalid_type", test_add_slide_invalid_type),
        # write_excel_data (2)
        ("test_write_excel_data_creates_file", test_write_excel_data_creates_file),
        ("test_write_excel_data_appends_rows", test_write_excel_data_appends_rows),
        # export_document (2)
        ("test_export_document_renames_word_file",
         test_export_document_renames_word_file),
        ("test_export_document_renders_ppt", test_export_document_renders_ppt),
        # merged cells
        ("test_merged_cell_read_side_gives_column_numbers",
         test_merged_cell_read_side_gives_column_numbers),
        ("test_merged_cell_write_inside_region_is_refused",
         test_merged_cell_write_inside_region_is_refused),
        ("test_merged_cell_write_at_origin_works_and_verifies",
         test_merged_cell_write_at_origin_works_and_verifies),
        ("test_merged_cell_vertical_merge_detected",
         test_merged_cell_vertical_merge_detected),
        # Excel finishing helpers + Word table row/column operations
        ("test_freeze_excel_panes_sets_and_clears",
         test_freeze_excel_panes_sets_and_clears),
        ("test_set_excel_conditional_format_validates_input",
         test_set_excel_conditional_format_validates_input),
        # long documents must not be locked by the 20-block cap
        ("test_a_long_document_can_still_take_an_add_block",
         test_a_long_document_can_still_take_an_add_block),
        ("test_add_excel_data_validation_list",
         test_add_excel_data_validation_list),
        # validation rules readable back; unreadable is not absent
        ("test_excel_data_validation_reads_back",
         test_excel_data_validation_reads_back),
        ("test_excel_validation_unreadable_is_unknown_not_false",
         test_excel_validation_unreadable_is_unknown_not_false),
        ("test_add_table_rows_then_fill_by_coordinates",
         test_add_table_rows_then_fill_by_coordinates),
        ("test_table_columns_refused_when_horizontally_merged",
         test_table_columns_refused_when_horizontally_merged),
        ("test_delete_table_rows_refuses_vertical_merge",
         test_delete_table_rows_refuses_vertical_merge),
        ("test_add_and_delete_table_columns_roundtrip",
         test_add_and_delete_table_columns_roundtrip),
        ("test_export_document_missing_file_reports_error",
         test_export_document_missing_file_reports_error),
        # Markdown pipe tables in content -> real tables (positive + negative)
        ("test_markdown_table_in_content_becomes_a_real_table",
         test_markdown_table_in_content_becomes_a_real_table),
        ("test_pipe_lines_without_a_separator_are_still_paragraphs",
         test_pipe_lines_without_a_separator_are_still_paragraphs),
        ("test_table_separator_variants_and_ragged_rows",
         test_table_separator_variants_and_ragged_rows),
        ("test_blocks_keep_their_order_with_a_table_in_the_middle",
         test_blocks_keep_their_order_with_a_table_in_the_middle),
    ]

    passed = 0
    failed = 0
    before = prod_snapshot()

    with _sandbox():
        for name, fn in tests:
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")

    # Production-directory guard: this file must never write there
    # Every test runs inside `_sandbox()`; the snapshot below proves the
    # production tree is byte-for-byte unchanged.
    # The criterion is `(path, size, mtime)`, not a set of paths --
    #    comparing paths alone reports "no change" when an existing file was
    #    rewritten.
    leaked = prod_diff(before, prod_snapshot())
    if leaked:
        failed += 1
        print("\n  FAIL  改动了生产目录 %d 处：%r"
              % (len(leaked), sorted(leaked.items())[:5]))
    else:
        print("\n  [ok] 生产目录逐字节未变")

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
