"""改前内容的留档 —— 右侧栏 diff 的底座。

## 为什么必须自己存一份

diff 要两侧。改后我们随时有（读磁盘就是）；改前没有 —— 要它就得
把用户文件的内容留一份。常见实现里多数都这么做（不能拿工具入参里的
`old_string` 当 before —— 那等于"模型说改哪就改哪"，它可能漏掉真实内容）。

它同时是文件恢复的另一半 —— 那份"改前内容"不只是给 diff 看的，
它就是 rewind 的底座。文件恢复用的是另一个抓取层（git 快照，覆盖更全）
别把"改前"存两遍。

## 四条判据

| # | 判据 | 为什么 |
|---|---|---|
| ① | 留改前内容 | 不留就没有 diff，也没有恢复 |
| ② | 新键 `file_history_days`（默认 30） | 与 `cleanupPeriodDays` 分开 —— 这是用户文件的副本，用户要单独控它 |
| ③ | 单文件 2 MiB 封顶，超了如实标「拿不到 diff」 | 不拿 `0` 兜底（读不到 ≠ 0） |
| ④ | 「立即清理」按钮 | 回收复用 `core/retention.py::prune_generic` |
| ⑤ | 总空间上限 | ③ 只封单个文件，④ 只按天清 —— 整个目录多大没人管。见下 |

## ⑤ 总空间上限

「备份空间大小」。两条成对，缺一条就不成立：

  · 上限（`MAX_TOTAL_MB`，默认 256 MiB，设置键 `file_history_max_mb`）
  · 腾地方的方向 —— 超了从最旧的开始删，删到装得下。

为什么是"删旧的"而不是"拒新的"：拒新的会让上限一满就从此没有 diff
（而且是静默的 —— 用户看到的是"查看改动文件"里一个空白面板）。
留档的价值随时间衰减（越旧越不可能要回滚），而新的那份永远最相关
牺牲最旧的。同族判据：`core/retention.py` 那句「一个『删』的动作，
读不到值时的真值不是"默认"，是"不动"」—— 这里是"读不到上限，不腾地方"。

腾地方排在"写下去"之前（`_make_room` 在 `snapshot_before` 里先调）——
先腾再写，新那一份永远不会被自己挤掉（按 mtime 从旧到新删，刚写的那个最新）。

落点：`data/runtime/`（运行数据不进源码目录）。它是用户文件的内容
目录在 `data/` 底下，而 `data/` 在 `run_command` 的 `PROTECTED_PATHS` 里
（模型不能拿一条 shell 把它搬走）。

## 接线点：一处钩子（`SecurityGuard.validate_path` 的写类 op）

与 `core/touched_files.py` 同一个钩子、同一条理由（"不列写工具名单"）——
它在 `SecurityGuard._note_touched` 里紧随 `note()` 之后调，**只在那个路径
本轮第一次被碰时**（`note()` 返回 True）才留档，一个文件一轮只留一份"改前"。

## diff 怎么算出来（这一条是"为什么版本号有用"）

`<会话>/<路径哈希>@v<N>` 是改动之前的内容。而 `<...>@v<N+1>` 是
下一次有人改它时留的"改前" —— 也就是这一次的改后。

    before(N) = 读 `@v<N>`
    after(N)  = 读 `@v<N+1>`（存在就用它）否则读磁盘

第 N 版之后的文件若再没被碰过，`after` 就是磁盘上的现状（正确）；
若被碰过，`after` 是彼时的快照（也正确 —— 而且不回受之后改动的影响）。
唯一的例外是如实标的边界：`run_command` 改的文件不算，
所以它能悄悄挪动 `after`。那与本模块的取舍同源，不是缺陷。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

__all__ = [
    "ROOT", "MAX_BYTES", "SETTING_KEY", "DEFAULT_DAYS",
    "TOTAL_SETTING_KEY", "DEFAULT_TOTAL_MB", "MAX_TOTAL_MB",
    "snapshot_before", "latest_version", "read_version", "diff_for",
    "created_in_session",
    "configured_days", "configured_max_mb", "total_bytes", "prune",
    # 轮次索引 / 封印 / 恢复
    "seal_turn", "plan_restore", "apply_restore",
]

#: 轮次索引的落点（每个会话目录一个）。
#: 它不匹配 `<sha>@v<N>` 那个模式，`_versions()` 不会把它当成一版内容。
_INDEX_NAME = "index.json"

#: 封印时用的哨兵：这个文件彼时不在了（与"没封印"是两件事，
#: 读不到 ≠ 0）。
_GONE = "GONE"

#: 存哪儿。运行数据一律进 `data/runtime/`（测试的沙箱也覆盖它）。
ROOT = Path("data/runtime/file_history")

#: 单文件封顶 —— 2 MiB（超过就只留标记、不存内容）。
MAX_BYTES = 2 * 1024 * 1024

#: 保留期那个键。与 `cleanupPeriodDays` 分开——
#: 那是"框架自己的留档"，这是"你的文件的一份副本"。
SETTING_KEY = "file_history_days"
DEFAULT_DAYS = 30

#: 整个目录的上限（「备份空间大小」）。
#: 单位取 MiB（界面上让人填"多少 MB"比填字节数合理）。
#: 与 `MAX_BYTES` 的关系是两条独立的轴：那条封一个文件，这条封一个目录。
TOTAL_SETTING_KEY = "file_history_max_mb"
DEFAULT_TOTAL_MB = 256

#: `/rewind` 时要不要把工作区文件也退回去。关掉 = 只退对话。
RESTORE_SETTING_KEY = "rewind_restore_files"
#: 兜底上下限 —— 防一个手抖的 `0` 把刚落盘的留档立刻删光
#: （`0` 不是"不留"，是"读不懂"，同 `core/retention.py` 那条方向）。
MIN_TOTAL_MB = 1
MAX_TOTAL_MB = 10240

_SETTINGS_PATH = Path("data/settings.json")

#: 三种留档形态（写在扩展名上，读的人一眼分得出"是内容 / 是新文件 / 太大"）：
#:   `.txt` = 内容留下来了 · `.new` = 这个文件彼时还不存在 · `.big` = 超过封顶
_SUFFIXES = (".txt", ".new", ".big")


# 设置（读不到，不清理，与 `core/retention.py` 同一条方向）

def configured_days(path: Optional[Path] = None) -> Optional[int]:
    """读 `file_history_days`；读不到 / 坏值，`None`（调用方约定：不清理）。

    与 `core/retention.py::configured_cleanup_days` 同形同向，但**故意的
    两份** —— 它们读的是两个不同的键（两个键分开，互不牵连）。
    `isinstance(True, int)` 是 `True`，布尔要单独挡。
    """
    p = path or _SETTINGS_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get(SETTING_KEY, DEFAULT_DAYS)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not (1 <= value <= 3650):
        return None
    return value


def configured_max_mb(path: Optional[Path] = None) -> Optional[int]:
    """读总空间上限（MiB）；读不到 / 坏值，`None`（约定：不腾地方）。

    与 `configured_days` 同形同向，但读的是另一个键 —— 它们是两根轴
    （一个管"活多久"，一个管"占多大"），合成一个数会让调其中一个静默动到另一个。
    """
    p = path or _SETTINGS_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get(TOTAL_SETTING_KEY, DEFAULT_TOTAL_MB)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not (MIN_TOTAL_MB <= value <= MAX_TOTAL_MB):
        return None
    return value


# 空间账

def _iter_files():
    """`(mtime, size, Path)` —— `ROOT` 底下所有文件，按 mtime 升序（最旧的在前）。

    不按扩展名筛：上限管的是"这个目录占多大"，所以口径必须是目录实际有多大
    —— 只数 `_SUFFIXES` 会让一个卡在半路的 `.tmp`（`_atomic_write_bytes` 崩在
    `os.replace` 之前）永远不被算进去、也永远删不掉。
    """
    out = []
    if not ROOT.is_dir():
        return out
    try:
        for p in ROOT.rglob("*"):
            try:
                if not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            out.append((st.st_mtime, st.st_size, p))
    except OSError:
        return out
    out.sort(key=lambda t: t[0])
    return out


def total_bytes() -> int:
    """这个目录现在占多少字节。纯读。"""
    return sum(size for _m, size, _p in _iter_files())


def _make_room(incoming: int) -> int:
    """腾出至少 *incoming* 字节，返回删了几个文件。

    从最旧的开始删（`_iter_files` 已按 mtime 升序）—— 见模块 docstring 判据 ⑤：
    留档的价值随时间衰减，而拒新的会让上限一满就从此没有 diff（静默）。

    读不到上限，一个都不删（同 `configured_days` 的方向）。
    永不抛 —— 它在写路径上。
    """
    cap_mb = configured_max_mb()
    if cap_mb is None:
        return 0
    cap = cap_mb * 1024 * 1024
    removed = 0
    try:
        total = total_bytes()
        if total + incoming <= cap:
            return 0
        # 删到 `total + incoming <= cap` 为止；删不动（权限/占用）就停手 ——
        # 死循环比"没腾够"坏得多（那会让一次写入卡住）。
        for _m, size, p in _iter_files():
            if total + incoming <= cap:
                break
            try:
                p.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        if removed:
            print("[file_history] 超过总空间上限 %d MiB，从最旧的开始清掉了 %d 份"
                  % (cap_mb, removed))
    except Exception as exc:                      # noqa: BLE001
        print("[file_history] 腾地方失败（不影响写入）：%s: %s"
              % (type(exc).__name__, exc))
    return removed


# 路径 → 文件名

def _path_hash(path: str) -> str:
    """路径的短哈希 —— 与常见的留档命名 `<hash>@v<N>` 同形。

    必须先归一化：同一条路径在本仓有
    两个写法 —— 留档那一侧（`SecurityGuard.validate_path`）拿到的是
    Windows 反斜杠的原始串，而 `core/touched_files.py::note` 把它归一成
    `Path(...).resolve().as_posix()`（正斜杠）再交给 `_touched_payload`
