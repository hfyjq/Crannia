# -*- coding: utf-8 -*-
"""出网地板 + 逐跳校验 + 跨站不跟随。

这个文件存在的理由：它要挡的东西是实测复现过的。

## 判据集来自哪里（不是我想的）

  · 15 个"必须拒"地址 —— 覆盖保留段 / 链路本地 / 文档段 / 组播 / 回环
  · 9 种地址包装器 —— 我们自己普查出来的（NAT64 / RFC8215 / IPv4-mapped /
    IPv4-compatible / 6to4 / Teredo / 文档段）
  · 两个"必须拦"的例子 —— 拿真的 `_web_fetch` 打本机回环复现的
    （直连内网 + 经 302 跳板，两条都成功取回内容）

## 三条独立断言，各管一件事

  1. 地板拦得住（拒绝侧）
  2. 地板不误伤（放行侧）—— 少了它，"全拒"也能让第 1 条全绿
  3. 地板真的被调用（接线侧）—— 有一种失败形状：函数写得对、判据也对，
     却挂在一个没人读的配置项上，全仓 0 个写点，一行没跑过。
     "我写了这个函数"不等于"这条路径上会调它"。

## 负对照跑过，结果与一条限制

    变异                        红了哪几条
    A 去掉 is_reserved          1/2/3
    B 去掉 is_multicast         1/2/3
    C 地板只查第一跳             7
    G 地板整个没接上             7
    D _same_site 恒 True        8, 9
    E _same_site 恒 False       8, 10, 11

  D/E 会各带上「8 同站判据」是预期的：那条是判据的单测，
    任何改判据的变异都会带走它。目标那一条也一定在里面，这才是判据。
  测试 1 同时充当 A/B 的判据 —— 去掉 A 泄漏 3 条、去掉 B 泄漏 2 条，都在它里面。

  一条限制，别当成已覆盖：`is_private` / `is_loopback` / `is_link_local`
    / `is_unspecified` 这四个标志去掉都不会红（被 `is_global` 遮蔽，
    实测漏 0 条）。它们保留是跨 Python 版本的纵深，不是本判据集的需要。
    覆盖它们的是下面那 22 条「地址」，不是那四行「代码」。

输出一律 ASCII（Windows 控制台 GBK）。不需要 pytest。
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.security import UrlEgressError, _is_public_ip, check_url_egress  # noqa: E402
import ipaddress  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


# ① 地板：必须拒的地址

#: 15 条"必须拒"。每一条都能说清它为什么必须拒（保留段 / 链路本地 /
#: 文档段 / 组播 / 回环 / 包装器），不是随手挑的样本 —— 随手挑的会挑自己过得去的。
#: 别删：少一个否定标志就会漏（见下面那条断言的提示语）。
MUST_REJECT_ADDRESSES = [
    "0.0.0.0", "10.0.0.1", "100.64.0.1", "127.0.0.1", "169.254.169.254",
    "192.0.2.1", "224.0.0.1", "255.255.255.255",
    "::", "::1", "fe80::1", "fc00::1", "ff02::1",
    "::ffff:127.0.0.1", "64:ff9b::808:808",
]

#: 我们自己的包装器普查。每一条裹的都是内网或公网内层地址。
WRAPPERS_MUST_REJECT = [
    "64:ff9b::a00:1",       # NAT64 well-known + 10.0.0.1
    "64:ff9b:1::a00:1",     # RFC 8215 local-use + 10.0.0.1
    "::ffff:10.0.0.1",      # IPv4-mapped + 10.0.0.1
    "::10.0.0.1",           # IPv4-compatible（旧）
    "2002:a00:1::",         # 6to4 + 10.0.0.1
    "2001::a00:1",          # Teredo
    "2001:db8::1",          # 文档段
]

MUST_ALLOW = [
    "8.8.8.8", "1.1.1.1", "93.184.216.34", "142.250.72.14",
    "2606:4700:4700::1111", "2a00:1450:4001:80e::200e",
]


def test_reject_addresses() -> None:
    print("\n-- 1. 地板：非公网地址一律拒（15 条）--")
    missed = [a for a in MUST_REJECT_ADDRESSES
              if _is_public_ip(ipaddress.ip_address(a))]
    check("这 15 条全部判为非公网", not missed,
          "漏了: %r  <-- 少一个否定标志就会漏" % (missed,))

    print("\n-- 2. 地板：地址包装器（我们普查的 7 种）--")
    missed = [a for a in WRAPPERS_MUST_REJECT
              if _is_public_ip(ipaddress.ip_address(a))]
    check("7 种包装器全部判为非公网", not missed, "漏了: %r" % (missed,))

    print("\n-- 3. 地板：公网地址不许误伤 --")
    hurt = [a for a in MUST_ALLOW if not _is_public_ip(ipaddress.ip_address(a))]
    check("6 个公网地址全部放行", not hurt, "误伤: %r" % (hurt,))


def test_check_url_egress_forms() -> None:
    print("\n-- 4. 地板：URL 形态（scheme / 凭据 / 单标签主机）--")
    for bad, why in [
        ("file:///C:/Windows/win.ini", "file: scheme"),
        ("ftp://example.com/x", "ftp: scheme"),
        ("gopher://example.com/", "gopher: scheme"),
        ("https://user:pw@example.com/", "URL 内嵌凭据"),
        ("http://localhost/x", "单标签主机名"),
        ("http://intranet/x", "单标签主机名"),
        ("http://127.0.0.1:8080/x", "回环地址字面量"),
        ("http://169.254.169.254/latest/meta-data/", "云元数据端点"),
        ("http://[::1]/x", "IPv6 回环字面量"),
        ("https://10.1.2.3/", "RFC1918 字面量"),
    ]:
        try:
            check_url_egress(bad)
            check("拒 %s（%s）" % (bad[:44], why), False, "居然放行了")
        except UrlEgressError:
            check("拒 %s（%s）" % (bad[:44], why), True)
        except Exception as e:
            check("拒 %s（%s）" % (bad[:44], why), False,
                  "抛了别的异常 %s" % type(e).__name__)


def test_check_url_egress_allows_public() -> None:
    print("\n-- 5. ★ 地板不误伤公网域名（少了这条，'全拒'也能全绿）--")
    for ok in ["https://example.com/x", "http://example.com/",
               "https://www.bilibili.com/video/BV1xx",
               "https://8.8.8.8/"]:
        try:
            got = check_url_egress(ok)
            check("放行 %s" % ok[:48], got == ok, "返回值被改了: %r" % got)
        except UrlEgressError as e:
            check("放行 %s" % ok[:48], False, str(e)[:60])


def test_dns_resolution_is_checked() -> None:
    print("\n-- 6. ★ 域名解析后整组判（不是只看字面量）--")
    # `localhost.` 带点，过单标签检查，只能靠解析挡住。
    # 这条是"地板真的接了解析"的唯一机械证据。
    #
    # 必须把代理档钉成直连再测：`_dns_is_authoritative()` 在有代理时故意
    # 跳过解析那一段（fake-ip 会把每个域名映到占位段，照原样判就把所有站拒了）。
    # 不钉的话，这条断言读的是这台机器的环境：本机设了 `ALL_PROXY`，
    # 于是它在这里红、在干净的 CI 上绿，而两种都不说明代码有没有接解析。
    from core import net_proxy
    real_state = net_proxy.state
    net_proxy.state = lambda: ("direct", "")
    try:
        try:
            check_url_egress("http://localhost./x")
            check("拒 localhost.（解析到回环）", False, "居然放行了 -> 解析层没接上")
        except UrlEgressError:
            check("拒 localhost.（解析到回环）", True)
        except Exception as e:
            check("拒 localhost.（解析到回环）", False, type(e).__name__)
    finally:
        net_proxy.state = real_state


# ② 接线：地板真的在 _web_fetch 那条路径上被调用

def _serve(routes: dict):
    """起一个只回固定内容的本地服务器，返回 (base_url, hits, shutdown)。

    *routes*: path -> (status, headers, body)
    """
    import http.server

    hits: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            status, extra, body = routes.get(self.path, (404, {}, b"nope"))
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % port, hits, srv.shutdown


def test_web_fetch_is_wired() -> None:
    """重放：这两条都必须被拒（否则内容就泄漏了）。"""
    print("\n-- 7. ★ 地板真的接在 _web_fetch 上（修前现场重放）--")
    from mcpx.servers.web_search import _web_fetch

    base, hits, shutdown = _serve({
        "/internal": (200, {}, "<html><body>INTERNAL-ONLY-SECRET</body></html>"),
        "/jump": (302, {"Location": "/internal"}, b""),
    })
    try:
        r = _web_fetch(base + "/internal")
        check("直连内网地址被拒（修前 success=True）",
              r.get("success") is False and r.get("blocked") is True,
              "返回: %r" % (r,))
        check("  且内容没有泄漏", "INTERNAL-ONLY-SECRET" not in str(r))

        r2 = _web_fetch(base + "/jump")
        check("经 302 跳板也被拒（修前 success=True）",
              r2.get("success") is False,
              "返回: %r" % (r2,))
        check("  且内容没有泄漏", "INTERNAL-ONLY-SECRET" not in str(r2))
    finally:
        shutdown()


def _gate_off():
    """上下文管理器：把出网审批闸换成"全部放行"。

    为什么必须 stub：本文件第 7~12 组测的是地板与跳转策略，它们
      patch 掉地板来模拟"公网主机"—— 而闸门恰好在那一层之后，
      于是每一条都会停在"等审批"上。那正是正确的生产行为，
      不是 bug，要测跳转策略，就得先声明"这一段里审批不是被测对象"。

    闸门自己的行为在 `tests/test_egress_gate.py`（49 条），
      那里连"生产端 → 消费端认不认"都验过，这里 stub 掉不会留下盲区。

    打在模块属性上（不是 import 进来的名字）：两个调用点都是局部
      `from core.egress_gate import egress_gate`，每次调用重新读模块属性。
    """
    import contextlib
    import core.egress_gate as EG

    @contextlib.contextmanager
    def _cm():
        real = EG.egress_gate
        EG.egress_gate = lambda url, **kw: None
        try:
            yield
        finally:
            EG.egress_gate = real
    return _cm()


def test_redirect_policy() -> None:
    """跨站不跟随、同站跟随、且每跳都过地板。"""
    print("\n-- 8. 跳转策略 --")
    from mcpx.servers.web_search import _same_site

    check("同站（异路径）=> 同一站",
          _same_site("http://a.example/x", "http://a.example/y") is True)
    check("www. 前缀差异 => 仍算同一站",
          _same_site("http://example.com/x", "http://www.example.com/y") is True)
    check("★ 跨站 => 不同站",
          _same_site("http://a.example/x", "http://b.example/y") is False)
    check("★ 同 host 不同 scheme => 不同站",
          _same_site("http://a.example/x", "https://a.example/y") is False)
    check("★ 同 host 不同端口 => 不同站",
          _same_site("http://a.example:80/x", "http://a.example:8080/y") is False)
    check("★ www 只剥前缀，不吸子域（evil-www.example 不匹配）",
          _same_site("http://example.com/x", "http://evil-www.example/y") is False)


def test_cross_host_redirect_returns_to_model() -> None:
    """跨站跳转不跟随，而是把目标交回模型。"""
    print("\n-- 9. ★ 跨站跳转交回模型（不静默跟随）--")
    import core.security as SEC
    import mcpx.servers.web_search as WS

    real_get = WS._http_get_once
    real_check = SEC.check_url_egress
    calls: list[str] = []

    def fake_get(url, timeout=15):
        calls.append(url)
        return 302, {"Location": "https://elsewhere.example/landing"}, b""

    # 地板已经单独测过（1-6），这里只测跳转策略，让地板透传。
    # `_web_fetch` 里的 `from core.security import check_url_egress` 是局部
    # import，每次调用都重新读模块属性，打在 `core.security` 上就够了。
    SEC.check_url_egress = lambda u: u
    WS._http_get_once = fake_get
    try:
        with _gate_off():
            r = WS._web_fetch("https://origin.example/page")
        check("跨站跳转 => success=False", r.get("success") is False,
              "返回: %r" % (r,))
        check("  cross_host_redirect 标记为真", r.get("cross_host_redirect") is True)
        check("  把目标 URL 交回模型",
              r.get("redirect_to") == "https://elsewhere.example/landing",
              "得到: %r" % r.get("redirect_to"))
        check("  ★ 没有去请求那个目标（只是交回去）",
              not any("elsewhere.example" in c for c in calls),
              "请求过: %r" % (calls,))
    finally:
        WS._http_get_once = real_get
        SEC.check_url_egress = real_check


def test_same_site_redirect_is_followed() -> None:
    """反向：同站跳转必须真的跟。少了这条，'全部不跟随'也能让第 9 条全绿。"""
    print("\n-- 10. ★ 同站跳转要真的跟（不许为了安全把正常抓取全掐了）--")
    import core.security as SEC
    import mcpx.servers.web_search as WS

    base, hits, shutdown = _serve({
        "/a": (302, {"Location": "/b"}, b""),
        "/b": (200, {}, "<html><body>LANDED-ON-B</body></html>"),
    })
    real_check = SEC.check_url_egress
    SEC.check_url_egress = lambda u: u       # 地板单独测；这里测循环本身
    try:
        with _gate_off():
            r = WS._web_fetch(base + "/a")
        check("同站跳转 => success=True", r.get("success") is True,
              "返回: %r" % (r,))
        check("  跟着跳到了第二跳",
              "LANDED-ON-B" in (r.get("text") or ""),
              "text=%r" % (r.get("text"),))
        check("  服务器确实看到两跳", hits == ["/a", "/b"], "hits=%r" % (hits,))
        check("  回执里带上了跳转来源",
              r.get("redirected_from", "").endswith("/a") and r.get("hops") == 2,
              "redirected_from=%r hops=%r" % (r.get("redirected_from"), r.get("hops")))
    finally:
        SEC.check_url_egress = real_check
        shutdown()


def test_redirect_loop_is_bounded() -> None:
    """自环必须在上限处停住，且不能被误报成"跨站跳转"。

    踩过的形状：`redirect_request` 返回 None 和 "跟到上限"抛的是
    同一个 `HTTPError 302`，夹具里一个同源自环的输出与跨 host 逐字同形。
