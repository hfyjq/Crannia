"""长期记忆的底座 —— 存储 + 派生索引 + 专用工具三件。

    结构:
        data/memory/facts/<scope>/*.md   一条记忆一个文件（frontmatter name/description + 正文）
        data/memory/.tombstones.json     用户删过的条目（slug 名单）—— 写路径不许重新生成

    索引是派生的，不落盘：`MEMORY.md` 这个名字只活在注入口那一块
      的头上；每次现算（照 `core/skill_index.py` 的形状），没有第二份真相源。
    明细走专用工具（不进 `READABLE_DIRS`）：`memory_read` / `memory_search` /
      `memory_write` —— 专用工具天然限定路径（同 `create_skill`），且 tombstone 只有挂在
      专用写工具上才挂得住（`write_file` 是通用路径）。

## 六条纪律（每条对应一条设计或一个失败模式）

### ① 索引派生 + 上限「截断 + 出声」
照 `skill_index`；上限 200 行 / 25,000 字符。
超了截断 + 出声（print 一行）；「不是截断、是 demote」是整理的语义修法，
两处各管各的。

### ② 内容过 `sanitize_text`（框架块不变量）
索引文本由 `build_memory_index` 打码后再返回；包裹 `<memory_index>` 照发。
机械读者：`tests/test_prompt_block_sanitize.py`（本模块在它的普查表里）。

### ③ tombstone 挂在写路径上
`memory_write` 先查账本，slug 删过，拒（"用户删过的不许重新生成"）。
`add_tombstone()` 是账本的写入口（编辑界面以后接它；现在只有测试用）。

### ④ 工具结果的隔离分类是手动登记的
`memory_read` / `memory_search` 的名字不落 `_READ_FAMILIES` 前缀（`memory_` 不在
`get_/read_/…` 那张表里），反查读者不会替它们报警。分类是刻意的：
记忆是背景事实、不是不可信数据 —— 明确「不给记忆加
`trust="untrusted"`（会推翻快照判据）」，进 `_TRUSTED_OUTPUT_TOOLS`，旁边写着理由。

### ⑤ 上限拒，不静默砍
name ≤ 40 字符 · description ≤ 120 字符（它将进索引那一行）· 正文 ≤ 4,000 字符。
超限一律拒并说清限额 —— 静默截断 = 让模型以为写进去了。

### ⑥ 「写记忆」开关在执行侧拒
关掉时 schema 照发、`memory_write` 返回拒绝 —— 不从 `tools=` 里摘
（摘了 = 在 tools 段断缓存前缀；同一取舍）。读不到/坏 JSON，默认开。

### ⑦ 「巩固模式」是一次只读放宽
整理（`core/memory_organize.py`）要读日报与会话笔记，而设计明确不去加宽
`DEFAULT_FRAMEWORK_READABLE_DIRS`（那是给所有轮所有工具的全局声明），
做法是同一对工具在一个 `ContextVar` 门后多认两个只读名字形状：
`logs/<日期>` 与 `sessions/<会话号>`（见 `organize_read_mode`）。
三份 schema 一个字都不改 —— 那个模式下父与子发的是同一张工具表
（保缓存），把整理专属的用法写进 schema 会同时误导主线（它用不了那些名字）。
判据：工具约束说明放在 `extra`，不放进共享的提示词正文。

## 两条写在案的取舍

- 不做 CAS（写前必读）：`file_tools` 的纪律 ② 防的是外部 watcher（微信目录那类），
  而记忆目录只有本工具写。代价是"模型覆盖一条记忆前可以不读"—— 组织它的是
  `memory_read` 的指引与 `description` 的指引，不是机制。
- `memory_search` 是零 LLM 的全扫（不抄"每轮预取"），
  "没有匹配"是一次扫完的结论，不是"搜不到"（本目录内不需要区分：
  数据量有界、扫描必完成）。

ASCII-only 打印（Windows GBK 控制台）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from core.content_sanitizer import sanitize_text
# 挂索引那一步与技能索引共用一处（见 `core/skill_index.py`）。
from core.image_input import append_text_block

# 常量

#: 事实文件的根。相对路径（相对路径可被测试的 chdir 钉住；
#: 绝对路径会改到生产）。
#: 测试用「猴补 + 恢复」钉死它（`tests/test_im_routing.py` 的 `_pinned` 同形）。
FACTS_ROOT = Path("data/memory/facts")

#: tombstone 账本 —— 可读区外（它是框架的账本，不是给模型看的）。
TOMBSTONES_PATH = Path("data/memory/.tombstones.json")

#: 只有一层作用域：`workspace:<id>`。任务层（第二层）
#: 是目录分层的预留，等任务区来时加一层目录即可。
DEFAULT_SCOPE = "workspace"

#: 索引上限（200 行 / 25,000 字符）。
MEMORY_INDEX_MAX_LINES = 200
MEMORY_INDEX_MAX_CHARS = 25000

#: 单条记忆的上限（纪律 ⑤：超限拒，不静默砍）。
MAX_NAME_CHARS = 40
MAX_DESC_CHARS = 120
MAX_BODY_CHARS = 4000

#: `memory_read` 的 handler 自裁线。循环闸的默认是 6000（档 3 的
#: `tool_result_max_chars`）—— handler 必须严格小于它（两把刀方向相反）。
#: 写入侧已限 4,000 + frontmatter，这里防的是手改过的文件。
MAX_READ_CHARS = 5600

#: `memory_search` 一次最多回几条（模型可调，上下夹取）。
DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 20

#: slug 里不允许出现的字符（Windows 文件名禁用集 + 控制字符）。
_SLUG_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


# 「巩固模式」：让同一对只读工具多认两类只读名字

#: 整理期间打开的只读根：`(logs_dir, sessions_dir)`；`None` = 关（主线永远是它）。
#: 用 `ContextVar` 而不是函数参数 —— 工具 handler 是 `agent_loop` 按名字直调的，
#: 没有"额外参数"这条通道；并发派发走 `copy_context`，任务级的值带得过去。
#: 会话级状态登记表的 census 扫不到它（`ContextVar(...)` 不是容器
#: 字面量）—— 这是有意的：任务级、自洁，跨轮/跨线程不会残留（同 `request_snapshot`）。
_ORGANIZE_ROOTS: ContextVar[Optional[Tuple[Path, Path]]] = ContextVar(
    "memory_organize_roots", default=None)

#: 巩固模式下认的两个名字形状：`logs/<日期>` / `sessions/<会话号>`（`.md` 可省）。
#: 段内不许有路径分隔符（正则挡住）= 越界就地拒。
_ORGANIZE_NAME_RE = re.compile(r"^(logs|sessions)/([^/\\]+?)(?:\.md)?$")


@contextlib.contextmanager
def organize_read_mode(logs_dir, sessions_dir):
    """打开「巩固模式」（`core/memory_organize.py` 在 fork 期间用）。

    只在整理 fork 的那一段生效：出去就 `reset`，主线拿不到
