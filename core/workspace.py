"""
Workspace Manager: work groups, lightweight path index, file watchers.

Provides:
  - WorkGroup / WorkspaceManager — multi-workspace with granular permissions
  - PathIndex — lightweight in-memory file index
  - FileWatcher / WatcherConfig — USN Journal-based file monitoring,
    with a polling fallback
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# 共享判据：走目录树时跳过哪些条目

#: 索引与文件原语共用同一个「这一条不扫」判据。
#:
#: 必须是同一个函数，不能各写一份：索引与 `glob` / `grep` 走的是同一棵目录树，
#: 两份规则一旦漂移，索引里看不见的文件 `grep` 却搜得到（或者反过来），
#: 而两个答案看着都对。
#: 机械读者 `tests/test_file_tools.py::test_shared_skip_rule` 对两处同时断言。
#:
#: 三段各有理由，一条都不许泛化：
#:   · `$` 开头 —— Windows 的隐藏/系统条目（`$RECYCLE.BIN` 那一族）
#:   · `.` 开头 —— 隐藏文件与目录（`.git` 在这里）
#:   · `_wip_` —— 框架自己的临时工作文件（`create_document` 的中间产物）。
#:     一个默认工作区里能积到上百个、占该目录条目的八九成 ——
#:     列进"工作区有些什么"只会淹掉真东西。
#: 不做"下划线开头一律隐藏"的泛化 —— 用户真的可以有 `_foo.docx`
#: （负对照钉着：`tests/test_workspace_index.py::test_only_wip_prefix_is_skipped`）。
#: 这是遍历的判据，不是访问的判据：显式路径照样能读写这些文件
#: （`read_file(".gitignore")` 不受它管 —— 那是 `SecurityGuard` 的活）。
def skip_walk_entry(name: str) -> bool:
    """走目录树时，这一条要不要跳过？（索引与 `glob`/`grep` 共用）"""
    return name.startswith((".", "$", "_wip_"))


# Data classes

@dataclass
class FileEntry:
    """A single file record in the path index."""
    name: str
    path: str       # absolute path
    ext: str        # lowercase, with dot (e.g. ".docx")
    size: int       # bytes
    modified: str   # 本地时间 "YYYY-MM-DD HH:MM"（不是 UTC）


@dataclass
class WorkGroup:
    """A single workspace directory with permissions."""
    id: str
    name: str
    path: str        # absolute path
    permissions: set[str] = field(default_factory=lambda: {
        "copy", "delete", "move", "rename", "write"
    })
    primary: bool = False


@dataclass
class WatcherConfig:
    """Configuration for a single file watcher."""
    id: str
    watch_path: str          # monitored directory
    target_workspace_id: str # workspace to sync files into
    sync_documents: bool = True   # word/excel/ppt/pdf/txt/csv/md
    sync_images: bool = False     # jpg/png/gif/bmp/psd/webp/ico/svg
    sync_media: bool = False      # mp4/mov/avi/mkv/mp3/wav/flac
    sync_archives: bool = False   # zip/7z/rar
    custom_extensions: list[str] = field(default_factory=list)
    notify_on_sync: bool = True
    auto_start: bool = True
    enabled: bool = False
    exclude_subdirs: list[str] = field(default_factory=list)  # auto-populated subdir names to skip
    # Snapshot mode: "boot" = re-snapshot every framework start;
    # "deploy" = single snapshot at watcher creation time.
    snapshot_mode: str = "boot"


# File type category definitions

DOCUMENT_EXTENSIONS = {
    # Word / writer
    ".txt", ".docx", ".doc", ".dot", ".md",
    # Excel / spreadsheet
    ".xlsx", ".xls", ".xlt", ".xlsm", ".csv",
    # PowerPoint / presentation
    ".pptx", ".ppt", ".pot", ".pps",
    # PDF
    ".pdf",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".ico", ".svg",
                     ".gif", ".bmp", ".psd"}
MEDIA_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav",
                     ".flac", ".aac", ".wma"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}

#: 上面几类里能当纯文本读的那几个（其余都要走各自的解析器）。
TEXT_EXTENSIONS = {".txt", ".md", ".csv"}

# 二进制护栏用的两个查询
#
# 为什么落在这里：这一段上面就是 "File type category definitions" ——
#   扩展名分类的唯一家。类型映射若分散在多处就会漂移（见 `_file_entry`
#   的 docstring），新的分类知识必须回到这里，不能另起一份。
#
# 无工具的那一批从既有集合推导，不手抄第二份：删掉 `.zip` 就等于
#   让 `extract_archive` 再也收不到它 —— 这才是"一处定义、多处引用"。

#: 二进制、且有专用工具的 → 该用哪个（值 = 工具名）。
BINARY_EXT_ROUTES: dict[str, str | None] = {
    ".pdf":  "parse_pdf",
    ".docx": "parse_docx",
    ".xlsx": "parse_xlsx",
    ".xls":  "parse_xlsx",
    ".pptx": "parse_pptx",
    ".ppt":  "parse_pptx",
    ".zip":  "extract_archive",
    ".7z":   "extract_archive",
    ".rar":  "extract_archive",
    # 老格式 Word 模板 / 宏表 / 放映稿 —— 目前没有工具吃得下
    ".doc": None, ".dot": None, ".xlt": None, ".xlsm": None,
    ".pot": None, ".pps": None,
}

#: 可执行体 / 字节码 / 数据库 / 字体 —— 上面四类都没覆盖，只能显式列。
#: 这一份是新知识（既有的分类集合里确实没有），不是重复。
_OPAQUE_EXTENSIONS = {
    ".exe", ".dll", ".so", ".pyd", ".bin", ".pyc", ".class", ".o", ".a",
    ".lib", ".db", ".sqlite", ".sqlite3", ".ttf", ".otf", ".woff", ".woff2",
}

#: 按文本读必然乱码的扩展名 —— 推导出来的，不是手抄的。
BINARY_EXTENSIONS = (
    (DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | MEDIA_EXTENSIONS
     | ARCHIVE_EXTENSIONS | _OPAQUE_EXTENSIONS)
    - TEXT_EXTENSIONS
)


def parser_hint_for(ext: str) -> tuple[bool, str | None]:
    """这个扩展名该不该按文本读？

    返回 (是否二进制, 该用的工具名或 None)。
    `None` 有两种含义，调用方要分开说：不是二进制（第一项为假）
    与是二进制但没有工具（第一项为真、第二项为 None）。
    """
    e = (ext or "").lower()
    if e not in BINARY_EXTENSIONS:
        return (False, None)
    return (True, BINARY_EXT_ROUTES.get(e))


#: 跨区多词搜索的相对下限：分数低于 `top × 这个比例` 的候选直接丢掉。
#: 判据来自两条真实查询：
#:
#: | 阈值 | 那串 426 个候选（top=16） | 「报告 2026」（top=4） |
#: |---|---|---|
#: | 不截 | 200 条 / 21,648 字符 | 26 条 / 3,782 字符 |
#: | ≥50% top | 8 条 / 865 字符 | 26 条（不变） |
#: | ≥75% top | 1 条 | 23 条 |
#:
#: 相对比绝对强（分数尺度随查询变，绝对阈值在那条上一个都筛不掉），
#: 且 50% 不是 75% —— 75% 只剩 1 条，最高分那个万一是碰巧命中的无关文件，
#: 真目标就被一起藏了。
#:
#: 只作用于多词 hint 那条路（它才有分数）。单词 hint 走 `fnmatch`，
#: 行为不变 —— 见 `_search_user_home` 里那段注释。
_SEARCH_RELATIVE_FLOOR = 0.5


#: 跨区扫盘的截止时间（秒）。
#:
#: 同类实现里一个是 20 秒、另两个是 30 秒；这里取 20。
#:
#: 它存在的理由不是性能，是把两种"没有"分开：若超时也只报 `0 个`，
#: 模型会对用户下结论「你电脑上没有这份资料」。而超时之后 `0 个` 有两种
#: 含义，且必须说出来：
#:
#:     判据：搜索可能匹配到了文件，但没在时限内跑完 ——
#:     必须让模型知道"没跑完"，而不是让它以为"没有匹配"。
#:
#: `os.walk` 是同步阻塞的，所以要把它改成可中断的遍历：在
#: 每个目录开头查一次 deadline（不是每个文件 —— 每目录一次已经能把
#: 超时误差控制在一个目录的量级，而且比逐文件快）。
#: 这也意味着不能靠 `signal.alarm` 了事。
_SEARCH_DEADLINE_SECONDS = 20.0


def _search_count_note(stats: dict, max_results: int) -> str:
    """超限/超时/被削时明写条数 —— 返回要追加到 `notice` 的那句，否则空串。

    判据：说条数不说字符 · 说它是按相关度排过序的 ·
    "不知道有多少"与"没搜完"要分开说。三条按严重度从高到低排，
    一次只说最重的那条（并列说会互相削弱）。

    `matched is None`（总数不可知）不等于 `matched == 0` ——
    读不到 ≠ 0，而 0 看起来像一个结论。
    """
    returned = int(stats.get("returned") or 0)
    matched = stats.get("matched")

    # ① 超时 —— 最重的一条：它直接决定"没有"这个结论能不能下。
    #    把超时的 0 当成「你电脑上没有这份资料」，正是要防的误读。
    if stats.get("timed_out"):
        return (f"（跨区搜索到 {int(_SEARCH_DEADLINE_SECONDS)} 秒就停了，"
                f"**可能还有匹配的文件没被扫到** —— 所以这个『没找到』"
                f"**不等于『不存在』**。换个更具体的目录或关键词再试）")

    # ② 撞上限 —— 总数不可知（连"扫了多少"都不知道）
    if stats.get("capped") and matched is None:
        return (f"（已到 {max_results} 个的上限就停了，**可能还有更多** —— "
                f"这个数是按扫描顺序截的，不是按相关度）")

    # ③ 有真数、但列表是排过序的前 N 个（下限削过尾 / 上限截过头）
    if matched is not None and matched > returned:
        return (f"（跨区共匹配 {matched} 个文件，已按相关度排序，"
                f"只列出前 {returned} 个 —— 换更具体的关键词能缩小范围）")
    return ""


def _search_root_note(stats: dict) -> str:
    """说清这一搜到底从哪儿开始 —— 返回要追加到 `notice` 的那句。

    判据：回执不能比搜索本身更自信。

    典型失败：模型传 `scope="global"`，而 `"global"` 在实现里就是用户
    家目录（`_search_user_home` 的 `root_path = Path.home()`）。要找一个
    不在家目录里的文件时，0 个结果是正确行为 —— 但若回执只写
    「Cross-zone search found 0 file(s)」、一个字没提搜的是哪儿，
    模型就会对用户说「全局搜索也没找到」。回执比搜索本身更自信。

    这一条是「永远要说」，不在 `_search_count_note` 那三档里。
    那三档按严重度排、一次只说最重的一条（并列会互相削弱）；
    而本条要挡的是「把『没搜那个地方』读成『那个地方没有』」——
    那个误读在结果正常、条数为 0 或不为 0 时一样会发生。

    返回的路径是解析后的那个（解析在 `_search_user_home` 里做，
    由它写进 `stats`）—— 不在这里重算一遍（一个判定只留一处）。
    """
    root = stats.get("root")
    if not root:
        return ""
    asked = (stats.get("root_requested") or "").strip()
    if stats.get("root_fell_back"):
        return (f"（⚠️ 你要的搜索位置 `{asked}` **没能解析成一个存在的目录**，"
                f"实际搜的是 `{root}` —— 这个『没找到』只覆盖后者）")
    if asked:
        return f"（搜索位置：`{root}`）"
    return (f"（搜索位置：`{root}` —— 这是**没指定位置时的默认值**（用户家目录），"
            f"**不是整块磁盘**。要搜别处就传具体路径，如 `D:/`）")

# Temp / cache file extensions — always ignored by watchers
# These are download placeholders, editor swap files, and system caches
# that should never be synced to the workspace.  A watcher seeing a
# .crdownload file for 2 seconds shouldn't trigger a sync.
TEMP_IGNORE_EXTENSIONS: set[str] = {
    # Browser / download manager incomplete downloads
    ".crdownload",    # Chrome, Edge (Chromium)
    ".download",      # Safari
    ".opdownload",    # Opera
    ".partial",       # IE 9+
    ".part",          # Firefox, JDownloader, eMule, youtube-dl
    ".dtapart",       # DownThemAll!
    ".adadownload",   # Adobe Download Assistant
    ".aria2",         # aria2 / uGet
    ".dlm",           # Akamai Download Manager
    ".dap",           # Download Accelerator Plus
    ".fdpart",        # Fresh Download
    ".egt",           # EagleGet
    # BitTorrent clients
    ".!ut",           # uTorrent
    ".!qb",           # qBittorrent
    ".bc!",           # BitComet
    ".bt!",           # BitSpirit
    ".!bt",           # BitTorrent
    ".az!",           # Vuze / Azureus
    # System & application temp files
    ".tmp",           # Generic temp
    ".temp",          # Generic temp
    ".bak",           # Backup copies
    ".swp",           # Vim swap
    ".swo",           # Vim swap (recovery)
    ".lnk",           # Windows shortcut
    ".lock",          # Lock files
    ".lck",           # Lock files
    ".dmp",           # Memory / crash dumps
    ".etl",           # Windows trace logs
    ".chk",           # Corel / recovered fragments
}
# Filesystem artefacts — always ignored by watchers regardless of extension
TEMP_IGNORE_FILENAMES: set[str] = {
    "thumbs.db", "desktop.ini", ".ds_store",
    "ehthumbs.db", "~$",  # prefix match for Office temp files
}

# Default user directories to exclude from cross-zone search
# These are high-noise / low-value areas that slow down searches and
# risk exposing sensitive or irrelevant data.
# Templates: `{home}` = user profile, absolute paths used for system dirs.
DEFAULT_CROSS_ZONE_EXCLUDE: list[str] = [
    # Windows system directories
    "C:/Windows",
    "C:/Program Files",
    "C:/Program Files (x86)",
    "C:/ProgramData",
    "C:/$Recycle.Bin",
    "C:/System Volume Information",
    "C:/Recovery",
    # App data — massive, noisy, potentially sensitive
    "{home}/AppData",
    # Dev dependency caches — millions of tiny files
    "{home}/node_modules",
    "{home}/.npm",
    "{home}/.cache",
    "{home}/.cargo",
    "{home}/.rustup",
    "{home}/.gradle",
    "{home}/.m2",         # Maven
    "{home}/.ivy2",       # SBT / Scala
    # Python virtualenvs / caches (if inside home)
    "{home}/.virtualenvs",
    "{home}/.pyenv",
    "{home}/__pycache__",
    # Git objects — huge, binary, useless to search
    "{home}/.git",
]


# PathIndex — lightweight in-memory file index

#: 索引的兜底过期线（秒）。
#:
#: 判据不是性能，是正确性：用户往工作区放一个文件，agent 必须看得见。
#: 若索引只在"它是空的"时候建，运行期放进去的文件直到重启都看不见——
#: agent 列出 62 个文件、搜了 4 个模式、每一个都回"没找到"，
#: 而同一刻 shell 用相对路径 `open()` 读得到它：同一个目录，一个看得见一个看不见。
#:
#: 两级刷新：
#:   ① 根目录指纹变了（直接子项增删），立刻重建 —— 每次搜索 1~3 次 syscall
#:   ② 都没变但超过这个秒数，重建一次（兜子目录里的增删，那不会动根的 mtime）
_INDEX_STALE_SECONDS = 300.0


def _root_stamps_of(roots: list) -> list:
    """根目录的 `(路径, st_mtime_ns)` —— 变了就说明有人在这里增删过。"""
    out = []
    for r in roots or []:
        try:
            out.append((str(r), os.stat(r).st_mtime_ns))
        except OSError:
            continue
    return out


class PathIndex:
    """
    Lightweight file-path index.

    Stores only filename, path, extension, size, and modification time.
    Built via `os.scandir()` for speed (3-5× faster than `os.walk`).

    Typical memory: ~50 MB for 1 million files.
    """

    def __init__(self) -> None:
        self._index: dict[str, FileEntry] = {}     # lower-path → FileEntry
        self._by_ext: dict[str, list[str]] = {}    # ext → [lower-paths]
        self._file_count: int = 0
        self._total_size: int = 0
        self._last_built: float = 0.0
        self._roots: list[Path] = []
        #: 根目录指纹（`(路径, st_mtime_ns)`）。它变 = 直接子项增删过
        #: —— 那正是"用户往工作区里放了一个文件"的形状。由
        #: `WorkspaceManager._maybe_refresh_index` 比对；每次搜索只花 1~3 次 syscall。
        self._root_stamps: list = []

    # Build

    def build(self, roots: list[Path],
              forbidden: list[Path] | None = None,
              progress_cb: Callable[[int, int], None] | None = None) -> int:
        """
        Scan *roots* recursively and build the index.

        Skips directories and files under *forbidden* paths.
        Calls *progress_cb(file_count, phase)* periodically if provided.
        Returns total file count indexed.
        """
        self._index.clear()
        self._by_ext.clear()
        self._file_count = 0
        self._total_size = 0
        self._roots = list(roots)
        self._root_stamps = _root_stamps_of(self._roots)

        forbidden = forbidden or []
        forbidden_resolved = set()
        for f in forbidden:
            try:
                forbidden_resolved.add(str(Path(f).resolve()))
            except Exception:
                pass

        for root in roots:
            try:
                self._scan_dir(root, forbidden_resolved, progress_cb)
            except (OSError, PermissionError):
                continue

        self._last_built = time.time()
        return self._file_count

    def _scan_dir(self, root: Path, forbidden: set[str],
                  progress_cb=None) -> None:
        """Recursively scan *root* with os.scandir()."""
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    # 跳过规则只有一处定义（`skip_walk_entry`，模块顶部）——
                    # 三段前缀各自的理由都写在那里，`glob`/`grep` 读同一个函数。
                    if skip_walk_entry(entry.name):
                        continue
                    try:
                        abs_path = Path(entry.path).resolve()
                    except OSError:
                        continue

                    # Check forbidden
                    path_str = str(abs_path)
                    if any(path_str.startswith(f) for f in forbidden):
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        self._scan_dir(abs_path, forbidden, progress_cb)
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            stat = entry.stat()
                        except OSError:
                            continue
                        ext = Path(entry.name).suffix.lower()
                        fe = FileEntry(
                            name=entry.name,
                            path=path_str,
                            ext=ext,
                            size=stat.st_size,
                            modified=local_time_str(stat.st_mtime),
                        )
                        lower_path = path_str.lower()
                        self._index[lower_path] = fe
                        self._by_ext.setdefault(ext, []).append(lower_path)
                        self._file_count += 1
                        self._total_size += stat.st_size

                        if progress_cb and self._file_count % 5000 == 0:
                            try:
                                progress_cb(self._file_count, 0)
                            except Exception:
                                pass
        except (OSError, PermissionError):
            pass

    # Search

    def _rel(self, path: str) -> str:
        """`path` 相对某个索引根的写法（正斜杠）。找不到根，返回原名。

        它让模式能表达目录（`表格1/*`）。相对根、不是绝对路径
        —— 拿绝对路径匹配的话，`*laefvain*` 这类模式会把整个工作区一次捞光。
        """
        low = path.lower()
        for r in self._roots:
            rs = str(r)
            if low.startswith(rs.lower()):
                return path[len(rs):].lstrip("\\/").replace("\\", "/")
        return path.replace("\\", "/")

    def _matches(self, fe: FileEntry, pat: str) -> bool:
        """文件名 ∪ 相对路径 —— 两个都试，任一命中即命中。

        `*` 在 `fnmatch` 里也吃分隔符（与 `glob` 不同），所以
        `*表格1*` 能命中 `表格1/ET.xlsx`、`表格*` 能命中 `表格1/…`。
        这是要的：模型是按"用户说的那个名字"去写的，那名字可能是个目录。
        """
        if fnmatch.fnmatch(fe.name.lower(), pat):
            return True
        return fnmatch.fnmatch(self._rel(fe.path).lower(), pat)

    def search(self, pattern: str = "*",
               extensions: list[str] | None = None,
               max_results: int = 200) -> list[FileEntry]:
        """
        Search the index for files matching *pattern* (fnmatch glob).

        Args:
            pattern: 匹配文件名或它在工作区下的相对路径（`/` 分隔），
                如 `"*.docx"` · "实训*" · "表格1/*"。
                不含通配符时按子串处理（`"表格1"` ≡ `"*表格1*"`），
                与 `_search_user_home` 同一条规矩。
            extensions: optional filter list (e.g. [".pdf", ".docx"])
            max_results: maximum results to return

        必须同时比 `fe.name` 与相对路径：只比文件名时"按目录筛"恒为 0
        （文件名不可能等于 `表格1/*`），而索引里只有文件、没有目录，
        一个目录名在整条链上不可表达。失败链的形状是：
        模型搜 `表格1/*` → 0 · `*表格1*` → 0，扩到跨区 → 0，
        放弃、反问用户，而 `workspace/表格1/ET.xlsx` 就在盘上。
        不是 CJK 的问题（换 `tables1/` 一样是 0）—— 是语义的问题。
        """
        results: list[FileEntry] = []
        ext_set = set(extensions) if extensions else None
        # 分隔符归一 + 无通配符，子串（照 `_search_user_home`）
        # 空串 / `"*"` 不进那条包装 —— `""` 包出来是 `"**"`（= 全匹配，
        #   看着一样但语义是"没给模式"，别让它绕过 `pattern not in ("*","*.*")`
        #   那道快路径判断）。
        pat = (pattern or "").replace("\\", "/").lower()
        if pat and "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        elif not pat:
            pat = "*"

        # If pattern is specific and extensions is set, use ext index
        if ext_set and pattern not in ("*", "*.*"):
            for ext in ext_set:
                paths = self._by_ext.get(ext, [])
                for lp in paths:
                    fe = self._index.get(lp)
                    if fe and self._matches(fe, pat):
                        results.append(fe)
                        if len(results) >= max_results:
                            return results
        else:
            for lp, fe in self._index.items():
                if self._matches(fe, pat):
                    if ext_set and fe.ext not in ext_set:
                        continue
                    results.append(fe)
                    if len(results) >= max_results:
                        break

        return results

    def search_by_name(self, name_hint: str,
                       max_results: int = 50) -> list[FileEntry]:
        """
        Fuzzy filename match — substring search (case-insensitive).
        Useful for vague queries like "实训报告" → matches "个人实训报告.docx".
        """
        results: list[FileEntry] = []
        hint_lower = name_hint.lower()
        for lp, fe in self._index.items():
            if hint_lower in fe.name.lower():
                results.append(fe)
                if len(results) >= max_results:
                    break
        return results

    def get_by_path(self, file_path: str) -> FileEntry | None:
        """Look up a single file by path."""
        return self._index.get(file_path.lower())

    # Incremental update

    def add_file(self, file_path: str) -> None:
        """Add or update a single file entry."""
        p = Path(file_path)
        if not p.is_file():
            return
        try:
            stat = p.stat()
        except OSError:
            return
        fe = FileEntry(
            name=p.name,
            path=str(p.resolve()),
            ext=p.suffix.lower(),
            size=stat.st_size,
            modified=local_time_str(stat.st_mtime),
        )
        lower = fe.path.lower()
        old = self._index.get(lower)
        if old:
            self._total_size -= old.size
            self._file_count -= 1
        self._index[lower] = fe
        self._by_ext.setdefault(fe.ext, []).append(lower)
        self._file_count += 1
        self._total_size += fe.size

    def remove_file(self, file_path: str) -> None:
        """Remove a file from the index."""
        lower = file_path.lower()
        fe = self._index.pop(lower, None)
        if fe:
            self._file_count -= 1
            self._total_size -= fe.size
            ext_list = self._by_ext.get(fe.ext, [])
            if lower in ext_list:
                ext_list.remove(lower)

    # Stats

    @property
    def stats(self) -> dict:
        """Return index statistics."""
        size_mb = round(self._total_size / (1024 * 1024), 2)
        return {
            "file_count": self._file_count,
            "total_size_bytes": self._total_size,
            "total_size_mb": size_mb,
            "last_built": datetime_to_iso(self._last_built) if self._last_built else "",
            "roots": [str(r) for r in self._roots],
        }


# WorkspaceManager

def _cross_zone_action(hint: str, ask_index: int = 1) -> dict:
    """跨区审批的待审批载荷 —— 选项由 `core/approval.py` 那唯一一份表渲染。

    这段文字只许有一份：仓库里出现副本就会不一致（换行、措辞各写各的），
    而改一次措辞要记得改三处，正是这类不一致的来源。现在统一从
    `core.approval.pending_payload` 出。

    *ask_index*（第几次被问起）由调用方从 `self.approval_ask_counts` 里取 ——
    那是个投影：工具没有会话身份，真值在调度器上，由 `agent_loop`
    在工具返回 `pending_approval` 时推进（见 `core/approval.ASK_COUNTED_MARKER`）。
    """
    from core.approval import pending_payload
    return pending_payload(
        "cross_zone", footer=False, ask_index=ask_index,
        file_hint=hint, scope="cross_zone",
    )


def _command_rules_match(rules, command: str) -> bool:
    """`command` 那一格里有没有一条前缀规则命中它。

    延迟 import：`core.command_policy` 是纯函数模块（自己什么都不 import），
    但这条边刻意不在模块顶层建 —— 顶层 import 会绕出循环 import，
    而这一条只在"查命令授权"那条路上走（不是每请求都过）。
    """
    try:
        from core.command_policy import any_rule_matches
        return any_rule_matches(rules, command)
    except Exception:                              # noqa: BLE001
        return False                               # 判不出来，不认（照旧问）


class WorkspaceManager:
    """
    Central registry for work groups, path indexing, and file watching.

    Usage::

        wm = WorkspaceManager(security_guard, settings)
        wm.load_from_settings(settings)
        wm.build_index()

        # Search
        results = wm.search_files("*.docx", scope="workspaces")

        # Add workspace
        wm.add_workspace("D:/projects", "项目代码")

        # Cross-zone notification
        wm.notify_if_cross_zone("实训报告.docx")
    """

    def __init__(self, security_guard, settings: dict | None = None):
        self._security = security_guard
        self._workspaces: dict[str, WorkGroup] = {}
        self._primary_id: str = ""
        self._path_index = PathIndex()
        self._cross_zone_enabled: bool = False
        self._auto_approve: bool = False
        self._one_time_approval: bool = False  # v2: one-shot approval for current request
        #: 这次一次性授权的发起者（会话 key）。与下面投影进来的
        #: `_approval_scope` 比对 —— 读者在 skill 里拿不到会话身份，所以由 wm
        #: 自己比。见 `has_approval()`。
        self._one_time_approval_owner: str = ""
        # 跨区审批的三个投影字段
        # 与 `_skip_delete_approval` 同一条规矩：这是投影，不是真值。
        # 真值在 `PipelineScheduler`（按会话），每轮开局由
        # `_project_session_flags()` 刷新一次。之所以要有它们：唯一的读者在
        # skill handler 里，拿不到会话身份。
        #
        # 「本次对话内自动放行」绝不能塞进 `_auto_approve` ——
        # 那个字段会被 `to_settings_dict()` 导出进 `settings.json`，等于把
        # 一次会话授权变成持久用户设置。
        self._cross_zone_session_auto: bool = False
        #: `{kind: count}` —— 按审批种类分（不是单个 int）。
        self._approval_ask_count: dict[str, int] = {}
        self._approval_scope: str = ""    # 本轮是哪个会话在问
        #: 自动模式"为什么没替你答"（可观测性）。
        #: 每轮由 `agent_loop._auto_review_gate` 先清后写；写它的唯一时机是
        #: "闸决定退回问人"那两支（连续/累计熔断 · 判了安全却不知道怎么放行）。
        #: 读者：审批载荷的 `body` —— 用户与事后的我们自己都看得见。
        self._auto_gate_note: str = ""
        #: 级联的两个总闸。单一来源 = `settings.json`。
        #:   以前它们只有设置页在写，而 `to_settings_dict()` 又用监听器推导着
        #:   覆盖，用户关掉、框架一跑又显示成开（「标签在说谎」）。
        #:   默认 `True` = 今天的行为一个字节不变（没人配置过就是照旧跑）。
        self._cascade_enabled: bool = True     # 「级联动态」总开关
        self._cascade_auto_start: bool = True  # 「自启动监听」
        self._watchers: dict[str, WatcherConfig] = {}
        self._watcher_instances: dict[str, object] = {}
        #: 上一次启动失败的原因（watcher_id -> 一句人话）。
        #: 读者是 `bus/core.py` 起完监听之后那一格：它要把原因原样交给
        #: 事故账本，而不是自己再编一句"监听服务启动失败" —— 用户看到的
        #: 必须是"哪个目录不存在"，否则他没法动手。
        self._watcher_errors: dict[str, str] = {}
        self._sync_event_queue: list[dict] = []       # v2: multi-event buffer
        self._last_polled_event_index: int = 0        # v2: queue position marker
        # session-level delete-approval skip. This is a PROJECTION,
        # not the truth: the truth is the per-session set in
        # `PipelineScheduler._session_skip_delete_approval`, refreshed onto
        # this flag once per turn by `_project_delete_skip()` — because the
        # only reader (file_parser's delete handler) has no session identity.
        # Writing it from anywhere else makes it process-wide by accident.
        self._skip_delete_approval: bool = False
        #: 删除的一次性授权不在这里 ——
        #: 它收进了下面那张按目标的表（`kind="delete"`，subject = 参数摘要）。
        #: 一个布尔叫 `one_time` 时，实际语义是「本轮内所有删除全部免问」，
        #: 那等于两套删除口径（布尔 + 按目标）= 两套真相源。
        #: 下面的批量闸一次性授权是另一件事（它数的是"几个"，
        #:   不是"删哪些"），单独一格。
        #: 批量闸那一次的一次性授权。
        #: 与普通那次的授权分开存，而且只有批量那次的结算器会写它
        #: （`pre_process._settle_delete_bulk`）—— 自动模式的分类器预授权写的是
        #: 按目标那张表，写不到这里，「分类器挡不住批量闸」这条判据才立得住。
        #: 它同时是重放的通行证：批准之后拿同样的参数重放那次
        #: `delete_files`，而 `len(valid) > limit` 照样成立，没有这一格会
        #: 无限审批循环。
        #: 它仍是布尔是有意的：过了它之后还有普通那道闸
        #: （按目标、要 subject 命中），单靠它删不掉第二个不同的目标。
        self._one_time_bulk_delete_approval: bool = False
        self._one_time_bulk_delete_approval_owner: str = ""
        #: 按目标的授权（域名 / 收件人 / 命令 / 删除）。
        #: `_target_once` 是轮内一次性（消费型、带属主），
        #: `_target_session` 是投影（每轮开局由调度器恢复）。
        #: 详见下面 `grant_target_once` 那一段的注释。
        self._target_once: dict[tuple[str, str], str] = {}
        self._target_session: dict[str, set[str]] = {}
        #: 白名单文件里那个裸 `*` 落在哪几个 kind 上。
        #: 判据是「它来自哪一格」而不是「值是不是 `*`」——
        #: 同一个 `*` 值有两个来源，而它们走的不是同一条路：
        #:   · 文件里的（设置页那三个「全部放行」开关），要过分类器；
        #:   · 用户刚批的（选项 5 / 那一轮的一次性授权），照旧短路。
        #: 由调度器的投影每轮整组替换（同 `_target_session` 一条规矩）。
        self._target_wildcard: set[str] = set()
        #: 命令执行的工作目录 —— 与上面同一族的投影，
        #: 真值在调度器上。见 `session_cwd` 那条 property 的注释。
        self._session_cwd: str = ""
        #: 本轮用户消息正文 —— 与上面同一族的投影，
        #: 真值在 `PipelineScheduler._project_session_flags(session_key, user_text)`。
        #: 唯一的读者是 `core/egress_gate.py::is_granted` 的第 ④ 半：
        #: 「这个域名用户点过名吗」（只允许 Web Fetch 访问用户以某种形式
        #: 提供过的域名）。
        #: 它每轮都变 —— 就是本轮那一句，不是整段对话的合集
        #: （免问的范围只到本轮）。
        #: 名字不带下划线是有意的：投影那一侧是 `setattr(wm, name, value)`，
        #: 而读者在工具 handler 里、拿不到 `ctx`，两边都直接碰这个公开字段。
        self.turn_user_message: str = ""
        #: 上面这一族投影字段的归属章 —— 每轮开局由调度器的
        #: `_project_session_flags()` 盖成那个会话的键。全局串行年代
        #: "轮到谁就是谁的"自动成立；每会话一把锁之后两个会话会交错投影到
        #: 同一组进程级字段上，读侧要问一句"这份投影是我的吗"
        #: （见 `_projection_is_mine`）。空章 = 还没投影过
        #: （老路径 / 测试直调），一律放行，不改单会话下的任何语义。
        self._projected_for: str = ""
        #: 工作目录那一格单有自己的写入章：写手是工具（shell），
        #: 它在轮中写、调度器轮末收回 —— 收回时也要验"这格是写给我的吗"。
        self._cwd_written_by: str = ""
        self._settings_generation: int = 0  # incremented on each load_from_settings
        # v1.2.0: 首次载入（框架启动）只建立基准，不算"设置变更"。
        # 否则每次重启后的第一条消息都会带上一条"工作区已更新"——
        # 而那是进程重启，不是用户改了设置。
        self._settings_baseline_set: bool = False
        self._default_workspace_path: str = ""  # from settings["workspace"], set by load_from_settings
        #: 「默认工作区不存在」只喊一次（`load_from_settings` 每次存设置都会跑）。
        self._warned_missing_ws: bool = False

        if settings:
            self.load_from_settings(settings)

    # Settings load / save

    def load_from_settings(self, settings: dict) -> None:
        """Load workspace configuration from settings dict."""
        #: 缺键的兜底 = true，与出厂基准、随包 `config.yaml`
        #: （`security.cross_zone.enabled`）和 `GET /api/settings` 的 setdefault
        #: 四处同值。它是能力开关：真正的闸是审批
        #: （`auto_approve` 默认 false，区外搜索仍然会问）。
        self._cross_zone_enabled = settings.get("cross_zone_enabled", True)
        self._auto_approve = settings.get("auto_approve", False)

        # 级联的两个总闸
        # 判据：存的就是生效的（单一来源）。以前这两格没人读，
        # 而 `to_settings_dict()` 用监听器推导着把它们盖回去，关不掉。
        _was_enabled = self._cascade_enabled
        self._cascade_enabled = bool(settings.get("cascade_enabled", True))
        self._cascade_auto_start = bool(settings.get("cascade_auto_start", True))
        if not self._cascade_enabled:
            # 关掉，真的停（不是只把开关画成灰的）。
            for _wid in list(self._watcher_instances):
                self.stop_watcher(_wid)
        elif not _was_enabled:
            #: 故意不在这里拉起监听器（`test_watcher_autostart` §6 的判据）：
            #:   `load_from_settings` 是每次存设置都会跑的路，挂上去等于
            #:   "改个别的设置顺手重启监听器"，而 `snapshot_mode="boot"` 的语义
            #:   是"每次框架启动" —— 在这里重快照就是把它变成"随机时刻"。
            #: 只如实告声，把"怎么让它生效"说给用户（别让他以为按了没用）。
            print("[workspace] 「级联动态」从关到开 —— 监听器会在**下次启动框架**时"
                  "按「自启动监听」拉起（也可以现在点设置页的「重启框架」）。")

        # Default workspace path — set via onboarding or security settings.
        # When present, this becomes the fallback for primary_path.
        ws_path = settings.get("workspace", "")
        if ws_path:
            resolved = str(Path(ws_path).expanduser().resolve())
            if Path(resolved).is_dir():
                self._default_workspace_path = resolved
            else:
                # 配的那个目录不在了（盘没插 / 被删 / 从旧机器带过来的路径）——
                #   不拿它当默认落点：写进去会把一个用户已经删掉的目录造回来。
                #   退回「默认工作区」那一条，并说一次，别让用户找不到原因。
                self._default_workspace_path = ""
                if not self._warned_missing_ws:
                    self._warned_missing_ws = True
                    print("[workspace] !! 默认工作区不存在：%s —— 请到设置页重新选一个"
                          % resolved)
        # Sync to security guard
        self._security.cross_zone_enabled = self._cross_zone_enabled
        self._security.auto_approve = self._auto_approve
        # 「关闭安全询问」开关（危险档降级）。在这里同步是刻意的 ——
        # `web/server.py` 的 `POST /api/settings` 结尾会调本函数，所以设置页
        # 一改就当场生效，不必重启（与上面两条同一条路）。
        self._security.relax_command_tier = bool(
            settings.get("relax_command_tier", False))
        # 「只读命令免问」（设置页）。与上面那条同一条路 ——
        # `POST /api/settings` 结尾会调本函数，设置页一改就当场生效。
        self._security.no_ask_readonly = bool(
            settings.get("no_ask_readonly", False))

        # Forbidden dirs
        #: 读不到键 = 从没配过，用默认那一批；配过就照用户那份，空表也照。
        #:   空表不算"没配"—— 用户把最后一条删掉时给的就是空表，
        #:   按"非空才设"判断会让内存里继续留着已经删掉的那些。
        forbidden = settings.get("forbidden_directories")
        if forbidden is None:
            forbidden = resolve_default_excludes()
        self._security.forbidden_dirs = list(forbidden)

        # 「工作组」开关已退场，工作区列表恒生效
        #
        # 判据不是"少一个开关"，而是这一格的语义已经没有了：
        #   · 开关 ON 那半的语义（"允许 agent 跨工作区执行操作"）归跨区那一条
        #     （`_auto_mode` / 跨区审批那一族），与"加载不加载工作区列表"无关；
        #   · 开关 OFF 那半干的是"清空 `_workspaces` 并回退默认"—— 而工作区列表
        #     是"默认视野"的来源（`lookup_roots()` / 跨区边界），
        #     关掉它等于把用户配过的目录从视野里删掉，而那只该由用户逐条删除控制。
        # 现在永远加载，与开关打开着的那一天逐字节同形。
        #
        # 旧的 `work_groups_enabled` 键读到了要出声：
        #   静默忽略一个用户改过的键比不许改更坏。
        #   出声一次就够（值不在就什么都不说）。
        if "work_groups_enabled" in settings:
            print("[workspace] settings.json 的 work_groups_enabled **不再生效** —— "
                  "工作区列表现在**恒生效**。"
                  "要去掉某个工作区，请在设置页里删掉它。")
        # 判据是 `"work_groups" in settings`，不是"列表非空" ——
        #   用户把最后一个工作区删掉时给的是一个空列表，而按"列表非空"判断
        #   会把已经删掉的那些原样留在内存里
        #   （症状：删了、界面没了、`lookup_roots()` 还找得到）。
        #   键不在，一个字节不动（别的调用方可能只传一部分设置）。
        if "work_groups" in settings:
            self._workspaces.clear()
            self._primary_id = ""
            for ws_data in (settings.get("work_groups") or []):
                wg = WorkGroup(
                    id=ws_data.get("id", uuid.uuid4().hex[:8]),
                    name=ws_data.get("name", ""),
                    path=ws_data.get("path", ""),
                    permissions=set(ws_data.get("permissions",
                        ["copy", "delete", "move", "rename", "write"])),
                    primary=ws_data.get("primary", False),
                )
                self._workspaces[wg.id] = wg
                if wg.primary:
                    self._primary_id = wg.id

        # Watchers
        watcher_list = settings.get("watchers", [])
        if watcher_list:
            self._watchers.clear()
            for w_data in watcher_list:
                wc = WatcherConfig(
                    id=w_data.get("id", uuid.uuid4().hex[:8]),
                    watch_path=w_data.get("watch_path", ""),
                    target_workspace_id=w_data.get("target_workspace_id", ""),
                    sync_documents=w_data.get("sync_documents", True),
                    sync_images=w_data.get("sync_images", False),
                    sync_media=w_data.get("sync_media", False),
                    sync_archives=w_data.get("sync_archives", False),
                    custom_extensions=w_data.get("custom_extensions", []),
                    notify_on_sync=w_data.get("notify_on_sync", True),
                    auto_start=w_data.get("auto_start", True),
                    enabled=w_data.get("enabled", False),
                    exclude_subdirs=w_data.get("exclude_subdirs", []),
                    snapshot_mode=w_data.get("snapshot_mode", "boot"),
                )
                self._watchers[wc.id] = wc

        # Reload security guard with current workspace paths
        self._reload_security_workspaces()
        # Rebuild the path index so searches reflect the new workspace set
        self.build_index()
        # Bump generation so PreProcessStage can inject a context note.
        # v1.2.0: 首次载入建立基准，不计数（见 __init__ 的注释）。
        if self._settings_baseline_set:
            self._settings_generation += 1
        else:
            self._settings_baseline_set = True

    def to_settings_dict(self) -> dict:
        """Export workspace configuration for saving to settings.json.

        IMPORTANT: only exports keys that reflect the *live* state.
        Keys that are user preferences (the saved work_groups list) are NOT
        exported — they live in settings.json and this projection must not
        overwrite them. `work_groups_enabled` is no longer exported: the
        switch is gone and nobody reads that key.

        `cascade_notify` 不在这张表里 —— 它若由
        `any(w.notify_on_sync …)` 推出来，就会与设置页那个开关的语义冲突：
        开关的标签是「是否启用通知 — 捕获文件时在页面右上角显示横幅通知」，
        那是用户的显示偏好，与"某个监听器要不要回调"是两件事。
        两个写入者抢同一个键，后果是用户把它关掉、一刷新又弹回来。
