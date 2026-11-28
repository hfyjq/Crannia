# -*- coding: utf-8 -*-
"""`web_request` —— 带自定义请求头的 HTTP 请求（出网权限重构 P2）。

`web_fetch` 是"取网页/下载"（GET + 正文抽取）；本工具补的是
"要带自定义头 / 要非 GET 方法"的那一类。它只补这一项：
不做续传、不做多连接、不提供关闭证书校验的开关。

## 四道闸

1. 地板：每一跳过 `core.security.check_url_egress`（非公网目标直接拒）。
2. 出网审批：每一跳过 `core.egress_gate.egress_gate`（同站跟随、跨站交回模型）。
   缓存不启用 —— 请求可能带副作用。
3. 头白名单：凭据头只接受 `credential` 条目名注入（值不进工具参数、不回显）；
   `Host` / `Content-Length` / `Transfer-Encoding` / `Connection` / `Proxy-*`
   不许覆盖。
4. 审计：每次调用写一行 EGRESS（目标 / 方法 / 结果）。

## 凭据条目

用户在 `data/settings.json` 里配：

    "web_request": {"credentials": {"名字": {"headers": {"Authorization": "..."}}}}

模型只能给条目名（`credential="名字"`），拿不到值；回执里也不回显。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

__all__ = ["TOOL_NAME", "SCHEMA", "web_request", "register",
           "credentials_public", "save_credential", "delete_credential"]

TOOL_NAME = "web_request"

ALLOWED_METHODS: tuple[str, ...] = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")

#: 模型不许自己填的凭据头（只接受 credential 条目注入）。
_CREDENTIAL_HEADERS: frozenset[str] = frozenset({
    "authorization", "cookie", "proxy-authorization", "x-api-key",
    "api-key", "x-auth-token", "x-access-token", "private-token",
})

#: 不许覆盖的头（防请求走私 / 绕 vhost）。
_FORBIDDEN_OVERRIDE: frozenset[str] = frozenset({
    "host", "content-length", "transfer-encoding", "connection",
    "expect", "upgrade", "te",
})

_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_HEADER_COUNT = 50
_MAX_HEADER_CHARS = 8 * 1024
_MAX_PREVIEW = 8000
_MAX_BYTES = 64 * 1024 * 1024
_TIMEOUT_S = 15

_MASKED_HEADERS = frozenset({"set-cookie", "authorization", "proxy-authenticate",
                             "www-authenticate"})

_TEXTUAL_PREFIXES = ("text/", "application/json", "application/xml",
                     "application/javascript", "application/x-www-form-urlencoded",
                     "application/ld+json")


SCHEMA = {
    "description": (
        "带自定义请求头的 HTTP 请求（web_fetch 是取网页/下载，这个是补充："
        "自定义头、非 GET 方法）。凭据只能通过 credential 条目名注入，"
        "不许自己写 Authorization / Cookie 这类头；Host / Content-Length 等"
        "不许覆盖。每一跳都过出网审批；跨站跳转交回给你，需要再调一次。"
    ),
    "inputSchema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整 URL（http/https）"},
            "method": {"type": "string", "enum": list(ALLOWED_METHODS),
                       "default": "GET"},
            "headers": {"type": "object",
                        "description": "自定义请求头（凭据头与 Host 一类不许写）"},
            "body": {"type": "string",
                     "description": "请求体（≤2MB）；GET/HEAD 一般不带"},
            "credential": {"type": "string",
                           "description": "设置里配好的凭据条目名（值不会回显）"},
            "timeout_s": {"type": "number", "default": _TIMEOUT_S},
        },
        "required": ["url"],
    },
}


@dataclass
class _Resp:
    status: int
    headers: dict
    body: bytes
    url: str


def _ok(data: dict, t0: float, **extra) -> dict:
    out = {"success": True, "tool": TOOL_NAME, "data": data, "error": "",
           "duration_ms": int((time.monotonic() - t0) * 1000)}
    out.update(extra)
    return out


def _err(msg: str, t0: float, **extra) -> dict:
    out = {"success": False, "tool": TOOL_NAME, "data": {}, "error": msg,
           "duration_ms": int((time.monotonic() - t0) * 1000)}
    out.update(extra)
    return out


def _credentials() -> dict:
    """设置里的凭据条目。读不到返回空表（fail-closed：等于没配）。"""
    try:
        raw = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    block = raw.get("web_request") if isinstance(raw, dict) else None
    creds = block.get("credentials") if isinstance(block, dict) else None
    return creds if isinstance(creds, dict) else {}


def credentials_public() -> list[dict]:
    """凭据条目的公开形状：只有名字与头名，值一律不回显。"""
    out = []
    for name, entry in sorted(_credentials().items()):
        headers = entry.get("headers") if isinstance(entry, dict) else None
        names = sorted(headers.keys()) if isinstance(headers, dict) else []
        out.append({"name": str(name), "headers": names})
    return out


def save_credential(name: str, headers: dict) -> tuple[bool, str]:
    """新增/合并一条凭据。返回值与错误原因。

    空值表示不修改那一格（与 skill_config_write 同一条判据）——
    界面不回显旧值，用户不可能"原样送回"，把空当清空等于每次保存都清光。
    """
    import json as _json
    from pathlib import Path as _Path
    n = str(name or "").strip()
    if not n:
        return False, "名字不能为空"
    if not isinstance(headers, dict) or not headers:
        return False, "至少要有一个头（Header: value）"
    clean: dict[str, str] = {}
    for k, v in headers.items():
        key = str(k or "").strip()
        val = str(v if v is not None else "")
        if not key:
            return False, "头名不能为空"
        if not val.strip():
            continue                    # 空值不覆盖
        clean[key] = val
    if not clean:
        return False, "所有头的值都是空的（空值表示不修改）"
    p = _Path("data/settings.json")
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    block = data.get("web_request")
    if not isinstance(block, dict):
        block = {}
        data["web_request"] = block
    creds = block.get("credentials")
    if not isinstance(creds, dict):
        creds = {}
        block["credentials"] = creds
    entry = creds.get(n)
    if not isinstance(entry, dict):
        entry = {}
    current = entry.get("headers")
    if not isinstance(current, dict):
        current = {}
    current.update(clean)
    entry["headers"] = current
    creds[n] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    import os as _os
    _os.replace(tmp, p)
    return True, ""


def delete_credential(name: str) -> bool:
    import json as _json
    from pathlib import Path as _Path
    n = str(name or "").strip()
    p = _Path("data/settings.json")
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    block = data.get("web_request") if isinstance(data, dict) else None
    creds = block.get("credentials") if isinstance(block, dict) else None
    if not isinstance(creds, dict) or n not in creds:
        return False
    del creds[n]
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    import os as _os
    _os.replace(tmp, p)
    return True


def _credential_headers(name: str) -> tuple[dict, str]:
    """条目名 -> `(要注入的头, 错误)`。"""
    n = str(name or "").strip()
    if not n:
        return {}, ""
    entry = _credentials().get(n)
    if not isinstance(entry, dict):
        return {}, ("没有这个凭据条目：%s。去设置页 → 网络请求 里配置"
                    "（模型只给条目名，值不进工具参数）。" % n)
    headers = entry.get("headers")
    if not isinstance(headers, dict) or not headers:
        return {}, "凭据条目 %s 没有可用的头。" % n
    return {str(k): str(v) for k, v in headers.items()}, ""


def _check_headers(headers) -> tuple[dict, str]:
    """模型给的头过白名单。返回 `(清洗后的头, 错误)`。"""
    if headers is None:
        return {}, ""
    if not isinstance(headers, dict):
        return {}, "headers 必须是对象（头名 -> 头值）。"
    if len(headers) > _MAX_HEADER_COUNT:
        return {}, "头太多了（上限 %d 个）。" % _MAX_HEADER_COUNT
    out = {}
    total = 0
    for k, v in headers.items():
        key = str(k or "").strip()
        val = str(v if v is not None else "")
        low = key.lower()
        if not key:
            return {}, "头的名字不能为空。"
        if low in _CREDENTIAL_HEADERS:
            return {}, ("凭据类请求头不能自己填（%s）。用 credential 条目名，"
                        "值由框架从设置里注入。" % key)
        if low in _FORBIDDEN_OVERRIDE or low.startswith("proxy-"):
            return {}, "这个头不许覆盖：%s（防请求走私 / 绕 vhost）。" % key
        total += len(key) + len(val)
        if total > _MAX_HEADER_CHARS:
            return {}, "头总长度超限（%d 字符）。" % _MAX_HEADER_CHARS
        out[key] = val
    return out, ""


def _mask_headers(headers: dict) -> dict:
    return {k: ("<已打码>" if k.lower() in _MASKED_HEADERS else v)
            for k, v in (headers or {}).items()}


def _is_textual(content_type: str) -> bool:
    ct = str(content_type or "").lower()
    return any(ct.startswith(p) for p in _TEXTUAL_PREFIXES)


def _send(method: str, url: str, headers: dict, body: Optional[bytes],
          timeout: float, max_bytes: int) -> _Resp:
    """发一次请求，不跟随重定向。curl_cffi 优先（中文站点的指纹问题）。"""
    from core import net_proxy as _np

    try:
        from curl_cffi import requests as _cr
        proxies = _np.curl_proxies()
        kw = {"proxies": proxies} if proxies is not None else {}
        r = _cr.request(method, url, headers=headers, data=body,
                        impersonate="chrome", timeout=timeout,
                        allow_redirects=False, stream=True, **kw)
        try:
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise _TooLarge(max_bytes)
        finally:
            try:
                r.close()
            except Exception:                               # noqa: BLE001
                pass
        return _Resp(r.status_code, dict(r.headers), bytes(buf), url)
    except ImportError:
        pass

    import httpx
    with _np.httpx_client(httpx.Client, url=url, timeout=timeout,
                          follow_redirects=False) as client:
        with client.stream(method, url, headers=headers, content=body) as resp:
            buf = bytearray()
            for chunk in resp.iter_bytes(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise _TooLarge(max_bytes)
            return _Resp(resp.status_code, dict(resp.headers), bytes(buf),
                         str(resp.url))


class _TooLarge(Exception):
    def __init__(self, limit: int):
        super().__init__("response too large (limit %d bytes)" % limit)
        self.limit = limit


def _save_binary(url: str, body: bytes) -> str:
    """二进制落盘到工具结果目录，回指针。"""
    from core.workspace import primary_output_dir
    import re
    name = (urlsplit(url).path.rsplit("/", 1)[-1] or "response.bin")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "response.bin"
    out_dir = Path(primary_output_dir()) / "tool_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("%d_%s" % (int(time.time()), name))
    path.write_bytes(body)
    return str(path)


def _grant_source(host: str) -> str:
    """这次凭什么放行：会话 / 白名单文件 / 都没命中（刚批的）。"""
    try:
        from core.allowlist import list_for, matches_any
        if matches_any(list_for("egress"), host):
            return "file"
    except Exception:                                       # noqa: BLE001
        pass
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm is not None and wm.has_target_grant("egress", host):
            return "session"
    except Exception:                                       # noqa: BLE001
        pass
    return "prompt"


def _audit(host: str, method: str, verdict: str) -> None:
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        guard = getattr(wm, "security", None) if wm is not None else None
        if guard is not None:
            guard.record_egress("http", host, "request " + method,
                                _grant_source(host), verdict)
    except Exception as exc:                                # noqa: BLE001
        print(f"[web_request] audit write failed ({type(exc).__name__}: {exc})")


def web_request(url: str, method: str = "GET", headers=None, body: str = "",
                credential: str = "", timeout_s: float = _TIMEOUT_S) -> dict:
    t0 = time.monotonic()
    u = str(url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return _err("url 必须以 http:// 或 https:// 开头", t0)
    m = str(method or "GET").strip().upper()
    if m not in ALLOWED_METHODS:
        return _err("method 只认 %s" % " / ".join(ALLOWED_METHODS), t0)

    clean, why = _check_headers(headers)
    if why:
        return _err(why, t0)
    injected, why = _credential_headers(credential)
    if why:
        return _err(why, t0)
    merged = dict(clean)
    merged.update(injected)          # 注入的头优先（模型也写不了同名）
    if "User-Agent" not in merged:
        merged["User-Agent"] = "Crannia/1.1"

    raw_body = body.encode("utf-8") if isinstance(body, str) else b""
    if len(raw_body) > _MAX_BODY_BYTES:
        return _err("body 超限（上限 2MB）", t0)

    from core.security import UrlEgressError, check_url_egress
    from core.egress_gate import egress_gate

    current = u
    hops: list[str] = []
    for _ in range(8):
        try:
            current = check_url_egress(current)
        except UrlEgressError as exc:
            _audit(_host_of(u), m, "denied")
            return _err("出网地板拒绝：%s" % exc, t0)

        gate = egress_gate(current, tool_name=TOOL_NAME)
        if gate is not None:
            _audit(_host_of(current), m, "asked")
            return gate

        hops.append(current)
        try:
            resp = _send(m, current, merged, raw_body or None,
                         float(timeout_s), _MAX_BYTES)
        except _TooLarge:
            _audit(_host_of(current), m, "error")
            return _err("响应超过 %d MB，未读取。" % (_MAX_BYTES // 1048576), t0)
        except Exception as exc:                            # noqa: BLE001
            _audit(_host_of(current), m, "error")
            return _err("%s: %s" % (type(exc).__name__, exc), t0)

        if resp.status not in (301, 302, 303, 307, 308):
            break
        loc = str(resp.headers.get("location") or resp.headers.get("Location") or "")
        if not loc:
            break
        nxt = _absolutize(current, loc)
        if not _same_site(current, nxt):
            _audit(_host_of(current), m, "ok")
            return _ok({
                "url": current, "status": resp.status,
                "redirect_to": nxt,
                "note": ("跨站跳转没有跟随。要跟过去，请对 redirect_to "
                         "再调一次 web_request（会重新过审批）。"),
            }, t0)
        current = nxt
    else:
        return _err("跳转次数超过上限。", t0)

    ct = str(resp.headers.get("content-type") or resp.headers.get("Content-Type") or "")
    data: dict = {"url": resp.url or current, "status": resp.status,
                  "headers": _mask_headers(resp.headers),
                  "hops": hops, "method": m}
    if _is_textual(ct) or (not ct and b"\x00" not in resp.body[:1024]):
        text = resp.body.decode("utf-8", "replace")
        data["body"] = text[:_MAX_PREVIEW]
        if len(text) > _MAX_PREVIEW:
            data["truncated"] = True
            data["body_length"] = len(text)
    else:
        path = _save_binary(resp.url or current, resp.body)
        data["binary_path"] = path
        data["bytes"] = len(resp.body)
        data["content_type"] = ct or "(未知)"
    _audit(_host_of(resp.url or current), m, "ok")
    return _ok(data, t0)


def _host_of(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _absolutize(base: str, loc: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, loc)


def _same_site(a: str, b: str) -> bool:
    """同站判据：scheme + 端口 + 主机（忽略 www. 前缀）。

    与 web_fetch 的同一条判据（那边是 `_same_site`；这里用同一份实现，
    两处各写一份必然漂）。
    """
    from core.egress_gate import same_site
    return same_site(a, b)


def register() -> None:
    """注册工具（由 `init_bus` 调用；`register_tool` 幂等）。"""
    from bus.registry import register_tool
    register_tool(TOOL_NAME, web_request, SCHEMA)
