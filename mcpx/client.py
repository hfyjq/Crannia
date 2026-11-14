"""
MCP Client — connect to external MCP servers and bridge their tools.

Supports:
  - stdio transport (spawn a subprocess, communicate via stdin/stdout)
  - Streamable HTTP transport (connect to a remote MCP server URL)
  - SSE transport (Server-Sent Events)

External MCP server tools are auto-discovered at connect time and
registered to bus/registry via the bridge.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import tool_permission as _tool_permission
from core import tool_output_kind as _tool_output_kind
from core.async_loop import open_owned
from mcpx.bridge import annotations_to_axes, mcp_tool_to_schema, make_mcp_handler


# 公开工具的名字是一个契约
#
# 两种处理并存：
#   · 把非法字符换成 `_`（API 允许的 pattern 是 `^[a-zA-Z0-9_-]{1,64}$`）
#   · 替换或截断改变了名字时，再附一段 `(serverName, rawName)` 的
#     12 位 SHA-256 尾巴，保证不同的 MCP 身份不会塌成同一个公开名
#
# 只换字符不够：MCP 允许工具名到 128 字符、含 `.`，
# 而函数名契约（OpenAI 兼容线 / Anthropic 都是它）只允许 `[A-Za-z0-9_-]` 且 ≤64。
# 直接用原名，轻则上游 400（整轮白跑），重则两个身份塌成同一个名字
# 而静默指错工具。

#: 函数名契约的长度上限 —— 两家 provider 线一致，取交集就是它。
MCP_NAME_MAX = 64

#: 12 位哈希尾巴的长度（含分隔它自己的那个 `_`）。
_HASH_TAIL = 13

_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _normalize_name(name: str) -> str:
    """把非法字符换成 `_`（只管字符，不管长度 —— 截断在 `_public_tool_name`）。

    它单独不够 —— 换字符是多对一的，
    两个不同的身份能换到同一个名字上（那正是哈希尾巴在补的洞）。
    """
    return _INVALID_NAME_CHARS.sub("_", name)


def _public_tool_name(server_name: str, raw_name: str) -> str:
    """`(server, raw)`，公开名。纯函数。

    ## 为什么不能只拼字符串

    `f"mcp__{server}__{raw}"` 有两个互相独立的塌缩源：

    | | 塌缩源 | 后果 |
    |---|---|---|
    | ① | 字符：MCP 允许到 128 字符、含 `.`，而函数名契约是 `[A-Za-z0-9_-]` 且 ≤64 | 上游 400（整轮白跑） |
    | ② | 分隔符自己：`server="a__b", raw="c"` 与 `server="a", raw="b__c"` 拼出来逐字相同 | 两个身份塌成同一个公开名，静默指错工具 |

    ②仍然成立 —— 规范化一个字符都没改它，所以"加个 `_normalize_name`
    就完了"是不够的（所以换字符之外还要加哈希尾巴）。

