"""文件原语五件套 —— 编程的底座。

    read_file · write_file · edit_file · glob · grep

## 为什么是它

编程能力的一切都长在这五个原语上。
在它之前一个都没有 —— 碰代码的唯一通路是 `run_command`，而那条通路的代价是四重的：
每一次都过审批 · 输出 4000 字符上限 · 没有"改前先读"的安全网 ·
没有只读探索档。

## 六条纪律（每条都对应一个已经踩过或验证过的坑）

### ① 行尾：匹配前规范化，写回时只 splice

本仓没有 `.gitattributes`，而树是混排的（逐字节
`core/agent_loop.py` / `bus/registry.py` 是 CRLF，
`core/todos.py` / `core/subagent.py` / `core/command_policy.py` 是 LF），
且 `core.autocrlf` 会在 checkout 时把 LF 的文件变成 CRLF。

模型从上下文里抄来的 `old_string` 大概率是 `\\n`，而目标文件是 CRLF ——
逐字节精确匹配会每一次都 0 命中，而症状看起来像"模型抄错了原文"。

所以：把 `old_string` / `new_string` 的行尾规范成目标文件的主行尾之后再匹配。
这不叫降级匹配 —— "只做精确匹配"禁的是"忽略缩进/空白容错"那一类
（那种匹配的代价是改错地方而你察觉不到）；规范化之后仍然是逐字节精确匹配，
一个字都不容错。写回只替换命中那一段（`str.replace(..., 1)`），不整篇重写 ——
混排文件不会被顺手洗成一套行尾。

### ② 写前必读（CAS 乐观锁）

`write_file` / `edit_file` 要求目标先前被读过、且读过之后没被改过
（记 `(st_mtime_ns, st_size)`）。判据同「写前版本校验」：工作区里挂着
watcher（微信目录那类会往里面同步文件），外部改动是真实的，不是理论推演。
新建文件不要求先读（它没得读）。写成功之后把新 stat 记回去 ——
否则"写完紧接着再改一次"会被自己的规则拒掉。

### ③ 纯文本，不套 JSON 信封（`read_file` / `grep`）

这两个的输出是第三人可写的正文，要进 `_ISOLATED_DOC_TOOLS` 被
`<user_document>` 包起来；而JSON 信封被字符截断就是半截 JSON。纯文本截断之后
仍然是可读的正文。
它俩的 handler 返回 `str`；循环里由 `as_text()` 认名单（不是认类型 ——
别的工具有一天返回 `str` 不该被这条改掉行为）。

`read_file` 唯一的例外是"这张文件是一张图"：
那时它返回 `ImageToolResult`（文本 + 图块），由 `_format_tool_result` 认。
那仍然不是"认类型" —— 它是一个具名类，和上面那条判据同源：
`as_text` 认名单、图片认那个类，没有一处是"看着像就当成它"。

### ④ 不跟符号链接

沿用 `PathIndex` 的 `entry.is_dir(follow_symlinks=False)` 判定。根路径本身走
`classify_path` 的 `resolve()`，根是符号链接时按目标分类，不从根上逃逸。

### ⑤ `glob`/`grep` 的根只许是 READ_WRITE 区

一个模式扫描是批量读 —— 放它扫用户家目录，就等于绕开了跨区审批。
只读区拒 + 带路线。

### ⑥ 「跳过哪些条目」分两层，各有各的边界

第一层：前缀规则（`.` / `$` / `_wip_`）由 `core.workspace.skip_walk_entry`
定义，索引与扫描读同一个函数。抄第二份的代价不是多写五行，是两份会漂移 ——
索引里看不见的文件 `grep` 却搜得到（或反过来），而两个答案看着都对。
机械读者钉着它（一条断言两处）。

第二层：扫描范围由 `core.walk_scope` 定义（依赖目录名 + `.gitignore`），
只作用于 `glob` / `grep`。理由：索引面向工作区里的文档，扫描面向任意代码树；
两层判据服务的对象不同，合成一处会让索引跟着代码规则走。
两条配套要求：
- 跳过必须报数（`跳过了 N 个目录/文件`），否则"没看到"会被读成"不存在"；
- 必须有退出开关（`include_ignored=true`），且 `read_file` 等按显式路径的工具
  不受本层管辖。

## 两条写在案的取舍

- 回执里的"还有 N 行"也是框架的话，却和正文一起被包裹（`read_file` / `grep`
  都在隔离名单里）。把它挑到包裹外面要改 `_format_tool_result` 的接线，而那句话里
  不含任何可执行的东西，包错的代价是一句顾问文本被当成数据。
  （同族：检测可以错，打码不能错。）
- `glob`/`grep` 回的是绝对路径，不是相对根的路径。相对路径更短，但它回不到
  另一个原语手里（`read_file` 的相对路径是按 cwd 与默认工作区解析的，而扫描的根
  可能既不是 cwd 也不是工作区），宁可多花字符，也不要一个会指错文件的路径。

## 这不是安全边界

工具面收窄不等于权限：工具面收窄不解决安全隔离；只读靠 prompt；
只读探索工具手里仍有命令执行。
真正的强制力仍然只有 `SecurityGuard` 的路径分级（三级）。
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
from pathlib import Path

from core.image_input import (
    MAX_SOURCE_BYTES,
    SUPPORTED_MEDIA,
    ImageInputError,
    ImageToolResult,
    image_kind_name,
    looks_like_image,
    prepare_path,
)
from core.walk_scope import gitignore_for, skips_dir, skips_file
from core.workspace import skip_walk_entry

# 常量

#: 读进来的上限（超过就拒，不是截断）—— 它防的是"一个 4 GB 的日志被
#: `read_bytes()` 拉进内存"。要看大文件里的一小块：先 `grep` 定位，再 `offset=` 读。
_MAX_READ_BYTES = 16 * 1024 * 1024

#: 判"这是不是二进制"只看开头这么多字节（同 `git` 的做法）。
_BINARY_SNIFF = 8192

#: 扩展名说是图片、内容却对不上时用来把话说准（`read_file` 的二进制那一支）。
#: 它不是"哪些文件是图片"的名单 —— 那个判据是 `looks_like_image`
#: （看魔数，不看扩展名，同 `sniff_media_type` 那条纪律）。
_IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".ico", ".avif", ".heic", ".svg", ".psd",
})

#: 两把刀的方向相反：handler 必须裁到
#: 严格小于循环闸口的值 —— 否则循环那把刀还会再切一次，而它保的是头部。
#:两个常量必须一起改；`_LOOP_BUDGET > _CHAR_CAP` 有断言钉着
#: （`tests/test_file_tools.py`），因为它们住在两个文件的两个函数里。
READ_FILE_CHAR_CAP = 60000        # handler 自己裁到这儿
READ_FILE_LOOP_BUDGET = 65536     # 循环闸给 `read_file` 的上限（覆盖档表那格）

#: 不传 `limit` 时读多少行。
_DEFAULT_READ_LINES = 2000

#: `grep` 单行显示上限 —— minified 的 JS/CSS 一行能到几万字符。
_MAX_LINE_CHARS = 400

#: `grep` 的上下文行数上限。超过这个数的"上下文"通常意味着该直接读那一段。
_MAX_GREP_CONTEXT = 10

#: `grep` 回执的总字符预算。命中数有 `limit` 管，但每命中带 2N 行上下文时
#: 行数会翻几倍 —— 超预算就停，并在回执里说清是预算停的。
_MAX_GREP_CHARS = 20000

#: 一次搜索的墙钟上限（rg 流式读；到点收工并把已拿到的交出去）。
_GREP_TIMEOUT_S = 60.0

#: `grep` 跳过单个文件的字节上限（跳过要报数，见 `_walk_files` 的调用方）。
_MAX_GREP_FILE_BYTES = 8 * 1024 * 1024

#: `edit_file` 的匹配跨度上限 —— 超过就说明"这不是一次编辑，是一次重写"。
_MAX_EDIT_SPAN = 10000

#: 扫描类工具的默认条数 / 内部硬上限（超了要说条数，不是静默截断）。
_DEFAULT_GLOB_LIMIT = 200
_DEFAULT_GREP_LIMIT = 100
_SCAN_HARD_MAX = 20000

#: 回执是纯文本的工具（纪律 ③）。循环里的 `as_text()` 认这个名单。
PLAIN_TEXT_TOOLS: frozenset[str] = frozenset({
    "read_file", "grep", "code_intel",
    # 记忆两件读工具也回纯文本（同一判据 ——
    # 截断之后仍是可读正文，不是半截 JSON）。写件 `memory_write` 回 dict，不在这里。
    "memory_read", "memory_search",
    # 查历史 —— 同一条判据（snippet 行，纯文本）。
    "history_search",
})


# 会话态：读态表（纪律 ②）

#: session_id -> {已解析路径: (st_mtime_ns, st_size)}
#:
#: 与 `bus.registry._discovered` / `core.todos._todos` 逐字同族：
#: 按一个马上要被 `/clear` 复用的 id 键的内存态，purge 也挂在同一个地方
#: （`MemoryManager.new_session`）。那句不变量现在多一条：
#: 新会话没有行、没有笔记、没有已发现工具、没有清单、没有读态。
#:
#: 不清的代价是具体的：下一段对话"看起来"已经读过上一段读过的文件
#:CAS 放行一次它从没做过的写前读，而那正是这道闸门存在的理由。
_reads: dict[str, dict[str, tuple[int, int]]] = {}


def forget_reads(session_id: str) -> None:
    """丢掉某会话的读态。唯一生产调用者在 `MemoryManager.new_session`（`/clear`）。"""
    _reads.pop(session_id or "", None)


def recent_reads(session_id: str, limit: int = 10) -> list[str]:
    """这段对话读过哪些文件（最近的在后，只读、不建表）。

    给压缩摘要用：压掉旧轮次之后，「我读过这个文件」是唯一还值得留下的
    那部分事实 —— 正文可以再读一次，而"读过"这件事没有别的地方记着。
    只回路径：正文重挂会把刚压掉的又塞回去。
    """
    got = _reads.get(session_id or "")
    if not got:
        return []
    paths = list(got.keys())
    return paths[-limit:] if limit else paths


def _stat_key(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def _note_read(session_id: str, path: Path) -> None:
    """记下"这段对话在这个版本上读过它"。写成功之后必须再调一次（纪律 ②）。"""
    try:
        _reads.setdefault(session_id or "", {})[str(path)] = _stat_key(path)
    except OSError:
        pass


def _read_guard(session_id: str, path: Path) -> str | None:
    """纪律 ②：改之前必须"读过 + 此后没被改过"。返回 `None` = 放行。

    三句话分开说，因为下一步的动作不同：没读过，去读；读过了但变了，重读；
    读不到状态，先确认文件还在不在。
    """
    if not path.exists():
        return None                      # 新建：没得读，不要求
    seen = _reads.get(session_id or "", {}).get(str(path))
    if seen is None:
        return ("这个文件**还没有在本会话里读过** —— 先 `read_file` 看一眼再改，"
                "否则你是在按记忆改一份可能已经变了的文件。")
    try:
        now = _stat_key(path)
    except OSError as exc:
        return "读不到它的状态（%s）—— 先 `read_file` 确认它还在。" % exc
    if now != seen:
        return ("这个文件**在读过之后被改过**（大小或修改时间变了）—— "
                "先重新 `read_file` 看一眼再决定怎么改。")
    return None


# 「整块覆盖」闸 —— 给产物用的那一半
#
# 上面那道 `_read_guard` 只服务 `read_file` / `write_file` / `edit_file`
# 三个文本原语。Office 产物（`.xlsx` / `.docx` / `.pptx`）走的是
# openpyxl / python-docx / python-pptx 直读，一条都不经过它
#「两个对话交替改同一份表」在产物这一侧静默发生（谁也不报错）。
#
# 判据：
#
#   | 磁盘上         | 本会话读过它吗 | 读过之后它变了吗 | 结果 |
#   |---------------|--------------|----------------|------|
#   | 不存在         | —            | —              | 放行（新建） |
#   | 存在           | 没读过        | —              | 拒 +「先读一遍」 |
#   | 存在           | 读过          | 变了            | 拒 +「重读一遍」 |
#   | 存在           | 读过          | 没变            | 放行（覆盖） |
#
#   逐字出处不写在这儿 —— 随发布出去的源码里不留外部实现的痕迹。
#
# 没有一家做「因为已存在所以问用户」 —— 审批按路径规则判、
#   从不 `stat` 目标（覆盖一份已存在文件与新建一份受完全相同的权限待遇）
#所以这里是拒，不是问。
#
# 范围只挂「整块覆盖」那一类 —— 界线是：把"内容级匹配"
#   那条编辑的"必须先读"撤了（那本身自带安全），而"整文件覆盖"那条留着。
#我们也不普惠：`add_word_paragraph` 这类自己 load 新鲜版本再改的工具
#   今天不要求先读，别把闸挂到它们头上。


def note_observed(path, session_id: str | None = None) -> None:
    """记下"这段对话看过 / 写过这个文件的这个版本"。

    读侧与写侧共用一张表（`_reads`）—— "每会话观察状态"
      的语义（`present@version`），只是我们压在既有的 `_reads` 上
      （一个口径一个地方，不新开第二张）。
    写成功之后也要调它（纪律 ② 的产物版）：否则模型自己刚写过一遍，
      下一次写会拿写之前的版本去比，误判成"被别人改过"。
    `session_id=None`，取当前请求快照（`_sid()`），供 skill 侧一行调用。
    """
    try:
        _note_read(_sid() if session_id is None else session_id, Path(path))
    except Exception as exc:                                  # noqa: BLE001
        # 记账失败不许影响读取本身（同 `security._note_touched` 的规矩）
        print("[observe] 读态记账失败（不影响本次读写）：%s: %s"
              % (type(exc).__name__, exc))


def overwrite_guard(path, *, reader: str = "", session_id: str | None = None):
    """整块覆盖一个可能已存在的文件之前，问一句「你这个读数还新鲜吗」。

    返回 `None` = 放行；返回 `{"error": ..., "hint": ...}` = 拒绝（带路线）。
    调用方直接 `return {"success": False, **blocked}`。

    *reader* = 建议模型用的那个读工具名（`get_document_text` /
    `read_excel_data` …）—— 路线必须兑现得了：那个名字得真存在。
    """
    p = Path(path)
    try:
        if not p.exists():
            return None                    # 新建：没得读，放行
    except OSError:
        return None
    sid = _sid() if session_id is None else (session_id or "")
    seen = _reads.get(sid, {}).get(str(p))
    do_read = ("先 `%s('%s')` 看一眼" % (reader, p)) if reader else "先读一眼它"
    if seen is None:
        return {
            "error": ("目标已存在：`%s` —— 本会话**还没读过它**，"
                      "直接写会把里面的内容**整个盖掉**。" % p),
            "hint": "%s再写，或者换一个输出名（`output_name`）。" % do_read,
        }
    try:
        now = _stat_key(p)
    except OSError as exc:
        return {
            "error": "读不到 `%s` 的状态（%s）—— 先确认它还在。" % (p, exc),
            "hint": do_read,
        }
    if now != seen:
        return {
            "error": ("`%s` **在你读过之后被改过**（大小或修改时间变了）—— "
                      "直接写会盖掉那次改动。" % p),
            "hint": "先重新 %s再决定怎么改。" % do_read,
        }
    return None


# 路径与编码

def _guard():
    """取安全核 + 工作区管理器。拿不到就抛（不是放行）。"""
    from bus.registry import get_workspace_manager

    wm = get_workspace_manager()
    guard = getattr(wm, "security", None)
    if guard is None:
        raise RuntimeError("SecurityGuard 还没初始化 —— 文件原语不在无安全核时跑")
    return guard, wm


def _resolve(file_path: str, wm) -> Path:
    """相对路径的名词落成绝对路径。

    形状采 `skills/file_parser/handler.py::_resolve_path`（先按原样，再试工作区），
    两处与它不同，都是有意的：

    1. 它试的是 `Path("workspace") / p.name` —— 丢掉目录部分。
       `read_file("src/main.py")` 于是可能读到 `workspace/main.py`：
       一个同名但完全不同的文件，而调用方不会发现。这里保留完整相对路径。
    2. 它硬编码 `Path("workspace")` —— 那正是「文件写哪儿有两套实现」。
       这里问 `WorkspaceManager.primary_path`（唯一的默认落点口径）。
    """
    p = Path(file_path)
    if p.exists():
        return p.resolve()
    if not p.is_absolute():
        base = getattr(wm, "primary_path", "") or ""
        if base:
            alt = Path(base) / p
            try:
                if alt.exists():
                    return alt.resolve()
            except OSError:
                pass
    # 都不在 —— 落回原样，交给 `classify_path` 报一句清楚的话
    return p.resolve()


def _decode(raw: bytes) -> tuple[str, str, bool]:
    """`(文本, 编码名, 有没有替换过)`。

    UTF-8 → GBK → 带替换的 UTF-8。两个兜底都要出声（`replace=True` 的那次尤其：
    替换过的字节是不可逆的，调用方有权知道它在看一份被改过的文本）。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc, False
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8", True