`files.changed` 事件里带的是正斜杠那份。

    哈希原始字符串的话，两边算出来的键不是同一个。
    盘上留档叫 `863e917ad275@v1`（反斜杠那份），而
    `latest_version()` 拿正斜杠去查 `0859104d238b@v`，恒返回 0
事件里 `version` 恒为 0，点文件名永远说「这个文件没有留档」，
    `/api/file-diff` 同病（它走的是同一个哈希）。

    兜底那支不是给正常路径用的：`resolve()` 理论上有极小概率抛
    （奇怪盘符/权限），而它在写路径上，绝不许因此拦住一次写入。
    """
    raw = str(path or "")
    try:
        key = Path(raw).resolve().as_posix() if raw else ""
    except Exception:                              # noqa: BLE001
        key = raw.replace("\\", "/")
    return hashlib.sha1(
        key.encode("utf-8", "replace")).hexdigest()[:12]


def _session_dir(session_id: str) -> Path:
    """会话目录名：可读前缀 + 短哈希。

    只做"替换非法字符 + 截断"不够 —— 两件事都能让不同的会话键落到同一个
    目录：替换会把 `a:b` 与 `a_b` 变成同一个名字，截断会让前 64 字符相同的
    两个键共用一份留档。而这里的目录装的是用户文件的内容，串了就是
    把别人的旧版本写回去。

    前缀保留可读性（排查时一眼看出是哪条会话），哈希保证唯一。
    """
    raw = str(session_id or "")
    s = re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:48]
    digest = hashlib.sha256(
        raw.encode("utf-8", "replace")).hexdigest()[:12]
    return ROOT / ("%s-%s" % (s or "_", digest))


def _versions(session_id: str, path: str) -> List[Tuple[int, str]]:
    """这个路径在这个会话里已有的留档：`[(版本号, 形态), ...]`，按版本升序。"""
    d = _session_dir(session_id)
    if not d.is_dir():
        return []
    prefix = _path_hash(path) + "@v"
    out: List[Tuple[int, str]] = []
    try:
        for p in d.iterdir():
            name = p.name
            if not name.startswith(prefix):
                continue
            body = name[len(prefix):]
            for suf in _SUFFIXES:
                if body.endswith(suf):
                    try:
                        out.append((int(body[:-len(suf)]), suf))
                    except ValueError:
                        pass
                    break
    except OSError:
        return []
    return sorted(out)


# 轮次索引
#
# ## 它补的是哪一格
#
# 留档存的是改前内容。而 `/rewind` 要回答的是"**第 N 轮结束时这个文件
# 长什么样**" —— 这一个问题靠"改前"单独一件答不了：改前只知道
# "我们改它之前它是什么"，不知道"那是第几轮的事"。
#补一张 `{路径: [{版本, 轮次, 封印哈希}]}` 的索引，每题各一格。
#
# ## 三个词
#
# · 版本 —— 已有的那个（`<sha>@v<N>.txt`）。
# · 轮次 —— 拍这一版的时候是第几轮（`short_term.thread_turn_count`，
#   它在一轮之内稳定，同一轮里先后碰的两个文件拿到同一个号）。
# · 封印 —— 这一版之后、那一轮结束时这个文件是什么（sha256）。
#   它是安全闸的输入：写回之前要核对"现在的内容还是我们上次留下的样子吗"，
#   不符就是用户自己动过 -> 拒，不许覆盖（判据与"外部改过就不动"那族同源）。
#   什么时候盖：轮末（`record_turn` 调 `seal_turn`）—— 每轮每路径一个哈希。

def restore_enabled() -> bool:
    """`rewind_restore_files`：回退时要不要连工作区文件一起退。

    读不到 / 缺键 / 坏值，默认开 —— 与上面 `configured_days` 那条
    方向相反，而这是有意的：那个是清理（读不到就别删用户的留档），
    这个是回退的一部分。
    读不到时退回"只退对话"会让用户拿到一个静默的半截：
    他以为回到了过去，文件却还是那之后的样子。
    同 `memory_store._memory_write_enabled` 那条判据（读不到，默认）。
    """
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return True
    val = data.get(RESTORE_SETTING_KEY, True)
    return val if isinstance(val, bool) else True


def _index_path(session_id: str) -> Path:
    return _session_dir(session_id) / _INDEX_NAME


def _load_index(session_id: str) -> dict:
    """读索引。读不到 / 坏了 / 形状不对，空字典。

    与 `configured_days` 同一条方向（读不到，退到一个保守的行为）：
    索引没了只是"不知道哪一版对应哪一轮"，回退那半边拒，
    而 diff 那半边照旧能用（它不依赖这张表）。
    """
    try:
        p = _index_path(str(session_id or ""))
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_index(session_id: str, data: dict) -> None:
    try:
        _atomic_write_bytes(
            _index_path(str(session_id or "")),
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        )
    except OSError as exc:
        print("[file_history] 轮次索引写失败（不影响写入）：%s" % exc)


def _record_version(session_id: str, path, version: int, turn: int) -> None:
    """把"这一版属于第几轮"记进索引。永不抛（它在写入热路径上）。"""
    try:
        idx = _load_index(session_id)
        idx.setdefault(str(path), []).append(
            {"v": int(version), "turn": int(turn), "after": None})
        _save_index(session_id, idx)
    except Exception as exc:  # noqa: BLE001
        print("[file_history] 轮次记账失败（不影响写入）：%s" % exc)


def _sha256_file(p: Path) -> Optional[str]:
    """文件的 sha256；不在，`_GONE`；读不动，`None`（读不到 ≠ 空）。"""
    try:
        if not p.is_file():
            return _GONE
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


# 留档

def snapshot_before(session_id: str, path, turn: int = 0) -> Optional[int]:
    """改之前留一份。返回版本号；留不下（读失败 / 没会话）返回 `None`。

    永不抛 —— 它在 `SecurityGuard.validate_path` 的热路径上，
    而"留档失败"绝不该拦住用户的一次写入（同 `core/touched_files.note`）。
    """
    sid = str(session_id or "")
    if not sid:
        return None
    try:
        src = Path(str(path))
        existing = _versions(sid, str(path))
        version = (existing[-1][0] + 1) if existing else 1
        d = _session_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        base = d / ("%s@v%d" % (_path_hash(str(path)), version))

        if not src.is_file():
            # 「这个文件彼时还不存在」是一件要记下来的事实 ——
            #   否则 diff 那一侧只能猜（拿空串当 before 会把"新建"读成"清空"）。
            base.with_suffix(".new").write_bytes(b"")
            _record_version(sid, str(path), version, turn)
            return version

        try:
            size = src.stat().st_size
        except OSError:
            return None
        if size > MAX_BYTES:
            # 不截断（截断出来的 diff 会撒谎：看起来像"只改了前半篇"）
            #留一个标记，让界面上如实说"拿不到 diff"（判据 ③）。
            base.with_suffix(".big").write_bytes(b"")
            print("[file_history] %s 有 %d 字节 > 封顶 %d —— 只留标记，不存内容"
                  % (src.name, size, MAX_BYTES))
            _record_version(sid, str(path), version, turn)
            return version

        # 先腾地方，再写下去。顺序是承重的 —— 见判据 ⑤：
        #   先写后腾的话，`_make_room` 有可能把刚写的那一份当成"最旧的"
        #   删掉（上限比 `MAX_BYTES` 还小时就是它），于是留档静默地没留下。
        payload = src.read_bytes()
        _make_room(len(payload))
        _atomic_write_bytes(base.with_suffix(".txt"), payload)
        _record_version(sid, str(path), version, turn)
        return version
    except Exception as exc:  # noqa: BLE001
        print("[file_history] 留档失败（不影响写入）：%s: %s"
              % (type(exc).__name__, exc))
        return None


def _atomic_write_bytes(dst: Path, data: bytes) -> None:
    """原子写（tmp + `os.replace`）—— 与 `core/allowlist.save` 同形。

    不原子的话，一个正在被 `GET /api/file-diff` 读的版本会读到半截内容，
    而那在 diff 里看起来像"文件被删掉了大半"（记忆库写不原子
