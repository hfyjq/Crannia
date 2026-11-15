#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一个插件的 `.mcp.json` 导进 `data/mcp_servers.json`。

    .venv/Scripts/python.exe scripts/import_mcp.py <插件目录或 .mcp.json> [--dry-run]

## 它做什么

1. 两种形状都认（平铺 `{"名字": {...}}` / 包装 `{"mcpServers": {...}}`）；
2. 把 `${CLAUDE_PLUGIN_ROOT}` 换成这个插件目录的绝对路径
   （插件里的本地 server 靠它找自己的文件）；
3. 列出配置里用到的 `${VAR}` —— 不展开、不落盘，只告诉用户要设哪些；
4. 合并进 `data/mcp_servers.json`（已有同名就跳过并出声，不覆盖用户改过的）。

## 为什么不自动扫

配置来源一多，用户就不知道"我到底装了什么"。导入是一次显式动作，
跑完看得见改了什么（`--dry-run` 只看不写）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CFG = Path("data/mcp_servers.json")
PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def find_mcp_json(target: Path) -> Path | None:
    """目录，找它下面的 `.mcp.json`；文件，就用它。"""
    if target.is_file() and target.name == ".mcp.json":
        return target
    if target.is_dir():
        p = target / ".mcp.json"
        if p.is_file():
            return p
    return None


def read_servers(path: Path) -> dict:
    """读成 `{名字: 配置}` —— 平铺与包装两种形状都收。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("配置不是对象")
    wrapped = data.get("mcpServers")
    if isinstance(wrapped, dict) and wrapped:
        return dict(wrapped)
    flat = {k: v for k, v in data.items()
            if isinstance(v, dict) and ("command" in v or "url" in v)}
    if flat:
        return flat
    raise ValueError("既没有 mcpServers 也不像平铺的服务器表")


def rewrite(value, plugin_dir: Path):
    """把 `${CLAUDE_PLUGIN_ROOT}` 换成真路径（递归进 list / dict）。"""
    if isinstance(value, str):
        return value.replace(PLUGIN_ROOT, str(plugin_dir))
    if isinstance(value, list):
        return [rewrite(v, plugin_dir) for v in value]
    if isinstance(value, dict):
        return {k: rewrite(v, plugin_dir) for k, v in value.items()}
    return value


def env_names(servers: dict) -> list[str]:
    """配置里出现的 `${VAR}` 变量名（除了 PLUGIN_ROOT）。"""
    out: set[str] = set()

    def walk(v):
        if isinstance(v, str):
            for m in _ENV.finditer(v):
                if m.group(0) != PLUGIN_ROOT:
                    out.add(m.group(1))
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(servers)
    return sorted(out)


def load_existing() -> dict:
    if not CFG.exists():
        return {}
    try:
        data = json.loads(CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("[import] WARNING: 现有 %s 读不出来 —— 本次不覆盖它" % CFG)
        raise SystemExit(2)
    wrapped = data.get("mcpServers")
    return dict(wrapped) if isinstance(wrapped, dict) else dict(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把一个插件的 .mcp.json 导入 MCP 配置")
    ap.add_argument("target", help="插件目录，或直接给 .mcp.json 的路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写")
    args = ap.parse_args(argv)

    target = Path(args.target).expanduser()
    cfg_path = find_mcp_json(target)
    if cfg_path is None:
        print("[import] 没找到 .mcp.json：%s" % target)
        return 2
    plugin_dir = cfg_path.parent

    try:
        servers = read_servers(cfg_path)
    except (OSError, ValueError) as exc:
        print("[import] 读不动：%s" % exc)
        return 2
    if not servers:
        print("[import] 这份配置里没有服务器")
        return 1

    servers = {k: rewrite(v, plugin_dir) for k, v in servers.items()}
    vars_ = env_names(servers)

    existing = load_existing()
    added, skipped = {}, []
    for name, cfg in servers.items():
        if name in existing:
            skipped.append(name)
            continue
        added[name] = cfg

    print("[import] 来源：%s" % cfg_path)
    print("[import] 读到 %d 个：%s" % (len(servers), ", ".join(sorted(servers))))
    if skipped:
        print("[import] 跳过 %d 个（已有同名）：%s"
              % (len(skipped), ", ".join(sorted(skipped))))
    if vars_:
        print("[import] 需要环境变量（先设好再用；本工具**不写**它们）：%s"
              % ", ".join(vars_))
    if not added:
        print("[import] 没有新条目 —— 什么都没写")
        return 0

    merged = dict(existing)
    merged.update(added)
    payload = {"mcpServers": merged}

    if args.dry_run:
        print("[import] --dry-run：将写入 %d 条" % len(added))
        print(json.dumps({"mcpServers": added}, ensure_ascii=False, indent=2))
        return 0

    CFG.parent.mkdir(parents=True, exist_ok=True)
    CFG.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("[import] 写入 %s：新增 %d 条，共 %d 条"
          % (CFG, len(added), len(merged)))
    print("[import] 重启框架后生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
