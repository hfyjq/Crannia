"""
Security kernel v2: three-tier path classification, workspace permissions, audit logging.

Level 0 — FORBIDDEN  : system dirs, never accessible
Level 1 — READ_ONLY  : cross-zone user dirs (search/read/copy only)
Level 2 — READ_WRITE : workspace dirs (full access, per-workspace fine-grained perms)

Executed BEFORE any file I/O or command execution. Any violation
raises SecurityError immediately — no recovery path that bypasses
the guard.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from enum import IntEnum, IntFlag
from pathlib import Path
from typing import Callable


class PathLevel(IntEnum):
    """Three-tier path classification."""
    FORBIDDEN = 0    # System dirs — always blocked
    READ_ONLY = 1    # Cross-zone user dirs — read/search/copy only
    READ_WRITE = 2   # Workspace dirs — full access subject to perms


class WorkspacePerm(IntFlag):
    """Fine-grained permissions within a workspace (Level 2)."""
    COPY = 1
    DELETE = 2
    MOVE = 4
    RENAME = 8
    WRITE = 16
    FULL = 31  # all flags


# System-critical paths, classified Level 0: never accessible.
DEFAULT_FORBIDDEN_DIRS: list[str] = [
    "C:/Windows",
    "C:/Program Files",
    "C:/Program Files (x86)",
    "C:/ProgramData",
    "C:/$Recycle.Bin",
    "C:/System Volume Information",
    "C:/Recovery",
]

#: 框架自己的敏感路径。
#:
#: 为什么必须有这一条：`classify_path` 的第 4 条兜底若写成 `FORBIDDEN`
#: （"在用户目录外，拒绝"），会把整个仓库树也拒掉 —— 因为本仓库
#: 在 `D:\work\…`、而用户目录在 `C:\Users\…`，agent 连自己项目里的文件都读不到，
#: 而用户要读的数据表只要不在用户目录下就一起被拒。
#:
#: 判据：「只要这个文件不在规避目录里 —— 那他就应该可以被读到」。
#: 兜底是 `READ_ONLY`（写仍然限工作区，那一半不动）。
#:
#: 但兜底一放，"规避清单"就成了唯一的保护，它必须变实。
#: 下面是框架自己的密钥与不可外读的账本，一条一条都要说得出为什么：
#:
#:   - `.env` 一族 —— API Key。框架靠它连上游，agent 读它没有任何正当用途。
#:   - `data/settings.json` —— 明文凭据（邮箱密码 / `web_token`），
#:     仓库自己 `.gitignore` 掉的就是它。
#:   - `data/memory/` —— 记忆库正文（长期事实 + 会话笔记 + 短期库）。
#:     用户身份信息就在里头 —— 它本来就是给 agent 用的，但走的是
#:     `memory_read`/`memory_search` 那条受控的路，不是 `read_file` 直读。
#:   - `logs/` —— 审计日志。 `ICE` 之后它里面有真值，
#:     让 agent 直接读等于给了一条绕过伪名化把真值捞回去的路。
#:
#: 相对路径，构造时按 cwd 解析（与 `DEFAULT_FRAMEWORK_READABLE_DIRS` 同一条判据）；
#: 生产在仓库根跑，测试在 `sandbox()` 里跑 —— 两边都对。
#: `_FORBIDDEN_SUBSTRINGS` 里不许放 `.env`：那是子串匹配，
#: `.environment` / `foo.env.bak` 会被一起打中。`.env` 走文件名匹配（见 `_is_forbidden`）。
DEFAULT_FORBIDDEN_PATHS: list[str] = [
    "data/settings.json",
    "data/memory",
    "logs",
    # 两份装明文 api key 的账本
    # 为什么必须在这里：兜底那一档是 `READ_ONLY`
    #    （判据「不在规避目录里就应该读得到」），这份清单就是"读不到"的
    #    唯一依据。漏一条 = 模型读得到自己的 key，而没有任何东西会红。
    # `web/server.py::_API_KEYS_PATH` 指向的是同一类账本。
    "data/api_keys.json",
    "data/runtime/provider_credentials.json",
    # QQ 机器人的 appId / appSecret。读到它就能冒充那个机器人发消息。
    # 旧的那份单文件（升级后由配置条目账本接管，文件可能还在）。
    "data/qq_bot_credentials.json",
    # 通道凭据条目（飞书 / 钉钉 / Telegram / QQ 的几套凭据都在这一份里）。
    "data/runtime/channel_credentials.json",
]

#: `.env` 一族的文件名（精确名或前缀 + `.`），不是子串。
_ENV_FILE_NAME = ".env"

#: 框架自己写、agent 必须能读回来的目录（Level 1 READ_ONLY）。
#:
#: 存在的唯一理由：工具结果超长时，框架会把它整份写到
#: `data/workdata/tool_results/`，然后在消息里告诉模型
#: *"Full output (7,507 chars) written to disk."* —— 如果那个路径在默认分级下是
#: FORBIDDEN（"在用户目录外，拒绝"，而 `data/` 正好落在那里）
#: 那句话是假的，agent 永远读不回来。
#:
#: 修法不是"别写"，是让写下去的东西真能取回来：
#: 制品不删、降级成指针，而指针必须真能取到。
#:
#: 只放 `data/workdata`，绝不放整个 `data/` —— 那里还有 `events/`、
#: `memory/`，agent 没有任何理由去读它自己的记忆库。
#: 而且这里给的是 READ_ONLY：框架写、agent 只读，方向与职责都一致。
DEFAULT_FRAMEWORK_READABLE_DIRS: list[str] = ["data/workdata"]

# Substrings that, if present anywhere in a path, mark it as forbidden
_FORBIDDEN_SUBSTRINGS: list[str] = [
    "System32",
    "SysWOW64",
    "WinSxS",
    "Windows Defender",
    "Microsoft SDKs",
]


class SecurityError(Exception):
    """Raised when a security policy is violated."""
    pass


class WriteNeedsApproval(SecurityError):
    """工作区之外写文件 —— 不是硬拒，是一道审批。

    ## 它为什么存在

    判据：「只要这个动作能过审批，那就放行」——
    而默认的"工作区外写"是硬拒（`READ_ONLY` + 非 read/copy，`SecurityError`）。
    于是「在一个目录改、在另一个目录写东西」做不到。

    ## 为什么是异常而不是"返回值"

    判定点在 :meth:`SecurityGuard.validate_path` —— 它在几百个 skill 调用点
    的深处，而"弹一次审批"这件事只有派发层做得了（它拿得到
    `messages` / `provider` / `origin`）。异常是这条链上唯一的回传通道。

    它是 `SecurityError` 的子类（有意的）：所有既有的
    `except SecurityError` 兜底一个字都不用改，而派发层那条
    `except WriteNeedsApproval` 排在前面，只有知道该接的人接得住。

    ## 三条它自带的硬约束

    ① 抛它的时候一个字节都还没写 —— `validate_path` 在每一条写路的最前面
       （`file_parser._ensure_output_path` 那条注释逐字写着"写盘的东西必须按
       write 过围栏"）。挂起之后"什么都没发生"这条不变量成立。
       已知的一处例外：`_ensure_output_path` 在过闸之前会
       `mkdir(parents=True, exist_ok=True)` —— 那个副作用在硬拒那条路上也存在。
    ② `FORBIDDEN` 走不到这里 —— `classify_path` 第 1 条就是它，
