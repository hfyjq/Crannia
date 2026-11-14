"""项目指令文件的发现与装载 —— 系统提示词里 `<leaf_context>` 那一段的来源。

## 找哪些文件

| 层 | 位置 | 文件名 |
|---|---|---|
| 用户全局 | `~/.crannia/` | `LEAF.md` · `AGENTS.md` |
| 项目（逐层） | 会话目录 → 项目根（含），每一层 | `LEAF.md` · `AGENTS.md` · `LEAF.local.md` · `AGENTS.local.md` |

* 项目根 = 从会话目录向上第一个含 `.git` 的目录；找不到就是会话目录自己。
  不向文件系统根一路走：那会把 `D:\\LEAF.md` 这类无关文件读进来。
* 加载顺序 = 优先级递增：全局在最前，然后项目根 → 会话目录（越靠近
  当前目录越靠后，越具体，压得住前面）。同一层内 Project 在前、Local 在后。
* `AGENTS.md` 是生态标准（跨工具通用）；`LEAF.md` 是我们约定的文件名，两者都认。
* 同一份内容如果在两个文件里逐字相同，只发一次（省的是每条请求的钱）。

## 怎么处理

剥 frontmatter → 剥 HTML 注释 → 只剩空白就当没有 → `sanitize_text`（框架块不变量）。
剥完再判空：出厂模板整份都在注释里，不剥的话每条请求都在为"给人看的说明"付费。

## 上限

软告警 40,000 字符，不截断 —— 超了只打一行（每进程一次）。
判据是用户拍过的：不知道用户有多少需求，就不设硬上限。

## 每轮现读

不带缓存：四个目录 × 四个名字的 `stat` 很便宜，而"用户刚改完就生效"值这个价。
代价如实记：一次 `.git` 探测 + 每层几个 `stat`，跑在每条请求上。
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "INSTRUCTION_NAMES", "LOCAL_NAMES", "WARN_CHARS", "GLOBAL_DIRNAME",
    "strip_html_comments", "strip_frontmatter", "project_root", "chain",
    "discover", "load", "status", "reset_warned",
]

#: 项目级（随仓库走）的候选文件名，顺序即同层内的加载顺序。
INSTRUCTION_NAMES = ("LEAF.md", "AGENTS.md")

#: 本机级（建议不进版本库）。名字跟着生态的 `.local` 约定走。
LOCAL_NAMES = ("LEAF.local.md", "AGENTS.local.md")

#: 用户全局那几份放哪（`~` 下）；目录不存在就整段跳过。
GLOBAL_DIRNAME = ".crannia"
GLOBAL_NAMES = ("LEAF.md", "AGENTS.md")

#: 认项目根的标记（与常见做法一致：最近的 `.git`）。
PROJECT_MARKERS = (".git",)

#: 软告警线，不是硬上限。超了只出声、不拦、不截断。
WARN_CHARS = 40000

#: HTML 注释块 —— 进请求之前先剥掉。
#:
#: 为什么：出厂模板的正文全在 `<!-- ... -->` 里（"建议内容 / 示例"），
#: 一个字的项目内容都没有。直接读会连注释一起读，进 `<leaf_context>`。
#: `estimate_request_tokens` 同一台仪器：含它 system = 2,046 / 不含 = 1,763
#:一份没有项目内容的模板注释，每条请求在付 283 tokens。
#:
#: 剥的是注释，不是"去掉空行"那一类容错：用户真填了内容，注释本来就被替换掉；
#:   这一条管的是"没填"那种情况不再收费，顺带保证"没删注释就填"时也不带上。
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

#: YAML frontmatter —— 开头那一段 `---` … `---`，进请求之前先剥掉。
#:
#: 判据：frontmatter 一律剥掉 —— 里面的 `name` / `description` / `paths:`
#:   不是给模型看的正文。
#: 只认"文件一开始就是 `---`"这一种形状，且必须有配对的收尾；
#:   没有收尾，不剥 —— 宁可多付钱，不静默吃掉用户正文。
#: `paths:` 的条件加载没有做（那是"只在碰到匹配文件时才进"）：
#:   今天 `paths:` 会连 frontmatter 一起被剥掉，正文照常每轮进。行为差异记在案。
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(\r?\n|\Z)", re.DOTALL)

#: 本进程最近一次装载读到的 `[(显示路径, 字符数), …]` —— 给日志 / 设置页用。
_loaded: list = []
#: 软告警每进程只出声一次（它挂在每请求的路上，每轮打一行会淹掉日志）。
_warned = False

#: 这一段读过哪个子目录里的文件 —— 从下一轮起，那几层的指令也进
#: `<leaf_context>`。键 = 通道会话键（与 `bus.registry._discovered` 同族，
#: `/clear` 清掉）。
#:
#: 判据：光把"会话目录 → 项目根"那一条链读进来是不够的 —— 模型在
#: `packages/x/` 里读文件时，`packages/x/LEAF.md` 才是它该遵守的那一份。
#: 代价如实记：每多一个目录，`<leaf_context>` 长一截，系统提示词这个
#:   前缀改写一次（那一刻整段前缀缓存作废，之后重新稳定）。
#:只在第一次碰某个目录时发生，不是每轮。
_touched: dict = {}


def strip_html_comments(text: str) -> str:
    """剥掉 HTML 注释；剥完只剩空白就返回空串（调用方据此静默省略整段）。"""
    stripped = _HTML_COMMENT_RE.sub("", text)
    return stripped if stripped.strip() else ""


def strip_frontmatter(text: str) -> str:
    """剥掉开头的 frontmatter；没有配对收尾就原样返回。"""
    return _FRONTMATTER_RE.sub("", text, count=1)


# 发现

def global_dir() -> Path:
    """用户全局那几份放哪（单独一个函数是为了测试能换掉它）。"""
    return Path.home() / GLOBAL_DIRNAME


def project_root(start: Path) -> Path:
    """从 *start* 向上找第一个含 `.git` 的目录（含自己）；找不到，`start`。"""
    cur = Path(start).resolve()
    for d in (cur, *cur.parents):
        if any((d / m).exists() for m in PROJECT_MARKERS):
            return d
    return cur


def chain(start: Path) -> list:
    """`[项目根, …, 会话目录]` —— 顺序即加载顺序（后者更具体）。"""
    start = Path(start).resolve()
    root = project_root(start)
    out = [start]
    cur = start
    while cur != root and cur.parent != cur:
        cur = cur.parent
        out.append(cur)
    return list(reversed(out))


def _existing(directory: Path, names) -> list:
    out = []
    for name in names:
        p = directory / name
        try:
            if p.is_file():
                out.append(p)
        except OSError:
            continue
    return out


def discover(start: Path) -> list:
    """要读的全部文件，按加载顺序（先全局，再项目根 → 会话目录）。"""
    out = _existing(global_dir(), GLOBAL_NAMES)
    for d in chain(start):
        out.extend(_existing(d, INSTRUCTION_NAMES))
        out.extend(_existing(d, LOCAL_NAMES))
    return out


def _display(path: Path, root: Path) -> str:
    """给模型看的标签：相对项目根；项目外（用户全局）写成 `~/…`。

    相对项目根而不是相对"起点"：同一个文件在两种起点下应该叫同一个名字
      （否则模型看到 `../LEAF.md` 与 `LEAF.md` 会以为是两份）。
    """
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        pass
    try:
        return "~/" + path.resolve().relative_to(Path.home()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


# 装载

def _start_dir(start=None) -> Path:
    """这一轮从哪个目录开始找。

    会话目录（用户选的那个）> 默认工作区；取不到就 `Path.cwd()`。
    惰性 import：`session_workspace` → `core.workspace` → … 这条链会绕回来。
    """
    if start is not None:
        return Path(start)
    try:
        from core import session_workspace
        cur = session_workspace.current_path()
        if cur is not None:
            return Path(cur)
    except Exception:                                       # noqa: BLE001
        pass
    try:
        from core.workspace import default_output_dir
        return Path(default_output_dir())
    except Exception:                                       # noqa: BLE001
        return Path.cwd()


def load(start=None) -> tuple:
    """读全部指令文件 → `(文本, [(显示路径, 字符数), …])`。

    * 文本带来源标签（一行一个路径），并有一句"越靠后越具体"的优先级说明；
    * 内容逐字相同的两份只留先出现的那份（省每条请求的钱）；
    * 一份都没有，`("", [])`（调用方静默省略整段）。
    """
    global _loaded, _warned
    base = _start_dir(start)
    root = project_root(base)
    #: 链（根 → 会话目录）+ 这一段碰过的子目录（更深，更具体，排后面）。
    paths = discover(base)
    key = _session_key()
    for d in touched_dirs(key):
        paths.extend(_existing(Path(d), INSTRUCTION_NAMES))
        paths.extend(_existing(Path(d), LOCAL_NAMES))
    texts, files, seen = [], [], set()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        text = strip_frontmatter(text)
        text = strip_html_comments(text)
        if not text:
            continue
        key = text.strip()
        if key in seen:                       # 同一份内容发两遍 = 付两遍钱
            continue
        seen.add(key)
        from core.content_sanitizer import sanitize_text
        text = sanitize_text(text)
        label = _display(path, root)
        files.append((label, len(text)))
        texts.append("%s:\n%s" % (label, text))
    _loaded = files
    total = sum(n for _p, n in files)
    if total > WARN_CHARS and not _warned:
        _warned = True
        print("[leaf] 项目指令文件偏大：%d 字符（软告警线 %d）—— %s"
              % (total, WARN_CHARS, " + ".join("%s(%d)" % (p, n) for p, n in files)))
        print("[leaf]   -> 仍然**全量发送**（这是提示不是限制）；"
              "每轮都在付费，值得回头删一批。")
    if not texts:
        return "", []
    head = "（以下是本项目的指令文件：先全局、后项目根、再到当前目录 —— **越靠后越具体**，与前面冲突时以后面为准）"
    return head + "\n\n" + "\n\n".join(texts), files


def _session_key() -> str:
    """这一段属于哪条会话（通道键）—— 与 `bus.registry._discovered` 同一个口径。"""
    try:
        from core.request_snapshot import current_session_id
        return str(current_session_id() or "")
    except Exception:                                       # noqa: BLE001
        return ""


def note_touched(path, session_key: str = "") -> None:
    """记下"这一段碰过 *path* 所在的目录"，下一轮起那几层的指令也进系统提示词。

    只对会话目录以下的目录生效（上面那几层已经在链里了）；
      会话目录自己那一层不算（它本来就在）。
    绝不抛：这是顺带记的一笔，弄挂一次工具调用不值。
    """
    try:
        target = Path(path).resolve().parent
        base = _start_dir().resolve()
        rel = target.relative_to(base)
    except (ValueError, OSError):
        return
    if not rel.parts:
        return                          # 就是会话目录自己
    key = str(session_key or _session_key())
    cur = base
    bucket = _touched.setdefault(key, set())
    for part in rel.parts:
        cur = cur / part
        bucket.add(str(cur))


def touched_dirs(session_key: str = "") -> list:
    """这一段碰过的子目录（排序后；浅的在前、深的在后）。"""
    key = str(session_key or _session_key())
    return sorted(_touched.get(key, ()))


def forget(session_key: str = "") -> None:
    """`/clear` 用：丢掉这一段记下的子目录（给了键只清那一条）。"""
    if session_key:
        _touched.pop(str(session_key), None)
    else:
        _touched.clear()


def status() -> dict:
    """装载现状（只读快照；给日志 / 设置页）。

    软告警的判据在 `over`：超了也不拦 —— 调用方只许拿它出声，
    不许拿它截断（不知道用户有多少需求，就不设硬上限）。
    """
    total = sum(n for _p, n in _loaded)
    return {
        "files": list(_loaded),
        "chars": total,
        "warn_chars": WARN_CHARS,
        "over": total > WARN_CHARS,
    }


def reset_warned() -> None:
    """测试用：放开"每进程一次"的告警闩。"""
    global _warned
    _warned = False
