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
from pathlib import Path
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


class _ResponseTooLarge(Exception):
    """响应超过单次上限 —— 在读的过程中就抛，不把整包读进来。"""

    def __init__(self, size, cap):
        self.size = int(size or 0)
        self.cap = int(cap or 0)
        super().__init__(
            "响应超过单次上限（上限 %d 字节，约 %.1f MB；本次 %d 字节）—— 没有保存。"
            "换一个更小的来源，或确认这个地址是不是一份文档。"
            % (self.cap, self.cap / 1048576.0, self.size))


def _declared_length(headers):
    """`Content-Length`（读不到或不是整数就 `None`）。"""
    for k, v in (headers or {}).items():
        if str(k).lower() == "content-length":
            try:
                return int(str(v))
            except (TypeError, ValueError):
                return None
    return None


def _check_declared_length(headers, max_bytes) -> None:
    """声明就超了：连读都不读，直接抛。"""
    n = _declared_length(headers)
    if max_bytes and n is not None and n > max_bytes:
        raise _ResponseTooLarge(n, max_bytes)


def _read_capped(read_fn, max_bytes):
    """读最多 `max_bytes` 字节，多出一个字节就抛。

    `read_fn(n)` 与 `urllib` 的 `Response.read(n)` 同形；`max_bytes` 为空 = 不限。
    """
    if not max_bytes:
        return read_fn()
    data = read_fn(int(max_bytes) + 1)
    if len(data) > max_bytes:
        raise _ResponseTooLarge(len(data), max_bytes)
    return data


def _read_stream(chunks, max_bytes):
    """把字节块累加到上限；超了立刻抛（后面的块不再读）。"""
    buf = bytearray()
    for chunk in chunks:
        buf += chunk
        if max_bytes and len(buf) > max_bytes:
            raise _ResponseTooLarge(len(buf), max_bytes)
    return bytes(buf)


def _http_get_once(url: str, timeout: int = 15, max_bytes=None):
    """一次 GET，不跟随重定向。返回 `(status, headers, body_bytes)`。

    不跟随是刻意的 —— 跳转由 :func:`_web_fetch` 自己走，好让每一跳都过地板。

    *max_bytes*：读的上限。给了就边读边判（声明先看一眼，读的过程中
    累计超过立刻抛 `_ResponseTooLarge`）—— 读完再比保护不了内存。
    """
    from core import net_proxy as _np
    _curl_proxies = _np.curl_proxies()      # 同 `_http_json`：curl 用这张表
    if _have_curl_cffi():
        from curl_cffi import requests as _cr
        kw = {"proxies": _curl_proxies} if _curl_proxies is not None else {}
        r = _cr.get(url, headers=_HEADERS, impersonate="chrome",
                    timeout=timeout, allow_redirects=False, stream=True, **kw)
        headers = dict(r.headers)
        try:
            _check_declared_length(headers, max_bytes)
            body = _read_stream(r.iter_content(chunk_size=65536), max_bytes)
        finally:
            try:
                r.close()
            except Exception:                              # noqa: BLE001
                pass
        return r.status_code, headers, body

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
            headers = dict(resp.headers)
            _check_declared_length(headers, max_bytes)
            return resp.status, headers, _read_capped(resp.read, max_bytes)
    except urllib.error.HTTPError as e:
        # 3xx 会走这里：处理器拒绝跟随后，HTTPErrorProcessor 把它当成未处理的错误
        headers = dict(e.headers or {})
        _check_declared_length(headers, max_bytes)
        return e.code, headers, _read_capped(e.read, max_bytes)
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


#: 单次抓取的内容上限。年报这类文件常见几十 MB（英文版尤其大），
#: 64 MB 覆盖得住；再大就不是"一次抓取"该干的事了。
#: 判在读的过程中（`_http_get_once` 的 `max_bytes`），不是读完再比 ——
#: 读完整包再判保护不了内存。
_MAX_BINARY_BYTES = 64 * 1024 * 1024

#: 一次抓取的超时。与常见抓取工具的取值一致（60 秒）；
#: 这里的 60 秒是整次请求的，不是每块。
_FETCH_TIMEOUT_S = 60

#: 明确是文本的那些声明前缀（`+json` / `+xml` 后缀按文本处理）。
_TEXTUAL_PREFIXES = ("text/", "application/json", "application/xml",
                     "application/javascript", "application/xhtml")

#: 魔数 -> 扩展名。只认强特征：`PK` 那类压缩容器分不出 docx / xlsx / zip，
#: 不在这一格猜 —— 交给声明的子树名（见 :func:`_binary_ext_of`）。
_MAGIC_EXT = ((b"%PDF-", "pdf"),)

#: 精确 mime -> 扩展名。只收"从子类型推不出来"的那几族（Office 的
#:   `vnd.openxmlformats-…` 现推出来是一串没意义的字母）。其余一律按
#:   子类型现推 —— 枚举 mime 必然漏（同"分类，不枚举"那条判据）。
_MIME_EXT = {
    "application/zip": "zip",
    "application/msword": "doc",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}

