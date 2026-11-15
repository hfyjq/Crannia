"""用户主动停用的 MCP 服务器 —— 账本那一半。

MCP 管理里，内置服务器也支持卸载。

## 它与 `bus/quarantine` 的关系：两件事，两个账本

======================  ==============================  ============================
                        `bus/quarantine`                 本模块
======================  ==============================  ============================
谁做的决定              框架（体检说这个插件坏了）    用户（我不想让它跑）
怎么撤销                解封（`release`）                启用（`enable`）
账本                    `data/runtime/quarantine.json`   `data/runtime/mcp_disabled.json`
======================  ==============================  ============================

不许合并成一个集合：合并之后「解封」会顺手把用户停用的也放出来
（标签在说谎的形状 —— 界面说"只放了框架判的那一半"，实际两半都放了）。

但判定点只有一个：两者在 `bus/registry.py::get_active_tool_names` 里
并成一个 `blocked` —— 那一条集合差全仓只有一处（有读者钉着）。

## 停用不摘注册表（这是有意的）

`disable()` 只改集合，不调 `unregister_owner` —— 于是「启用」是真的可逆的。
判据同 `quarantine`：`release()` 也不重新注册，它靠的就是同一个筛子
（`_QUARANTINED` ∋ owner，工具进不了 `tools=`、也过不了 `is_tool_active`）。
代价：被停用的外部服务器照样会去连（连接那一步没被拦）—— 它只是不出工具。
换来的是「停用/启用」两边都当场生效、且不需要重启。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

#: 账本落点。运行数据一律进 `data/runtime/`，
#: 不许落在 `mcpx/` 或 `core/` 下。
LEDGER_PATH = Path("data/runtime/mcp_disabled.json")

_LOCK = threading.Lock()
_cache: dict[str, dict] | None = None


# ----------------------------------------------------------------------
# 账本读写
# ----------------------------------------------------------------------

def _read() -> dict[str, dict]:
    try:
        raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:                                    # noqa: BLE001
        # 账本坏了，当没有停用（同 `quarantine._read` 那条判据：
        #   宁可多放一个工具出来，不可凭一个读不动的文件把用户的东西关掉）。
        print(f"[mcp_disabled] 账本读不动（{type(exc).__name__}: {exc}）—— 按空账本走")
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


def _sync_registry(data: dict[str, dict]) -> None:
    """把账本的键集合推给注册表 —— 唯一的那一处筛子（`bus/registry.py`）。"""
    try:
        from bus.registry import set_mcp_disabled
        set_mcp_disabled(data.keys())
    except Exception as exc:                                    # noqa: BLE001
        # 推不进去不影响账本本身（下次启动还有）。但要说出来。
        print(f"[mcp_disabled] 推给注册表失败（{type(exc).__name__}: {exc}）")


def load() -> dict[str, dict]:
    """读账本 + 同步筛子。启动时调一次（`bus/core.py`，与 `quarantine.load` 并排）。"""
    global _cache
    with _LOCK:
        data = _read()
        _cache = data
    _sync_registry(data)
    return dict(data)


def all_entries() -> dict[str, dict]:
    """当前停用表（懒加载）。返回副本。"""
    global _cache
    if _cache is None:
        return load()
    return dict(_cache)


# ----------------------------------------------------------------------
# 判据
# ----------------------------------------------------------------------

def owner_for(server_name: str) -> str:
    """MCP 服务器名 → 归属键。与 `bus/quarantine.owner_for_mcp` 逐字同形。"""
    return "mcp:" + str(server_name or "").strip()


def is_disabled(owner: str) -> bool:
    """这个归属被用户停用了吗。"""
    return str(owner or "") in all_entries()


def disabled_server_names() -> set[str]:
    """被停用的服务器名（去掉 `mcp:` 前缀）—— 给界面用。"""
    return {o.split(":", 1)[1] for o in all_entries() if o.startswith("mcp:")}


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------

def disable(owner: str, *, reason: str = "") -> dict:
    """停用一个 MCP 归属。幂等（重复停用保留最早的 `since`）。

    不摘注册表 —— 理由见模块 docstring（"停用不摘注册表"那一段）。
    """
    global _cache
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("owner 不能为空（它是停用的粒度：mcp:<服务器名>）")
    now = time.time()
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        old = data.get(owner) or {}
        entry = {
            "owner": owner,
            "name": owner.split(":", 1)[-1],
            "reason": str(reason or "") or "用户在 MCP 管理里停用了它",
            "since": old.get("since", now),
            "last_seen": now,
        }
        data[owner] = entry
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            # 写不进去也照样停用（本次运行内是真的）—— 同 `quarantine` 那条。
            print(f"[mcp_disabled] 账本写不进去（{type(exc).__name__}: {exc}）"
                  f" —— 本次运行**仍然停用** '{owner}'")
        _sync_registry(data)
    return entry


def enable(owner: str) -> bool:
    """启用（= 撤销停用）。返回是不是真的撤掉了一个。"""
    global _cache
    owner = str(owner or "").strip()
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        if owner not in data:
            return False
        data.pop(owner, None)
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            print(f"[mcp_disabled] 账本写不进去（{type(exc).__name__}: {exc}）"
                  f" —— 本次运行**仍然启用** '{owner}'")
        _sync_registry(data)
    return True


def reset_for_tests() -> None:
    """清内存缓存 + 清筛子（用例之间互不污染）。不碰磁盘。"""
    global _cache
    with _LOCK:
        _cache = None
    try:
        from bus.registry import set_mcp_disabled
        set_mcp_disabled(())
    except Exception:                                           # noqa: BLE001
        pass