主线的 `memory_read`/`memory_search` 行为一个字节不变。
    """
    token = _ORGANIZE_ROOTS.set((Path(logs_dir), Path(sessions_dir)))
    try:
        yield
    finally:
        _ORGANIZE_ROOTS.reset(token)


def _read_organize_source(name: str) -> Optional[str]:
    """巩固模式下读一条非记忆素材（日报 / 会话笔记）。`None` = 不归它管。

    越界（带分隔符 / `..` / 不在根下）就地拒，返回一句说明而不是抛。
    """
    roots = _ORGANIZE_ROOTS.get()
    if roots is None:
        return None
    m = _ORGANIZE_NAME_RE.match((name or "").strip().replace("\\", "/"))
    if not m:
        return None
    kind, stem = m.group(1), m.group(2)
    base = roots[0] if kind == "logs" else roots[1]
    label = "日报" if kind == "logs" else "会话笔记"
    # 越界判据是解析出来的结果、不是名字长什么样 —— 正则已经挡住了路径分隔符，
    #    但"挡输入"这条路永远要穷举花样（`C:.md` 这种带盘符的相对路径会整个替掉
    #    左操作数，正则看不出来）。把目标解析出来，再问它是否仍在根下。
    try:
        target = (base / (stem + ".md")).resolve()
        target.relative_to(base.resolve())
    except (OSError, ValueError):
        return None                       # 越界，当"不是素材"，落到 facts 分支
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return (f"没有 {label} `{stem}` —— 巩固模式下只能读**存在**的那几个，"
                f"名字见任务说明里那张清单。")
    text = text.strip()
    if len(text) > MAX_READ_CHARS:
        return (f"# {label} {stem}\n\n{text[:MAX_READ_CHARS]}"
                f"\n\n[超过 {MAX_READ_CHARS} 字符，已截断]")
    return f"# {label} {stem}\n\n{text}"


# 路径与 slug

def _scope_dir(scope: str = DEFAULT_SCOPE) -> Path:
    return FACTS_ROOT / scope


def slugify(name: str) -> str:
    """把标题变成安全的文件名主干。保留 CJK（本仓的用户名与标题多半是中文）。

    这是身份来源：tombstone 与"新建 vs 更新"都按它判 —— 改这个函数
    等于改"什么算同一条记忆"，动手前先看 `test_memory_store.py` 的 slug 用例。
    """
    s = _SLUG_BAD_RE.sub("-", str(name or "").strip())
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("- .")
    return s[:60]


# 文档解析（frontmatter + 正文）

def _parse_document(text: str) -> Tuple[Optional[Dict], Optional[str]]:
    """拆 `---\\n…\\n---\\n正文`。坏就返回 (None, None) —— 不猜、不救。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            except Exception:
                return None, None
            if not isinstance(meta, dict):
                return None, None
            return meta, "\n".join(lines[i + 1:])
    return None, None


