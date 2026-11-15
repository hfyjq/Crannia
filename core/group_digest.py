"""群聊的「本轮摘要」账本。

它不是消息、不是主理人、不显示给用户。它只做一件事：
让下一轮的路由器与成员知道「上一轮谁做了什么」。

## 为什么单独存，不塞进 `messages`

- 塞进消息流会变成一条 `user` / `assistant` 行，污染轮次锚点
  （`collect_anchor_rows` 取的是最后一条 `user`，会把收藏的 指到摘要上）；
- 它每轮都变，放进 system prompt 会改写缓存前缀，让后续请求全部重算；
- 它只是派发用的上下文，不是对话。

落 `data/runtime/group_digests.json`，键 = 存储 id（`ctx.storage_id`）。
`/clear` 会铸新的存储 id，旧摘要天然失效，purge 清单不用加行。

## 形状

```json
{
  "<storage_id>": {
    "group_id": "g...",
    "created_at": 0,
    "items": [{"name": "...", "role": "...", "conclusion": "..."}]
  }
}
```
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

LEDGER_PATH = Path("data/runtime/group_digests.json")

#: 最多留多少条（一个存储 id 一条；超了按 created_at 丢最老的）。
MAX_ENTRIES = 100

_LOCK = threading.Lock()
_cache: dict[str, dict] | None = None


def _read() -> dict[str, dict]:
    try:
        raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:                                    # noqa: BLE001
        print(f"[group_digest] 账本读不动（{type(exc).__name__}: {exc}）—— 按空账本走")
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(data: dict[str, dict]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LEDGER_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, LEDGER_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> dict[str, dict]:
    global _cache
    with _LOCK:
        _cache = _read()
    return dict(_cache)


def _all() -> dict[str, dict]:
    global _cache
    if _cache is None:
        return load()
    return dict(_cache)


def get(storage_id: str) -> dict | None:
    """取某一段的摘要；没有就 `None`（不是空字典 —— 同 `groups.get`）。"""
    entry = _all().get(str(storage_id or "").strip())
    return dict(entry) if entry else None


def set(storage_id: str, *, group_id: str, items: list[dict]) -> None:  # noqa: A001
    """写某一段的摘要（整条替换；`items` 为空也不删 —— 那是"跑过但没人说话"）。"""
    sid = str(storage_id or "").strip()
    if not sid:
        return
    global _cache
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        data[sid] = {
            "group_id": str(group_id or ""),
            "created_at": time.time(),
            "items": [dict(it) for it in (items or []) if isinstance(it, dict)],
        }
        if len(data) > MAX_ENTRIES:
            keep = sorted(data.items(),
                          key=lambda kv: float((kv[1] or {}).get("created_at") or 0),
                          reverse=True)[:MAX_ENTRIES]
            data = dict(keep)
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            print(f"[group_digest] 账本写不进去（{type(exc).__name__}: {exc}）"
                  f" —— 本次运行**仍然生效** '{sid}'")


def clear(storage_id: str) -> bool:
    """删一条（测试 / 未来的显式"清空本轮记忆"用）。"""
    sid = str(storage_id or "").strip()
    global _cache
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        if sid not in data:
            return False
        data.pop(sid, None)
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            print(f"[group_digest] 账本写不进去（{type(exc).__name__}: {exc}）")
    return True


def reset_for_tests() -> None:
    """清内存缓存。不碰磁盘（同 `groups.reset_for_tests`）。"""
    global _cache
    with _LOCK:
        _cache = {}
