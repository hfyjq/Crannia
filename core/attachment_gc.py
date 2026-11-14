"""回收没有任何消息引用的旧图片附件（`data/attachments/`）。

## 为什么需要它

截图 / 粘贴图 / 工具回执里的图都无条件落盘（内容寻址，只去重字节完全相同的），
而历史上没有任何清理器 —— 接管每观测一次就一张全窗 PNG（1 MB 级别）。
一次测量：204 MB / 680 个文件，最老的 20 天前。

## 判据（删东西的那条线画在哪）

1. 只删"没有任何文本引用它"的。引用面 = 三处：
   `messages.content`（库里的指针）· 会话笔记 `data/memory/sessions/*.md` ·
   日报 `data/memory/logs/*.md`。只删一处都没提到的。
2. 只删够老的（默认读 `cleanupPeriodDays`，30 天）。刚拍的照片可能
   还在被"这一轮"引用（消息还没落库）。
3. 读不到保留期 / 库打不开，一个都不删（与 `core/retention.py` 同一条方向：
   一个「删」的动作，读不到值时的真值是"不动"，不是"默认"）。
4. 文件名里的 16 位十六进制就是身份（`image_input._spill` 的 sha256 前缀）；
   不是这个形状的文件不碰。

## 它不做什么

不压缩、不改规格、不动引用它的图（高清截图仍有必要）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional, Set

from core.image_input import ATTACHMENT_DIR
from core.retention import configured_cleanup_days

__all__ = ["ID_RE", "RefsUnreadable", "referenced_ids", "prune",
           "maybe_prune_daily"]

#: 附件指针的唯一形状：`data/attachments/<16 hex>.<ext>`。
#: 与 `core/image_input.py` 的 `_PATH_RE` 是同一个形状，但这里要的是
#:    "从一段自由文本里找出来"（那边是"整段必须恰好是它"），所以单独一条。
#: 扩展名表不在这里：从 `core.artifacts.ID_EXTENSIONS` 拼 ——
#:    之前这里是手写的 `png|jpg|webp|gif`，而判据 4 是"不是这个形状就不碰"
#:音视频附件永不回收（回收器名不副实，且没有任何东西会红）。
def _ext_alt() -> str:
    from core.artifacts import ID_EXTENSIONS
    return "|".join(ID_EXTENSIONS)


ID_RE = re.compile(r"data/attachments/([0-9a-f]{16})\.(?:" + _ext_alt() + r")")
_ID_NAME_RE = re.compile(r"^([0-9a-f]{16})\.(?:" + _ext_alt() + r")$")

_DB = Path("data/memory/short_term.db")
_NOTES = Path("data/memory/sessions")
_LOGS = Path("data/memory/logs")

#: 一天最多跑一次（启动钩子的节流戳）。
_STAMP = Path("data/runtime/attachment_gc.json")
_MIN_INTERVAL_S = 24 * 3600


def _ids_in_text(text: str) -> Set[str]:
    return set(ID_RE.findall(text or ""))


def _texts_from_db(db_path: Path) -> Iterable[str]:
    """逐行吐 `messages.content`（只读打开：绝不因为一次清理改动库）。"""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True,
                           timeout=5)
    try:
        cur = conn.execute(
            "SELECT content FROM messages "
            "WHERE content LIKE '%data/attachments/%'")
        for (content,) in cur:
            if isinstance(content, str):
                yield content
    finally:
        conn.close()


class RefsUnreadable(RuntimeError):
    """引用面有一处读不全 —— 删除动作必须整体停下。

    读不到的那一份可能正是某个附件的唯一引用；跳过它再删，等于
    把"我没看见"当成"没人引用"。
    """


def _texts_from_dir(d: Path, *, strict: bool = False) -> Iterable[str]:
    """逐个吐目录下的 Markdown 文本。

    `strict=True` 时任何一份读不出来就抛 `RefsUnreadable` —— 给"删东西"
    那条路用。目录整个不在不算读不全（没有笔记 ≠ 读不到笔记）。

    列目录不用 `Path.glob`：它会吞掉扫描期的 `OSError`（`glob` 模块
    自己的 `scandir` 那段是 `except OSError: pass`），于是"目录打得开但
    读不了"会得到一个空结果 —— 看起来像"这里没有笔记"，而删除路径
    会据此把有引用的附件当孤儿。`os.scandir` 会把那个异常抛出来。
    """
    if not d.exists():
        return
    try:
        with os.scandir(d) as it:
            entries = sorted(Path(e.path) for e in it
                             if e.name.endswith(".md") and e.is_file())
    except OSError as exc:
        if strict:
            raise RefsUnreadable("%s 列不出来：%s" % (d, exc)) from exc
        return
    for p in entries:
        try:
            yield p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            if strict:
                raise RefsUnreadable("%s 读不了：%s" % (p, exc)) from exc
            continue


def referenced_ids(db_path: Optional[Path] = None,
                   notes_dir: Optional[Path] = None,
                   logs_dir: Optional[Path] = None,
                   *, strict: bool = False) -> Set[str]:
    """三处文本里出现过的附件 id。

    任何一处读不到都只是"那一处没有贡献"，但调用方要分得开
      「库打不开」与「库是空的」—— 见 :func:`prune` 的 `skipped`。

    `strict=True`：笔记/日报里有任何一份读不出来就抛 `RefsUnreadable`。
      默认 False 是给"看还剩多少引用"这类只读用途的 —— 少算一点无妨；
      删除那条路必须用 True。
    """
    db_path = Path(db_path) if db_path else _DB
    notes_dir = Path(notes_dir) if notes_dir else _NOTES
    logs_dir = Path(logs_dir) if logs_dir else _LOGS
    out: Set[str] = set()
    for text in _texts_from_db(db_path):
        out |= _ids_in_text(text)
    for text in _texts_from_dir(notes_dir, strict=strict):
        out |= _ids_in_text(text)
    for text in _texts_from_dir(logs_dir, strict=strict):
        out |= _ids_in_text(text)
    return out


def prune(days: Optional[int] = None, *, now: Optional[float] = None,
          attachment_dir: Optional[Path] = None,
          db_path: Optional[Path] = None,
          notes_dir: Optional[Path] = None,
          logs_dir: Optional[Path] = None) -> dict:
    """删掉「没被引用 且 够老」的附件。返回读数 dict。

    读数：`deleted`（个）· `freed`（字节）· `kept_referenced` · `kept_recent` ·
    `skipped`（原因：`no_retention` / `db_unreadable` / `refs_unreadable` /
    `no_dir`）。
    """
    d = Path(attachment_dir) if attachment_dir else ATTACHMENT_DIR
    stats = {"deleted": 0, "freed": 0, "kept_referenced": 0, "kept_recent": 0,
             "skipped": ""}
    if not d.exists():
        stats["skipped"] = "no_dir"
        return stats

    if days is None:
        days = configured_cleanup_days()
    if days is None:
        #: 读不到保留期，不删（与 `retention.py` 同一条方向）。
        stats["skipped"] = "no_retention"
        return stats

    db = Path(db_path) if db_path else _DB
    try:
        # strict：引用面读不全就整个停下 —— 读不到的那一份可能正是
        # 某个附件的唯一引用（跳过它再删 = 把"我没看见"当成"没人引用"）。
        refs = referenced_ids(db, notes_dir, logs_dir, strict=True)
    except RefsUnreadable as exc:
        stats["skipped"] = "refs_unreadable"
        stats["error"] = str(exc)
        return stats
    except Exception as exc:                                   # noqa: BLE001
        #: 库打不开，引用面是未知，此时删除等于赌"没人引用"。
        stats["skipped"] = "db_unreadable"
        stats["error"] = "%s: %s" % (type(exc).__name__, exc)
        return stats

    cutoff = (now if now is not None else time.time()) - days * 86400
    for p in d.iterdir():
        if not p.is_file():
            continue
        m = _ID_NAME_RE.fullmatch(p.name)
        if m is None:
            continue                       # 不是我们造的，不碰
        if m.group(1) in refs:
            stats["kept_referenced"] += 1
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            stats["kept_recent"] += 1      # 刚落的，这一轮可能还在用
            continue
        try:
            p.unlink()
        except OSError:
            continue
        stats["deleted"] += 1
        stats["freed"] += st.st_size
    return stats


def maybe_prune_daily(now: Optional[float] = None,
                      stamp_path: Optional[Path] = None) -> Optional[dict]:
    """启动钩子用：一天最多一次。没到点返回 `None`（不出声）。

    只记"跑过没有"，不记读数 —— 读数留给调用方打日志。
    """
    stamp = Path(stamp_path) if stamp_path else _STAMP
    ts = now if now is not None else time.time()
    last = 0.0
    try:
        last = float(json.loads(stamp.read_text(encoding="utf-8")).get("at") or 0)
    except Exception:                                          # noqa: BLE001
        last = 0.0
    if ts - last < _MIN_INTERVAL_S:
        return None
    stats = prune(now=ts)
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps({"at": ts}), encoding="utf-8")
    except OSError:
        pass                               # 戳写不进去，下次再跑一遍（无害）
    return stats