`.env` 一族即使躺在被批准的目录里也照旧硬拒。
    ③ 它只由"这一次调用"承担：同意之后框架重放那一次调用，
       不是"把那个目录永久打开"。
    """

    def __init__(self, message: str, *, path: str = "", op: str = "",
                 subject: str = ""):
        super().__init__(message)
        #: 被拒的那个文件（绝对路径）—— 报给用户看的就是它。
        self.path = str(path)
        #: 被拒的那个动作（当前只会是 `"write"`，见 `_write_outside_may_ask`）。
        self.op = str(op)
        #: 被审批物 = 目标的父目录。
        #: 它与放行判定（`core.workspace.write_outside_granted`）问的
        #: 必须是同一个串 —— 两个串各算一次就会漂，而症状是
        #: 批了之后照样弹。
        self.subject = str(subject)


# 出网侧的地板 —— 路径分级管文件，这一段管网
#
# 致命三要素（私有数据 + 不可信内容 + 对外发送）的第三条腿。这一段是确定性
# 的那一半：非公网目标没有任何正当用途，不问用户，直接拒。
#
# 已知实现的分工可以归成三类：
#   · 客户端没有 SSRF 地板，把准入放在服务端（fail-closed）；
#     客户端只有 `hostname.split('.').length < 2` 这一条结构性拦截
#   · 有字面量层（每跳校验）+ 规范 IP 段表，
#     但 DNS 层那一半不可达（挂着它的那一层配置只有读点、没有写点）
# 写得最好的那层一行没跑过
#   · 按 IP 段分类库 + 整组地址拒绝 + 连接钉死 + NAT64/DNS64 前缀
#
# 我们没有那层中转服务，所以放在服务端的那一半必须自己做。
# 不能用"域名黑名单"这个形状 —— 要按 RFC 段分类。
#    枚举域名必然漏（见下面 `_is_public_ip` 那段）。
#
# 本段的两条已知残余（写清楚，别让下一个人以为堵上了）：
#   1. DNS rebinding 没堵。这里解析一次、之后由 HTTP 客户端再解析一次，
#      两次之间是 TOCTOU 窗口。彻底的办法是把解析结果钉进连接
#      （把 lookup 钉进 HTTP 客户端）；Python 的 urllib/curl_cffi
#      都没有对应的干净接点，猴补 `socket.getaddrinfo` 是全局态，
#      在 waitress `threads=12` 下不安全。
# 缓解（不是堵死）：`_resolve_public_addresses` 整组拒绝 ——
#      轮询式 rebinding（一半公网一半内网）会被挡下；只有按时间换答案的
#      能过。
#   2. `169.254.169.254` 这类云元数据端点没有专项黑名单，靠 `is_link_local`
#      挡。这是刻意的：专项表会烂，分类不会。

#: 只允许这两种 scheme。`file:` / `ftp:` / `gopher:` / `data:` 一律拒。
#: 已知实现有把 `http` 升级为 `https` 再发的做法。这里只做白名单，
#: 不升级 —— 升级会改变请求语义，而且国内仍有大量只有 http 的站点
#: （`.gov.cn` 一类）。这条取舍记录在案。
_EGRESS_SCHEMES: tuple[str, ...] = ("http", "https")


class UrlEgressError(SecurityError):
    """出网目标被地板拒绝。

    单独一个类型（而不是复用 `SecurityError`）是为了让调用方能区分
    "路径被拒"与"目标被拒"，两者的用户提示不一样。
    """


def _is_public_ip(ip) -> bool:
    """这个地址是不是可用于对外访问的公网单播地址。

    按 RFC 段分类，不枚举（依据见本节开头）。判据是量出来的，
    不是设计的 —— 拿一份 15 个"必须拒"地址的测试清单逐个量过，
    `is_global` 单独漏 3 个：

        `224.0.0.1`        is_global=True   ← IPv4 多播
        `ff02::1`          is_global=True   ← IPv6 多播
        `64:ff9b::808:808` is_global=True   ← NAT64 前缀

四个否定标志一个都不能省。漏掉 `is_reserved` 就漏掉整族 NAT64，
    而 NAT64 是包装器 —— 它能裹着 `10.0.0.1` 穿过任何"看着像公网"的检查。
    包装器没法枚举，只能分类。

    不需要解包（有的实现专门写了一层 NAT64 解包；这里不做）：
    实际 9 种包装器（NAT64 well-known / RFC8215 local-use / IPv4-mapped /
    IPv4-compatible / 6to4 / Teredo / 文档段）全部被上面这几条挡住。
    代价是过度拦截 —— `64:ff9b::808:808`（NAT64 裹公网 8.8.8.8）也拒。
    这与"必须拒"的测试清单一致，不是偏差。
    现实后果：纯 NAT64 网络下抓不到网。记录在案。

    哪几个标志真的承重 —— 量下来，拿 22 条判据逐个去掉
    一个标志再跑：

        is_multicast      漏 2 条   `224.0.0.1` `ff02::1`
        is_reserved       漏 3 条   `64:ff9b::808:808` `64:ff9b::a00:1` `::10.0.0.1`
        is_private        漏 0 条   ← 被 `is_global` 遮蔽
        is_loopback       漏 0 条   ← 同上
        is_link_local     漏 0 条   ← 同上
        is_unspecified    漏 0 条   ← 同上

只有两个标志在当前判据集下承重（`is_global` 单独漏 5/22）。
    但那四个遮蔽项刻意保留，理由不是"可能有用"，是：
    `is_global` 是 CPython 里跨版本改过语义的属性（`192.0.0.0/24` 一类），
    而 `is_private` / `is_loopback` / `is_link_local` / `is_unspecified`
    是按 RFC 段定义的稳定属性。让稳定的那组当主判据，`is_global` 当兜底，
    顺序与直觉相反但意图如此。
    代价：这四条无法用负对照证伪（去掉都不红）。别再假设"测试覆盖了它们" ——
    覆盖它们的是 `tests/test_url_egress.py` 里那 22 条地址，不是这四行代码。
    """
    if (ip.is_multicast or ip.is_reserved or ip.is_private
            or ip.is_loopback or ip.is_link_local or ip.is_unspecified):
        return False
    return ip.is_global


#: fake-ip 的占位段。Clash / Surge 这类工具的默认值就是它 ——
#: RFC 2544 网络设备基准测试段，正常公网不会出现。
_FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"),)


def _is_fake_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    return any(ip in net for net in _FAKE_IP_NETS)


def _resolve_public_addresses(hostname: str) -> list[str]:
    """把 *hostname* 解析成地址列表；组内任一非公网，整组抛错。

    「整组拒绝」是一条既有规则，采用它的理由是
    它同时是一层 rebinding 缓解：轮询式 rebinding（一次查询里一半公网
    一半内网）会被挡下。留下的窗口只有"按时间换答案"，见本节开头的残余 1。

    返回的是地址字符串（只用于日志/报错，不进模型的上下文 ——
    刻意不把解析出的内网 IP 泄露进模型上下文）。
    """
    import socket as _socket

    try:
        infos = _socket.getaddrinfo(hostname, None, proto=_socket.IPPROTO_TCP)
    except _socket.gaierror as e:
        raise UrlEgressError(f"无法解析主机名 {hostname!r}（{e.strerror or e}）") from e

    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        # IPv6 带 scope id（`fe80::1%eth0`），分类前要剥掉
        addr = addr.split("%", 1)[0]
        if addr not in addrs:
            addrs.append(addr)

    if not addrs:
        raise UrlEgressError(f"主机名 {hostname!r} 没有解析出任何地址")

    # fake-ip：整组都落在 198.18.0.0/15，这不是真实目标。
    #   本机开着 Clash 的 fake-ip 时，每一个域名都解析到这个基准测试段
    #   （`example.com -> 198.18.0.53`），不认它的话，地板会把
    #   所有网站都拒掉，用户看到的是"一开代理框架就废了"。
    #   判据刻意只看这一段（RFC 2544 基准段，正常公网不会出现）——
    #   不按"有没有配代理"放宽，因为 DNS 是系统做的、跟我们那档设置无关。
    if all(_is_fake_ip(a) for a in addrs):
        print(f"[egress] {hostname} -> {addrs[0]} 是 fake-ip 占位地址"
              f"（代理的 DNS 模式）—— 跳过 IP 段判定")
        return addrs

    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise UrlEgressError(f"主机名 {hostname!r} 解析出无法识别的地址") from None
        if not _is_public_ip(ip):
            # 地址不进异常文本（那条会进模型上下文）。只进 print。
            print(f"[egress] DENY {hostname} -> {addr} (non-public address)")
            raise UrlEgressError(
                f"拒绝：{hostname} 解析到非公网地址（内网 / 回环 / 保留段）。"
                f"出网只允许访问公网目标。"
            )
    return addrs


def _dns_is_authoritative() -> bool:
    """本机的 DNS 解析结果能不能代表真实目标？

    走代理时不能：目的地由代理解析；而 fake-ip 模式（Clash 默认）
      把所有域名映到 `198.18.0.0/15` 这种占位段 ——
      于是地板会把每一个网站都拒掉（现场：`example.com -> 198.18.0.53`）。