它是单一来源（`settings.json` 里那一格，只有设置页写它）。
        `cascade_enabled` / `cascade_auto_start` 已处置：
        它们以前也是"UI 直接写、这里又推导着覆盖"的形状 —— 后果是关不掉
        （用户关掉、框架一跑，`GET` 又推成 `len(watchers) > 0`）。
        现在是单一来源（`settings.json` 那一格），这里不再导出；
        "监听器到底在不在跑"由 `get_watcher_status()`（`/api/watchers/status`）说 ——
        那是读数，不是设置。
        """
        result: dict = {
            "cross_zone_enabled": self._cross_zone_enabled,
            "auto_approve": self._auto_approve,
        }
        # Only include work_groups when workspaces are actually loaded.
        # Otherwise leave the saved list alone.
        if self._workspaces:
            result["work_groups"] = [
                {
                    "id": wg.id,
                    "name": wg.name,
                    "path": wg.path,
                    "permissions": sorted(wg.permissions),
                    "primary": wg.primary,
                }
                for wg in self._workspaces.values()
            ]
        result["watchers"] = [
                {
                    "id": wc.id,
                    "watch_path": wc.watch_path,
                    "target_workspace_id": wc.target_workspace_id,
                    "sync_documents": wc.sync_documents,
                    "sync_images": wc.sync_images,
                    "sync_media": wc.sync_media,
                    "sync_archives": wc.sync_archives,
                    "custom_extensions": wc.custom_extensions,
                    "notify_on_sync": wc.notify_on_sync,
                    "auto_start": wc.auto_start,
                    "enabled": wc.enabled,
                    "exclude_subdirs": wc.exclude_subdirs,
                    "snapshot_mode": wc.snapshot_mode,
                }
                for wc in self._watchers.values()
            ]
        return result

    def _reload_security_workspaces(self) -> None:
        """Push current workspace list + permissions to SecurityGuard.

        必须同时包含默认工作区。

        `SecurityGuard.reload_workspaces()` 的语义是整体替换（不是追加）——
        只推工作组的话，只要用户配了任意一个工作组，默认工作区
        （`settings["workspace"]`，即 `./workspace`）就会被从允许目录里挤掉。

        症状：agent 能列出工作区文件（走的是 PathIndex / 跨区那条路），却打不开
        其中任何一个 —— `parse_*` / `search_chunks` / `summarize_document`
        全部报 SecurityError: Access denied。而工作区本来就是它干活的地方。

        复现条件：`settings.json` 里只有一个指向桌面的工作组 →
        安全核允许目录只剩桌面 → 工作区内文件全部被拒。
        """
        seen: set[str] = set()
        ws_data: list[dict] = []

        def _add(path: str, perms: list[str]) -> None:
            if not path:
                return
            try:
                key = str(Path(path).resolve())
            except Exception:
                return
            if key in seen:          # 工作组恰好就是默认工作区时不重复添加
                return
            seen.add(key)
            ws_data.append({"path": path, "permissions": perms})

        # 默认工作区在前 —— 它是 agent 干活时的默认落点。
        # 权限用与 SecurityGuard 初始允许目录一致的默认集（见 security.py:166）。
        #
        # 读 `primary_path` 而不是只读 `_default_workspace_path`：全新安装里
        #   settings 还没有这一格（onboarding 之前是空串），而 `primary_path`
        #   会回落到 `paths.default_workspace_dir()` —— 那才是 agent 真正要写的
        #   地方。只登记那个空串，全新安装的第一次写入就是
        #   "outside every workspace — writing there needs one approval"，
        #   而用户根本没配过任何与它冲突的东西。
        # 这里不替用户建目录：用户自己配的那条可能只是盘没插 / 目录被删，
        #   造回来是在替用户做决定；真的写入时由 `write_file` 自己建父目录。
        _add(self.primary_path, ["copy", "delete", "move", "rename", "write"])
        for wg in self._workspaces.values():
            _add(wg.path, list(wg.permissions))

        self._security.reload_workspaces(ws_data)

    # Work group CRUD

    @property
    def workspaces(self) -> list[WorkGroup]:
        return list(self._workspaces.values())

    @property
    def primary(self) -> WorkGroup | None:
        """Return the explicitly-set primary work group, or None.
        Does NOT fall back to the first work group — that would
        silently redirect file deliveries away from the default
        workspace.  Callers should use `primary_path` instead."""
        if self._primary_id and self._primary_id in self._workspaces:
            return self._workspaces[self._primary_id]
        return None

    @property
    def primary_path(self) -> str:
        """Return the path where files should land by default.
        Priority: explicitly-primary work group → default workspace dir."""
        p = self.primary
        if p:
            return p.path
        # Fall back to the user-configured default workspace (from onboarding
        # or settings), then to "./workspace" relative to CWD as last resort.
        if self._default_workspace_path:
            return self._default_workspace_path
        # 最后这层回退只在一处（`core/paths.default_workspace_dir`）——
        #   安装版给的是「文档\Crannia\workspace」，不是安装目录也不是数据根。
        from core import paths as _paths
        return str(_paths.default_workspace_dir().resolve())

    def add_workspace(self, path: str, name: str = "",
                      permissions: list[str] | None = None,
                      primary: bool = False) -> WorkGroup:
        """Add a new workspace directory."""
        resolved = str(Path(path).resolve())
        wg = WorkGroup(
            id=uuid.uuid4().hex[:8],
            name=name or Path(resolved).name,
            path=resolved,
            permissions=set(permissions or ["copy", "delete", "move", "rename", "write"]),
            primary=primary,
        )
        if primary:
            for ws in self._workspaces.values():
                ws.primary = False
            self._primary_id = wg.id
        self._workspaces[wg.id] = wg
        self._reload_security_workspaces()
        return wg

    def update_workspace(self, ws_id: str,
                         name: str | None = None,
                         permissions: list[str] | None = None) -> bool:
        """Update workspace name and/or permissions."""
        wg = self._workspaces.get(ws_id)
        if not wg:
            return False
        if name is not None:
            wg.name = name
        if permissions is not None:
            wg.permissions = set(permissions)
        self._reload_security_workspaces()
        return True

    def remove_workspace(self, ws_id: str) -> bool:
        """Remove a workspace (does NOT delete files)."""
        if ws_id not in self._workspaces:
            return False
        del self._workspaces[ws_id]
        if self._primary_id == ws_id:
            self._primary_id = next(iter(self._workspaces), "")
            if self._primary_id:
                self._workspaces[self._primary_id].primary = True
        self._reload_security_workspaces()
        # Rebuild index so stale entries from removed workspace are purged
        self.build_index()
        return True

    def set_primary(self, ws_id: str) -> bool:
        """Set a workspace as primary."""
        if ws_id not in self._workspaces:
            return False
        for wg in self._workspaces.values():
            wg.primary = False
        self._workspaces[ws_id].primary = True
        self._primary_id = ws_id
        return True

    # Cross-zone settings

    @property
    def cross_zone_enabled(self) -> bool:
        return self._cross_zone_enabled

    @cross_zone_enabled.setter
    def cross_zone_enabled(self, value: bool) -> None:
        self._cross_zone_enabled = value
        self._security.cross_zone_enabled = value

    @property
    def auto_approve(self) -> bool:
        return self._auto_approve

    @auto_approve.setter
    def auto_approve(self, value: bool) -> None:
        self._auto_approve = value
        self._security.auto_approve = value

    def grant_one_time_approval(self, owner: str = "") -> None:
        """Grant a one-shot cross-zone approval for the current turn.

        *owner* 是发起这次授权的会话 key。它必须与投影进来的
        `approval_scope` 对得上，授权才算数 —— 因为读者在 skill 里拿不到会话
        身份，只能由 wm 自己比。见 `has_approval()`。
        """
        self._one_time_approval = True
        self._one_time_approval_owner = owner

    def has_approval(self) -> bool:
        """这次一次性授权当前会话能不能用（非消费型）。

        不只看标志 —— 还要属主对得上。只查标志的话它是一个进程级布尔：
        A 会话拿到的授权，在 B 会话里也生效（窗口 = A 那一轮结束之前）。
        对不上，返回 False，退化成"再问一次"（失败方向安全）。
        """
        return (self._one_time_approval
                and self._one_time_approval_owner == self._approval_scope)

    def take_cross_zone_grant(self) -> bool:
        """取用一次性跨区授权 —— 消费型：取到就置回 False。

        与 `has_approval()` 只差"取即减"，但那是"一次性"这三个字的全部
        含义。只有非消费型的 `has_approval()` 时，名字叫一次性、
        行为却是本轮无限次。

        与之相对，`auto_approve`（持久设置）和 `cross_zone_session_auto`
        （本会话）不消耗 —— 它们是"以后都不用问"，不是"这一次放行"。
        那个顺序写在下游（file_parser 的 `_cross_zone_allowed`），不写在这里。
        """
        if self.has_approval():
            self._one_time_approval = False
            self._one_time_approval_owner = ""
            return True
        return False

    def reset_one_time_approval(self) -> None:
        """Reset approval at turn boundary."""
        self._one_time_approval = False
        self._one_time_approval_owner = ""

    def clear_approval_projection(self) -> None:
        """把投影字段全部清成安全值（轮末调；轮首由投影写回真值）。

        这一步是"投影失败方向安全"的唯一兑现方式。
        只靠"轮首写真实值"是不够的 —— 轮首那次投影自己可能失败
        （拿不到 registry / wm），失败时字段会保持上一轮的值，于是 Bob 的回合
        直接继承 Alice 的授权。先清、再写，失败就退化成"全都关着"
        （= 再问一次），方向才是安全的。
        """
        self._skip_delete_approval = False
        self._cross_zone_session_auto = False
        self._approval_ask_count = {}
        self._approval_scope = ""
        # 命令的工作目录也是投影（真值在调度器按会话存）。
        # 失败方向是安全的：回退到工作区，而不是上一条命令待过的目录。
        self._session_cwd = ""
        # 本轮用户消息同理 —— 清成空串 = "这一轮没有点名任何域名"
        # 所以出网那一闸退回"照问"（安全侧）。不清的话，上一轮点过的域名
        #   会跨轮继续免问 —— 而免问范围只到本轮。
        self.turn_user_message = ""
        # 按目标的投影那一半也在清之列。
        # `_target_once`（轮内一次性）不在这里 —— 它由 `reset_target_grants()`
        # 清，两个都在调度器的 `finally` 里调。分开是因为语义不同：
        # 这个方法是"投影字段清成安全值"，那个是"一次性授权作废"。
        self._target_session.clear()
        self._target_wildcard.clear()

    def _projection_is_mine(self) -> bool:
        """这份投影是当前工具调用那个会话投的吗。

        空值一律放行（两个方向）：没盖过章、或拿不到快照的退化路径
        （测试直调 / 设置页 / 后台线程）走的是单会话行为 —— 这条检查只为
        并发窗口而设，不为改变单会话下的任何语义。
        """
        # getattr：测试用 `__new__` 构 wm（绕过 __init__），字段可能不在
        mine = getattr(self, "_projected_for", "") or ""
        if not mine:
            return True
        try:
            from core.request_snapshot import current_session_id
            cur = current_session_id()
        except Exception:
            cur = ""
        if not cur:
            return True
        return cur == mine

    @property
    def cross_zone_session_auto(self) -> bool:
        """本会话「不再询问跨区搜索」——投影，见 `__init__` 的注释。"""
        if not self._projection_is_mine():
            return False          # 不是我的投影，安全值（照问）
        return self._cross_zone_session_auto

    @cross_zone_session_auto.setter
    def cross_zone_session_auto(self, value: bool) -> None:
        self._cross_zone_session_auto = value

    @property
    def approval_ask_counts(self) -> dict:
        """本会话每个审批种类被问过几次 —— 投影，用来决定要不要出升级项。

        形状是 `{kind: count}`（不是单个 int）。读的人必须按 kind 取，
        见 `_cross_zone_action` 的调用点。
        """
        return dict(self._approval_ask_count)

    @approval_ask_counts.setter
    def approval_ask_counts(self, value) -> None:
        self._approval_ask_count = {
            str(k): int(v or 0) for k, v in (value or {}).items()}

    @property
    def approval_scope(self) -> str:
        """本轮是哪个会话在问 —— 投影，给无会话身份的读者补上身份。"""
        return self._approval_scope

    @approval_scope.setter
    def approval_scope(self, value: str) -> None:
        self._approval_scope = value or ""

    @property
    def auto_gate_note(self) -> str:
        """自动模式为什么没替你答（空串 = 它没打算答，或者它答了）。

        判据："没发生"必须看得见。
        那两条"退回问人"的分支若只 `print` 到 stdout，而 stdout 在桌面壳里
        没人收（`main.cjs` 只转发渲染进程那三条），事后就一条线索都没有：
        看审批弹窗时看不出是谁在问、为什么问，只能靠猜。
