# -*- coding: utf-8 -*-
"""出网能力目录 —— 「能力 × 目的地 × 授权」三轴模型的第一轴（）。

## 它是什么

一张中心表：**每条出网通路声明它是哪个通道、目的地是哪个参数、
替掉了哪些 deny 命令族、分类器能不能自动放行、要不要写审计。**

两个来源，一个查询口：

  · 框架工具 → `FRAMEWORK_CHANNELS`（本文件，静态）
  · 技能工具 → manifest 的 `egress:` 段（装载期由
    `bus/loaders/skill_loader` 调 :func:`declare` 登记，运行期只读）

## 为什么要有它

`deny` 档拒绝的是"无结构的静默通道"（`curl` / `ssh` / `nc` 这些命令）。
拒绝必须有出路，而出路是一组注册着的工具。没有这张表时，
"哪些命令有替身、替身叫什么、哪个参数是目的地"散落在四处
（`command_policy` 的拒绝话术、各 skill 的 manifest、审批的 subject、
审计的字段），改一处漏三处。

## deny 路线表

:data:`DENY_ROUTES` 把 `core/command_policy.EGRESS_COMMANDS` 的每个命令族
映射到一个结构化替身（或一个"不提供"的理由）。命令那一侧的拒绝话术
从它生成 —— 一处写。

判据（）：只承诺注册着的工具。所以路线里的每个工具名都必须能在
注册表里找到（`tests/test_egress_channels.py` 按注册表反查）。

## 声明字段（一个都不能少）

    channel 通道：http / ssh / dns / tcp
    target 哪个参数是目的地（"" = 固定后端，如 web_search）；
              审批、授权、审计都以它为单位
    route_for 它替代哪些 deny 命令族（没有就空）
    auto 分类器能不能自动放行（False = 永不被自动放行，必问）
    audit True = 每次调用写一行 EGRESS 审计

`auto=False` 的两条依据：敏感数据外发与远端执行
属最高风险，分类器不得代答。`ssh_exec` / `ssh_upload` 是第一批。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "EGRESS_FIELDS", "EgressChannel", "Route",
    "FRAMEWORK_CHANNELS", "FRAMEWORK_TOOLS",
    "DENY_ROUTES", "route_of_family", "deny_message",
    "declare", "forget_declared", "declared_channels",
    "channels", "lookup", "auto_allowed", "auto_allowed_for_call",
    "audit_enabled", "channel_of", "route_for_tool",
]

#: 一条声明必须给全的字段（读者逐项核）。
EGRESS_FIELDS: tuple[str, ...] = ("channel", "target", "route_for", "auto", "audit")


@dataclass(frozen=True, slots=True)
class EgressChannel:
    """一条出网通路的声明。字段含义见模块 docstring。"""
    tool: str
    channel: str
    target: str = ""
    route_for: tuple[str, ...] = ()
    auto: bool = True
    audit: bool = True
    source: str = ""
    #: 没有对应 deny 族时，为什么它仍然值得存在（读者 ④ 点名要它）。
    note: str = ""


# 框架自带的出网工具
#
# 只收"真的会发出网络请求"的那些工具。`file_parser` 的本地工具、
# `net_ifaces` / `net_routes`（只读本机状态，不出网）不在表里 ——
# 它们没有目的地，审批与审计以目的地为单位，没有目的话这两件事无从谈起。
FRAMEWORK_CHANNELS: dict[str, EgressChannel] = {
    # 取网页 / 下载二进制。GET 一族，走地板 + 域名闸。
    "web_fetch": EgressChannel(
        tool="web_fetch", channel="http", target="url",
        route_for=("curl", "wget", "wget2"), auto=True, audit=True,
        source="framework"),
    # 搜索：目的地固定是搜索后端，query 不是目的地（见 `§3.10` 作用域边界）。
    "web_search": EgressChannel(
        tool="web_search", channel="http", target="",
        route_for=(), auto=True, audit=True, source="framework",
        note="固定后端：模型能给的不是目的地（是 query），"
             "所以它替代不了任何 deny 族 —— 它就是搜索本身"),
    # 带自定义头的 HTTP 请求（P2）。纯只读 GET 可被分类器放行，
    # 带 body / 凭据 / 非 GET 的永远要问（`auto_allowed_for_call`）。
    "web_request": EgressChannel(
        tool="web_request", channel="http", target="url",
        route_for=("curl", "wget", "wget2"), auto=True, audit=True,
        source="framework"),
}

#: 框架自带的出网工具名（测试与环境探测用）。
FRAMEWORK_TOOLS: frozenset[str] = frozenset(FRAMEWORK_CHANNELS)


@dataclass(frozen=True, slots=True)
class Route:
    """一个 deny 命令族的结构化替身（或"不提供"的理由）。

    `tools` 非空 -> 提供替身，`hint` 给最小调用示例；
    `tools` 为空 -> 明说"不提供"，`reason` 说明为什么。
    """
    families: tuple[str, ...]
    tools: tuple[str, ...] = ()
    hint: str = ""
    reason: str = ""


# 路线表 —— 17 个命令族一个不漏（读者按 `EGRESS_COMMANDS` 反查）。
#
# 每条的判据：替身必须兑现得了（ 的"敢不敢承诺"）。做不到的那几族
# 明确写"不提供"并给理由 —— 沉默会让模型以为换个写法就能绕过去。
DENY_ROUTES: tuple[Route, ...] = (
    Route(
        families=("curl", "wget", "wget2"),
        tools=("web_fetch", "web_request"),
        hint=('取网页或下载文件：web_fetch(url="https://…")；'
              "要自定义请求头或用非 GET 方法：web_request(url=…, method=…, "
              'headers=…)。'),
    ),
    Route(
        families=("ssh", "scp", "sftp"),
        tools=("ssh_exec", "ssh_upload", "ssh_download"),
        hint=('远端执行 ssh_exec(host="…", command="…")；'
              '传文件 ssh_upload / ssh_download。'),
    ),
    Route(
        families=("nc", "netcat", "ncat", "socat"),
        tools=("net_ports", "ssh_probe", "ssh_forward"),
        hint='端口探活 net_ports(host="…", ports="22,80")；'
             'SSH 探活 ssh_probe(host="…")；'
             '隧道 ssh_forward(host="…", remote_host="…", remote_port=…)'
             '（本地转发，后台跑）。',
    ),
    Route(
        families=("rsync",),
        tools=("ssh_upload", "ssh_download"),
        hint="没有增量同步语义；逐个文件走 ssh_upload / ssh_download。",
    ),
    Route(
        families=("aria2c",),
        reason="多连接、分片、BT 下载不属于 agent 必需；单个文件用 web_fetch 下载。",
    ),
    Route(
        families=("ftp", "lftp", "cadaver"),
        reason="明文协议与 WebDAV 没有替身；传文件请用 ssh_upload / ssh_download（SFTP）。",
    ),
    Route(
        families=("telnet",),
        reason="明文交互没有替身；端口探活请用 net_ports。",
    ),
    Route(
        families=("rclone",),
        reason="云盘同步的凭据面太大；应作为 MCP 服务器或单独的 skill 接入。",
    ),
)

#: 族 → 路线（派生一次；读者查"每个族都有路线"）。
_ROUTE_BY_FAMILY: dict[str, Route] = {}
for _r in DENY_ROUTES:
    for _f in _r.families:
        _ROUTE_BY_FAMILY[_f] = _r
del _r, _f


def route_of_family(family: str) -> Optional[Route]:
    """这个命令族有没有路线；没有返回 `None`（读者会红）。"""
    return _ROUTE_BY_FAMILY.get(str(family or "").strip().lower())


_DENY_HEAD = (
    "`{name}` 是出网类命令，框架不执行它 —— 这一档是**确定性拒绝**（不问）："
    "出网地板只对框架自己的联网工具生效，放开这条命令等于把地板绕过去。"
)


def deny_message(name: str) -> str:
    """`deny` 档的拒绝话术 —— 按族取路线。不承诺没注册的工具（）。

    `name` 是命令名（小写）。路线表里没有它时退回一句给 `web_fetch` 的
    通用路线 —— 那是安全的那一侧（少给一条具体路线，不是多承诺一个工具）。
    读者钉着"路线表覆盖 `EGRESS_COMMANDS` 全集"，所以兜底不会在
    已知族上被走到。
    """
    n = str(name or "").strip().lower()
    head = _DENY_HEAD.format(name=n)
    route = _ROUTE_BY_FAMILY.get(n)
    if route is None:
        return head + '要做网络请求请用 web_fetch(url="https://…")。'
    if route.tools:
        return head + "要做同样的事请改用：" + route.hint
    return head + "这一类**没有替身**：" + route.reason


# 技能工具的声明（manifest 的 `egress:` 段，装载期登记）
#
# 运行期只读；重新装载同名工具时就地替换（与 `register_tool` 的幂等
# 同一条规矩：`/api/restart` 会在同一进程里再登记一遍）。
_LOCK = threading.Lock()
_DECLARED: dict[str, EgressChannel] = {}


def declare(tool: str, spec: dict, *, source: str = "") -> tuple[bool, str]:
    """登记一条技能工具的 egress 声明。返回 `(是否成功, 错误原因)`。

    缺字段 / 类型不对一律拒（出声，不猜默认值）—— 默认值会把
    "没声明"变成"声明成 auto=True"，而那是放行的一侧。
    """
    t = str(tool or "").strip()
    if not t:
        return False, "工具名为空"
    if not isinstance(spec, dict):
        return False, "egress 段不是对象"
    missing = [k for k in EGRESS_FIELDS if k not in spec]
    if missing:
        return False, "缺字段：" + "、".join(missing)
    ch = str(spec.get("channel") or "").strip().lower()
    if not ch:
        return False, "channel 不能为空"
    target = spec.get("target")
    if not isinstance(target, str):
        return False, "target 必须是字符串（固定后端写空串）"
    rf = spec.get("route_for") or ()
    if not isinstance(rf, (list, tuple)) or not all(isinstance(x, str) for x in rf):
        return False, "route_for 必须是字符串列表"
    auto = spec.get("auto")
    audit = spec.get("audit")
    if not isinstance(auto, bool):
        return False, "auto 必须是布尔"
    if not isinstance(audit, bool):
        return False, "audit 必须是布尔"
    with _LOCK:
        _DECLARED[t] = EgressChannel(
            tool=t, channel=ch, target=target,
            route_for=tuple(x.lower() for x in rf),
            auto=auto, audit=audit, source=source or "",
            note=str(spec.get("note") or ""))
    return True, ""


def forget_declared(*, source: str = "") -> None:
    """撤掉声明。`source` 非空只撤那一个技能的（重新装载时先撤后登）。"""
    with _LOCK:
        if not source:
            _DECLARED.clear()
            return
        for k in [k for k, v in _DECLARED.items() if v.source == source]:
            del _DECLARED[k]


def declared_channels() -> dict[str, EgressChannel]:
    """当前已登记的技能工具声明（快照）。"""
    with _LOCK:
        return dict(_DECLARED)


def channels() -> dict[str, EgressChannel]:
    """框架 + 技能，合并后的全表（同名以技能声明为准）。"""
    out = dict(FRAMEWORK_CHANNELS)
    out.update(declared_channels())
    return out


def lookup(tool: str) -> Optional[EgressChannel]:
    """一个工具的出网声明；不是出网工具返回 `None`。"""
    t = str(tool or "")
    ch = declared_channels().get(t)
    return ch if ch is not None else FRAMEWORK_CHANNELS.get(t)


def channel_of(tool: str) -> str:
    ch = lookup(tool)
    return ch.channel if ch is not None else ""


def auto_allowed(tool: str) -> bool:
    """分类器能不能自动放行这个工具。

    没声明 -> `True`（默认行为不变：不是出网工具的那些工具不受本表管辖）。
    声明了且 `auto=False` -> 永不被自动放行（`core/agent_loop` 消费）。
    """
    ch = lookup(tool)
    return True if ch is None else ch.auto


def auto_allowed_for_call(tool: str, args=None) -> bool:
    """按这一次的具体调用判分类器能不能代答。

    与 auto_allowed 的差别只有一条：web_request 的只读那一半。
    纯只读 GET（无 body、无凭据）可自动放行；带 body / 凭据 / 非 GET
    的一律要问 —— 一次 POST 的副作用分类器判不了。
    """
    if not auto_allowed(tool):
        return False
    if str(tool or "") == "web_request":
        a = args if isinstance(args, dict) else {}
        method = str(a.get("method") or "GET").strip().upper()
        if method not in ("GET", "HEAD"):
            return False
        if a.get("body"):
            return False
        if a.get("credential"):
            return False
    return True


def audit_enabled(tool: str) -> bool:
    ch = lookup(tool)
    return False if ch is None else ch.audit


def route_for_tool(tool: str) -> tuple[str, ...]:
    """这个工具替代了哪些 deny 命令族（没有就空）。"""
    ch = lookup(tool)
    return ch.route_for if ch is not None else ()
