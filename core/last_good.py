# -*- coding: utf-8 -*-
"""上次成功启动的快照 —— `#00` / `#03` 的共同前提。

## 它回答的问题

「这台机器上一次好好跑起来时，配置长什么样？」

没有它，`#03` 的「正在尝试解决」就只能干等 —— 而起不来的原因里，
配置坏了是最常见、也是唯一有确定解法的那个。

## 只快照小、且会变坏的东西

`data/` 下那几个 JSON，加起来不到 10 KB。

- 不快照凭据（`api_keys` / `*_credentials`）：多存一份就是多一个明文
  泄露面，而凭据丢了本来就该重填。
- 不快照那 507 MB 的附件与运行时缓存：它们不是起不来的原因，
  每启动一次存一份的代价远超收益。

## 判据：真的初始化完成才算「好」

`mark_good()` 只在框架真的跑起来那一刻调用（`_FRAMEWORK_RUNNING = True`），
不在"进程起来了"时 —— 后者可能带着一堆坏配置，那就把坏的那份存成了好的，
下次拿它去"恢复"等于把故障固化下来。

## 恢复不自动

`restore()` 只在有人明确要的时候调。自动回滚会把"用户刚改的配置"
悄悄换掉，而用户可能正是为了改它才启动的。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
from pathlib import Path

__all__ = ["CONFIG_FILES", "mark_good", "last_good", "restore", "snapshot_dir"]

#: 要快照的配置。凭据类刻意不在里面（理由见模块 docstring）。
CONFIG_FILES: tuple[str, ...] = (
    "data/settings.json",
    "data/mcp_servers.json",
    "data/allowlist.json",
    "data/runtime/model_configs.json",
)

_DIR = Path("data") / "last_good"


def snapshot_dir() -> Path:
    return _DIR


def _sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _snap_path(rel: str) -> Path:
    """`data/settings.json` -> `data/last_good/configs/data/settings.json`。

    保持相对结构而不是拍平：拍平之后 `data/settings.json` 与
    `data/runtime/model_configs.json` 会撞在同一个扁平名空间里。
    """
    return _DIR / "configs" / rel


def mark_good() -> dict:
    """记一次「这一次真的跑起来了」。不许抛 —— 它跑在启动路径上。

    返回写下的 meta（失败时带 `ok: False`）。调用方要不要用它都行 ——
    这条的目的是"下次能恢复"，不是"这一次要个返回值"。
    """
    now = _dt.datetime.now().isoformat(timespec="seconds")
    files: dict[str, str] = {}
    try:
        for rel in CONFIG_FILES:
            src = Path(rel)
            if not src.is_file():
                continue
            dst = _snap_path(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files[rel] = _sha(src)
    except OSError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    meta = {"ok": True, "at": now, "files": files}
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        (_DIR / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return meta


def last_good() -> dict | None:
    """上次成功的 meta；没有或读不动 -> `None`。"""
    try:
        raw = (_DIR / "meta.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("files") else None


def restore() -> dict:
    """把配置恢复到上次成功时的样子。

    当前那份改名留档（`<名>.broken-<时间戳>`），不删 —— 那是证据，
    而且"恢复错了"这件事本身要能查。

    返回 `{"ok": bool, "restored": [...], "skipped": [...], "failed": [...]}`。
    """
    meta = last_good()
    if not meta:
        return {"ok": False, "reason": "没有上次成功的快照",
                "restored": [], "skipped": [], "failed": []}

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    restored: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for rel in meta.get("files") or {}:
        src = _snap_path(rel)
        dst = Path(rel)
        if not src.is_file():
            continue
        # 已经一样：不动（"恢复"不该把文件摸一遍又放回去）
        if dst.is_file() and _sha(dst) == _sha(src):
            skipped.append(rel)
            continue
        try:
            if dst.is_file():
                dst.replace(dst.with_name(dst.name + ".broken-" + stamp))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(rel)
        except OSError:
            failed.append(rel)

    return {"ok": True, "restored": restored, "skipped": skipped,
            "failed": failed, "at": meta.get("at")}