判据：名字动过（换过字符 或 超长被截）就拼一段
    12 位 sha256 尾巴，输入是 `server\\0raw` ——
    必须用 `\\0` 而不是 `__`：用 `__` 会让②那个塌缩源在哈希里重演
    （`("a__b","c")` 与 `("a","b__c")` 又会得到同一个哈希）。

    没动过的名字逐字不变 —— 真实世界里 `mcp__github__create_issue`
    这类本来就合法，所以这条规则对现存的合法名字一个字节都不改。
    也因此别把公开名反解回 `(server, raw)` ——
    名字是纯函数，反解在多 server 下不成立
    （公开名永远不发给 server，也永远不从公开名反解原名）。
    """
    joined = f"mcp__{server_name}__{raw_name}"
    normalized = _normalize_name(joined)
    # 三个条件各堵一头：`normalized == joined` 堵①的换字符、长度堵①的截断、
    #   `split("__") == 3` 堵②（`mcp__a__b__c` 拆出来是 4 段，那个串
    #   没法反解回唯一个 `(server, raw)`）。最后这条是本仓库自有 ——
    #   我们没有"重名 `serverName` 在装载时就失败"那条规矩
    #这处歧义只能在这里自己堵。
    if (normalized == joined and len(normalized) <= MCP_NAME_MAX
            and len(normalized.split("__")) == 3):
        return normalized
    digest = hashlib.sha256(
        f"{server_name}\0{raw_name}".encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:MCP_NAME_MAX - _HASH_TAIL]}_{digest}"


# 「契约 env」—— 让别人写的 server 不知道自己换了宿主
#
# 三层保证：读得进 · 谁放进来都能用、不用问我是谁 · 跑得起。
# 而对方的执行体（MCP server / 插件脚本）只认最后一层。
#
# 这就是"能直接装"的机制：插件里的 MCP server 靠 `CLAUDE_PLUGIN_ROOT`
#   这类变量定位自己的文件，宿主只要照这一套注入，**别人写的 server
#   原样就能跑**，它根本不知道自己换了宿主。
#   实际形态：有些 server 不读宿主的任何配置文件
#   （`.claude/*` 与 `.mcp.json` 全 0 命中），只认 `--mcp-config` /
#   `--strict-mcp-config` / 这几个 env。
#
# 代价写在明处：注入之后对方的 server 以为自己在原来那个宿主里。
#   那不是漏洞，是这条路买到东西的那一面 —— 但它意味着
#   「把别人的 server 拉起来」= 让别人的代码在你这台机器上跑，
#   而"信任边界"那一格（谁有权限往里放）不能省。


def build_contract_env(plugin_root: str | None = None) -> dict:
    """契约 env。调用方把它合进 server 自己的 `env`。

    | 变量 | 我们给什么 | 依据 |
    |---|---|---|
    | `CLAUDE_PROJECT_DIR` | `str(Path.cwd())` | 语义是"你从哪儿启动的"，与本仓库的约定一致 —— `core/skill_guidance.py::_skills_root` 的注释写着：生产里 cwd 就是仓库根。写成 `Path(__file__).parent.parent` 会多出第二套"仓库根在哪"（一个口径一个地方） |
    | `CLAUDE_PLUGIN_ROOT` | 只在调用方给得出时才给 | 见下 |

    ## `CLAUDE_PLUGIN_ROOT` 猜不得

    它的语义是「提供这个 server 的那个插件目录」—— server 用它找随包带的东西。
    我们没有"插件"这个单位（`data/mcp_servers.json` 里是裸的 `command`/`args`）。
猜一个（比如 `data/`）的后果是 server 摸到自己的文件却摸错地方：
    那比"没有这个变量"更难排查（后者它至少会明说缺什么）。
只在配置里声明了 `plugin_root` 时才注入。

    将来要读插件的目录布局（`.claude-plugin/plugin.json`）时，
      那个目录就是它 —— 到那时这里一个字都不用改，改的是喂给它的那个值。

    ## 两个有意不给的

    * `CLAUDE_SESSION_ID` —— 我们这条连接是进程级常驻的
      （进程里只有一条连接，活到进程结束）。没有一个"当前会话"可以填，
      而填一个错的比不填更坏（server 可能拿它当状态文件名，跨会话串）。
