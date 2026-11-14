"""
Built-in web_search MCP server.

Provides web search capability without requiring an external API key.
Primary backend: Parallel MCP (`search.parallel.ai/mcp`, key optional).
Fallback: `ddgs` (DuckDuckGo), used when the primary is unavailable.

Tools exposed:
  - web_search(query, max_results=5) — search the web
  - web_fetch(url) — fetch and extract text from a URL

Registers directly to bus/registry via register_tools().
"""

from __future__ import annotations

import json
import re
import html as html_mod
from typing import Any


def register_tools() -> int:
    """
    Register web_search and web_fetch tools to bus/registry.

    Returns the number of tools registered (2).
    """
    from bus.registry import register_tool
    from mcpx.servers import owner_of

    #: 给内置 MCP 一个自己的归属。
    #: 不传 `owner` 就会落到默认的 `framework`，
    #: ① 「MCP 管理」按归属停用它做不到（`unregister_owner` 明确拒摘 `framework`）
    #: ② 它与框架自己的工具在这一格上分不开。
    #: 这样一改，"外来登记不许顶掉"那条保护就更依赖装载顺序了 ——
    #:   判据是先注册的赢（`register_tool` 只在 `owner == FRAMEWORK_OWNER` 时放行覆盖）。
    #:   本仓库的装载顺序因此必须是 MCP 在前、技能在后
    #:   （`bus/core.py` 里那一段跟着挪了位置，注释在那儿）。
    _OWNER = owner_of("web_search")

    register_tool(
        "web_search",
        _web_search,
        {
            "description": (
                "[READ] Search the web and return structured results "
                "(title, URL, snippet). Use when you need current information "
                "or facts beyond your training data. Free, no API key required."
            ),
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query in natural language",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Maximum number of results (1-10, default 5)",
                    },
                },
                "required": ["query"],
            },
        },
        owner=_OWNER,
    )

    register_tool(
        "web_fetch",
        _web_fetch,
        {
            "description": (
                "[READ] Fetch the full text content of a web page by URL. "
                "Use after web_search to read a specific result in detail. "
                "Returns plain text (HTML stripped). Max 8000 chars."
            ),
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Web page URL to fetch (must start with http:// or https://)",
                    },
                },
                "required": ["url"],
            },
        },
        owner=_OWNER,
    )

    return 2


# web_search handler
#
# 回执必须把三种结局分开，不能只回 `success: true` / `success: false`：
#
#   只数 `len(results)` 时，**「后端不可达」与「搜到了但结果全是垃圾」都落在
#   `success: true` 那一支** —— 例如回 5 条
#   `aquadvorik.ru` / `tarif17.ru` 的 SEO 垃圾页，而 `count: 5` 看着像成功。
#模型只能自己看出不对才说"网络检索返回的结果不可用"，框架一个字都没提醒它。
#   而且它永远 `ok=True`：`tool.end` 的 `ok` 判的是"handler 抛没抛"
#      （`core/agent_loop.py::_tool_error`），本函数从不抛，事件日志/审计里绿不出来。
#
# 「失败要如实报」的要点三条：
#     ① 成功 / 「跑成了但没结果」/ 「没跑成」是三种东西，不能塌成一个 ——
#        "没跑成"必须能与"跑成了但没找到"区分。
#     ② 永久性失败要单说出来，否则模型会拿同一个查询原样转圈。
#     ③ 不做「相关性检查」：能做的只有结构性的那几件（按 url 去重、
#        丢无 snippet 项、域名黑白名单），而"相关"本身判不了 ——
#        要判就得自己发明一套判据。「垃圾结果」的解法是换一个能给对结果的源，
#        不是加一个猜的闸。
#
# 后端用 `Parallel MCP`（`search.parallel.ai/mcp`，key 可选）。
#   它回的带正文摘录，质量比 `ddgs` 稳定；`ddgs` 作为第二后端保留
#   （能连 DuckDuckGo 的网络仍可用它）。
#   必须走 `curl_cffi`，不能裸 `urllib`：裸 urllib 会撞
#      Cloudflare 1010「blocked access based on your browser's signature」、
#      或超时 —— 与 `web_fetch` 撞的是同一面墙，
#      本仓库已经有那把钥匙（`_http_get_once` 那张表）。
#   如实标：这是一个第三方服务（查询会发出去），免费额度可能变。

