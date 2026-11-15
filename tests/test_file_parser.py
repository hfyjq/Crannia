"""
Tests for the file_parser PDF operations (pdf_info, merge_pdfs,
split_pdf, extract_pages).

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_file_parser.py
"""

import contextlib
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sandbox import (  # noqa: E402
    sandbox as _sandbox_ctx,
    tempdir as _tempdir,
    tmp_audit_log as _tmp_audit_log,
)

#: 沙箱的短别名 —— 正文里读起来不吵。
sandbox = _sandbox_ctx

# ------------------------------------------------------------------
# Test fixtures
# ------------------------------------------------------------------

#: 夹具是现造的，不读生产工作区里的任何文件。
#:
#: 原来这里指向 `workspace/` 下的一份真实研报 —— 那是某台机器上恰好存在的
#: 用户文档，换台机器（或公开仓，那里 `workspace/` 是空的）就整批红。
#: 判据不是"有没有那份文件"，是"解析器能不能处理一份真 PDF"，所以造一份
#: 五页的：有二进制流、有多页（merge / split / 指定页范围都要用）。
#:
#: 绝对路径：进沙箱做 PDF 操作的用例，沙箱里 `workspace/` 是空的，
#: 只有绝对路径才读得到。只读，不写 —— 产物一律进沙箱（见 `_pdf_sandbox`）。
@lru_cache(maxsize=1)
def _fixture_pdf() -> Path:
    """造一份五页的真 PDF（有二进制流），返回它的绝对路径。"""
    from reportlab.pdfgen import canvas

    path = _tempdir("crannia-file-parser-") / "sample-report.pdf"
    c = canvas.Canvas(str(path))
    c.setFont("Helvetica", 12)
    for page in range(1, 6):
        c.drawString(72, 720, "Crannia fixture, page %d." % page)
        c.showPage()
    c.save()
    return path


_WORKSPACE = Path("workspace")


def _setup_security():
    """Initialize a minimal SecurityGuard for testing."""
    from core.security import SecurityGuard
    from skills.file_parser.handler import init
    guard = SecurityGuard(
        allowed_dirs=["./workspace"],
        command_blacklist=[],
        audit_log_path=_tmp_audit_log(),
    )
    init(guard)
    return guard


def _cleanup_outputs(*names: str):
    """Remove test output files from workspace."""
    for name in names:
        p = _WORKSPACE / name
        if p.exists():
            p.unlink()


# ------------------------------------------------------------------
# pdf_info tests
# ------------------------------------------------------------------


@contextlib.contextmanager
def _pdf_sandbox():
    """一个沙箱，并把仓里那份真 PDF 拷进去；yield 沙箱里那份的路径（字符串）。

    ## 它防的是净零污染

    `merge_pdfs` / `split_pdf` / `extract_pages` 这些用例若直接在**真的
    `workspace/`** 上操作、跑完自己清掉，净变化为零，
    `scripts/check_test_pollution.py` 的端到端快照（比 `(路径, 大小, mtime)`
    的前后）看不见它，一直报"干净"。而它们确实在生产目录上动手：
    并行跑测试时会撞进别人的 `prod_snapshot()` / `prod_diff()` 窗口。

    用它的用例：输入输出都在沙箱里 —— 不管那个工具是"写在输入旁边"
    还是"写在 `./workspace`"，产物都跑不出去。
    """
    with sandbox():
        dst = Path("workspace") / _fixture_pdf().name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_fixture_pdf(), dst)
        yield str(dst)


def test_pdf_info_valid():
    """pdf_info returns metadata for a valid PDF."""
    _setup_security()
    from skills.file_parser.handler import pdf_info

    result = pdf_info(str(_fixture_pdf()))
    assert result["success"], f"Expected success, got: {result}"
    meta = result["metadata"]
    assert meta["page_count"] > 0, "Should have pages"
    assert meta["file_name"] == _fixture_pdf().name
    assert isinstance(meta["file_size_bytes"], int)
    assert meta["file_size_bytes"] > 0
    assert "is_encrypted" in meta


