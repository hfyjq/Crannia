"""
File parser skill handlers: parse_pdf, parse_txt.

Hard constraints enforced here:
  1. Streaming: pdfplumber page iterator (PDF) / line iterator (TXT) — no read().
  2. Security: validate_path() BEFORE any file is opened.
  3. Chunking: all text flows through core.chunker.

Note: search_chunks and summarize_document are handled by agent_loop.py
directly (they operate on session_chunks, not on files), but their tool
definitions live in manifest.yaml so the LLM sees them.
"""

import io
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions as _Perm

from core.chunker import chunk_stream
# 模块级导入，不是函数里延迟导入：
#   延迟导入若解析到另一份旧副本并抛 `ImportError`，而调用处又包着
#   `except Exception`，就会静默变成"跳过探测"、一个字不修。
#   所以放到模块级：解析不了就在装载时炸，不会伪装成"这份文件没问题"。
from core.pdf_text_repair import diagnose as _pdf_diagnose
from core.pdf_text_repair import raw_char_text as _raw_char_text
from core.pdf_text_repair import short_font_name as _short_font_name
from core.security import SecurityGuard
# `modified` 的口径只有一处定义 —— 本地时间 `YYYY-MM-DD HH:MM`。
# legacy 扫描与索引两条路都从 `core.workspace` 取，避免同字段不同量纲。
from core.workspace import (
    find_in_roots, local_time_str, lookup_roots, primary_output_dir,
)

# Injected by skill_loader at startup
_security: SecurityGuard | None = None


def init(security: SecurityGuard) -> None:
    """Called by skill_loader after module import. Sets the shared security guard."""
    global _security
    _security = security


# ------------------------------------------------------------------
# 文本解析器的二进制护栏
# ------------------------------------------------------------------
#
# 为什么要有这一段：`parse_txt` 的编码阶梯是
# `["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]` —— 而 `latin-1` 永不失败
# （每个字节都能映射成一个字符），喂任何二进制文件都必然"成功"、
# 异常阶梯永远走不到底。`parse_csv` 逐字同一个阶梯，同一个洞。
#
# 代价的量级：把一份 `.pdf` 交给 `parse_txt` 会先拿到乱码，
# 模型往往要多花约 5 轮 / 6 秒 / 上下文多涨约 65k tokens 才自己发现
# 该改 `parse_pdf` 重读。
#
# 判据方向（把误差掰到便宜的那边）：
#   误报的代价 = 一次立刻可见的报错 + 一条路线；
#   漏报的代价 = 5 轮 + 65k tokens（而且模型"成功"了，没有任何信号说它错了）。
#   所以宁可拒。

# 这里没有扩展名表 —— 那张表在 `core/workspace.py`。
# handler.py 里不许出现以 `.docx`/`.pdf` 为键的字典字面量
# （类型映射若有多份会漂移）。扩展名分类本来就住在 `core/workspace.py`。
# 护栏是对的，位置必须在那一处。

#: 采样字节数。够长到能撞上任何文件头里的 NUL，又不至于把大文件读进来。
_BINARY_SNIFF_BYTES = 8192


def _binary_refusal(path: Path) -> str | None:
    """这个文件不该按文本读吗？该的话返回带路线的拒绝理由，否则 `None`。

    两道，顺序不可换：
      ① 扩展名（确定性）—— 它抓的是"我们知道这是什么"，
         所以能给出精确的替代工具（`.pdf` → `parse_pdf`）。
         这一步承重：PDF 的头部是 `%PDF-1.7` 这种 ASCII，光靠内容嗅探抓不住。
      ② 内容嗅探（概率性）—— 抓"扩展名没说是二进制、但内容看着是"的。
         判据只看 ASCII 字节里控制字符的占比：CJK 的 GBK/UTF-8 多字节
         全在 ≥0x80，不参与统计，中文文本不会误报（这一点必须验，
         否则这个护栏会把最常用的那类文件拒掉）。
    """
    from core.workspace import parser_hint_for
    is_bin, route = parser_hint_for(path.suffix)
    if is_bin:
        if route:
            return (f"{path.name} 是二进制文件（{path.suffix}），按文本读只会得到乱码。"
                    f"改用 `{route}`。")
        return (f"{path.name} 是二进制文件（{path.suffix}），按文本读只会得到乱码，"
                f"而目前没有解析这种格式的工具。别用 parse_txt / parse_csv 重试。")

    try:
        with open(str(path), "rb") as fh:
            head = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return None            # 读不到就交给下面正常那条路去报错
    if not head:
        return None
    ascii_bytes = [b for b in head if b < 0x80]
    if not ascii_bytes:
        return None            # 全非 ASCII（例如 UTF-16 中文）—— 不在这里判
    # \t \n \x0b \x0c \r \x08 是文本里合法的控制字符
    _OK_CTRL = (8, 9, 10, 11, 12, 13)
    bad = sum(1 for b in ascii_bytes if b < 0x20 and b not in _OK_CTRL)
    if bad / len(ascii_bytes) > 0.05:
        return (f"{path.name} 的内容看着是二进制（前 {len(head)} 字节里 "
                f"{bad} 个控制字符），按文本读只会得到乱码。"
                f"确认这是文本文件的话，改个 .txt 扩展名再来。")
    return None


# ------------------------------------------------------------------
# Tool handlers
# ------------------------------------------------------------------

def parse_pdf(file_path: str) -> List[Dict]:
    """
    Stream-parse a PDF file.

    - 用 pdfplumber（MIT）逐页取文本。
    - Iterates pages one at a time; each page's text is fed into chunk_stream()
      and then released.
    - The full document text is NEVER accumulated in memory.

    先诊断"文本层坏没坏"，再决定怎么读。
    可疑字体（Type0 且无 `/ToUnicode`）的 PDF，直接读会照常返回一串错字
    （数字与英文还是对的），那串东西会原样进 chunk → 摘要 → 上下文，
    全程零报警。三种结局见 `_pdf_read_plan()`。

    Args:
        file_path: Path to the PDF file. Tries as-is, then under workspace/.

    Returns:
        List of chunk dicts: {"page", "chunk_index", "text"}.
    """
    _require_security()

    resolved_path = _require_file(file_path)

    # Security: validate BEFORE touching the file
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]

    plan = _pdf_read_plan(resolved)

    def page_iterator() -> Iterator[Tuple[int, str]]:
        import pdfplumber
        with pdfplumber.open(str(resolved)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                yield (page_num, _page_text(page, plan))

    return chunk_stream(page_iterator())


def _page_text(page, plan) -> str:
    """一页的正文。`plan is None`，原路（`extract_text()`，一个字节不改）。

    要修的时候走 `extract_text_lines()` —— 它逐行给出 `chars`，
    每个 char 自带 `fontname`；只对可疑字体的字符动手
    （同一页上 `DIN`/`Georgia` 那些数字是对的，平移会把它们毁掉）。

    取数层是 **pdfplumber 的 `extract_text_lines().chars`**
      （每个 char 自带 `fontname`）；修复算法只对可疑字体动手。
    """
    if plan is None:
        return page.extract_text() or ""
    try:
        lines = page.extract_text_lines()
    except Exception:                                  # noqa: BLE001 - 老版本没有这 API
        lines = []
    if not lines:
        return page.extract_text() or ""
    out: List[str] = []
    for line in lines:
        parts: List[str] = []
        seen: set = set()          # 见下：同一字形会以完全相同的位置重复出现
        for ch in line.get("chars") or []:
            # 没有 ToUnicode 的 Type0 页上，pdfplumber 的
            #   `chars` 会把同一个字形在同一位置列 ~10 次（假粗体/多层绘制），
            #   而 `extract_text()` 会去重。这里自己按 (x0, top, text) 去重，
            #   否则修出来的正文是「小小小小小小…」。
            key = (round(float(ch.get("x0", 0)), 1),
                   round(float(ch.get("top", 0)), 1),
                   str(ch.get("text") or ""))
            if key in seen:
                continue
            seen.add(key)
            t = _raw_char_text(ch.get("text") or "")
            if _short_font_name(ch.get("fontname", "")) in plan.fonts:
                t = plan.decode(t)
            parts.append(t)
        out.append("".join(parts))
    return "\n".join(out)


def _pdf_read_plan(path):
    """读之前先问一句：这份 PDF 的文本层坏没坏？ 返回修复计划或 `None`。

    三种结局：
      · 没有可疑字体 / 判不出来，`None`，照原样读
      · 判出来是坏的、而且修得了，返回计划 + 控制台出声
      · 判出来是坏的、但修不了，`raise` —— 理由见下

    第三种为什么是 `raise` 而不是返回一句提示：`parse_pdf` 是分块工具
    （返回 `List[Dict]`），而 `agent_loop` 里 `_CHUNK_TOOLS` 那条分支会把返回值
    直接 `session_chunks.extend(...)`、再把工具结果换成一句固定英文桩
    所以 chunks 永远不进 messages，塞在里面的提示模型根本看不到。
    抛出去由循环统一变成 `Tool execution error: ...` 回填给模型 ——
    这是本仓库给分块工具定的既定形状（同 `_binary_refusal` 那条注释）。
    判定本身失败（打不开 / 探测抛异常）不许把解析拦下来 —— 那是两件事：
    "确认它坏了"才拒，"没判出来"照读。
    """
    try:
        diag = _pdf_diagnose(str(path))
    except Exception as exc:                       # noqa: BLE001
        print(f"[file_parser] PDF 文本层探测跳过（{type(exc).__name__}: {exc}）")
        return None
    if diag is None:
        return None
    if diag.repair is None:
        raise ValueError(
            "这份 PDF 的**文本层**坏了（字体没有 /ToUnicode），直接读只会得到乱码，"
            "而乱码会被当成正文用。诊断：%s。\n"
            "可以试的：① `pdf_to_images` 渲出几页 PNG 再看（看得见，但只够抽样，"
            "别拿它当全文）；② 换一份有文本层的 PDF；"
            "③ 若确认是「整字体平移」那类，本工具本该自动修 —— 这次没解出偏移，"
            "**别硬猜**，如实告诉用户这份文件读不出来。" % diag.verdict.reason
        )
    print("[file_parser] PDF 文本层已自动修复：%s" % diag.repair.summary())
    return diag.repair


def parse_txt(file_path: str, encoding: str = "utf-8") -> List[Dict]:
    """
    Stream-parse a TXT file.

    - Opens with built-in open() using a context manager.
    - Reads ONE LINE AT A TIME (never .read() or .readlines()).
    - Accumulates lines into pseudo-pages (every 100 lines = 1 "page").
    - Each pseudo-page is fed into chunk_stream() and released.

    Args:
        file_path: Path to the TXT file. Tries as-is, then under workspace/.
        encoding: File encoding (default "utf-8").

    Returns:
        List of chunk dicts: {"page", "chunk_index", "text"}.
    """
    _require_security()

    resolved_path = _require_file(file_path)

    # Security: validate BEFORE touching the file
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]

    # 先问"这到底是不是文本"，再走编码阶梯。
    # 位置在这里是刻意的：`validate_path` 之后（先过围栏）、
    # 编码阶梯之前（否则 `latin-1` 会让它永远"成功"）。
    # 是 `raise` 不是 `return {"success": False}` —— 本函数是分块工具
    # （返回 `List[Dict]`），返回 dict 会打乱 `agent_loop` 里 `_CHUNK_TOOLS`
    # 那条分支的契约。抛出去由循环统一变成 `Tool execution error: ...` 回填给模型。
    _refusal = _binary_refusal(Path(str(resolved)))
    if _refusal:
        raise ValueError(_refusal)

    resolution_order = [encoding]
    if encoding == "utf-8":
        # Auto-detect common Chinese encodings when UTF-8 fails.
        # Many Chinese CSV/TXT files use GBK/GB2312 without a BOM.
        resolution_order = ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]

    last_error = None
    for enc in resolution_order:
        try:
            def line_iterator() -> Iterator[Tuple[int, str]]:
                with open(str(resolved), "r", encoding=enc) as f:
                    page_num = 1
                    buffer: List[str] = []
                    line_count = 0
                    for line in f:
                        buffer.append(line)
                        line_count += 1
                        if line_count >= 100:
                            yield (page_num, "".join(buffer))
                            page_num += 1
                            buffer = []
                            line_count = 0
                    if buffer:  # flush remainder
                        yield (page_num, "".join(buffer))

            result = chunk_stream(line_iterator())
            if enc != encoding:
                print(f"[file_parser] parse_txt: fell back to {enc} "
                      f"encoding for {file_path}")
            return result
        except (UnicodeDecodeError, UnicodeError) as e:
            last_error = e
            continue

    raise ValueError(
        f"Failed to decode {file_path} with encodings "
        f"{resolution_order}: {last_error}"
    )