#: Parallel 的 MCP 端点。它是 JSON-RPC over HTTP（不是 REST）。
_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
#: 单次请求超时。正常 1~5 秒；45 秒还没回基本就是被拦/挂了。
_PARALLEL_TIMEOUT = 45
#: 每条摘录最多留多少字符。 Parallel 给的是正文摘录（动辄几千字），
#: 不裁的话一次搜索就能把上下文吃掉一大块。`ddgs` 那条路裁 300，这里给宽一点
#: —— 摘录是这个后端的价值所在（别把有用的东西裁成没用的）。
_SNIPPET_CAP = 800
#: 不同后端在回执里的署名（给模型看的，也是排查时的抓手）。
_BACKENDS = ("parallel", "ddgs")


def _permanent_block(status: int, text: str) -> bool:
    """这次失败是不是原样重试也不可能成功的那一类？

    永久性失败要单独分类并交给模型（明确告诉模型"在条件改变之前重试
    不可能成功"）—— 不分的话，模型会拿同一个查询原样转圈，
    而每一次都要等一个超时。

    今天入这一类的只有 Cloudflare 的指纹拦截：裸 `urllib` 撞过
    `Error 1010: The site owner has blocked access based on your browser's signature`
    —— 换个查询、重试几次都一样（要用 `curl_cffi` 换指纹才是解法，不是重试）。
    """
    if status in (1010, 403):
        return True
    return "1010" in (text or "")[:400]


def _http_json(url: str, payload: dict, *, headers: dict,
               timeout: int = _PARALLEL_TIMEOUT):
    """POST 一段 JSON-RPC，返回 `(ok, data_or_reason, permanent)`。

    必须走 `curl_cffi`（理由见文件头那张表）—— 裸 `urllib` 会撞 Cloudflare 1010。
    """
    body = json.dumps(payload).encode("utf-8")
    h = dict(headers)
    h["Content-Type"] = "application/json"
    # 代理走一处口径（`core/net_proxy`）：`None` = 什么都不传（跟随环境变量，
    #    = 今天的行为）；`direct` 档给的是显式"不用代理"的写法。
    from core import net_proxy as _np
    #: 两张表：curl_cffi 不认 `{"http": None}`，
    #:   它会把"空值"解析成"不下发 CURLOPT_PROXY"，照旧读 `ALL_PROXY`。
    #:`direct` 档必须给 `{"all": ""}`（见 `core/net_proxy.curl_proxies`）。
    _curl_proxies = _np.curl_proxies()
    if _have_curl_cffi():
        from curl_cffi import requests as _cr
        try:
            kw = {"proxies": _curl_proxies} if _curl_proxies is not None else {}
            r = _cr.post(url, data=body, headers=h, impersonate="chrome",
                         timeout=timeout, **kw)
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e), False
        status, text = r.status_code, r.text
    else:
        # 退回 urllib 会更容易被 Cloudflare 1010 拦 —— 这条路是兜底，不是等价物。
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, data=body, headers=h, method="POST")
        #: urllib 不认 `ALL_PROXY` 的 socks5，而它认得 `HTTPS_PROXY` ——
        #:   两条路（curl_cffi / urllib）读到的代理可以不一样，别当成同一个。
        #:   `None`（跟随环境变量），默认 opener；否则显式给一张表
        #:   （`direct` 那张表是空的 = 不走代理；`manual` 那张表是那一个地址）。
        _proxies = _np.requests_proxies()
        _opener = (urllib.request.build_opener()
                   if _proxies is None else
                   urllib.request.build_opener(urllib.request.ProxyHandler(
                       {k: v for k, v in _proxies.items() if v})))
        try:
            with _opener.open(req, timeout=timeout) as resp:
                status, text = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            text = (e.read() or b"").decode("utf-8", "replace")
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e), False

    if status != 200:
        return (False,
                "HTTP %s: %s" % (status, text[:200].replace("\n", " ")),
                _permanent_block(status, text))
    return True, _parse_sse_json(text), False