def _dominant_eol(raw: bytes) -> str:
    """这个文件的主行尾。纪律 ①：本仓是混排的，所以这条必须现算，不能假设。"""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n")
    return "\r\n" if crlf and crlf * 2 >= lf else "\n"


def _to_eol(text: str, eol: str) -> str:
    """把 *text* 的行尾统一成 *eol*（孤立 `\\r` 也算行尾）。"""
    norm = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return norm if eol == "\n" else norm.replace("\n", eol)


def _eol_name(eol: str) -> str:
    return "CRLF" if eol == "\r\n" else "LF"


def _posix(path: Path) -> str:
    """回执里的路径一律用正斜杠（同 `agent_loop._truncate_to_artifact` 的口径）。"""
    return str(path).replace("\\", "/")


# 走目录树（`glob` / `grep` 共用）

def _iter_files(root: Path, *, include_ignored: bool = False):
    """走 *root* 下的所有文件，产出 `([(绝对 Path, 相对 posix 串)], 跳过读数)`。

    前缀规则与索引共用 `core.workspace.skip_walk_entry`（纪律 ⑥）。
    扫描另加两层（`core.walk_scope`）：固定目录名与 `.gitignore`；
      `include_ignored=True` 时两层关闭，只剩前缀规则。
    不跟符号链接（纪律 ④）：目录不进、文件不产。
    逐层排序 —— 文件系统给的顺序是不确定的，而"同一条命令两次给不同的答案"
      会让排查变成猜谜（`test_file_tools` 也靠它才能钉住顺序）。
    坏目录 / 权限错误一律跳过（探针不许自己崩）。
    返回列表不是生成器：调用方要按 hard-max 截断，而"边走边判"会让
      截断点随文件系统顺序漂移（同一棵树上两次调用截到不同的 200 条）。
    跳过读数必须回到回执里：被忽略的目录与"不存在"是两件事。
    """
    out: list[tuple[Path, str]] = []
    stats = {"dirs": 0, "files": 0}
    stack: list[tuple[Path, str, tuple]] = [(root, "", ())]
    seen: set[str] = set()
    while stack:
        cur, prefix, rules = stack.pop()
        try:
            key = str(cur.resolve())
        except OSError:
            continue
        if key in seen:            # Junction / 硬链接环（Windows 上真实存在）
            continue
        seen.add(key)
        if not include_ignored:
            rules = rules + gitignore_for(cur, prefix)
        try:
            with os.scandir(cur) as entries:
                items = sorted(entries, key=lambda e: e.name)
        except OSError:
            continue
        for entry in items:
            if skip_walk_entry(entry.name):
                continue
            rel = prefix + entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not include_ignored and skips_dir(rules, entry.name, rel):
                        stats["dirs"] += 1
                        continue
                    stack.append((Path(entry.path), rel + "/", rules))
                elif entry.is_file(follow_symlinks=False):
                    if not include_ignored and skips_file(rules, rel):
                        stats["files"] += 1
                        continue
                    out.append((Path(entry.path), rel))
            except OSError:
                continue
    return out, stats