def parse_csv(file_path: str, encoding: str = "utf-8") -> List[Dict]:
    """
    Parse a CSV file with encoding auto-detection and header preservation.

    Uses Python's csv module for proper tabular handling.  The first row
    is treated as a header and repeated in every chunk so BM25 keyword
    search can match column names against query terms.

    Args:
        file_path: Path to the CSV file.
        encoding: Preferred encoding (default "utf-8").  Falls back to
                  GBK/GB2312/GB18030 on decode error.

    Returns:
        List of chunk dicts: {"page", "chunk_index", "text"}.
        Each chunk's text prefixes the header row so column context is
        always available to the retriever and LLM.
    """
    import csv
    import io

    _require_security()

    resolved_path = _require_file(file_path)
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]

    # 同一个洞的第二处 —— `parse_csv` 的编码阶梯与
    # `parse_txt` 逐字相同（包括那个永不失败的 `latin-1`），所以喂二进制
    # 一样会"成功"返回乱码。两处是一个根因，只堵一处等于留了一扇同样的门。
    _refusal = _binary_refusal(Path(str(resolved)))
    if _refusal:
        raise ValueError(_refusal)

    # Encoding resolution (same fallback chain as parse_txt)
    resolution_order = [encoding]
    if encoding == "utf-8":
        resolution_order = ["utf-8", "gbk", "gb2312", "gb18030", "latin-1"]

    last_error = None
    for enc in resolution_order:
        try:
            raw = Path(str(resolved)).read_bytes()
            text = raw.decode(enc)
            if enc != encoding:
                print(f"[file_parser] parse_csv: fell back to {enc} "
                      f"encoding for {file_path}")

            reader = csv.reader(io.StringIO(text))
            rows = list(reader)
            if not rows:
                return []

            header = rows[0]
            header_line = ",".join(header)

            # Build pseudo-pages: every 50 data rows = 1 "page",
            # with the header repeated so column names are always
            # in context for BM25 and the LLM.
            def csv_line_iterator() -> Iterator[Tuple[int, str]]:
                page_num = 1
                buffer: List[str] = []
                for i, row in enumerate(rows[1:], start=1):
                    buffer.append(",".join(row))
                    if len(buffer) >= 50:
                        yield (
                            page_num,
                            f"[CSV header: {header_line}]\n"
                            + "\n".join(buffer),
                        )
                        page_num += 1
                        buffer = []
                if buffer:
                    yield (
                        page_num,
                        f"[CSV header: {header_line}]\n"
                        + "\n".join(buffer),
                    )

            return chunk_stream(csv_line_iterator())

        except (UnicodeDecodeError, UnicodeError) as e:
            last_error = e
            continue

    raise ValueError(
        f"Failed to decode CSV {file_path} with encodings "
        f"{resolution_order}: {last_error}"
    )


