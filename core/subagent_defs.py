# -*- coding: utf-8 -*-
"""子 agent 定义：agents/<name>.md 的发现与解析。

一个定义文件由 YAML frontmatter 与正文组成：

    ---
    name: reviewer
    description: 审查改动并列出问题
    tools: [Read, Grep]
    maxTurns: 4
    ---
    你是一名代码审查员。只报告可复现的问题，不修改文件。

正文作为角色说明，加在派发指令前面。三个可选字段的含义：

- `tools`：工具白名单。按执行侧生效，不改变发给模型的工具表，因此不影响
  父子同前缀的缓存。省略表示不限制。
- `maxTurns`：轮次上限。只能低于当前档位给出的预算，高于档位的值会被压低。
- `description`：写入 `run_subagent` 的工具描述，供模型选择。

扫描根按顺序为 `agents/`、`.agents/agents/`、`~/.agents/agents/`、
`~/.claude/agents/`（外加插件包里的 `agents/`）。同名定义以先出现的为准。

支持的字段与扫描根以 README 的「互操作」一节为准 —— 那里只留一份。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.tool_names import normalise_tools as _normalise_tools

__all__ = ["SubagentDef", "agent_roots", "discover_agents", "find_agent",
           "tool_schema"]

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: 清单在工具描述里的字符上限。
_LISTING_MAX_CHARS = 400


@dataclass(frozen=True)
class SubagentDef:
    """一个子 agent 定义。"""

    name: str
    description: str
    body: str
    tools: Optional[frozenset]
    max_turns: Optional[int]
    path: Path

    def directive(self, task: str) -> str:
        """拼出派发指令：角色说明 + 四段式任务描述。"""
        from core.subagent import build_directive

        body = self.body.strip()
        head = "【角色】你是 %s。\n" % self.name
        if body:
            head += body + "\n\n"
        else:
            head += "\n"
        return head + build_directive(task)


def agent_roots() -> list[Path]:
    """扫描根的列表，按优先级排列，去重。

    路径在调用时计算，不在导入时固定。
    """
    roots = [
        Path("agents"),
        Path(".agents") / "agents",
        Path.home() / ".agents" / "agents",
        Path.home() / ".claude" / "agents",
    ]
    # 插件包里的 agents/ 目录。
    try:
        from core import plugin_packages
        roots.extend(plugin_packages.agents_dirs())
    except Exception as exc:                                  # noqa: BLE001
        print("[subagent] 读取插件包失败：%s: %s" % (type(exc).__name__, exc))
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        try:
            key = str(r.resolve())
        except OSError:
            key = str(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _parse(path: Path) -> Optional[SubagentDef]:
    """解析一个定义文件。没有 frontmatter 或 YAML 损坏时返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        import yaml
        meta = yaml.safe_load(m.group(1))
    except Exception:                                     # noqa: BLE001
        print("[subagent] 跳过 %s：frontmatter 无法解析" % path)
        return None
    if not isinstance(meta, dict):
        return None

    name = str(meta.get("name") or path.stem).strip()
    if not _NAME_RE.match(name):
        print("[subagent] 跳过 %s：名字不合法（%r）" % (path, name))
        return None
    description = str(meta.get("description") or "").strip()

    turns = meta.get("maxTurns")
    if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
        turns = None

    return SubagentDef(
        name=name,
        description=description,
        body=m.group(2).strip(),
        tools=_normalise_tools(meta.get("tools")),
        max_turns=turns,
        path=path,
    )


def discover_agents(roots=None) -> list[SubagentDef]:
    """扫描全部根，返回按名字排序的定义列表。同名以先出现的为准。"""
    out: list[SubagentDef] = []
    seen: set[str] = set()
    for root in (roots if roots is not None else agent_roots()):
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            files = sorted(base.glob("*.md"), key=lambda p: p.name)
        except OSError:
            continue
        for f in files:
            d = _parse(f)
            if d is None or d.name in seen:
                continue
            seen.add(d.name)
            out.append(d)
    return sorted(out, key=lambda d: d.name)


def find_agent(name: str, roots=None) -> Optional[SubagentDef]:
    """按名字查找定义，找不到返回 None。"""
    key = (name or "").strip()
    if not key:
        return None
    for d in discover_agents(roots):
        if d.name == key:
            return d
    return None


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def tool_schema(base: dict, roots=None) -> dict:
    """在 run_subagent 的 schema 上补充 agent 参数与可用定义清单。

    没有定义文件时原样返回，不增加任何字符。
    """
    schema = copy.deepcopy(base)
    defs = discover_agents(roots)
    if not defs:
        return schema

    props = schema.setdefault("inputSchema", {}).setdefault("properties", {})
    props["agent"] = {
        "type": "string",
        "description": "子 agent 定义名。不填时使用通用子 agent。",
    }
    listing = "；".join(
        "%s：%s" % (d.name, _clip(d.description, 50)) if d.description else d.name
        for d in defs)
    if len(listing) > _LISTING_MAX_CHARS:
        listing = listing[:_LISTING_MAX_CHARS - 1] + "…"
    schema["description"] = (
        str(base.get("description") or "").rstrip()
        + "\n可用的子 agent 定义：" + listing + "。")
    return schema