def _glob_match(parts: tuple[str, ...], pats: tuple[str, ...]) -> bool:
    """`**` 感知的逐段 glob 匹配（每一段交给 `fnmatch`）。

    为什么不直接用 `Path.glob` / `glob.glob`：那两个不认我们的跳过规则
    （纪律 ⑥ 的三前缀），也不认"不跟符号链接"（纪律 ④），而且 `**` 的语义
    还得自己兜 —— `` 匹配零段或多段（`a//b` 必须能匹配 `a/b`，
    这是标准 glob 的定义，也是模型最容易写出来的那种模式）。
    """
    if not pats:
        return not parts
    head = pats[0]
    if head == "**":
        # `**` 匹配零段：跳过它试剩下的模式
        if _glob_match(parts, pats[1:]):
            return True
        # 或者吃掉一段再试
        return bool(parts) and _glob_match(parts[1:], pats)
    if not parts:
        return False
    if not fnmatch.fnmatch(parts[0], head):
        return False
    return _glob_match(parts[1:], pats[1:])


def matches(pattern: str, rel_path: str) -> bool:
    """一条规则管两个工具：模式里有 `/` 就锚定（匹配相对路径），
    没有就匹配文件名（任意层级）。

    这是 ripgrep / gitignore 的惯例，故意不采 `pathlib` 的"一律锚定"：
    `glob("*.py")` 在 pathlib 下只扫顶层，而九成调用者要的是"这个项目里所有 `.py`"。
    更要紧的是 `grep` 的 `glob=` 参数 —— 那里两个惯例都有人用，
    而"这次是哪一套"是调用者看不见的东西。一个规则，两个工具，写在两处
    schema 的 description 里。
    """
    pats = tuple(s for s in str(pattern).replace("\\", "/").split("/") if s)
    if not pats:
        return False
    parts = tuple(rel_path.split("/"))
    if "/" in str(pattern):
        return _glob_match(parts, pats)
    return fnmatch.fnmatch(parts[-1], pats[-1])


# 扫描类工具共用的根判定（纪律 ⑤）

def _scan_root(root, guard, wm) -> tuple[Path | None, dict | None]:
    """把 `root=` 落成一个只许是 READ_WRITE 区的根。返回 `(路径, 错误体)`。

    三档（`classify_path` 现算，不新建第三份"工作区根列表" ——
    `build_index` 里那份是局部变量、`_reload_security_workspaces` 里还有一份，
    再抄一份就是第三套真相源）：

    | 根分类 | 行为 |
    |---|---|
    | `READ_WRITE` | 走 |
    | `READ_ONLY`（不在规避清单里的其余路径 —— 含用户家目录 / 框架制品） | 拒 + 带路线 |
    | `FORBIDDEN`（系统目录 / 框架自己的密钥与账本） | 报错 |
    """
    from core.security import PathLevel

    if root in (None, ""):
        base = getattr(wm, "primary_path", "") or ""
        if not base:
            return None, {"success": False,
                          "error": "没有可用的默认工作区 —— 请显式给 `root=`。"}
        path = Path(base)
    else:
        try:
            path = _resolve(str(root), wm)
        except Exception as exc:
            return None, {"success": False, "error": "路径解析失败（%s）。" % exc}

    if not path.exists():
        return None, {"success": False, "error": "根目录不存在：`%s`。" % _posix(path)}
    if not path.is_dir():
        return None, {"success": False,
                      "error": "`%s` 是**文件**不是目录 —— `root=` 要一个目录。"
                               % _posix(path)}

    _resolved, level = guard.classify_path(str(path))
    if level == PathLevel.READ_WRITE:
        return path, None
    if level == PathLevel.READ_ONLY:
        return None, {
            "success": False,
            "error": ("`%s` 在**只读区**（不在规避清单里的可读处，含用户家目录）—— "
                      "一个模式扫描是**批量读**，从这里扫就等于绕开跨区审批。"
                      % _posix(path)),
            "hint": ("跨区找文件用 `request_cross_zone_search`（它会走审批）；"
                     "要看**某一个**具体文件，直接 `read_file` 是允许的。"),
        }
    # 措辞要点名真正的禁止区：兜底是 READ_ONLY，
    #    `core/` 与 `data/` 的绝大部分已经是只读区，
    #    只有回避清单那几条还是禁止区。标签不许说谎。
    return None, {"success": False,
                  "error": ("`%s` 在**禁止区**（系统目录；以及框架自己的密钥与账本："
                            "`.env` / `data/settings.json` / `data/memory/` / `logs/`）。"
                            % _posix(path))}