#: 子类型 -> 扩展名的别名（标准子类型与常见扩展名不同名的那几个）。
#: 空串 = 类型本身没有信息，交给 URL 后缀。
_SUB_ALIASES = {
    "jpeg": "jpg", "pjpeg": "jpg",
    "tiff": "tif",
    "svg+xml": "svg",
    "x-icon": "ico", "vnd.microsoft.icon": "ico", "x-ms-bmp": "bmp",
    "x-photoshop": "psd", "vnd.adobe.photoshop": "psd",
    "quicktime": "mov", "x-matroska": "mkv",
    "x-rar-compressed": "rar", "vnd.rar": "rar",
    "x-7z-compressed": "7z", "gzip": "gz", "x-gzip": "gz",
    "x-tar": "tar", "x-bzip2": "bz2", "zstd": "zst", "x-xz": "xz",
    "x-flac": "flac", "x-m4a": "m4a", "x-wav": "wav",
    "msword": "doc", "vnd.ms-excel": "xls",
    "octet-stream": "",
}


def _safe_ext(name: str) -> str:
    """把任意字符串收成一个安全的扩展名（小写字母数字，最多 8 位）。"""
    s = re.sub(r"\+.*$", "", str(name or ""))
    s = re.sub(r"^(x-|vnd\.)", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)[:8]


def _url_ext(url: str) -> str:
    """URL 路径最后那段的后缀（没有就空串）。"""
    from urllib.parse import urlsplit
    try:
        name = (urlsplit(url).path or "").rsplit("/", 1)[-1]
    except Exception:                                          # noqa: BLE001
        name = ""
    m = re.search(r"\.([A-Za-z0-9]{1,8})$", name or "")
    return m.group(1).lower() if m else ""


def _ext_from_subtype(sub: str) -> str:
    if sub in _SUB_ALIASES:
        return _SUB_ALIASES[sub]
    # 相机 RAW 常写成 x-厂商-扩展名（x-canon-cr2 / x-nikon-nef）：取最后一段。
    if sub.startswith("x-") and sub.count("-") >= 2:
        return _safe_ext(sub.rsplit("-", 1)[-1])
    return _safe_ext(sub)


def _binary_ext_of(content_type: str, raw: bytes, url: str = "") -> str | None:
    """这份响应是不是二进制？是就回扩展名，不是回 `None`。

    三层，前面的赢：
      1. 魔数：PDF，以及 `core.artifacts.sniff` 认得出的图片 / 音视频；
      2. `Content-Type`：先查精确表，再按子类型现推；
      3. URL 后缀（声明成 `application/octet-stream` 的常是这一类）。
    都推不出来才回 `bin` —— 不枚举 mime。
    """
    for magic, ext in _MAGIC_EXT:
        if raw.startswith(magic):
            return ext
    try:
        from core.artifacts import ext_for_mime, sniff
        _sniffed = sniff(raw)
        if _sniffed:
            _ext = ext_for_mime(_sniffed)
            if _ext:
                return _ext
    except Exception:                                          # noqa: BLE001
        pass
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return None
    if ct.startswith(_TEXTUAL_PREFIXES) or ct.endswith(("+json", "+xml")):
        return None
    if ct in _MIME_EXT:
        return _MIME_EXT[ct]
    ext = _ext_from_subtype(ct.split("/", 1)[-1])
    if not ext:
        ext = _url_ext(url)
    return ext or "bin"


def _url_stem(url: str) -> str:
    """从 URL 最后一段取一个安全的文件名主干。取不到就叫 `download`。"""
    from urllib.parse import urlsplit
    try:
        name = (urlsplit(url).path or "").rsplit("/", 1)[-1]
    except Exception:                                          # noqa: BLE001
        name = ""
    name = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", name or "")
    name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
    return (name[:40].strip("_") or "download")


def _save_fetched_binary(url: str, raw: bytes, content_type: str,
                         ext: str) -> dict:
    """把二进制响应落盘，回一条"去读本地文件"的回执。

    落点是 `data/workdata/tool_results/` —— 与超长文本那边同一处
    （机器可读的那条指针就指向它）。目录的唯一来源是
    `core.agent_loop._resolve_tool_output_dir`，这里不另写一个路径字面量。
    """
    if len(raw) > _MAX_BINARY_BYTES:
        return {
            "success": False,
            "url": url,
            "content_type": content_type,
            "error": ("下载内容 %.1f MB，超过单次上限 %.0f MB —— 没有保存。"
                      "换一个更小的来源，或确认这个地址是不是一份文档。"
                      % (len(raw) / 1048576.0, _MAX_BINARY_BYTES / 1048576.0)),
        }
    from datetime import datetime

    from core.agent_loop import _resolve_tool_output_dir

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = _resolve_tool_output_dir() / ("%s_%s.%s" % (ts, _url_stem(url), ext))
    with open(out_path, "wb") as f:
        f.write(raw)
    shown = str(out_path).replace("\\", "/")
    print(f"[web_fetch] 二进制响应已落盘：{shown}（{len(raw)} 字节，{content_type}）")
    return {
        "success": True,
        "url": url,
        "content_type": content_type,
        "saved_path": shown,
        "bytes": len(raw),
        "note": ("这是一份二进制文件（不是网页文本），已经保存到本地。"
                 "用对应的解析工具读它 —— PDF 用 `read_pdf_pages` 按页读，"
                 "或 `parse_pdf` + `search_chunks`；不要把它当文本读。"),
    }