def parse_entry(path: Path) -> Optional[Dict]:
    """读一个事实文件 → `{slug,name,description,body,path}`；坏文件返回 None。

    整段在 `_IO_LOCK` 里 —— 不加锁的话，后台 fork 正在写这条时
    这里会读到半截，而坏文件返回 `None`，那条记忆静默消失
    （不报错、不留痕，看起来就像"它没想起来"）。实际比例见 `_atomic_write_text`。
    """
    try:
        with _IO_LOCK:
            text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_document(text)
    if meta is None:
        return None
    name = str(meta.get("name") or "").strip()
    desc = str(meta.get("description") or "").strip()
    if not name:
        return None
    return {
        "slug": path.stem,
        "name": name,
        "description": desc,
        "body": (body or "").strip(),
        "path": path,
    }


def list_entries(scope: str = DEFAULT_SCOPE) -> List[Dict]:
    """全部条目，按 slug 排序（排序必须确定 —— 索引挂消息侧，序一漂
    前缀就漂）。坏文件跳过 + 出声（不静默）。"""
    root = _scope_dir(scope)
    if not root.is_dir():
        return []
    out: List[Dict] = []
    for p in sorted(root.glob("*.md")):
        entry = parse_entry(p)
        if entry is None:
            print(f"[memory_store] 跳过 {p} —— frontmatter 缺失或坏掉（不猜不救）")
            continue
        out.append(entry)
    out.sort(key=lambda e: e["slug"])
    return out


def read_entry(name: str, scope: str = DEFAULT_SCOPE) -> Optional[Dict]:
    """按名字读数（slug 归一之后找文件）。"""
    slug = slugify(name)
    if not slug:
        return None
    return parse_entry(_scope_dir(scope) / (slug + ".md"))


# tombstone 账本

def load_tombstones() -> Dict[str, Dict]:
    """`{slug: {name, at}}`。文件坏了，出声 + 视为空（见模块 docstring 取舍：
    账本只有本模块写，坏了是手改的；重建比堵死写路径好 —— 但那要看得见）。"""
    if not TOMBSTONES_PATH.exists():
        return {}
    try:
        data = json.loads(TOMBSTONES_PATH.read_text(encoding="utf-8"))
        tob = data.get("tombstones") or {}
        return tob if isinstance(tob, dict) else {}
    except Exception as exc:
        print(f"[memory_store] tombstones 读不出来（{type(exc).__name__}）—— 本次视为空账本")
        return {}


#: 记忆库的读写锁 —— 进程内，包住"写一条"与"读一条"。
#:
#: 为什么需要：后台抽取 fork 与用户轮真会重叠（实际 1.44s 窗口），
#: 而 `build_memory_index()` 在每个用户轮开头扫所有条目。
#: 不加锁，读者看到被截断的文件，`parse_entry` 返回 `None`，静默少一条。
#:
#: 为什么不是"临时文件 + `os.replace`"（试过，更糟）：
#: Windows 的 `os.replace` 走 `MoveFileEx`，目标文件被读句柄打开时会失败
#: （Python 的 `open()` 不含 `FILE_SHARE_DELETE`），实际变成**读写两头一起报
#: `PermissionError`，坏读率反而从 5.3% 升到 9.3%。这条路在 Windows 上不成立**，
#: 别再试一次。
#:
#: `RLock` 不是 `Lock`：`memory_search` 扫目录时会在持有它的路径上再进
#: `parse_entry`（重入）。用 `Lock` 会当场自锁。
#: 作用域只限本进程 —— 两个 WebUI 实例同开一个记忆库仍会互相踩，
#: 但那不是今天的部署形态（记在案）。
_IO_LOCK = threading.RLock()


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 `os.replace` 换过去。

    为什么必须原子：后台抽取 fork 与用户轮是真会重叠的
    （实际 1.44s 重叠窗口），而
    `build_memory_index()` 在每个用户轮开头扫所有条目、`memory_search`
    也随时在扫。旧的写法是 `open(path, "w")` —— 先截断、再写，
    并发读者会读到半截文件，而 `parse_entry` 对坏文件返回 `None`