落一格到 wm 上，由审批载荷的 `body` 带出来（用户看得见、库里有记录）。

        与 `approval_scope` 同族：它是每一轮的投影，不是设置 ——
        `to_settings_dict()` 不许带它出去（那是把一次会话内的诊断变成持久配置）。
        """
        try:
            return self._auto_gate_note or ""
        except AttributeError:      # 测试用 `__new__` 构 wm，字段可能不在
            return ""

    @auto_gate_note.setter
    def auto_gate_note(self, value) -> None:
        self._auto_gate_note = str(value or "")

    @property
    def skip_delete_approval(self) -> bool:
        if not self._projection_is_mine():
            return False          # 不是我的投影，安全值（照问）
        return self._skip_delete_approval

    @skip_delete_approval.setter
    def skip_delete_approval(self, value: bool) -> None:
        self._skip_delete_approval = value

    def grant_one_time_delete_approval(self, owner: str = "",
                                       subject: str = "") -> None:
        """本次一次性放行这一次删除（本轮内有效）。

        它按目标 —— *subject* 是这次调用的参数摘要
        （`core.approval.delete_subject`）。若只用一个布尔，
        名字叫 `one_time`、实际是「本轮内所有删除全部免问」——
        批了第 1 个，第 2 个连浮窗都不弹（自动模式连分类器都不过）。

        不新开一套存储 —— 直接落到 `_target_once` 那张按目标的表上
        （`run_command` / 出网 在用的同一张）。判据：
        不要再往里加第三套（见下面「按目标的授权」那一段）。

        *owner* 是发起这次授权的会话 key（属主规矩，
        由 `has_target_grant` 一并执行）。
        """
        self.grant_target_once("delete", str(subject or ""), owner)

    def has_delete_approval(self, subject: str = "") -> bool:
        """这一次删除当前会话放不放行（本轮一次性 · 按目标 · 非消费型）。

        与跨区那份（`has_approval()`）同一条规矩：不只看有没有，还要属主
        对得上 —— 属主那一半由 `has_target_grant` 执行，
        本方法只是删除那一支的门面。

        *subject* 必须是这次的参数摘要；调用方要保证它非空
        （空 = 取不到目标 = 放行不了，fail-closed）。传空是能查的
        （`("delete","")` 这一格谁也不会去写），但那样就等于退回无目标的放行。
        """
        return self.has_target_grant("delete", str(subject or ""))

    def grant_one_time_bulk_delete_approval(self, owner: str = "") -> None:
        """人刚批过那一次批量删除。与上面那份同一套写法。

        只有 `_settle_delete_bulk` 可以调它 —— 那是"用户点了同意"唯一
        会走的结算器。自动模式的分类器走 `_pre_grant_for_auto`，它写的是
        `grant_one_time_delete_approval`，写不到这一格，批量闸挡得住分类器。
        """
        self._one_time_bulk_delete_approval = True
        self._one_time_bulk_delete_approval_owner = owner

    def has_bulk_delete_approval(self) -> bool:
        """批量那份一次性授权当前会话能不能用（非消费型）。

        与 `has_delete_approval()` 逐字同一条规矩：不只看标志，还要属主对得上。
        """
        # 权限解禁（完全权限 + 设置开关），批量删除那道熔断也不问。
        #   判据是上下文（每轮由 `AgentLoopStage` 钉），不看设置文件。
        #   这一条在 `_projection_is_mine` 之前：解禁是"这一轮的属性"，
        #     与"授权是谁批的"无关。
        try:
            from core import unlock
            if unlock.bypassing():
                return True
        except Exception:                                   # noqa: BLE001
            pass
        if not self._projection_is_mine():
            return False          # 授权串会话不比串不了更糟 —— 安全值
        return (self._one_time_bulk_delete_approval
                and self._one_time_bulk_delete_approval_owner
                == self._approval_scope)

    def reset_delete_approval(self) -> None:
        """Reset delete approvals at turn boundary."""
        # 普通那次的一次性授权住在 `_target_once` 里（`kind="delete"`）
        # 这里顺手把它也清掉。
        #   只清 `"delete"` 那些键 —— `_target_once` 还装着 `command` /
        #   `destructive`，它们各有各的轮末语义，不许被这一句连坐。
        #   不在 `reset_target_grants()` 一处清就够：那个函数是
        #   "全部一次性"的口，而这个函数的名字承诺的是"删除的授权没了"。
        #   两个性质都要成立，两处都清（幂等，不冲突）。
        #   反过来说：任何"只调一个 reset"的新调用点都不会漏掉删除这条。
        for _k in [k for k in self._target_once if k[0] == "delete"]:
            self._target_once.pop(_k, None)
        # 批量那份是同一条规矩（只活一轮）—— 清在这里，
        #   不另开一个 reset 函数。理由：`reset_delete_approval` 的调用点
        #   在 `scheduler` 的 `finally` 里、有断言专门钉着，
        #   多开一个函数 = 多一个会被漏掉的调用点。
        self._one_time_bulk_delete_approval = False
        self._one_time_bulk_delete_approval_owner = ""
        # `_skip_delete_approval` is deliberately NOT touched here: it is
        # session-scoped and re-projected at the START of every turn from the
        # scheduler's per-session set. Treating it as a plain turn-boundary
        # flag would make it process-wide by accident.

    # 按目标的授权（出网 / 执行 / 删除 / 区外写）
    #
    # 为什么不复用上面那两套：删除与跨区的授权是布尔的
    # （"本次会话内不再问删除" —— 整类放行）。出网与执行的被审批物是目标
    # （哪个域名 / 哪条命令），一个布尔表达不了。
    #
    # 也不给每个域各写一套：它们同形，只是 `kind` 不同。
    # 所以这里是一套按 `kind` 参数化的存储 —— 上面那两套布尔型不动，
    # 但不要再往里加第三套。
    #
    # 两半，与删除那侧同一条规矩：
    #   · `_target_once`   —— 一次性、消费型、带属主，轮末被 `reset` 清掉。
    #                         它只负责"用户刚批过，这一轮内重新提交要能过"。
    #   · `_target_session` —— 投影，每轮开局由调度器从会话真值恢复。
    #                         它是"本次对话内不再问这个目标"。
    #
    # `"*"` 是通配：选项 5「本次对话内全部自动放行」就是把 `"*"`
    # 放进会话集合。不另设一个"自动放行"标志 —— 那会变成第三套机制。

    def grant_target_once(self, kind: str, subject: str, owner: str = "") -> None:
        """本次一次性放行 *subject*（本轮内有效，消费型）。

        本轮内"用户刚批过"全靠它 —— 会话集合那一半是投影，本轮开局
        就投影完了，不含刚刚新增的这个目标。所以结算时两处都要写：
        这里写 wm（本轮的工具看得到），`ctx.session_target_grants` 写载体
        （调度器轮末落盘、轮首再投影回来）。
        """
        self._target_once[(str(kind), str(subject))] = str(owner or "")

    def set_target_session(self, kind: str, subjects) -> None:
        """整组替换这个 kind 的会话级集合 —— 调度的投影入口。

        是 `set`（替换）不是 `update`（合并）：投影必须能收回权限
        （用户删掉白名单条目、`/clear`、换会话）。合并式的投影只能加不能减。
        """
        self._target_session[str(kind)] = set(subjects or ())

    def set_target_wildcard(self, kind: str, present: bool) -> None:
        """`kind` 这一组里文件来源的那个裸 `*` 在不在。

        与 `set_target_session` 成对调用（投影那一段），也是整组替换的语义
        —— 用户把白名单里那行 `*` 删掉，下一轮它就不再是"开关命中"。
        """
        k = str(kind)
        if present:
            self._target_wildcard.add(k)
        else:
            self._target_wildcard.discard(k)

    def matched_target_grant(self, kind: str, subject: str, *,
                             prefix: bool = False) -> str:
        """命中的是具体那一条还是开关那个裸 `*`。

        返回 `"specific"` / `"wildcard"` / `""`（没命中）。

        为什么不复用 `has_target_grant` 那一个布尔：这两者必须
        分开走 ——
          · 用户亲手加的那一条 / 他刚批过的那个对象，照旧短路；
          · 裸 `*`（设置页那个「全部放行」开关），要过分类器
            （打开开关是交给分类器判，不是真的全放）。
        同一个布尔两者都答"命中了"，表达不了这个分叉。

        具体条目优先：`s in allowed` 命中就回 `specific` —— 哪怕那一组里
        同时也躺着 `*`。用户明确点名的那一条，不该被"顺便"降级去排队。
        `_target_once` 里的 `*`（选项 5 当场写的那一格）算 `specific`：
        那是用户刚刚在这个会话里批的，不是设置页那个常开的开关。
        """
        if not self._projection_is_mine():
            return ""
        k, s = str(kind), str(subject)
        if self._target_once.get((k, s), None) == self._approval_scope:
            return "specific"
        allowed = self._target_session.get(k) or set()
        if s in allowed:
            return "specific"
        # `command` 那一格还有前缀规则（形状 `npm run *`）。
        #    `prefix` 由调用方给 —— `core/shell.py` 只在 `TIER_ALLOW` 时传
        #    `True`：`guarded` 档（`git` / 提到受保护路径）越不过安全路径那一档，
        #    判定序上 `allow` 规则排在路径约束之后。
        if prefix and k == "command":
            if _command_rules_match(allowed, s):
                return "specific"
        if k in getattr(self, "_target_wildcard", set()):
            return "wildcard"
        if "*" in allowed:
            return "specific"
        if self._target_once.get((k, "*"), None) == self._approval_scope:
            return "specific"
        return ""

    def has_target_grant(self, kind: str, subject: str, *,
                         prefix: bool = False) -> bool:
        """这一轮能不能放行 *subject* —— 会话级 ∪ 一次性。

        与删除那侧同形（那边是 `skip_delete_approval or has_delete_approval()`），
        只是合成在一个方法里，省得每个调用点都要记得查两半。

        一次性那半要比属主（`has_delete_approval` 同款）：
        wm 是进程单例，不记属主的话 A 会话批过的目标在 B 会话里也能用。
        会话级那半不比属主 —— 它是每轮投影，天然只属于本会话。
        """
        if not self._projection_is_mine():
            return False          # 投影不是我的，安全值（照问 / 照拦）
        k, s = str(kind), str(subject)
        allowed = self._target_session.get(k) or set()
        if s in allowed or "*" in allowed:
            return True
        # `command` 的前缀规则（`prefix` 由调用方按档位给，
        #    见 `matched_target_grant` 那段注释）。
        if prefix and k == "command" and _command_rules_match(allowed, s):
            return True
        if self._target_once.get((k, s), None) == self._approval_scope:
            return True
        # 一次性那半也要认通配。
        #
        # 机理：选项 5 把 `"*"` 写进的是载体（`ctx.session_target_grants`），
        # 而那东西要到下一轮开局才被投影回这里 —— 本轮里它什么也没做；
        # 本轮能过的只有一次性那半，而它记的是提交时的精确 subject
        # 所以模型重提交一条不同的命令时照样弹，
        # 用户会连点两次「5」、第二次才生效。
        #
        # 结算时两半都写 `"*"`，本条就是那一半的读者。
        # 一次性那半轮末被 `reset_target_grants` 清掉（消费型），
        # 所以这个通配只活本轮；下一轮起由投影接着生效 —— 两半接力，不重不漏。
        return self._target_once.get((k, "*"), None) == self._approval_scope

    def reset_target_grants(self) -> None:
        """轮末清一次性那一半。

        与 `reset_delete_approval` 同一条规矩：不动投影（`_target_session`），
        它每轮开局会被重新投影。
        """
        self._target_once.clear()

    # 命令执行的两处配套

    @property
    def security(self) -> object:
        """路径分级器（`SecurityGuard`）—— 只读暴露给需要它的工具。

        唯一的读者是 `core/shell.py::_resolve_cwd`：它要判"这个目录在不在
        工作区里"。让调用方自己去 `_security` 摸，就是把一个私有字段变成
        事实上的公共接口；而另起一套"允许目录"的算法就是第二套真相源
        （那份列表由 `_reload_security_workspaces()` 同步）。
        """
        return self._security

    @property
    def session_cwd(self) -> str:
        """命令执行的工作目录 —— 投影。

        与上面那批同一条规矩：这是投影，不是真值。
        真值在 `PipelineScheduler._session_cwd`（按会话），每轮开局由
        `_project_session_flags()` 刷进来、轮末收回去。

        唯一直接写它的是 `core/shell.py::run_command`（命令跑完之后
        bash 回报的 `$PWD`）—— 工具拿不到会话身份，跟 `grant_target_once`
        是同一条理由。顺序是"轮首投影 → 轮内工具改 → 轮末收回"，不并发。

        不给它做进程级单值：会话 A 的 `cd` 会出现在会话 B 里 ——
        那就是"session 级"实际变成进程级跨会话。
        并发下加归属检查 —— 不是本会话投的就返回空
        （命令回退到工作区；方向安全）。
        """
        if not self._projection_is_mine():
            return ""
        return self._session_cwd

    @session_cwd.setter
    def session_cwd(self, value) -> None:
        self._session_cwd = str(value or "")
        # 写入章：写手是 shell 工具，它在自己的快照上下文里，
        # 拿得到会话键；拿不到就退回落章（投影进程序）。调度器轮末只收
        # 写给自己那一格（不然 A 的 `cd` 会被收进 B 的真值表）。
        try:
            from core.request_snapshot import current_session_id
            self._cwd_written_by = current_session_id() or self._projected_for
        except Exception:
            self._cwd_written_by = self._projected_for

    # Path index

    def build_index(self) -> int:
        """Build/rebuild the in-memory path index from all workspace roots.

        默认工作区必须在里面。若这里只拿工作组当根、`./workspace`
        只在 `if not roots`（一个工作组都没有）时兜底，会出现一条真实的链路断裂：

        * 写：`copy_to_workspace` 用 `primary_path`（没有 primary 工作组，回退到
          默认工作区）；`office_parser` 更是硬编码 `Path("workspace")`
        * 列/搜：`list_workspace_files` 从这个索引里列
        *，用户加了一个桌面文件夹当工作组之后，默认工作区从索引里消失：
          agent 把文件写进去，自己列不出来也搜不到：`workspace/` 里有
          `_wip_*.docx`，而它回答"工作区共 14 个文件"全在桌面那个文件夹。

        不变量：你写的地方，你必须看得见。索引的根 = 允许目录的全集
        （默认工作区 + 各工作组），与 `_reload_security_workspaces()` 构造的那份
        列表同源；`./workspace` 仍保留为"什么都没配"时的最后兜底。
        """
        roots: list[Path] = []
        seen: set[str] = set()

        def _add(p: str) -> None:
            if not p:
                return
            try:
                resolved = Path(p).expanduser().resolve()
            except Exception:
                return
            key = str(resolved)
            if key in seen or not resolved.exists():
                return
            seen.add(key)
            roots.append(resolved)

        # 默认工作区在前 —— 它是 agent 干活时的默认落点（与 :596 同序）
        _add(self._default_workspace_path)
        for wg in self._workspaces.values():
            _add(wg.path)
        if not roots:
            # 什么都没配时的最后兜底（保持原行为）
            _add("./workspace")

        # 用户明确选过的工作区目录优先于规避清单：根本身就落在某条规避目录里时，
        #   那条规避会把整个工作区扫成空的（`%TEMP%` 就在 `AppData` 下面）。
        def _inside(root: Path, bad: Path) -> bool:
            try:
                root.relative_to(bad)
                return True
            except ValueError:
                return False

        forbidden = [f for f in self._security._forbidden_dirs
                     if not any(_inside(r, Path(f)) for r in roots)]
        return self._path_index.build(roots, forbidden)

    def _maybe_refresh_index(self) -> None:
        """过期就重建（不是"只在空的时候建"，见 `_INDEX_STALE_SECONDS`）。

        不逐个文件 `stat` —— 那在几十万文件上是 O(n) 的 syscall 风暴。
        比对根目录的 `st_mtime_ns` 就够：用户拖一个文件进工作区，
        根目录的直接子项就变了。子目录里的增删由那条兜底过期线覆盖。
        """
        idx = self._path_index
        if idx._file_count == 0:
            self.build_index()
            return
        stamps = _root_stamps_of(idx._roots)
        if stamps != idx._root_stamps:
            self.build_index()
            return
        if time.time() - idx._last_built > _INDEX_STALE_SECONDS:
            self.build_index()

    def search_files(self, pattern: str = "*",
                     scope: str = "workspaces",
                     extensions: list[str] | None = None,
                     max_results: int = 200,
                     search_root: str = "",
                     granted: bool = False) -> tuple[list[FileEntry], str | None, dict | None]:
        """
        Search for files by pattern.

        *scope*:
          - "workspaces": only search work group directories (uses index)
          - "cross_zone": search all readable areas (index + on-demand scan)
          - "global": workspaces → if empty + auto_approve, expand to cross_zone.
            If auto_approve is OFF, returns an `action_required` prompt instead.

        *search_root*: when scope is "cross_zone" or "global", narrow the
          search to this directory instead of the entire user home.
          Accepts paths like "D:/", "C:/Users/X/Desktop", or Chinese
          names like "桌面", "下载", "文档".

        Returns (results, notice, action_required).
        """
        notice: str | None = None
        action: dict | None = None

        # 过期就重建，不是"只在空的时候建"。
        self._maybe_refresh_index()

        # Search workspaces
        results = self._path_index.search(pattern, extensions, max_results)
        if results:
            return results, None, None

        # Cross-zone expansion
        if scope in ("cross_zone", "global") and self._cross_zone_enabled:
            # 只看 `granted` —— 不自己再查授权标志。
            # 若这里也读 `self._one_time_approval`，"谁被授权"这件事就有
            # 两个判据（工具一处、这里一处）。一旦工具把授权消费掉，
            # 这里就变成假、返回 `action`，而调用方把 action 丢了
            # 就静默返回"没找到"（明明什么都没搜）。
            # 现在：能力由调用方当参数传下来，这里只负责用它。
            if self._auto_approve or granted:
                # 缓存：授权之后才查
                # 这一段若放在函数最前面（workspace 搜索之前、授权判定
                # 之前），而且不看 scope，那么 `search_files(hint)`
                # （默认 scope="workspaces"、零授权）也能拿到跨区的缓存路径。
                # 缓存里装的只有跨区结果（写入点只有本分支和 `find_file`），
                # 所以"查缓存"这件事本身就是一次跨区访问，必须待在门后面。
                cached = self._cache_lookup(pattern)
                if cached:
                    cached_results = []
                    for p in cached:
                        try:
                            st = os.stat(p)
                            cached_results.append(FileEntry(
                                name=Path(p).name, path=p,
                                ext=Path(p).suffix.lower(), size=st.st_size,
                                modified=local_time_str(st.st_mtime),
                            ))
                        except OSError:
                            pass
                    if cached_results:
                        return cached_results, None, None

                # Silent expansion (auto-approve or one-time grant)
                notice = self._security.notify_cross_zone(pattern)
                scan: dict = {}
                results = self._search_user_home(pattern, extensions, max_results,
                                                search_root, stats=scan)
                # 说出条数
                #
                # 报数格式照 `Showing N of M paths`。超限时若只说字符数
                # （`[truncated: N more chars]`），模型知道少了多少字符，
                # 不知道少了多少文件，更不知道给它的这些是按相关度排过序的。
                # 后者是要紧的：不说，模型会拿"列表里没有"当成"不存在"。
                # 根先于条数。根是语境（「刚才在哪儿找的」），
                # 条数是附加的保留意见 —— 反过来读会像是「条数那句在限定根」。
                root_note = _search_root_note(scan)
                if root_note:
                    notice = f"{notice}{root_note}" if notice else root_note
                count_note = _search_count_note(scan, max_results)
                if count_note:
                    notice = f"{notice}{count_note}" if notice else count_note
                if results:
                    self._cache_store(pattern, [r.path for r in results])
            else:
                # Need user approval — return prompt
                # 选项表收进 `core/approval.py`，保证只有一份措辞。
                action = _cross_zone_action(pattern, self.approval_ask_counts.get("cross_zone", 0) + 1)

        return results, notice, action

    def find_file(self, name_hint: str,
                  max_results: int = 50,
                  granted: bool = False) -> tuple[list[FileEntry], str | None, dict | None]:
        """
        Fuzzy file search by name substring.
        Searches workspaces first; expands to cross-zone if no results.
        Respects auto_approve — returns action_required prompt if OFF.

        必须在这里先初始化 `notice` / `action`（`search_files` 也是）。
        否则只要走到"需要审批"的分支（`auto_approve` 关着、或者 `granted`
        为假），最后那句 `return results, notice, action` 就会抛
        `UnboundLocalError`。
        """
        notice: str | None = None
        action: dict | None = None

        self._maybe_refresh_index()

        # Search workspaces
        results = self._path_index.search_by_name(name_hint, max_results)
        if results:
            return results, None, None

        # Cross-zone expansion
        if self._cross_zone_enabled:
            # 同 `search_files`：只看传下来的 `granted`。
            # 若这里只认 `_auto_approve`，同一个授权走
            # `search_files` 生效、走 `find_file` 被静默忽略。
            if self._auto_approve or granted:
                # 缓存授权之后才查 —— 与 `search_files` 同规矩。
                cached = self._cache_lookup(name_hint)
                if cached:
                    cached_results = []
                    for p in cached:
                        try:
                            st = os.stat(p)
                            cached_results.append(FileEntry(
                                name=Path(p).name, path=p,
                                ext=Path(p).suffix.lower(), size=st.st_size,
                                modified=local_time_str(st.st_mtime),
                            ))
                        except OSError:
                            pass
                    if cached_results:
                        return cached_results, None, None

                notice = self._security.notify_cross_zone(name_hint)
                results = self._search_user_home(name_hint, None, max_results)
                if results:
                    self._cache_store(name_hint, [r.path for r in results])
            else:
                # 与 `search_files` 同规矩（共用同一份选项表，连 `ask_index`
                # 也一致 —— 否则两条路会给出不同的选项数）
                action = _cross_zone_action(name_hint, self.approval_ask_counts.get("cross_zone", 0) + 1)
        return results, notice, action

    # Cross-zone path cache

    _CACHE_PATH = Path("data/runtime/cross_zone_cache.json")

    def _cache_load(self) -> dict:
        """Load the cross-zone path cache."""
        if self._CACHE_PATH.exists():
            try:
                return json.loads(self._CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _cache_save(self, data: dict) -> None:
        """Persist the cross-zone path cache."""
        self._CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _cache_lookup(self, hint: str) -> list[str] | None:
        """Look up cached paths for a search hint —— 精确匹配，不做模糊。

        不能做"模糊兜底"（把待查 hint 与每条已存 hint 做子串包含、
        len >= 3 就命中）：缓存回答的是"这个查询跑过没有"，
        子串匹配回答的是"这个查询像不像跑过的那个"
        —— 后者会把另一个查询的答案端上来，而调用方无法分辨。
        要放宽，前提是先有"结果属于哪个查询"的证据；现在没有
        （`session_id` 字段存了，但全仓没有任何地方写过它，
        所以它恒为空串，见 :meth:`_cache_store`）。

        缓存只做一件事：同一个关键词、已授权过，就不再扫一遍盘。
        """
        key = self._cache_key(hint)
        data = self._cache_load()
        entry = data.get(key)
        if entry:
            return self._validate_cache_entry(data, key, entry)
        return None

    def _validate_cache_entry(self, data: dict, key: str, entry: dict) -> list[str] | None:
        """过期 + 文件还在不在 + 更新 last_hit。

        只检查过期是不够的。缓存交出去的是一串路径，而路径会消失
        （用户删了、移走了、换了盘）—— 返回一个不存在的路径等于告诉模型
        "找到了"，然后让它去解析一个 404。整条都失效就当作未命中，
        让调用方去真搜。
        """
        from datetime import datetime, timezone, timedelta

        created = entry.get("created_at", "")
        try:
            created_dt = datetime.fromisoformat(created)
            if datetime.now(timezone.utc) - created_dt > timedelta(days=7):
                del data[key]
                self._cache_save(data)
                return None
        except Exception:
            pass  # 时间戳坏了不当作过期 —— 让下面那关去判

        paths = [p for p in entry.get("found_paths", []) if Path(p).exists()]
        if not paths:
            del data[key]
            self._cache_save(data)
            return None

        entry["found_paths"] = paths
        entry["last_hit"] = datetime.now(timezone.utc).isoformat()
        self._cache_save(data)
        return paths

    def _cache_store(self, hint: str, paths: list[str]) -> None:
        """Store cross-zone search results in cache.

        这一格 `session_id` 恒为空串。字段留着（老缓存文件里有它），
        但值是明写的空串：写成 `getattr(self, '_current_session_id', '')`
        会看起来像"接上了、只是暂时取不到"，实际是"这条路上根本没有那个东西"
        —— 全仓没有任何地方写过那个属性。理由写在这里，不藏在一个 `getattr`
        的兜底值里。
        于是这张缓存是跨会话的 —— 这是已知且有意的。
        别读成"已经按会话隔离了"。
        """
        key = self._cache_key(hint)
        data = self._cache_load()
        from datetime import datetime, timezone
        data[key] = {
            "session_id": "",
            "hint": hint,
            "found_paths": paths,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_hit": datetime.now(timezone.utc).isoformat(),
        }
        self._cache_save(data)

    def cache_clear_for_session(self, session_id: str = "") -> None:
        """清缓存：空串 = 全清；非空串 = 拒（不是"尽力而为"）。

        为什么非空串要拒：每条记录的 `session_id`
        都恒为空串（见 :meth:`_cache_store`），拿真 id 来清时，过滤条件
        v.get("session_id") != session_id 对每一条都成立，一条都不删，
        而调用方看到的是"函数正常返回"。
        静默地什么都没做，长得像做完了 —— 这就是"不承诺兑现不了的事"。