#: 扩展名 → 中文类型名。模块级常量：legacy 回退分支（except 之后）也要用它 ——
#: 若定义在 try 块里，新路径一抛异常，回退分支就会 UnboundLocalError，
#: 把一个可恢复的降级变成崩溃。
_SIZE_UNITS = (("B", 1), ("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3))


def _human_size(n: int) -> str:
    """字节数 -> 人读的大小。

    这个量只能有一处格式：多处各写一份会让 `f"{n}B"`（无空格）
    与 `f"{n} B"` 并存 —— 同一个量两个格式，而各算各的不会报错。
    """
    for unit, scale in reversed(_SIZE_UNITS):
        if n >= scale:
            return f"{n} B" if unit == "B" else f"{n / scale:.1f} {unit}"
    return f"{n} B"


def _file_entry(path: str, ext: str = "", size: int | None = None,
                modified: str = "") -> dict:
    """文件条目的唯一构造点。

    四个调用点（工作区索引 / legacy 扫描 / 跨区缓存 / 跨区搜索）必须共用
    这一份。各写一份时类型映射甚至出现过三份（模块级 13 条 + 两处内联 7 条）
    —— 那就是分块工具名单漂移的同一个形状。现在一处定义、四处引用。

    只留四个字段，其余都推得出来。依据是量级：每条约 216 字符，而工具结果
    预算是 6,000 字符，一条 32 条的索引一页装不下，而模型不翻页
    （`hint` 里明写了 `offset=20`，它拿同样的参数又调了一次 —— 白烧一轮）。

      - `path`       —— 唯一的解析句柄。原样保留：相对化要先解决工作组根的
                        解析（`_resolve_path` 只对默认工作区回落到 `workspace/`）
      - `ext`        —— 决定下游用哪个 `parse_*`，机器精确且总有值
      - `size_human` —— 给人看的大小
      - `modified`   —— 本地时间，归一到 `YYYY-MM-DD HH:MM`；
                        口径只有一处定义 —— `core.workspace::local_time_str`。
                        "形状读起来像本地、值却是 UTC"是必须避免的坏形状

    砍掉的三个都是可推出来的：
      - `name` = `path` 的最后一段
      - `size` = `size_human` 的机器读法（模型不排序）
      - `type` = `ext` 的中文标签，而且有损：`Word文档` 分不出 `.doc` / `.docx`
        （下游是两个不同的工具）；`.jpg` / `.png` 根本没有条目，恒为空串

    跨区缓存那条路不扫盘（只有路径），所以 `size` / `modified` 缺省时不进字典。
    """
    entry: dict = {"path": path, "ext": ext}
    if size is not None:
        entry["size_human"] = _human_size(size)
    if modified:
        # 口径已统一 —— 两条路都从 `core.workspace::local_time_str`
        # 取本地时间 `YYYY-MM-DD HH:MM`（若索引给 UTC ISO、legacy 给本地，
        # 模型报出的「15:59」和用户看到的「00:15」会差 8 小时）。
        # 这行归一化保留：它现在对两条路都是恒等的，是防第三种形状的后手。
        entry["modified"] = modified[:16].replace("T", " ")
    return entry


def list_workspace_files(
    pattern: str = "*",
    sort_by: str = "time",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """
    List files across all registered workspaces with pagination.

    Returns a stats summary and a page of results.  When the
    total count exceeds *limit*, use *offset* to fetch the next page.
    The stats (total count, size, file types) are always computed over
    the full result set — you do NOT need to paginate to get the full
    picture; use stats to decide whether to narrow the pattern or
    paginate further.

    When cross-zone search is enabled and no results are found in the
    workspaces, call `request_cross_zone_search` to expand the scope.

    Args:
        pattern: 匹配文件名或它在工作区下的相对路径（`/` 分隔），
            如 `"*.docx"` · "实训*" · "表格1/*"。不含通配符时按子串处理。
            默认 `"*"`。匹配面是文件名 ∪ 相对路径；只比文件名的话，
            像 "表格1/*" 这样的目录筛选会恒为 0。
        sort_by: `"time"` (most recent first), `"name"`, or `"size"`.
        limit:  Max files per page (1–50, default 20).
        offset: 0-based page offset.
    """
    import fnmatch
    # `modified` 的格式化统一走 `local_time_str`（口径只有一处定义），
    # 所以这里不需要 `datetime` 导入。

    _require_security()
    limit = max(1, min(limit, 50))

    # v2: try WorkspaceManager first
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            # 索引过期就重建，不是"只在空的时候建"。
            # 这里的落点要单独看 —— `list_workspace_files` 绕过
            #    `WorkspaceManager.search_files`，直接调 `wm._path_index.search`，
            #    所以只修 `workspace.py` 那两处盖不住它。
            wm._maybe_refresh_index()
            # Get ALL matching files from index (fast, in-memory)
            all_results = wm._path_index.search(pattern, max_results=10000)
            total = len(all_results)
            #: 读数必须带范围。索引扫的是「默认工作区 + 所有已注册工作组」，
            #:   而核对的人（模型或用户）往往只数默认工作区，于是把范围差
            #:   读成「索引里留着盘上没有的文件」（真机上 618 对 494，差额
            #:   129 全在工作组里）。同一条判据：一个数没有来源，就会被当成
            #:   另一个量的读数。
            roots = [str(p) for p in (getattr(wm._path_index, "_roots", None) or [])]
            notice: str | None = None

            # If empty, try fuzzy matching before suggesting cross-zone.
            # This prevents the model from guessing with a wrong file name
            # and gives it meaningful alternatives to offer the user.
            if total == 0:
                # Try fuzzy substring match as a fallback
                fuzzy = wm._path_index.search_by_name(
                    pattern.strip("*") if pattern != "*" else "",
                    max_results=10,
                )
                fuzzy_files = []
                if fuzzy:
                    for fe in fuzzy:
                        fuzzy_files.append(_file_entry(fe.path, fe.ext))
                if wm.cross_zone_enabled:
                    hint = (
                        "No exact match in workspaces."
                        + (f" Similar files: {[f['name'] for f in fuzzy_files[:5]]}."
                           if fuzzy_files else "")
                        + " Call request_cross_zone_search to search globally."
                    )
                    return {
                        "stats": {"total": 0, "total_size": "0 B", "by_type": {},
                                  "roots": roots},
                        "page": {"files": [], "offset": 0, "limit": limit, "has_more": False},
                        "hint": hint,
                        **({"similar": fuzzy_files} if fuzzy_files else {}),
                    }
                else:
                    hint = (
                        "No files."
                        + (f" Similar files in workspace: {[f['name'] for f in fuzzy_files[:5]]}."
                           if fuzzy_files else "")
                        + " Cross-zone search is DISABLED. Tell user to enable it in 安全与设置."
                    )
                    return {
                        "stats": {"total": 0, "total_size": "0 B", "by_type": {},
                                  "roots": roots},
                        "page": {"files": [], "offset": 0, "limit": limit, "has_more": False},
                        "hint": hint,
                        **({"similar": fuzzy_files} if fuzzy_files else {}),
                    }

            # Compute stats over ALL results
            total_bytes = sum(fe.size for fe in all_results)
            by_type: dict[str, int] = {}
            for fe in all_results:
                ext = fe.ext if fe.ext else "(none)"
                by_type[ext] = by_type.get(ext, 0) + 1

            total_size_str = _human_size(total_bytes)

            # Sort
            if sort_by == "name":
                all_results.sort(key=lambda fe: fe.name.lower())
            elif sort_by == "size":
                all_results.sort(key=lambda fe: fe.size, reverse=True)
            else:
                # `time` 必须真的按时间排：索引序是 walk 的插入序
                #    （≈名字序），不能当作时间序 —— 否则 `sort_by="time"` 与
                #    `sort_by="name"` 会逐字节同序（`size` 那条不受影响）。
                #    `modified` 是定宽零填充的 "YYYY-MM-DD HH:MM"
                #    所以字符串序就是时间序（不用解析成 datetime）。
                all_results.sort(key=lambda fe: fe.modified, reverse=True)

            # Slice page
            page_results = all_results[offset:offset + limit]
            files = [
                _file_entry(fe.path, fe.ext, fe.size, fe.modified)
                for fe in page_results
            ]

            has_more = offset + limit < total
            return {
                "stats": {
                    "total": total,
                    "total_size": total_size_str,
                    "by_type": by_type,
                    "roots": roots,
                },
                "page": {
                    "files": files,
                    "offset": offset,
                    "limit": limit,
                    "has_more": has_more,
                },
                "hint": (
                    f"Found {total} file(s), {total_size_str}. "
                    + (f"Showing {offset+1}-{min(offset+limit, total)}. "
                       f"Use offset={offset+limit} for next page, "
                       f"or narrow pattern to filter."
                       if has_more else "All files shown.")
                ),
                **({"notice": notice} if notice else {}),
            }

    except Exception as _e:
        # 这里必须出声，不能 `except Exception: pass`：新路径的真实失败原因
        # 被吞掉后，若 legacy 分支也出问题，呈现给模型的就是一个毫无线索的错误。
        # 静默降级不是容错，是把诊断信息丢掉。
        print(f"[list_workspace_files] WorkspaceManager path failed "
              f"({type(_e).__name__}: {_e}) — falling back to legacy scan")

    # Legacy fallback
    # 落点口径只有一处（`primary_output_dir()` 自己带三层回退，
    # 拿不到管理器时它就是 `./workspace`）。
    ws = primary_output_dir()
    #: 降级这条路只扫一个根 —— 照样说清楚是哪个（同上面那条判据：
    #:   读数必须带范围，否则"少扫了一个目录"会被读成"盘上就这些"）。
    legacy_roots = [str(ws)]
    if not ws.exists():
        return {
            "stats": {"total": 0, "total_size": "0 B", "by_type": {},
                      "roots": legacy_roots},
            "page": {"files": [], "offset": 0, "limit": limit, "has_more": False},
            "hint": "Workspace directory does not exist yet.",
        }

    resolved = _security.validate_path(str(ws.resolve()))  # type: ignore[union-attr]
    # 先收元数据、后建条目：条目本身不带 `size` / `name`，
    # 而排序与求和要用它们，让它们留在中间结构里，别为了瘦身把排序改坏。
    _meta: list[tuple[str, int, dict]] = []
    for entry in sorted(Path(str(resolved)).iterdir()):
        if not entry.is_file():
            continue
        if not fnmatch.fnmatch(entry.name, pattern):
            continue
        stat = entry.stat()
        _meta.append((
            entry.name.lower(),
            stat.st_size,
            _file_entry(
                str(entry.resolve()), entry.suffix.lower(), stat.st_size,
                local_time_str(stat.st_mtime),
            ),
        ))

    total = len(_meta)
    if sort_by == "name":
        _meta.sort(key=lambda t: t[0])
    elif sort_by == "size":
        _meta.sort(key=lambda t: t[1], reverse=True)
    else:
        # 与上面那条同一条判据 —— 若不显式按 `modified` 排序，
        #   `time` 就会退化成 `iterdir()` 的顺序（≈名字序）。
        _meta.sort(key=lambda t: t[2]["modified"], reverse=True)

    by_type: dict[str, int] = {}
    for _, _, _e in _meta:
        by_type[_e["ext"]] = by_type.get(_e["ext"], 0) + 1
    total_bytes = sum(s for _, s, _ in _meta)
    total_size_str = _human_size(total_bytes)
    page = [e for _, _, e in _meta[offset:offset + limit]]
    has_more = offset + limit < total
    return {
        "stats": {"total": total, "total_size": total_size_str, "by_type": by_type,
                  "roots": legacy_roots},
        "page": {"files": page, "offset": offset, "limit": limit, "has_more": has_more},
        "hint": (
            f"Found {total} file(s), {total_size_str}. "
            + (f"Use offset={offset+limit} for next page." if has_more else "All files shown.")
        ),
    }


def _cross_zone_allowed(wm) -> bool:
    """这次跨区搜索允不允许跑 —— 顺序即策略。

    两层是两种不同的东西，别合并：

    ==========================  ==========================  ==========
    来源                         含义                        取用后
    ==========================  ==========================  ==========
    `cross_zone_session_auto` 本会话「不再询问」（投影）      不消耗
    `take_cross_zone_grant()` 一次性授权（带属主，投影）      取即减
    ==========================  ==========================  ==========

    顺序即策略：消费型的那个必须放最后，否则前面那个"以后都不用问"
    会被按次记账记掉 —— 那正是"一次性授权"这个名字上的自相矛盾。

    `auto_approve`（设置页「自动放行跨区搜索」）不在这里判定 ——
    那个开关不等于放行，它表示「交给分类器判」：
    判安全时，`agent_loop._pre_grant_for_auto` 会发一条一次性授权，
    由下面 `take_cross_zone_grant()` 兑现；判拦，那道闸直接拒
    （走不到这里）；没答上来（手动档），没有授权，弹审批窗
    （这正是"退回问人"落在实处）。
    保留 `cross_zone_session_auto` —— 那是用户在本会话里明确说过
    「本次对话内不再询问」，与设置页那个常开的开关不是一件事。
    """
    if wm.cross_zone_session_auto:
        return True
    return wm.take_cross_zone_grant()


def _delete_prompt(file_list: str) -> str:
    """删除审批的提示语 —— 由 `core/approval.py` 那唯一一份选项表渲染。

    不要在别处再拼一份：选项表一旦有多份副本，跨区与删除两条通道
    会长出不一致的按钮与措辞。
    """
    from core.approval import render_delete_body, render_prompt
    return render_prompt("delete", 1,
                         body=render_delete_body(file_list),
                         footer=False)


def request_cross_zone_search(
    file_hint: str = "",
    scope: str = "global",
) -> dict:
    """
    Request permission to search outside the workspace for a file.

    Call this tool when `list_workspace_files` returns no results
    and you believe the file may be elsewhere on the user's computer.

    The framework checks the gates in this order —
    only the last one can hand you files:

    1. 跨区搜索关着，status: "error"，到此为止（缓存也不端）。
    2. 未授权，status: "pending_approval"，框架接管、直接把
       格式化提示语发给用户。你的回复会被提示语取代。等用户回答，
       PreProcessStage 会注入一条系统指令告诉你下一步。
       这一步在缓存之前 —— 缓存不替代授权。
    3. 已授权，先查缓存（from_cache: True 时答案来自上次同关键词的
       搜索），没命中才真扫盘。

    auto_approve（设置页「自动放行跨区搜索」开着） —— 它不等于
    "第 2 步直接放行"：那个开关表示「交给分类器判」
    （判安全，框架发一条一次性授权，第 3 步放行；判拦，直接拒；
    手动档下没答上来，退回问你）。见 `_cross_zone_allowed` 的注释。

    Args:
        file_hint: 要找的文件名或一串关键词。空格分开会被当成多个关键词，
                   任一个出现在文件名里就算候选，命中词多的排前面
                   （空格分开的中英混合关键词是最自然的用法；若当成整串子串，
                   会一个都匹配不到）。
                   关键词只匹配文件名，不搜文件内容。
        scope: 搜索位置。"global" 只搜用户家目录
               （等于 `Path.home()`，不是整个磁盘）；
               要搜别处就传具体路径，如 "D:/"。
               传了一个解析不出或不存在的路径时，
               实际搜的是家目录，而回执里会写明这一点
               —— 回执不会比搜索本身更自信。
    """
    import fnmatch
    # `modified` 的格式化统一走 `local_time_str`，这里不需要 `datetime` 导入。

    _require_security()

    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            # 1. 跨区搜索整个被关掉，到此为止
            # 这条必须在缓存之前。若缓存在最前面，"关闭跨区搜索"
            # 就关不干净：以前搜到过的工作区外路径照样被端出来。设置页自己的
            # 描述是「允许 Agent 在工作区外搜索、读取和复制文件」，
            # 关着的时候它不该还能拿到工作区外的路径。
            if not wm.cross_zone_enabled:
                return {
                    "status": "error",
                    "hint": (
                        "跨区搜索功能已在安全设置中关闭。"
                        "请告知用户：工作区中没有找到该文件，且跨区搜索已被禁用。"
                        "如需使用跨区搜索，请前往安全与设置页面开启「是否启用跨区搜索（只读）」。"
                    ),
                }

            # 2. 授权判定（收成一个函数，顺序即策略）
            # 先判定、后查缓存。若缓存在这里之前且直接 return，
            # 等于给"要不要问用户"开了第三条路：批过一次之后，
            # 同一个文件的查询会没审批就过（而缓存那句提示语可能还写着
            # "跨区搜索当前已关闭"）。缓存该做的事是"授权之后不重复扫盘"，
            # 不是"替代授权"。
            #
            # 代价：缓存命中也会消耗一次性授权（判定在缓存之前）。
            # 这是安全方向：用户授权的是"这一次搜索"，答案从哪来是优化细节，
            # 下次仍然重新问。反过来（缓存命中不消耗）会让授权被无限续期。
            approved = _cross_zone_allowed(wm)
            if approved:
                # 3. 已授权，缓存是加速，不是绕道
                cached = wm._cache_lookup(file_hint)
                if cached:
                    # 这条路不扫盘（缓存的意义就是省掉那棵树），所以
                    # `size` / `modified` 拿不到就不给 —— `_file_entry` 允许缺省。
                    # 别为了"形状统一"去 `stat()` 一遍：缓存记录可能已经过期，
                    # 挡它的是 `_validate_cache_entry` 的存在性过滤，
                    # 这里只做兜底。
                    files = [_file_entry(p, Path(p).suffix.lower()) for p in cached]
                    return {"status":"completed","files":files,"count":len(files),
                            "from_cache":True,
                            "hint":f"从缓存中找到 {len(files)} 个匹配文件"
                                   f"（此前已授权搜索过同样的关键词）。"}

                # 4. 真搜索
                results, notice, action = wm.search_files(
                    file_hint, scope="cross_zone", max_results=200,
                    search_root=scope if scope != "global" else "",
                    granted=True)
                # 第三返回值绝不能再丢。它是内层"我认为
                # 你没被授予能力"的唯一信号；丢了就会返回一个
                # `completed` + 0 个文件的答复，而那句话的意思是
                # "搜过了、没有" —— 实际上一次搜索都没跑。
                # （真 manager 在 `granted=True` 下不会返回它 —— 这个分支守的是
                #  `search_files` 写在契约里的那个返回值，不是死代码。）
                if action is not None:
                    return {
                        "status": "pending_approval",
                        "file_hint": file_hint,
                        "scope": scope,
                        "prompt": action.get("prompt", ""),
                    }
                files = [
                    _file_entry(fe.path, fe.ext, fe.size, fe.modified)
                    for fe in results
                ]
                result = {
                    "status": "completed",
                    "files": files,
                    "count": len(files),
                    "hint": (
                        f"Cross-zone search found {len(files)} file(s) matching "
                        f"'{file_hint}'. Use the exact 'path' to call parse tools."
                    ),
                }
                if notice:
                    result["notice"] = notice
                return result

            # 5. 走到这里，没被授权，报待审批
            # 这里不该再查一次缓存：`approved` 为假意味着中间什么都没跑、
            # 缓存不可能被写。一段永远不执行的"兜底"比没有更糟 ——
            # 读的人会以为它在兜什么。
            #
            # 提示语不就地拼 —— 选项表在 `core/approval.py`。
            # 多份副本会长出不一致的按钮与措辞。
            from core.approval import pending_payload
            # `ask_index` 从投影上取（+1）—— 真值在调度器上，
            # 由 `agent_loop` 在返回 pending_approval 时推进。
            # `wm` 可能是替身（测试里就是），没有这个属性就当第 1 次。
            ask_index = int((getattr(wm, "approval_ask_counts", {}) or {}).get("cross_zone", 0)) + 1
            return pending_payload(
                "cross_zone", footer=False, ask_index=ask_index,
                file_hint=file_hint, scope=scope,
            )

    except Exception:
        pass

    return {
        "status": "error",
        "hint": "Workspace manager not available. Please upload files directly.",
    }


def copy_to_workspace(file_path: str) -> dict:
    """
    Copy a file from any accessible location (including cross-zone
    directories like Desktop, Downloads, D: drive, etc.) into the
    workspace.

    Use this when you found a file via cross-zone search and the user
    wants to keep it in the workspace for later use.

    Args:
        file_path: Absolute path to the source file.
    """
    import shutil
    _require_security()

    src = Path(file_path)
    if not src.exists():
        try:
            src = Path(file_path).resolve()
        except Exception:
            pass
        if not src.exists():
            return {"success": False, "error": f"源文件不存在: {file_path}"}

    try:
        resolved = _security.validate_path(str(src), "copy")
    except Exception as e:
        return {"success": False, "error": f"安全限制: {e}"}

    # 落点口径只有一处（`primary_output_dir()` 里面就是 `wm.primary_path`
    # + 同样的回退），不要在这里再拼一层 try/except + 三层回退。
    target_dir = primary_output_dir()

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / src.name

    try:
        shutil.copy2(str(resolved), str(dest))
        return {
            "success": True,
            "source": str(resolved),
            "destination": str(dest),
            "size_kb": round(dest.stat().st_size / 1024, 1),
            "hint": f"已复制 {src.name} 到工作区。后续可直接用文件名操作。",
        }
    except OSError as e:
        return {"success": False, "error": f"复制失败: {e}"}


#: 批量删除的出厂阈值。`0` = 关闭这个闸
#: （回到"全交给分类器 / 会话授权"）。
#:
#: 兜底方向是保守：读不到 / 键没有 / 坏 JSON / 坏值，回 35，不是 0
#: （安全轴的兜底必须偏保守）。
#: 阈值是"一次删多少才需要人批"，不是"一次最多能删多少" —— 常见的一次日记批量
#:   在 31 条上下，取 30 会把它挡在闸上。
BATCH_DELETE_LIMIT_DEFAULT = 35

#: 落 `data/settings.json` 的那个键。
BATCH_DELETE_LIMIT_KEY = "delete_batch_limit"


def _batch_delete_limit() -> int:
    """这次要按多严的阈值拦 —— 每次现读。

    读法是读设置项的通用形状（同 `_stream_enabled`）：
    文件不存在 / 读坏了 / 键没有，一律回默认值，绝不因此让一轮对话失败。

    `0` 是合法的（= 用户关掉了这个闸），判据必须是 `> 0`，
    不能拿 `or` 把 0 当假值吃掉（那会让"关不掉"）。
    """
    import json
    from pathlib import Path
    try:
        p = Path("data/settings.json")
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8")).get(
                BATCH_DELETE_LIMIT_KEY, BATCH_DELETE_LIMIT_DEFAULT)
            # 只收真整数：`True`/`"30"`/`30.5` 一律回默认 ——
            #    JSON 里 `yes`/`1` 被读成同一件事是碰巧对，不是对。
            if isinstance(raw, bool) or not isinstance(raw, int):
                return BATCH_DELETE_LIMIT_DEFAULT
            return max(0, raw)
    except Exception:                                  # noqa: BLE001
        pass
    return BATCH_DELETE_LIMIT_DEFAULT


def delete_files(file_paths: list | None = None, pattern: str = "") -> dict:
    """
    Delete files from the workspace.  Two modes:

    1. Explicit list: pass `file_paths` with specific paths.
    2. Pattern bulk: pass `pattern` (e.g. `"*wip*"` or `"*.tmp"`)
       to auto-expand to all matching workspace files.  Much faster for
       bulk cleanup — no need to list files first.

    IMPORTANT: This tool requires user confirmation.  When called,
    the framework will ask the user to approve.  Choose option 2
    ("不再询问") to skip approval for subsequent calls in this session.

    Files go to the system recycle bin, so the user can restore
    them from Explorer. Tell the user that instead of "永久删除" when it
    applies — and read `recycled` / `permanently_deleted` / `hint` in the
    result: when the bin is unavailable or full the delete *is* permanent.

    Args:
        file_paths: Specific file paths (for targeted deletion).
        pattern: Glob pattern for bulk deletion (e.g. `"*wip*"`).
            If provided, file_paths is ignored.
    """
    _require_security()

    # 这一次调用的被审批物
    # 必须在参数被改写之前算 —— 下面 `pattern` 那一支会把 `file_paths`
    #    换成展开后的清单，而重放拿的是原始参数
    #    （`ctx.approval_replay` 里的那一份），在下面算就对不上，症状是
    #    「用户批了、框架重放，却又弹一次」。
    from core.approval import delete_subject
    _subject = delete_subject({"file_paths": file_paths, "pattern": pattern})

    # Pattern mode: auto-expand from workspace index
    if pattern:
        resolved_list: list[str] = []
        try:
            from bus.registry import get_workspace_manager
            wm = get_workspace_manager()
            if wm:
                # 同上：同一条病、同一个修法
                wm._maybe_refresh_index()
                matches = wm._path_index.search(pattern, max_results=10000)
                resolved_list = [fe.path for fe in matches]
        except Exception:
            pass
        if not resolved_list:
            # "没有匹配"有三种成因，必须分开说。
            #
            # 判据：模型拿到一句笼统的「没有匹配的文件」，只会得出"这个目录是空的"
            # ——而真实成因可能是它把 pattern 写成了路径（那条永远匹配不上，
            # 因为这里是按文件名比的，`core/workspace.py::PathIndex.search`）。
            # 典型症状：模型写 `workspace/_测试区C/*`，报"没有匹配"，
            # 它去 `ls` 一看文件明明在，两边都"对"，而它无从纠正。
            if "/" in str(pattern) or "\\" in str(pattern):
                return {"success": False, "error": (
                    f"`pattern` **不能带目录** —— 它按**文件名**匹配，**不认路径**。"
                    f"你写的是 '{pattern}'，里面有目录分隔符 ⇒ **永远匹配不上**"
                    f"（这**不是**「那个目录是空的」）。"
                    f"⇒ 想在某个目录里删，用 `file_paths` 逐个指名；"
                    f"想按名字批量删，pattern 只写名字那一段（如 '*.tmp'），"
                    f"⚠️ 注意它**不限定目录**、是整个工作区。")}
            try:
                from bus.registry import get_workspace_manager
                _wm = get_workspace_manager()
                _n = len(getattr(_wm, "_path_index")._index) if _wm else 0
            except Exception:
                _n = -1
            if _n == 0:
                return {"success": False, "error": (
                    "**工作区索引是空的**（不是「没有匹配」）—— 目录可能还没建，"
                    "或索引还没刷新。先 `list_workspace_files` 看一眼。")}
            return {"success": False, "error": (
                f"工作区里**确实没有**文件名匹配 '{pattern}' 的文件"
                f"（当前索引里有 {_n} 个文件）—— 这是一个结论，不是「没查到」。")}
        file_paths = resolved_list

    if not file_paths:
        return {"success": False, "error": "未指定要删除的文件"}

    # Protected directories — framework data, never deletable
    # v2: data/workdata/ is the framework's internal data directory.
    # It is NOT registered as a workspace and is never user-accessible.
    # We hard-reject any path under it as a defense-in-depth measure.
    _WORKDATA = Path("data/workdata").resolve()
    # 「查」这一类不能只试一个根 —— 文件可能在任何一个工作组里。
    # 只试 `Path("workspace")` 的话，设了 primary 之后
    # "删一个刚生成的文件"会找不到它（而回执说的是"文件不存在"）。
    _WS_ROOTS = lookup_roots()

    # Resolve and validate all paths
    valid: list[Path] = []
    invalid: list[str] = []
    for fp in file_paths:
        p = Path(fp)
        if not p.exists():
            # Try resolving relative to the workspace —— 全部根都试
            found = None
            for _root in _WS_ROOTS:
                _cand = _root / p.name
                if _cand.exists():
                    found = _cand
                    break
            p = found if found is not None else (_WS_ROOTS[0] / p.name)
        if not p.exists():
            invalid.append(str(fp))
            continue
        try:
            p_resolved = p.resolve()
        except Exception:
            invalid.append(f"{fp} (无法解析路径)")
            continue

        # Hard block: never allow deletion under data/workdata/
        try:
            p_resolved.relative_to(_WORKDATA)
            invalid.append(f"{fp} (框架数据目录，禁止删除)")
            continue
        except ValueError:
            pass  # not under workdata — proceed

        # Workspace boundary check
        # Only files under a registered workspace (or the default workspace)
        # are eligible for deletion.  The security guard enforces per-workspace
        # permissions (delete must be in the workspace's perm set).
        try:
            resolved = _security.validate_path(str(p_resolved), "delete")
            valid.append(resolved)
        except Exception as e:
            invalid.append(f"{fp} ({e})")

    if not valid:
        return {"success": False, "error": "没有可删除的有效文件",
                "invalid": invalid}

    # 批量闸 ——「超过阈值，越过下面那两条免问授权」
    #
    # 这一处是唯一的：`_do_delete` 全仓只有下面那一个调用点，
    #    而 `send_to_recycle_bin` 只有 `_do_delete` 一个调用点
    #    所以堵住这一句就堵住了全部删除出口（不是"我挑了几条路堵"，
    #    是只有一条路）。
    #
    # 为什么加在 handler 里而不是分类器里：
    #   · 一处实现同时盖住两条免问路（自动模式的分类器预授权 + 会话级
    #     「本次会话内不再询问」）—— 分类器那边一行都不用改：
    #     它照常给小删除发预授权，只是大删除拿到授权也没用；
    #   · `len(valid)` 在这儿是准的：`pattern` 分支在上面已经展开过
    #     （`wm._path_index.search`），不用在闸里再展开一次
    #     （那会是第二套展开逻辑）。
    #
    # 阈值 `0` = 关掉这个闸，判据是 `> 0`，别拿 `or` 把 0 吃掉。
    _limit = _batch_delete_limit()
    _over = _limit > 0 and len(valid) > _limit

    if _over:
        # 重放的通行证：用户刚同意过这一次批量删除，
    #    框架会拿同样的参数重放这个工具调用 —— 而 `len(valid) > _limit`
    #    照样成立，不认这张通行证就会再弹一次 = 无限审批循环。
    #
    # 它是一格独立的载体，分类器写不到：自动模式的预授权走的是
    #    `grant_one_time_delete_approval`，那是 `has_delete_approval()` 那一格，
    #    而这里问的是 `has_bulk_delete_approval()`，分类器越不过这个闸。
        try:
            from bus.registry import get_workspace_manager
            _wm = get_workspace_manager()
            if _wm and _wm.has_bulk_delete_approval():
                _over = False
        except Exception:
            pass

    # Check approval (one-time or session skip)
    if not _over:
        try:
            from bus.registry import get_workspace_manager
            wm = get_workspace_manager()
            # 一次性那一半按目标问 —— 若是布尔，
            #    "本轮里批过任意一次删除"就够再删任意别的文件。
            #    `_subject` 空，这一半恒不放行（fail-closed）。
            #    会话级那一半（`skip_delete_approval`）照旧整类 ——
            #    用户点的是「本次会话内不再询问删除」，他说的就是整类。
            if wm and (wm.skip_delete_approval
                       or (_subject and wm.has_delete_approval(_subject))):
                return _do_delete(valid, invalid)
        except Exception:
            pass

    # Return pending approval
    # 浮窗上要给路径，不能只给文件名。
    #   只给 `p.name` 时，长名还会被 `_shorten_name` 截成 `前8…后4`
    #   所以用户批准时不知道那是哪个目录下的哪个文件 ——
    #   而"莫名其妙的错删"有一半就来自这里：看不见就没法判断。
    #   相对工作区根就够（那是用户自己设的目录），拿不到才回退完整路径。
    file_names = [_display_path(p, _WS_ROOTS) for p in valid]
    total_size = sum(p.stat().st_size for p in valid)
    total_size_str = (
        f"{total_size} B" if total_size < 1024 else
        f"{total_size/1024:.1f} KB" if total_size < 1024*1024 else
        f"{total_size/1024/1024:.1f} MB"
    )

    # 排版留在这儿（只有这条通道拿得到文件大小），收多少 / 怎么标注省了几个
    # 交给 `core/approval.compact_file_list` —— 那是唯一的策略来源。
    from core.approval import compact_file_list
    rows = [f"  {i+1}. {n} ({_size_str(p)})"
            for i, (p, n) in enumerate(zip(valid, file_names))]
    # 说清这次为什么还要问。
    #   用户种过「本次会话内不再询问」之后再被问一次，看起来像设置坏了 ——
    #   而这一句与提示语同源（`file_list` 是两条通道的共同渲染源），
    #   所以两个通道都会带上它，不会只有一边说。
    _note = f"文件 （共 {len(valid)} 个文件，{total_size_str}）"
    if _over:
        _note += ("\n  ⚠️ 本次要删 %d 个，**超过批量阈值 %d** —— "
                  "这一条**即使本次会话已授权，仍要你看一眼**。"
                  % (len(valid), _limit))
    file_list = compact_file_list(rows, total_note=_note)

    return {
        "status": "pending_delete_approval",
        # 这一格决定弹哪张选项表 ——
        #   `delete_bulk` 只有「1 同意 / 2 不同意」，没有「本次会话内不再询问」。
        #   为什么必须换表：带着那一项，用户点一次就
        #   `_session_skip_delete_approval` 种下，本次会话后面再删 500 个也不问
        #   所以闸会把自己关掉。
        "approval_kind": "delete_bulk" if _over else "delete",
        # 被审批物随载荷一起走 —— 结算器（用户点了同意）要拿它
        #    发那张"这一次"的通行证，而它必须与上面那道闸问的是同一个串。
        #    重算一遍就会漂（"两套渲染源"的同族）。
        "subject": _subject,
        "file_paths": [str(p) for p in valid],
        "invalid": invalid,
        # 选项表收在 `core/approval.py`（唯一一份）
        "prompt": _delete_prompt(file_list),
        # 把已经排好版、已经折叠过的列表也带出去 —— 浮窗的渲染源
        # 就是它。若浮窗自己拿 `file_paths` 重拼一份、且没有折叠逻辑，
        # 同一个授权会在两条通道上长得不一样（IM 列 5 个，Web 全列）。
        "file_list": file_list,
    }


def _do_delete(valid: list, invalid: list) -> dict:
    """送进系统回收站，删不掉的退回永久删并同步索引。

    第一步必须是回收站：`core/recycle.send_to_recycle_bin`
    （`FOF_ALLOWUNDO`），用户在资源管理器里自己就能还原，我们零新界面。
    只有回收站送不进去的那些才退回 `p.unlink()`。

    降级那条路要说出来（`core/recycle.explain` 负责措辞）：
    回收站不可用、或配额满了系统静默永久删 —— 那两条都是有损的，
    判据是「删了 ≠ 删得回来」（同"读不到 ≠ 0"）。
    """
    from core.recycle import explain, send_to_recycle_bin

    outcome = send_to_recycle_bin(valid)
    moved = list(outcome.moved)
    _in_bin = set(moved)

    # 没进回收站的那些 —— 退回旧行为（永久删）。
    # 策略在这里、不在 `core/recycle` 里：那个模块只管"送"，不替调用方决定硬删。
    permanent: list[str] = []
    failed: list[str] = []
    for p in valid:
        if str(p) in _in_bin:
            continue
        try:
            p.unlink()
            permanent.append(str(p))
        except OSError as e:
            failed.append(f"{p.name}: {e}")

    deleted = moved + permanent
    # Keep path index in sync so subsequent listings are accurate
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            for d in deleted:
                try:
                    wm._path_index.remove_file(d)
                except Exception:
                    pass
    except Exception:
        pass

    head = explain(outcome, permanent_ok=len(permanent))
    return {
        "success": len(failed) == 0,
        "deleted": deleted,
        "failed": failed,
        "invalid": invalid,
        "count": len(deleted),
        # 删到哪儿去了 —— 这两格是回收站能力存在的理由，别省。
        "recycled": len(moved),
        "permanently_deleted": len(permanent),
        "hint": head + (f"，{len(failed)} 个失败" if failed else ""),
    }


def _display_path(p, roots) -> str:
    """浮窗/提示语上显示哪个字符串。

    判据：看不见路径，就判不出是不是错删。
    只给 `p.name`、且长名被 `_shorten_name` 截成 `前8…后4` 时，
    用户批准时不知道那是哪个目录下的哪个文件。

    相对某个工作区根就够（那是用户自己设的目录，短且认得出来）；
    一个都套不上，回退完整路径—— 不回退成文件名，
    那正是本条修掉的形状。文件名那一节仍走 `_shorten_name`（长名还会被截），
    但目录留在前面，所以"是哪个目录下的"这一半信息不再丢。
    """
    try:
        base, rel = None, None
        for root in roots or ():
            try:
                rel = Path(p).relative_to(Path(root).resolve())
                base = rel
                break
            except (ValueError, OSError):
                continue
        shown = base if base is not None else Path(p)
    except Exception:                                  # noqa: BLE001
        return str(p)
    parent = str(shown.parent)
    name = _shorten_name(shown.name)
    return name if parent in ("", ".") else "%s/%s" % (parent.replace("\\", "/"), name)


def _shorten_name(name: str) -> str:
    """Shorten a filename for display: keep first 8 + last 4 chars."""
    stem, ext = Path(name).stem, Path(name).suffix
    if len(stem) <= 10:
        return name
    return stem[:8] + "…" + stem[-4:] + ext


def _size_str(p: Path) -> str:
    """Human-readable size."""
    try:
        s = p.stat().st_size
    except OSError:
        return "?"
    if s < 1024:
        return f"{s}B"
    elif s < 1024 * 1024:
        return f"{s/1024:.0f}KB"
    return f"{s/1024/1024:.1f}MB"


# ------------------------------------------------------------------
# Placeholder handlers for search_chunks / summarize_document
# These exist for registry lookup only — real logic is in agent_loop.
# ------------------------------------------------------------------

def search_chunks(query: str = "", file_path: str = "") -> List[Dict]:
    """
    Search indexed chunks (handled by agent_loop via BM25Retriever).
    This function exists for registry lookup only — the real logic is in agent_loop.
    """
    raise NotImplementedError("search_chunks is handled by agent_loop")


def summarize_document(file_path: str = "") -> str:
    """
    Summarize indexed document (handled by agent_loop via iterative_summarize).
    This function exists for registry lookup only — the real logic is in agent_loop.
    """
    raise NotImplementedError("summarize_document is handled by agent_loop")


# ------------------------------------------------------------------
# PDF manipulation tools
# ------------------------------------------------------------------

def pdf_info(file_path: str) -> dict:
    """
    Extract PDF metadata and structure without parsing text content.

    Uses pypdf (BSD-3) to read document metadata, page count, TOC,
    encryption status, and other properties.

    Args:
        file_path: Path to the PDF file.

    Returns:
        dict: {"success": True, "metadata": {...}} with file info.
    """
    _require_security()
    resolved_path = _resolve_path(file_path)
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]

    if not resolved.suffix.lower() == ".pdf":
        return {"success": False, "error": f"Not a PDF file: {file_path}"}

    try:
        reader = PdfReader(str(resolved))
        is_encrypted = bool(reader.is_encrypted)
        if is_encrypted:
            # 空密码能开就继续；开不了就只报"它加密了"（页数/元数据读不出来不是错）
            try:
                reader.decrypt("")
            except Exception:                          # noqa: BLE001
                pass
        try:
            page_count = len(reader.pages)
        except Exception:                              # noqa: BLE001
            page_count = 0
        meta = reader.metadata or {}
        try:
            _hdr = str(reader.pdf_header or "")
            fmt = ("PDF " + _hdr.split("-", 1)[1]) if "-" in _hdr else ""
        except Exception:                              # noqa: BLE001
            fmt = ""

        def _s(key):
            v = meta.get(key, "") if meta else ""
            return str(v or "")

        info = {
            "file_name": resolved.name,
            "file_size_bytes": resolved.stat().st_size,
            "page_count": page_count,
            "format": fmt,
            "title": _s("/Title"),
            "author": _s("/Author"),
            "subject": _s("/Subject"),
            "keywords": _s("/Keywords"),
            "creator": _s("/Creator"),
            "producer": _s("/Producer"),
            "creation_date": _s("/CreationDate"),
            "modification_date": _s("/ModDate"),
            "is_encrypted": is_encrypted,
        }

        # TOC / outline（pypdf 给的是嵌套 list，要摊平并把 destination 换成页码）
        toc_formatted: list = []

        def _walk(items, level: int) -> None:
            for item in items or []:
                if isinstance(item, list):
                    _walk(item, level + 1)
                    continue
                try:
                    pno = reader.get_destination_page_number(item) + 1
                except Exception:                      # noqa: BLE001
                    pno = 0
                toc_formatted.append({
                    "level": level,
                    "title": str(getattr(item, "title", "") or ""),
                    "page": pno,
                })

        try:
            _walk(reader.outline, 1)
        except Exception:                              # noqa: BLE001 - 坏的 outline 不毁整份报告
            toc_formatted = []
        info["toc"] = toc_formatted
        info["toc_count"] = len(toc_formatted)

        return {"success": True, "metadata": info}

    except Exception as e:
        return {"success": False, "error": f"PDF info extraction failed: {type(e).__name__}: {e}"}


def merge_pdfs(file_paths: list, output_name: str = "merged.pdf") -> dict:
    """
    Merge multiple PDF files into a single output PDF.

    Each source is opened, validated, and its pages inserted into a new
    document in the order provided.  Encrypted PDFs are rejected with
    a clear error.

    Args:
        file_paths: List of PDF file paths to merge, in order.
        output_name: Output filename (default: "merged.pdf").

    Returns:
        dict with success, output_path, total_pages, source_count.
    """
    _require_security()

    if not file_paths:
        return {"success": False, "error": "file_paths cannot be empty."}
    if not output_name.endswith(".pdf"):
        output_name += ".pdf"

    # Resolve and validate all input paths
    resolved_paths: list = []
    for fp in file_paths:
        rp = _resolve_path(fp)
        validated = _security.validate_path(str(rp))  # type: ignore[union-attr]
        if not validated.suffix.lower() == ".pdf":
            return {"success": False, "error": f"Not a PDF file: {fp}"}
        resolved_paths.append(validated)

    # Security: validate output destination
    # 写那一类 —— 落点只有一个口径（`primary_output_dir`）
    out_path = _security.validate_path(  # type: ignore[union-attr]
        str(primary_output_dir() / output_name)
    )

    # Merge
    try:
        writer = PdfWriter()
        total_pages = 0
        for rp in resolved_paths:
            src = PdfReader(str(rp))
            if src.is_encrypted:
                return {
                    "success": False,
                    "error": f"PDF is encrypted and cannot be merged: {rp.name}",
                }
            for page in src.pages:
                writer.add_page(page)
            total_pages += len(src.pages)
        with open(str(out_path), "wb") as fh:
            writer.write(fh)
    except Exception as e:
        return {"success": False, "error": f"Merge failed: {type(e).__name__}: {e}"}

    return {
        "success": True,
        "output_path": str(out_path),
        "total_pages": total_pages,
        "source_count": len(resolved_paths),
    }


def split_pdf(
    file_path: str,
    pages_per_file: int = 1,
    output_pattern: str = "",
) -> dict:
    """
    Split a PDF into multiple files, each containing N pages.

    Args:
        file_path: Path to the PDF file to split.
        pages_per_file: Number of pages per output file (default: 1).
        output_pattern: Custom filename pattern using {n} for part number.
                       Default: '{stem}_part{n}.pdf'.

    Returns:
        dict with success, output_files list, source_page_count, file_count.
    """
    _require_security()
    resolved_path = _resolve_path(file_path)
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]

    if not resolved.suffix.lower() == ".pdf":
        return {"success": False, "error": f"Not a PDF file: {file_path}"}

    stem = resolved.stem
    pattern = output_pattern or f"{stem}_part{{n}}.pdf"

    files_written: list = []
    try:
        src = PdfReader(str(resolved))
        if src.is_encrypted:
            return {"success": False,
                    "error": f"PDF is encrypted and cannot be split: {resolved.name}"}
        total = len(src.pages)
        part = 0
        for start in range(0, total, pages_per_file):
            part += 1
            end = min(start + pages_per_file, total)

            out_doc = PdfWriter()
            for i in range(start, end):
                out_doc.add_page(src.pages[i])

            fname = pattern.format(n=part)
            if not fname.endswith(".pdf"):
                fname += ".pdf"
            out_dir = _security.validate_path(  # type: ignore[union-attr]
                str(primary_output_dir() / fname)
            )
            with open(str(out_dir), "wb") as fh:
                out_doc.write(fh)

            files_written.append({
                "path": str(out_dir),
                "pages": end - start,
            })

    except Exception as e:
        return {"success": False, "error": f"Split failed: {type(e).__name__}: {e}"}

    return {
        "success": True,
        "output_files": files_written,
        "source_page_count": total,
        "file_count": len(files_written),
    }