那条记忆在这一轮静默消失（不报错、不留痕，看起来就像"它没想起来"）。

    实际比例（写循环 × 并发读，3 秒）：9448 次读里 503 次读到残缺 = 5.32%。
    生产里写很少，概率低得多，但机制是真的、而且重叠已被证明可达。

    同一处风险还有账本：`_save_tombstones` 写一半被读到，`json.loads` 抛，
    `load_tombstones` 的 `except` 视为空账本，封条暂时失效。
    两条都走这个函数。

    临时文件必须同目录（同卷）—— 跨卷 `os.replace` 会退化成"先删后拷"，
    那就不原子了。文件名带 pid 是为了两个进程同时写同一条时不互相踩。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with _IO_LOCK:                      # 见 `_IO_LOCK` 的注释：Windows 上必须串起来
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, path)
        finally:
            # 换成功之后 tmp 已经不在了；失败时清掉，别在记忆目录里留垃圾。
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def _save_tombstones(tob: Dict[str, Dict]) -> None:
    """账本的唯一写点（`add` / `remove` 都走它 —— 两处各写一份 JSON 会漂）。"""
    _atomic_write_text(
        TOMBSTONES_PATH,
        json.dumps({"tombstones": tob}, ensure_ascii=False, indent=2, sort_keys=True),
    )