def test_pdf_info_nonexistent():
    """pdf_info returns error for nonexistent file."""
    _setup_security()
    from skills.file_parser.handler import pdf_info

    result = pdf_info("workspace/nonexistent_file_12345.pdf")
    assert not result["success"], "Should fail for nonexistent file"
    assert "error" in result


def test_a_missing_file_says_where_it_looked():
    """解析类工具找不到文件时，要说清查过哪儿、下一步给什么。

    失败形状：下游 `open()` 只抛一行 `No such file or directory: <路径>`
    —— 模型拿到它只能猜（实测：它连试两次同一个裸文件名，然后才去 `cp`）。
    """
    _setup_security()
    from skills.file_parser.handler import parse_pdf

    try:
        parse_pdf("绝对不存在的文件_12345.pdf")
        assert False, "找不到文件时必须抛"
    except FileNotFoundError as exc:
        msg = str(exc)
        assert "找不到这个文件" in msg, msg
        assert "完整路径" in msg, msg


def test_pdf_info_not_a_pdf():
    """pdf_info returns error for non-PDF file."""
    _setup_security()
    from skills.file_parser.handler import pdf_info

    result = pdf_info(str(Path("workspace/te1.txt")))
    assert not result["success"], "Should reject non-PDF file"
    assert "PDF" in result["error"]


# ------------------------------------------------------------------
# merge_pdfs tests
# ------------------------------------------------------------------

def test_merge_pdfs_two_copies():

    # 输入输出都在沙箱里 —— 不在真的 `workspace/` 上做 PDF 操作，
    #   产物也就不会落进别人的观察窗口。见 `_pdf_sandbox` 的 docstring。
    with _pdf_sandbox() as _p:
        """Merge a PDF with itself produces double-page output."""
        _setup_security()
        from skills.file_parser.handler import merge_pdfs

        # Merge the same PDF twice (simulates two files)
        result = merge_pdfs(
            [_p, _p],
            output_name="test_merge_double.pdf",
        )
        assert result["success"], f"Merge failed: {result}"
        assert result["source_count"] == 2
        assert result["total_pages"] > 0

        # Verify output exists
        out_path = Path(result["output_path"])
        assert out_path.exists()
        assert out_path.stat().st_size > 0

        _cleanup_outputs("test_merge_double.pdf")


def test_merge_pdfs_empty_paths():
    """merge_pdfs returns error for empty file_paths."""
    _setup_security()
    from skills.file_parser.handler import merge_pdfs

    result = merge_pdfs([])
    assert not result["success"]
    assert "empty" in result["error"].lower()


def test_merge_pdfs_nonexistent():
    """merge_pdfs returns error for nonexistent file."""
    _setup_security()
    from skills.file_parser.handler import merge_pdfs

    result = merge_pdfs([str(_fixture_pdf()), "workspace/nonexistent.pdf"])
    assert not result["success"]


def test_merge_pdfs_auto_adds_pdf_extension():

    # 输入输出都在沙箱里 —— 不在真的 `workspace/` 上做 PDF 操作，
    #   产物也就不会落进别人的观察窗口。见 `_pdf_sandbox` 的 docstring。
    with _pdf_sandbox() as _p:
        """merge_pdfs auto-appends .pdf to output_name."""
        _setup_security()
        from skills.file_parser.handler import merge_pdfs

        result = merge_pdfs(
            [_p],
            output_name="test_no_ext",
        )
        assert result["success"]
        assert result["output_path"].endswith(".pdf")

        _cleanup_outputs("test_no_ext.pdf")


# ------------------------------------------------------------------
# split_pdf tests
# ------------------------------------------------------------------

