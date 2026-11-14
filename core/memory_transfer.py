"""记忆包的导出 / 导入。

## 包里有什么

| 带不带 | 什么 | 为什么 |
|---|---|---|
| ✅ 默认 | `facts/<scope>/*.md`（长期事实） | 这是"记忆"的本体 |
| ✅ 默认 | `sessions/*.md`（会话笔记） | 同上 |
| ✅ 默认 | `.tombstones.json`（删除账本） | 必须：没有它，删掉的事实会在新机器复活 |
| ☑️ 勾选才带 | `short_term.db`（聊天历史） | 6 MB 量级 · 含工具结果原文 |
| ❌ 不带 | `logs/`（日报） | 新机器自己生成；带过去只是旧噪音 |
| ✅ 包里 | `manifest.json` | 版本 / 导出时间 / 条目数 / 是否含历史 |

## 三条判据

1. 用的是 zip，但绝不 `extractall`：每个条目都得过一张白名单
   （`facts/` / `sessions/` 前缀，或三个已知顶层文件），并且解析出来的目标
   必须仍在记忆根下。zip-slip 是这类功能最常见的那口井。
2. 冲突 = 目标已存在且内容不同（一样的不叫冲突，也不报）。
   三条路：取消（界面不发导入请求）· 覆盖 · 跳过冲突 = 保留本地那份。
   界面上问的那句：「存在冲突条目，继续导入会覆盖。冲突条目：…」
3. 删除账本只并、不删（同 `NEVER_RESTORE` 那条）：导入别人的包
   不许把你本机的封条解开 —— 那会让已经删掉的条目重新长出来。

## 聊天历史是并行，不是换文件

`short_term.db` 在框架跑着的时候是开着的。Windows 上拿新文件覆盖一个
打开着的文件会失败（`os.replace` 走 `MoveFileEx`，目标被读句柄打开时不成立），
所以这里改成把行并进去（`INSERT OR IGNORE`，按主键去重）——
与"跳过冲突 = 保留本地"逐字同向，而且不需要停框架。
并进去的行要重开会话/重启才在上下文里看得见（装载是启动时读的）。
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

#: 包的 schema。读到更高的 = 拒（不猜）。
SCHEMA = 1

#: 包的身份证。
KIND = "crannia-memory"

#: 顶层那几个知名的文件名（白名单的一部分）。
MANIFEST = "manifest.json"
TOMBSTONE_NAME = ".tombstones.json"
DB_NAME = "short_term.db"

#: 允许出现在包里的前缀（`facts/<scope>/<slug>.md` 与 `sessions/<sid>.md`）。
PREFIXES = ("facts/", "sessions/")

#: 解压上限（zip 炸弹）。本机：49 条事实 + 115 份笔记 + 6.25 MB 库
#: —— 上限给到 200 MB / 单文件 50 MB，够松，但拦得住"解出一个几百 G"的那种。
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ONE_BYTES = 50 * 1024 * 1024

#: 历史并行的表（显式名单，不是"库里有什么就并什么"）。
#: `sqlite_sequence` 不在里面（它是 autoincrement 的书签，并它只会打架）。
DB_TABLES = ("sessions", "session_threads", "session_bindings", "session_models",
             "session_workspaces", "messages", "stars", "pins")

_NAME_RE = re.compile(r"^name:\s*(.+)$", re.M)


def _paths(root=None) -> dict:
    r = Path(root) if root else Path("data/memory")
    return {"root": r, "facts": r / "facts", "sessions": r / "sessions",
            "tombstones": r / TOMBSTONE_NAME, "db": r / DB_NAME}


def _memory_name(text: str, fallback: str) -> str:
    """从 frontmatter 里抠 `name:` —— 冲突清单上写它比写 slug 有人味。"""
    m = _NAME_RE.search(text[:600] if isinstance(text, str) else "")
    return (m.group(1).strip().strip('"').strip("'") if m else "") or fallback


def _iter_md(base: Path):
    """`base` 下所有 `.md`（只一层：facts/<scope>/ 与 sessions/ 都是平的）。"""
    if not base.is_dir():
        return
    for p in sorted(base.rglob("*.md")):
        if p.is_file():
            yield p


# 导出

def _snapshot_db_bytes(db_path: Path) -> bytes | None:
    """一致性快照 —— 用 SQLite 自己的 `backup()`，不是拷主库文件。

    判据：库开着 WAL（`core/memory/short_term.py` 的 `PRAGMA journal_mode=WAL`），
    刚提交还没 checkpoint 的那一截在 `-wal` 里、主库文件量不到
    （`short_term.py::content_bytes` 的 docstring 自己写着这一条）。
    新建库写一行立刻导出，拷主库那份一行都没有。

    `backup()` 走读事务，拿到的是"此刻的库"，与 `-wal` 在不在无关。
    快照失败不静默：打印出声，再退回直接读主库 —— 那种包里可能少最近那条，
    而"少一条"与"没有这条功能"是两件事，得让用户看得见。
    """
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(str(db_path))
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return Path(tmp).read_bytes()
    except (sqlite3.Error, OSError) as exc:
        print("[memory-transfer] 快照失败（%s: %s）—— 退回直接读主库文件，"
              "包里的历史可能少最近那条" % (type(exc).__name__, exc))
        try:
            return db_path.read_bytes()
        except OSError:
            return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def build_zip(*, include_history: bool = False, app_version: str = "",
              root=None) -> bytes:
    """造一个记忆包（bytes，由端点直接回给浏览器）。"""
    p = _paths(root)
    buf = io.BytesIO()
    counts = {"facts": 0, "sessions": 0, "tombstones": 0}
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for base, label in ((p["facts"], "facts"), (p["sessions"], "sessions")):
            for f in _iter_md(base):
                rel = f.relative_to(p["root"]).as_posix()
                zf.writestr(rel, f.read_bytes())
                counts[label] += 1
        tob_text = ""
        try:
            if p["tombstones"].is_file():
                tob_text = p["tombstones"].read_text(encoding="utf-8")
                counts["tombstones"] = len(
                    (json.loads(tob_text) or {}).get("tombstones") or {})
        except (OSError, ValueError):
            tob_text = ""
        if tob_text:
            zf.writestr(TOMBSTONE_NAME, tob_text)
        includes_history = False
        if include_history and p["db"].is_file():
            blob = _snapshot_db_bytes(p["db"])
            if blob is not None:
                zf.writestr(DB_NAME, blob)
                includes_history = True
        manifest = {
            "kind": KIND,
            "schema": SCHEMA,
            "app_version": str(app_version or ""),
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "counts": counts,
            "includes_history": includes_history,
            #: 写出来，免得有人问"日报怎么没带"。
            "excludes": ["logs"],
        }
        zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()


# 读包（过白名单，绝不 extractall）

def _safe_entries(data: bytes) -> tuple:
    """返回 `(manifest, {相对名: bytes}, 警告列表)`。不写盘。"""
    warnings: list = []
    files: dict = {}
    total = 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        return {}, {}, ["格式错误（%s）" % type(exc).__name__]
    with zf:
        for info in zf.infolist():
            name = (info.filename or "").replace("\\", "/")
            if info.is_dir():
                continue
            # 白名单：只有三类名字能进（manifest / 两个顶层文件 / 两个前缀）。
            if name not in (MANIFEST, TOMBSTONE_NAME, DB_NAME) \
                    and not name.startswith(PREFIXES):
                warnings.append("跳过不认识的条目：%s" % name)
                continue
            # 名字本身也要过一遍（`..` / 盘符 / 开头的斜杠都不许）。
            if ".." in name.split("/") or name.startswith("/") or ":" in name:
                warnings.append("跳过可疑路径：%s" % name)
                continue
            if info.file_size > MAX_ONE_BYTES:
                warnings.append("跳过过大的条目：%s" % name)
                continue
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                warnings.append("包体过大，条目已截断")
                break
            try:
                files[name] = zf.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                warnings.append("读不出 %s（%s）" % (name, type(exc).__name__))
    manifest: dict = {}
    raw = files.pop(MANIFEST, None)
    if raw is not None:
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            manifest = {}
    return manifest, files, warnings


def _check_manifest(manifest: dict) -> str:
    if not isinstance(manifest, dict) or not manifest:
        return "包内没有 manifest ——未知包"
    if manifest.get("kind") != KIND:
        return "错误包体（kind=%r）" % (manifest.get("kind"),)
    try:
        schema = int(manifest.get("schema") or 0)
    except (TypeError, ValueError):
        return "manifest 里的 schema 读不出来"
    if schema > SCHEMA:
        return "包体版本更新，需要优先更新程序"
    return ""


def inspect_zip(data: bytes, *, root=None) -> dict:
    """看一眼这个包（一个字节都不写）：有几个条目、哪些会冲突。"""
    p = _paths(root)
    manifest, files, warnings = _safe_entries(data)
    err = _check_manifest(manifest)
    if err:
        return {"ok": False, "error": err, "conflicts": [], "counts": {},
                "warnings": warnings}

    conflicts: list = []
    new = changed = same = 0
    for name, blob in files.items():
        if name in (TOMBSTONE_NAME, DB_NAME):
            continue
        target = p["root"] / name
        if not target.is_file():
            new += 1
            continue
        try:
            old = target.read_bytes()
        except OSError:
            new += 1
            continue
        if old == blob:
            same += 1
            continue
        changed += 1
        conflicts.append({
            "path": name,
            "kind": "note" if name.startswith("sessions/") else "fact",
            "name": _memory_name(blob.decode("utf-8", "replace"),
                                 Path(name).stem),
        })
    return {
        "ok": True,
        "manifest": manifest,
        "counts": {"new": new, "conflict": changed, "identical": same},
        "conflicts": conflicts,
        "includes_history": bool(manifest.get("includes_history")),
        "has_history": DB_NAME in files,
        "warnings": warnings,
    }


# 导入

def _merge_tombstones(blob: bytes, path: Path) -> dict:
    """把包里的账本并进本机（不删本机任何一条）。

    真正的写点在 `core/memory_store.merge_tombstones`（账本只有一个写点）。
    """
    try:
        incoming = (json.loads(blob.decode("utf-8")) or {}).get("tombstones") or {}
    except (ValueError, UnicodeDecodeError):
        return {"added": 0, "error": "删除账本读不出来"}
    if not isinstance(incoming, dict):
        return {"added": 0, "error": "删除账本的形状不对"}
    from core import memory_store as MS
    res = MS.merge_tombstones(incoming, path=path)
    if not res.get("success"):
        return {"added": 0, "error": res.get("error") or "账本写不进去"}
    return {"added": res.get("added", 0), "kept": res.get("total", 0)}


def _merge_history(blob: bytes, db_path: Path) -> dict:
    """把包里的 `short_term.db` 并行进来（`INSERT OR IGNORE`，按主键去重）。

    为什么不是覆盖文件：见模块开头那段（Windows 上目标被打开时换不了）。
    为什么是 `OR IGNORE`：与「跳过冲突 = 保留本地」同一条判据 ——
      同一个 id 在本机已经存在时，本地那份说了算。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lv_mempack_") as td:
        src_path = Path(td) / DB_NAME
        src_path.write_bytes(blob)
        try:
            src = sqlite3.connect(str(src_path))
        except sqlite3.Error as exc:
            return {"inserted": 0, "error": "包里的记忆库打不开（%s）" % exc}
        try:
            dst = sqlite3.connect(str(db_path))
        except sqlite3.Error as exc:
            src.close()
            return {"inserted": 0, "error": "本机记忆库打不开（%s）" % exc}
        inserted = 0
        tables: dict = {}
        try:
            for table in DB_TABLES:
                try:
                    src_cols = [r[1] for r in src.execute(
                        "PRAGMA table_info(%s)" % table)]
                    dst_cols = [r[1] for r in dst.execute(
                        "PRAGMA table_info(%s)" % table)]
                except sqlite3.Error:
                    continue
                if not src_cols or not dst_cols:
                    continue
                cols = [c for c in src_cols if c in dst_cols]
                if not cols:
                    continue
                col_sql = ", ".join('"%s"' % c for c in cols)
                marks = ", ".join("?" * len(cols))
                rows = src.execute(
                    'SELECT %s FROM "%s"' % (col_sql, table)).fetchall()
                before = dst.total_changes
                dst.executemany(
                    'INSERT OR IGNORE INTO "%s" (%s) VALUES (%s)'
                    % (table, col_sql, marks), rows)
                got = dst.total_changes - before
                inserted += got
                tables[table] = got
            dst.commit()
        except sqlite3.Error as exc:
            return {"inserted": 0, "error": "并库失败（%s）" % exc}
        finally:
            src.close()
            dst.close()
    return {"inserted": inserted, "tables": tables}