def add_tombstone(name_or_slug: str) -> str:
    """把一个条目记进账本（写入口）。返回 slug；空名字抛 ValueError。

    调它的只有用户动作（编辑界面）与测试 —— 抽取侧
    与整理侧都只读账本（"生成前先查"），
    写账本永远是用户动作。
    """
    slug = slugify(name_or_slug)
    if not slug:
        raise ValueError("空的 slug —— 没东西可记。")
    tob = load_tombstones()
    tob[slug] = {"name": str(name_or_slug).strip()[:MAX_NAME_CHARS],
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save_tombstones(tob)
    return slug


def remove_tombstone(name_or_slug: str) -> Dict:
    """撤销一条 tombstone（用户的"恢复"动作）。返回回执 dict。

    为什么必须有它：账本只进不出的话，一次误删就是永久的 ——
    而唯一的补救办法是手改 `data/memory/.tombstones.json`（那既不出声、也没人知道）。
    它不把条目内容恢复回来（内容在删除那一刻就没了）；它只是解除
    "这个名字不许再被写"的封条。恢复内容靠重新写一条。
    """
    slug = slugify(name_or_slug)
    tob = load_tombstones()
    if slug not in tob:
        return {"success": False, "error": f"`{name_or_slug}` 不在删除账本里。"}
    erased = tob.pop(slug)
    try:
        _save_tombstones(tob)
    except OSError as exc:
        return {"success": False, "error": f"账本写失败（{exc}）。"}
    print(f"[memory_store] 解除封条 {slug}（{erased.get('name') or slug}）")
    return {"success": True, "slug": slug, "name": erased.get("name") or slug}


def merge_tombstones(raw: Dict, path: Optional[Path] = None) -> Dict:
    """把另一份账本并进来（「导入记忆包」用）。只加不删。

    判据：导入别人的包不许解开本机的封条 —— 那会让已经删掉的条目
      在下一轮后台抽取时重新长出来（用户看到的是「我删过它，它又回来了」）。
    写盘复用 `_save_tombstones` 用的同一个原语 `_atomic_write_text`（原子换文件）。
    `path` 只有导入那条路（和测试）会传；主线永远用 `TOMBSTONES_PATH`。
    """
    p = Path(path) if path else TOMBSTONES_PATH
    if not isinstance(raw, dict):
        return {"success": False, "error": "账本形状不对（不是对象）"}
    tob = load_tombstones() if not path else {}
    if path:
        try:
            if p.is_file():
                tob = (json.loads(p.read_text(encoding="utf-8")) or {}).get(
                    "tombstones") or {}
        except (OSError, ValueError):
            tob = {}
    added = 0
    for slug, meta in raw.items():
        if not isinstance(meta, dict) or slug in tob:
            continue
        tob[str(slug)] = {
            "name": str(meta.get("name") or slug)[:MAX_NAME_CHARS],
            "at": str(meta.get("at") or time.strftime("%Y-%m-%dT%H:%M:%S")),
        }
        added += 1
    if added:
        _atomic_write_text(
            p,
            json.dumps({"tombstones": tob}, ensure_ascii=False,
                       indent=2, sort_keys=True),
        )
    return {"success": True, "added": added, "total": len(tob)}


def delete_entry(name: str, scope: str = DEFAULT_SCOPE, *,
                 seal: bool | None = None) -> Dict:
    """删一条记忆（用户动作，编辑界面）。返回回执 dict。

    顺序是先记账本、再删文件 —— 反过来的话，中间崩一下就得到
    "文件没了、账本也没记"的状态，而抽取下一轮就会把它重新生成
    （这恰恰是 tombstone 要防的那件事）。
    删除不可逆（内容不保留）—— 界面那边必须先问一次。
    """
    entry = read_entry(name, scope)
    if entry is None:
        return {"success": False, "error": f"没有 `{name}` 这条记忆。"}
    #: `seal=None`，按全局开关（`memory_auto_seal`，默认开）。
    #: 关掉之后：删掉的名字不再被封，后台抽取器可以把它再生成回来。
    _sealed = _auto_seal_enabled() if seal is None else bool(seal)
    if _sealed:
        add_tombstone(entry["name"])
    try:
        entry["path"].unlink()
    except OSError as exc:
        return {"success": False,
                "error": f"删文件失败（{exc}）—— 账本已记，重试即可。"}
    print(f"[memory_store] 删除记忆 {entry['slug']}.md（已记 tombstone）")
    return {"success": True, "slug": entry["slug"], "name": entry["name"],
            "sealed": _sealed}


def delete_all(scope: str = DEFAULT_SCOPE, *, seal: bool | None = None) -> Dict:
    """清空整个记忆库（用户动作）。返回回执 dict。

    与 `delete_entry` 同一条语义（记封条 + 删文件），只是成批做 ——
    用户看到的因此是「清空了，而且不会自己回来」。

    为什么不是"循环调 `delete_entry`"：`add_tombstone` 每次都
    `load_tombstones()` 再 `_save_tombstones()`，而后者是整表重写
    （`_atomic_write_text`：写 tmp + `os.replace`）。N 条就写 N 次盘、N 次 rename
条数一多，每多写一次就多一个中间态，中途崩一下会停在
    "删了一半、账本记了一半"。这里改成一趟：读一次账本、全部记上、写一次、
    再逐个删文件。

    顺序仍然是先记账本、再删文件：反过来的话，中间崩一下
    就得到"文件没了、账本也没记"，下一轮抽取会把它重新生成。

    `failed` 与"本来就空"分得开（读不到 ≠ 0）：
    空库返回 `count=0` 且 `failed=[]`；删一半出错两条都在返回值里。
    删不掉的条目封条已经落上了 —— 那一支停在"文件还在、名字已封"，
    是安全的那一侧（不会重生），并且如实报在 `failed` 里。
    删除不可逆（内容不保留）—— 界面那边必须先问一次；后路是逐条
    `remove_tombstone`（只解除封条，内容不回来）。
    """
    with _IO_LOCK:
        entries = list_entries(scope)
        if not entries:
            return {"success": True, "deleted": [], "failed": [], "count": 0}
        _sealed = _auto_seal_enabled() if seal is None else bool(seal)
        tob = load_tombstones()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        if _sealed:
            for e in entries:
                tob[e["slug"]] = {"name": str(e["name"])[:MAX_NAME_CHARS], "at": now}
        try:
            _save_tombstones(tob)          # 一趟只写这一次
        except OSError as exc:
            return {"success": False, "deleted": [], "failed": [], "count": 0,
                    "error": f"账本写失败（{exc}）—— 一条都没删。"}
        deleted: List[str] = []
        failed: List[Dict] = []
        for e in entries:
            try:
                e["path"].unlink()
                deleted.append(e["slug"])
            except OSError as exc:
                failed.append({"slug": e["slug"], "error": str(exc)})
        tail = f"，失败 {len(failed)}" if failed else ""
        print(f"[memory_store] 清空记忆库：删 {len(deleted)} 条、"
              f"落 {len(deleted)} 条封条{tail}")
        return {"success": not failed, "deleted": deleted, "failed": failed,
                "count": len(deleted)}


def list_tombstones() -> List[Dict]:
    """账本的列表视图（给编辑界面看）。按删除时间倒序。"""
    tob = load_tombstones()
    rows = [{"slug": s, **(v or {})} for s, v in tob.items()]
    rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return rows


# 写（`memory_write` 的真身）

def _auto_seal_enabled() -> bool:
    """「删除时自动落封条」这个全局开关。读不到 / 缺键 / 坏 JSON，默认开。

    默认开是安全的那个方向：封条挡的是"后台抽取器把删掉的事实又生成回来"。
    关掉之后那条保障就没了 —— 界面上必须把这句话说在开关旁边。
    """
    try:
        data = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
    except Exception:
        return True
    val = data.get("memory_auto_seal", True)
    return val if isinstance(val, bool) else True


def _memory_write_enabled() -> bool:
    """「写记忆」开关。读不到 / 缺键 / 坏 JSON，默认开
    （判据「读不到，默认」；本功能的设计默认就是开）。"""
    try:
        data = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
    except Exception:
        return True
    val = data.get("memory_write", True)
    return val if isinstance(val, bool) else True


def _validate(name: str, description: str, content: str) -> Optional[str]:
    """返回错误串（None = 过）。超限拒，不静默砍（纪律 ⑤）。"""
    if not name.strip():
        return "name 不能是空的（它是索引里那行的标题）。"
    if "\n" in name.strip() or "\n" in description:
        return "name 与 description 都要是**单行**（description 会进索引那一行）。"
    if len(name.strip()) > MAX_NAME_CHARS:
        return f"name 超过 {MAX_NAME_CHARS} 字符（现在 {len(name.strip())}）—— 请缩写。"
    if not description.strip():
        return "description 不能是空的（一句话钩子，索引靠它）。"
    if len(description.strip()) > MAX_DESC_CHARS:
        return f"description 超过 {MAX_DESC_CHARS} 字符（现在 {len(description.strip())}）—— 请缩写。"
    if not content.strip():
        return "content 不能是空的（正文才是真正的记忆）。"
    if len(content) > MAX_BODY_CHARS:
        return f"正文超过 {MAX_BODY_CHARS} 字符（现在 {len(content)}）—— 太长的东西拆成几条，或存成文件再指路。"
    if not slugify(name):
        return "这个名字转不出来合法的文件名（全是禁用字符）—— 换一个。"
    return None


def write_entry(name: str, description: str, content: str,
                scope: str = DEFAULT_SCOPE) -> Dict:
    """写一条记忆（新建或按 slug 更新）。返回回执 dict（风格同 `write_file`）。"""
    if not _memory_write_enabled():
        return {"success": False,
                "error": "记忆写入已在设置里关闭（data/settings.json 的 memory_write）。",
                "hint": "让用户去设置页打开「写记忆」。"}

    err = _validate(name, description, content)
    if err:
        return {"success": False, "error": err}

    name, description = name.strip(), description.strip()
    slug = slugify(name)

    tob = load_tombstones()
    if slug in tob:
        erased = tob[slug].get("name") or slug
        return {"success": False,
                "error": (f"这条被用户**删掉过**（{erased}）—— 删过的记忆不许重新生成"
                          "（tombstone）。要记新的请换一个名字。"),
                "hint": "这是用户的意图，不是技术故障。"}

    root = _scope_dir(scope)
    path = root / (slug + ".md")
    existed = path.exists()
    front = yaml.safe_dump({"name": name, "description": description},
                           allow_unicode=True, default_flow_style=False,
                           sort_keys=False).strip()
    doc = f"---\n{front}\n---\n\n{content.strip()}\n"
    try:
        # 原子写 —— 旧的 `open(path, "w")` 是"先截断再写"，
        #   并发读会看到半截，`parse_entry` 返回 None，那条记忆静默消失。
        _atomic_write_text(path, doc)
    except OSError as exc:
        return {"success": False, "error": f"写失败（{exc}）。"}

    print(f"[memory_store] {'更新' if existed else '新建'}记忆 {slug}.md")
    return {
        "success": True,
        "path": path.as_posix(),
        "slug": slug,
        "created": not existed,
        "index_line": _index_line({"slug": slug, "name": name,
                                   "description": description}),
    }


# 检索（`memory_search` 的真身）—— 零 LLM 全扫

def search_entries(query: str, limit: int = DEFAULT_SEARCH_LIMIT,
                   scope: str = DEFAULT_SCOPE) -> Tuple[List[Dict], int]:
    """返回 `(命中列表, 扫过的条数)`。打分：名字 3 / 描述 2 / 正文 1（每项封顶一次）。"""
    entries = list_entries(scope)
    terms = [t for t in re.split(r"\s+", (query or "").strip().lower()) if t]
    hits: List[Dict] = []
    if terms:
        for e in entries:
            score = 0
            hay_name, hay_desc, hay_body = (
                e["name"].lower(), e["description"].lower(), e["body"].lower())
            for t in terms:
                if t in hay_name:
                    score += 3
                if t in hay_desc:
                    score += 2
                if t in hay_body:
                    score += 1
            if score:
                hits.append({**e, "score": score})
    hits.sort(key=lambda e: (-e["score"], e["slug"]))
    limit = max(1, min(int(limit or DEFAULT_SEARCH_LIMIT), MAX_SEARCH_LIMIT))
    return hits[:limit], len(entries)


# 派生索引（`build_memory_index` + `append_memory_index`）

def _index_line(entry: Dict) -> str:
    return f"- [{entry['name']}]({entry['slug']}.md) — {entry['description']}"


def build_memory_index(scope: str = DEFAULT_SCOPE) -> str:
    """派生索引文本（空库返回空串 —— 调用方据此跳过注入）。

    纪律 ①：超 200 行 / 25,000 字符，截断 + 出声（print）。
    纪律 ②：返回前过 `sanitize_text`（框架块不变量）。
    """
    entries = list_entries(scope)
    if not entries:
        return ""

    lines: List[str] = []
    chars = 0
    truncated_by = ""
    for e in entries:
        line = _index_line(e)
        if len(lines) >= MEMORY_INDEX_MAX_LINES:
            truncated_by = f"{MEMORY_INDEX_MAX_LINES} 行"
            break
        if chars + len(line) + 1 > MEMORY_INDEX_MAX_CHARS:
            truncated_by = f"{MEMORY_INDEX_MAX_CHARS} 字符"
            break
        lines.append(line)
        chars += len(line) + 1
    if truncated_by:
        print(f"[memory_store] 索引超过 {truncated_by} —— 只列了前 {len(lines)} 条"
              f"（该做一次「记忆巩固」把它降下来）")

    return sanitize_text("\n".join(lines))


def append_memory_index(messages: List[dict], index_text: str) -> List[dict]:
    """把索引附在最后一条 user 消息的尾巴上，返回新的列表。

    三条刻意选择与 `append_skill_index` 逐字相同（副本不改原表 / 附在尾部
    而不是新加一条 user 消息 / 找不到 user 消息就不注入），理由见那里。
    顺序：调用方先 append 技能索引、再 append 本索引
    —— 新块追加在最后，已有字节一个不动。
    """
    if not index_text:
        return messages

    block = (
        "\n\n<memory_index>\n"
        "以下是你的**长期记忆**索引（跨会话的持久事实，由记忆库实时生成，"
        "不是用户的话，不用回答它）。要某条的全文用 `memory_read(name)`；"
        "想记新的用 `memory_write`；检索用 `memory_search(query)`。\n"
        f"{index_text}\n"
        "</memory_index>"
    )

    # 与技能索引同一个函数（同一条判据，见
    # `core/image_input.append_text_block`）—— 带图那一轮那条 user 消息是块列表，
    # 从前这里整支跳过，每一个图片轮上记忆索引也静默不挂。
    return append_text_block(messages, block)


# 三份 schema（每请求的永久税，改措辞必须重量一次）

MEMORY_READ_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "memory_read",
    "description": (
        "读长期记忆里的一条（name 取自记忆索引里 `[...]` 那个标题）。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "记忆标题（索引里的 `[...]`）"},
        },
        "required": ["name"],
    },
}

