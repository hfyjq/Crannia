"""
MCP Manager — lifecycle, built-in server loading, external connection management.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

#: 「运行环境缺失」不是「连不上」。
#:
#: 现场：没装 Node 的机器上，`npx` 不存在，`_connect_stdio` 抛
#: `FileNotFoundError [WinError 2]`，被记成 `mcp_connect_failed`，界面弹
#: 「兼容性警告 / 不支持 / 已被 Crannia 屏蔽」。
#: 而真正的原因是这台机器少了那个可执行文件 —— 装上就好，
#: 和"这个插件坏了"是两件完全不同的事（标签在说谎）。
MCP_MISSING_RUNTIME = "mcp_missing_runtime"

#: 缺哪个可执行文件时该怎么跟用户说。只影响文案，不影响判定。
_RUNTIME_HINTS = {
    "npx": "Node.js（安装包自带npx）",
    "node": "Node.js",
    "npm": "Node.js（安装包自带npm）",
    "uvx": "uv（装好后 uvx 才在 PATH 上）",
    "uv": "uv（装好后 uv 才在 PATH 上）",
    "python": "Python",
    "python3": "Python",
}


def missing_runtime(cfg: dict) -> str:
    """stdio 服务器的启动命令在这台机器上找不找得到。

    返回缺的那个命令名（空串 = 没问题）。只对 stdio 有意义：
    `sse` / `streamable_http` 没有本地可执行文件。
    """
    import shutil
    from pathlib import Path

    if str(cfg.get("transport") or "stdio") != "stdio":
        return ""
    cmd = str(cfg.get("command") or "").strip()
    if not cmd:
        # 缺 `command` 是另一个错（配置写坏了），交给下面真实的连接去报 ——
        # 在这里冒充"缺运行时"会把一个配置错误说成"你没装东西"。
        return ""
    if "/" in cmd or "\\" in cmd:
        # 带路径的写法（`./node_modules/.bin/x`、`C:\node\npx.cmd`）：判文件在不在，
        # 不走 PATH —— `which()` 对带路径的输入语义与平台有关。
        return "" if Path(cmd).is_file() else cmd
    return "" if shutil.which(cmd) else cmd


def missing_runtime_reason(name: str, cmd: str) -> str:
    """给用户看的那句话。不出现「不兼容 / 不支持 / 已屏蔽」。"""
    hint = _RUNTIME_HINTS.get(cmd.lower().replace(".cmd", "").replace(".exe", ""),
                              "它依赖的那个程序")
    return ("外部 MCP 服务器「%s」要用命令 %s 启动，当前环境找不到此对象。"
            "这不是兼容性问题 —— 装上%s之后重启尝试；"
            "或改用 Python（python -m …）或 HTTP 型的 MCP 服务器。"
            % (name, cmd, hint))


@dataclass
class MCPManager:
    """Central orchestrator for MCP integration.

    Called once at framework startup by bus/core.py.  Loads built-in
    servers, optionally connects to configured external servers, and
    bridges all discovered tools to bus/registry.
    """

    config: dict = field(default_factory=dict)
    _external_servers: dict[str, Any] = field(default_factory=dict)
    _tools_registered: int = 0

    def load_builtin_servers(self) -> int:
        """
        Load all built-in MCP servers and register their tools.

        Built-in servers are defined in mcp/servers/ and run in-process.
        Their tools are directly registered to bus/registry without
        going through MCP transport.

        Returns the total number of tools registered.
        """
        import importlib
        from mcpx.servers import BUILTIN_SERVERS

        total = 0
        # 按 **`mcpx/servers/__init__.py::BUILTIN_SERVERS`**
        #   那份名单去 import —— 不要在这里写死某个具体模块名，
        #   否则"内置 MCP 有哪几条"会在三个地方各有一份（这里 / `get_status`
        #   里那个常数 / `web/server.py` 的两处硬编码）。
        for name in BUILTIN_SERVERS:
            try:
                mod = importlib.import_module("mcpx.servers." + name)
                n = mod.register_tools()
                total += n
                print(f"[mcp] Built-in {name}: {n} tools registered")
            except Exception as e:                              # noqa: BLE001
                print(f"[mcp] WARNING: Failed to load {name} server: {e}")

        self._tools_registered = total
        return total

    def connect_external(self, server_configs: list[dict]) -> int:
        """
        Connect to external MCP servers and register their tools.

        Each config dict should have:
          - name: str
          - transport: "stdio" | "sse" | "streamable_http"
          - command, args, env (for stdio)
          - url (for HTTP)

        Returns the number of tools registered.
        """
        # 必须用 `submit(...)`，不能用 `asyncio.run(...)`。
        #   每个 server 起一个新的循环时，连接绑定在"建立它的那个循环"上
        #循环一退连接就废（症状同 `mcpx/bridge.py::_run_async` 那条）。
        #全部投到同一个常驻循环上（`core/async_loop.py`）。
        from core.async_loop import submit

        total = 0
        for cfg in server_configs:
            name = cfg.get("name", "unknown")
            transport = cfg.get("transport", "stdio")

            # 连之前先看环境：缺 `npx` / `uvx` 与"服务器坏了"
            #    是两件事，而它们此前被压成同一句「不兼容 / 已屏蔽」。
            missing = missing_runtime(cfg)
            if missing:
                print(f"[mcp] '{name}' 跳过：找不到命令 {missing}（运行环境缺失，不是兼容性问题）")
                # `detail` 只写 ASCII 的 `command=<名字>`：界面从这一格里
                #   抠命令名，再用 `t()` 拼一句当前语言的提示（`plugin-notice-view.js
                #   的 missingCommand`）。写中文进去，英文界面就只能显示中文。
                self._record(name, code=MCP_MISSING_RUNTIME,
                             reason=missing_runtime_reason(name, missing),
                             detail="command=%s" % missing)
                continue

            try:
                if transport == "stdio":
                    server = submit(_connect_stdio(name, cfg))
                elif transport in ("sse", "streamable_http"):
                    server = submit(_connect_http(name, cfg))
                else:
                    print(f"[mcp] Unknown transport '{transport}' for server '{name}'")
                    continue

                if server and server._tools:
                    from mcpx.client import register_external_server
                    n = register_external_server(
                        server, trusted_output=cfg.get("trusted_output") is True)
                    self._external_servers[name] = server
                    total += n
                    print(f"[mcp] External '{name}': {n} tools ({transport})")
                    # 连上了就把旧账划掉——
                    #   账本是持久的，而闸只看账本。用户装好 Node 再重启时，
                    #   上一次那条 `mcp_missing_runtime` / `mcp_connect_failed`
                    #   还在，服务器连上了、工具却全被摘掉，而且不会再弹窗
                    #   （`hits` 已经不是 1）。一个"修好也没救"的死结。
                    self._clear(name)
            except Exception as e:
                print(f"[mcp] WARNING: Failed to connect '{name}': {e}")
                # 连不上的外部服务器要记账 + 屏蔽。
                #   只 print 一行、不落任何状态时 —— "这台服务器挂了"
                #   只在启动那一刻的终端里有一行字，而工具要么没注册（模型说
                #   "我没有这个工具"），要么注册了、调用时才抛（模型自己去猜）。
                self._record(name, code="mcp_connect_failed",
                             reason="外部 MCP 服务器连不上：%s: %s" % (type(e).__name__, e),
                             detail=str(e)[:2000])

        self._tools_registered += total
        return total

    @staticmethod
    def _record(name: str, *, code: str, reason: str, detail: str = "") -> None:
        """记账 + 屏蔽一条外部服务器。它自己不许炸（记不上账不该中断启动）。

        归属键逐字同 `register_external_server` 那一处
        （它是"工具属于谁"的唯一来源，见 `bus/quarantine.owner_for_mcp`）。
        """
        try:
            from bus.quarantine import owner_for_mcp, quarantine, describe
            print(describe(quarantine(owner_for_mcp(name), code=code,
                                      reason=reason, detail=detail)))
        except Exception as inner:                              # noqa: BLE001
            print(f"[quarantine] 记账失败（{type(inner).__name__}: {inner}）")

    @staticmethod
    def _clear(name: str) -> None:
        """连上了，把这条的旧账划掉（幂等：没有那条账就什么都不做）。"""
        try:
            from bus.quarantine import all_entries, owner_for_mcp, release
            owner = owner_for_mcp(name)
            if owner in all_entries():
                release(owner)
                print(f"[quarantine] '{owner}' 这次连上了 —— **已解封**")
        except Exception as inner:                              # noqa: BLE001
            print(f"[quarantine] 解封失败（{type(inner).__name__}: {inner}）")

    def get_status(self) -> dict:
        """MCP 子系统的状态。两半都必须据实：

        不要返回手抄的常数（例如把内置数量写死为 `1`）——
        名字要从 `mcpx.servers.BUILTIN_SERVERS` 来、外部的从 `_external_servers` 现数。
        """
        from mcpx.servers import BUILTIN_SERVERS
        return {
            "builtin_servers": len(BUILTIN_SERVERS),
            "builtin_names": list(BUILTIN_SERVERS),
            "external_servers": len(self._external_servers),
            "external_names": sorted(self._external_servers),
            "tools_registered": self._tools_registered,
        }


async def _connect_stdio(name: str, cfg: dict):
    """Async helper for stdio connections."""
    from mcpx.client import connect_stdio_server
    return await connect_stdio_server(
        name=name,
        command=cfg["command"],
        args=cfg.get("args", []),
        env=cfg.get("env"),
        # `plugin_root` 不是从配置里推断出来的，而是"这个 server 住在哪个
        #   插件目录"；入口只有这一个，显式声明才认。
        plugin_root=cfg.get("plugin_root"),
    )


async def _connect_http(name: str, cfg: dict):
    """Async helper for HTTP connections."""
    from mcpx.client import connect_http_server
    return await connect_http_server(
        name=name,
        url=cfg["url"],
        headers=cfg.get("headers") or None,
    )


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_manager: MCPManager | None = None


def get_manager() -> MCPManager:
    """Return the module-level MCPManager singleton."""
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


def init_mcp(config: dict | None = None) -> int:
    """
    One-shot initialisation called by bus/core.py at startup.

    Loads built-in servers and external servers from
    `data/mcp_servers.json` (standard MCP format).

    Returns the number of MCP tools registered.
    """
    mgr = get_manager()
    mgr.config = config or {}

    total = mgr.load_builtin_servers()

    # Load external servers from data/mcp_servers.json (standard format)
    external = _load_external_config()
    if not external:
        # Fallback: legacy config.yaml mcp_servers
        external = mgr.config.get("mcp_servers", [])

    if external:
        total += mgr.connect_external(external)

    print(f"[mcp] Initialised: {total} tools ({len(mgr._external_servers)} external servers)")
    return total


#: 配置里的环境变量占位符：`${NAME}`。
#: 展开发生在解析那一刻（我们进程内），因为 stdio 的子进程环境是
#: 白名单清洗过的 —— 把展开留给它拿不到值。
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_headers(raw):
    """把 `{"Authorization": "Bearer ${TOKEN}"}` 展开成真值。

    返回 `(headers, missing)`：`missing` 非空，这个 server 该被跳过并出声。
    只念变量名，不念值 —— 那是凭据，它不该出现在任何一行日志里。
    """
    if not isinstance(raw, dict) or not raw:
        return {}, []
    out: dict = {}
    missing: list[str] = []
    for k, v in raw.items():
        if not isinstance(v, str):
            continue

        def _sub(m, _missing=missing):
            name = m.group(1)
            val = os.environ.get(name)
            if not val:
                _missing.append(name)
                return m.group(0)
            return val

        out[str(k)] = _ENV_PLACEHOLDER.sub(_sub, v)
    return out, missing


#: 插件根占位符。声明了 `plugin_root` 时在 command / args / env 里替换。
_PLUGIN_ROOT_TOKENS = ("${CLAUDE_PLUGIN_ROOT}", "${CRANNIA_PLUGIN_ROOT}")


def _expand_plugin_root(value, root):
    """把插件根占位符替换成实际路径。未声明 plugin_root 时原样返回。"""
    if not root:
        return value
    if isinstance(value, str):
        for token in _PLUGIN_ROOT_TOKENS:
            value = value.replace(token, str(root))
        return value
    if isinstance(value, list):
        return [_expand_plugin_root(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _expand_plugin_root(v, root) for k, v in value.items()}
    return value


def _looks_like_server(v) -> bool:
    """一个值像不像"MCP 服务器配置"：dict，且带 `command` 或 `url`。

    判据窄是有意的：宁可少认一条，也不能把 `{"$schema": "..."}` 这种
    键当成服务器名（那会去起一个叫 `$schema` 的进程）。
    """
    return isinstance(v, dict) and ("command" in v or "url" in v)


def _looks_like_flat_map(data) -> bool:
    """整份配置是不是平铺的（没有 `mcpServers`，每个值都像服务器）。

    全部值都要像 —— 有一个不像就不是平铺，按坏配置处理（返回空）。
    """
    return (isinstance(data, dict) and bool(data)
            and all(_looks_like_server(v) for v in data.values()))


def _load_user_mcp_config() -> list[dict]:
    """
    Load external MCP server configs from `data/mcp_servers.json`.

    Converts the standard MCP format::

        {"mcpServers": {"name": {"command": "npx", "args": [...]}}}

    to the internal format::

        [{"name": "name", "transport": "stdio", "command": "npx", "args": [...]}]
    """
    import json
    from pathlib import Path

    cfg_path = Path("data/mcp_servers.json")
    if not cfg_path.exists():
        return []

    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[mcp] WARNING: Failed to parse mcp_servers.json: {e}")
        return []

    servers = data.get("mcpServers")
    if isinstance(servers, dict) and servers:
        # 包装层优先。顶层同时还有 server 条目，收前者、忽略后者并出声
        #   （静默丢配置是反模式；混写说明用户以为两份都生效）。
        extra = [k for k, v in data.items()
                 if k != "mcpServers" and _looks_like_server(v)]
        if extra:
            print("[mcp] 这份配置同时有 `mcpServers` 和顶层条目 —— "
                  "只收前者，忽略 %s" % ", ".join(sorted(extra)))
    elif _looks_like_flat_map(data):
        # 平铺形状：没有 `mcpServers` 包装层，直接是 `{"名字": {...}}`。
        #   相当一部分现成配置是这一种 —— 只认包装层会有一大片装不上。
        servers = data
    else:
        return []
    if not servers:
        return []

    return _entries_from_servers(servers)


def _entries_from_servers(servers: dict, default_root: str | None = None) -> list[dict]:
    """把一份 `{名字: 配置}` 收成内部条目。*default_root* 用于插件包。"""
    result: list[dict] = []
    for name, cfg in servers.items():
        entry: dict = {"name": name}

        # `trusted_output` 必须跟着走这一跳。
        #   消费点 `connect_external` 读的是转换后的 cfg，不是原始配置 ——
        #   原来这一格只留在原始 cfg 里，到这里就没了，于是
        #   `register_external_server(..., trusted_output=False)` 永远是 False。
        #   用户按文档写了 `trusted_output: true`，输出照样被隔离；而没有任何
        #   东西红（注册函数那条 `is True` 只是没命中）。
        #   只收真 True —— 与消费点同一条严格判定，字符串 "true" 不算。
        if cfg.get("trusted_output") is True:
            entry["trusted_output"] = True

        command = cfg.get("command", "")
        args = cfg.get("args", [])
        env = cfg.get("env", {})

        if command:
            # stdio transport (has a command to run)
            _root = cfg.get("plugin_root") or default_root
            entry["transport"] = "stdio"
            entry["command"] = _expand_plugin_root(command, _root)
            entry["args"] = _expand_plugin_root(args, _root)
            entry["env"] = _expand_plugin_root(env, _root)
            # `plugin_root` 是我们加的一个键 ——
            #   标准 MCP 配置里没有它（那个值来自"文件在哪个插件目录"）。
            #   插件布局解析可以自动填这一格；
            #   在那之前，手写一个就能让"靠 `CLAUDE_PLUGIN_ROOT` 找自己文件"
            #   的 server 跑起来。
            if _root:
                entry["plugin_root"] = _root
        elif cfg.get("url"):
            # HTTP transport (has a URL)
            entry["transport"] = "streamable_http"
            entry["url"] = cfg["url"]
            # 认证头：展开 `${VAR}`；缺变量就跳过这个 server 并出声
            #   （fail-closed —— 发一个空 token 上去只会得到一次 401，而
            #   用户看不出是"本机没配"还是"服务端拒了"）。
            hdrs, missing = _expand_headers(cfg.get("headers"))
            if missing:
                print("[mcp] WARNING: server '%s' 需要环境变量 %s，本机没有 —— 跳过"
                      % (name, ", ".join(sorted(set(missing)))))
                continue
            if hdrs:
                entry["headers"] = hdrs
        else:
            print(f"[mcp] WARNING: Server '{name}' missing 'command' or 'url' — skipped")
            continue

        result.append(entry)

    return result


def _package_mcp_configs(taken: set) -> list[dict]:
    """插件包里的 `.mcp.json`。只扫用户自己放的包根，不扫其他产品的缓存。

    *taken* 是已经被用户配置文件占用的服务器名：重名时以用户配置为准。
    """
    import json
    from pathlib import Path

    try:
        from core import plugin_packages as _pp
    except Exception:                                         # noqa: BLE001
        return []

    roots = [Path("plugins"),
             Path(".agents") / "plugins",
             Path.home() / ".agents" / "plugins"]
    allowed = _pp.workspace_plugins_allowed()
    out: list[dict] = []
    for pkg in _pp.discover_packages(roots):
        if pkg.from_workspace and not allowed:
            if (pkg.root / ".mcp.json").is_file():
                print("[mcp] 工作区插件包 %s 的 .mcp.json 没有加载：要先在"
                      "「设置 → 安全设置 → 工作区插件」里打开" % pkg.name)
            continue
        f = pkg.root / ".mcp.json"
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:                              # noqa: BLE001
            print("[mcp] WARNING: 插件包 %s 的 .mcp.json 读不出来（%s: %s）—— 跳过"
                  % (pkg.name, type(exc).__name__, exc))
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            servers = data if _looks_like_flat_map(data) else {}
        if not isinstance(servers, dict) or not servers:
            continue
        dup = sorted(set(servers) & taken)
        if dup:
            print("[mcp] 插件包 %s 的服务器 %s 与已有配置重名 —— 以已有配置为准"
                  % (pkg.name, ", ".join(dup)))
            servers = {k: v for k, v in servers.items() if k not in taken}
        # 绝对路径：子进程的 cwd 不一定与我们相同。
        try:
            _root = str(pkg.root.resolve())
        except OSError:
            _root = str(pkg.root)
        entries = _entries_from_servers(servers, default_root=_root)
        taken.update(e["name"] for e in entries)
        out.extend(entries)
    return out


def _load_external_config() -> list[dict]:
    """外部 MCP 配置：用户文件优先，插件包补充。

    插件包里的服务器自动带上 `plugin_root`，因此 `.mcp.json` 里可以用
    `${CLAUDE_PLUGIN_ROOT}` 定位自己的文件。
    """
    user = _load_user_mcp_config()
    taken = {e["name"] for e in user}
    return user + _package_mcp_configs(taken)
