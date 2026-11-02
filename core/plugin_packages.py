"""插件包发现：一个目录里可以同时带多种组件。

按目录约定识别，不执行任何代码：

| 组件 | 路径 | 用途 |
|---|---|---|
| 元数据 | `plugin.json` 或 `.claude-plugin/plugin.json` | 名字与描述 |
| MCP | `.mcp.json` | 服务器配置 |
| 技能 | `skills/` | 含 `SKILL.md` 的目录 |
| 命令 | `commands/*.md` | 认得出，但没有装载器 —— 见 README 的「互操作」 |
| 子 agent | `agents/*.md` | 子 agent 定义 |
| 钩子 | `hooks/hooks.json` | 事件钩子 |

扫描根为 `plugins/`、`.agents/plugins/`、`~/.agents/plugins/`，
以及两个插件缓存目录的三层布局 `<市场>/<插件>/<版本>/`。

本模块只回答"有哪些包、每个包有哪些组件"。是否加载某个组件由各自的装载器决定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

__all__ = ["PluginPackage", "package_roots", "discover_packages", "agents_dirs",
           "workspace_plugins_allowed"]

#: 单个包内可能出现的组件标记。
_MARKERS = ("plugin.json", ".claude-plugin/plugin.json", ".mcp.json",
            "skills", "commands", "agents", "hooks")

#: 最多返回多少个包。缓存目录可能很大，超过上限按路径排序截断。
MAX_PACKAGES = 200


@dataclass(frozen=True)
class PluginPackage:
    name: str
    root: Path
    #: 这个包是从工作区（相对路径的根）里发现的吗。
    #:
    #: 判据：工作区里的包跟着仓库走 —— clone / 解压 / 同事发来的目录都会
    #: 带一份，而它里面写着要执行的命令（Hooks）或要起的进程（`.mcp.json`）。
    #: 所以这两样贡献要先过 `plugins_allow_workspace` 那道门。
    from_workspace: bool = False

    def component(self, name: str) -> Optional[Path]:
        p = self.root / name
        return p if p.exists() else None


def package_roots() -> list[Path]:
    """扫描根，按优先级排列。路径在调用时计算。"""
    home = Path.home()
    return [
        Path("plugins"),
        Path(".agents") / "plugins",
        home / ".agents" / "plugins",
        home / ".claude" / "plugins" / "cache",
        home / ".workbuddy" / "plugins" / "cache",
    ]


def workspace_plugins_allowed() -> bool:
    """工作区里的插件包能不能贡献 Hooks 与 MCP 服务器。默认关。

    判据与 `hooks_allow_project` / `lsp_allow_project` 逐字同一条：
    那两份配置跟着仓库走，而那些贡献都是"在你的机器上执行东西"。
    用户自己机器上的包根（`~/.agents/plugins` 与两个缓存目录）不看这个开关 ——
    它们不是跟着别人的仓库进来的。

    读不到设置文件 / 坏 JSON 就是关（与 `hooks._project_allowed` 同向）。
    每次现读，改完当场生效。
    """
    try:
        data = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("plugins_allow_workspace") is True


def _looks_like_package(d: Path) -> bool:
    for marker in _MARKERS:
        try:
            if (d / marker).exists():
                return True
        except OSError:
            continue
    return False


def _package_name(d: Path) -> str:
    for rel in ("plugin.json", ".claude-plugin/plugin.json"):
        p = d / rel
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            continue
        if isinstance(data, dict):
            name = str(data.get("name") or "").strip()
            if name:
                return name
    return d.name


def _walk(base: Path, depth: int, out: list[PluginPackage]) -> None:
    """在 *base* 下最多走 *depth* 层，找到包就停（不再深入）。"""
    if depth < 0 or len(out) >= MAX_PACKAGES:
        return
    if _looks_like_package(base):
        out.append(PluginPackage(_package_name(base), base))
        return
    if depth == 0:
        return
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith((".", "_", "$")):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        _walk(entry, depth - 1, out)


def discover_packages(roots: Iterable[Path] | None = None) -> list[PluginPackage]:
    """扫描全部根，返回去重后的包列表（按名字排序）。"""
    out: list[PluginPackage] = []
    seen: set[str] = set()
    for root in (roots if roots is not None else package_roots()):
        base = Path(root)
        if not base.is_dir():
            continue
        # 缓存目录是三层（市场/插件/版本），其余根是一层。
        depth = 3 if base.name == "cache" else 1
        found: list[PluginPackage] = []
        _walk(base, depth, found)
        _from_ws = not base.is_absolute()
        if _from_ws:
            found = [PluginPackage(p.name, p.root, True) for p in found]
        for pkg in found:
            try:
                key = str(pkg.root.resolve())
            except OSError:
                key = str(pkg.root)
            if key in seen:
                continue
            seen.add(key)
            out.append(pkg)
    return sorted(out, key=lambda p: (p.name, str(p.root)))


def agents_dirs(roots: Iterable[Path] | None = None) -> list[Path]:
    """所有插件包里的 `agents/` 目录。"""
    out: list[Path] = []
    for pkg in discover_packages(roots):
        d = pkg.component("agents")
        if d is not None and d.is_dir():
            out.append(d)
    return out