MEMORY_SEARCH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "memory_search",
    "description": (
        "在长期记忆里检索（名字 / 描述 / 正文一起扫）。"
        "返回命中条目的标题与描述 —— 要全文再用 memory_read。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要搜的词（可多个，空格分隔）"},
            "limit": {"type": "integer",
                      "description": f"最多回几条（默认 {DEFAULT_SEARCH_LIMIT}）"},
        },
        "required": ["query"],
    },
}

MEMORY_WRITE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "memory_write",
    # 从 system prompt 搬进来的行为规则。
    #   那段「How:」原来在 `prompt_assembler._CORE_INSTRUCTIONS` 里，
    #   406 字符，每次请求都交一遍 —— 而它的三个数（≤40/≤120/≤4000）
    #   在下面三个参数的 description 里逐字就有（`MAX_*_CHARS` 一处定义）。
    #两套真相源（不在 system prompt 里镜像工具目录）。
    #   不能整段删掉 —— 里面还有四条行为规则：
    #     先搜再写 / 同名的更新不重复 / 写错在源头改写 / 索引是派生的。
    #     搬到这里是因为它就该在读的人正要动手的那一刻出现（just-in-time）。
    "description": (
        "新建 / 更新一条长期记忆（跨会话应该记住的事实、偏好、决定）。"
        "**先 `memory_search` 再写**；同名的更新、写错在源头改写。索引是派生的，别手工维护。"
        "**一次性的对话内容不要写进来。**"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": f"标题（≤{MAX_NAME_CHARS} 字符，单行；同标题=更新那条）"},
            "description": {"type": "string",
                            "description": f"一句话钩子（≤{MAX_DESC_CHARS} 字符，会进索引）"},
            "content": {"type": "string",
                        "description": f"正文（≤{MAX_BODY_CHARS} 字符）"},
        },
        "required": ["name", "description", "content"],
    },
}