只有在直连（或 `system` 档且环境里根本没有代理变量）时，
      解析结果才代表真实目标，那一段 IP 判定才有意义。

    不跳过的仍是：scheme · URL 内嵌凭据 · 单标签主机名 ·
      IP 字面量（它不经 DNS，代理也改不了它的含义）。
    """
    try:
        from core import net_proxy as _np
        code, url = _np.state()
    except Exception:                                          # noqa: BLE001
        return True
    if code == "manual":
        return not bool(url)
    if code == "system":
        import os
        keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy")
        return not any(os.environ.get(k) for k in keys)
    return True                                                # direct


def _egress_url_parts(url: str):
    """URL 那几条与"目标是不是公网"无关的判定，返回 `(原文, 主机名)`。

    `check_url_egress` 与 `check_mcp_endpoint` 共用这一处 —— 两份各写一遍，
    迟早一边收紧了另一边没动。
    """
    from urllib.parse import urlsplit

    raw = (url or "").strip()
    if not raw:
        raise UrlEgressError("URL 为空")

    try:
        parts = urlsplit(raw)
    except ValueError as e:
        raise UrlEgressError(f"URL 无法解析：{e}") from e

    scheme = (parts.scheme or "").lower()
    if scheme not in _EGRESS_SCHEMES:
        raise UrlEgressError(
            f"拒绝：只允许 http / https，收到 {scheme or '(空)'}。"
        )

    # URL 内嵌凭据 —— 已知实现都拒
    if parts.username or parts.password:
        raise UrlEgressError("拒绝：URL 里不允许内嵌用户名/密码。")

    host = (parts.hostname or "").strip()
    if not host:
        raise UrlEgressError("拒绝：URL 没有主机名。")
    return raw, host


def host_is_local(host: str) -> bool:
    """这个主机名是不是本机 / 局域网（回环 · 私网 · 链路本地 · 单标签名字）。

    与"公网"配对：`_is_public_ip` 说了什么算公网，这里只说"哪些不是"。
    解析不出来（DNS 失败）按不是本地处理 —— 交给地板去拒，
    而不是在这里吞掉一个拼错的名字。
    """
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h == "localhost":
        return True
    # 公网 DNS 没有单标签名字，单标签就是局域网名字（`mcpbox`）。
    if "." not in h:
        return True
    try:
        return not _is_public_ip(ipaddress.ip_address(h))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None)
    except OSError:
        return False
    addrs = set()
    for info in infos:
        try:
            addrs.add(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError, TypeError):
            continue
    return bool(addrs) and any(not _is_public_ip(ip) for ip in addrs)


def check_mcp_endpoint(url: str) -> str:
    """外部 MCP 的 endpoint 能不能连 —— 本机与 LAN 放行，其余走同一张地板。

    为什么与 `check_url_egress` 分开：MCP 的合法部署里本机与内网是常态
    （`http://127.0.0.1:3000/mcp`、`http://192.168.1.9:8080`），
    而那张地板是给"模型自己挑的网址"的，它拒一切非公网目标。
    共用的是同一条前置校验（scheme / 内嵌凭据 / 主机名），不是两套。

    重定向另有一条：公网 endpoint 跳到本机/内网要拒（见 `mcpx/client.py`
    的 `_redirect_guard`）—— 那是"跳板"的形状，用户批准的是原来那个地址。
    """
    raw, host = _egress_url_parts(url)
    if host_is_local(host):
        return raw
    return check_url_egress(raw)


def check_url_egress(url: str) -> str:
    """出网地板的唯一入口：这个 URL 能不能发出去。返回规范化后的 URL。

    每一跳都要调它（不只是第一跳）。`urllib` 默认
    跟随重定向 —— 一个只回 302 的跳板就能把内网内容取回来，
    只检查第一个 URL 等于把围栏装在旁边那扇门上。已知实现
    （`maxRedirects: 0`、`redirect: "manual"` + 每跳校验、自实现循环每跳校验）
    都是每跳重校验。

    挡住的是：非 http(s) scheme · URL 内嵌凭据 · 单标签主机名 ·
    解析到非公网地址的主机名。不挡公网域名 —— 那是审批那一层的事。
    """
    raw, host = _egress_url_parts(url)

    # 单标签主机名（`localhost` / `intranet` / `router`）——
    # 有的实现用 `hostname.split('.').length < 2`，顺带挡住 `localhost`；
    # 我们把它写成显式的规则，因为"它被挡住"应当是设计而不是巧合。
    # （`localhost` 恰好是单标签，但 `localhost.localdomain` 不是。）
    if "." not in host:
        raise UrlEgressError(
            f"拒绝：{host} 是单标签主机名（本地/内网名字），出网必须是公网域名。"
        )

    # 已经是 IP 字面量，直接判；否则解析后整组判
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # 走代理时这一判定没有意义（见 `_dns_is_authoritative` 的 docstring）。
        if _dns_is_authoritative():
            _resolve_public_addresses(host)
    else:
        if not _is_public_ip(ip):
            print(f"[egress] DENY {host} (literal non-public address)")
            raise UrlEgressError(
                f"拒绝：{host} 是非公网地址（内网 / 回环 / 保留段）。"
            )

    return raw


# 同一命令在短窗口内被记两次，出声（取证装置）
#: 判据：两处同类事件的形状逐字相同 —— `[audit]` 里连续两条一模一样的
#: 命令，界面上两次"需要你确认执行这条命令"。而归属未定：
#:   ① 模型重发（它收到"待审批"又调了一遍）② 框架重派（审批通过后重放）
#: —— 两者的修法完全不同。所以先出声、再定修法。
#
#: 这里只出声、绝不拦：拦错了会把正常的一次重试也挡掉，
#:   而目前还不知道该拦哪一种（不许静默；也不许猜着拦）。
#
#: 这张表是跨会话的（重复的是命令原文，跟哪个会话无关）
#: 它进 `core/state_registry.py` 的 `PROCESS_SHARED`，`/clear` 不清它
#:   （与 `_maps` 正相反：那是会话级的状态，这是进程级的史）。
_DUP_COMMAND_WINDOW_S = 30.0

#: `命令原文 -> (上一次的 monotonic, 累计次数)`。加锁 —— 六个通道
#: 各在自己的线程里跑 `run_command`（`state_registry` 那一行写着这条）。
_recent_commands: dict = {}
_dup_lock = threading.Lock()


def _unlock_bypassing() -> bool:
    """权限解禁（完全权限 + 开关）时受保护路径不再拦。见 `core/unlock.py`。"""
    try:
        from core import unlock
        return unlock.bypassing()
    except Exception:                                       # noqa: BLE001
        return False


def _note_touched(resolved_path: str) -> None:
    """把"这一轮碰过的文件"记进 `core/touched_files`，
    并（只在第一次）留一份改前内容给右侧栏 diff（`core/file_history`）。

    会话号只能从请求快照上取（`core/request_snapshot.current_session_id`）——
    `validate_path` 拿不到 `ctx`。这是那台机器存在的理由之一（同 `core/shell.py`）。
    取不到就记进 `""` 那一格，不许因此打断写入（同 `todos` 的判据）。
    永不抛 —— 它在每一次路径校验上（热路径），观测不许阻断功能。

    留档只在"本轮第一次碰它"时做（`note()` 的返回值就是那个判据）：
    同一个文件一轮内改两次的话，第二次留的"改前"已经是改过了的内容 ——
    存下去会把真正的改前覆盖掉，而症状是"diff 看起来只改了后半段"。
    留档与 `note()` 同一个钩子（写类 op，且只在 `validate_path` 放行之后）
    —— 与"不列写工具名单"同一条理由。
    """
    try:
        from core.request_snapshot import current_session_id
        from core.touched_files import WRITE_OPS, note   # noqa: F401 (文档锚)
        sid = current_session_id()
        first_time = note(sid, resolved_path)
        if first_time:
            from core.file_history import snapshot_before
            snapshot_before(sid, resolved_path, turn=_current_turn(sid))
    except Exception as exc:  # noqa: BLE001
        print("[touched] 接线失败（不影响写入）：%s: %s" % (type(exc).__name__, exc))


def _current_turn(channel_key: str) -> int:
    """这一轮是这条线程的第几轮（给文件留档盖的戳）。

    `SecurityGuard.validate_path` 拿不到 `ctx`，会话号只能从请求快照上取
    （`current_session_id`），而同一条线程"已完成几轮"只有存储面知道