def extract_pages(
    file_path: str,
    pages: list,
    output_name: str = "",
) -> dict:
    """
    Extract specific pages from a PDF into a new PDF file.

    Pages are 1-indexed (page 1 is the first page).  Duplicate page
    numbers are allowed — each occurrence produces a copy of that page
    in the output.

    Args:
        file_path: Path to the source PDF file.
        pages: List of page numbers to extract (1-indexed).
        output_name: Output filename. Default: '{stem}_extracted.pdf'.

    Returns:
        dict with success, output_path, extracted_page_count.
    """
    _require_security()
    resolved_path = _resolve_path(file_path)
    resolved = _security.validate_path(str(resolved_path))  # type: ignore[union-attr]

    if not resolved.suffix.lower() == ".pdf":
        return {"success": False, "error": f"Not a PDF file: {file_path}"}
    if not pages:
        return {"success": False, "error": "pages list cannot be empty."}

    stem = resolved.stem
    fname = output_name or f"{stem}_extracted.pdf"
    if not fname.endswith(".pdf"):
        fname += ".pdf"

    out_path = _security.validate_path(  # type: ignore[union-attr]
        str(primary_output_dir() / fname)
    )

    try:
        src = PdfReader(str(resolved))
        if src.is_encrypted:
            return {"success": False,
                    "error": f"PDF is encrypted and cannot be read: {resolved.name}"}
        total_pages = len(src.pages)
        out_doc = PdfWriter()

        for page_num in pages:
            if page_num < 1 or page_num > total_pages:
                return {
                    "success": False,
                    "error": (
                        f"Page {page_num} is out of range. "
                        f"Document has {total_pages} pages (1-indexed)."
                    ),
                }
            out_doc.add_page(src.pages[page_num - 1])

        with open(str(out_path), "wb") as fh:
            out_doc.write(fh)

    except Exception as e:
        return {"success": False, "error": f"Page extraction failed: {type(e).__name__}: {e}"}

    return {
        "success": True,
        "output_path": str(out_path),
        "extracted_page_count": len(pages),
        "source_page_count": total_pages,
    }