def _parse_sse_json(raw: str):
    """MCP 的响应可能是裸 JSON，也可能是 SSE（`data: {...}`）——两种都认。"""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                continue
    return None


def _search_parallel(query: str, max_results: int):
    """后端①：Parallel MCP。返回 `(status, results, detail, permanent)`。

    `status` ∈ `{"ok", "empty", "failed"}` —— **「跑成了但没结果」与
    「没跑成」是两件事**（核心判据，出处见文件头）。

    只要一次 HTTP 往返：`tools/call` 不需要先
    `initialize`、也不需要 `mcp-session-id` 头**（`http=200, 3.2s`）——
    "Cloudflare 1010" 是裸 urllib 的指纹被拦，与协议无关。
别再加 `initialize`：那是两次往返换一个用不上的 session。
    """
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "web_search",
                   "arguments": {"objective": query, "search_queries": [query]}},
    }
    ok, data, perm = _http_json(_PARALLEL_MCP_URL, call, headers={})
    if not ok:
        return "failed", [], "请求失败：" + str(data), perm

    res = (data or {}).get("result") or {}
    blocks = res.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    if res.get("isError"):
        #: 服务端的报错是结构化且带重试指引的（「Missing required argument:
        #:   search_queries. ... Retry: keep your objective and add search_queries.」）
        #:原样带给模型，别吞掉。
        return "failed", [], "服务端报错：" + text[:300], False

    try:
        payload = json.loads(text)
    except ValueError:
        return "failed", [], "返回不是 JSON：" + text[:200], False

    out = []
    for it in (payload.get("results") or [])[:max_results]:
        if not isinstance(it, dict):
            continue
        ex = it.get("excerpts") or []
        snippet = " ".join(str(x) for x in ex) if isinstance(ex, list) else str(ex)
        out.append({
            "title": str(it.get("title") or "")[:200],
            "url": str(it.get("url") or ""),
            "snippet": re.sub(r"\s+", " ", snippet).strip()[:_SNIPPET_CAP],
        })
    if not out:
        return "empty", [], "返回 0 条", False
    return "ok", out, "", False