把那个"做不到"变成一次看得见的拒绝。

        真要按会话隔离，先回答一个设计问题：这个 hint→paths 缓存该不该
        按会话隔离？判据是"缓存回答的是这个查询跑过没有" —— 而那与谁问的无关。
        """
        if session_id:
            raise ValueError(
                "cache_clear_for_session 只能全清（传空串）—— 这张缓存的每一条记录"
                " session_id 都是空的（跨会话缓存，见 _cache_store 的 docstring）。"
                " 传一个真 id 进来看起来像按会话清了，其实**一条都不会删**。"
                " 要按会话隔离请先把设计问题定下来。")
        self._cache_save({})

    @staticmethod
    def _cache_key(hint: str) -> str:
        """Generate a cache key from a search hint."""
        import hashlib
        return hashlib.md5(hint.strip().lower().encode()).hexdigest()[:12]

    def _search_user_home(self, pattern: str,
                          extensions: list[str] | None,
                          max_results: int,
                          search_root: str = "",
                          stats: dict | None = None) -> list[FileEntry]:
        """On-demand scan of user home (or a specific directory) for a file pattern.

        If *search_root* is provided and points to an existing directory,
        the search starts from there instead of the entire user home.

        If *pattern* contains no wildcards (`*` or `?`), it is
        treated as a substring search (wrapped with `*` on both ends)
        so that "报告" matches "实训报告.docx".

        *stats* —— 出参。填 `matched` / `returned` /
        `floor_dropped` / `capped`，给调用方拿去"说清条数"。

        为什么是出参 dict，不是第四个返回值：`search_files` 返回三元组，
        调用方都在按三元组解包；加第四个返回值等于把"解包数量对不上"这个坑
        再挖一遍。
        """
        results: list[FileEntry] = []
        # Resolve search root
        root_path = Path.home()
        fell_back = False
        if search_root:
            resolved = _resolve_search_path(search_root)
            if resolved and resolved.exists():
                root_path = resolved
            else:
                # 点了名却没解析出来 —— 若静默回落到家目录，
                # 回执照旧说「搜过了」，模型传 `D:/没有这个目录` 得到的却是
                # 家目录的结果，它无从分辨。`root_fell_back` 就是那个
                # 「说不出口的事实」。
                fell_back = True
        # 「搜的是哪个根」必须跟着结果走。
        #
        # 填在这里而不是 `_finish` 里 —— 早退那几条路（撞上限）
        # 根本不经过 `_finish`，而它们恰恰是最需要这句话的。
        # 根从第一行就确定了，所以就在第一行记。
        if stats is not None:
            stats["root"] = str(root_path)
            stats["root_requested"] = search_root or ""
            stats["root_fell_back"] = fell_back
        ext_set = set(extensions) if extensions else None
        # Auto-wrap bare filenames for substring matching
        search_pattern = pattern
        if '*' not in pattern and '?' not in pattern:
            search_pattern = f"*{pattern}*"

        # 多词 hint：按词打分
        #
        # 模型会传
        # `操作系统启发 降低代理工具使用延迟 agent tool use latency`
        # ——空格分开的关键词，而 `fnmatch(f, f"*{pattern}*")` 要求
        # 整串是文件名的一部分，一个都匹配不到。
        # 后果比"搜不到"更糟：工具回 `0 个`，模型据此对用户下结论
        # 「你电脑上没有这份资料」。
        #
        # 为什么不是"全部词都命中"：那串里有 `agent/tool/use/latency` 四个词
        # 根本不在文件名里（文件名是中文的），AND 照样是 0。
        # 任一词命中即候选。
        #
        # 排序按 命中字符的总长度，不是"命中词数"。两种规则的差别是
        # 决定性的 ——
        #
        #   长度和=16  命中词数=2  一种受操作系统启发来降低代理工具使用延迟的方法.docx  ← 目标
        #   长度和=9   命中词数=2  AgentTool.ts
        #   长度和=9   命中词数=2  agent_tool.py
        #
        # 按词数排的话目标是并列第 2 名往后、同分按名字排（CJK 排在 ASCII
        # 之后），它落到 6000 字符截断之外，等于还是找不到。按长度排
        # 它是第 1 名（`降低代理工具使用延迟` 一个词就 10 个字，比 `tool`/`use`
        # 这些泛词具体得多 —— 长度是"具体性"的一个够用的代理）。
        #
        # 单词 hint 不走这条路 —— 它保持 `fnmatch` 行为不变
        # （文件名里合法地含空格的情况不受影响）。
        tokens = [t for t in pattern.lower().split() if t]
        use_tokens = (len(tokens) > 1
                      and '*' not in pattern and '?' not in pattern)
        #: 候选池上限。多词时先收集、后排序，所以要防着"某个词太泛"把内存撑爆。
        #: （那个 hint 在用户桌面上有 426 个候选，所以这个上限不是假想。）
        _candidate_cap = max_results * 20 if max_results else 0
        candidates: list[tuple[int, int, str, str, str, os.stat_result]] = []
        # 漫游时跳过的目录名。前两个是兜底，其余来自规避清单。
        _skip_dirs: set[str] = {
            "AppData", "Application Data",
        }
        # 只认 SecurityGuard 上那一份 —— 它现在就是"用户在设置里看到的那张表"
        #   （`load_from_settings` 读不到键时才用默认填）。这里不再无条件并一遍
        #   默认：界面上删掉的目录如果还在被跳过，那张表就是一句假话。
        try:
            excluded_parts = set()
            for d in self._security._forbidden_dirs:
                try:
                    excluded_parts.add(Path(d).name)
                except Exception:
                    pass
            _skip_dirs.update(excluded_parts)
        except Exception:
            pass

        # 所有出口都要报数：只在末尾填 `stats` 是不够的 ——
        # walk 里"到上限就停"的 `return` 直接跳过它，恰恰在
        # "最该说没搜完"的那条路上，`stats` 会是空的。
        #: 「走完了吗」的两个独立原因 —— 它们都必须能单独说出来
        #: （撞上限 / 超时）。拼成一个布尔就等于把
        #: "我没搜完"和"我不知道有多少"混成一句话。
        _walk_state = {"capped": False, "timed_out": False}

        def _finish(entries, matched, capped=False, floor_dropped=0):
            # 超时，真实总数不可知，一律报 `None`（含多词那条路：
            # 它本来能给出 `len(candidates)`，但那个数只覆盖扫到的那部分，
            # 报出来就是"扫了这么多、就这么多"—— 而那正是要分开的两种"没有"）。
            if _walk_state["timed_out"]:
                matched = None
            if stats is not None:
                stats["matched"] = matched
                stats["returned"] = len(entries)
                stats["floor_dropped"] = floor_dropped
                stats["capped"] = bool(capped or _walk_state["capped"])
                stats["timed_out"] = _walk_state["timed_out"]
            return entries

        # 可中断的遍历
        # `os.walk` 本身是同步阻塞的，没有取消点，在每个目录开头查一次
        # deadline。检查在 `dirs[:] = ...` 之前：剪枝也算这次遍历的
        # 成本，而且早退之后 `dirs` 不再被用。
        deadline = time.monotonic() + _SEARCH_DEADLINE_SECONDS

        # Use onerror to skip inaccessible directories instead of
        # aborting the entire search on the first permission error.
        def _walk_error(_err):
            pass  # silently skip
        try:
            for root, dirs, files in os.walk(root_path, onerror=_walk_error):
                if time.monotonic() > deadline:
                    _walk_state["timed_out"] = True
                    break
                # Skip hidden/system dirs + default excludes
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and not d.startswith("$")
                           and d not in _skip_dirs]
                for f in files:
                    if max_results and len(results) >= max_results:
                        # 总数不可知（walk 还没走完），matched=None，
                        # 与「没搜完」分开报
                        _walk_state["capped"] = True
                        return _finish(results, None, True)
                    if _candidate_cap and len(candidates) >= _candidate_cap:
                        _walk_state["capped"] = True
                        return _finish(results, None, True)
                    ext = Path(f).suffix.lower()
                    if ext_set and ext not in ext_set:
                        continue
                    if use_tokens:
                        name_l = f.lower()
                        hits = [t for t in tokens if t in name_l]
                        if not hits:
                            continue
                        fp = os.path.join(root, f)
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        candidates.append(
                            (sum(len(t) for t in hits), len(hits), f, fp, ext, st))
                        continue
                    if fnmatch.fnmatch(f.lower(), search_pattern.lower()):
                        fp = os.path.join(root, f)
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        results.append(FileEntry(
                            name=f, path=fp, ext=ext,
                            size=st.st_size,
                            modified=local_time_str(st.st_mtime),
                        ))
        except Exception:
            pass

        if use_tokens and candidates:
            # 命中字符多的排前面；再同分按命中词数、最后按名字。
            # 末尾那两级是为了稳定 —— 文件系统给的顺序是任意的，
            # 同分时结果不该随它变。
            candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
            matched = len(candidates)
            # 相对下限
            #
            # 只有上限 200、没有下限是不够的。那串 426 个候选打满 200 条
            # = 46,736 字符，而工具结果预算 6,000，模型只看到约 25 条，
            # 其余 175 条写盘且读不回来；其中 103 条只命中一个泛词。
            #
            # 为什么是相对不是绝对：分数尺度随查询变（一条 top=16、
            # 另一条 top=4）。绝对阈值（"≥4 分"）在那个查询上一个都筛不掉；
            # 而相对阈值对分数均匀的正常查询完全不动（"报告 2026" 那条
            # 26 → 26 一个没变）。
            #
            # 50% 不是 75%：75% 在那个查询上只剩 1 条 —— 万一最高分那个
            # 是碰巧命中的无关文件，真目标就被一起藏了。
            floor = candidates[0][0] * _SEARCH_RELATIVE_FLOOR
            kept = [c for c in candidates if c[0] >= floor]
            for _chars, _n, f, fp, ext, st in kept[:max_results or None]:
                results.append(FileEntry(
                    name=f, path=fp, ext=ext,
                    size=st.st_size,
                    modified=local_time_str(st.st_mtime),
                ))
            # 上限是在排序之后才切的（上面那句 `kept[:max_results]`），
            # 这里 `matched`/`floor_dropped` 都是真数，`capped` 可判。
            return _finish(results, matched,
                           max_results and len(kept) > max_results,
                           floor_dropped=matched - len(kept))

        # 走到这里 = walk 走完了（唯二的早退在上面两个 `_finish(..., True)`
        # 里），`len(results)` 就是真实总数，不管走的是哪条匹配路。
        # 所以：多词那条真的没有匹配，`0`（"你电脑上没有这份资料"这个结论
        # 就长在这个数上）；glob 那条匹配了几个就是几个。两者都是结论，
        # 不是"读不到"。真正读不到的是撞上限那条路（报 `None`）。
        return _finish(results, len(results), False)

    @property
    def index_stats(self) -> dict:
        return self._path_index.stats

    @property
    def settings_generation(self) -> int:
        """Monotonically increasing counter bumped on every load_from_settings.
        PreProcessStage compares this to detect live setting changes."""
        return self._settings_generation

    # Per-session generation tracking

    def check_settings_changed(self, session_key: str) -> str | None:
        """Return a context-injection string if workspace settings have
        changed since the last turn for *session_key*, or None.

        The caller (PreProcessStage) prepends the returned string to the
        user message so the LLM knows to re-evaluate the workspace state.
        """
        last = self._last_known_gen.get(session_key, 0)
        if self._settings_generation > last:
            self._last_known_gen[session_key] = self._settings_generation
            # v1.2.0: 陈述句，不是祈使句。
            #
            # 原文是"请调用 list_workspace_files 重新检查可用的工作区文件"，
            # 且被前置进 ctx.user_message。那有三处越界：
            #   ① 它是命令，强迫 agent 立即行动 —— 与用户真正问的东西无关
            #      （用户说"你在吗"，agent 却去列了整个工作区）
            #   ② 混淆了"系统给的背景"与"用户说的话"
            #   ③ 会被写进 SQLite 历史 —— 用户实际没说过那句话
            #
            # 现在只陈述事实，由调用方放进 background context。agent 因此
            # 总是知道当前状态（不会拿旧清单回答），但没有任何"去行动"的
            # 暗示，也不会污染历史。
            return "工作区设置已更新。"
        return None

    _last_known_gen: dict[str, int] = {}

    # Watcher management (Phase C - stubs for now)

    @property
    def watchers(self) -> list[WatcherConfig]:
        return list(self._watchers.values())

    def add_watcher(self, config: WatcherConfig) -> WatcherConfig:
        if not config.id:
            config.id = uuid.uuid4().hex[:8]
        self._watchers[config.id] = config
        return config

    def remove_watcher(self, watcher_id: str) -> bool:
        if watcher_id not in self._watchers:
            return False
        self.stop_watcher(watcher_id)
        del self._watchers[watcher_id]
        return True

    def update_watcher(self, watcher_id: str, **kwargs) -> bool:
        wc = self._watchers.get(watcher_id)
        if not wc:
            return False
        for key, value in kwargs.items():
            if hasattr(wc, key):
                setattr(wc, key, value)
        return True

    def toggle_watcher(self, watcher_id: str, enabled: bool) -> bool:
        wc = self._watchers.get(watcher_id)
        if not wc:
            return False
        wc.enabled = enabled
        if enabled:
            self.start_watcher(watcher_id)
        else:
            self.stop_watcher(watcher_id)
        return True

    def stop_watcher(self, watcher_id: str) -> None:
        """Stop a running watcher instance."""
        instance = self._watcher_instances.pop(watcher_id, None)
        if instance and hasattr(instance, 'stop'):
            instance.stop()

    def start_watcher(self, watcher_id: str) -> bool:
        """Start a file watcher using USN Journal (Windows) or polling fallback."""
        if not self._cascade_enabled:
            #: 总闸关着，任何起法都不许起（`/api/watchers/<id>/toggle`
            #:   也走这里）—— 否则"设置页关掉了、另一边还能点开"就是两套口径。
            print(f"[workspace] 「级联动态」总开关关着 —— 不启动监听器 '{watcher_id}'")
            return False
        wc = self._watchers.get(watcher_id)
        if not wc or not wc.enabled:
            return False
        # 目录不存在不是"暂时还没有文件"，是"这个监听永远不会有触发"。
        #   下面两条循环（`_build_snapshot` / `_discover_new_files`）对不存在的
        #   路径都是静默返回空 —— 于是启动照样成功、用户那边一条变更都不会来。
        #   微信换过数据目录之后就是这个形状，而界面上一片安静。
        try:
            _watch_ok = Path(wc.watch_path).exists()
        except OSError:
            _watch_ok = False
        if not _watch_ok:
            _vp = str(wc.watch_path or "?")
            self._watcher_failed(
                watcher_id, "监听的目录不存在：%s" % _vp,
                reason_tpl="监听的目录不存在：{path}", reason_vars={"path": _vp})
            return False
        # Stop existing instance if any
        self.stop_watcher(watcher_id)
        # Determine target workspace path
        target_path = self.primary_path
        if wc.target_workspace_id:
            target_ws = self._workspaces.get(wc.target_workspace_id)
            if target_ws:
                target_path = target_ws.path

        try:
            watcher = USNWatcher(
                watch_path=wc.watch_path,
                target_path=target_path,
                sync_documents=wc.sync_documents,
                sync_images=wc.sync_images,
                sync_media=wc.sync_media,
                sync_archives=wc.sync_archives,
                custom_extensions=wc.custom_extensions,
                on_sync=self._on_watcher_sync if wc.notify_on_sync else None,
                watcher_id=watcher_id,
                exclude_subdirs=wc.exclude_subdirs,
                snapshot_mode=wc.snapshot_mode,
            )
            watcher.start()
            self._watcher_up(watcher_id, watcher)
            print(f"[workspace] USN watcher '{watcher_id}' started on {wc.watch_path}")
            return True
        except Exception as e:
            print(f"[workspace] Failed to start USN watcher '{watcher_id}': {e}")
            # Fallback: use polling watcher
            try:
                watcher = PollingWatcher(
                    watch_path=wc.watch_path,
                    target_path=target_path,
                    sync_documents=wc.sync_documents,
                    sync_images=wc.sync_images,
                    sync_media=wc.sync_media,
                    sync_archives=wc.sync_archives,
                    custom_extensions=wc.custom_extensions,
                    on_sync=self._on_watcher_sync if wc.notify_on_sync else None,
                    watcher_id=watcher_id,
                    exclude_subdirs=wc.exclude_subdirs,
                    snapshot_mode=wc.snapshot_mode,
                )
                watcher.start()
                self._watcher_up(watcher_id, watcher)
                print(f"[workspace] Fallback polling watcher '{watcher_id}' started")
                return True
            except Exception as e2:
                print(f"[workspace] Polling watcher also failed: {e2}")
                self._watcher_failed(
                    watcher_id, "监听服务启动失败：%s" % e2)
                return False

    def _watcher_failed(self, watcher_id: str, reason: str,
                        reason_tpl: str = "", reason_vars: dict | None = None) -> None:
        """记下原因，并交给事故账本。

        三条起法（自启动 / 设置页那个开关 / `init_bus`）都走 `start_watcher`，
        所以事故只在那一处记 —— 写在三个调用点就是三套口径。
        用户开了监听、以为在监听，实际一条变更都不会触发，而终端那行他看不到；
        界面上那句横幅（`#02`）才是他会看见的东西。

        *reason_tpl* / *reason_vars*：这句原因给人看的那份模板（见
        core.incidents.note 的 docstring）。不传就只有拼好的中文串，
        英文界面下它不会被翻译。
        """
        self._watcher_errors[watcher_id] = reason
        print(f"[workspace] {reason} —— 不启动监听器 '{watcher_id}'")
        try:
            from core import incidents as _inc
            wc = self._watchers.get(watcher_id)
            _inc.note("监听：" + str(watcher_id), "watcher", reason,
                      "watch_path=" + str(getattr(wc, "watch_path", "")),
                      component_tpl="监听：{id}",
                      component_vars={"id": str(watcher_id)},
                      error_tpl=reason_tpl, error_vars=reason_vars)
        except Exception:                                      # noqa: BLE001
            # 账本自己出问题不该连累监听器。
            pass

    def _watcher_up(self, watcher_id: str, watcher) -> None:
        """登记一个真的起来了的实例，并把上一次的失败痕迹撤掉。

        撤痕迹是成对的那一半：用户把目录改回来、重新启用之后，
        界面上的那条事故必须跟着消失 —— 否则它变成一条没人能撤的旧账。
        """
        self._watcher_instances[watcher_id] = watcher
        self._watcher_errors.pop(watcher_id, None)
        try:
            from core import incidents as _inc
            _inc.resolve("监听：" + str(watcher_id), "watcher")
        except Exception:                                      # noqa: BLE001
            # 账本自己出问题不该连累监听器。
            pass

    def watcher_error(self, watcher_id: str) -> str:
        """上一次启动失败的原因；没失败过就是空串。"""
        return self._watcher_errors.get(watcher_id, "") or ""

    def _on_watcher_sync(self, watcher_id: str, filename: str, target: str) -> None:
        """Callback invoked when a watcher syncs a file.
        Pushes to a queue so multiple simultaneous captures aren't lost,
        AND updates the PathIndex so the agent can find the file immediately."""
        # Look up the watcher config for its display name
        wc = self._watchers.get(watcher_id)
        watch_dir_name = Path(wc.watch_path).name if wc else watcher_id
        self._sync_event_queue.append({
            "watcher_id": watcher_id,
            "watch_name": watch_dir_name,
            "filename": filename,
            "target": target,
            "time": time.time(),
        })
        # Keep at most 50 events to bound memory
        if len(self._sync_event_queue) > 50:
            self._sync_event_queue = self._sync_event_queue[-50:]
            self._last_polled_event_index = max(0,
                self._last_polled_event_index - (len(self._sync_event_queue) - 50))
        # Update PathIndex so list_workspace_files finds the file
        self._path_index.add_file(target)

    def autostart_watchers(self) -> dict:
        """框架启动时把已启用的监听器自动拉起来。

        没有这一条时，`start_watcher` 全仓只有两个调用点 —— 它自己的定义、
        以及 `toggle_watcher(enabled=True)`（用户在界面上点的那一下）
