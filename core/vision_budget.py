"""接管截图的图预算与可见性判定 —— 一张表 + 三级回落。

## 为什么单开一张表

`providers/model_capabilities.json` 是 refetch 脚本整份覆盖的（`fetched` / `snapshot`
都在里面），手工加字段下一次刷新就没了。而「这张图给多大」是我们自己的取舍，
不是上游数据，所以放在 `providers/vision_profiles.json`，由这里读。
新增一家供应商 = 表里加一行，不改代码。

## 为什么判不出来要往「能看图」那边倒

与 `providers/base.py::accepts_images` 同一方向：判错的代价是一次上游 400（可诊断、
可恢复、用户改一个开关就好），而默认 False 会让绝大多数用户的接管静默残废。
差别在于这里必须留下依据：`VisionProfile.confirmed` 为 False 时，调用方要把
"没确认这个模型能不能看图"说出来，而不是像现在那样把图发出去当没事。

## 回落的顺序

- 图预算按 provider 查（缩放是它做的），顺序：显式入参 → `config.yaml` 的
  `model.provider` → 表的 `_default` → 内置兜底。
- 可见性按 model 查，顺序：`config.yaml` 的 `model.supports_vision`（用户显式声明
  赢过一切）→ 能力表里该模型的 `input` → 判不出（fail-open，且 `confirmed=False`）。
- 模型名取 `request_snapshot` 里刚发出去的那条（`/model` 换过之后跟着变），
  拿不到才退到 `config.yaml`。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

#: 内置兜底 —— 表读不到、字段写坏时用。与改这张表之前的现状对齐（`MAX_EDGE_PX`）。
_FALLBACK_EDGE = 2000
_FALLBACK_PIXELS = 4_000_000

#: 合理的上下界。表是用户可编辑的文件，写个 0 或 99999 不该让截图链路失控。
_MIN_EDGE = 256
_MAX_EDGE = 4096

_LOCK = threading.Lock()
#: 路径 -> (mtime, 解析后的 dict)
_CACHE: dict[str, tuple[float, dict]] = {}


@dataclass(frozen=True)
class VisionProfile:
    """一次接管观察要用的图参数 + 它凭什么这么判。"""

    can_see: bool
    #: False 表示"没有这个模型的视觉能力记录" —— 调用方要把这句话说出去。
    confirmed: bool
    provider: str
    model: str
    max_edge: int
    max_pixels: int
    #: 判定依据（诊断/测试用，不是给模型的文案）
    source: str
    budget_note: str = ""

    def scale_for(self, width: int, height: int) -> float:
        """这张图要缩到几倍（1.0 = 不动）。等比，比例坐标不受影响。"""
        longest = max(int(width or 0), int(height or 0))
        if longest <= 0:
            return 1.0
        k = 1.0
        if longest > self.max_edge:
            k = self.max_edge / float(longest)
        pixels = int(width or 0) * int(height or 0)
        if pixels > 0 and pixels * k * k > self.max_pixels:
            k = (self.max_pixels / float(pixels)) ** 0.5
        return min(1.0, max(0.01, k))


def _read_json(name: str) -> dict:
    """读 `providers/<name>`，按 mtime 缓存；读坏了退回上一次成功的副本。

    路径只走 `paths.asset_dir` —— `providers/` 是代码资产，安装版的 cwd
    在数据根（`%APPDATA%/Crannia`），那里没有 providers 目录。写成相对路径
    在开发树全绿、装出来必崩（`tests/test_paths.py` 有一条按源码反查的断言）。
    """
    try:
        from core import paths
        path = Path(paths.asset_dir("providers")) / name
    except Exception:                                    # noqa: BLE001
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = str(path)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        # 解析失败不算"表里没有"：保住上一份，别让一次手滑把预算打回兜底
        with _LOCK:
            hit = _CACHE.get(key)
        return hit[1] if hit is not None else {}
    if not isinstance(data, dict):
        return {}
    with _LOCK:
        _CACHE[key] = (mtime, data)
    return data


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
    except Exception:                                    # noqa: BLE001
        return {}
    section = cfg.get("model") if isinstance(cfg, dict) else None
    section = section if isinstance(section, dict) else {}
    with _LOCK:
        _CACHE[key] = (mtime, section)
    return section


def _current_model(explicit: str = "") -> str:
    if explicit:
        return str(explicit)
    try:
        from core import request_snapshot
        snap = request_snapshot.get_current()
        if snap is not None and snap.model:
            return str(snap.model)
    except Exception:                                    # noqa: BLE001
        pass
    return str(_model_config().get("model_name") or "")


def _active_provider(explicit: str = "") -> str:
    if explicit:
        return str(explicit)
    return str(_model_config().get("provider") or "")


def _int_in(value, low: int, high: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if low <= n <= high else default


def _budget_for(provider: str) -> tuple[int, int, str]:
    table = _read_json("vision_profiles.json")
    row = table.get(provider) if provider else None
    if not isinstance(row, dict):
        row = table.get("_default")
    if not isinstance(row, dict):
        return _FALLBACK_EDGE, _FALLBACK_PIXELS, "内置兜底（表里没有这一家）"
    edge = _int_in(row.get("max_edge"), _MIN_EDGE, _MAX_EDGE, _FALLBACK_EDGE)
    pixels = _int_in(row.get("max_pixels"), 1, 100_000_000, _FALLBACK_PIXELS)
    return edge, pixels, str(row.get("note") or "")


def _inputs_of(provider: str, model: str):
    """能力表里这个模型的 input 列表；找不到返回 None（不等于"只能收文本"）。"""
    if not model:
        return None
    rows = _read_json("model_capabilities.json").get("providers")
    if not isinstance(rows, dict):
        return None
    order = [provider] if provider else []
    order += [name for name in rows if name not in order]
    # openrouter / siliconflow 这类聚合商会带 `vendor/model` 前缀，精确查不到就试尾巴
    names = [model]
    if "/" in model:
        names.append(model.split("/")[-1])
    for name in order:
        bucket = rows.get(name)
        if not isinstance(bucket, dict):
            continue
        for candidate in names:
            row = bucket.get(candidate)
            if isinstance(row, dict):
                got = row.get("input")
                if isinstance(got, list):
                    return [str(x).lower() for x in got]
    return None


def _visibility(provider: str, model: str) -> tuple[bool, bool, str]:
    cfg = _model_config()
    declared = cfg.get("supports_vision")
    if isinstance(declared, bool):
        return declared, True, "config.yaml 的 model.supports_vision"

    inputs = _inputs_of(provider, model)
    if inputs is not None:
        return ("image" in inputs), True, "能力表 providers/model_capabilities.json"

    # 没有记录：往"能看图"那边倒，但把不确定说出来（调用方负责出声）
    return True, False, "没有这个模型的视觉能力记录"


def resolve(provider: str = "", model: str = "") -> VisionProfile:
    """当前这一轮该给多大的图、能不能给。读表失败也返回可用值（不抛）。"""
    prov = _active_provider(provider)
    mdl = _current_model(model)
    edge, pixels, note = _budget_for(prov)
    can_see, confirmed, source = _visibility(prov, mdl)
    return VisionProfile(
        can_see=can_see,
        confirmed=confirmed,
        provider=prov,
        model=mdl,
        max_edge=edge,
        max_pixels=pixels,
        source=source,
        budget_note=note,
    )


def reset_cache() -> None:
    """只给测试用：表被改写后强制重读。"""
    with _LOCK:
        _CACHE.clear()