# ------------------------------------------------------------------
# PDF 页面操作族 + 图片族
# ------------------------------------------------------------------
#
# 为什么成族地加：像「把 4 张扫描图合成一份 PDF」这样的请求，
# 若没有对应工具，模型只能自己写 PIL 脚本走 `run_command`
# （那是正确的做法，不是绕路），代价是必然过审批（一次意图两次浮窗）。
#
# 「`merge_pdfs`/`split_pdf`/`extract_pages` 已经有了」——别再重复造：
# 这三件属于 `file_parser` 既有的 PDF 族，且已覆盖测试
# （`tests/test_file_parser.py`）。这一族补的是它们盖不住的那几件：
# 图片→PDF、PDF→图片、旋转、水印、压缩、加密、页序重排，
# 以及图片自己的压缩/缩放/转格式/旋转。
#
# 依赖：`pypdf`(BSD-3) · `pypdfium2`(BSD-3/Apache-2) ·
#   `reportlab`(BSD) · `PIL`(MIT-CMU) —— 全部宽松许可，零 AGPL。
#
# 输出路径一律走 `_out_path()`（见下面）：它是这一族唯一的出入口，
# 因为它同时承担「落 workspace」与「按 `"write"` 校验」两件事。
# 「按读校验、然后写」那个家族在这一族里从出生起就不存在。


