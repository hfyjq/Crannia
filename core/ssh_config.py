# -*- coding: utf-8 -*-
"""SSH 凭据与主机密钥的唯一读取口。

私钥路径 / 口令 / 密码都不进工具参数；回执里只出现条目名与主机。

## 两个存储

1. 凭据 —— 设置页 `skill_configs.ssh`（`config.fields` 在
   `skills/ssh/manifest.yaml`，口令与密码是 `format: password` /
   `x-secret: true`），外加 `CRANNIA_SSH_KEY` 环境变量作为私钥路径的兜底。
   形状（`entries` 命名条目 + `active`，旧的单条形状也认）：

       {"active": "pi", "entries": {"pi": {"key_path": "...", "user": "pi",
                                           "passphrase": "...", "password": "...",
                                           "sudo_password": "..."}}}

2. 主机密钥 —— `data/runtime/ssh/known_hosts`，框架自己的一份。
   决定 4（`出网权限重构-设计-2026-11-26.md §12`）：不写用户的
   `~/.ssh/known_hosts` —— 那是用户的文件，框架未经允许不该动它。
   读的时候两份都读（用户已经手动连过的主机直接能用）；写的只有自己那份。

## 优先级

设置页 > 环境变量（只对私钥路径）。读不到 = 未配置（fail-closed：
`skills/ssh` 的回执会给"去设置页填"的路线）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

__all__ = ["SCOPE", "ENV_KEY", "load", "resolve", "resolve_entry",
           "known_hosts_path", "add_known_host"]

_SETTINGS_PATH = Path("data/settings.json")
SCOPE = "ssh"
ENV_KEY = "CRANNIA_SSH_KEY"

#: 框架自己的 known_hosts（决定 4：不碰 `~/.ssh/known_hosts`）。
_KNOWN_HOSTS = Path("data/runtime/ssh/known_hosts")


def load(path: Optional[Path] = None) -> dict:
    p = path or _SETTINGS_PATH
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        print("[ssh] cannot read settings (%s) - treated as unconfigured" % type(exc).__name__)
        return {}
    cfg = (raw.get("skill_configs") or {}).get(SCOPE) or {}
    return cfg if isinstance(cfg, dict) else {}


def resolve_entry(key_name: str = "") -> dict:
    """选中的那一条凭据，原样返回（含 `name` / `source`）。没有就空 dict。

    `key_name` 命中 `entries` 里的名字；不填用 `active` 或第一条。
    旧形状（顶层的 `key_path`）当一条名叫 `default` 的条目。
    环境变量只在私钥路径上兜底 —— 密码类的东西不走环境变量
    （它们会出现在进程环境里，比设置页更不该躺在那儿）。
    """
    cfg = load()
    entries = cfg.get("entries")
    chosen: dict = {}
    source = ""
    if isinstance(entries, dict) and entries:
        name = str(key_name or cfg.get("active") or "").strip()
        if name and name in entries and isinstance(entries[name], dict):
            chosen = dict(entries[name])
            source = name
        else:
            first_name, first_val = next(iter(entries.items()))
            if isinstance(first_val, dict):
                chosen, source = dict(first_val), str(first_name)
    elif cfg.get("key_path"):
        # 旧的单条形状
        chosen, source = dict(cfg), "default"
    if not chosen:
        env = os.environ.get(ENV_KEY, "").strip()
        if env:
            chosen, source = {"key_path": env}, "env"
    if chosen:
        chosen["name"] = source or "default"
        chosen["source"] = source or "default"
    return chosen


def resolve(key_name: str = "") -> tuple[str, str, str]:
    """兼容旧调用：返回 `(私钥路径, 口令, 条目名)`；没有就空串。"""
    e = resolve_entry(key_name)
    return (str(e.get("key_path") or ""), str(e.get("passphrase") or ""),
            str(e.get("name") or ""))


def known_hosts_path() -> Path:
    """框架自己的 known_hosts 路径（唯一一处）。"""
    return _KNOWN_HOSTS


def add_known_host(line: str) -> bool:
    """把一行 known_hosts 记进框架自己的表。已经在了返回 `False`（幂等）。

    形状校验是窄的：`host keytype base64`，至少三段，且类型以已知前缀开头。
    这样调用方（审批结算）就不会因为一个坏载荷把整份文件写坏 ——
    那是"用户点了信任、下次还问"查不出因的那类故障。
    """
    text = " ".join(str(line or "").split())
    if not text or len(text) > 4096:
        return False
    parts = text.split(" ")
    if len(parts) < 3:
        return False
    if not parts[1].startswith(("ssh-", "ecdsa-", "sk-", "rsa-")):
        return False
    p = _KNOWN_HOSTS
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.exists() and text in p.read_text(encoding="utf-8").splitlines():
            return False
    except OSError:
        pass
    with p.open("a", encoding="utf-8") as f:
        f.write(text + "\n")
    return True