# 三个 handler（`memory_read` / `memory_search` 返 str；`memory_write` 返 dict）

def memory_read(name: str = "") -> str:
    """读一条的全文。永远返回 str（在 `PLAIN_TEXT_TOOLS` 里 —— 见 `file_tools` 纪律 ③）。

    巩固模式下它先认 `logs/…` / `sessions/…` 两类只读素材 ——
    那两行分支在主线（模式关）根本不进。
    """
    src = _read_organize_source(name)
    if src is not None:
        return src
    entry = read_entry(name)
    if entry is None:
        entries = list_entries()
        if not entries:
            return "没有这条记忆，而且记忆库还是空的。"
        names = "、".join(e["name"] for e in entries[:20])
        more = f"（共 {len(entries)} 条）" if len(entries) > 20 else ""
        return (f"没有 `{name}` 这条记忆。现有：{names}{more}。"
                f"找不到要找的，可以用 `memory_search(query)` 搜一下。")

    body = entry["body"]
    truncated = ""
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS]
        truncated = f"\n\n[内容超过 {MAX_READ_CHARS} 字符，已截断]"
    return f"# {entry['name']}\n{entry['description']}\n\n{body}{truncated}"


def memory_search(query: str = "", limit: int = DEFAULT_SEARCH_LIMIT) -> str:
    """检索。永远返回 str。没命中时把"扫了几条"说出来（结论 vs 搜不到）。

    巩固模式下空查询 = 列出全部索引（整理需要一个 `ls`）——
    主线（模式关）仍然是"没给 query"那句。
    """
    q = (query or "").strip()
    if not q:
        if _ORGANIZE_ROOTS.get() is not None:
            index = build_memory_index()
            if not index:
                return "（巩固模式）记忆库是空的 —— 还没有任何条目。"
            return ("（巩固模式）当前全部记忆的索引（要全文用 memory_read(name)）：\n"
                    + index)
        return "没有给 query —— 说要搜什么词。"
    hits, total = search_entries(q, limit)
    if not hits:
        return f"搜完了全部 {total} 条，没有匹配 `{q}`。"
    lines = [f"命中 {len(hits)} 条（共 {total} 条）："]
    for h in hits:
        lines.append(_index_line(h))
    lines.append("要全文用 `memory_read(name)`。")
    return "\n".join(lines)