def _out_path(name: str) -> Path:
    """本族唯一的输出路径入口：落 `workspace/` + 按 `"write"` 校验。

    两个理由合成一个函数：
      ① 写盘的东西必须按 `"write"` 过围栏，默认的 `"read"` 挡不住写；
      ② 只取 basename —— 名字是模型给的，拼路径前必须剥掉 `../`
         （`create_document` 的 txt 分支就是同一条规则）。
    绕过这个函数去拼 `Path("workspace") / name` 就等于把上面两条一起放弃。
    """
    from pathlib import PurePath
    safe = PurePath(str(name).replace("\\", "/")).name or "output"
    target = primary_output_dir() / safe
    target.parent.mkdir(parents=True, exist_ok=True)
    return Path(_security.validate_path(str(target), "write"))  # type: ignore[union-attr]


def _ensure_pdf_ext(name: str) -> str:
    safe = Path(str(name).replace("\\", "/")).name or "output.pdf"
    return safe if safe.lower().endswith(".pdf") else safe + ".pdf"


def _input_pdf(file_path: str) -> Path:
    """输入 PDF 的公共前置：解析 → 按 `"read"` 校验 → 确认是 PDF。"""
    _require_security()
    resolved = _security.validate_path(
        str(_resolve_path(file_path)), "read")  # type: ignore[union-attr]
    if not resolved.exists():
        raise ValueError(f"文件不存在: {file_path}")
    if resolved.suffix.lower() != ".pdf":
        raise ValueError(f"需要 PDF 文件，但给的是 '{resolved.suffix}': {file_path}")
    return resolved


def images_to_pdf(file_paths: list, output_name: str = "images.pdf") -> dict:
    """把多张图片合成一份 PDF。

    支持的输入：png / jpg / jpeg / bmp / gif / webp / tif / tiff（PIL 能开的都行）。
    顺序就是你给的顺序 —— 不排序、不猜（文件名里的数字序要靠调用方自己排好，
    这跟 `merge_pdfs` 是同一条规矩）。

    为什么必须有它：`merge_pdfs` 只吃 PDF、帮不上忙；
    没有它时只能写 PIL 脚本走 `run_command`，而那条路必然过审批。
    """
    _require_security()
    if not file_paths:
        return {"success": False, "error": "file_paths 不能为空。"}

    from PIL import Image

    out_path = _out_path(_ensure_pdf_ext(output_name))
    frames = []
    try:
        for fp in file_paths:
            src = _security.validate_path(
                str(_resolve_path(fp)), "read")  # type: ignore[union-attr]
            if not src.exists():
                return {"success": False, "error": f"图片不存在: {fp}"}
            img = Image.open(str(src))
            # PDF 不能存 RGBA/P —— 一律先转 RGB（透明底变白，别丢成黑）
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            frames.append(img)
        if not frames:
            return {"success": False, "error": "没有可用的图片。"}
        first, rest = frames[0], frames[1:]
        first.save(str(out_path), "PDF", save_all=True, append_images=rest,
                   resolution=150.0)
    except Exception as e:
        return {"success": False,
                "error": f"合成 PDF 失败: {type(e).__name__}: {e}"}

    return {
        "success": True,
        "output_path": str(out_path),
        "page_count": len(frames),
        "source_count": len(file_paths),
        "hint": "用 deliver_file 发给用户，或继续用别的 PDF 工具处理它。",
    }


def pdf_to_images(file_path: str, output_prefix: str = "",
                  pages: list | None = None, dpi: int = 150,
                  fmt: str = "png") -> dict:
    """把 PDF 每页渲染成图片（扫描件的逆操作）。

    `pages` 空 = 全部页；`pages` 是 1 基（与 `extract_pages` 一致）。
    `fmt` 取 png / jpg。

    一页一个文件，页多时文件多。`max_pages` 兜底 50 页，
    要更多就分批（避免一次调用产出一整个目录）。
    """
    resolved = _input_pdf(file_path)
    fmt = "jpg" if str(fmt).lower() in ("jpg", "jpeg") else "png"
    dpi = max(36, min(int(dpi or 150), 600))
    stem = output_prefix or resolved.stem
    stem = Path(str(stem).replace("\\", "/")).name or "page"

    import pypdfium2 as pdfium

    written: list = []
    total = 0
    doc = pdfium.PdfDocument(str(resolved))
    try:
        total = len(doc)
        wanted = [p for p in (pages or range(1, total + 1))
                  if isinstance(p, int) and 1 <= p <= total]
        if not wanted:
            return {"success": False, "error": f"pages 为空或全部越界（共 {total} 页）"}
        if len(wanted) > 50:
            return {"success": False,
                    "error": f"一次最多渲染 50 页（你给了 {len(wanted)} 页）—— 分批来。"}
        for pno in wanted:
            bitmap = doc[pno - 1].render(scale=dpi / 72.0)
            img = bitmap.to_pil()
            out = _out_path(f"{stem}_p{pno:03d}.{fmt}")
            if fmt == "jpg":
                img.convert("RGB").save(str(out), "JPEG", quality=85)
            else:
                img.save(str(out), "PNG")
            written.append({"page": pno, "path": str(out),
                            "bytes": out.stat().st_size})
    finally:
        doc.close()

    return {
        "success": True,
        "output_files": written,
        "file_count": len(written),
        "source_page_count": total,
        "dpi": dpi,
        # 每页一张图 —— 界面按缩略图网格展示
        #   （`core/artifacts.py` 收进附件目录、最多 12 张，见 `MAX_PER_RESULT`）。
        "artifacts": [{"path": w["path"], "name": Path(w["path"]).name}
                      for w in written],
    }


def rotate_pdf_pages(file_path: str, angle: int = 90,
                     pages: list | None = None,
                     output_name: str = "") -> dict:
    """旋转 PDF 的页（`pages` 空 = 全部页；`pages` 1 基）。

    `angle` 是增量：90 / 180 / 270 顺时针。在已有旋转上叠加
    （`pypdf` 的 `Page.rotate` 是增量：在当前 `/Rotate` 上累加）。
    """
    resolved = _input_pdf(file_path)
    if int(angle) % 90 != 0:
        return {"success": False, "error": f"angle 必须是 90 的倍数，给的是 {angle}"}
    out_path = _out_path(_ensure_pdf_ext(
        output_name or f"{resolved.stem}_rotated.pdf"))

    src = PdfReader(str(resolved))
    if src.is_encrypted:
        return {"success": False,
                "error": f"PDF is encrypted and cannot be rotated: {resolved.name}"}
    total = len(src.pages)
    wanted = [p for p in (pages or range(1, total + 1))
              if isinstance(p, int) and 1 <= p <= total]
    if not wanted:
        return {"success": False, "error": f"pages 为空或全部越界（共 {total} 页）"}
    wanted_set = set(wanted)
    writer = PdfWriter()
    for i, page in enumerate(src.pages, start=1):
        if i in wanted_set:
            page.rotate(int(angle))
        writer.add_page(page)
    with open(str(out_path), "wb") as fh:
        writer.write(fh)

    return {"success": True, "output_path": str(out_path),
            "rotated_pages": len(wanted), "angle": int(angle)}


def reorder_pdf_pages(file_path: str, pages: list,
                      output_name: str = "") -> dict:
    """按 `pages` 给的顺序重排整个 PDF（可用来删页：不列出来就等于丢掉）。

    `pages` 1 基，允许重复（重复即复制该页），也允许不连续。
    """
    resolved = _input_pdf(file_path)
    if not pages:
        return {"success": False, "error": "pages 不能为空。"}
    out_path = _out_path(_ensure_pdf_ext(
        output_name or f"{resolved.stem}_reordered.pdf"))

    src = PdfReader(str(resolved))
    if src.is_encrypted:
        return {"success": False,
                "error": f"PDF is encrypted and cannot be reordered: {resolved.name}"}
    total = len(src.pages)
    bad = [p for p in pages if not isinstance(p, int) or not 1 <= p <= total]
    if bad:
        return {"success": False,
                "error": f"这些页码越界（共 {total} 页，1 基）: {bad}"}
    writer = PdfWriter()
    for p in pages:
        writer.add_page(src.pages[p - 1])
    with open(str(out_path), "wb") as fh:
        writer.write(fh)

    return {"success": True, "output_path": str(out_path),
            "page_count": len(pages), "source_page_count": total}


def pdf_watermark(file_path: str, text: str, output_name: str = "",
                  font_size: int = 40, opacity: float = 0.25,
                  angle: int = 45, color: str = "#FF0000") -> dict:
    """给每一页加文字水印（斜排、居中、半透明）。

    中文可用 —— 走 reportlab 自带的 CID 字体 `STSong-Light`（无需外部字体文件）；
    纯 ASCII 时用 `Helvetica`。
    透明度是近似的：`setFillAlpha` 在部分阅读器里
    只影响填充色，不改变已存在的底层内容。
    """
    resolved = _input_pdf(file_path)
    text = (text or "").strip()
    if not text:
        return {"success": False, "error": "水印文字不能为空。"}
    out_path = _out_path(_ensure_pdf_ext(
        output_name or f"{resolved.stem}_watermarked.pdf"))

    hexs = str(color).lstrip("#")
    try:
        if len(hexs) != 6:
            raise ValueError
        int(hexs, 16)
    except Exception:
        return {"success": False, "error": f"颜色格式不对: {color}（要 #RRGGBB）"}

    is_cjk = any(ord(ch) > 0x2E80 for ch in text)
    opacity = max(0.05, min(float(opacity), 1.0))

    # 水印页用 reportlab（BSD）画，再用
    #   pypdf（BSD-3）的 `merge_page` 盖上去。中文走 reportlab
    #   自带的 CID 字体（`STSong-Light`，无需外部字体文件）。
    from reportlab.pdfgen import canvas as _rl_canvas
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    if is_cjk:
        fontname = "STSong-Light"
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(fontname))
        except Exception:                              # noqa: BLE001 - 重复注册
            pass
    else:
        fontname = "Helvetica"

    src = PdfReader(str(resolved))
    if src.is_encrypted:
        return {"success": False,
                "error": f"PDF is encrypted and cannot be watermarked: {resolved.name}"}
    # 页面从一开始就属于 writer：pypdf 7.x 起，未挂到 writer 的页面调用
    #   `merge_page()`（内部走 `replace_contents()`）会直接抛错。
    writer = PdfWriter(clone_from=str(resolved))
    for page in writer.pages:
        pw = float(page.mediabox.width)
        ph = float(page.mediabox.height)
        buf = io.BytesIO()
        c = _rl_canvas.Canvas(buf, pagesize=(pw, ph))
        c.setFillColor(HexColor("#" + hexs))
        c.setFillAlpha(opacity)
        # 字号：斜排后文本的外接宽度会被放大，所以宁小勿大
        fs = max(8, min(int(font_size), int(pw / max(1, len(text)) * 2)))
        c.setFont(fontname, fs)
        c.saveState()
        c.translate(pw / 2, ph / 2)
        c.rotate(float(angle))
        c.drawCentredString(0, 0, text)
        c.restoreState()
        c.showPage()
        c.save()
        buf.seek(0)
        page.merge_page(PdfReader(buf).pages[0])
    with open(str(out_path), "wb") as fh:
        writer.write(fh)

    return {"success": True, "output_path": str(out_path),
            "page_count": len(PdfReader(str(out_path)).pages),
            "font": fontname, "opacity": opacity}


def compress_pdf(file_path: str, output_name: str = "") -> dict:
    """压缩 PDF（无损那一档：去重对象 + 压缩流 + 清理结构）。

    不重采样图片 —— 那是有损的，而且"压到多小才算够"是调用方的判断。
    这里只做结构层面的瘦身，并把前后字节数如实报出来（`saved_bytes` 可能是
    负数或零：已经压过的文件再压一次常常反而变大）。
    """
    resolved = _input_pdf(file_path)
    before = resolved.stat().st_size
    out_path = _out_path(_ensure_pdf_ext(
        output_name or f"{resolved.stem}_compressed.pdf"))

    src = PdfReader(str(resolved))
    if src.is_encrypted:
        return {"success": False,
                "error": f"PDF is encrypted and cannot be compressed: {resolved.name}"}
    writer = PdfWriter(clone_from=src)
    for page in writer.pages:
        page.compress_content_streams()
    try:
        writer.compress_identical_objects()
    except Exception:                                  # noqa: BLE001 - 老版本没有
        pass
    with open(str(out_path), "wb") as fh:
        writer.write(fh)

    after = out_path.stat().st_size
    return {
        "success": True,
        "output_path": str(out_path),
        "size_before_bytes": before,
        "size_after_bytes": after,
        "saved_bytes": before - after,
        "hint": ("没有变小" if after >= before else
                 f"减小了 {before - after} 字节"),
    }