def _as_int(value, name: str):
    """把 schema 说的 integer 收干净。坏值明说，不静默塌成默认值
    （参数坏了不许跑，否则模型看到的是一次"读了别处"的结果）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("%s 要是整数，收到的是 %r" % (name, value))


# 1. read_file

def _read_image(file_path: str, path: Path, media: str, size: int):
    """`read_file` 撞上一张图片时走的那一支。

    返回的是 `ImageToolResult`（不是 `str`）—— 循环里由
    `core.agent_loop._format_tool_result` 认它：文本走截断 /
    `<user_document>` 包裹，图片块在包裹之外。四个返回出口里
    三条是"说得出路线"的错误串、一条是真结果。

    错误文案一律走 `ImageInputError.brief`（给对话那一份）——
    `detail` 是给控制台的，框架自己的诊断不进模型的上下文
    （一份排障指南会让模型跑去找工具修）。
    """
    if media not in SUPPORTED_MEDIA:
        return ("错误：`%s` 是一张 **%s** 图片，而这里只收 %s 四种。\n"
                "提示：让用户转成 PNG / JPEG 再发；或者你先用 "
                "`convert_image_format` 转一份再读它。"
                % (file_path, image_kind_name(media),
                   " / ".join(image_kind_name(m) for m in SUPPORTED_MEDIA)))
    if size > MAX_SOURCE_BYTES:
        return ("错误：图片有 %.1f MB，超过能处理的 %d MB。\n"
                "提示：先用 `compress_image` 压一下，或者让用户裁小一块再读。"
                % (size / 1e6, MAX_SOURCE_BYTES // (1024 * 1024)))

    try:
        prepared = prepare_path(path, source="read_file 读的 %s" % path.name)
    except ImageInputError as exc:
        return "错误：读不了这张图 —— %s" % exc.brief

    # 纪律 ②（写前必读的 CAS）：读到了就记账 —— 这一支确实把文件内容
    # 交出去了（图块就是内容）。不记的话，"先 read_file 再 write_file"
    # 那条链会在图片文件上断掉，而症状看起来像"我没读过它"。
    _note_read(_sid(), path)
    return ImageToolResult(
        text="（这是一张图片 —— 内容在下面的图块里，不在上面这段文字里。）",
        prepared=[prepared],
    )


def _note_touched(path) -> None:
    """记下"这一段碰过这个目录"，下一轮起那几层的指令也进系统提示词。

    顺带的一笔，绝不抛（`leaf_md.note_touched` 自己也不抛）。
    """
    try:
        from core import leaf_md
        leaf_md.note_touched(path)
    except Exception:                                       # noqa: BLE001
        pass


def read_file(file_path: str = "", offset=None, limit=None) -> str:
    """读一个文本文件（行号口径分页）。回执是纯文本（纪律 ③）。

    行号前缀用 `%6d\\t`（同 `cat -n` / `grep -n` 的形状）—— 它是模型最认的写法，
    而 `edit_file` 报"这段原文出现在第几行"时也要用同一把尺子。
    """
    if not file_path:
        return "错误：没有给 file_path。"

    try:
        start = 1 if offset is None else int(offset)
        take = _DEFAULT_READ_LINES if limit is None else int(limit)
    except (TypeError, ValueError):
        return ("错误：offset / limit 要是整数（收到 offset=%r, limit=%r）。"
                % (offset, limit))

    try:
        guard, wm = _guard()
    except Exception as exc:
        return "错误：%s" % exc
    try:
        path = _resolve(str(file_path), wm)
    except Exception as exc:
        return "错误：路径解析失败（%s）。" % exc

    _note_touched(path)
    try:
        guard.validate_path(str(path), "read")
    except Exception as exc:
        return ("错误：%s\n提示：工作区里的文件可以直接读；跨区的具体文件也允许读，"
                "但**按模式扫描**要走 `request_cross_zone_search`。" % exc)

    if not path.exists():
        return ("错误：找不到 `%s`。\n提示：`glob('**/*<关键词>*')` 可以按名字找。"
                % file_path)
    if path.is_dir():
        return ("错误：`%s` 是**目录**不是文件。\n提示：列目录用 `glob('*', root='%s')`。"
                % (file_path, file_path))

    try:
        size = path.stat().st_size
    except OSError as exc:
        return "错误：读不到它的状态（%s）。" % exc

    # 图片分支
    #
    # "模型自己把图读进来"是一格基本能力：没有它，用户说
    # 「看看 `workspace/xxx.png`」时，模型唯一的路是 `run_command` + 猜。
    #
    # 不新开一个工具：「文本与图片同一件」是常见做法，而新开一个
    # 常驻工具 = 每请求多付一份 schema 的永久税（加工具先算这笔账）。
    #
    # 它必须排在文本那条上限之前：图片的上限是 `MAX_SOURCE_BYTES`
    # （20 MiB），比 `_MAX_READ_BYTES`（16 MiB）宽，先过文本闸的话，
    # 一张 18 MiB 的截图会拿到「超过单次读取上限 16 MB」——而那是句谎话
    # （图片这条路的上限更高，而且它压根不该走那条路）。
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        return "错误：读失败（%s）。" % exc

    media = looks_like_image(head)
    # SVG 是文本，不是图片。
    #   它的"魔数"就是 `<svg` / `<?xml`，而 Pillow 不认 SVG，送进图片管线
    #   只会拿到「只收 PNG/JPEG/WebP/GIF 四种」；连带 `write_file` 的
    #   「写前必读」也过不去（`_read_image` 根本没走到 `_note_read`）
    #改一页 SVG 只能写临时文件 + `run_command mv` 绕道。
    #到这儿直接落到下面的文本路径（读得到、也记得上账）。
    # `looks_like_image` 对任何 `<?xml` 开头的东西都回 `image/svg+xml`
    #   （它判不出别的 XML），这一支顺带把普通 XML 也放回文本路径 —— 那是对的。
    if media is not None and media != "image/svg+xml":
        return _read_image(file_path, path, media, size)

    if size > _MAX_READ_BYTES:
        return ("错误：文件有 %.1f MB，超过单次读取上限 %d MB。\n"
                "提示：先用 `grep` 定位要看的那几行，再用 `offset=` / `limit=` 读那一段。"
                % (size / 1e6, _MAX_READ_BYTES // (1024 * 1024)))

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return "错误：读失败（%s）。" % exc

    if b"\x00" in raw[:_BINARY_SNIFF]:
        # 止血：从前这里一律说「看起来是二进制文件」—— 诚实，
        # 但没路线。上面那条 `looks_like_image` 已经把"认得出的图"接走了，
        # 所以到这儿的是真认不出的东西；仍然要把「扩展名说是图」
        # 这一支单独说出来（那是用户最可能踩的一格）。
        hint = ""
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            hint = ("⚠️ 它的扩展名是 `%s`，但文件头对不上任何图片格式 —— "
                    "**别把它当图片**（可能是个没下完的文件，或者后缀被改过）。\n"
                    % path.suffix.lower())
        return ("错误：`%s` 看起来是**二进制文件**（内容里有空字节）。\n"
                "%s提示：用对应的解析工具读它（`parse_docx` / `read_excel_data` 等）；"
                "或者先用 `run_command` 跑 `file` 看一眼它到底是什么。"
                % (file_path, hint))

    text, enc, replaced = _decode(raw)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    total = len(lines)

    start = max(1, start)
    end = min(total, start - 1 + max(1, take))

    out: list[str] = []
    used = 0
    idx = start - 1
    while idx < end:
        row = "%6d\t%s" % (idx + 1, lines[idx])
        if used + len(row) + 1 > READ_FILE_CHAR_CAP:
            break
        out.append(row)
        used += len(row) + 1
        idx += 1

    head = ""
    if replaced:
        head = "（注意：解码时有字节对不上，已用替换字符 —— 看到的不是逐字节原文）\n"
    if not out:
        return head + "（这个范围内没有内容 —— 文件共 %d 行）" % total

    body = head + "\n".join(out)
    if idx < total:
        body += ("\n\n（下面还有 %d 行没显示 —— 用 offset=%d 接着读）"
                 % (total - idx, idx + 1))

    # 纪律 ②：读到了就记账。只有真把内容交出去了才记 ——
    # 一个被拒的读不该让"写前必读"那道闸以为它读过了。
    _note_read(_sid(), path)
    return body


# 2. write_file

def write_file(file_path: str = "", content=None) -> dict:
    """整篇写一个文本文件。新建不要求先读；覆盖已有文件要求（纪律 ②）。"""
    if not file_path:
        return {"success": False, "error": "没有给 file_path。"}
    if content is None:
        content = ""

    session_id = _sid()
    try:
        guard, wm = _guard()
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    try:
        path = _resolve(str(file_path), wm)
    except Exception as exc:
        return {"success": False, "error": "路径解析失败（%s）。" % exc}

    existed = path.exists()
    if existed and path.is_dir():
        return {"success": False,
                "error": "`%s` 是**目录** —— write_file 写的是文件。" % _posix(path)}
    if existed:
        blocked = _read_guard(session_id, path)
        if blocked:
            return {"success": False, "error": blocked,
                    "hint": "先 `read_file('%s')`。" % file_path}

    _note_touched(path)
    try:
        guard.validate_path(str(path), "write")
    except Exception as exc:
        return {"success": False, "error": str(exc),
                "hint": "写只能在 READ_WRITE 区（默认工作区 / 工作组）里做。"}

    # 纪律 ①：覆盖已有文件时沿用它的行尾（新建文件原样写 —— 那是调用方的选择）
    eol = "\n"
    if existed:
        try:
            eol = _dominant_eol(path.read_bytes())
        except OSError:
            eol = "\n"
    payload = _to_eol(str(content), eol)
    data = payload.encode("utf-8")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        return {"success": False, "error": "写失败（%s）。" % exc}

    _note_read(session_id, path)          # 纪律 ②：写完把新 stat 记回去
    print("[file] write_file %s (%d bytes, %s)"
          % (_posix(path), len(data), "覆盖" if existed else "新建"))

    lines = payload.count("\n") + (0 if payload.endswith("\n") or not payload else 1)
    out = {
        "success": True,
        "path": _posix(path),
        "bytes": len(data),
        "lines": lines,
        "created": not existed,
    }
    if existed and payload != str(content):
        # 只在真的动过行尾时出声（否则每次都多一行噪声）
        out["note"] = ("原文件是 %s 行尾，已按它写入（你的内容里是另一种）。"
                       % _eol_name(eol))
    _attach_diagnostics(path, out)
    return out


# 3. edit_file

def _hit_lines(text: str, needle: str) -> list[int]:
    """*needle* 每次出现所在的行号（1 起）—— "改哪一处是猜的"时给模型看的。"""
    out: list[int] = []
    at = 0
    while True:
        i = text.find(needle, at)
        if i < 0:
            return out
        out.append(text.count("\n", 0, i) + 1)
        at = i + len(needle)


def edit_file(file_path: str = "", old_string=None, new_string=None,
              replace_all=None) -> dict:
    """一次精确替换。默认要求 `old_string` 在文件里只出现一次。

    三条拒绝（每条都带路线，且文件一个字节都不动）：

    | 情形 | 行为 |
    |---|---|
    | 0 命中 | 拒 + 路线：逐字符精确（含缩进），先 `read_file` 抄原文 |
    | >1 命中 | 拒 + 列出命中行号，要么带更多上下文、要么 `replace_all=true` |
    | 跨度超限 | 拒 + 路线：用 `write_file` 整篇写 |

    `replace_all` 是留口不是默认 —— 默认行为仍是"一次一处"，且 >1 命中时
    先拒（改错地方而察觉不到的代价，比多问一次高）。
    """
    if not file_path:
        return {"success": False, "error": "没有给 file_path。"}
    old_string = "" if old_string is None else str(old_string)
    new_string = "" if new_string is None else str(new_string)

    if not old_string:
        return {"success": False,
                "error": "old_string 是空的 —— 精确匹配要一段**逐字符**的原文。",
                "hint": "先 `read_file` 把要改的那段抄进来（含行首缩进）。"}
    if old_string == new_string:
        return {"success": False,
                "error": "old_string 与 new_string 一模一样 —— 这次编辑什么都不会改。"}
    if len(old_string) > _MAX_EDIT_SPAN:
        return {"success": False,
                "error": ("old_string 有 %d 个字符，超过单次编辑上限 %d —— "
                          "这不是一次编辑，是一次重写。" % (len(old_string), _MAX_EDIT_SPAN)),
                "hint": "改用 `write_file` 整篇写。"}

    session_id = _sid()
    try:
        guard, wm = _guard()
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    try:
        path = _resolve(str(file_path), wm)
    except Exception as exc:
        return {"success": False, "error": "路径解析失败（%s）。" % exc}

    if not path.exists():
        return {"success": False, "error": "找不到 `%s`。" % file_path,
                "hint": "要**新建**文件用 `write_file`（它不要求先读）。"}
    if path.is_dir():
        return {"success": False,
                "error": "`%s` 是**目录**。" % _posix(path)}

    blocked = _read_guard(session_id, path)
    if blocked:
        return {"success": False, "error": blocked,
                "hint": "先 `read_file('%s')`。" % file_path}

    _note_touched(path)
    try:
        guard.validate_path(str(path), "write")
    except Exception as exc:
        return {"success": False, "error": str(exc),
                "hint": "改只能在 READ_WRITE 区里做。"}

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"success": False, "error": "读失败（%s）。" % exc}
    if b"\x00" in raw[:_BINARY_SNIFF]:
        return {"success": False,
                "error": "`%s` 是二进制文件 —— edit_file 只改文本。" % file_path}

    text, enc, replaced_bytes = _decode(raw)
    eol = _dominant_eol(raw)

    # 纪律 ① 的落点：把 old/new 的行尾规范成这个文件的主行尾，之后
    # 仍然是逐字节精确匹配。不做这一步，本仓里 CRLF 的文件每一次都 0 命中。
    old_eol, new_eol = _to_eol(old_string, eol), _to_eol(new_string, eol)
    count = text.count(old_eol)
    used_eol = eol

    if count == 0:
        # 混排文件的兜底：这一处用的是另一种行尾。
        # 这不是降级匹配 —— 另一种行尾也是一次逐字节精确匹配，
        # 只是先试的那一种没中。而且要说出来（静默换口径就是另一种撒谎）。
        other = "\n" if eol == "\r\n" else "\r\n"
        alt_old, alt_new = _to_eol(old_string, other), _to_eol(new_string, other)
        if alt_old != old_eol:
            alt_count = text.count(alt_old)
            if alt_count:
                count, used_eol = alt_count, other
                old_eol, new_eol = alt_old, alt_new

    # 引号排版变体的第二次尝试
    #
    # 判据：弯引号 / 全角引号是同一个引号的排版变体，不是"忽略差异"。
    #   只在逐字节匹配 0 命中之后做，且仍然要求唯一命中、改的仍是原文那一段。
    #    否决的是"忽略空白/缩进"，这条不在那条的射程里。
    if count == 0:
        folded = _fold_quotes(text)
        folded_old = _fold_quotes(old_eol)
        # 两边任一有排版变体才值得再试；都没有，折完还是刚才那一次逐字节匹配。
        if folded_old and (folded_old != old_eol or folded != text):
            n_fold = folded.count(folded_old)
            if n_fold == 1:
                idx = folded.find(folded_old)
                actual = text[idx:idx + len(folded_old)]
                new_text = text[:idx] + new_eol + text[idx + len(actual):]
                return _finish_edit(
                    file_path, path, session_id, new_text, enc, replaced_bytes, 1,
                    eol, new_eol,
                    note=("原文用的是引号的另一种排版（弯引号 / 全角），"
                          "已按原样替换那一段；新文本按你给的原样写入。"))
            if n_fold > 1:
                return {"success": False,
                        "error": ("这段原文按引号排版归一后有 **%d 处**候选 —— "
                                  "改哪一处是猜的。" % n_fold),
                        "hint": ("带上更多上下文让它唯一；或者把原文里的引号"
                                 "改成文件里实际用的那一种再传。")}

    # 渲染产物：行号前缀 / 转义写法
    #
    # 这两类也不是"容错"：它们是把文件的渲染结果当成了文件本身
    #   （`read_file` 带行号、模型把 `\n` 写成两个字面字符）。
    #   仍然要求唯一命中；否决的"忽略空白/缩进"不在这里。
    if count == 0:
        for _why, _variant, _fold in (
            ("行号前缀", _strip_line_number_prefixes(old_eol),
             _strip_line_number_prefixes),
            ("转义写法", _unescape_visible(old_eol), _unescape_visible),
        ):
            if not _variant or _variant == old_eol:
                continue
            # 变体是在规范化之前算的（剥前缀要看到 `\n`），
            #   而对齐文件行尾这一步必须补上 —— 否则 CRLF 文件永远 0 命中。
            _variant = _to_eol(_variant, used_eol)
            n_v = text.count(_variant)
            if n_v > 1:
                return {"success": False,
                        "error": ("这段原文去掉%s后有 **%d 处**候选 —— "
                                  "改哪一处是猜的。" % (_why, n_v)),
                        "hint": "带上更多上下文让它唯一，或者传文件里实际的内容。"}
            if n_v != 1:
                continue
            idx = text.find(_variant)
            actual = text[idx:idx + len(_variant)]
            new_used = _to_eol(_fold(new_eol) or new_eol, used_eol)
            new_text = text[:idx] + new_used + text[idx + len(actual):]
            return _finish_edit(
                file_path, path, session_id, new_text, enc, replaced_bytes, 1,
                eol, new_used,
                note=("原文带着%s，已按文件里的实际内容匹配并替换"
                      "（写进去的新文本也去了同一层）。" % _why))

    if count == 0:
        return {"success": False,
                "error": ("在 `%s` 里找不到这段原文（**逐字符**精确匹配，含缩进与标点）。"
                          % file_path),
                "hint": ("先 `read_file('%s')` 把那段**原样**抄出来（注意行首的空格/"
                         "制表符）；要新建内容用 `write_file`。" % file_path)}

    if count > 1 and not replace_all:
        where = _hit_lines(text, old_eol)
        return {"success": False,
                "error": ("这段原文在 `%s` 里出现了 **%d 次**（%s）—— "
                          "只改一处的话，改哪一处是猜的。"
                          % (file_path, count,
                             "、".join("行 %d" % n for n in where[:20]))),
                "hint": ("带上更多上下文（前后各一两行）让它唯一；"
                         "确实要**全部改掉**就传 `replace_all=true`。")}

    if replace_all:
        new_text, n = text.replace(old_eol, new_eol), count
    else:
        new_text, n = text.replace(old_eol, new_eol, 1), 1

    return _finish_edit(file_path, path, session_id, new_text, enc,
                        replaced_bytes, n, eol, new_eol,
                        used_eol=used_eol)


#: 引号的排版变体 → ASCII 引号。只收"同一个引号的另一种写法"；
#: 中文的「」『』是另一种标点，不在表里（折了就是改语义）。
_QUOTE_FOLD = str.maketrans({
    "“": '"', "”": '"', "＂": '"',
    "‘": "'", "’": "'", "＇": "'",
})


#: `read_file` 的渲染前缀：若干空白 + 行号 + 制表符/冒号/空格。
_LINE_PREFIX_RE = re.compile(r"^[ \t]*\d+[\t: ] ?")

#: 可见的转义写法（模型把 `\n` 当字面量抄进 old_string 的情况）。
_ESCAPE_RE = re.compile(r"\\(n|t|r|\"|'|`)")
_ESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "`": "`"}


def _strip_line_number_prefixes(text: str) -> str:
    """剥掉每行开头的渲染行号。所有非空行都带前缀时才算，否则返回空串。"""
    lines = text.split("\n")
    out, hit = [], 0
    for line in lines:
        m = _LINE_PREFIX_RE.match(line)
        if m:
            out.append(line[m.end():])
            hit += 1
        else:
            out.append(line)
    nonempty = sum(1 for line in lines if line.strip())
    if hit < 2 or hit < nonempty:
        return ""
    return "\n".join(out)


def _unescape_visible(text: str) -> str:
    """把 `\n` / `\t` / `\"` 这类字面写法还原成真字符。没变化返回空串。"""
    out = _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(1)], text)
    return out if out != text else ""


def _fold_quotes(text: str) -> str:
    """把引号的排版变体折成 ASCII。长度不变（逐字符一对一）——