def test_split_pdf_single_page_per_file():

    # 输入输出都在沙箱里 —— 不在真的 `workspace/` 上做 PDF 操作，
    #   产物也就不会落进别人的观察窗口。见 `_pdf_sandbox` 的 docstring。
    with _pdf_sandbox() as _p:
        """Split PDF into 1 page per file — verify count."""
        _setup_security()
        from skills.file_parser.handler import split_pdf

        result = split_pdf(_p, pages_per_file=10)
        assert result["success"], f"Split failed: {result}"
        assert result["file_count"] > 0
        assert result["source_page_count"] > 0

        # Each file should have exactly 10 pages (except possibly the last)
        for f in result["output_files"]:
            assert 1 <= f["pages"] <= 10
            # Clean up
            Path(f["path"]).unlink(missing_ok=True)


def test_split_pdf_nonexistent():
    """split_pdf returns error for nonexistent file."""
    _setup_security()
    from skills.file_parser.handler import split_pdf

    result = split_pdf("workspace/nonexistent.pdf")
    assert not result["success"]


def test_split_pdf_custom_pattern():
    """split_pdf with custom output pattern.

    这条必须沙箱化。若在真的 `workspace/` 里建 `custom_N.pdf`、
    跑完自己 `unlink` 掉，净变化为零，`scripts/check_test_pollution.py`
    的端到端快照（`(路径, 大小, mtime)` 比前后）看不见它，一直报"干净"。
    而它确实在生产目录上动手：并行跑测试时会撞进别人的
    `prod_snapshot()` / `prod_diff()` 观察窗口，让无关用例偶发报「污染了生产目录」。

    做法：输入也搬进沙箱（把仓里那份真 PDF 拷进来）—— 这样不管
    `split_pdf` 是"写在输入旁边"还是"写在 `./workspace`"，产物都留在沙箱里。
    """
    _src = Path(__file__).resolve().parent.parent / _fixture_pdf()
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import split_pdf

        dst = Path("workspace") / _src.name
        dst.write_bytes(_src.read_bytes())

        result = split_pdf(
            str(dst),
            pages_per_file=20,
            output_pattern="custom_{n}.pdf",
        )
        assert result["success"]
        for f in result["output_files"]:
            assert "custom_" in f["path"]
        # 不再 `unlink` —— 沙箱会自己收；留着删反而掩盖"产物落在哪"


# ------------------------------------------------------------------
# extract_pages tests
# ------------------------------------------------------------------

def test_extract_pages_specific():

    # 输入输出都在沙箱里 —— 不在真的 `workspace/` 上做 PDF 操作，
    #   产物也就不会落进别人的观察窗口。见 `_pdf_sandbox` 的 docstring。
    with _pdf_sandbox() as _p:
        """Extract pages 1, 3, 5 from a PDF."""
        _setup_security()
        from skills.file_parser.handler import extract_pages

        result = extract_pages(
            _p,
            pages=[1, 3, 5],
            output_name="test_extracted.pdf",
        )
        assert result["success"], f"Extract failed: {result}"
        assert result["extracted_page_count"] == 3
        assert Path(result["output_path"]).exists()

        _cleanup_outputs("test_extracted.pdf")


def test_extract_pages_duplicates():

    # 输入输出都在沙箱里 —— 不在真的 `workspace/` 上做 PDF 操作，
    #   产物也就不会落进别人的观察窗口。见 `_pdf_sandbox` 的 docstring。
    with _pdf_sandbox() as _p:
        """Extract with duplicate page numbers produces copies."""
        _setup_security()
        from skills.file_parser.handler import extract_pages

        result = extract_pages(
            _p,
            pages=[1, 1, 1],
            output_name="test_dup.pdf",
        )
        assert result["success"]
        assert result["extracted_page_count"] == 3

        _cleanup_outputs("test_dup.pdf")


def test_extract_pages_out_of_range():
    """Extract returns error for out-of-range page number."""
    _setup_security()
    from skills.file_parser.handler import extract_pages

    result = extract_pages(str(_fixture_pdf()), pages=[99999])
    assert not result["success"]
    assert "out of range" in result["error"].lower()