def _render_response(url: str, status: int, content_type: str, raw: bytes) -> dict:
    """把一份成功响应变成回执：二进制落盘，文本抽正文。

    抓取那一刻与缓存命中那一刻走的是同一段 —— 缓存里存的就是它的产物。
    """
    # 二进制（PDF 等）不按文本解码 —— 解出来是一墙替换字符，模型只能放弃。
    # 落盘 + 回路径，由解析类工具接手（`parse_pdf` / `read_pdf_pages` 那一族）。
    # 判据两条：声明不是文本族；或者魔数认得出（服务端常把 PDF 标成
    #   `application/octet-stream`，光看声明会漏）。
    _bin_ext = _binary_ext_of(content_type, raw, url)
    if _bin_ext is not None:
        return _save_fetched_binary(url, raw, content_type, _bin_ext)

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

    return {
        "success": True,
        "url": url,
        "content_type": content_type,
        "text": truncated,
        "length": len(truncated),
        "original_length": len(text),
    }


def _cached_result(url: str, hit: dict) -> dict:
    """缓存命中的回执：复用上次那份结果，加上"这是缓存"的两个字段。

    二进制那份复用同一个 `saved_path`（不重复落盘）；那份文件若已被清掉
    （留档清理），就从缓存的原始字节重新渲染一次。
    """
    out = hit.get("result")
    saved = str((out or {}).get("saved_path") or "")
    if not out or (saved and not Path(saved).is_file()):
        out = _render_response(url, int(hit.get("status") or 200),
                               hit.get("content_type") or "",
                               hit.get("raw") or b"")
    out = dict(out)
    out["cached"] = True
    out["cache_age_s"] = int(max(0.0, float(hit.get("age_s") or 0.0)))
    return out


def _web_fetch(url: str) -> dict:
    """
    Fetch a web page and extract its readable text content.

    三层：
      1. 地板 —— 每一跳都过 `core.security.check_url_egress`（非公网目标直接拒）
      2. 传输 —— `curl_cffi` 浏览器指纹（见本节开头那张表）
      3. 跳转 —— 同站跟随、跨站交回模型

    同一个地址 30 分钟内再抓一次不出网，走 `core.web_cache`（命中时
    回执带 `cached: true` 与 `cache_age_s`）。

    Strips HTML tags, scripts, and styles. Returns plain text
    limited to 8000 characters to protect LLM context.
    """
    from urllib.parse import urljoin

    from core import web_cache as _web_cache
    from core.security import UrlEgressError, check_url_egress
    # 域名级审批闸 —— 与地板成对出现（地板管"能不能"，它管"该不该"）。
    from core.egress_gate import egress_gate

    current = (url or "").strip()
    if not current:
        return {"success": False, "error": "URL 为空"}
    # 缓存键用模型给的这一个地址，不是跳转之后那一个 ——
    # 模型下一次问的还是同一个地址，命中要发生在第一跳上。
    requested = current

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

        # ①.2 响应缓存 —— 命中就到此为止，这一次一个字节都不发。
        #
        # 位置承重：在审批闸之前。闸问的是"该不该把这次请求发出去"，
        #   而没有发出去的就不该问（判据在 `core/web_cache.py`）。
        #   地板与金丝雀排在它前面：那两条只看地址，不看网络，
        #   缓存命中它们照样要过。
        _hit = _web_cache.get(current)
        if _hit is not None:
            return _cached_result(current, _hit)

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
            status, headers, raw = _http_get_once(
                current, timeout=_FETCH_TIMEOUT_S,
                max_bytes=_MAX_BINARY_BYTES)
        except _ResponseTooLarge as e:
            return {"success": False, "error": str(e), "url": current}
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

    out = _render_response(current, status, content_type, raw)
    # 只在真的跟过跳转时才带这一格 —— 免得给模型增加每轮都得读的噪声
    if len(hops) > 1:
        out["redirected_from"] = hops[0]
        out["hops"] = len(hops)
    # 只缓存成功响应（判据见 `core/web_cache.py`）：走到这里已经是终态。
    _web_cache.put(requested, status=status, content_type=content_type,
                   raw=raw, result=out)
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