并发读静默丢条目，同一个形状）。
    """
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def latest_version(session_id: str, path) -> int:
    """这个路径在这个会话里最新的版本号；没有留档，`0`。"""
    v = _versions(str(session_id or ""), str(path))
    return v[-1][0] if v else 0


def created_in_session(session_id: str, path) -> bool:
    """这个路径是不是本会话里我们第一次碰它时它还不存在（= 是我们造出来的）。

    ## 为什么要读这个标记

    `snapshot_before()` 在"这个文件彼时还不存在"时留一个 `.new` 标记
判据本来就落盘了，读它即可。

    ## 它买的是什么

    分类器那条「不可逆销毁」规则带清空条件「那些目标是本会话里刚建出来的」
    （`core/auto_review.py::_BLOCK_GROUPS`）—— 但那是LLM 从转写里猜：
    转写被压缩过、或那个文件是 `run_command` 里的脚本造的，它看不见
判成"不是自建"，拒/弹窗。而这件事我们手上有确定数据。
读它，就是把这个放行口从"猜"换成"查"。

    ## 三条边界（都往不判那一侧倒）

    · 只认第一版（`@v1`）—— 中间某一版是 `.new` 说明"那时它被删过"，
      与"是不是我们造的"是两件事；
    · 没有留档，`False`（判不了 = 不放过）—— 读不到 != 0；
    · `run_command` 改的文件本来就不留档（本模块自己的取舍）