def test_extract_pages_empty_list():
    """Extract returns error for empty pages list."""
    _setup_security()
    from skills.file_parser.handler import extract_pages

    result = extract_pages(str(_fixture_pdf()), pages=[])
    assert not result["success"]
    assert "empty" in result["error"].lower()


def test_extract_pages_nonexistent():
    """Extract returns error for nonexistent file."""
    _setup_security()
    from skills.file_parser.handler import extract_pages

    result = extract_pages("workspace/nonexistent.pdf", pages=[1])
    assert not result["success"]


# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# 文本解析器的二进制护栏
# ------------------------------------------------------------------
#
# 这一批自带 `sandbox()`，不要再往生产 `workspace/` 写。
# 夹具也在沙箱里现造：真实 PDF 用 reportlab 生成，不依赖 `_fixture_pdf()`
# 那份恰好躺在生产工作区里的文件。
# 夹具与断言用 reportlab + pypdf —— 不把已移除的 AGPL 依赖钉在测试里。

def _make_pdf(path: Path) -> None:
    """造一份最小的真 PDF（有二进制流）。"""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    c.setFont("Helvetica", 10)
    c.drawString(72, 72, "hello")
    c.showPage()
    c.save()


def test_parse_txt_refuses_binary_by_extension():
    """`.pdf` 交给 `parse_txt` 必须拒，且指出该用哪个工具。

    根因是编码阶梯的尾巴 `latin-1` 永不失败 —— 喂任何二进制都"成功"，
    模型会把乱码当成正文读下去，而且没有任何信号告诉它读错了。
    """
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import parse_txt
        _make_pdf(Path("workspace/real.pdf"))
        try:
            parse_txt("workspace/real.pdf")
            raise AssertionError("PDF 交给 parse_txt 竟然没被拒")
        except ValueError as e:
            assert "parse_pdf" in str(e), f"拒绝里必须带路线: {e}"


def test_parse_csv_refuses_binary_too():
    """同一个洞的第二处：`parse_csv` 的阶梯与 `parse_txt` 逐字相同。"""
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import parse_csv
        _make_pdf(Path("workspace/real.pdf"))
        try:
            parse_csv("workspace/real.pdf")
            raise AssertionError("PDF 交给 parse_csv 竟然没被拒")
        except ValueError as e:
            assert "parse_pdf" in str(e), e


def test_parse_txt_refuses_binary_by_content():
    """扩展名是 `.txt`、内容是二进制的，靠内容嗅探拦。

    这一段是"扩展名名单"的兜底 —— 名单本身会漏（新格式、改错的扩展名）。
    """
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import parse_txt
        p = Path("workspace/_test_bin_disguised.txt")
        p.write_bytes(bytes(range(0, 32)) * 40)
        try:
            parse_txt(str(p))
            raise AssertionError("伪装成 .txt 的二进制竟然没被拒")
        except ValueError as e:
            assert "二进制" in str(e), e


def test_parse_txt_still_reads_chinese_gbk():
    """误报检查 —— 中文 GBK 文本必须照读不误。

    这一条是上面那个护栏的代价护栏：GBK/UTF-8 的多字节全在 ≥0x80，
    而判据只看 ASCII 字节里控制字符的占比。如果哪天有人把判据改成
    "非 ASCII 字节算二进制"，中文文件会全部被拒 —— 而那是本工具
    最主要的使用场景。没有这条测试，那种改动不会有任何东西变红。
    """
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import parse_txt
        p = Path("workspace/_test_gbk.txt")
        p.write_bytes("这是一份中文文本文件\n第二行内容\n".encode("gbk"))
        chunks = parse_txt(str(p))
        assert chunks, "GBK 中文文本读出来是空的"
        assert "这是一份中文文本文件" in chunks[0]["text"], chunks[0]["text"][:40]


def test_parse_txt_plain_text_unaffected():
    """UTF-8 纯文本 / 中文 / CSV 的正常路径一个字节没变。"""
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import parse_txt, parse_csv
        p = Path("workspace/_test_plain.txt")
        p.write_text("中文 utf-8 内容\n" * 3, encoding="utf-8")
        c = Path("workspace/_test_plain.csv")
        c.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
        assert parse_txt(str(p)), "普通文本读不出来"
        assert parse_csv(str(c)), "普通 CSV 读不出来"