def memory_write(name: str = "", description: str = "", content: str = "") -> Dict:
    """写一条。回执 dict（风格同 `write_file`）。"""
    return write_entry(name, description, content)


MEMORY_DELETE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "memory_delete",
    #: 描述里必须写反向判据（什么时候别用）—— 删除不可逆，
    #:   模型自己"顺手清理"是这一类工具最常见的滥用。
    "description": (
        "删除一条长期记忆。**内容不保留**。只在用户说这条不对 / 过期 / 让你删掉时用；"
        "不确定是哪一条就先 `memory_search`。删除前先跟用户说清删的是哪一条。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "要删的条目标题（或它的 slug）"},
        },
        "required": ["name"],
    },
}


def memory_delete(name: str = "") -> Dict:
    """删一条。回执 dict（风格同 `write_entry`）。

    与界面同一个动作（`delete_entry`），封条与否由同一个全局开关
    决定（`memory_auto_seal`），不会出现"界面上关了、模型这边还封"。
    """
    nm = str(name or "").strip()
    if not nm:
        return {"success": False,
                "error": "要删哪一条？请传 `name`（不确定先用 `memory_search`）。"}
    res = delete_entry(nm)
    if res.get("success"):
        res["note"] = (
            "已删除，内容不保留。" + ("这个名字已被封条封住，之后不会再被自动生成。"
                                     if res.get("sealed")
                                     else "自动封条已关闭 ⇒ 后台抽取可能把它再生成回来。")
            + "要恢复同名条目：在设置页解除封条后重新写。"
        )
    return res
