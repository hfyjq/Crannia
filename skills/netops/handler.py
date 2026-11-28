# -*- coding: utf-8 -*-
"""网络诊断脚手架 —— 把命令输出归一化成 LLM 能稳定消费的 JSON。

为什么不是直接跑命令：同一个 ping/traceroute/dig，Windows 与 Linux 的命令名、
参数、输出格式都不同。模型手搓会得到三种结果。这里吃掉平台差异，
输出统一 envelope；裸命令仍可走 run_command，本工具集是增量而非替代。

依赖：psutil/httpx 已在 requirements；DNS 完整查询用 dnspython（ISC）。
只做只读诊断。写操作（SSH/配置）不在这里。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

__all__ = ["init", "net_ifaces", "net_routes", "net_dns", "net_ports",
           "net_http_probe"]

DEFAULT_TIMEOUT = 3.0
MAX_PORTS = 64
MAX_BODY = 32 * 1024


#: 由 `bus` 在装载时注入。
_guard = None


def init(security_guard=None) -> None:
    global _guard
    _guard = security_guard


def _audit(channel: str, target: str, action: str, verdict: str = "") -> None:
    """写一行 EGRESS 审计（诊断类也要留痕 —— 决定 7：只审计、不弹窗）。"""
    if _guard is None:
        return
    try:
        _guard.record_egress(channel, target, action, "auto", verdict)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[netops] audit write failed ({type(exc).__name__}: {exc})")


def _ok(tool: str, data: dict, started: float, **extra) -> dict:
    out = {"success": True, "tool": tool, "data": data, "error": "",
           "duration_ms": int((time.monotonic() - started) * 1000)}
    out.update(extra)
    return out


def _err(tool: str, msg: str, started: float, **extra) -> dict:
    out = {"success": False, "tool": tool, "data": {}, "error": msg,
           "duration_ms": int((time.monotonic() - started) * 1000)}
    out.update(extra)
    return out


def _addr(nic: Any) -> str:
    return str(getattr(nic, "address", "") or "")


# 工具

def net_ifaces() -> dict:
    """网卡配置 —— Windows ipconfig / Linux ip addr 的统一形状。"""
    t0 = time.monotonic()
    try:
        import psutil
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        out = []
        for name, nics in addrs.items():
            v4 = [_addr(n) for n in nics
                  if getattr(n, "family", None) == socket.AF_INET]
            v6 = [_addr(n) for n in nics
                  if getattr(n, "family", None) == socket.AF_INET6]
            mac = next((_addr(n) for n in nics
                        if getattr(n, "family", None) == getattr(psutil, "AF_LINK", -1)
                        or "MAC" in str(getattr(n, "family", ""))), "")
            masks = [str(getattr(n, "netmask", "") or "")
                     for n in nics if getattr(n, "family", None) == socket.AF_INET]
            st = stats.get(name)
            out.append({
                "name": name, "ipv4": v4, "ipv6": v6,
                "netmask": masks[0] if masks else "", "mac": mac,
                "is_up": bool(getattr(st, "isup", False)),
                "speed_mbps": int(getattr(st, "speed", 0) or 0),
                "mtu": int(getattr(st, "mtu", 0) or 0),
            })
        return _ok("net_ifaces", {"interfaces": out}, t0)
    except Exception as exc:                        # noqa: BLE001
        return _err("net_ifaces", "%s: %s" % (type(exc).__name__, exc), t0)


def net_routes() -> dict:
    """路由表 —— psutil 没有路由 API，只能按平台解析系统命令/文件。

    路由是跨网段排障的关键（tracert 断在第一跳之外时，多半是路由写错），
    所以值得胶一层，而不是让模型分别记 route print / ip route。
    """
    t0 = time.monotonic()
    try:
        if sys.platform.startswith("linux"):
            rows = _linux_routes()
        elif sys.platform == "win32":
            rows = _windows_routes()
        else:
            rows = _bsd_routes()
        default = next((r for r in rows if r["dest"] in ("0.0.0.0", "::/0")), None)
        return _ok("net_routes", {
            "routes": rows,
            "default_gateway": default["next_hop"] if default else "",
        }, t0)
    except Exception as exc:                        # noqa: BLE001
        return _err("net_routes", "%s: %s" % (type(exc).__name__, exc), t0)


def _hex_ip(s: str) -> str:
    try:
        b = bytes.fromhex(s.strip())
        if len(b) == 4:
            return str(ipaddress.IPv4Address(b))
    except Exception:                               # noqa: BLE001
        pass
    return s


def _linux_routes() -> list[dict]:
    # /proc/net/route: iface dest mask gw flags refcnt use metric ...（hex 小端 IP）
    rows = []
    for line in open("/proc/net/route", encoding="utf-8", errors="replace").read().splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        dest_hex, mask_hex, gw_hex = f[1], f[2], f[3]
        try:
            mask_int = int(mask_hex, 16)
            prefix = mask_int.bit_count()
        except Exception:                           # noqa: BLE001
            prefix = 0
        rows.append({
            "dest": "%s/%d" % (_hex_ip(dest_hex), prefix),
            "next_hop": _hex_ip(gw_hex), "iface": f[0], "metric": int(f[6] or 0),
        })
    return rows


def _windows_routes() -> list[dict]:
    out = subprocess.run(
        ["route", "print", "0.0.0.0"], capture_output=True, text=True,
        timeout=DEFAULT_TIMEOUT, errors="replace")
    rows = []
    for line in out.stdout.splitlines():
        f = line.split()
        # Windows 表：目标 掩码 网关 接口 跃点；只认以 IP 开头的行
        if len(f) >= 5 and re.match(r"^\d+\.\d+\.\d+\.\d+", f[0]):
            rows.append({"dest": f[0], "next_hop": f[2],
                         "iface": f[3], "metric": int(f[4] or 0)})
    return rows


def _bsd_routes() -> list[dict]:
    out = subprocess.run(["netstat", "-rn", "-f", "inet"],
                         capture_output=True, text=True,
                         timeout=DEFAULT_TIMEOUT, errors="replace")
    rows = []
    for line in out.stdout.splitlines():
        f = line.split()
        if len(f) >= 2 and re.match(r"^\d+\.\d+\.\d+\.\d+", f[0]):
            rows.append({"dest": f[0], "next_hop": f[1],
                         "iface": f[-1], "metric": 0})
    return rows


def net_dns(name: str, record_type: str = "A", timeout_s: float = 3.0) -> dict:
    """DNS 查询 —— dig/nslookup 输出不一致，这里给统一 answer/TTL。

    dnspython 缺失时降级到标准库（只给地址，无 TTL/类型链），并在回执里说清。
    """
    t0 = time.monotonic()
    host = str(name or "").strip()
    rtype = str(record_type or "A").strip().upper()
    if not host:
        return _err("net_dns", "name 不能为空", t0)
    _audit("dns", host, "resolve")
    try:
        import dns.resolver
        r = dns.resolver.Resolver()
        r.timeout = float(timeout_s)
        r.lifetime = float(timeout_s)
        answers = []
        try:
            resp = r.resolve(host, rtype)
            ttl = int(getattr(resp.rrset, "ttl", 0) or 0)
            answers = [{"type": rtype, "value": str(x), "ttl": ttl} for x in resp]
            canonical = str(getattr(resp, "canonical_name", "") or host)
        except dns.resolver.NXDOMAIN:
            return _ok("net_dns", {"name": host, "type": rtype,
                                   "answers": [], "status": "NXDOMAIN"}, t0)
        except dns.resolver.NoAnswer:
            canonical = host
        return _ok("net_dns", {
            "name": host, "type": rtype, "answers": answers,
            "canonical": canonical, "resolver": str(r.nameservers[0]),
            "status": "OK" if answers else "NO_ANSWER",
        }, t0)
    except ImportError:
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC)
            vals = sorted({x[4][0] for x in infos})
            return _ok("net_dns", {
                "name": host, "type": rtype,
                "answers": [{"type": "ADDR", "value": v, "ttl": 0} for v in vals],
                "resolver": "system", "status": "OK",
                "degraded": "dnspython 未装：无 TTL/记录类型，只有解析地址",
            }, t0)
        except Exception as exc:                    # noqa: BLE001
            return _err("net_dns", "解析失败：%s" % exc, t0)
    except Exception as exc:                        # noqa: BLE001
        return _err("net_dns", "%s: %s" % (type(exc).__name__, exc), t0)


def net_ports(host: str, ports: Any = None, timeout_s: float = 1.5,
              max_workers: int = 16) -> dict:
    """给定端口列表的连通性 —— 并发 + 超时 + 统一回执。

    只探测用户给的端口，不做网段扫描；nc 被 deny 档拦，正好由这里提供正规路。
    """
    t0 = time.monotonic()
    target = str(host or "").strip()
    if not target:
        return _err("net_ports", "host 不能为空", t0)
    if isinstance(ports, str):
        parts = re.split(r"[,\s]+", ports.strip())
    else:
        parts = [str(p) for p in (ports or [])]
    nums = []
    for p in parts:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 65535 and n not in nums:
            nums.append(n)
    if not nums:
        return _err("net_ports", "没有有效端口（1–65535）", t0)
    _audit("tcp", target, "ports:%d" % len(nums))
    if len(nums) > MAX_PORTS:
        nums = nums[:MAX_PORTS]
        truncated = True
    else:
        truncated = False

    def probe(port: int) -> dict:
        try:
            with socket.create_connection((target, port), timeout=float(timeout_s)):
                return {"port": port, "open": True, "service": ""}
        except OSError:
            return {"port": port, "open": False, "service": ""}

    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(32, int(max_workers)))) as ex:
        futs = {ex.submit(probe, p): p for p in nums}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:                       # noqa: BLE001
                results.append({"port": futs[fut], "open": False, "service": ""})
    results.sort(key=lambda r: r["port"])
    return _ok("net_ports", {"host": target, "results": results}, t0,
               truncated=truncated)


def _cert_fields(der: bytes) -> dict:
    """一张 DER 证书的三个字段（cryptography 解；解不开返回空表）。"""
    try:
        from cryptography import x509
        c = x509.load_der_x509_certificate(der)
        return {
            "subject": c.subject.rfc4514_string(),
            "issuer": c.issuer.rfc4514_string(),
            "not_before": c.not_valid_before_utc.isoformat(),
            "not_after": c.not_valid_after_utc.isoformat(),
        }
    except Exception:                                   # noqa: BLE001
        return {}


def _tls_report(host: str, port: int, timeout: float) -> dict:
    """TLS 明细：版本 / 叶子证书 / 链条 / 校验失败原因。

    请求本身仍然 verify=True（这里不提供关闭开关）。
    校验失败时另开一条不校验的诊断连接取链，并在回执里标注来源。
    """
    import ssl

    out: dict = {}

    def _chain_of(sock) -> list:
        try:
            certs = sock.get_verified_chain() or []
        except Exception:                               # noqa: BLE001
            return []
        rows = []
        for c in certs:
            try:
                der = c.public_bytes(ssl.ENCODING_DER)
            except Exception:                           # noqa: BLE001
                continue
            row = _cert_fields(der)
            if row:
                rows.append(row)
        return rows

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as sock:
                cert = sock.getpeercert() or {}
                out.update({
                    "version": sock.version() or "",
                    "issuer": dict(x[0] for x in cert.get("issuer", ())),
                    "not_after": cert.get("notAfter", ""),
                    "not_before": cert.get("notBefore", ""),
                    "subject_alt_names": [v for _, v in
                                          cert.get("subjectAltName", ())],
                })
                chain = _chain_of(sock)
                if chain:
                    out["chain"] = chain
                else:
                    out["chain_incomplete"] = True
        return out
    except ssl.SSLCertVerificationError as exc:
        out["verify_error"] = {
            "code": int(getattr(exc, "verify_code", 0) or 0),
            "message": str(getattr(exc, "verify_message", "") or exc),
        }
    except Exception as exc:                            # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out

    # 校验失败了：另开一条不校验的连接取链 —— 只为诊断展示，请求仍是 verify=True。
    try:
        ctx2 = ssl._create_unverified_context()
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with ctx2.wrap_socket(raw, server_hostname=host) as sock:
                chain = _chain_of(sock)
                if chain:
                    out["chain"] = chain
                    out["chain_note"] = "证书链取自一次不校验的诊断连接（仅用于展示）"
    except Exception:                                   # noqa: BLE001
        pass
    return out


def _proxy_report() -> dict:
    """这一跳实际走的代理档（地址打码）。

    mode 是设置里的三档（system / direct / manual）；effective 是这一刻
    真正走的东西（打码后的地址，或 direct）。
    """
    try:
        from core import net_proxy as _np
        d = _np.describe()
        eff = str(d.get("effective") or "")
        return {"mode": d.get("mode") or "", "effective": bool(eff and eff != "direct"),
                "effective_url": eff, "source": d.get("source") or "",
                "hint": d.get("hint") or ""}
    except Exception as exc:                            # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def net_http_probe(url: str, timeout_s: float = 8.0) -> dict:
    """HTTP 探针 —— 状态码/重定向链/TLS 有效期/安全头。

    比 curl 包一层好在：输出结构化、异常统一、跨平台一致；curl 在 deny 档。
    """
    t0 = time.monotonic()
    u = str(url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return _err("net_http_probe", "url 必须以 http:// 或 https:// 开头", t0)
    from core.egress_gate import host_of
    _audit("http", host_of(u) or u, "probe")
    try:
        import httpx
        # 代理跟随设置页那一档（全仓造客户端的地方都经过 core/net_proxy）。
        from core import net_proxy as _np
        with _np.httpx_client(httpx.Client, url=u, timeout=float(timeout_s),
                              follow_redirects=True, verify=True) as client:
            resp = client.get(u)
        chain = [{"status": h.status_code, "url": str(h.headers.get("location", ""))}
                 for h in resp.history]
        sec_headers = {k: v for k, v in resp.headers.items()
                       if k.lower() in ("content-security-policy",
                                        "strict-transport-security",
                                        "x-content-type-options",
                                        "x-frame-options", "referrer-policy")}
        tls: dict = {}
        if u.lower().startswith("https://"):
            tls = _tls_report(httpx.URL(u).host, 443, float(timeout_s))
        return _ok("net_http_probe", {
            "url": u, "status": resp.status_code,
            "final_url": str(resp.url), "redirects": chain,
            "http_version": resp.http_version,
            "security_headers": sec_headers, "tls": tls,
            "proxy": _proxy_report(),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "body_preview": resp.text[:512] if len(resp.text) <= MAX_BODY else "",
        }, t0)
    except Exception as exc:                        # noqa: BLE001
        return _err("net_http_probe", "%s: %s" % (type(exc).__name__, exc), t0)