# ------------------------------------------------------------------
# PDF 页面操作族 + 图片族（11 件）
# ------------------------------------------------------------------
#
# 全部自带 `sandbox()` + 现造夹具。判据尽量落在产物本身
# （页数、页序、像素颜色、是否需要密码），而不是"工具说成功了"。

def _mk_pdf(path: Path, pages: int = 3, label: str = "page") -> None:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.setPageSize((200 + i * 10, 300))               # 每页宽度不同，可判页序
        c.setFont("Helvetica", 10)
        c.drawString(20, 280, f"{label} {i + 1}")
        c.showPage()
    c.save()


def _mk_img(path: Path, size=(80, 60), color=(255, 0, 0), mode="RGB") -> None:
    from PIL import Image
    Image.new(mode, size, color).save(str(path))


def test_images_to_pdf_builds_one_page_per_image():
    """图片 → PDF：页数 = 图片数，按给定顺序，不重排。"""
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import images_to_pdf
        from pypdf import PdfReader
        for i, c in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)], 1):
            _mk_img(Path(f"workspace/i{i}.png"), size=(60 + i * 20, 40), color=c)
        r = images_to_pdf(["workspace/i3.png", "workspace/i1.png"],
                          "out.pdf")
        assert r["success"], r
        assert r["page_count"] == 2, r
        pages = PdfReader(r["output_path"]).pages
        w0, w1 = float(pages[0].mediabox.width), float(pages[1].mediabox.width)
        assert w0 > w1, f"页序被改了：{w0} vs {w1}"


def test_images_to_pdf_rejects_empty_and_missing():
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import images_to_pdf
        assert not images_to_pdf([])["success"]
        assert not images_to_pdf(["workspace/nope.png"])["success"]


def test_pdf_to_images_renders_requested_pages():
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import pdf_to_images
        _mk_pdf(Path("workspace/s.pdf"), 3)
        r = pdf_to_images("workspace/s.pdf", pages=[1, 3], dpi=72)
        assert r["success"], r
        assert [f["page"] for f in r["output_files"]] == [1, 3], r
        for f in r["output_files"]:
            assert Path(f["path"]).exists() and f["bytes"] > 0, f


def test_rotate_pdf_pages_only_touches_listed_pages():
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import rotate_pdf_pages
        from pypdf import PdfReader
        _mk_pdf(Path("workspace/s.pdf"), 3)
        r = rotate_pdf_pages("workspace/s.pdf", angle=90, pages=[1])
        assert r["success"], r
        got = [p.rotation for p in PdfReader(r["output_path"]).pages]
        assert got == [90, 0, 0], got
        assert not rotate_pdf_pages("workspace/s.pdf", angle=45)["success"], \
            "非 90 的倍数应当被拒"


def test_reorder_pdf_pages_can_drop_and_duplicate():
    """`select` 的语义：不列出来的页就等于丢掉，列两次就等于复制。"""
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import reorder_pdf_pages
        from pypdf import PdfReader
        _mk_pdf(Path("workspace/s.pdf"), 3, label="P")
        r = reorder_pdf_pages("workspace/s.pdf", pages=[3, 1, 1])
        assert r["success"], r
        texts = [p.extract_text().strip()
                 for p in PdfReader(r["output_path"]).pages]
        assert texts == ["P 3", "P 1", "P 1"], texts
        assert not reorder_pdf_pages("workspace/s.pdf", pages=[9])["success"]