过一道绑定表 —— 与 `core/history_tools._sid()` 走的是同一条路。

    拿不到，`0`：那表示"第一轮"，而留档本身照做
    （宁可把轮次记成 0，也不许因为记不出轮次就不留档 —— 那会把 diff 一起弄坏）。
    """
    try:
        from bus.registry import get_memory_manager
        mm = get_memory_manager()
        if mm is None or not channel_key:
            return 0
        sid = mm.short_term.bound_storage_id(channel_key)
        return mm.short_term.thread_turn_count(sid) if sid else 0
    except Exception:  # noqa: BLE001
        return 0


#: 写类 op 的本地副本 —— 它与 `core/touched_files.WRITE_OPS` 必须一致，
#: 而"一致"这件事由 `tests/test_touched_files.py` 两边对账钉着。
#: （这里不直接 import 的理由：`security.py` 是最底层，它 import 谁都要想一秒；
#:  而这一条是纯常量、只在那一行用一次。）
_WRITE_OPS_FOR_TOUCHED = frozenset({"write", "delete", "move", "rename"})


def _note_command_duplicate(command: str) -> None:
    """同一条命令在 `_DUP_COMMAND_WINDOW_S` 秒内又被执行了，打一行。

    永不抛 —— 它是观测，不是功能（同 `memory.saved` 的判据：观测不阻断）。
    """
    try:
        key = command or ""
        now = time.monotonic()
        with _dup_lock:
            prev, n = _recent_commands.get(key, (0.0, 0))
            # 窗口之外，重新起算（不是"累计次数" —— 那是另一个问题，
            # 而这里要回答的只有「紧接着又来一次」）
            n = n + 1 if (prev and now - prev <= _DUP_COMMAND_WINDOW_S) else 1
            _recent_commands[key] = (now, n)
            # 表不许无界长（同 `context_report` 的 `_MAX_SESSIONS` 那条判据）
            if len(_recent_commands) > 200:
                _recent_commands.pop(next(iter(_recent_commands)))
        if n == 2:
            print(
                "[w28] 同一条命令在 %.0f 秒内被记了第 %d 次 —— **归属未定**"
                "（模型重发 / 框架重派，修法不同）。取证：看它的 `tool_call_id` "
                "是不是同一个；命令：%s" % (_DUP_COMMAND_WINDOW_S, n, key[:120])
            )
    except Exception as exc:  # noqa: BLE001
        print("[w28] 重复命令探测自己出错（不影响执行）：%s: %s"
              % (type(exc).__name__, exc))


class SecurityGuard:
    """
    Validates file paths against a three-tier security model and commands
    against a blacklist. Logs all actions to an audit log.

    - PathLevel classification (FORBIDDEN / READ_ONLY / READ_WRITE)
    - Per-workspace fine-grained permissions (WorkspacePerm)
    - Cross-zone read-only access to user directories
    - Auto-approve mode (skip permission prompts)
    - Runtime workspace reload
    """

    # Credential sanitization patterns
    _SANITIZE_PATTERNS: list[tuple[str, str]] = [
        (r'(?:sk|ak|api[_-]?key)[-_](?:proj[_\-])?[a-zA-Z0-9]{20,}',
         "API_KEY"),
        (r'(?:ghp|gho|github[_\-]?pat)[_\-][a-zA-Z0-9]{20,}',
         "GITHUB_TOKEN"),
        (r'eyJ[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{10,}',
         "JWT"),
        (r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'
         r'.*?'
         r'-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
         "PEM_KEY"),
        (r'ssh-(?:rsa|ed25519|dss|ecdsa) AAAA[a-zA-Z0-9+/=]{80,}',
         "SSH_KEY"),
        (r'(?:bearer|token|secret|password|passwd|auth)\s*[:=]\s*["\'][^\s]{16,}["\']',
         "CREDENTIAL"),
    ]

    def __init__(
        self,
        allowed_dirs: list[str],
        command_blacklist: list[str],
        audit_log_path: str,
        *,
        cross_zone_enabled: bool = True,
        auto_approve: bool = False,
        forbidden_dirs: list[str] | None = None,
        user_home: str | None = None,
        framework_readable_dirs: list[str] | None = None,
        relax_command_tier: bool = False,
        no_ask_readonly: bool = False,
    ) -> None:
        # Allowed directories (Level 2: READ_WRITE)
        self._readwrite_dirs: list[Path] = [Path(d).resolve() for d in allowed_dirs]

        # Forbidden directories (Level 0)
        _forbidden = list(DEFAULT_FORBIDDEN_DIRS)
        if forbidden_dirs:
            _forbidden.extend(forbidden_dirs)
        self._forbidden_dirs: list[Path] = [
            Path(d).resolve() for d in _forbidden if Path(d).exists()
        ]
        # Also resolve non-existing forbidden dirs (user may add future paths)
        for d in _forbidden:
            p = Path(d)
            if not p.exists():
                try:
                    resolved = p.resolve()
                except Exception:
                    resolved = p
                self._forbidden_dirs.append(resolved)

        # 框架自己的敏感路径
        # 见 `DEFAULT_FORBIDDEN_PATHS` 的注释：兜底是 READ_ONLY 之后，
        # 这份清单就是唯一的保护，所以它必须在这条构造路径上被解析出来。
        # 相对路径按 cwd 解析（同 `DEFAULT_FRAMEWORK_READABLE_DIRS`）；
        # 不存在也照样收 —— 判决要在文件被创建之前就成立。
        self._forbidden_paths: list[Path] = []
        for _p in DEFAULT_FORBIDDEN_PATHS:
            try:
                self._forbidden_paths.append(Path(_p).resolve())
            except Exception:          # noqa: BLE001 — 解析不了就当字面量
                self._forbidden_paths.append(Path(_p))

        # Framework-authored, agent-readable dirs (Level 1 READ_ONLY)
        # 见 `DEFAULT_FRAMEWORK_READABLE_DIRS` 的注释。
        _fw = (DEFAULT_FRAMEWORK_READABLE_DIRS if framework_readable_dirs is None
               else framework_readable_dirs)
        self._framework_readable: list[Path] = [Path(d).resolve() for d in _fw]

        # User home for Level 1 READ_ONLY scope
        self._user_home: Path = Path(user_home or str(Path.home())).resolve()

        # Cross-zone settings
        self._cross_zone_enabled = cross_zone_enabled
        self._auto_approve = auto_approve
        # 危险档降级开关（「关闭安全询问」）
        # 读者：`core/shell.py::run_command` → `core/command_policy.classify_command(
        # ..., relax_guarded=)`。语义见那条命令的 `relax` 分支。
        self._relax_command_tier = bool(relax_command_tier)
        # 免问集开关（设置页「只读命令免问」）
        # 读者：`core/shell.py::run_command`。与上面那条是两件事：
        # 那条把危险档降级，这条把无需判定的那一类整个跳过询问。
        self._no_ask_readonly = bool(no_ask_readonly)

        # Command blacklist
        self._command_blacklist = set(cmd.lower() for cmd in command_blacklist)

        # Audit log
        self._audit_log_path = Path(audit_log_path)
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        # logger 名字必须带上路径。
        #
        # 如果所有实例共用一个固定名字 `"audit"`，再加上
        # `if not self._logger.handlers:` 这一句
        # 所以第一个 guard 装了处理器之后，后面所有 guard 都往第一个的文件里写
        # —— 第二个 guard 的 `audit_log_path` 被静默忽略。
        #
        # 这是"看着是实例状态，其实是进程全局"的一类形状：
        # 同一个名字的 logger 是解释器级全局对象，不是每个实例一份。
        _logger_name = "audit:" + str(self._audit_log_path)
        self._logger = logging.getLogger(_logger_name)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            #: 原来是 `FileHandler` —— 只追加、永不轮转，
            #:   而审计是每一条写/管理档工具调用与每一次审批的流水。
            #:   8 MB × 5 份 ≈ 40 MB 封顶，够查一次事故，不会把盘吃满。
            from logging.handlers import RotatingFileHandler
            handler = RotatingFileHandler(
                self._audit_log_path, maxBytes=8 * 1024 * 1024, backupCount=5,
                encoding="utf-8", delay=True,
            )
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            self._logger.addHandler(handler)

        # Per-workspace fine-grained permissions
        # key = resolved path str → set of action names
        self._workspace_perms: dict[str, set[str]] = {}
        for d in self._readwrite_dirs:
            self._workspace_perms[str(d)] = {
                "copy", "delete", "move", "rename", "write"
            }

        # Cross-zone notification callback
        self._cross_zone_notify: Callable[[str], None] | None = None

    # Public API

    # Settings

    @property
    def cross_zone_enabled(self) -> bool:
        return self._cross_zone_enabled

    @cross_zone_enabled.setter
    def cross_zone_enabled(self, value: bool) -> None:
        self._cross_zone_enabled = value

    @property
    def auto_approve(self) -> bool:
        return self._auto_approve

    @auto_approve.setter
    def auto_approve(self, value: bool) -> None:
        self._auto_approve = value

    @property
    def relax_command_tier(self) -> bool:
        """「关闭安全询问」开关。

        它做什么：把强制档（`guarded`：`git` / 提到受保护路径的命令）
        降级成普通档 —— 普通档仍然会问，只是问完之后可以「本次对话内
        不再询问此命令」。

        不是"不问"。判据：第一个问题必须还在 —— 否则一次点击就等于
        永久放行一整类危险操作，而那个点击是在"我再也不想看到这个弹窗了"
        的心态下做的。
        `deny` 档不受它影响 —— 那是确定性拒绝（出网类命令），
        连问都不问的东西，没有"关掉询问"可言。见 `core/command_policy.py`。
        它是进程级设置，不是会话级 —— 与 `auto_approve`/`cross_zone_enabled`
        同一族（设置按定义就是进程级的）。会话级的那些走
        `WorkspaceManager` 上那批投影字段，别混。
        """
        return self._relax_command_tier

    @relax_command_tier.setter
    def relax_command_tier(self, value: bool) -> None:
        self._relax_command_tier = bool(value)

    @property
    def no_ask_readonly(self) -> bool:
        """「只读命令免问」开关。

        它做什么：`core/command_policy.no_ask_reason()` 认出来的那一类
        （命令名白名单 + 无重定向 / 无管道 / 无取变量）不弹窗，直接执行。

        它与 `relax_command_tier` 不是同一件事，别混：
        · 那条把危险档（`git` / 提到受保护路径）降级 —— 降完仍然会问；
        · 这条只碰 `allow` 档，`guarded` 一个字不受影响（照旧走它自己的审批流程）。
        `deny` 档更不受影响 —— 那是确定性拒绝（出网类），
        连问都不问的东西，没有"免问"可言。判据同 `relax_command_tier` 那条。
        默认 `False`（照常问） —— 它放宽的是询问，
        属于安全轴，缺配置 / 配置坏了 / 键名写错，一律朝"问"倒。
        """
        return self._no_ask_readonly

    @no_ask_readonly.setter
    def no_ask_readonly(self, value: bool) -> None:
        self._no_ask_readonly = bool(value)

    @property
    def forbidden_dirs(self) -> list[str]:
        return [str(d) for d in self._forbidden_dirs]

    @forbidden_dirs.setter
    def forbidden_dirs(self, paths: list[str]) -> None:
        """整张表替换，不再把 `DEFAULT_FORBIDDEN_DIRS` 并回来。

        默认那批只是构造时的种子（见 `__init__`）。设置页把它交出去之后再并一遍，
        用户删掉的每一条都会在内存里原地复活 —— 界面上写着删了，判定时还在拦，
        那就是标签在说谎。
        """
        self._forbidden_dirs = []
        for d in paths:
            try:
                self._forbidden_dirs.append(Path(d).resolve())
            except Exception:
                self._forbidden_dirs.append(Path(d))

    def set_cross_zone_notify_callback(self, cb: Callable[[str], None] | None) -> None:
        """Set a callback invoked when cross-zone search is triggered."""
        self._cross_zone_notify = cb

    # Path classification

    def classify_path(self, file_path: str) -> tuple[Path, PathLevel]:
        """
        Classify *file_path* into FORBIDDEN / READ_ONLY / READ_WRITE.

        Returns (resolved_path, level).  Does NOT raise — the caller
        decides what to do with the classification.

        Level 0 (FORBIDDEN) is reserved for paths that are always blocked;
        a path that maps to FORBIDDEN will still be returned here so the
        caller can present a clear message, but any attempt to validate it
        will raise SecurityError.
        """
        try:
            resolved = Path(file_path).resolve()
        except Exception:
            resolved = Path(file_path)

        # 1. Check FORBIDDEN first
        if self._is_forbidden(resolved):
            # 权限解禁（完全权限 + 设置开关），受保护路径当普通路径处理。
            #   判据是上下文（`core/unlock.py::bypassing`，由
            #     `AgentLoopStage` 每轮钉一次），不是现场读设置 ——
            #     设置只决定"钉不钉得成"，两处各读一次会分叉。
            #   代价：`.env` 这类密钥读得到、系统目录也读得到。
            if _unlock_bypassing():
                print("[unlock] 受保护路径放行：%s" % resolved)
                return resolved, PathLevel.READ_WRITE
            return resolved, PathLevel.FORBIDDEN

        # 2. Check READ_WRITE (workspace)
        for ws_dir in self._readwrite_dirs:
            try:
                resolved.relative_to(ws_dir)
                return resolved, PathLevel.READ_WRITE
            except ValueError:
                continue

        # 2.3 计划模式：本会话自己那一份计划文件 → READ_WRITE
        #
        # 判据：
        # 不是"plans 目录"，是 `plans/<本会话的 slug>.md` ——
        #    目录级白名单会让 agent（或一次提示注入）往那儿写任意文件，
        #    而 `data/` 底下还躺着 `settings.json` / `memory/`。
        #
        # 判定收在 :meth:`_is_plan_artifact`（唯一解码处）——
        #    `validate_path` 的 Level 2 也要问同一件事，各写一次就会漂。
        # 放在 2.5 之前：兜底是 READ_ONLY，写会被拒；而计划文件必须可写。
        if self._is_plan_artifact(resolved):
            return resolved, PathLevel.READ_WRITE

        # 2.4 这一段对话自己选定的目录 → READ_WRITE
        #
        # 判据：「这一段对话选定的目录，免问」——
        # 用户已经用"选它"表过态，不该问第二遍。
        #
        # 放在 2.5/3 之前（与 2.3 同一条理由）：兜底是 READ_ONLY、写会被拒，
        #   而"这一段自己选的目录"必须可写 —— 否则这个功能一上线就是
        #   "选了区外目录，产物一个都落不下来"，比没有它更坏。
        # 第 1 条（FORBIDDEN）在它前面，所以 `.env` 一族即使在选定目录里
        #   也照样是 FORBIDDEN —— 顺序承重，不许重排。
        if self._is_session_workspace(resolved):
            return resolved, PathLevel.READ_WRITE


        # 2.5 框架自己的制品目录 → READ_ONLY
        #     放在 3/4 之前：否则第 4 条兜底会把 `data/` 判成 FORBIDDEN，
        #     而那正是"框架写了、agent 读不回来"的来源。
        for fw_dir in self._framework_readable:
            try:
                resolved.relative_to(fw_dir)
                return resolved, PathLevel.READ_ONLY
            except ValueError:
                continue

        # 3. 兜底 = READ_ONLY。
        #
        # 如果这一条是 `FORBIDDEN`（"在用户目录外，拒绝"），
        # 它的实际效果是把整个仓库树也拒了：本仓库在 `D:\work\…`、
        # 用户目录在 `C:\Users\…`，agent 连自己项目里的文件都读不到，
        # 而用户要读的数据表只要不在用户目录下，就跟着一起被拒。
        #
        # 判据：
        #   「只要这个文件不在规避目录里 —— 那他就应该可以被读到」
        # 判据不是"在用户目录里才可读"，是"不在规避清单里就可读"。
        # 于是判决的唯一依据移到 `_is_forbidden`：系统目录（`DEFAULT_FORBIDDEN_DIRS`
        # / `_FORBIDDEN_SUBSTRINGS`）+ 框架自己的敏感路径（`DEFAULT_FORBIDDEN_PATHS`）。
        #
        # 只放开了读：`READ_ONLY` 在 `validate_path` 里只放 `read`/`copy`，
        # 写仍然必须落在工作区。
        # "用户目录，READ_ONLY"与这一条结果相同，故合并；
        # `cross_zone_enabled` 仍然管别处（跨区搜索与审批），只是不再管分级。
        return resolved, PathLevel.READ_ONLY

    def _is_plan_artifact(self, resolved: Path) -> bool:
        """这份路径是不是本会话自己那一份计划文件。

        唯一的解码处（`core/plan.py::is_current_plan_file` 只在这里被判）——
        两个调用者（`classify_path` 的 2.3 与 `validate_path` 的 Level 2）
        问的是同一个问题，各写一次就会漂，而两个答案看着都对。

        为什么 `classify_path` 一处不够：
        `classify_path` 只答"能不能读/写"（分级）；而 Level 2 的写闸另有一道
        "在不在已注册工作区里" —— 计划文件是框架制品（`data/plans/`），
        两道门里它只过得了第一道。只加 2.3 的话 `write_file` 仍会报
        *"is not in any registered workspace"*，2.3 就成了空头支票。
        """
        try:
            from core.plan import is_current_plan_file
            return bool(is_current_plan_file(resolved))
        except Exception:
            return False

    def _write_outside_granted(self, resolved: Path, required_op: str) -> bool:
        """这一处区外写已经批过了吗。

        判据在 :func:`core.workspace.write_outside_granted`（唯一一处）——
          这里只做"这个 op 算不算写"这一层。

        判不了，`False`（fail-closed：照问一次，不静默放行）。
          方向是掐出来的：这条路走错的代价是多弹一次窗，
          反方向是围栏开了个洞。
        """
        if str(required_op) != "write":
            return False
        try:
            from core.workspace import write_outside_granted
            return bool(write_outside_granted(resolved))
        except Exception:                           # noqa: BLE001
            return False

    def _write_outside_may_ask(self, resolved: Path, required_op: str) -> bool:
        """这一处判不下来时，该转成一道审批还是照旧硬拒。

        只放 `write` 这一档 —— `delete` / `move` / `rename` 照旧硬拒。
          两条理由：
          ① 删除那条路上已经有自己的审批（`has_delete_approval` +
             批量硬闸 `delete_bulk`），把它接进来会让"删一个区外文件"
             走两套账，而两套账正是反复栽过的形状；
          ② 判据是最小的那一刀：先修「在区外写东西」这半条。
             其余 op 要放开是另一条决定。

        这里是策略、上面那个是事实 —— 分开是因为它们的读者不同：
          · 事实（批过没有）会被结算器和分类器读到；
          · 策略（哪些 op 该问）只属于这一个判定点。
        """
        return str(required_op) == "write"

    def _is_session_workspace(self, resolved: Path) -> bool:
        """这份路径是不是这一段对话自己选定的那个目录。

        唯一的解码处 —— 两个调用者（`classify_path` 的 2.4 与
        `validate_path` 的 Level 2）问的是同一个问题，各写一次就会漂
        （`_is_plan_artifact` 那条逐字同族）。

        为什么 `classify_path` 一处不够：与计划文件一模一样 ——
        `classify_path` 只答"能不能读/写"（分级），而 Level 2 的写闸另有一道
        "在不在已注册工作区里"。只加 2.4 的话，用户选了一个区外目录之后
        每一个产物都写不出来，报的是 *"is not in any registered workspace"*
        —— 那就是"外围栏装了、内围栏没装"，而症状是看起来装了。

        ## 这一条是审批那一格的实现，不是"目录=权限"的推论

        判据：「工作区外写，走审批」+「这一段对话选定的目录，免问」
        —— 用户已经用"选它"表过态，不该问第二遍。
        这里落的是后半句（免问）；前半句（其它区外的地方，问）见
        `core/approval.py` 的 `cross_zone_write` 那一路。

        `FORBIDDEN` 仍然赢 —— 本方法只在 `classify_path` 的
        第 1 条之后被调用（`_is_forbidden` 先跑），所以 `.env` 一族 /
        `data/settings.json` / `data/memory/` / `logs/` 即使躺在选定的目录里，
        照样硬拒。这一条有测试钉着（`test_session_workspace.py`）。

        它不动根集 —— `build_index` / `lookup_roots` / 跨区搜索
        一个字节都不变（"这一段往哪儿写"与"文件可能在哪儿"是两件事）。
        """
        try:
            from core import session_workspace
            d = session_workspace.current()
            if not d:
                return False
            # 存进去的就是 `normalize_dir()` 之后的值（`set_for` 那一处），
            #   所以这里是同一个口径的第二半 —— 不在这里再归一一次
            #   （再归一一次就是两处定义，而它们会随 `expanduser` 之类的细节漂）。
            resolved.relative_to(Path(d))
            return True
        except ValueError:
            return False
        except Exception:                       # noqa: BLE001 —— 判不了就当不是
            return False

    def validate_path(
        self, file_path: str, required_op: str = "read"
    ) -> Path:
        """
        Validate *file_path* for *required_op*.

        *required_op* is one of: read, copy, write, delete, move, rename.

        - Level 0 (FORBIDDEN): always raises SecurityError.
        - Level 1 (READ_ONLY): only 'read' and 'copy' are allowed.
        - Level 2 (READ_WRITE): checks per-workspace permission flags.

        Returns the resolved Path on success.
        """
        resolved, level = self.classify_path(file_path)

        if level == PathLevel.FORBIDDEN:
            self._audit("PATH_DENY", str(resolved))
            raise SecurityError(
                f"Access denied: '{file_path}' (resolved: '{resolved}') "
                f"is in a forbidden or inaccessible area."
            )

        if level == PathLevel.READ_ONLY:
            if required_op in ("read", "copy"):
                self._audit("PATH_ALLOW_RO", str(resolved))
                return resolved
            # 已经批过的区外写，放行。
            #
            # 判据在 :func:`core.workspace.write_outside_granted`（唯一一处）——
            # 它问的是"这一段对话批过这个目录没有"，与本方法上面的计划文件旁路
            # 同一族（都是"这一条路已经有人做过决定"）。
            # 放在 `FORBIDDEN` 之后（那一条在 `classify_path` 的第 1 条
            #    就返回了），`.env` 一族即使躺在被批准的目录里也照样硬拒。
            if self._write_outside_granted(resolved, required_op):
                self._audit("PATH_ALLOW_OUTSIDE", str(resolved))
                if required_op in _WRITE_OPS_FOR_TOUCHED:
                    _note_touched(str(resolved))
                return resolved
            # 还没批过，不是硬拒，是"该问一次" —— 抛一个专用异常，
            #    由派发层接住并变成一次审批。
            #    抛它的时候一个字节都还没写（见 `WriteNeedsApproval`）。
            if self._write_outside_may_ask(resolved, required_op):
                self._audit("PATH_DENY_ASK", str(resolved))
                from core.workspace import write_outside_subject
                raise WriteNeedsApproval(
                    f"Access denied: '{file_path}' is outside every workspace — "
                    f"writing there needs one approval.",
                    path=str(resolved), op=str(required_op),
                    subject=write_outside_subject(resolved),
                )
            self._audit("PATH_DENY_RO", str(resolved))
            raise SecurityError(
                f"Access denied: '{file_path}' is in cross-zone read-only area. "
                f"Operation '{required_op}' requires workspace (Level 2) access."
            )

        # Level 2 (READ_WRITE): check workspace permissions
        #
        # 本会话自己那一份计划文件走一条旁路 ——
        #    它是框架制品（`data/plans/`），不在任何工作区里，而下面这道
        #    "在不在已注册工作区里"是为用户文件设的门。
        #    这一支必须存在：`classify_path` 的 2.3 只把它判成 READ_WRITE，
        #    落到这里照样会被拒（「is not in any registered workspace」）——
        #    那就是"外围栏装了、内围栏没装"，而症状是看起来装了。
        if self._is_plan_artifact(resolved):
            self._audit("PATH_ALLOW", str(resolved))
            if required_op in _WRITE_OPS_FOR_TOUCHED:
                _note_touched(str(resolved))
            return resolved

        # 这一段对话自己选定的目录 —— 同一条旁路。
        #
        # 判据与 2.4 逐字相同（"用户选过了，不该问第二遍"），而这一支必须存在
        # 的理由也与计划文件那条逐字相同：`classify_path` 的 2.4 只把它判成
        # READ_WRITE，落到下面 `_find_workspace` 那道"在不在已注册工作区里"照样
        # 会被拒（`is not in any registered workspace`），只做 2.4 就是
        # 空头支票，而症状是看起来装了（`_is_plan_artifact` 的注释记着同一个坑）。
        if self._is_session_workspace(resolved):
            self._audit("PATH_ALLOW", str(resolved))
            if required_op in _WRITE_OPS_FOR_TOUCHED:
                _note_touched(str(resolved))
            return resolved

        ws_dir = self._find_workspace(resolved)
        if ws_dir is None:
            self._audit("PATH_DENY", str(resolved))
            raise SecurityError(
                f"Access denied: '{file_path}' is not in any registered workspace."
            )

        ws_perms = self._workspace_perms.get(str(ws_dir), set())
        if required_op not in ws_perms and required_op != "read":
            # "read" is always allowed in any workspace
            self._audit("PATH_DENY_PERM", str(resolved))
            raise SecurityError(
                f"Access denied: workspace '{ws_dir}' does not allow "
                f"'{required_op}' operation."
            )

        self._audit("PATH_ALLOW", str(resolved))
        # "本轮改动了哪些文件"的唯一收集点
        #
        # 为什么不列一张"写工具"名单：那是一张会烂的名单
        #   （漏登记一个新工具就会静默失去记录）。而这里是
        #   每一个碰路径的工具都要过的那道门，`required_op` 已经在区分读写
        # 所以写类的四个 op 就是"这是要改它"的判据，不用另立一套。
        #
        # 只在 Level 2 这一支记：上面那支（READ_ONLY）只放 read/copy，
        #   写根本过不去，在那儿记等于记了个没发生的事。
        # 只记验证通过的（写类 op）—— 拒掉的路径不该出现在"我改了什么"里
        #   （用户看到会以为真的改了）。
        if required_op in _WRITE_OPS_FOR_TOUCHED:
            _note_touched(str(resolved))
        return resolved

    def validate_path_readonly(self, file_path: str) -> Path:
        """Convenience: validate for read-only access."""
        return self.validate_path(file_path, "read")

    # Workspace management (runtime reload)

    def reload_workspaces(self, workspaces: list[dict]) -> None:
        """
        Reload workspace list and permissions at runtime.

        *workspaces*: list of dicts with keys:
          - path: str
          - permissions: list[str]  (copy, delete, move, rename, write)

        If *workspaces* is empty, the existing workspace list is left
        untouched (preserving the initial allowed_dirs from config).
        """
        if not workspaces:
            return  # keep the initial allowed_dirs from config
        self._readwrite_dirs = []
        self._workspace_perms = {}
        for ws in workspaces:
            try:
                resolved = Path(ws["path"]).resolve()
            except Exception:
                continue
            self._readwrite_dirs.append(resolved)
            perms = set(ws.get("permissions", ["copy", "delete", "move", "rename", "write"]))
            self._workspace_perms[str(resolved)] = perms

    def check_workspace_perm(self, resolved: Path, action: str) -> bool:
        """Check if *resolved* path's workspace allows *action*."""
        ws_dir = self._find_workspace(resolved)
        if ws_dir is None:
            return False
        return action in self._workspace_perms.get(str(ws_dir), set())

    # Command validation

    def validate_command(self, command: str) -> bool:
        """
        Return True if *command* does not contain any blacklisted substring
        (case-insensitive). Raises SecurityError otherwise.

        全仓零调用 —— 而且刻意不接：
        配置里那张表是 `["sudo", "rm -rf", "del /f", "format"]`，子串匹配，
        而 `format` 会误伤 `git log --format=%H` 和 `"...".format(x)`
        —— "枚举必然漏，而且还会误伤"，正是不做静态危险命令检测的理由。
        别为了"让它有读者"而接上它。
        """
        lower_cmd = command.lower()
        for banned in self._command_blacklist:
            if banned in lower_cmd:
                self._audit("CMD_DENY", command)
                raise SecurityError(
                    f"Command blocked: '{command}' matches blacklisted pattern '{banned}'"
                )
        self._audit("CMD_ALLOW", command)
        return True

    def record_command(self, command: str, outcome: str = "exec") -> None:
        """把一条真的执行了的命令写进审计日志。

        ## 为什么单独加这一个

        `core/shell.py::run_command` 是框架里唯一能执行任意命令的工具，
        两条写盘路径都不覆盖它：
          ① `core/agent_loop.py` 对 write/admin 类工具只 `print()` 一行
             （控制台输出在长跑进程里会滚掉）
          ② `run_command` 不走 `validate_path` —— bash 本来就不走
执行过的命令事后查不到。

        ## 只记，不判

        不做静态危险命令检测 —— 那套做法一半是纯提示，也有的压根没有。
        判定归 `core/command_policy.py`，这里只负责留痕。

        命令原文不截断：`run_command` 入口已经限了
        `_MAX_COMMAND_CHARS = 3000`，一行有上界。
        截断审计内容等于"被审计的不是被执行的那条"，那比不记还糟。
        """
        self._audit(f"CMD_{str(outcome).upper()}", command)
        _note_command_duplicate(command)


    def record_tool_call(self, tool_name: str, permission: str,
                         outcome: str, args_summary: str = "") -> None:
        """把一次工具调用写进审计日志。

        ## 为什么加这个

        `core/agent_loop.py` 对 write/admin 类工具只 `print()` 一行
        （两处：sync 与 stream，必须锁步），而 `logs/audit.log` 里可能
        一条工具调用记录都没有 —— 文件可能已有 188 KB，
        全是 `PATH_ALLOW`/`PATH_DENY`。
一个长跑进程里，「控制台那行」会滚掉；只有落盘的才算审计。

        ## 记的是结果，不是决定

        判据：审计的可用性 = 「能不能回答事后的问题」，
        而事后的问题几乎总是「结果是什么」。反例：把 52 次 `rm -rf` 逐条记了，
        但谁掏空了那个 venv 查不出来。
所以 `outcome` 是必填的：`ok` / `error:<异常类名>` / `denied`
        / `pending`（挂起等审批，这一次没有执行）。

        ## 拒绝掉的单独一个 action，不许混进 `TOOL_CALL`

        被拒绝的调用如果照样打 `ADMIN tool called: no_such_tool_at_all`，
        就是一条看着像证据的假记录。
        修法不是「不记」（被拒绝的恰恰最该记 —— 用户会问「为什么我的文件没改」），
        是换一个 action 名：`TOOL_DENIED`。

        ## 只记 `write` / `admin` 两档

        `read` 档是每次 `read_file` 都算一次的风暴量级，记它会淹掉真信号。
        这个取舍与 `record_command`「只记，不判」是同一条：留痕，不制造噪声。

        ### ⏳ 未做（如实记）

        更完整的做法是逐行哈希链（每行 `prevHash`/`hash`，外部 `state.json`
        记 `sequence`/`lastHash`），篡改可发现。那是改日志格式的独立一块，
        尚未实现 —— 它不改变本方法的存在理由。
        """
        # `pending` 单独一个 action —— 挂起等审批的没有执行，
        # 记成 `TOOL_CALL` 就是一条说"它跑过"的假记录；而它之后不会
        # 补一条真结果把这条改掉（被中和掉的那条永远停在 pending）。
        if outcome == "denied":
            action = "TOOL_DENIED"
        elif outcome == "pending":
            action = "TOOL_PENDING"
        else:
            action = "TOOL_CALL"
        target = f"{str(permission).upper()} {tool_name} -> {outcome}"
        if args_summary:
            target += f" | args={args_summary}"
        self._audit(action, target)

    def record_approval(self, kind: str, subject: str, key: str,
                        outcome: str, matched_rule: str = "",
                        rule_label: str = "matchedRule") -> None:
        """把一次审批的结算写进审计日志。

        ## 为什么要它 —— 一句话

        如果没有「文件安全拒绝」这类记录，遇到
        「界面说拒绝了、事还是做成了」这一类问题，就查不出因。
        措辞要准：不是"审批有 bug"，是"审批没有痕迹" ——
        后者更慢才发作。

        ## 它同时是那条判据的落点：「拒绝必须是原子的，并且要留痕」

        · 原子：拒绝了，那次调用确实没执行（工具在返回 `pending_*`
          那一刻什么都没做）；
        · 留痕：就是这一条。
        反面形态：界面说"拒绝了"，而事还是做成了。

        ## `matchedRule` 记的是什么

        它把"命中的是哪一条规则"落进审计；同一件事在
        `WorkspaceManager.matched_target_grant` 里回答（具体那一条 vs
        开关那个裸 `*`）。结算之后问它，记下这一刻生效的是哪一种：

          · `specific` —— 用户点名的那一条 / 刚批的这个对象
          · `wildcard` —— 设置页那个「全部放行」开关（裸 `*`）
          · `""`       —— 没有规则命中（拒绝 / 没批过）

        记的是结算之后那一刻的答案（"以后按哪一条走"），不是"刚才为什么问"
          —— 后者是"没有规则命中"，那是常量、记了没信息。
        `subject` 可能含路径/命令原文，与 `record_tool_call` 同一条规矩：
          调用方先打码再传（凭据不进审计）。
        """
        action = "APPROVAL_" + str(outcome or "unknown").upper()
        target = "kind=%s | key=%s | subject=%s" % (
            kind or "?", key or "-", subject or "-")
        #: 字段名由调用方给，因为"命中的是哪条规则"有两种，
        #: 而它们该去查两份不同的证据：
        #:   · `matchedRule`    —— 授权表（手动结算那条路）
        #:   · `classifierRule` —— 分类器（自动模式那条路）
        #: 合成一个字段就能把"该查哪一份"这个信息弄丢。
        target += " | %s=%s" % (str(rule_label or "matchedRule"),
                                matched_rule or "none")
        self._audit(action, target)

    def record_ice(self, action: str, detail: str) -> None:
        """把一次 `ICE` 信息侧的读数写进同一个审计日志。

        ## 为什么它必须落盘，不能只 `print`

        同 :meth:`record_tool_call` 那一段的理由（日志可能已有 188 KB 而
        工具调用记录 0 条，长跑进程里控制台那行滚掉了）。

        ICE 的读数尤其经不起滚 —— 它是「这一次到底有没有走句柄回路」的
        唯一证据：任务成功有两种截然不同的过程 ——

            ① 模型搬了句柄（读到的值换成 `【PII:N】` → 打进工具参数 → 还原成真值）
            ② 模型写了个脚本绕开（读源写目标，真值从头到尾没进过模型）

        两种的结果文件长得一模一样（都正确、都没有泄漏），
        只有 `detail` 里的计数分得开：①`工具参数 > 0`，② 整行不出现。
        反例：任务是"把值抄进另一个文件"时，模型可以写个 Python 脚本照做，
        输出逐字节全对 —— 而字符根本没进过它。所以判据不能只看产物。

        ## 只记计数

        `detail` 里只该有 `还原 N 处 · 工具参数 N 条 · 丢弃 N 处` 这种数 ——
        不记真值、也不记句柄下标对应的值。审计是给人看的，不是第二份数据。
        """
        self._audit(action, detail)

    # Output sanitizer

    @staticmethod
    def sanitize_output(text: str, auditor=None) -> str:
        """
        Scan *text* for credential-like patterns and replace matches
        with `[REDACTED:<type>]` placeholders.
        """
        import re
        result = text
        for pattern, label in SecurityGuard._SANITIZE_PATTERNS:
            compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
            if compiled.search(result) and auditor:
                auditor("OUTPUT_SANITIZE", label)
            result = compiled.sub(f"[REDACTED:{label}]", result)
        return result


    # 按参数名打码
    #
    # 与上面那几条模式并列，而不是替代它 —— 两者互补：
    #
    #       sanitize_output('{"password": "AUTHCODE"}')  ->  原样返回
    #       sanitize_output('password=AUTHCODE')         ->  原样返回
    #       sanitize_output('a1b2c3d4e5f6g7h8')          ->  原样返回
    #       sanitize_output('sk-proj-abcdef...')         ->  [REDACTED:API_KEY]
    #
    #   模式那一套只认得出有形状的凭据。而邮箱授权码没有形状 ——
    #   16 位小写字母，与任何普通 token 无法区分。
    # 但工具参数知道哪个键叫 `password` —— 那是比正则强得多的信息。

    #: 键名命中这些的值会被换掉。归一化见 `_normalise_key`。
    #:
    #: 这是黑名单 —— 覆盖不到"没见过的那种写法"。验证过 22 个真实工具参数里
    #:   会出现的名字（`X-Api-Key` · `apiSecret` · `Cookie` · `AccessKeyId` …），
    #:   按 `_normalise_key` 归一之后全部落在这张表或下面那条后缀规则上。
    #: 漏掉的那种形状不是靠再加一行解决的 —— 见 `agent_loop` 那段注释里
    #:   对"覆盖范围"的说明。
    SENSITIVE_ARG_KEYS: frozenset = frozenset({
        "password", "passwd", "pwd", "passphrase",
        "token", "access_token", "refresh_token", "auth_token", "session_token",
        "secret", "client_secret", "app_secret", "api_secret",
        "api_key", "apikey", "appkey", "access_key", "secret_key", "private_key",
        "app_key", "auth_header", "access_key_id", "secret_access_key",
        "auth", "authorization", "credential", "credentials",
        "cookie", "set_cookie", "bearer", "signature", "sig",
        "otp", "mfa_code", "pin",
        "授权码", "密码", "密钥",
    })

    #: 换上去的占位符。刻意说清是哪个键读的人才知道要补什么。
    REDACTED_ARG = "[REDACTED:CREDENTIAL]"

    @staticmethod
    def _normalise_key(key: str) -> str:
        """参数名归一：驼峰拆词、`-` 与空格都归一成 `_`、再转小写。

        `X-Api-Key` 得到 `x_api_key` · `apiSecret` 得到 `api_secret` ·
        `AccessKeyId` 得到 `access_key_id` · `XMLParser` 得到 `xml_parser`

        为什么不能只做 `lower()` + `replace("-", "_")`：那样 `apiSecret` 得到
          `apisecret`（没有下划线，名单里的 `api_secret` 认不出），而
          `X-Api-Key` 得到 `x_api_key`（多了前缀，名单里的 `api_key` 也认不出）
          —— 归一化本身反而制造了漏点。
        """
        s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip())
        s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
        return s.lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def is_sensitive_key(key: str) -> bool:
        """这个参数名是不是凭据类？

        三条判据，任一命中即算：
          ① 归一化后与名单全等；
          ② 名单里的名字是它的后缀（`x_api_key` 认 `api_key`）——
             只对 6 个字符以上的词做，短词（`pin` / `sig` / `otp`）做后缀
             会误伤 `spin` / `pilot` 这类普通参数名；
          ③ 以 `_password` 结尾（`smtp_password` / `db_password` 那种）。
        """
        if not isinstance(key, str):
            return False
        k = SecurityGuard._normalise_key(key)
        if k in SecurityGuard.SENSITIVE_ARG_KEYS:
            return True
        if k.endswith("_password"):
            return True
        return any(len(w) >= 6 and k.endswith("_" + w)
                   for w in SecurityGuard.SENSITIVE_ARG_KEYS)

    @staticmethod
    def redact_credential_args(value, _depth: int = 0):
        """把嵌套结构里键名命中凭据表的那些值换成占位符。

        非破坏性：返回新对象，调用方给进来的 dict / list 一个字节都不动
        —— 它们可能正是 `messages` 里那个共享对象（历史是共享的，
        原地改它等于改缓存前缀）。
        递归有深度上限：工具参数可能很深（也许还有环），不能无界递归。
        """
        if _depth > 6:
            return value
        if isinstance(value, dict):
            return {
                k: (SecurityGuard.REDACTED_ARG
                    if SecurityGuard.is_sensitive_key(k)
                    else SecurityGuard.redact_credential_args(v, _depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [SecurityGuard.redact_credential_args(v, _depth + 1)
                    for v in value]
        return value

    @staticmethod
    def redact_tool_call_arguments(raw):
        """`tool_calls[].function.arguments` 是一个 JSON 字符串，解 → 打码 → 编。

        解不出来就原样返回，不猜。历史里允许有坏 JSON
        （流式累积器会初始化成空串，Anthropic 也发空串）—— 把一段坏 JSON
        "修"成别的样子，比留着它更坏。
        """
        if not isinstance(raw, str) or not raw:
            return raw
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return raw
        if not isinstance(parsed, dict):
            return raw
        redacted = SecurityGuard.redact_credential_args(parsed)
        if redacted == parsed:
            return raw                      # 没命中就逐字节不动（前缀友好）
        return json.dumps(redacted, ensure_ascii=False)

    # Cross-zone notification

    def notify_cross_zone(self, filename_hint: str = "") -> str:
        """
        Trigger cross-zone notification. Returns the message string
        and calls the registered callback if any.
        """
        msg = (
            "工作区内没有您想要的文件，Crannia正在扩大搜索范围"
        )
        if filename_hint:
            msg += f"（目标：{filename_hint}）"
        if self._cross_zone_notify:
            try:
                self._cross_zone_notify(msg)
            except Exception:
                pass
        return msg

    # Internal helpers

    def _is_forbidden(self, resolved: Path) -> bool:
        """Check if *resolved* is under any forbidden directory."""
        resolved_str = str(resolved).replace("\\", "/").lower()
        # Check path substrings
        for sub in _FORBIDDEN_SUBSTRINGS:
            if sub.lower() in resolved_str:
                return True
        # `.env` 一族走文件名，不走上面那张子串表 ——
        #   子串匹配会把 `.environment` / `foo.env.bak` 一起打中（见常量处的注释）。
        _name = resolved.name.lower()
        if _name == _ENV_FILE_NAME or _name.startswith(_ENV_FILE_NAME + "."):
            return True
        # 框架自己的敏感路径 —— 兜底是 READ_ONLY 之后，
        #   这几条是"读不到"的唯一依据，所以它们在判决里的位置必须靠前且明确。
        for fp in self._forbidden_paths:
            try:
                resolved.relative_to(fp)
                return True
            except (ValueError, OSError):
                continue
        # Check forbidden dirs
        for forbidden in self._forbidden_dirs:
            try:
                resolved.relative_to(forbidden)
                return True
            except (ValueError, OSError):
                continue
        return False

    def _find_workspace(self, resolved: Path) -> Path | None:
        """Return the workspace directory that contains *resolved*, or None."""
        for ws_dir in self._readwrite_dirs:
            try:
                resolved.relative_to(ws_dir)
                return ws_dir
            except ValueError:
                continue
        return None

    def _audit(self, action: str, target: str) -> None:
        """Write a structured audit entry."""
        self._logger.info("%s | %s", action, target)