留白。等连接真按会话分了再给（那天这条注释要跟着改）。
    * `CLAUDE_PLUGIN_DATA` —— 它是插件的数据目录。我们同样没有
      "插件"这个单位，没有可指的目录。留白。

    合并次序：契约在前、server 自己的 `env` 在后（后者赢）——
    那是用户/作者显式写下的东西，他比我们清楚自己的 server 要什么。

    ## 顺带量到的：这一层是白名单，不是"继承父进程"

    `mcp.client.stdio.get_default_environment()` 只传 12 个：
    `APPDATA · HOMEDRIVE · HOMEPATH · LOCALAPPDATA · PATH · PATHEXT ·
     PROCESSOR_ARCHITECTURE · SYSTEMDRIVE · SYSTEMROOT · TEMP · USERNAME · USERPROFILE`

    （本机量过：父进程 103 个 → 传 12 个；`ANTHROPIC_AUTH_TOKEN` /
    `DEEPSEEK_API_KEY` / `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 全在砍掉的那一侧。
    实现是 `DEFAULT_INHERITED_ENV_VARS` 那张硬白名单，并且跳过以 `()` 开头的值
    —— 那是 bash 导出的函数定义，是一类注入面。）

两个结论：
    1. 我们不需要给 MCP 再写一套 env 白名单 —— SDK 已经给了，而且比我们会写的窄。
    2. **反过来说：`build_contract_env` 是"想给 server 任何东西"的唯一通路** ——
       不写在这里的东西，它必然看不到（这也正是它为什么值得单独一个函数）。

    别把这份名单与 `core/shell.py` 那 31 项的命令环境白名单合并 —— 威胁面不同：
    那条要的是"用户的工具链能用"（所以留 `PATH` 一族 + 工具变量），
    而一个 MCP server 什么都不该白拿（它要什么，由它自己的 `env` 声明）。
    """
    env = {"CLAUDE_PROJECT_DIR": str(Path.cwd())}
    if plugin_root:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return env


@dataclass
class ExternalMCPServer:
    """Descriptor for an external MCP server connection."""

    name: str
    transport: str  # "stdio" | "sse" | "streamable_http"

    # stdio transport
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    # HTTP transport
    url: str = ""
    #: HTTP 请求头（认证类 MCP 需要它）。只在内存里 ——
    #: 配置里存的是 `${VAR}` 占位符，展开发生在解析那一刻，不落盘、不打印。
    headers: dict = field(default_factory=dict)

    # Runtime state
    _session: Any = None
    #: 连接的家。
    #: 它是一个 `core.async_loop.OwnedResource` —— 一个专属任务从头到尾
    #: 持有 `stdio_client` + `ClientSession` 两层上下文，直到 `aclose()`。
    #: 不能改成"这里存个 `AsyncExitStack`、谁调谁退" —— 见 `open_owned`
    #: 的 docstring（anyio 的 task group 进入与退出必须在同一个 Task）。
    _owned: Any = None
    _tools: list[dict] = field(default_factory=list)

    @property
    def connected(self) -> bool:
        """真判据。

        为什么不能只看 `_session`：它一旦被赋值就再也不会变回 `None` ——
        若连接在 `connect_stdio_server` 返回之前就被 `async with` 关掉，
        这个属性照样会报 `True`。死连接报活
        （一个永远为真的判据不是判据）。

        三个条件都要：① 会话在 ② 专属任务还没收完栈（`owned.closed`）
        ③ 常驻循环还活着（循环一死，挂在它上面的 session 也废了）。

        它够不到什么（如实记，`别读成"堵上了"`）：子进程自己悄悄死了
        它看不出来 —— 那要等下一次 `call_tool` 抛错才知道。主动探活得加一次真的
        IPC 往返，而这是个属性（会被高频读），不在这里做。
        """
        if self._session is None or self._owned is None:
            return False
        if self._owned.closed:
            return False
        from core.async_loop import current_loop
        # 用只读的 `current_loop()`，不是 `get_loop()` —— 判定不许有副作用
        #   （`get_loop()` 会起线程，见它的 docstring）。
        return current_loop() is not None

    async def aclose(self) -> None:
        """关掉这个连接（连同它的子进程与管道）。幂等。

        走专属任务收栈 —— `submit(server.aclose())` 从任意线程调都行，
        真正退出上下文的仍然是那个任务（判据见 `open_owned`）。
        """
        owned, self._owned = self._owned, None
        self._session = None
        if owned is not None:
            await owned.aclose()


async def connect_stdio_server(
    name: str,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    plugin_root: str | None = None,
) -> ExternalMCPServer:
    """
    Connect to an MCP server via stdio transport.

    Args:
        name: Human-readable name for this server.
        command: Executable to spawn (e.g. "npx", "uvx", "python").
        args: Arguments to the command.
        env: Extra environment variables. 它赢过契约 env（见 `build_contract_env`）。
        plugin_root: 这个 server 的「插件根」。给了就注入
            `CLAUDE_PLUGIN_ROOT`；不给就不注入 —— 猜错比没有更难查。

    Returns:
        ExternalMCPServer with discovered tools populated.
    """
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    # 契约 env 在前、server 自己的 env 在后（后者赢）。
    #   合并后的那一份同时交给 SDK 与 dataclass —— 两处用同一个值，
    #   免得"跑起来用的"与"记下来的"不是同一个东西。
    merged_env = {**build_contract_env(plugin_root), **(env or {})}

    params = StdioServerParameters(
        command=command,
        args=args or [],
        env=merged_env,
    )

    server = ExternalMCPServer(
        name=name,
        transport="stdio",
        command=command,
        args=args or [],
        env=merged_env,
    )

    # 栈不退出。
    #   若写成 `async with stdio_client(...) as (read, write): async with ClientSession(...)`，
    #   两层的 `__aexit__` 都会在 `return server` 之前跑掉
    #外面拿到的 `server._session` 指向一个已经关掉的 session。
    #   症状很难认：`list_tools` 成功（工具列得出来，"看起来接上了"），
    #     而 `call_tool` 抛 `ClosedResourceError` —— 一个在返回之前就已经死了的连接。
    #把栈挂在 server 上，活到 `aclose()` 为止。
    async def _enter(stack: AsyncExitStack) -> ExternalMCPServer:
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.list_tools()
        server._tools = _tools_from_result(result)
        server._session = session
        return server

    # `open_owned` 起一个专属任务把这两层上下文一直握着（半路失败它会自己收栈）。
    server._owned = await open_owned(_enter)
    return server


def _redirect_guard(endpoint: str):
    """公网 endpoint 跳到本机 / 内网 —— 拒。返回 httpx 的 response 钩子。

    为什么只拦这一个方向：endpoint 本来就在本机/LAN 时，跳到别处是用户
    自己的部署；而"公网 → 内网"是跳板的形状 —— 用户批准的是原来那个地址。
    """
    from urllib.parse import urlsplit

    from core.security import UrlEgressError, host_is_local

    try:
        base_local = host_is_local(urlsplit(endpoint).hostname or "")
    except ValueError:
        base_local = False

    def _hook(response):
        if base_local:
            return
        try:
            hop_host = urlsplit(str(getattr(response, "url", ""))).hostname or ""
        except ValueError:
            return
        if hop_host and host_is_local(hop_host):
            print("[mcp] 重定向到本机/内网地址被拒：%s" % (response.url,))
            raise UrlEgressError(
                "MCP endpoint 从公网跳到了本机/内网：%s（用户批准的是原来那个地址）"
                % (response.url,))

    return _hook


def mcp_http_client_factory(headers=None, timeout=None, auth=None,
                            *, endpoint: str = ""):
    """MCP SDK 的 httpx 客户端工厂 —— 只加一件事：代理跟随设置页那一档。

    判据沿用 出网代理一处口径（`core/net_proxy.py`）。
      SDK 默认自己建客户端（`trust_env=True`，读环境变量），那等于绕过
      设置页 —— 用户切了「直连」而 MCP 还在走环境里的代理，看到的是
      "改了没反应"。本机就撞过：`curl` 走死代理连不上，
      而 SDK 那边恰好没有（两边对代理的处置不一致 = 用户无法预测）。
    其余默认值照抄 SDK（`follow_redirects=True` + 那两个超时），
      不要顺手改 —— 长连接的 read 超时短了，工具调用会莫名断在半路。
    """
    import httpx
    from core import net_proxy as _np
    from mcp.shared._httpx_utils import (
        MCP_DEFAULT_TIMEOUT, MCP_DEFAULT_SSE_READ_TIMEOUT,
    )

    kwargs: dict = {
        "follow_redirects": True,
        "timeout": timeout or httpx.Timeout(
            MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
    }
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    # 重定向逐跳检查。`follow_redirects=True` 保留（很多服务器 http→https 会跳），
    # 但每一跳都要过一次判定 —— 天花板那层对 `web_fetch` 就是这么做的。
    if endpoint:
        kwargs["event_hooks"] = {"response": [_redirect_guard(endpoint)]}
    # 走 `net_proxy.httpx_client`（不是自己拼 kwargs）——
    #   "全仓造客户端的地方都经过 `core/net_proxy`" 那条普查按调用点判
    #   （`tests/test_net_proxy.py`）。
    #: `url=` 让本机 / 内网的 MCP 服务器绕开 `manual` 档的代理（判据在
    #:   `core/net_proxy.is_local_target`）—— 本地服务器是正常部署，
    #:   被公司代理接走会回一句 502，而用户看到的是"这个服务器连不上"。
    return _np.httpx_client(httpx.AsyncClient, url=endpoint, **kwargs)


async def connect_http_server(
    name: str,
    url: str,
    headers: dict | None = None,
) -> ExternalMCPServer:
    """
    Connect to an MCP server via Streamable HTTP transport.

    Args:
        name: Human-readable name for this server.
        url: MCP server endpoint URL.

    Returns:
        ExternalMCPServer with discovered tools populated.
    """
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.client.session import ClientSession

    # 连接前先过判定：本机与 LAN 放行，其余走同一张出网地板。
    # 失败会抛到 `manager.connect_external` 那一侧 —— 它记账并屏蔽这台服务器，
    # 而不是让工具注册成功、调用时才炸。
    from core.security import check_mcp_endpoint
    check_mcp_endpoint(url)

    server = ExternalMCPServer(
        name=name,
        transport="streamable_http",
        url=url,
        headers=dict(headers or {}),
    )

    # 与 stdio 那条同一个形状（栈不退出）—— 见上面那段长注释。
    async def _enter(stack: AsyncExitStack) -> ExternalMCPServer:
        import functools
        _factory = functools.partial(mcp_http_client_factory, endpoint=url)
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(url, headers=(headers or None),
                                  httpx_client_factory=_factory))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.list_tools()
        server._tools = _tools_from_result(result)
        server._session = session
        return server

    server._owned = await open_owned(_enter)
    return server


def _tools_from_result(result: Any) -> list[dict]:
    """把 `list_tools()` 的结果转成我们的工具表。

    两处连接（stdio / http）逐字相同的那八行收在这里 —— 抄第二份的代价
    不是多写八行，是两份会漂移，而两个答案看上去都对。
    """
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": t.inputSchema if hasattr(t, "inputSchema") and t.inputSchema else {},
            # 把注解带出来 —— 它是那两根轴的唯一来源。
            #   只取三样、不读 `annotations`，每个外部工具
            #   都会落进兜底（`admin` / `destructive=False`），而没有任何东西红。
            "annotations": getattr(t, "annotations", None),
        }
        for t in result.tools
    ]


def _quarantine_dead_server(server) -> None:
    """一台 MCP 服务器在运行期被发现有毛病，记账 + 屏蔽。

    幂等（同一台服务器会被若干个工具各发现一次）—— `quarantine()` 自己幂等，
    这里只是省掉重复的打印。它自己不许炸：一个用来记账的动作把调用搞挂了，
    方向就反了（那台服务器本来就是坏的）。
    """
    try:
        from bus import quarantine as Q
        owner = Q.owner_for_mcp(server.name)
        first = owner not in Q.all_entries()
        entry = Q.quarantine(owner, code="mcp_session_lost",
                             reason="外部 MCP 服务器 '{}' 的连接掉了（调用时发现）"
                                    .format(server.name))
        if first:
            print(Q.describe(entry) + " —— 它名下的工具**全部**不再可用")
            # 同上：这是"真的坏了"（服务器掉线、工具下线），进 `#02` 的账。
            try:
                from core import incidents
                incidents.note("MCP：" + str(server.name), "mcp_quarantine",
                               "服务器连接掉了，它名下的工具全部不可用",
                               "code=mcp_session_lost")
            except Exception:                                   # noqa: BLE001
                pass
    except Exception as exc:                                        # noqa: BLE001
        print(f"[quarantine] 记账失败（{type(exc).__name__}: {exc}）")


