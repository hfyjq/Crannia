---
name: netops
description: Structured, cross-platform network diagnostics — local interfaces, routing table, DNS records, port reachability, and HTTP/TLS probes with a single JSON envelope. Use when the user asks to inspect network configuration, diagnose connectivity, check DNS, test whether a port is open, or probe an HTTP endpoint. Read-only; does not replace run_command for arbitrary commands.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: net_ifaces, net_routes, net_dns, net_ports, net_http_probe
---

# 网络诊断（netops）

把日常排障里"同一个命令在不同机器输出不同"的那部分做成统一回执。
**只读诊断**，不改配置、不发写请求。

## 什么时候用

- 看网卡 / IP / 网关 / 速率 ⇒ `net_ifaces`
- 跨网段不通、怀疑路由写错 ⇒ `net_routes`（先看路由，再 tracert）
- 域名解析 / TTL / CNAME ⇒ `net_dns`
- 某主机端口通不通 ⇒ `net_ports`（给明确列表，不扫网段）
- HTTP 状态 / 重定向 / 证书 / 安全头 ⇒ `net_http_probe`

## 什么时候不用

- 用户要**跑任意命令**（ping 连续发包、traceroute 完整路径、tcpdump 抓包）⇒
  用 `run_command` 或 `computer`，本工具集只做结构化归一化
- 要**改配置 / 登录远端** ⇒ SSH skill（单独配置）或 run_command

## 回执

所有工具返回同一形状：

```json
{"success": true, "tool": "net_ports", "data": {...},
 "error": "", "duration_ms": 123, "truncated": false}
```

`error` 非空即失败；`truncated=true` 表示结果被截断（端口最多 64 个）。
每次调用自动进框架审计日志。

## 边界

- 完全权限解禁对这些工具没有额外意义——它们本来就只读
- `net_http_probe` 走 httpx（不做写请求），证书取不到时回执里给原因，不静默
- `net_dns` 在 dnspython 缺失时降级为系统解析（无 TTL），回执标 `degraded`