def apply_zip(data: bytes, *, on_conflict: str = "",
              with_history: bool = False, root=None) -> dict:
    """真写。`on_conflict` 只认 `overwrite` / `skip`，没有默认值。

    没给就是拒 —— 用户明确要的是三选一（取消 / 覆盖 / 跳过冲突），
      替他选一个就是替他做决定。取消在界面那一侧（根本不发导入请求）。
    """
    if on_conflict not in ("overwrite", "skip"):
        return {"ok": False, "error": "导入前要说清：覆盖（overwrite）还是跳过冲突（skip）"}
    p = _paths(root)
    manifest, files, warnings = _safe_entries(data)
    err = _check_manifest(manifest)
    if err:
        return {"ok": False, "error": err, "warnings": warnings}

    info = inspect_zip(data, root=root)
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error"), "warnings": warnings}

    written = skipped = 0
    for name, blob in files.items():
        if name in (TOMBSTONE_NAME, DB_NAME):
            continue
        target = p["root"] / name
        rel = target.resolve()
        try:
            rel.relative_to(p["root"].resolve())
        except ValueError:
            warnings.append("跳过越界的条目：%s" % name)
            continue
        if target.is_file():
            try:
                if target.read_bytes() == blob:
                    continue                      # 一样的不写（也不叫冲突）
            except OSError:
                pass
            if on_conflict == "skip":
                skipped += 1
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        written += 1

    out = {"ok": True, "written": written, "skipped": skipped,
           "conflicts": info["conflicts"], "on_conflict": on_conflict}
    if TOMBSTONE_NAME in files:
        out["tombstones"] = _merge_tombstones(files[TOMBSTONE_NAME],
                                              p["tombstones"])
    if DB_NAME in files and with_history:
        out["history"] = _merge_history(files[DB_NAME], p["db"])
    elif DB_NAME in files:
        out["history"] = {"inserted": 0, "skipped": True}
    out["warnings"] = warnings
    return out