def register_external_server(server: ExternalMCPServer, *,
                             trusted_output: bool = False) -> int:
    """
    Register tools from an external MCP server into bus/registry.

    Creates wrapper handlers that call the MCP server's tools via
    the persistent session.

    *trusted_output*：用户在 `data/mcp_servers.json` 里给这台服务器写了
    `trusted_output: true`。它只做一件事 —— 把这些工具的输出形态声明成
    `structured`（不隔离）。默认 False：外部 MCP 的返回按第三方内容处置，
    带 `<user_document trust="untrusted">` 包裹，并剥控制字符、转义框架保留标签。
    判据在本地（用户明确信任），不在服务器的注解里。

    Returns the number of tools registered.
    """
    from bus.registry import register_tool

    count = 0
    for tool_def in server._tools:
        tool_name = tool_def["name"]
        # 公开名是 `(server, raw)` 的纯函数 —— 见 `_public_tool_name`。
        prefixed_name = _public_tool_name(server.name, tool_name)

        # Build schema
        schema = mcp_tool_to_schema(tool_def)

        # Create handler that calls the MCP server's tool
        # 闭包里的是 `raw_name`，不是公开名
        #   （执行器闭包捕获的是 `rawName`，公开名永远不发给 server）——
        #   规范化之后这两者不是同一个串（有哈希尾巴），
        #   所以别改成 `_public_tool_name` 的结果，也别反解。
        # Note: since the session is async, we capture the tool_name
        async def _call_tool(name: str = tool_name, arguments: dict | None = None) -> Any:
            if server._session is None:
                # 运行期那一半：
                #   上面那个 `connected` 属性够不到"子进程自己悄悄死了"，
                #   而够得到的就是这里 —— 调用那一刻发现 `_session` 没了。
                #当场记账 + 屏蔽（同一台服务器的其它工具也一起收）。
                _quarantine_dead_server(server)
                raise RuntimeError(f"MCP server '{server.name}' not connected")
            return await server._session.call_tool(name, arguments or {})

        handler = make_mcp_handler(tool_name, _call_tool)

        # 先声明、后登记 —— 次序承重。
        #   `register_tool` 内部立刻 `_resolve_permission(name)` / `_resolve_destructive(name)`
        #   把结果冻结进注册表（`tool_perms` / `tool_destructive` + 两张 source 表）。
        #   声明晚一步，那两格就永远停在兜底值上，而且不会有任何东西红 ——
        #   两处都"有值"，只是来源那一列写着 `default` 而不是 `declared`
        #   （值对了、来源错了，等于没有）。
        perm, destructive = annotations_to_axes(tool_def.get("annotations"))
        if not _tool_permission.declare(prefixed_name, perm):
            print(f"[mcp] WARNING: '{tool_name}' 的档位 {perm!r} 被拒（落回兜底）")
        if not _tool_permission.declare_destructive(prefixed_name, destructive):
            print(f"[mcp] WARNING: '{tool_name}' 的 destructive 声明被拒（落回兜底）")
        if trusted_output:
            # 用户明确信任这台服务器只回结构化结果 —— 落到同一条 `declare`
            # 上，不另开一个"可信服务器"集合：两个真相源迟早漂，而漂的方向
            # 通常是更松（这边放行、那边忘了）。
            _tool_output_kind.declare(prefixed_name,
                                      _tool_output_kind.KIND_STRUCTURED)
        # `concurrency_safe` 有意不声明 —— MCP 规范里没有对应的 hint
        #   （`isConcurrencySafe` 在 SDK 1.28.1 里零命中）。
        #   它的兜底派生自权限档（`permission == "read"`），`readOnlyHint=True`
        #   的工具自动就是并发安全的。声明一遍只会把派生覆盖成同一个值，
        #   白白多一个"有人定过"的假信号。别顺手给它加一行。
        # 归属 = 这个 server。于是**两个 server
        # 撞名**（前缀拼出来一样）会被拒，而不是后到的静默顶掉前一个 ——
        # 那正是 `mcp__` 名无规范化、无防塌缩时最容易出事的形状。
        if register_tool(prefixed_name, handler, schema,
                         owner=f"mcp:{server.name}"):
            count += 1

    return count