def test_pdf_watermark_handles_chinese_and_custom_angle():
    """中文水印必须能落进 PDF（内置字体，无需外部字体文件）。

    `insert_text(rotate=)` 只吃 0/90/180/270 —— 45 度会
    `ValueError: bad rotate value`，所以走 `morph`。
    """
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import pdf_watermark
        from pypdf import PdfReader
        _mk_pdf(Path("workspace/s.pdf"), 2)
        r = pdf_watermark("workspace/s.pdf", "内部资料 请勿外传",
                          output_name="wm.pdf", angle=45)
        assert r["success"], r
        assert r["font"] == "STSong-Light", r
        # 45° 旋转的文字：pdfplumber 会按视觉行重排成竖排；
        # pypdf 的 extract_text 按内容流顺序，逐字断言更稳。
        text = PdfReader(r["output_path"]).pages[0].extract_text() or ""
        assert "内部资料 请勿外传" in text, text


def test_compress_pdf_reports_real_sizes():
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import compress_pdf
        _mk_pdf(Path("workspace/s.pdf"), 3)
        r = compress_pdf("workspace/s.pdf")
        assert r["success"], r
        assert r["size_after_bytes"] == Path(r["output_path"]).stat().st_size, r
        assert r["saved_bytes"] == r["size_before_bytes"] - r["size_after_bytes"]


def test_encrypt_pdf_really_requires_a_password():
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import encrypt_pdf
        from pypdf import PdfReader
        _mk_pdf(Path("workspace/s.pdf"), 1)
        r = encrypt_pdf("workspace/s.pdf", "pw123", output_name="enc.pdf")
        assert r["success"], r
        reader = PdfReader(r["output_path"])
        assert reader.is_encrypted, "加了密却不需要密码 —— 等于没加密"
        assert reader.decrypt("wrong") == 0, "错误密码竟然通过了"
        assert reader.decrypt("pw123") != 0, "正确密码打不开"


def test_compress_image_and_resize_report_output_size():
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import compress_image, resize_image
        _mk_img(Path("workspace/i.png"), size=(200, 100))
        r = compress_image("workspace/i.png", quality=60, max_width=50)
        assert r["success"], r
        # 报的必须是产物尺寸（报原图会是一个看着对其实错的数）
        assert r["source_size"] == [200, 100], r
        assert r["output_size"] == [50, 25], r
        r2 = resize_image("workspace/i.png", width=40)
        assert r2["success"] and r2["output_size"] == [40, 20], r2
        assert not resize_image("workspace/i.png")["success"], "两个都不给应当报错"


def test_convert_image_format_fills_transparency_with_white():
    """转 jpg 丢透明通道时填白，不填黑（填黑看着像把图弄坏了）。"""
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import convert_image_format
        from PIL import Image
        p = Path("workspace/t.png")
        Image.new("RGBA", (20, 20), (255, 0, 0, 0)).save(str(p))   # 全透明
        r = convert_image_format(str(p), "jpg")
        assert r["success"], r
        px = Image.open(r["output_path"]).convert("RGB").getpixel((5, 5))
        assert px == (255, 255, 255), f"透明区没填白，实际 {px}"
        assert not convert_image_format(str(p), "gif")["success"], "不支持的格式应当拒"


def test_rotate_image_expands_canvas():
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import rotate_image
        from PIL import Image
        _mk_img(Path("workspace/i.png"), size=(80, 20))
        r = rotate_image("workspace/i.png", 90)
        assert r["success"], r
        im = Image.open(r["output_path"])
        assert (im.width, im.height) == (20, 80), (im.width, im.height)
        assert not rotate_image("workspace/i.png", 360)["success"], "360 度等于没转"


def test_new_tools_strip_path_traversal_from_output_name():
    """`_out_path()` 是这一族唯一的出口，它必须剥掉 `../`。

    输出路径是模型给的，直接拼就是任意写。
    这里同时钉住"按 write 校验"（`_out_path` 的另一半职责）。
    """
    with sandbox():
        _setup_security()
        from skills.file_parser.handler import images_to_pdf, compress_pdf
        _mk_img(Path("workspace/i.png"))
        _mk_pdf(Path("workspace/s.pdf"), 1)
        r = images_to_pdf(["workspace/i.png"], "../../evil.pdf")
        assert r["success"], r
        got = Path(r["output_path"]).resolve()
        assert got.parent.name == "workspace", f"逃出工作区了: {got}"
        assert got.name == "evil.pdf", got.name
        r2 = compress_pdf("workspace/s.pdf", output_name="../evil2.pdf")
        assert r2["success"], r2
        assert Path(r2["output_path"]).parent.name == "workspace", r2["output_path"]


