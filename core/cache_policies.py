"""缓存与回收策略的解析 —— 一张表，一条回落链。

## 为什么单开一张表

`providers/model_capabilities.json` / `window_table.json` / `cost_table.json` 由
`scripts/refetch_window_table.py` 整份覆盖，手工字段会被冲掉。而"这一家怎么缓存、
能不能客户端裁图"是我们的取舍，不是上游数据，所以放在 `providers/cache_policies.json`。
新增一家 = 加一行 JSON 加一条表驱动测试，不改代码。

## 回落顺序

模型级覆盖（表内 `min_prefix_by_model`）→ provider → `_default` → 内置兜底。
`config.yaml` 的显式声明赢过整张表，与 `model_capabilities` 那条判据同形。
读表失败时退回上一次成功的副本，再失败才用内置兜底；不抛异常 ——
一个策略问题不该把一轮对话弄崩。

## 为什么不把默认写死在代码里

代码里散落的 provider 分支会随着供应商增减而腐坏，而"这家怎么缓存"是
会变的外部事实。表里只写偏离默认的那几家，其余自动落 `_default`。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

#: 内置兜底，对齐表里的 `_default`。表整份读不到时用，不让热路径炸。
_FALLBACK = {
    "strategy": "unknown",
    "cache_mode": "unknown",
    "cache_format": "none",
    "explicit_enabled": False,
    "eviction": "batch",
    "batch_every": 25,
    "keep_images": 3,
    "max_inline_images": 12,
    "min_prefix_tokens": 0,
    "max_breakpoints": 0,
    "ttl_seconds": 0,
    "lookback_blocks": 0,
    "tools_must_be_stable": False,
}

#: 字段的合法范围。表是用户可以手改的文件，写个 -1 或 99999 不该让请求链路失控。
_RANGES = {
    "batch_every": (1, 10000),
    "keep_images": (0, 100),
    "max_inline_images": (1, 1000),
    "min_prefix_tokens": (0, 10_000_000),
    "max_breakpoints": (0, 4),
    "ttl_seconds": (0, 86400),
    "lookback_blocks": (0, 1000),
}

_STRATEGIES = frozenset({"implicit", "explicit_blocks", "routing_key",
                         "server_clear", "unknown"})
_CACHE_MODES = frozenset({"implicit", "explicit", "mutually_exclusive", "none", "unknown"})
_FORMATS = frozenset({"anthropic", "alibaba", "openai_native",
                      "routing_only", "cached_content", "none"})
_EVICTIONS = frozenset({"batch", "server", "none"})

_LOCK = threading.RLock()
#: 路径 -> (mtime, 解析后的 dict)。模块级可变，登记在 core/state_registry.py。
_CACHE: dict[str, tuple[float, dict]] = {}


@dataclass(frozen=True)
class PolicyProfile:
    """一次请求该按什么策略走，以及它凭什么这么判。"""

    strategy: str
    cache_mode: str
    cache_format: str
    explicit_enabled: bool
    eviction: str
    batch_every: int
    keep_images: int
    max_inline_images: int
    min_prefix_tokens: int
    max_breakpoints: int
    ttl_seconds: int
    lookback_blocks: int
    tools_must_be_stable: bool
    provider: str
    model: str
    source: str
    note: str = ""

    def min_prefix_for(self, model: str = "") -> int:
        """模型级最小前缀覆盖，没有就用 provider 那一行的值。

        匹配顺序按"最长优先"的成熟做法：先精确（忽略大小写），再按 id 长度
        降序做子串匹配 —— 长的快照名要先命中，不然 `claude-sonnet-4` 会把
        `claude-sonnet-4-6` 抢走。表里没有这一格时返回 provider 级的值。
        """
        row = _read_table().get(self.provider)
        table = row.get("min_prefix_by_model") if isinstance(row, dict) else None
        if not isinstance(table, dict):
            return self.min_prefix_tokens
        target = str(model or self.model or "")
        if not target:
            return self.min_prefix_tokens
        keys = sorted((str(k) for k in table), key=len, reverse=True)
        low = target.lower()
        exact = next((k for k in keys if k.lower() == low), "")
        hit = exact or next((k for k in keys if k.lower() in low), "")
        if not hit:
            return self.min_prefix_tokens
        return _bounded("min_prefix_tokens", table.get(hit), self.min_prefix_tokens)


def _table_path() -> Path:
    """代码资产按安装根找，不按 cwd —— 安装版的 cwd 在数据根。"""
    from core import paths
    return Path(paths.asset_dir("providers")) / "cache_policies.json"


def _read_table() -> dict:
    try:
        mtime = _table_path().stat().st_mtime
    except Exception:                                # noqa: BLE001
        return {}
    with _LOCK:
        hit = _CACHE.get(str(_table_path()))
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        data = json.loads(_table_path().read_text(encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        # 读坏了保住上一份，别让一次手滑把策略打回兜底
        with _LOCK:
            hit = _CACHE.get(str(_table_path()))
        return hit[1] if hit is not None else {}
    if not isinstance(data, dict):
        return {}
    with _LOCK:
        _CACHE[str(_table_path())] = (mtime, data)
    return data


def _bounded(key: str, value, default):
    if key not in _RANGES:
        return default
    low, high = _RANGES[key]
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if low <= n <= high else default


def _enum(key: str, value, allowed: frozenset[str], default: str) -> str:
    got = str(value or "").strip().lower()
    return got if got in allowed else default


def _model_config() -> dict:
    """`config.yaml` 的 `model` 段（按 mtime 缓存）。读不到就是空。"""
    path = Path("config.yaml")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = str(path.resolve())
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        from bus.core import load_config
        cfg = load_config(path)
    except Exception:                                # noqa: BLE001
        return {}
    section = cfg.get("model") if isinstance(cfg, dict) else None
    section = section if isinstance(section, dict) else {}
    with _LOCK:
        _CACHE[key] = (mtime, section)
    return section


def resolve(provider: str = "", model: str = "") -> PolicyProfile:
    """当前这一轮该按哪套缓存策略走。读表失败也返回可用值。"""
    table = _read_table()
    cfg = _model_config()
    prov = str(provider or cfg.get("provider") or "")
    mdl = str(model or cfg.get("model_name") or "")

    row = table.get(prov) if prov else None
    if not isinstance(row, dict):
        row = table.get("_default")
    if not isinstance(row, dict):
        row = _FALLBACK

    view = dict(_FALLBACK)
    view.update(row)

    # config 显式声明赢过表。只认写清楚的值，空串/不认的枚举当没写。
    # 字段名不是统一加前缀：strategy 那格在 config 里叫 cache_strategy，
    # 其余三格本来就带 cache_ 前缀。
    cfg_keys = {"strategy": "cache_strategy", "cache_mode": "cache_mode",
                "cache_format": "cache_format", "eviction": "eviction"}
    for key, candidates in (("strategy", _STRATEGIES),
                            ("cache_mode", _CACHE_MODES),
                            ("cache_format", _FORMATS),
                            ("eviction", _EVICTIONS)):
        explicit = cfg.get(cfg_keys[key])
        if explicit:
            view[key] = _enum(key, explicit, candidates, str(view.get(key) or ""))
    for key in ("min_prefix_tokens", "max_breakpoints", "ttl_seconds",
                "lookback_blocks", "batch_every", "keep_images"):
        # 表里的值本身可能越界，先把表值夹到合法范围；config 又越界时
        # 退回那个已夹过的表值，而不是退回 0（0 会让断点上限变成"不许打点"）。
        table_value = _bounded(key, view.get(key), _FALLBACK[key])
        if key in cfg:
            view[key] = _bounded(key, cfg.get(key), table_value)
        else:
            view[key] = table_value
    if "explicit_enabled" in cfg and isinstance(cfg["explicit_enabled"], bool):
        view["explicit_enabled"] = cfg["explicit_enabled"]

    return PolicyProfile(
        strategy=_enum("strategy", view.get("strategy"), _STRATEGIES, "unknown"),
        cache_mode=_enum("cache_mode", view.get("cache_mode"), _CACHE_MODES, "unknown"),
        cache_format=_enum("cache_format", view.get("cache_format"), _FORMATS, "none"),
        explicit_enabled=bool(view.get("explicit_enabled", False)),
        eviction=_enum("eviction", view.get("eviction"), _EVICTIONS, "none"),
        batch_every=_bounded("batch_every", view.get("batch_every"),
                             _FALLBACK["batch_every"]),
        keep_images=_bounded("keep_images", view.get("keep_images"),
                             _FALLBACK["keep_images"]),
        max_inline_images=_bounded("max_inline_images", view.get("max_inline_images"),
                                   _FALLBACK["max_inline_images"]),
        min_prefix_tokens=_bounded("min_prefix_tokens", view.get("min_prefix_tokens"),
                                   _FALLBACK["min_prefix_tokens"]),
        max_breakpoints=_bounded("max_breakpoints", view.get("max_breakpoints"),
                                 _FALLBACK["max_breakpoints"]),
        ttl_seconds=_bounded("ttl_seconds", view.get("ttl_seconds"),
                             _FALLBACK["ttl_seconds"]),
        lookback_blocks=_bounded("lookback_blocks", view.get("lookback_blocks"),
                                 _FALLBACK["lookback_blocks"]),
        tools_must_be_stable=bool(view.get("tools_must_be_stable", False)),
        provider=prov,
        model=mdl,
        source="providers/cache_policies.json",
        note=str(row.get("note") or ""),
    )


def reset_cache() -> None:
    """只给测试用：表被改写后强制重读。"""
    with _LOCK:
        _CACHE.clear()
