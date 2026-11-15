"""模型能力表 —— 按模型查，不靠用户勾选。

## 它解决什么

多模态输入走路由：模型支持就发原生部件，不支持就明说。
而"支不支持"这个判据如果只有一个全局键
（`model.supports_vision` / `model.supports_audio`）—— 端配置页换一条配置条目时，
它与模型脱钩：`_model_cfg_of` 只传 model / api_key / base_url / window，
于是 `supports_vision` 永远是默认 True、`supports_audio` 永远是默认 False。

能力是模型元数据里的一格（`capabilities` / `inputModalities`），不是配置页的勾选框。
需要一张模型 -> 能力的表，形状与 `window_table.json` /
`cost_table.json` 同族。

## 表从哪来

`providers/model_capabilities.json` 由 `scripts/refetch_window_table.py`
从 `models.dev/api.json` 的 `modalities` 字段摘出（同一个脚本、第三份产物）。
静态数据：测试读仓库里那份，离线；要更新人来跑一次脚本。
它是"上游说"，不是"我们说"：用户显式写在 `config.yaml` 的声明仍然赢过它
（顺序承重：`声明 > 查表 > 兜底`）。

## 匹配规则

1. 同一 provider 下精确匹配 model 名；
2. 不行，大小写不敏感；
3. 再不行，归一化（小写 + 丢掉最后一个 `/` 之前的部分）后唯一才认
   —— 多个候选不猜（读不到与读到好几个都得落回兜底）。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 随代码发布的数据 —— 用模块位置解析，不用相对路径
#: （`sandbox()` 的 chdir 之后相对路径会静默读空表）。
TABLE_PATH = Path(__file__).resolve().parent.parent / "providers" / "model_capabilities.json"

_table_cache: dict | None = None
_meta_cache: dict | None = None


def _read(path: Path | None = None) -> dict:
    p = Path(path) if path is not None else TABLE_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("[model_capabilities] 能力表不存在（%s）—— 全部落回兜底" % p)
        return {}
    except Exception as exc:                                    # noqa: BLE001
        print("[model_capabilities] 能力表读不动（%s: %s）—— 全部落回兜底"
              % (type(exc).__name__, exc))
        return {}
    return raw if isinstance(raw, dict) else {}


def load_table(path=None) -> dict:
    """读表并缓存。返回整份 `{provider: {model: {"input": [...], ...}}}`。"""
    global _table_cache, _meta_cache
    raw = _read(Path(path) if path is not None else None)
    providers = raw.get("providers")
    meta = {
        "source": str(raw.get("source") or ""),
        "fetched": str(raw.get("fetched") or ""),
    }
    if path is None:
        _table_cache = providers if isinstance(providers, dict) else {}
        _meta_cache = meta
    return providers if isinstance(providers, dict) else {}


def _all() -> tuple[dict, dict]:
    global _table_cache, _meta_cache
    if _table_cache is None:
        load_table()
    return _table_cache or {}, _meta_cache or {}


def reset_for_tests() -> None:
    global _table_cache, _meta_cache
    _table_cache = None
    _meta_cache = None


def _pick(names: list[str], model: str) -> str | None:
    """在同一 provider 的行里找 *model* —— 精确 -> 小写 -> 归一化唯一。"""
    if model in names:
        return model
    low = {n.lower(): n for n in names}
    if model.lower() in low:
        return low[model.lower()]
    tail = model.rsplit("/", 1)[-1].lower()
    if not tail:
        return None
    hits = [n for n in names if n.rsplit("/", 1)[-1].lower() == tail]
    return hits[0] if len(hits) == 1 else None


def lookup(provider_dir: str, model: str) -> dict | None:
    """`(provider 目录名, 模型名)` -> 一条能力行；没有/表坏，`None`。

    `None` 是"没有这一层"，不是"什么都不能"（读不到 ≠ 0）。
      调用方据此落回默认值（vision True / audio False）。
    """
    table, _ = _all()
    prov = table.get(str(provider_dir or ""))
    if not isinstance(prov, dict):
        return None
    if not str(model or "").strip():
        return None
    key = _pick(list(prov), str(model).strip())
    if key is None:
        return None
    row = prov.get(key)
    return dict(row) if isinstance(row, dict) else None


def supports(provider_dir: str, model: str, modality: str) -> bool | None:
    """这个模型能不能收 *modality*（`image` / `audio` / `video` / `pdf` / `text`）。

    返回 `None` = 表里没有这条（调用方落回默认），不是 `False`。
    """
    row = lookup(provider_dir, model)
    if row is None:
        return None
    inp = row.get("input")
    if not isinstance(inp, list):
        return None
    return str(modality) in [str(x) for x in inp]


def source_note() -> str:
    """一句给人看的来源（端配置页/日志用）。不带 `fetched` 时说清它没有。"""
    _, meta = _all()
    src = meta.get("source") or "未知"
    got = meta.get("fetched") or "没有日期"
    return "查表(models.dev %s, %s)" % (got, src)