if __name__ == "__main__":
    tests = [
        # pdf_info
        ("test_pdf_info_valid", test_pdf_info_valid),
        ("test_pdf_info_nonexistent", test_pdf_info_nonexistent),
        ("test_a_missing_file_says_where_it_looked",
         test_a_missing_file_says_where_it_looked),
        ("test_pdf_info_not_a_pdf", test_pdf_info_not_a_pdf),
        # merge_pdfs
        ("test_merge_pdfs_two_copies", test_merge_pdfs_two_copies),
        ("test_merge_pdfs_empty_paths", test_merge_pdfs_empty_paths),
        ("test_merge_pdfs_nonexistent", test_merge_pdfs_nonexistent),
        ("test_merge_pdfs_auto_adds_pdf_extension", test_merge_pdfs_auto_adds_pdf_extension),
        # split_pdf
        ("test_split_pdf_single_page_per_file", test_split_pdf_single_page_per_file),
        ("test_split_pdf_nonexistent", test_split_pdf_nonexistent),
        ("test_split_pdf_custom_pattern", test_split_pdf_custom_pattern),
        # extract_pages
        ("test_extract_pages_specific", test_extract_pages_specific),
        ("test_extract_pages_duplicates", test_extract_pages_duplicates),
        ("test_extract_pages_out_of_range", test_extract_pages_out_of_range),
        ("test_extract_pages_empty_list", test_extract_pages_empty_list),
        ("test_extract_pages_nonexistent", test_extract_pages_nonexistent),
        # 文本解析器的二进制护栏
        ("test_parse_txt_refuses_binary_by_extension",
         test_parse_txt_refuses_binary_by_extension),
        ("test_parse_csv_refuses_binary_too", test_parse_csv_refuses_binary_too),
        ("test_parse_txt_refuses_binary_by_content",
         test_parse_txt_refuses_binary_by_content),
        ("test_parse_txt_still_reads_chinese_gbk",
         test_parse_txt_still_reads_chinese_gbk),
        ("test_parse_txt_plain_text_unaffected",
         test_parse_txt_plain_text_unaffected),
        # PDF 页面操作族 + 图片族
        ("test_images_to_pdf_builds_one_page_per_image",
         test_images_to_pdf_builds_one_page_per_image),
        ("test_images_to_pdf_rejects_empty_and_missing",
         test_images_to_pdf_rejects_empty_and_missing),
        ("test_pdf_to_images_renders_requested_pages",
         test_pdf_to_images_renders_requested_pages),
        ("test_rotate_pdf_pages_only_touches_listed_pages",
         test_rotate_pdf_pages_only_touches_listed_pages),
        ("test_reorder_pdf_pages_can_drop_and_duplicate",
         test_reorder_pdf_pages_can_drop_and_duplicate),
        ("test_pdf_watermark_handles_chinese_and_custom_angle",
         test_pdf_watermark_handles_chinese_and_custom_angle),
        ("test_compress_pdf_reports_real_sizes",
         test_compress_pdf_reports_real_sizes),
        ("test_encrypt_pdf_really_requires_a_password",
         test_encrypt_pdf_really_requires_a_password),
        ("test_compress_image_and_resize_report_output_size",
         test_compress_image_and_resize_report_output_size),
        ("test_convert_image_format_fills_transparency_with_white",
         test_convert_image_format_fills_transparency_with_white),
        ("test_rotate_image_expands_canvas", test_rotate_image_expands_canvas),
        ("test_new_tools_strip_path_traversal_from_output_name",
         test_new_tools_strip_path_traversal_from_output_name),
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