折叠后找到的位置可以直接映射回原文。"""
    return text.translate(_QUOTE_FOLD)


def _finish_edit(file_path, path, session_id, new_text, enc, replaced_bytes,
                 n, eol, new_used, *, used_eol=None, note="") -> dict:
    """替换已经算好之后的唯一落盘出口（两条匹配路径共用）。

    `new_used` 是真正写进去的那一段（回执里的行号靠它算）；它与 `new_eol`
    只在引号归一那条路上不同。
    """
    try:
        path.write_bytes(new_text.encode(enc, errors="replace"))
    except OSError as exc:
        return {"success": False, "error": "写失败（%s）。" % exc}

    _note_read(session_id, path)          # 纪律 ②

    print("[file] edit_file %s (%d 处, %s)"
          % (_posix(path), n, _eol_name(used_eol or eol)))

    out = {
        "success": True,
        "path": _posix(path),
        "replaced": n,
        "lines": _hit_lines(new_text, new_used) if (n == 1 and new_used) else None,
    }
    notes = [x for x in (note,) if x]
    if used_eol is not None and used_eol != eol:
        notes.append("这一处用的是 %s 行尾，与文件主行尾（%s）**不一致** —— "
                     "已按它原样改了这一处，别的地方没动。"
                     % (_eol_name(used_eol), _eol_name(eol)))
    if notes:
        out["note"] = " ".join(notes)
    if replaced_bytes:
        out["warning"] = ("这个文件解码时有字节对不上（已用替换字符），"
                          "写回去会**不可逆地**改掉那几个字节。")
    _attach_diagnostics(path, out)
    return out


# 4. glob

def glob(pattern: str = "", root=None, limit=None,
         include_ignored=None) -> dict:
    """按模式找文件。返回绝对路径（理由见模块 docstring 的取舍②）。"""
    if not pattern:
        return {"success": False, "error": "没有给 pattern。",
                "hint": "例：`**/*.py`（任意层级）· `*.md`（任意层级里叫这个名字的）· "
                        "`src/**/*.py`（锚在 src 下）。"}
    try:
        cap = _as_int(limit, "limit") or _DEFAULT_GLOB_LIMIT
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    try:
        guard, wm = _guard()
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    base, err = _scan_root(root, guard, wm)
    if err is not None:
        return err

    files, skipped = _iter_files(base, include_ignored=bool(include_ignored))
    hits: list[str] = []
    for p, rel in files:
        if matches(pattern, rel):
            hits.append(_posix(p))
            if len(hits) >= _SCAN_HARD_MAX:
                break
    hits.sort()

    truncated = len(hits) > cap
    shown = hits[:cap]
    out = {
        "success": True,
        "root": _posix(base),
        "count": len(shown),
        "files": shown,
    }
    if truncated:
        out["truncated"] = True
        out["note"] = ("只回了前 %d 条（模式里加上更具体的目录或后缀能收窄）。" % cap)
    elif not shown:
        out["note"] = ("这个根下没有匹配的文件。根是 `%s`（扫了 %d 个条目）。"
                       % (_posix(base), len(files)))
    skip_note = _skipped_note(skipped)
    if skip_note:
        out["skipped"] = skip_note
    return out


def _attach_diagnostics(path: Path, out: dict) -> None:
    """写完之后顺带取一次语言服务器诊断。取不到就什么都不加。

    三条边界：
      ① 只有配置了语言服务器、且这个扩展名有服务器时才会有内容（否则 `diagnose`
         直接返回 `None`，连进程都不起）；
      ② 任何失败都不影响这次写入 —— 诊断是增益，不是写入的前置条件；
      ③ "没在规定时间内给出报告"，不加字段（不许把"没报告"说成"没有错误"）。
    """
    try:
        from core import code_intel
        got = code_intel.diagnose(path)
    except Exception:                                  # noqa: BLE001
        return
    if not got:
        return
    out["diagnostics"] = got["text"]
    out["diagnostics_count"] = got["count"]


def _skipped_note(skipped: dict) -> str:
    """被扫描范围挡掉的条目 —— 要说出来，否则"没看到"会被读成"不存在"。"""
    dirs = int(skipped.get("dirs") or 0)
    files = int(skipped.get("files") or 0)
    if not dirs and not files:
        return ""
    parts = []
    if dirs:
        parts.append("%d 个目录（依赖 / 构建产物 / `.gitignore` 指定）" % dirs)
    if files:
        parts.append("%d 个文件" % files)
    return ("跳过了 %s。**这批不在结果里，不等于不存在**；要一并扫就传 "
            "`include_ignored=true`。" % "、".join(parts))


# 5. grep

#: `grep` 的三种回执形状（取值与常见的 agent 工具同名，模型那边的先验可以直接用）。
#: 默认 `content`（与"只列文件名"那种取向相反）：我们的回执形状是既有契约
#: （`PLAIN_TEXT_TOOLS` 与一堆测试都钉着），换默认等于让每一次老调用都变样。
_GREP_MODES = ("content", "files_with_matches", "count")


def grep(pattern: str = "", root=None, glob=None, limit=None,
         context=None, include_ignored=None, output_mode: str = "") -> str:
    """按正则搜文件内容。回执是纯文本（纪律 ③）。

    * `content`（默认）—— `路径:行号:内容`；`context=N` 时每条命中带前后各 N 行，
      上下文行用 `路径-行号-内容`，块与块之间一行 `--`（与常见命令行搜索的读法一致）。
    * `files_with_matches` —— 只回路径，一行一个（问"哪些文件提到它"时省上下文）。
    * `count` —— `路径: 命中数`，一行一个文件；这个数是该文件的全部命中，
      不受 `limit` 影响（`limit` 在那两种模式下数的是文件）。

    `context` 只在 `content` 下有意义：另外两种模式给了就拒（带路线），
    不静默丢 —— 用户要的上下文没给，看起来像"搜到了但没有上下文"。
    """
    if not pattern:
        return "错误：没有给 pattern。"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return ("错误：正则写错了（%s）：`%s`\n"
                "提示：`pattern` 是 **Python 正则**；要搜的文本里有 `.` `*` `(` 这类"
                "字符时先转义（在字符前面加反斜杠）。" % (exc, pattern))

    mode = str(output_mode or "content").strip().lower()
    if mode not in _GREP_MODES:
        return ("错误：output_mode 只能是 %s（收到 `%s`）。\n"
                "提示：只想知道「哪些文件提到它」用 files_with_matches；"
                "要每个文件的命中数用 count；要看命中行（可带上下文）用 content。"
                % (" / ".join(_GREP_MODES), output_mode))

    try:
        cap = _as_int(limit, "limit") or _DEFAULT_GREP_LIMIT
        ctx = _as_int(context, "context") or 0
    except ValueError as exc:
        return "错误：%s" % exc
    if ctx < 0 or ctx > _MAX_GREP_CONTEXT:
        return ("错误：context 要在 0 到 %d 之间（收到 %d）。"
                % (_MAX_GREP_CONTEXT, ctx))
    if ctx > 0 and mode != "content":
        return ("错误：context 只在 output_mode=\"content\" 时有效（现在是 `%s`）。\n"
                "提示：要上下文就把 output_mode 改成 content；"
                "只列文件 / 只计数时去掉 context。" % mode)

    try:
        guard, wm = _guard()
    except Exception as exc:
        return "错误：%s" % exc

    base, err = _scan_root(root, guard, wm)
    if err is not None:
        msg = "错误：%s" % err.get("error", "")
        hint = err.get("hint", "")
        return msg + ("\n提示：%s" % hint if hint else "")

    # 走随包的 ripgrep（`core/rg.py`）：千万级文件时"先走完整棵树再匹配"
    #   在这条路上不成立（旧的 `_iter_files` 刻意返回整张列表，GB 级内存）。
    #   排除名单从 `walk_scope.SCAN_SKIP_DIRS` 那一份常量生成 ——
    #      一处定义、两处渲染（不是第二套真相源）。
    from core import rg as _rg
    from core.walk_scope import SCAN_SKIP_DIRS as _SKIP_DIRS
    try:
        _args = _rg.build_args(
            pattern, base, glob=str(glob or ""), context=ctx,
            include_ignored=bool(include_ignored),
            skip_dirs=_SKIP_DIRS,
            max_file_bytes=_MAX_GREP_FILE_BYTES,
            max_line_chars=_MAX_LINE_CHARS,
        )
        _run = _rg.RgRun(_args, timeout=_GREP_TIMEOUT_S)
    except _rg.RgUnavailable as exc:
        return ("错误：搜索要用随包的 ripgrep，但这台机器上没有可用的那一份。\n"
                "提示：%s" % exc)

    lines_out: list[str] = []
    hit_paths: set[str] = set()
    per_file: dict[str, int] = {}
    file_order: list[str] = []
    #: `content`：已列出的命中行数；`count`：全部命中行数（不受 `limit` 影响）。
    hits = 0
    #: 已列出的条目数 —— `content` 数行，另外两种数文件。
    listed = 0
    used = 0
    cut_by_budget = False
    last_path: str = ""
    last_line: int = -10

    def _txt(node) -> str:
        """rg 的 `{text: …}` / `{bytes: base64}` → `str`（用我们自己的解码器）。

        为什么要这一步：`--encoding none` 下非 UTF-8 的行以 base64 回来，
          而本仓的 `_decode` 认 cp936 / UTF-16 / UTF-8 —— 让 rg 自己猜会把
          "非 UTF-8 也能命中"这条旧行为悄悄改掉。
        """
        if not isinstance(node, dict):
            return ""
        val = node.get("text")
        if isinstance(val, str):
            return val
        raw64 = node.get("bytes")
        if isinstance(raw64, str):
            try:
                raw = base64.b64decode(raw64)
            except Exception:                                # noqa: BLE001
                return ""
            return _decode(raw)[0]
        return ""

    def _clip(line: str) -> str:
        if len(line) > _MAX_LINE_CHARS:
            return line[:_MAX_LINE_CHARS] + "..."
        return line

    try:
        with _run:
            for _ev in _run.events():
                _type = _ev.get("type")
                _data = _ev.get("data") or {}
                _path = _txt(_data.get("path")).strip()
                if not _path:
                    continue
                #: rg 给的是绝对路径；回执一直用 posix 形式。
                rel = _posix(Path(_path))
                _no = int(_data.get("line_number") or 0)

                if _type == "match":
                    if mode == "files_with_matches":
                        if rel in hit_paths:
                            continue
                        hit_paths.add(rel)
                        row = rel
                        if used + len(row) + 1 > _MAX_GREP_CHARS:
                            cut_by_budget = True
                            break
                        lines_out.append(row)
                        used += len(row) + 1
                        listed += 1
                        if listed >= cap:
                            break
                        continue

                    if mode == "count":
                        if rel not in per_file:
                            per_file[rel] = 0
                            file_order.append(rel)
                        per_file[rel] += 1
                        hits += 1
                        continue

                    # content
                    if hits >= cap:
                        break
                    line = _clip(_txt(_data.get("lines")).rstrip("\n"))
                    if ctx > 0 and lines_out and rel == last_path and _no > last_line + 1:
                        lines_out.append("--")
                    row = "%s:%d:%s" % (rel, _no, line)
                    if used + len(row) + 1 > _MAX_GREP_CHARS:
                        cut_by_budget = True
                        break
                    lines_out.append(row)
                    used += len(row) + 1
                    hits += 1
                    hit_paths.add(rel)
                    last_path, last_line = rel, _no
                    if hits >= cap:
                        break
                    continue

                if _type == "context" and mode == "content" and ctx > 0:
                    if hits >= cap:
                        continue
                    line = _clip(_txt(_data.get("lines")).rstrip("\n"))
                    if lines_out and rel == last_path and _no > last_line + 1:
                        lines_out.append("--")
                    row = "%s-%d-%s" % (rel, _no, line)
                    if used + len(row) + 1 > _MAX_GREP_CHARS:
                        cut_by_budget = True
                        break
                    lines_out.append(row)
                    used += len(row) + 1
                    hit_paths.add(rel)
                    last_path, last_line = rel, _no
    finally:
        timed_out = bool(getattr(_run, "timed_out", False))
        _stats = dict(getattr(_run, "stats", {}) or {})
        _rc = getattr(_run, "returncode", None)
        _err = str(getattr(_run, "error_text", "") or "").strip()

    #: rg 的退出码：0 有命中 · 1 没命中 · 2 出错。
    #:   出错时不许回"没有匹配"（那是把故障报成结论）。
    if _rc not in (0, 1, None) and _err:
        _shown = _err if len(_err) <= 600 else (_err[:600] + "…（已截断）")
        return ("错误：搜索引擎报错（rg 退出码 %s）：\n%s\n"
                "提示：`pattern` 要能同时被 Python 正则与 PCRE2 接受；"
                "拿不准就把特殊字符转义。" % (_rc, _shown))

    #: `count` 的条目是跑完之后按文件摊出来的（`limit` 数的是文件数）。
    if mode == "count":
        for rel in file_order:
            if listed >= cap:
                break
            row = "%s:%d" % (rel, per_file[rel])
            if used + len(row) + 1 > _MAX_GREP_CHARS:
                cut_by_budget = True
                break
            lines_out.append(row)
            used += len(row) + 1
            listed += 1
            hit_paths.add(rel)

    notes: list[str] = []
    if timed_out:
        notes.append("搜索超过 %d 秒就收工了（结果可能不全 —— 缩小 pattern / glob，"
                     "或把 root 指到更小的目录）" % int(_GREP_TIMEOUT_S))
    if cut_by_budget:
        notes.append("回执到 %d 字符就停了（缩小 pattern 或把 context 调小可收窄）"
                     % _MAX_GREP_CHARS)
    elif mode == "content" and hits >= cap:
        notes.append("只列到 limit=%d 条就停了 —— 换个更具体的 pattern / glob 能收窄"
                     % cap)
    elif mode != "content" and listed >= cap:
        notes.append("只列到 limit=%d 个文件就停了 —— 缩小 glob 或换个更具体的 pattern"
                     " 能收窄" % cap)

    if not lines_out:
        _scanned = int((_stats or {}).get("searches") or 0)
        body = "没有匹配。（根：`%s`" % _posix(base)
        if _scanned:
            body += "，扫了 %d 个文件" % _scanned
        body += "）"
        if notes:
            body += "\n" + "\n".join("· " + n for n in notes)
        return body

    if mode == "files_with_matches":
        tail = "\n（%d 个文件命中）" % len(hit_paths)
    elif mode == "count":
        tail = "\n（%d 个文件，共 %d 处命中）" % (len(hit_paths), hits)
    else:
        tail = "\n（%d 个文件命中 %d 行）" % (len(hit_paths), hits)
    if notes:
        tail += "\n" + "\n".join("· " + n for n in notes)
    return "\n".join(lines_out) + tail


def _context_blocks(total: int, matched: list, ctx: int) -> list:
    """把命中行号摊成上下文区块 `[(起, 止, 命中行集)]`，相邻块合并。

    合并不是为了好看：两处命中相距小于 2×ctx 时分开会在中间夹一行 `--`，
    读者看不出那两段其实是同一片代码。
    """
    if ctx <= 0:
        return [(i, i, {i}) for i in matched]
    blocks: list[tuple[int, int, set]] = []
    for i in matched:
        lo, hi = max(0, i - ctx), min(total - 1, i + ctx)
        if blocks and lo <= blocks[-1][1] + 1:
            prev_lo, prev_hi, prev_set = blocks[-1]
            prev_set.add(i)
            blocks[-1] = (prev_lo, max(prev_hi, hi), prev_set)
        else:
            blocks.append((lo, hi, {i}))
    return blocks


# 会话号（与 `core/todos.py` 读同一个地方）

def _sid() -> str:
    """当前工具调用属于哪个会话。判据与理由在 `request_snapshot.current_session_id`。"""
    from core.request_snapshot import current_session_id

    return current_session_id()


def json_default(obj):
    """`as_text` 那条 `json.dumps` 的 `default=`。

    病灶：`read_excel_data` 的返回里带 `datetime`，没有 `default=` 时
    `json.dumps` 抛 `TypeError: Object of type datetime is not JSON serializable`。
    它抛在"唯一一处把 handler 返回值变成 wire 文本"的地方，整条回执被
    `except` 收成一行 `Tool execution error: …`，模型拿到的不是数据。
    症状看起来像"这个工具坏了"（读得到，却被自己序列化炸掉）。

    修在这里、不修在每个 skill 的返回里 —— 后者是 N 处，而且下一次有人
    返回一个 `Decimal` 就复发一次。`_tool_result_of` 那三条路（同步 / 流式 / 审批重放）
    共用这一个函数，所以这里就是那个"一处"。

    逐类给形状，不许一句 `str()` 兜底：`datetime` 的 `str()` 看着像样，
    但 `set` 的 `str()` 是 `{'a', 'b'}` —— 那是 Python repr、不是 JSON，
    读的人无从分辨。认不出来的才退回 `str`（至少不抛，且原文可见）。
    """
    import datetime as _dt
    import decimal as _dec

    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        # ISO 8601 —— "modified 有 UTC / 本地两种口径"的教训：
        # 带不带时区写在串里，别让读的人猜。
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, _dec.Decimal):
        # 不转 float —— 那会静默丢精度，而表格里的钱和比率正是靠它。
        return str(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        # 留个看得见的占位，不悄悄塞一段 base64（那会把回执撑爆）。
        return f"<{len(obj)} bytes>"
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)          # 定序：同一份结果两次跑要逐字一样
    return str(obj)


def as_text(result, tool_name: str) -> str:
    """工具回执进 messages 前的最后一道形状转换（循环里两条路都调它）。

    `PLAIN_TEXT_TOOLS` 里的工具返回 `str` 时原样出去 —— 不套 `json.dumps`。
    两个理由，都在纪律 ③ 里：`\\n` 不用转义成两个字符（长文件上是几个百分点），
    以及被字符截断时切出来的是可读正文，不是半截 JSON。
    认的是名单不是类型：`isinstance(result, str)` 单独一条会让将来某个
    恰好返回字符串的工具静默换掉它今天的 wire 形状。
    `default=json_default` —— 见它的 docstring；别摘掉它。
    """
    if tool_name in PLAIN_TEXT_TOOLS and isinstance(result, str):
        return result

    return json.dumps(result, ensure_ascii=False, default=json_default)


# 五份 schema
#
# 与所有常驻工具同一条规矩：这些都是每请求的永久税，
#   改措辞必须重量一次（`scripts/measure_request.py`）。
# `$schema` 照旧写上（它在装载期校验、进 wire 前被剥掉），
#   不写的话 `skill_loader.validate_tool_schema()` 认不出来。

READ_FILE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "read_file",
    "description": (
        "读一个文件。文本按行返回（带行号，长文件用 offset/limit 分段）；"
        "**图片（PNG/JPEG/WebP/GIF）会作为真正的图返回给你看**，"
        "所以「看看这张截图」直接读它就行。**改文件之前必须先读它。**"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string",
                          "description": "文件路径（相对默认工作区，或绝对路径）"},
            "offset": {"type": "integer",
                       "description": "从第几行开始（1 起，默认第 1 行）"},
            "limit": {"type": "integer",
                      "description": "最多读几行（默认 2000）"},
        },
        "required": ["file_path"],
    },
}

WRITE_FILE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "write_file",
    "description": (
        "整篇写一个文本文件（新建 / 覆盖）。**覆盖已有文件前必须先 read_file** —— "
        "文件在读过之后被外部改过的话会被拒。只想改一处请用 edit_file。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整的新内容"},
        },
        "required": ["file_path", "content"],
    },
}

EDIT_FILE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "edit_file",
    "description": (
        "把文件里的一段原文换成新的一段（**逐字符精确匹配**，必须先 read_file）。"
        "默认要求这段原文**只出现一次**；出现了多次会被拒并列出行号 —— "
        "确实要全改就传 replace_all=true。改整篇请用 write_file。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string",
                           "description": "要被替换掉的原文（逐字符，含行首缩进）"},
            "new_string": {"type": "string", "description": "替换成什么"},
            "replace_all": {"type": "boolean",
                            "description": "true = 全部替换（默认只改一处）"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}

GLOB_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "glob",
    "description": (
        "按文件名模式找文件（`**` 表示任意层级）。模式里**没有斜杠**就按文件名匹配"
        "（如 `*.py` = 任意层级的 .py）；**有斜杠**就按相对路径匹配（如 `src/*.py`）。"
        "只回文件名，不看内容 —— 找内容用 grep。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "例：`**/*.py`、`*.md`"},
            "root": {"type": "string",
                     "description": "从哪个目录开始（默认工作区；只许工作区内的目录）"},
            "limit": {"type": "integer", "description": "最多回几条（默认 200）"},
            "include_ignored": {
                "type": "boolean",
                "description": "true = 连被跳过的目录/文件一起扫（默认跳过并报数）",
            },
        },
        "required": ["pattern"],
    },
}

GREP_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "grep",
    "description": (
        "在文件内容里搜正则（Python 语法）。`output_mode` 选回执形状："
        "content（默认，命中行）/ files_with_matches（只回路径）/ count（每文件命中数）"
        "—— 只问「哪些文件提到它」就用 files_with_matches。"
        "`glob` 限定文件；`context=N` 带前后各 N 行（**仅 content**）。"
        "默认跳过依赖/构建产物/`.gitignore` 并报数，`include_ignored=true` 连它们一起扫。"
        "只搜工作区，跨区用 request_cross_zone_search。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则"},
            "root": {"type": "string", "description": "从哪个目录开始（默认工作区）"},
            "glob": {"type": "string",
                     "description": "只看这些文件（如 `*.py`、`src/**`）"},
            "output_mode": {
                "type": "string",
                "enum": list(_GREP_MODES),
                "description": ("content（默认）/ files_with_matches / count；"
                                "`limit` 在非 content 下数的是**文件**"),
            },
            "limit": {"type": "integer", "description": "最多回几条（默认 100）"},
            "context": {"type": "integer",
                        "description": ("每条命中带前后各几行（0–%d，默认 0；"
                                        "仅 content）" % _MAX_GREP_CONTEXT)},
            "include_ignored": {
                "type": "boolean",
                "description": "true = 连被跳过的目录/文件一起扫（默认跳过）",
            },
        },
        "required": ["pattern"],
    },
}
