"""GitHub 令牌的唯一读取口。

优先级（高到低）：

1. 设置页填的那一格：`settings.json` 的 `skill_configs.github.token`
2. 环境变量 `CRANNIA_GITHUB_TOKEN`
3. 环境变量 `GITHUB_PERSONAL_ACCESS_TOKEN`

令牌永远不是工具参数 —— 模型看不到它，也没法在对话里把它说出来。
  （同族判据：`core/mail_config.py` 是邮箱那一格的唯一读取口。）
读不到 / 文件坏了，返回空串（不抛）：调用方据此回一句"没配令牌"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

__all__ = ["SCOPE", "ENV_NAMES", "token", "configured_via",
           "load", "entries"]

_SETTINGS_PATH = Path("data/settings.json")

#: 配置在 `skill_configs` 里的那个键 —— 与技能目录同名。
SCOPE = "github"

#: 环境变量，按优先级。
ENV_NAMES = ("CRANNIA_GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")


def load(path: Optional[Path] = None) -> dict:
    """`skill_configs.github` 那一格（原样，含明文令牌 —— 只给解析用）。"""
    p = path or _SETTINGS_PATH
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        print("[github] 读不到设置里的令牌（%s）—— 视为未配置" % type(exc).__name__)
        return {}
    cfg = (raw.get("skill_configs") or {}).get(SCOPE) or {}
    return cfg if isinstance(cfg, dict) else {}


def entries(path: Optional[Path] = None) -> tuple[dict, str]:
    """`({名字: {token: …}}, 默认那个名字)`。

    两种形状都认（与 `core/mail_config` 同一条取舍）：
      · 新的多条目：`{"active": "工作号", "entries": {"工作号": {...}}}`
      · 旧的单条：`{"token": "..."}`，当成一条，名字 `default`
    `active` 指到一个不存在的名字，退而取第一个（"有配置"比"什么都没有"好）。
    """
    cfg = load(path)
    if not cfg:
        return {}, ""
    multi = cfg.get("entries")
    if isinstance(multi, dict) and multi:
        active = str(cfg.get("active") or "")
        if active not in multi:
            active = sorted(multi)[0]
        return {str(k): (v if isinstance(v, dict) else {})
                for k, v in multi.items()}, active
    if cfg.get("token"):
        return {"default": {"token": cfg.get("token")}}, "default"
    return {}, ""


def _from_settings(path: Optional[Path] = None) -> str:
    table, active = entries(path)
    if not table:
        return ""
    return str((table.get(active) or {}).get("token") or "").strip()


def _from_env() -> str:
    for name in ENV_NAMES:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return ""


def token(path: Optional[Path] = None) -> str:
    """生效的令牌；没有就空串。"""
    return _from_settings(path) or _from_env()


def configured_via(path: Optional[Path] = None) -> str:
    """这一格是从哪来的：`settings` / `env` / 空串。给回执用（不说令牌本身）。"""
    if _from_settings(path):
        return "settings"
    if _from_env():
        return "env"
    return ""