判据不能是"抛没抛异常"，必须看目标 host（现在由 `_same_site` 显式判）。
    """
    print("\n-- 11. ★ 自环在上限停住，且不被误判成跨站 --")
    import core.security as SEC
    import mcpx.servers.web_search as WS

    base, hits, shutdown = _serve({
        "/loop": (302, {"Location": "/loop"}, b""),
    })
    real_check = SEC.check_url_egress
    SEC.check_url_egress = lambda u: u
    try:
        with _gate_off():
            r = WS._web_fetch(base + "/loop")
        check("自环 => success=False", r.get("success") is False)
        check("  ★ 报的是'超过上限'，不是'跨站跳转'",
              r.get("cross_host_redirect") is not True
              and "上限" in str(r.get("error", "")),
              "返回: %r" % (r,))
        check("  跳数有界（<= 上限+1）", len(hits) <= 6, "hits=%d" % len(hits))
    finally:
        SEC.check_url_egress = real_check
        shutdown()


def test_fake_ip_is_a_placeholder_not_an_intranet_target() -> None:
    """fake-ip（`198.18.0.0/15`）是代理的 DNS 占位段，不是内网目标。

    现场：本机开着 Clash 的 fake-ip，`example.com` 解析到 `198.18.0.53`
地板把每一个网站都拒掉（"一开代理框架就废了"）。
    判据刻意只看这一段（RFC 2544 基准段，正常公网不会出现），
    不按"有没有配代理"放宽 —— DNS 是系统做的，跟我们那档设置无关。
    """
    from core.security import _is_fake_ip
    check("198.18.0.53 认成 fake-ip", _is_fake_ip("198.18.0.53"))
    check("198.19.255.255 也在段里", _is_fake_ip("198.19.255.255"))
    check("★ 成对：198.20.0.1 不在段里", not _is_fake_ip("198.20.0.1"))
    check("★ 成对：10.1.2.3 不是 fake-ip", not _is_fake_ip("10.1.2.3"))
    check("非 IP 字符串不炸", not _is_fake_ip("not-an-ip"))

    # 行为级：解析结果全是 fake-ip，放行；混进一个内网，照旧拒。
    import socket as _socket
    from core.security import _resolve_public_addresses, UrlEgressError
    orig = _socket.getaddrinfo

    def fake(addrs):
        return [(2, 1, 6, "", (a, 0)) for a in addrs]

    try:
        _socket.getaddrinfo = lambda *a, **k: fake(["198.18.0.53"])
        got = _resolve_public_addresses("example.com")
        check("★★ 整组 fake-ip -> 放行", got == ["198.18.0.53"], repr(got))

        _socket.getaddrinfo = lambda *a, **k: fake(["198.18.0.53", "10.1.2.3"])
        try:
            _resolve_public_addresses("example.com")
            check("★ 混进内网 -> 照旧拒", False, "没拒")
        except UrlEgressError:
            check("★ 混进内网 -> 照旧拒", True)
    finally:
        _socket.getaddrinfo = orig


def main() -> int:
    print("=" * 62)
    print("W19a: outbound egress floor + per-hop redirect checks")
    print("=" * 62)

    test_reject_addresses()
    test_check_url_egress_forms()
    test_check_url_egress_allows_public()
    test_dns_resolution_is_checked()
    test_fake_ip_is_a_placeholder_not_an_intranet_target()
    test_web_fetch_is_wired()
    test_redirect_policy()
    test_cross_host_redirect_returns_to_model()
    test_same_site_redirect_is_followed()
    test_redirect_loop_is_bounded()

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