脚本造出来的文件在这里同样判成"不是自建"，照旧过闸。
      方向是安全的那一侧，如实记在这里。

    ##  本模块里"形态"有两套约定，差一个点

    · `_versions()` 返回的是 `_SUFFIXES` 里那个串，`'.new'`（带点）；
    · `read_version()` 返回的是文档化的三选一，`'new'`（不带点）。

拿 `== "new"` 去比 `_versions()` 的结果恒为假，而症状是"这层永远不跳过"
    （看着像"功能没生效"，不像"比错了格式"）。
    这里用便宜的 `_versions()`（只读目录名、不读内容）并比 `'.new'`；
    这条判据要有前提自检 —— 判据恒不成立时"反面"也会一起"通过"。
    """
    try:
        vs = _versions(str(session_id or ""), str(path))
    except Exception:                              # noqa: BLE001
        return False
    if not vs:
        return False
    return vs[0][1] == ".new"


def read_version(session_id: str, path, version: int) -> Tuple[str, str]:
    """读某一版。返回 `(形态, 文本)`。

    形态三选一：`"txt"`（内容）· `"new"`（彼时不存在）· `"big"`（超过封顶）·
    `""`（这一版根本不存在 —— 读不到 ≠ 0）。
    """
    d = _session_dir(str(session_id or ""))
    base = d / ("%s@v%d" % (_path_hash(str(path)), int(version)))
    try:
        p = base.with_suffix(".txt")
        if p.is_file():
            try:
                return "txt", p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        if base.with_suffix(".new").is_file():
            return "new", ""
        if base.with_suffix(".big").is_file():
            return "big", ""
    except OSError:
        pass
    return "", ""


def _version_bytes(session_id: str, path, version: int) -> Optional[bytes]:
    """某一版的原始字节（写回用）。

    不许拿 `read_version` 代替 —— 那个返回的是文本
    （`read_text(..., errors="replace")`），拿它写回一个非 UTF-8 的文件
    会把认不出的字节静默换成 U+FFFD（即一个替换字符），文件被改坏，
    而且没人报错。
    """
    p = _session_dir(str(session_id or "")) / (
        "%s@v%d.txt" % (_path_hash(str(path)), int(version)))
    try:
        return p.read_bytes() if p.is_file() else None
    except OSError:
        return None


# 封印 / 计划 / 执行

def seal_turn(session_id: str, turn: int = 0) -> int:
    """轮末把"这一轮改完之后长什么样"记进索引，返回封印了几条。

    只碰每个路径的最后一版且还没封印的那些 —— 那一版的"之后"
      就是现在（此后没有别的改动），一个哈希就够，代价 = 每轮 O(路径数)。
    永不抛：轮末的记账，坏掉不该影响一轮对话。
    """
    sid = str(session_id or "")
    if not sid:
        return 0
    try:
        idx = _load_index(sid)
        n = 0
        for path, entries in idx.items():
            if not entries:
                continue
            last = entries[-1]
            if last.get("after"):
                continue
            h = _sha256_file(Path(path))
            if h is None:              # 读不动，不封印（下次再说）
                continue
            last["after"] = h
            n += 1
        if n:
            _save_index(sid, idx)
        return n
    except Exception as exc:  # noqa: BLE001
        print("[file_history] 轮末封印失败（不影响本轮）：%s" % exc)
        return 0


def plan_restore(session_id: str, before_turn: int) -> List[dict]:
    """回到第 *before_turn* 轮之前：每个被改过的文件该退到哪一版。

    先把两个"第几"对齐（这一格最容易错一位）：
    `turn` 记的是"拍这一版时这条线程已经完成了几轮"
    （`short_term.thread_turn_count`，在第 K 轮里它等于 K-1）。