def _search_ddgs(query: str, max_results: int):
    """后端②：`ddgs`（第二后端）。保留 —— 能连 DuckDuckGo 的网络仍然好使。

    本机连不上 DuckDuckGo，`ddgs` 会退到别的引擎并返回
    与查询无关的垃圾，这里只按"跑成没跑成"分类，不判相关性。
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return "failed", [], "未装 `ddgs`（pip install ddgs）", True
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return "failed", [], "%s: %s" % (type(e).__name__, e), False
    if not results:
        return "empty", [], "返回 0 条", False
    out = []
    for r in results:
        out.append({
            "title": str(r.get("title", ""))[:200],
            "url": r.get("href", r.get("link", "")),
            "snippet": (r.get("body", r.get("snippet", "")) or "")[:_SNIPPET_CAP],
        })
    return "ok", out, "", False


def _web_search(query: str, max_results: int = 5) -> dict:
    """联网搜索。两条后端依次试，并把三种结局分开说（回执的全部要点）。

    | 结局 | 回什么 |
    |---|---|
    | 有结果 | `success: true` + `results` + `backend` |
    | 跑成了但一条没有 | `success: true, count: 0` + `No results found.` |
    | 一条都没跑成 | `success: false` + `error` + `hint` + `tried`（逐后端说清为什么） |

    第三行的 `tried` 是刻意的：只回一句"搜索失败"的话，模型没法判断该不该重试
    （"永久性失败"单列出来就是为了这个）。
    """
    query = (query or "").strip()
    if not query:
        return {"success": False, "error": "Search query cannot be empty."}

    max_results = min(max(max_results, 1), 10)

    tried = []
    answered_empty = None
    any_permanent = False
    for name in _BACKENDS:
        fn = _search_parallel if name == "parallel" else _search_ddgs
        try:
            status, results, detail, perm = fn(query, max_results)
        except Exception as e:  # noqa: BLE001
            status, results, detail, perm = "failed", [], "%s: %s" % (type(e).__name__, e), False
        if status == "ok":
            return {"success": True, "count": len(results), "query": query,
                    "backend": name, "results": results}
        if status == "empty" and answered_empty is None:
            answered_empty = name
        any_permanent = any_permanent or bool(perm)
        tried.append("%s: %s" % (name, detail or status))

    if answered_empty is not None:
        #: 「跑成了但没结果」不是失败 —— 判据：
        #:   只有真失败才走 `WebError`，空结果是正常返回的一句 `No results found.`。
        return {"success": True, "count": 0, "query": query,
                "backend": answered_empty, "results": [],
                "message": "No results found for this query."}

    hint = ("这**不是**「搜到了 0 条」，而是**一条后端都没跑通** —— "
            "别把「没找到」当成「不存在」。逐条原因见 `tried`。")
    if any_permanent:
        #: 永久性失败要明确标出"别重试" —— 否则模型会拿同一个查询转圈，
        #:   而每一次都要等一个超时。只有 `curl_cffi` 在场才是解法。
        hint += ("★ 其中还有**换查询也没用**的那一类（被按浏览器指纹拦）—— "
                 "**别原样重试**：要么改用 `web_fetch` 直接打开你已知的网址，"
                 "要么如实告诉用户这条查不到。")
    else:
        hint += "可以换一个更宽的查询再试一次，或改用 `web_fetch` 直接打开你已知的网址。"

    return {
        "success": False,
        "query": query,
        "error": "没有一条搜索后端跑成。",
        "hint": hint,
        "tried": tried,
    }


# web_fetch handler

# HTTP 传输层
#
# 两件事一起做：换栈（指纹）+ 逐跳校验（地板）。改的是同一条函数。
#
# 为什么必须换栈（量出来的，不是推断）：
#
#   目标             urllib / requests      curl_cffi(impersonate=chrome)
#   www.baidu.com    0/5 稳定失败           3/3
#   www.zhihu.com    0/5 稳定失败           3/3
#   www.gov.cn       0/5 稳定失败           3/3
#   www.bilibili.com 5/5                    3/3
#   example.com      5/5                    3/3
#
#   逐条排除过，全部不是原因：
#     · 网络不通      —— 裸 TCP 通、裸 TLS 握手成功（baidu 129ms / TLSv1.2）
#     · TLS 版本套件  —— zhihu/gov.cn 与 bilibili 协商结果逐字相同
#                        （TLSv1.3 / TLS_AES_256_GCM_SHA384 / http/1.1）
#     · 请求头 / UA   —— 四种组合（Chrome UA / 不发 UA / 老实报 urllib /
#                        去掉 Accept-Language）全部 0/3
#     · 我们代码的错  —— `requests` 独立复现，两个栈同一结果
#拦的是 Python 的 TLS 客户端指纹，只有换栈能解。
#
# 这是一次刻意的取舍，记录在案：`curl_cffi` 伪装浏览器指纹以通过反爬。
# 它把"我们是谁"变得和浏览器不可区分，站点方未必乐见。换来的是一扇真的能开的门
# （不换栈时，中文站点会稳定失败，而这类半坏状态从外部看不出来）。
# 没装 `curl_cffi` 时退回 urllib：对外国站点仍然可用，中文站点会失败并在
# `error` 里说清 —— 比整个工具崩掉好。

#: 跟几跳。取 5：同源跳转超过 5 次的站点本身就可疑。
_MAX_REDIRECTS = 5

_REDIRECT_CODES = (301, 302, 303, 307, 308)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _have_curl_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


def _http_get_once(url: str, timeout: int = 15):
    """一次 GET，不跟随重定向。返回 `(status, headers, body_bytes)`。

    不跟随是刻意的 —— 跳转由 :func:`_web_fetch` 自己走，好让每一跳都过地板。
    """
    from core import net_proxy as _np
    _curl_proxies = _np.curl_proxies()      # 同 `_http_json`：curl 用这张表
    if _have_curl_cffi():
        from curl_cffi import requests as _cr
        kw = {"proxies": _curl_proxies} if _curl_proxies is not None else {}
        r = _cr.get(url, headers=_HEADERS, impersonate="chrome",
                    timeout=timeout, allow_redirects=False, **kw)
        return r.status_code, dict(r.headers), r.content

    # 退回 urllib：自己把重定向处理器关掉
    # 不能靠"捕获 HTTPError 3xx"来判断"这是个重定向" ——
    # `redirect_request` 返回 None 和 "跟到上限"抛的是同一个 `HTTPError 302`。
    # 夹具里一个同源自环，输出与跨 host 逐字同形。
    # 用显式处理器把"不跟随"变成确定性行为，就不会误判。
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    #: 代理同一张表：`None` = 跟随环境变量；否则显式给一张
    #:   （`direct` 那张是空的 = 不走代理）。
    _proxies = _np.requests_proxies()
    _handlers = [_NoRedirect]
    if _proxies is not None:
        _handlers.append(urllib.request.ProxyHandler(
            {k: v for k, v in _proxies.items() if v}))
    opener = urllib.request.build_opener(*_handlers)
    try:
        with opener.open(urllib.request.Request(url, headers=_HEADERS),
                         timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        # 3xx 会走这里：处理器拒绝跟随后，HTTPErrorProcessor 把它当成未处理的错误
        return e.code, dict(e.headers or {}), (e.read() or b"")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def _same_site(a: str, b: str) -> bool:
    """两个 URL 是不是同一个站（scheme + 端口 + host，host 忽略 `www.` 前缀）。

    判据：protocol 相同、port 相同、剥掉 `www.` 后 hostname 完全相等。
    跨 host 的跳转不跟随，而是交回模型
    —— 模型要重新调一次 `web_fetch`，于是重新走一遍判定。

    忽略 `www.` 是有代价的取舍：多跟一跳同名站（`example.com` → `www.example.com`
    很常见），换掉的是大量正常抓取被退回给模型。
    """
    from urllib.parse import urlsplit

    pa, pb = urlsplit(a), urlsplit(b)

    def norm(p):
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        port = p.port or (443 if p.scheme == "https" else 80)
        return (p.scheme.lower(), host, port)

    return norm(pa) == norm(pb)


def _web_fetch(url: str) -> dict:
    """
    Fetch a web page and extract its readable text content.

    三层：
      1. 地板 —— 每一跳都过 `core.security.check_url_egress`（非公网目标直接拒）
      2. 传输 —— `curl_cffi` 浏览器指纹（见本节开头那张表）
      3. 跳转 —— 同站跟随、跨站交回模型

    Strips HTML tags, scripts, and styles. Returns plain text
    limited to 8000 characters to protect LLM context.
    """
    from urllib.parse import urljoin

    from core.security import UrlEgressError, check_url_egress
    # 域名级审批闸 —— 与地板成对出现（地板管"能不能"，它管"该不该"）。
    from core.egress_gate import egress_gate

    current = (url or "").strip()
    if not current:
        return {"success": False, "error": "URL 为空"}

    hops: list[str] = []
    status = 0
    headers: dict = {}
    raw: bytes = b""

    for _hop in range(_MAX_REDIRECTS + 1):
        # ⓪ 金丝雀（`ICE` 网络侧）—— 排在一切之前
        #
        # 必须在 `check_url_egress` 之前：诱饵域名用的是 `.invalid`
        #    （RFC 2606 保留、永远解析不到），地板会在"解析失败"那一支
        #    把它拦下，而那是另一件事（"这个域名解析不了"不等于
        #    "这是一次金丝雀触发"）—— 排后面的话这条闸永远不会响。
        # 逐跳都要过（与地板同一条纪律）：跳转的目标里也可能带着诱饵。
        from core.ice.canary import check_outbound
        _canary = check_outbound(current, where="web_fetch")
        if _canary is not None:
            return _canary
        # 弱信号：只报不拦 —— 它没有 `return`。
        #    紧跟金丝雀、同在"地板之前"，理由与它同：这是"看这个 URL"的地方，
        #    地板那一支失败时会把整段跳出去，排后面就看不到了。
        from core.ice.net_heuristics import note_signals
        note_signals(current, where="web_fetch")

        # ① 地板：每一跳都要过，不只是第一跳
        try:
            current = check_url_egress(current)
        except UrlEgressError as e:
            return {"success": False, "error": str(e),
                    "blocked": True, "url": current}

        # ①.5 域名级审批闸 —— 在发请求之前
        #
        # 位置承重：地板之后、`_http_get_once` 之前。
        #   · 在地板之后，`deny` 先于 `ask`（安全求值的固定顺序）
        #   · 在发请求之前，没批过的域名一个字节都不发出去
        # 逐跳都要过（与地板同一条纪律）：同站跳转允许 `www.` 那一类改写，
        #   换个主机名就是另一个域名。批过就记住了，所以正常只问一次。
        gate = egress_gate(current)
        if gate is not None:
            return gate

        hops.append(current)

        # ② 传输
        try:
            status, headers, raw = _http_get_once(current)
        except RuntimeError as e:
            return {"success": False, "error": str(e), "url": current}
        except Exception as e:
            return {"success": False,
                    "error": f"Fetch failed: {type(e).__name__}: {e}",
                    "url": current}

        # ③ 跳转
        if status not in _REDIRECT_CODES:
            break

        location = headers.get("Location") or headers.get("location") or ""
        if not location:
            break
        target = urljoin(current, location)

        if not _same_site(target, current):
            # 不跟随 —— 交回模型，让它自己再调一次（于是重走一遍判定）
            return {
                "success": False,
                "redirect_to": target,
                "cross_host_redirect": True,
                "url": current,
                "error": (
                    f"该地址跳转到了另一个站点：{target}\n"
                    f"出于安全考虑跨站跳转不会被自动跟随。"
                    f"如果你确实需要这个目标，请直接对那个 URL 再调用一次 web_fetch。"
                ),
            }
        current = target
    else:
        return {"success": False,
                "error": f"跳转次数超过上限（{_MAX_REDIRECTS} 跳）", "url": current}

    # HTTP 层错误
    if status >= 400:
        return {"success": False, "error": f"HTTP {status}", "url": current}

    content_type = headers.get("Content-Type", "") or headers.get("content-type", "")

    encoding = "utf-8"
    for part in content_type.split(";"):
        if "charset=" in part.lower():
            encoding = part.split("=")[-1].strip().strip('"').strip("'")
            break

    try:
        html = raw.decode(encoding, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")

    # Extract text
    text = _html_to_text(html)

    # Truncate for context protection
    truncated = text[:8000]
    if len(text) > 8000:
        truncated += f"\n\n... (truncated, original length: {len(text)} chars)"

    out = {
        "success": True,
        "url": current,
        "content_type": content_type,
        "text": truncated,
        "length": len(truncated),
        "original_length": len(text),
    }
    # 只在真的跟过跳转时才带这一格 —— 免得给模型增加每轮都得读的噪声
    if len(hops) > 1:
        out["redirected_from"] = hops[0]
        out["hops"] = len(hops)
    return out


def _html_to_text(html_str: str) -> str:
    """Strip HTML tags and extract readable text."""
    # Remove scripts and styles
    html_str = re.sub(
        r"<(script|style|noscript|iframe|nav|footer|header|aside)[^>]*>.*?</\1>",
        "", html_str, flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove HTML comments
    html_str = re.sub(r"<!--.*?-->", "", html_str, flags=re.DOTALL)
    # Replace block-level tags with newlines
    html_str = re.sub(
        r"</?(div|p|h[1-6]|li|tr|br|hr|article|section|table)[^>]*>",
        "\n", html_str, flags=re.IGNORECASE,
    )
    # Remove remaining tags
    html_str = re.sub(r"<[^>]+>", "", html_str)
    # Decode HTML entities
    html_str = html_mod.unescape(html_str)
    # Collapse whitespace
    html_str = re.sub(r"[ \t]+", " ", html_str)
    html_str = re.sub(r"\n{3,}", "\n\n", html_str)
    html_str = html_str.strip()

    return html_str