重启一次就全没了，必须有人再点一次。

        ## 两个条件都要满足（不是"启用了就拉"）

          · `enabled` —— 用户明确启用了这个监听器；
          · `auto_start` —— 这个监听器自己的"开机自启"（默认 `True`，
            写进 `settings.json` 的 `watchers[]` 里）。

        ## 两条刻意的取舍

        · 返回一份读数（`started` / `failed` / `skipped` 三张名单），
          不是一句话 —— 调用方（启动日志）要能如实说出"拉了几个、跳了几个"。
          `failed` 与 `skipped` 分开（读不到 ≠ 0）：
          前者是"想拉但起不来"，后者是"本来就不该拉"。
        · 失败不拦启动 —— 一个监听器起不来绝不是"框架不许启动"的理由
          （同 `snapshot_before` 那条"永不抛"的方向）。

        可以重复调（`/api/restart` 会在同一进程里再走一遍 `initialize()`）：
        `start_watcher` 自己先 `stop_watcher`，不会起两个实例；
        而重跑一次正是 `snapshot_mode="boot"` 声明的语义（"每次框架启动重新快照"）。
        """
        started: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        # 两个总闸先看：关着就一个都不起，并如实报 skipped
        #   （"本来就不该起" ≠ "想起但起不来" —— 这两笔不许混）。
        if not (self._cascade_enabled and self._cascade_auto_start):
            for wc in self._watchers.values():
                skipped.append(wc.id)
            if skipped:
                print("[workspace] 监听器自启动被总开关关掉了（「级联动态」/"
                      "「自启动监听」）—— 跳过 %d 个" % len(skipped))
            return {"started": [], "failed": [], "skipped": skipped}
        for wc in list(self._watchers.values()):
            if not (wc.enabled and wc.auto_start):
                skipped.append(wc.id)
                continue
            try:
                ok = self.start_watcher(wc.id)
            except Exception as exc:              # noqa: BLE001
                print(f"[workspace] 自动启动监听器 '{wc.id}' 失败: "
                      f"{type(exc).__name__}: {exc}")
                ok = False
            (started if ok else failed).append(wc.id)
        if started or failed:
            print(f"[workspace] 自动启动监听器：起了 {len(started)} 个"
                  + (f"，失败 {len(failed)} 个" if failed else "")
                  + (f"，跳过 {len(skipped)} 个（未启用 / 未开自启）"
                     if skipped else ""))
        return {"started": started, "failed": failed, "skipped": skipped}

    def get_watcher_status(self) -> list[dict]:
        """Return status for all watchers."""
        result = []
        for wc in self._watchers.values():
            instance = self._watcher_instances.get(wc.id)
            running = instance is not None and getattr(instance, '_running', False)
            result.append({
                "id": wc.id,
                "watch_path": wc.watch_path,
                "enabled": wc.enabled,
                "running": running,
                "notify_on_sync": wc.notify_on_sync,
            })
        return result

    def get_last_sync_event(self) -> dict | None:
        """Return the most recent sync event (for backward compat)."""
        return self._sync_event_queue[-1] if self._sync_event_queue else None

    def poll_watcher_events(self) -> list[dict]:
        """Return all new sync events since last poll.
        Drains the queue so each event is only delivered once."""
        events = self._sync_event_queue[self._last_polled_event_index:]
        self._last_polled_event_index = len(self._sync_event_queue)
        return events


# File Watchers — USN Journal + Polling fallback

import fnmatch as _fnmatch
from core.loop_health import LoopHealth
import shutil as _shutil
import threading as _threading


class _BaseWatcher:
    """Common logic for file watchers."""

    # Subdirectory names that are always noise — encrypted blobs,
    # migration leftovers, or system artefacts that should never
    # be synced regardless of which app created them.
    _ALWAYS_EXCLUDE_DIRS: set[str] = {"attach", "migrate"}

    # When these three sibling directories exist under a watch path,
    # it's almost certainly a WeChat (xwechat_files) directory.
    _WECHAT_SIGNATURE: tuple[str, ...] = ("file", "video", "attach")

    # Snapshot persistence
    _SNAPSHOT_DIR = Path("data/workdata/watcher_snapshots")

    def __init__(self, watch_path, target_path,
                 sync_documents, sync_images, sync_media,
                 custom_extensions, on_sync, watcher_id,
                 exclude_subdirs=None, snapshot_mode="boot",
                 sync_archives=False):
        self.watch_path = watch_path
        self.target_path = target_path
        self.watcher_id = watcher_id
        self._on_sync = on_sync
        self._running = False
        self._thread = None
        self._snapshot_mode = snapshot_mode
        # Build extension set
        self._extensions: set[str] = set()
        if sync_documents:
            self._extensions.update(DOCUMENT_EXTENSIONS)
        if sync_images:
            self._extensions.update(IMAGE_EXTENSIONS)
        if sync_media:
            self._extensions.update(MEDIA_EXTENSIONS)
        if sync_archives:
            self._extensions.update(ARCHIVE_EXTENSIONS)
        for ext in (custom_extensions or []):
            ext = ext.strip()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext:
                self._extensions.add(ext.lower())
        # Build exclude set: user-configured + auto-detected + always-exclude
        self._exclude_dirs: set[str] = set(self._ALWAYS_EXCLUDE_DIRS)
        if exclude_subdirs:
            self._exclude_dirs.update(exclude_subdirs)
        # Auto-detect WeChat directories regardless of parent name
        auto = self._detect_wechat_subdirs()
        if auto:
            self._exclude_dirs.update(auto)
            print(f"[watcher:{watcher_id}] Auto-detected WeChat dir — "
                  f"excluding: {auto}")
        # Snapshot: known files with their mtimes
        self._snapshot: dict[str, float] = {}   # full path → mtime

    # Snapshot I/O

    def _snapshot_path(self) -> Path:
        """Path to this watcher's snapshot file."""
        return self._SNAPSHOT_DIR / f"{self.watcher_id}.json"

    def _load_snapshot(self) -> dict[str, float]:
        """Load snapshot from disk. Returns {} if missing or not applicable."""
        if self._snapshot_mode == "boot":
            # "boot" mode: always start fresh
            return {}
        sp = self._snapshot_path()
        if sp.exists():
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                return data.get("files", {})
            except Exception:
                pass
        return {}

    def _save_snapshot(self, files: dict[str, float]) -> None:
        """Persist the current file snapshot to disk.
        Only saved in "deploy" mode; "boot" mode snapshots live in memory."""
        if self._snapshot_mode != "deploy":
            return
        self._SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        self._snapshot_path().write_text(
            json.dumps({
                "files": files,
                "watch_path": self.watch_path,
                "saved_at": datetime_to_iso(time.time()),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_snapshot(self) -> dict[str, float]:
        """Walk the watch directory and build a fresh file→mtime snapshot."""
        snapshot: dict[str, float] = {}
        wp = Path(self.watch_path)
        if not wp.exists():
            return snapshot
        try:
            for root, dirs, files in os.walk(wp):
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and not d.startswith("$")
                           and d not in self._exclude_dirs]
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        snapshot[fpath] = os.stat(fpath).st_mtime
                    except OSError:
                        pass
        except (OSError, PermissionError):
            pass
        return snapshot

    # Files modified less than this many seconds ago are considered
    # "still being written" and are skipped until the next poll.
    # Prevents syncing half-downloaded files (e.g. a browser saving a
    # large .docx that takes 5+ seconds to complete).
    _FILE_STABLE_SECONDS: float = 2.0

    def _discover_new_files(self) -> list[str]:
        """Walk the watch dir and return paths NOT in the snapshot.
        Files with a very recent mtime are skipped (still being written).
        Updates both the in-memory snapshot and the persisted copy."""
        new_files: list[str] = []
        current: dict[str, float] = {}
        now = time.time()
        wp = Path(self.watch_path)
        if not wp.exists():
            return new_files
        try:
            for root, dirs, files in os.walk(wp):
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and not d.startswith("$")
                           and d not in self._exclude_dirs]
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        st = os.stat(fpath)
                        mtime = st.st_mtime
                        current[fpath] = mtime
                        old_mtime = self._snapshot.get(fpath)
                        if old_mtime is None or mtime > old_mtime:
                            # Skip files still being written
                            if now - mtime < self._FILE_STABLE_SECONDS:
                                continue
                            new_files.append(fpath)
                    except OSError:
                        pass
        except (OSError, PermissionError):
            pass
        # Update snapshot with current state
        self._snapshot = current
        self._save_snapshot(current)
        return new_files

    @classmethod
    def _detect_wechat_subdirs(cls, watch_path: str = "") -> list[str]:
        """Check if *watch_path* looks like a WeChat directory by looking
        for the characteristic `msg/{file,video,attach}` sibling pattern.
        Returns the dirs to exclude (empty if no match).

        This works regardless of whether the parent is named
        `xwechat_files` or something custom — it detects by structure.
        """
        if not watch_path:
            return []
        wp = Path(watch_path)
        if not wp.exists():
            return []

        # Look for msg/{file, video, attach} siblings
        # WeChat structure: .../msg/file, .../msg/video, .../msg/attach
        msg_dir = wp / "msg"
        if not msg_dir.is_dir():
            # Maybe the watch path IS the msg directory itself
            msg_dir = wp
        sig_hits = 0
        for sub in cls._WECHAT_SIGNATURE:
            if (msg_dir / sub).is_dir():
                sig_hits += 1
        # Require at least 2 of the 3 signature dirs to confirm
        if sig_hits >= 2:
            return ["attach", "migrate"]
        return []

    def _should_sync(self, filename: str) -> bool:
        """Check if filename should be synced based on extension filter
        AND temp-file exclusion list."""
        if self._is_temp_file(filename):
            return False
        if not self._extensions:
            return True  # sync everything
        ext = Path(filename).suffix.lower()
        return ext in self._extensions

    @staticmethod
    def _is_temp_file(filename: str) -> bool:
        """Return True if *filename* is a known temp / cache / download artefact.

        Checks both extension-based patterns and filename-based patterns
        (e.g. `~$report.docx` Office lock files, `Thumbs.db`).
        """
        name_lower = filename.lower()
        ext = Path(name_lower).suffix
        # Short-lived file; wait for the final rename (e.g. .crdownload → .pdf)
        if ext in TEMP_IGNORE_EXTENSIONS:
            return True
        # Office temp lock files: ~$anything.docx
        if name_lower.startswith("~$"):
            return True
        # Known system artefacts
        if name_lower in TEMP_IGNORE_FILENAMES:
            return True
        return False

    def _sync_file(self, filepath: str) -> bool:
        """Copy file to target workspace. Returns True on success."""
        try:
            src = Path(filepath)
            if not src.is_file():
                return False
            dest_dir = Path(self.target_path) / "_cascade_imports"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            # Avoid overwriting with same content
            if dest.exists():
                if dest.stat().st_size == src.stat().st_size:
                    return False
                # Rename: add timestamp
                ts = time.strftime("%Y%m%d_%H%M%S")
                stem = dest.stem
                dest = dest_dir / f"{stem}_{ts}{src.suffix}"
            _shutil.copy2(str(src), str(dest))
            if self._on_sync:
                try:
                    self._on_sync(self.watcher_id, src.name, str(dest))
                except Exception:
                    pass
            return True
        except (OSError, PermissionError) as e:
            print(f"[watcher:{self.watcher_id}] Sync error for {filepath}: {e}")
            return False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = _threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run_loop(self) -> None:
        raise NotImplementedError


class PollingWatcher(_BaseWatcher):
    """Simple polling-based file watcher (cross-platform fallback).

    Uses a persistent snapshot to detect only files that are new or
    modified since the watcher started — never syncs pre-existing files.
    """

    def _run_loop(self) -> None:
        """Poll the watch directory every 5 seconds."""
        # Load persisted snapshot (deploy mode) or build fresh (boot mode)
        loaded = self._load_snapshot()
        if loaded:
            self._snapshot = loaded
            print(f"[watcher:{self.watcher_id}] Loaded snapshot: "
                  f"{len(loaded)} files (mode={self._snapshot_mode})")
        else:
            self._snapshot = self._build_snapshot()
            self._save_snapshot(self._snapshot)
            print(f"[watcher:{self.watcher_id}] Built snapshot: "
                  f"{len(self._snapshot)} files (mode={self._snapshot_mode})")

        # 日志要有形状 —— 只 `print(…: {e})` 时无计数、无时长，
        # 挂 10 小时的 watcher 与闪一下的在盘上长得一样。
        # 判定和微信轮询同一份（`core/loop_health`），本文件不抄第二份。
        #
        # 这里刻意不用退避：`time.sleep(5)` 是循环固有节拍（成功路径
        #   也睡 5 秒），频率本来就没问题 —— 借的只是它的日志形状那一半。
        #   所以 `fail()` 返回的 `wait` 被丢掉是有意的，不是漏读。
        _health = LoopHealth(f"watcher:{self.watcher_id}")

        while self._running:
            try:
                new = self._discover_new_files()
                for fpath in new:
                    fname = Path(fpath).name
                    if self._should_sync(fname):
                        self._sync_file(fpath)
                _line = _health.ok(time.monotonic())
                if _line is not None:
                    print(_line)
            except Exception as e:
                _wait, _line = _health.fail(e, time.monotonic())
                if _line is not None:
                    print(_line)
            time.sleep(5)


class USNWatcher(_BaseWatcher):
    """
    Windows NTFS USN Journal watcher — inspired by Everything (voidtools).

    Architecture: USN Journal as trigger, snapshot-based discovery.

    The USN Journal tells us *that* something changed (fast, low-overhead),
    and a snapshot comparison discovers *what* changed.  The snapshot is
    built once at startup (or loaded from disk in "deploy" mode), so
    pre-existing files are NEVER synced.

    Falls back to PollingWatcher if USN Journal is inaccessible.
    """

    _DEBOUNCE_SECONDS: float = 1.0

    def __init__(self, watch_path, target_path, **kwargs):
        super().__init__(watch_path, target_path, **kwargs)
        self._volume = self._get_volume(watch_path)
        self._last_usn: int = 0
        self._last_event_time: float = 0.0
        self._scan_scheduled: bool = False

    @staticmethod
    def _get_volume(path: str) -> str:
        p = Path(path)
        if p.drive:
            return p.drive + "\\"
        return "C:"

    @staticmethod
    def is_available(path: str) -> bool:
        if os.name != "nt":
            return False
        try:
            import ctypes
            vol = USNWatcher._get_volume(path)
            handle = ctypes.windll.kernel32.CreateFileW(
                f"\\\\.\\{vol.rstrip('\\')}", 0x80000000,
                3, None, 3, 0x02000000, None
            )
            if handle and handle != -1:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
        except Exception:
            pass
        return False

    def _run_loop(self) -> None:
        """Main event loop — USN polling + debounced scan."""
        if not USNWatcher.is_available(self.watch_path):
            print(f"[watcher:{self.watcher_id}] USN not available, using polling")
            self._run_polling_fallback()
            return

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        vol_path = f"\\\\.\\{self._volume.rstrip(chr(92))}"
        h = kernel32.CreateFileW(
            vol_path, 0x80000000, 3, None, 3, 0x02000000, None
        )
        if h == -1 or not h:
            print(f"[watcher:{self.watcher_id}] Cannot open volume {vol_path}")
            self._run_polling_fallback()
            return

        # Build or load snapshot
        loaded = self._load_snapshot()
        if loaded:
            self._snapshot = loaded
            print(f"[watcher:{self.watcher_id}] Loaded snapshot: "
                  f"{len(loaded)} files (mode={self._snapshot_mode})")
        else:
            self._snapshot = self._build_snapshot()
            self._save_snapshot(self._snapshot)
            print(f"[watcher:{self.watcher_id}] Built snapshot: "
                  f"{len(self._snapshot)} files (mode={self._snapshot_mode})")

        try:
            FSCTL_READ_USN_JOURNAL = 0x000900BB
            USN_REASON_FILE_CREATE = 0x00000100
            USN_REASON_DATA_OVERWRITE = 0x00000002
            USN_REASON_CLOSE = 0x80000000
            USN_REASON_RENAME_NEW_NAME = 0x00001000

            while self._running:
                try:
                    buf = (ctypes.c_ubyte * 65536)()
                    read_req = ctypes.create_string_buffer(40)
                    ctypes.memmove(read_req, ctypes.c_ulonglong(self._last_usn), 8)
                    ctypes.memmove(
                        ctypes.c_void_p(ctypes.addressof(read_req) + 8),
                        ctypes.c_uint(USN_REASON_FILE_CREATE |
                                       USN_REASON_DATA_OVERWRITE |
                                       USN_REASON_CLOSE |
                                       USN_REASON_RENAME_NEW_NAME), 4
                    )
                    ctypes.memmove(
                        ctypes.c_void_p(ctypes.addressof(read_req) + 16),
                        ctypes.c_ulonglong(0), 8
                    )

                    bytes_returned = wintypes.DWORD(0)
                    ret = kernel32.DeviceIoControl(
                        h, FSCTL_READ_USN_JOURNAL,
                        read_req, 40, buf, 65536,
                        ctypes.byref(bytes_returned), None
                    )

                    if ret and bytes_returned.value > 0:
                        self._last_event_time = time.time()
                        self._scan_scheduled = True
                        offset = 0
                        while offset < bytes_returned.value:
                            rec_len = ctypes.c_uint.from_buffer(buf, offset).value
                            if rec_len == 0:
                                break
                            usn = ctypes.c_ulonglong.from_buffer(buf, offset + 24).value
                            if usn > self._last_usn:
                                self._last_usn = usn
                            offset += rec_len

                    if self._scan_scheduled:
                        elapsed = time.time() - self._last_event_time
                        if elapsed >= self._DEBOUNCE_SECONDS:
                            new = self._discover_new_files()
                            for fpath in new:
                                if self._should_sync(Path(fpath).name):
                                    self._sync_file(fpath)
                            self._scan_scheduled = False

                except Exception as e:
                    print(f"[watcher:{self.watcher_id}] USN loop error: {e}")

                time.sleep(1)

        finally:
            kernel32.CloseHandle(h)

    def _run_polling_fallback(self) -> None:
        """Run a PollingWatcher internally when USN is not available."""
        fallback = PollingWatcher(
            self.watch_path, self.target_path,
            sync_documents=bool(DOCUMENT_EXTENSIONS & self._extensions),
            sync_images=bool(IMAGE_EXTENSIONS & self._extensions),
            sync_media=bool(MEDIA_EXTENSIONS & self._extensions),
            sync_archives=bool(ARCHIVE_EXTENSIONS & self._extensions),
            custom_extensions=list(self._extensions - DOCUMENT_EXTENSIONS
                                   - IMAGE_EXTENSIONS - MEDIA_EXTENSIONS
                                   - ARCHIVE_EXTENSIONS),
            on_sync=self._on_sync,
            watcher_id=self.watcher_id + "-fallback",
            exclude_subdirs=list(self._exclude_dirs),
            snapshot_mode=self._snapshot_mode,
        )
        # Share the snapshot so the fallback doesn't re-sync everything
        fallback._snapshot = dict(self._snapshot)
        fallback._running = True
        fallback._run_loop()


# Helpers

def datetime_to_iso(ts: float) -> str:
    """Convert a POSIX timestamp to ISO datetime string."""
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return ""


def local_time_str(ts: float) -> str:
    """文件条目的时间戳 → 本地时间 `YYYY-MM-DD HH:MM`。

    与上面 `datetime_to_iso`（UTC、给机器看的元数据）成对：
    那一个喂 `last_built` / `saved_at`，这一个喂模型和用户看的 `modified=`。

    为什么是本地时间（按分量排）：

    1. 下游会把时区信息丢掉 —— `_file_entry` 只取前 16 个字符
一个不带偏移量的 UTC 串看起来就是本地时间。
       那不是"少了个标记"，那是误导。
    2. 读它的是模型、报给的是用户：模型会照着这个串向用户描述文件时间，
       给错时间等于它编造 —— 判据是先查仪器，再怀疑模型。
    3. 多数派：legacy 扫描那条路本来就是本地（`%Y-%m-%d %H:%M`），
       `skills/mail_parser` / 短期库 / 语义库的时间戳也都是本地。

    本机是单机桌面（不是跨时区服务器），"本地"没有歧义。
    真要支持跨时区，正确做法是保留偏移量，不是换个口径 —— 那是另一件事。

    机械读者：`tests/test_file_time.py`（两条路必须给出同一个串）。
    """
    from datetime import datetime
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return ""


def _resolve_search_path(hint: str) -> Path | None:
    """Resolve a user-provided location hint to an actual filesystem path.

    Supports:
      - Absolute paths: `D:/`, `C:/Users/X/Desktop`
      - Chinese location names mapped to Windows known folders:
        桌面 → Desktop, 下载 → Downloads, 文档 → Documents
    """
    if not hint or hint == "global":
        return None
    # Absolute path
    p = Path(hint)
    if p.is_absolute() or (len(hint) >= 2 and hint[1] == ":"):
        return p if p.exists() else None
    # Chinese → known folder
    _KNOWN_FOLDERS: dict[str, str] = {
        "桌面": "Desktop", "下载": "Downloads", "文档": "Documents",
        "图片": "Pictures", "音乐": "Music", "视频": "Videos",
        "desktop": "Desktop", "downloads": "Downloads",
    }
    mapped = _KNOWN_FOLDERS.get(hint.lower().strip())
    if mapped:
        return Path.home() / mapped
    # Try as subdirectory of home
    candidate = Path.home() / hint
    if candidate.exists():
        return candidate
    return None


def resolve_default_excludes() -> list[str]:
    """Expand `{home}` placeholders in DEFAULT_CROSS_ZONE_EXCLUDE to
    real filesystem paths for the current user.  Only returns paths
    that actually exist on disk (non-existent entries are harmless
    noise in the forbidden list).

    Called by the settings API when populating default forbidden dirs,
    and by `_search_user_home()` to skip these directories during
    cross-zone walks.
    """
    home = str(Path.home())
    resolved: list[str] = []
    for template in DEFAULT_CROSS_ZONE_EXCLUDE:
        if "{home}" in template:
            expanded = template.replace("{home}", home)
        else:
            expanded = template
        if Path(expanded).exists():
            resolved.append(expanded)
    return resolved


# "文件写哪儿"只许有一个口径
#
# ## 为什么必须统一
#
# `copy_to_workspace` 走 `WorkspaceManager.primary_path`，而 `office_parser`
# 8 处、`file_parser` 11 处、`mail_parser` 1 处、`test_weather` 1 处
# 各自硬编码 `Path("workspace")`，同一条链上有两个答案（同一个量
# 两处各算各的，只不过这次分歧的是路径而不是 token 数）。
#
# 没有 `primary: true` 的工作组时不会暴露：`primary_path` 回退成默认
# 工作区，两边碰巧都落在 `workspace/`。
# 一旦用户把某个工作组设成 primary：一个写工作组、一个写 `workspace/`。
#
# 不能一把 sed 全换 —— 这些调用点是三类语义，各自要单独判：
#
# | 类 | 问的是什么 | 用哪个 |
# |---|---|---|
# | 写 | 产物该落在哪 | `primary_output_dir()`（一个答案） |
# | 查 | 这个文件可能在哪儿 | `lookup_roots()`（全部根都试） |
# | 包含性 | 这个路径在不在工作区内 | `SecurityGuard.classify_path`（别自己拼） |
#
# "查"那一类最容易改错：把 `Path("workspace") / name` 直接换成
# `primary_output_dir() / name` 看起来更"统一"，实际是把能找着的东西弄丢
# —— 文件可能在任何一个工作组里，而 primary 只有一个。

def write_outside_subject(resolved) -> str:
    """被审批物是什么 —— 区外写那一道审批问的是"哪个目录"。

    唯一一处：三个读者都问它 ——
      · 产者（`SecurityGuard` 抛 `WriteNeedsApproval` 时算 `subject`）
      · 读者（:func:`write_outside_granted` 的放行判定）
      · 结算器（`pre_process._settle_write_outside` 把 `pend["subject"]` 交给
        `_settle_target`）
    各算一次就会漂，而症状是「批了之后照样弹」——
    两个串一个是 `D:\\out`、一个是 `D:/out`，看着一样却比不相等。

    选父目录而不是"文件自己"：用户看到的选项 2 是「本次对话内不再询问
      此目录」—— 逐字就是"目录"。同一批里往同一个目录写十个文件
      只问一次；写进子目录会再问一次（那是另一个目录，而弹窗如实说了是哪个）。
    """
    try:
        return str(Path(str(resolved)).parent)
    except Exception:                                           # noqa: BLE001
        return ""


def write_outside_granted(resolved) -> bool:
    """这一段对话批过"往这个目录写"没有。

    走的还是那张既有的按目标授权表（`has_target_grant`）——
      `write_outside` 在 `allowlist.SESSION_KINDS` 里
轮首那次投影自动把它带上（不必另写一条投影）。
    它天然只有会话档 —— `write_outside` 不在 `allowlist.KINDS` 里，
      `allowlist.add()` 会拒，它永远写不进持久白名单
      （不提供"永久"档）。
    """
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm is None:
            return False
        subj = write_outside_subject(resolved)
        return bool(subj) and bool(wm.has_target_grant("write_outside", subj))
    except Exception as exc:                                    # noqa: BLE001
        print(f"[workspace] 区外写放行判定失败（按**未批过**处理）："
              f"{type(exc).__name__}: {exc}")
        return False


def default_output_dir() -> Path:
    """默认工作区 —— 那三层回退的唯一一处。

    = `WorkspaceManager.primary_path`（显式 primary 工作组 → 用户设的默认工作区
      → 最后才回退 `./workspace`）。那个属性自己已经带了三层回退，
    这里只是把"怎么拿到 wm"收成一处，不另发明一套回退。

    它不知道任何一段对话 —— "这一段选定的目录"那一层由
      :func:`primary_output_dir` 叠在它上面。

    抽它出来是为了 `lookup_roots()`：那个函数若直接用
      `primary_output_dir()`，而后者跟着对话走，
      "查"那一类就会顺手变成"先在这一段选中的目录里找"。
      `lookup_roots()` 必须保持「默认工作区 + 那组」，索引 / 跨区搜索 /
      `file_parser` 一族全不动，两个调用方各取自己要的那一层，
      回退只有一处。
    """
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm is not None:
            p = getattr(wm, "primary_path", "") or ""
            if p:
                return Path(p)
    except Exception as exc:  # noqa: BLE001 —— 拿不到就回退，**不许弄挂一次工具调用**
        print(f"[workspace] default_output_dir 取不到管理器（回退默认）："
              f"{type(exc).__name__}: {exc}")
    from core import paths as _paths
    return _paths.default_workspace_dir()


def primary_output_dir() -> Path:
    """产物该落在哪 —— 全仓唯一口径。

    这一格跟着对话走 ——
    这一段对话选定了目录就是它，没选就是 :func:`default_output_dir`。

    读的是环境（`core/session_workspace` 那根 `ContextVar`），不是参数
      —— 调用点在 `skills/`，而工具 handler 的签名是 `handler(tool_args)`，
      拿不到 `ctx`、也拿不到 `session_id`。
    取不到（测试夹具直接调、框架没起来、后台线程），回默认，
      不抛 —— 同 `default_output_dir` 那条兜底取向：这里赔不起一次工具调用。
    """
    try:
        from core import session_workspace
        d = session_workspace.current()
        if d:
            return Path(d)
    except Exception as exc:  # noqa: BLE001 —— 同下：取不到就回默认
        print(f"[workspace] primary_output_dir 取不到会话工作目录（回退默认）："
              f"{type(exc).__name__}: {exc}")
    return default_output_dir()


def lookup_roots() -> list[Path]:
    """这个文件可能在哪儿 —— 全部根，按序试（"查"那一类）。

    默认工作区排在最前（用户最可能指的是它），其余工作组按注册序跟在后面。
    去重、保序（同一路径被注册两次时不许出现两次）。

    第一个根是 :func:`default_output_dir` 而不是 `primary_output_dir()`
      —— 见 `default_output_dir` 的注释（"查"不跟着对话走）。
    """
    roots: list[Path] = [default_output_dir()]
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm is not None:
            for wg in (getattr(wm, "workspaces", None) or []):
                p = getattr(wg, "path", "") or ""
                if p:
                    roots.append(Path(p))
    except Exception as exc:  # noqa: BLE001 —— 同上：查不到就是"只有一个根"
        print(f"[workspace] lookup_roots 取不到工作组（只用默认根）："
              f"{type(exc).__name__}: {exc}")
    seen: set = set()
    out: list[Path] = []
    for r in roots:
        k = str(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def find_in_roots(name: str) -> Path | None:
    """在 `lookup_roots()` 里找第一个存在的 `name`；都没有，`None`。

    只按名字找（`root / p.name`）—— 这不是路径解析器，
    是"这个文件名是不是已经在工作区里了"。
    """
    for root in lookup_roots():
        cand = root / name
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    return None
