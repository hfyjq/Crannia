"""技能的启用状态，读写 `data/settings.json` 的 `skill_configs.<name>.state`。

状态值与技能指令面共用：

| 值 | 工具型技能 | 指令型技能 |
|---|---|---|
| `on`（默认） | 工具可用 | 出现在模型索引与 `/` 命令里 |
| `name-only` | 工具可用 | 索引里只列名字 |
| `user-invocable-only` | 工具可用 | 不进模型索引，`/` 命令仍可用 |
| `off` | 工具不可用、不可执行 | 两者都不出现 |

`off` 是"停用"。工具型的停用由 `bus/registry.py` 的集合差生效，
呈现与执行共用同一个判定（`get_active_tool_names`），因此不需要在别处再判一次。

状态没有独立账本：设置文件是唯一来源，读写都在这个模块。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict

__all__ = ["STATES", "states", "state_of", "set_state", "refresh", "is_off"]

SETTINGS_PATH = Path("data") / "settings.json"

STATES = ("on", "name-only", "user-invocable-only", "off")

_lock = threading.Lock()


def _read() -> dict:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:                                    # noqa: BLE001
        print("[skill_state] 设置读不出来（%s: %s），按全部启用处理"
              % (type(exc).__name__, exc))
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, SETTINGS_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def states() -> Dict[str, str]:
    """全部技能的状态。未设置或值不认识时按 `on`。"""
    raw = (_read().get("skill_configs") or {})
    out: Dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        if isinstance(entry, dict):
            st = entry.get("state")
            out[str(name)] = st if isinstance(st, str) and st in STATES else "on"
    return out


def state_of(name: str) -> str:
    return states().get(str(name), "on")


def is_off(name: str) -> bool:
    return state_of(name) == "off"


def set_state(name: str, state: str) -> str:
    """写入一个技能的状态并立即生效。返回写入后的状态。"""
    key = str(name or "").strip()
    if not key:
        raise ValueError("技能名不能为空")
    value = str(state or "").strip()
    if value not in STATES:
        raise ValueError("状态必须是 %s 之一（收到 %r）" % (" / ".join(STATES), state))

    with _lock:
        data = _read()
        configs = data.get("skill_configs")
        if not isinstance(configs, dict):
            configs = {}
            data["skill_configs"] = configs
        entry = configs.get(key)
        if not isinstance(entry, dict):
            entry = {}
            configs[key] = entry
        entry["state"] = value
        _write(data)
    refresh()
    return value


def refresh() -> set:
    """把当前停用的技能同步到注册表，返回被停用的归属集合。

    框架启动时调用一次，状态变更时再调用。没有停用的技能时是空集合。
    """
    from bus.registry import set_skill_disabled

    owners = {"skill:" + name for name, st in states().items() if st == "off"}
    result = set_skill_disabled(owners)
    # 指令型技能的 / 命令表按根清单缓存，状态不在缓存键里；改状态后要清一次。
    try:
        from bus.loaders.skill_md import invalidate_command_map
        invalidate_command_map()
    except Exception:                                           # noqa: BLE001
        pass
    return result