def encrypt_pdf(file_path: str, user_password: str,
                owner_password: str = "", output_name: str = "",
                allow_printing: bool = True, allow_copying: bool = False) -> dict:
    """给 PDF 加密（AES-256）。

    `user_password` = 打开文件要输的那个；`owner_password` 不给就用同一个。
    密码是模型从对话里拿到的 —— 工具只负责落盘，不回显密码。
    """
    resolved = _input_pdf(file_path)
    if not user_password:
        return {"success": False, "error": "user_password 不能为空。"}
    out_path = _out_path(_ensure_pdf_ext(
        output_name or f"{resolved.stem}_encrypted.pdf"))

    perm = _Perm(0)
    if allow_printing:
        perm |= _Perm.PRINT | _Perm.PRINT_TO_REPRESENTATION
    if allow_copying:
        perm |= _Perm.EXTRACT | _Perm.EXTRACT_TEXT_AND_GRAPHICS

    try:
        src = PdfReader(str(resolved))
        if src.is_encrypted:
            return {"success": False,
                    "error": f"PDF is already encrypted: {resolved.name}"}
        writer = PdfWriter(clone_from=src)
        writer.encrypt(str(user_password),
                       str(owner_password or user_password),
                       permissions_flag=perm,
                       algorithm="AES-256")
        with open(str(out_path), "wb") as fh:
            writer.write(fh)
    except Exception as e:
        return {"success": False, "error": f"加密失败: {type(e).__name__}: {e}"}

    return {"success": True, "output_path": str(out_path),
            "encryption": "AES-256", "allow_printing": bool(allow_printing),
            "allow_copying": bool(allow_copying),
            "hint": "把密码告诉用户时用你自己组织的话，不要说「已自动设置」。"}


# 图片族（这一族是产出方向）

def _input_image(file_path: str):
    """输入图片的公共前置：解析 → 按 `"read"` 校验 → 用 PIL 打开。"""
    _require_security()
    from PIL import Image
    src = _security.validate_path(
        str(_resolve_path(file_path)), "read")  # type: ignore[union-attr]
    if not src.exists():
        raise ValueError(f"图片不存在: {file_path}")
    return src, Image.open(str(src))


def compress_image(file_path: str, output_name: str = "",
                   quality: int = 80, max_width: int = 0) -> dict:
    """压缩图片（重编码 + 可选限宽）。

    `quality` 1-95（jpg/webp 有效）；`max_width` >0 时先等比缩到该宽度。
    png 走 `optimize=True`（png 没有 quality）。
    """
    src, img = _input_image(file_path)
    from PIL import Image

    quality = max(1, min(int(quality or 80), 95))
    suffix = (src.suffix or ".jpg").lower()
    if suffix == ".png":
        suffix = ".png"
    out_name = output_name or f"{src.stem}_compressed{suffix}"
    out_path = _out_path(out_name)

    try:
        im = img
        if max_width and im.width > int(max_width):
            ratio = int(max_width) / im.width
            # `Resampling` 在模块上（`Image.Resampling`），
            # 不在实例上 —— 写成 `im.Resampling` 会 AttributeError。
            # 注意 `compress_image` 默认 `max_width=0` 时会跳过这个分支，
            # 所以这类错误可能只在 `resize_image` 上现形。
            im = im.resize((int(max_width), max(1, int(im.height * ratio))),
                           Image.Resampling.LANCZOS)
        save_kw = {"optimize": True}
        if out_path.suffix.lower() in (".jpg", ".jpeg", ".webp"):
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            save_kw["quality"] = quality
        im.save(str(out_path), **save_kw)
    except Exception as e:
        return {"success": False, "error": f"压缩图片失败: {type(e).__name__}: {e}"}

    before, after = src.stat().st_size, out_path.stat().st_size
    return {"success": True, "output_path": str(out_path),
            "size_before_bytes": before, "size_after_bytes": after,
            "saved_bytes": before - after,
            # 报的必须是产物的尺寸。读 `img`（原图对象）的话，
            # `max_width` 明明缩了、回执却报原尺寸（一个看着对、其实错的数）。
            # 口径与 `resize_image` 对齐：`source_size` / `output_size`。
            "source_size": [img.width, img.height],
            "output_size": [im.width, im.height]}


def resize_image(file_path: str, width: int = 0, height: int = 0,
                 output_name: str = "") -> dict:
    """缩放图片。只给一个维度时等比缩放；两个都给则拉伸到该尺寸。

    两个都不给 = 报错（不是"原样返回"）—— 静默无操作会让调用方以为改过了。
    """
    src, img = _input_image(file_path)
    from PIL import Image

    w, h = int(width or 0), int(height or 0)
    if w <= 0 and h <= 0:
        return {"success": False, "error": "width / height 至少要给一个。"}
    if w <= 0:
        w = max(1, round(img.width * h / img.height))
    if h <= 0:
        h = max(1, round(img.height * w / img.width))

    out_path = _out_path(output_name or f"{src.stem}_{w}x{h}{src.suffix}")
    try:
        img.resize((w, h), Image.Resampling.LANCZOS).save(str(out_path))
    except Exception as e:
        return {"success": False, "error": f"缩放失败: {type(e).__name__}: {e}"}

    return {"success": True, "output_path": str(out_path),
            "source_size": [img.width, img.height], "output_size": [w, h]}


def convert_image_format(file_path: str, target_format: str = "png",
                         output_name: str = "") -> dict:
    """转换图片格式（png / jpg / webp / bmp / tiff）。

    转 jpg/bmp 时丢掉透明通道 —— 透明区会填白，不填黑
    （填黑是 PIL 默认的观感，而那看着像把图弄坏了）。
    """
    src, img = _input_image(file_path)
    fmt = str(target_format or "png").lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in ("png", "jpg", "webp", "bmp", "tiff"):
        return {"success": False,
                "error": f"不支持的格式 '{target_format}'。可选: png/jpg/webp/bmp/tiff"}
    out_path = _out_path(output_name or f"{src.stem}.{fmt}")
    try:
        im = img
        if fmt in ("jpg", "bmp") and im.mode in ("RGBA", "LA", "P"):
            from PIL import Image as _I
            bg = _I.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        im.save(str(out_path))
    except Exception as e:
        return {"success": False, "error": f"格式转换失败: {type(e).__name__}: {e}"}

    return {"success": True, "output_path": str(out_path),
            "source_format": (src.suffix or "").lstrip(".").lower(),
            "target_format": fmt}


def rotate_image(file_path: str, angle: int = 90,
                 output_name: str = "") -> dict:
    """旋转图片。`angle` 是角度增量，正数逆时针（PIL 的约定）。

    `expand=True` —— 90/270 度时把画布撑开，否则图会被裁掉一角。
    """
    src, img = _input_image(file_path)
    a = int(angle or 0) % 360
    if a == 0:
        return {"success": False, "error": "angle 取 360 的倍数等于没转，不执行。"}
    out_path = _out_path(output_name or f"{src.stem}_rot{a}{src.suffix}")
    try:
        img.rotate(a, expand=True).save(str(out_path))
    except Exception as e:
        return {"success": False, "error": f"旋转失败: {type(e).__name__}: {e}"}

    return {"success": True, "output_path": str(out_path), "angle": a}


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _require_security() -> None:
    if _security is None:
        raise RuntimeError(
            "SecurityGuard not initialized. "
            "The skill_loader must call init() before any handler is invoked."
        )


def _path_in_workspace(p: Path) -> bool:
    """这个路径落在可写工作区（Level 2）里吗？

    判据取自 `SecurityGuard.classify_path`，不自己比字符串前缀：
    工作区可以有多个、也可以是绝对路径（`WorkspaceManager` 配的），
    而 `str(p).startswith("workspace")` 在那些情况下会给出
    看着对、其实错的答案 —— 而这里判错的代价是"交付错文件"。

    拿不到安全核时返回 `False` —— fail-closed：宁可走"照调用方给的路径走
    + 后面那次 `validate_path`"，也不要走"工作区优先"这条会覆盖调用方输入的支。
    """
    if _security is None:
        return False
    try:
        from core.security import PathLevel
        _, level = _security.classify_path(str(p))
        return level == PathLevel.READ_WRITE
    except Exception:
        return False


def _resolve_path(file_path: str) -> Path:
    """Try file_path as-is; if it doesn't exist, try under workspace/.

    都找不到就带着路线抛 `FileNotFoundError`：说清查过哪几个根、
    下一步该给什么。原来这里返回一个不存在的路径，由下游 `open()` 抛
    `FileNotFoundError`（只有一行路径）—— 模型拿到它只能猜。
    """
    p = Path(file_path)
    if p.exists():
        return p.resolve()
    # Try under workspaces
    # 「查」—— 全部根都试，不是只试一个
    alt = find_in_roots(p.name)
    if alt is None:
        alt = lookup_roots()[0] / p.name
    if alt.exists():
        return alt.resolve()
    # Neither exists — return the original for security to reject clearly
    return p.resolve()


def _require_file(file_path: str) -> Path:
    """解析类工具的入口：找不到就带着路线说清楚。

    `_resolve_path` 的契约是"返回一个路径"（别的工具还要拿它去判存在性），
    所以"带路线"这件事放在这一层：说清查过哪几个根、下一步该给什么。
    原来下游 `open()` 抛的只有一行路径 —— 模型拿到它只能猜。
    """
    p = _resolve_path(file_path)
    if p.exists():
        return p
    roots: list[str] = []
    try:
        roots = [str(r) for r in lookup_roots()[:3]]
    except Exception:                                          # noqa: BLE001
        roots = []
    where = ("（查过：%s）" % "、".join(roots)) if roots else ""
    raise FileNotFoundError(
        "找不到这个文件：%s%s。如果它在别处，请传完整路径 ——"
        " 先用 `list_workspace_files` 或 `ls` 拿到绝对路径再传给这个工具。"
        % (file_path, where))


# File delivery to user

# Module-level delivery context — set by the pipeline before tool calls
_delivery_session: dict = {}

def set_delivery_context(session_key: str, channel_name: str = "",
                         user_id: str = "", extra: dict | None = None) -> None:
    """Called by the pipeline to register the current session for delivery."""
    global _delivery_session
    _delivery_session = {
        "session_key": session_key,
        "channel": channel_name,
        "user_id": user_id,
        "extra": extra or {},
    }


#: 本轮交付给用户的文件。`deliver_file` 写，两个读者：
#: Web UI 在 /api/chat 响应里读（渲染可点的下载按钮），IM 通道在回复之后
#: 取走自己那一轮的发出去（`core/delivery_outbox`）。单靠回复文本里的路径
#: 做不到这两件事。
def _record_delivery(name: str, path: Path, method: str, size_kb: float,
                     *, channel: str = "", user_id: str = "",
                     session_key: str = "") -> None:
    from core import delivery_outbox as _outbox
    _outbox.put(name=name, path=str(path), method=method, size_kb=size_kb,
                session_key=session_key, channel=channel, user_id=user_id)


def take_delivered() -> list[dict]:
    """取出并清空本轮交付记录（Web 那条路：一次请求取一次）。

    IM 通道不走这里 —— 它用 `core/delivery_outbox.take_for(...)` 按
    `session_key + channel + user_id` 三格筛，只取自己那一轮的（六个通道
    共用一个进程，私聊还共用 `workspace:private` 这一个键）。
    """
    from core import delivery_outbox as _outbox
    return _outbox.take_all()


def _queue_channel_delivery(ctx: dict, path: Path, name: str,
                            size_kb: float) -> dict | None:
    """当前通道自己会发文件就登记一条并返回回执；否则 `None`（走旧路）。

    这里只登记，不发送 —— 判据见 `deliver_file` 的 docstring 末段。
    判据用能力协议（`channels/capabilities.py::MediaSendCapable`），
      不按平台名硬编码：谁实现了它就自动被这条路认出来。
    """
    channel_name = str(ctx.get("channel", "") or "")
    if not channel_name:
        return None
    platform = (channel_name[:-len("_channel")]
                if channel_name.endswith("_channel") else channel_name)
    try:
        from channels.capabilities import MediaSendCapable
        from core.cron.runner import resolve_channel
        ch = resolve_channel(platform)
    except Exception as exc:                                   # noqa: BLE001
        print("[deliver] 找通道失败（%s: %s），改走邮件" % (type(exc).__name__, exc))
        return None
    if ch is None or not isinstance(ch, MediaSendCapable):
        return None

    try:
        from core import delivery_outbox as _outbox
        _outbox.put(name=name, path=str(path), method="channel", size_kb=size_kb,
                    session_key=str(ctx.get("session_key", "") or ""),
                    channel=channel_name,
                    user_id=str(ctx.get("user_id", "") or ""))
    except Exception as exc:                                   # noqa: BLE001
        print("[deliver] 登记待发文件失败（%s: %s），改走邮件"
              % (type(exc).__name__, exc))
        return None

    return {
        "success": True,
        "method": "channel",
        "file": name,
        "size_kb": size_kb,
        "path": str(path),
        #: 这条回执是给模型看的操作状态，不是给用户看的话。
        #:   不写这一句，模型会把"走哪个通道、失败会怎么报"原样转述出去 ——
        #:   用户读到的是框架内部机制（"随这条回复一起发出""通道会单独回一句话"）。
        #:   失败那条路自己会产回执（见 `_drain_delivered`），这里不必预告。
        "message": ("已交给 %s 通道，随这条回复一起发出：%s（%s KB）。"
                    "（操作回执，不必向用户转述通道名与发送机制。）"
                    % (platform, name, size_kb)),
    }