回到第 N 轮之前 = 要"第 N-1 轮结束时那个文件的样子"
      = 那些 `turn >= N-1` 的版本里最早那一版的「改前」
      （那一版是第 N-1 轮之后第一次改它时拍的，它拍到的正是"在那之前"）。

      · 一条都不满足，此后没被我们改过，不出现在计划里
      · `kind == "new"`，彼时它还不存在，恢复 = 删掉它
      · `blocked` 非空，不许动，逐条给人话

    安全闸：核对"现在的内容还是我们上次留下的那份吗"
    （拿 `seal_turn` 存的那个哈希比）。不符，`blocked`。
    用户可能自己在编辑器里改过 —— 那正是"不许静默改坏"要拦的事。
    """
    sid = str(session_id or "")
    if not sid:
        return []
    cutoff = int(before_turn) - 1
    idx = _load_index(sid)
    out: List[dict] = []
    for path, entries in sorted(idx.items()):
        later = [e for e in entries if int(e.get("turn", -1)) >= cutoff]
        if not later:
            continue
        first = later[0]
        kind, _text = read_version(sid, path, first["v"])
        last = entries[-1]
        blocked = ""
        if not kind:
            blocked = "这一版的留档不在了"
        elif kind == "big":
            blocked = "它超过单文件封顶，只留了标记、没有内容"
        elif not last.get("after"):
            blocked = "它还没有被封印（认不出现在这份是不是我们留下的）"
        else:
            now = _sha256_file(Path(path))
            if now is None:
                blocked = "读不动它"
            elif now != last["after"]:
                blocked = "它**被外面改过**（现在这份不是我们上次留下的）"
        out.append({"path": path, "version": first["v"],
                    "turn": int(first.get("turn", -1)),
                    "kind": kind, "blocked": blocked,
                    # 校验过的那份哈希要跟着计划走 —— 检查与写入之间还有一段时间，
                    # 执行侧要靠它再核一次（不然计划之后被改过的内容会被覆盖）。
                    "expected_after": last.get("after") or ""})
    return out


def apply_restore(session_id: str, plan: List[dict]) -> Tuple[int, List[str]]:
    """执行 `plan_restore` 的计划。返回 `(动了几条, 逐条人话)`。

    全批要么都过、要么一条都不写。三条检查都在写第一个字节之前：

      ① `blocked`（安全闸，来自 `plan_restore`）；
      ② 每一条要写的内容都先读回内存（读不回的整批不动）；
      ③ 每个目标文件的现状也先读一份 —— 写的中途失败时用它还原。

    ②③ 都过了才动手。写入中途失败（磁盘满 / 权限）会把已经动过的逐个还原，
    还原失败的那几个如实报出来 —— "回退了一半"必须是能看见的结果，
    不是一句"已回退"。
    """
    sid = str(session_id or "")
    lines: List[str] = []
    blocked = [it for it in plan if it.get("blocked")]
    if blocked:
        for it in blocked:
            lines.append("· **没动** `%s` —— %s" % (it["path"], it["blocked"]))
        lines.append("★ **一个文件都没写**：有一条过不了安全闸，整批停下"
                     "（宁可不动，也不动一半）。")
        return 0, lines

    # ① 读齐要写的内容
    jobs: List[dict] = []
    unreadable: List[dict] = []
    for it in plan:
        payload = None
        if it["kind"] != "new":
            payload = _version_bytes(sid, it["path"], it["version"])
            if payload is None:
                unreadable.append(it)
                continue
        jobs.append({"it": it, "payload": payload, "existed": False,
                     "original": b""})
    if unreadable:
        for it in unreadable:
            lines.append("· **没动** `%s` —— 留档读不回来" % it["path"])
        lines.append("★ **一个文件都没写**：有一条的内容读不回来，整批停下"
                     "（宁可不动，也不动一半）。")
        return 0, lines

    # ② 读现状（回滚用），同时再核一次哈希 —— 计划是刚才算的，
    #    而"刚才"到现在之间文件可能被用户、同步盘或另一个会话改过。
    #    `plan_restore` 核过一次，这里核的是同一份计划在执行那一刻的样子
    #    （TOCTOU 窗口：不核就会把新内容当成"我们上次留下的"覆盖掉）。
    capture_failed: List[dict] = []
    drifted: List[dict] = []
    for job in jobs:
        it = job["it"]
        p = Path(it["path"])
        try:
            job["existed"] = p.exists()
            job["original"] = p.read_bytes() if job["existed"] else b""
        except OSError as exc:
            capture_failed.append(it)
            lines.append("· **没动** `%s` —— 读不了它现在的内容（%s）"
                         % (it["path"], exc))
            continue
        want = str(it.get("expected_after") or "")
        if want and not job["existed"]:
            #: 计划时它还在（`expected_after` 是它的哈希），执行时没了 ——
            #: 外面有人删了它。此前这一支被跳过校验，于是"退回旧版"会把它
            #: 重新创建出来：那是用我们的留档去覆盖一个外部删除的决定。
            drifted.append(it)
            lines.append("· **没动** `%s` —— 出了计划之后它被删掉了"
                         % it["path"])
        elif want:
            got = hashlib.sha256(job["original"]).hexdigest()
            if got != want:
                drifted.append(it)
                lines.append("· **没动** `%s` —— 出了计划之后它又被改过"
                             % it["path"])
    if capture_failed:
        lines.append("★ **一个文件都没写**：有一条的现状读不出来，整批停下"
                     "（没有现状就没有还原的底）。")
        return 0, lines
    if drifted:
        lines.append("★ **一个文件都没写**：有一条在计划之后被动过（改过或被删），"
                     "整批停下（再退一次就会盖掉外面的改动，或把别人删掉的文件"
                     "重新建出来）。")
        return 0, lines

    n = 0
    done: List[dict] = []
    failed_at = ""
    for job in jobs:
        it = job["it"]
        p = Path(it["path"])
        try:
            if it["kind"] == "new":
                if p.exists():
                    p.unlink()
                    n += 1
                    lines.append("· 删掉了 `%s`（那一轮时它还不存在）" % it["path"])
                else:
                    lines.append("· `%s` 本来就不在，跳过" % it["path"])
                done.append(job)
                continue
            _atomic_write_bytes(p, job["payload"])
            n += 1
            lines.append("· 把 `%s` 退回了 v%d" % (it["path"], it["version"]))
            done.append(job)
        except OSError as exc:
            failed_at = it["path"]
            lines.append("· `%s` 写失败：%s" % (it["path"], exc))
            break

    if not failed_at:
        return n, lines

    # 中途失败 —— 把已经动过的还原，逐个报结果
    left = 0
    for job in reversed(done):
        it = job["it"]
        p = Path(it["path"])
        try:
            if job["existed"]:
                _atomic_write_bytes(p, job["original"])
            elif p.exists():
                p.unlink()
            lines.append("· 还原了 `%s`" % it["path"])
        except OSError as exc:
            left += 1
            lines.append("· ⚠️ `%s` 还原失败：%s（它现在停在退回后的样子）"
                         % (it["path"], exc))
    if left:
        lines.append("⚠️ **回退中断**：%d 个文件没能还原，其余已退回原样。"
                     "请核对上面这几条。" % left)
        # 回执是按产品约定只说一句的（`_handle_rewind` 丢掉 `lines`），
        # 所以这条必须另找一个持久出口 —— 否则"文件停在半路"只活在
        # 一个没人看的返回值里。事故账本同时喂横幅与模型。
        _note_rollback_failure(lines, left)
    else:
        lines.append("★ **一个文件都没写**：写到 `%s` 时失败，"
                     "已经动过的都已还原。" % failed_at)
    return left, lines


def _note_rollback_failure(lines: List[str], left: int) -> None:
    """补偿失败进事故账本。不许抛 —— 记账不许把回退这条路再弄挂一次。"""
    try:
        from core import incidents
        worst = [ln for ln in lines if "还原失败" in ln][:5]
        incidents.note(
            "文件回退", "rewind_rollback",
            "回退补偿失败：%d 个文件停在退回后的样子（另外 %d 条已还原）"
            % (left, max(0, len(lines) - left)),
            detail="\n".join(worst))
    except Exception:                                          # noqa: BLE001
        pass


#: 轮末算 `+N/-M` 时的体积门限 —— 任一侧超过它就不给数。
#:
#: 轮末最多 30 个文件，每个要读两份留档再跑一次 `difflib`；这里买的是
#: 「一轮的收尾不被几个大文件拖住」。拿不到数只是列表里少一个 `+N/-M`，
#: 点开仍然看得到完整 diff（那一次是用户主动点的，慢一点可以接受）。
COUNT_MAX_BYTES = 256 * 1024


def diff_for(session_id: str, path, version: int,
             *, max_bytes: int | None = None) -> dict:
    """给界面用的一次 diff 的全部信息。纯读，不改任何东西。

    返回 `{ok, kind, diff, before, after, text, note, added, removed}`：

      * `kind` —— `"diff"` 有 diff；`"new"` 这一轮新建的文件；
        `"big"` 超过封顶；`"missing"` 没有留档（老会话 / 不在本会话里）；
        `"gone"` 改前有、现在文件不在了（删除）
      * `diff` —— unified diff 文本（`kind != "diff"` 时是 `""`）
      * `note` —— 人读的那一句（每一支都要有：界面上不许出现空白面板）
      * `added` / `removed` —— 这一版净增/净减的行数。知道才给：
        拿不到的几支没有这两个键（写 `0` 会让它看起来像"没变"）。
        `new` 给 `added`（全部是新增），`gone` 给 `removed`。

    *max_bytes* 给了就加一道体积门限：任一侧超过它，直接回 `kind="big"`
    且不给行数 —— 轮末那 30 次调用付不起大文件的代价。点开看 diff
    的那条路不传它（用户主动点，慢一点可以接受）。
    """
    sid = str(session_id or "")
    # 二进制（docx / xlsx / 图片 / 压缩包 …）：按文本比较只会得到乱码。
    #   判据用 `core.workspace.parser_hint_for` —— 扩展名分类只在那里有一份。
    try:
        from core.workspace import parser_hint_for
        if parser_hint_for(Path(str(path)).suffix)[0]:
            return {"ok": False, "kind": "binary", "diff": "", "before": "",
                    "after": "", "text": "",
                    "note": "这是二进制文件，不按文本比较它的改动"}
    except Exception:                                    # noqa: BLE001
        pass
    if not sid or latest_version(sid, path) == 0:
        return {"ok": False, "kind": "missing", "diff": "", "before": "",
                "after": "", "text": "",
                "note": "这个文件没有留档（留档从本会话开始；旧会话不会补）"}

    kind_before, before = read_version(sid, path, version)
    if kind_before == "":
        return {"ok": False, "kind": "missing", "diff": "", "before": "",
                "after": "", "text": "",
                "note": "这一版的留档不在了（可能已被保留期清掉）"}
    if kind_before == "big":
        return {"ok": False, "kind": "big", "diff": "", "before": "",
                "after": "", "text": "",
                "note": "改前文件超过 2 MiB 封顶，没有留内容 —— 拿不到 diff"
                        "（这是如实说，不是'没改动'）"}

    # after：先看下一版（那是"下一次有人改它时"留的改前 = 这一次的改后），
    #   没有就读磁盘。两条都对，理由见模块 docstring 里那张式子。
    kind_after, after = read_version(sid, path, int(version) + 1)
    if kind_after == "txt":
        pass
    elif kind_after in ("new", "big"):
        # 下一版是"彼时不存在"，这一次把它删掉了；"太大"，只能说不知道。
        after = ""
    else:
        try:
            p = Path(str(path))
            after = (p.read_text(encoding="utf-8", errors="replace")
                     if p.is_file() else "")
        except OSError:
            after = ""

    if max_bytes and (len(before) > max_bytes or len(after) > max_bytes):
        return {"ok": False, "kind": "big", "diff": "", "before": "",
                "after": "", "text": "",
                "note": "文件较大（超过 %d KiB），这里不算行数" % (max_bytes // 1024)}

    if kind_before == "new":
        return {"ok": True, "kind": "new", "diff": "", "before": "",
                "after": after, "text": after,
                "added": len(after.splitlines()), "removed": 0,
                "note": "这个文件是这一轮新建的（改前不存在）"}

    src = str(path)
    diff_lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="改前 " + Path(src).name, tofile="改后 " + Path(src).name,
        lineterm="", n=3))

    added = sum(1 for ln in diff_lines
                if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines
                  if ln.startswith("-") and not ln.startswith("---"))

    if not diff_lines:
        # 内容一样也是一个结论 —— 但它与我们记的"碰过它"矛盾
        #   （可能有工具只是验证了写权限却没真改）。如实说，不假装"改了很多"。
        return {"ok": True, "kind": "diff", "diff": "", "before": before,
                "after": after, "text": after, "added": 0, "removed": 0,
                "note": "内容与改前逐字相同（写权限被验证过，但没真改动内容）"}

    if after == "" and not Path(src).is_file():
        return {"ok": True, "kind": "gone", "diff": "\n".join(diff_lines),
                "before": before, "after": "", "text": "",
                "added": 0, "removed": removed,
                "note": "这个文件现在不在了（改前的内容留在这里）"}

    return {"ok": True, "kind": "diff", "diff": "\n".join(diff_lines),
            "before": before, "after": after, "text": after,
            "added": added, "removed": removed, "note": ""}


# 保留期

def prune(days: Optional[int] = None, now: Optional[datetime] = None) -> int:
    """按 mtime 清过期留档，再按总空间上限削一次，返回删了几个文件。

    回收复用现成的 `core/retention.py::prune_generic`（判据 ④），
    不在这里另写一套"按时间删"（同一个动作写两份，迟早漂）。
    两根轴各读各的 —— 保留期读不到，时间那一半不删（老规矩），
    上限读不到，空间那一半不删。两半独立，坏一个不是"两个都不做"。
    """
    from core.retention import prune_generic

    if days is None:
        days = configured_days()
    if not ROOT.is_dir():
        return 0
    removed = 0
    # 两半独立：时间那一半与空间那一半各读各的、各做各的。
    #   不能写成 `if days is None: return 0` —— 那会让"保留期读不出来"
    #   顺手把空间上限也关掉（两件事被一个早退绑在一起）。判据就是那句
    #   「坏一个不是两个都不做」：它们读的是两个键（`SETTING_KEY` 与
    #   `TOTAL_SETTING_KEY`），一个坏了不代表另一个也该停。
    if days is not None:
        cutoff = (now or datetime.now()) - timedelta(days=days)
        for d in ROOT.iterdir():
            if d.is_dir():
                removed += prune_generic(
                    d, days=days, now=cutoff + timedelta(days=days),
                    suffixes=_SUFFIXES)
    # 再按总空间上限削一次。
    #   为什么必须在这里也做：用户把上限调小之后，若只有 `snapshot_before` 会腾，
    #   那就得等到下一次写文件才生效 —— 而「立即清理」按钮存在的理由正是
    #   "我不想等"。`incoming=0` = "不为任何新东西留位，削到上限以内"。
    removed += _make_room(0)
    # 空目录也收掉（否则会话目录会越攒越多，而每个都是空的 —— 没人看得见）
    for d in ROOT.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            try:
                d.rmdir()
            except OSError:
                pass
    if removed:
        # 两个原因分开说（别让一行话同时承担两件事）；
        # 保留期读不到时不提它，只报空间那一半。
        why = ("超过 %d 天" % days) if days is not None else None
        if why:
            print("[file_history] 清掉了 %d 份改前留档（%s / 或超过总空间上限）"
                  % (removed, why))
        else:
            print("[file_history] 清掉了 %d 份改前留档（保留期读不到，"
                  "只按总空间上限削）" % removed)
    return removed