def deliver_file(file_path: str, via: str = "auto") -> dict:
    """把已生成的文件推送给用户。

    三条必须避免的形状：

    1. 描述诱导调用 —— 若描述写成"生成文件后调用"，
       模型会每生成一个文件就调一次，而用户根本没要求发送。
    2. 通道顺序写死"邮件优先" —— 会话可能是 Web：用户就坐在屏幕前，
       发邮件毫无意义，只是白等一次 SMTP 往返（可能挂在
       `SMTPServerDisconnected`）。
    3. 失败信息污染回复 —— SMTP 的报错若堆进 message，用户会看到一条
       与自己无关的 "邮件发送失败"。

    按会话通道决定（`_delivery_session["channel"]`）：

    ===========================  ====================================
    web / cli                    只给下载/路径（用户在本地，不发邮件）
    有 send_media_file 的通道     直发：登记一条，通道在回复之后发出去
    其余 IM 通道 / 未知通道       邮件（那些通道还没有出站媒体实现）
    ===========================  ====================================

    `via` 可显式指定：用户说"发到我邮箱" → `via="email"`。

    `via="download"` 只在 `web` / `cli` 会话里成立：IM 会话里
    「只给路径」= 对方什么都没收到（钉钉里要图，
    模型选了 `download`，用户一张都没收到，而回执写着"文件已就绪"）
    所以那种情况下降级成直发，回执里说明降级 —— 判据是
    远端的人用不了服务器上的本地路径，而不是"照参数办事"。

    直发那条为什么是"登记"而不是"现在就发"：本函数是同步工具，跑在
      agent 的事件循环线程上；而发送要用通道的异步客户端，跨循环用会挂、
      在同一个循环里 `asyncio.run` 会死锁。登记进 `core/delivery_outbox`，
      通道自己（在它的循环里、回复之后）取走发掉；发不出去它会回一句话。
    """
    # 「交付哪个文件」的判据。
    #
    # 常见的错误请求是「Agent 拿到 Desktop 源文件路径而非 workspace 生成文件路径」。
    # 若解析只用「这个路径存在吗」当判据，
    # 一个工作区外的、存在的绝对路径会无条件胜出，而「工作区里刚生成的那个」
    # 连比较的机会都没有。同名两份都在时：
    #     传 `C:\...\dup.txt`，会交付"外面的同名文件"，工作区回落根本没跑。
    #
    # 还有一条更硬的：这个函数必须自己调 `validate_path` ——
    # 硬约束是「`validate_path()` 在 `open()` 之前」。Web 通道下游
    # `/api/download` 会 403（症状是"一个点了没用的按钮"），而邮件/IM 通道
    # 没有任何下游检查，那份文件真会被发出去。
    #
    # 判据（工作区优先 + 可听），两档：
    #   ① 工作区里有同名产物，它赢，且回执里说清"我交的是哪一份"
    #      —— 判据是"交付已生成的产物"（这就是本工具文档里的职责），
    #      而不是"调用方恰好给了个存在的路径"。
    #   ② 工作区里没有，照调用方给的路径走（"把我桌面那个 pdf 发我"是正当用法），
    #      但必须过 `validate_path`。
    #
    # op 用 `"read"` 而不是 `"write"`，这是刻意的：跨区 READ_ONLY 的
    # `read` 是放行的 —— 而"把桌面上那份发我"本来就该能发（那是用户自己的文件、
    # 用户自己点的）。这一句拦住的是 FORBIDDEN 区（系统目录），
    # 不是"把跨区文件也一律拒掉"。别把它读成一条防外发的闸 ——
    # 出网地板管的是"出网"，与这里无关。
    _require_security()
    given = Path(file_path)
    #: 相对路径先按「工作区」解析，再退回进程 CWD：
    #:   模型给的是 `_pdfpages/p01.png`（相对工作区），而这里原来只按
    #:   进程 CWD（仓库根）解析，回 `文件不存在`，它改成绝对路径重发
    #:   才发出去。模型补不上那一次，就是"它说发了、用户什么都没收到"。
    #:   判据是「写那口径只有一处」（`primary_output_dir()`），
    #:   不是"调用方按哪个目录想事情"。两边都没有才走后面的报错。
    if not given.is_absolute():
        for _base in (primary_output_dir(), Path.cwd()):
            _cand = _base / given
            if _cand.exists():
                given = _cand
                break
    # 「查」—— 同名产物可能落在任何一个工作组里
    ws_copy = find_in_roots(given.name) or (lookup_roots()[0] / given.name)
    prefer_note = ""
    if ws_copy.exists() and not _path_in_workspace(given):
        # ① —— 覆盖调用方给的路径。出声是关键：万一这次调用方真是要外面那份，
        #      回执里有完整路径，模型能自己纠正（而不是被静默换掉）。
        prefer_note = (f"（你给的是工作区外的 {given}，而工作区里有同名产物；"
                       f"已按'交付已生成产物'交付工作区那一份。"
                       f"若确实要外面那份，请改用 copy_to_workspace 复制进来再交付。）")
        path = ws_copy.resolve()
    else:
        path = given
        if not path.exists():
            if ws_copy.exists():
                path = ws_copy
            else:
                return {"success": False, "error": f"文件不存在: {file_path}",
                        "hint": "检查文件路径是否正确，或文件是否已成功导出"}
    try:
        path = _security.validate_path(str(path), "read")  # type: ignore[union-attr]
    except Exception as e:
        return {"success": False, "error": f"安全限制: {e}",
                "hint": "跨区只读区的文件不能直接交付；先用 copy_to_workspace "
                        "复制进工作区，再交付工作区里那一份。"}

    name = path.name
    size_kb = round(path.stat().st_size / 1024, 1)
    ctx = _delivery_session
    results = []

    channel = str(ctx.get("channel", "") or "").lower()
    _local_session = any(k in channel for k in ("web", "cli"))

    # 通道直发：只有自己会把字节送上去的通道才有这一条（微信 / 飞书）
    #
    # `download` 在 IM 会话里等于「什么都没发」（钉钉）：
    #   用户在群里说「你给我第三页个图」，模型选了 `via="download"`，
    #   回执 `{"success": true, "method": "download", "message": "📥 文件已就绪…"}`
    #   而用户一张图都没收到 —— 文件只留在服务器硬盘上，远端的人拿一个
    #   `D:\…` 路径没有任何用处。回执还写着"已就绪"，这是「标签在说谎」同族。
    #   所以降级成直发，并在回执里说清降级这件事（模型据此才不会再说"路径给你了"）。
    #   `web` / `cli` 会话不受影响 —— 那里 `download` 本来就是对的。
    _downgraded = (via == "download" and not _local_session)
    if (via == "auto" or _downgraded) and not _local_session:
        _queued = _queue_channel_delivery(ctx, path, name, size_kb)
        if _queued is not None:
            _notes = []
            if _downgraded:
                _notes.append(
                    "★ `via=\"download\"` 在 IM 会话里**对方收不到**（那只是把文件"
                    "留在服务器上）—— 已按**直发**处理。不要再对用户说"
                    "「文件已就绪 / 路径是 D:\\…」，那对他没用。")
                _queued["via"] = "download->auto"
            # 替换了调用方给的路径就必须出声（判据见上面「交付哪一份」那一段）
            #   —— 直发这条路上它同样承重：模型以为发的是 A、用户收到的是 B。
            if prefer_note:
                _notes.append(prefer_note)
            if _notes:
                _queued["message"] = ((_queued.get("message") or "")
                                      + "\n" + "\n".join(_notes))
            return _queued

    # 先决定要不要走邮件（见上方 docstring）
    want_email = (via == "email") or (via == "auto" and not _local_session)

    # Tier 1: Email attachment (if mail_parser is configured)
    #
    # 读配置只走 `core.mail_config` 那一个口。
    #
    # 若这里自己内联一份（读 settings + 认多账号 + provider 预设兜底），
    # 而 `send_email` 一个字都不读，同一个「读」有两份，
    # 而其中一份谁都不读（症状：设置页说"已配置"，模型还追着用户要授权码）。
    # 所以权威读取口只有 `core.mail_config`。行为逐字不变（多账号 / 旧扁平格式 /
    #   `active` 指向不存在的 id 时退而取第一个 / provider 预设兜底，都在它里面）。
    from core import mail_config as _mailcfg
    _acc = _mailcfg.active_account()
    email_addr = _acc.get("email_address", "")
    email_pass = _acc.get("password", "")
    preset = _acc.get("_provider", "qq")

    print(f"[deliver] mail_cfg: email={'***' if email_addr else 'MISSING'}, "
          f"pass={'***' if email_pass else 'MISSING'}, preset={preset}")
    if not want_email:
        print(f"[deliver] via={via} channel={channel!r} → 本地会话，走下载不走邮件")
    elif email_addr and email_pass:
        print(f"[deliver] entering email send block...")
        try:
            smtp, smtp_port = _mailcfg.smtp_params(_acc)

            # mail_parser is lazy-loaded — its _security may be None.
            # Bootstrap it with our own security (or create one if needed).
            import skills.mail_parser.handler as mail_handler
            if mail_handler._security is None:
                if _security is not None:
                    mail_handler.init(_security)
                else:
                    from core.security import SecurityGuard
                    mail_handler.init(SecurityGuard(
                        # 这是包含性那一类 —— 取真实落点，不取字面量。
                        allowed_dirs=[str(primary_output_dir())],
                        command_blacklist=[],
                        audit_log_path="data/audit.log",
                    ))

            result = mail_handler.send_email(
                smtp_server=smtp,
                email_address=email_addr,
                password=email_pass,
                to=email_addr,
                subject=f"Crannia 文件: {name}",
                body=f"请查收附件中的文件：{name}（{size_kb}KB）",
                port=smtp_port,
                attachments=[str(path)],
            )
            print(f"[deliver] send_email result: success={result.get('success')}, "
                  f"error={result.get('error', 'none')}")
            if result.get("success"):
                results.append(f"📧 文件已通过邮件发送至 {email_addr}")
                return {"success": True, "method": "email",
                        "file": name, "size_kb": size_kb,
                        "message": f"文件 {name} 已发送至 {email_addr}，请检查邮箱（含垃圾箱）"}
            else:
                results.append(f"⚠️ 邮件发送失败: {result.get('error', '未知错误')}")
        except Exception as e:
            ename = type(e).__name__
            # Network errors — give a human-readable hint
            if 'gaierror' in ename or 'getaddrinfo' in str(e).lower():
                results.append(
                    f"⚠️ 邮件发送失败：无法连接邮件服务器（{smtp}:{smtp_port}）。"
                    f"请检查网络连接或稍后重试。"
                )
            elif 'timeout' in str(e).lower() or 'Timeout' in ename:
                results.append(
                    f"⚠️ 邮件发送超时：服务器 {smtp}:{smtp_port} 无响应。请稍后重试。"
                )
            else:
                results.append(f"⚠️ 邮件发送异常: {ename}: {e}")
    elif want_email:
        results.append("📧 邮件未配置。在 Web UI「技能」页配置 mail_parser 后即可自动发送文件。")

    # Tier 2: Download
    # 兜底永远给：不管上面成功与否，用户总能拿到文件。
    method = "email" if any(r.startswith("📧") for r in results) else "download"
    results.append(f"📥 文件已就绪: {name} ({size_kb}KB)")
    if method == "download":
        results.append(f"   路径: {path}")

    # 记录本轮交付，供 Web UI 渲染下载按钮
    # 只在本地会话记：IM 那几个分支没有读者（Web 端点只服务本地会话），
    #    记了会在待发件箱里留到上限（`core/delivery_outbox`）。
    #    直发那条自己已经登记过（`method="channel"`），别在这里再记一次。
    if _local_session:
        _record_delivery(name, path, method, size_kb)

    if prefer_note:
        # 走的是"工作区优先"那一条，必须在回执里出声。
        # 静默换文件是这一族里最坏的形状：模型以为自己交的是 A，
        # 用户收到的是 B，而两边都不会发现。
        results.append(prefer_note)

    return {
        "success": True,
        "method": method,
        "file": name,
        "size_kb": size_kb,
        "path": str(path),
        "message": "\n".join(results),
    }
