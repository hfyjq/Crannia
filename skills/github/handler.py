"""GitHub 原生集成 —— 直接走 REST API，不经过 MCP。

## 为什么是原生的

远程 MCP 那一条（`api.githubcopilot.com`）能连、46 个工具也能列出来，
但它要求每一个用户各自把令牌交给那个服务、且工具的形态由对方决定。
原生这一条只依赖 `api.github.com` 这个公开 REST 面：凭据留在本机环境变量里、
错误由我们自己说清、每一个工具的语义我们自己定。

## 凭据

`CRANNIA_GITHUB_TOKEN` 优先，其次 `GITHUB_PERSONAL_ACCESS_TOKEN`。
两个都没设，工具照常注册，调用时回一句"没配令牌"（不是崩、也不是假装成功）。

## 代理

每次请求现造客户端，参数取自 `core/net_proxy.py` ——
设置页改档之后，下一次调用就跟着走（不像 MCP 那样是长连接，要重连）。

## 错误

四种分开说，不合并成一句"请求失败"：401 令牌无效 / 403 权限或限流 /
404 找不到 / 网络不通（含代理）。限流会把 `reset` 时间带出来。
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "github_whoami", "github_get_file", "github_search_code",
    "github_list_issues", "github_get_issue", "github_create_issue",
    "github_list_pulls", "github_get_pull", "github_create_pull",
    "github_list_commits",
]

API = "https://api.github.com"
_TIMEOUT_S = 30.0


def init(security_guard=None) -> None:
    """装载钩子。不做网络请求、不校验令牌 —— 那是每次调用时的事。"""
    return None


# 基础设施

def _token() -> str:
    """唯一读取口在 `core/github_config`（设置页 > 环境变量）。"""
    from core import github_config as _gc
    return _gc.token()


def _no_token() -> dict:
    return {
        "success": False,
        "error": "没有配置 GitHub 令牌。",
        "hint": ("去**设置页 → 技能与连接器 → GitHub** 填一个；"
                 "或者设环境变量 `CRANNIA_GITHUB_TOKEN`。"
                 "细粒度令牌只需要目标仓库的 Contents 只读即可。"),
    }


def _client():
    """按当前设置造一个 httpx 客户端（代理跟随设置页那一档）。"""
    from core import net_proxy as _np
    import httpx

    # 走 `net_proxy.httpx_client` —— 代理跟着设置页那一档走，
    #   而且"全仓造客户端的地方都经过 `core/net_proxy`"那条普查按调用点判。
    return _np.httpx_client(httpx.Client, follow_redirects=True,
                            timeout=_TIMEOUT_S)


def _rate_hint(resp) -> str:
    left = resp.headers.get("X-RateLimit-Remaining", "")
    reset = resp.headers.get("X-RateLimit-Reset", "")
    if not left and not reset:
        return ""
    return "（剩余 %s 次，重置时间戳 %s）" % (left or "?", reset or "?")


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None) -> dict:
    """唯一出口。返回 `{success, ...}`，从不抛。"""
    tok = _token()
    if not tok:
        return _no_token()
    headers = {
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "crannia",
    }
    try:
        with _client() as c:
            r = c.request(method, API + path, headers=headers,
                          params=params or None,
                          json=body if body is not None else None)
    except Exception as exc:                                  # noqa: BLE001
        return {"success": False,
                "error": "连不上 GitHub（%s）。" % type(exc).__name__,
                "hint": "如果是网络问题：设置页的「出网代理」要选对（system / manual）。"}

    if r.status_code == 401:
        return {"success": False, "status": 401,
                "error": "令牌无效或已过期。",
                "hint": "重新生成一个，更新环境变量后重启框架。"}
    if r.status_code == 403:
        return {"success": False, "status": 403,
                "error": "权限不足或触发了限流。" + _rate_hint(r),
                "hint": "细粒度令牌要给目标仓库对应的权限（Contents 至少要只读）。"}
    if r.status_code == 404:
        return {"success": False, "status": 404,
                "error": "找不到（仓库 / 文件 / 编号可能不对，或令牌看不到它）。"}
    if r.status_code >= 400:
        try:
            detail = r.json().get("message", "")
        except ValueError:
            detail = (r.text or "")[:200]
        return {"success": False, "status": r.status_code,
                "error": "GitHub 返回 %d：%s" % (r.status_code, detail)}
    try:
        return {"success": True, "status": r.status_code, "data": r.json()}
    except ValueError:
        return {"success": True, "status": r.status_code, "text": (r.text or "")[:2000]}


def _repo_path(owner: str, repo: str) -> str:
    return "/repos/%s/%s" % (owner.strip(), repo.strip())


def _limit(n, default: int, cap: int = 100) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = default
    return max(1, min(v, cap))


# 工具

def github_whoami() -> dict:
    r = _request("GET", "/user")
    if not r.get("success"):
        return r
    u = r["data"]
    return {"success": True, "login": u.get("login", ""),
            "name": u.get("name", ""),
            "public_repos": u.get("public_repos", 0),
            "html_url": u.get("html_url", ""),
            "note": "令牌可用。"}


def github_get_file(owner: str = "", repo: str = "", path: str = "",
                    ref: str = "") -> dict:
    if not owner or not repo:
        return {"success": False, "error": "要 owner 和 repo。"}
    r = _request("GET", _repo_path(owner, repo) + "/contents/" + (path or ""),
                 params={"ref": ref} if ref else None)
    if not r.get("success"):
        return r
    data = r["data"]
    if isinstance(data, list):
        return {"success": True, "kind": "dir",
                "entries": [{"name": e.get("name"), "type": e.get("type"),
                             "size": e.get("size"), "path": e.get("path")}
                            for e in data[:200]],
                "truncated": len(data) > 200}
    import base64
    raw = data.get("content") or ""
    try:
        text = base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:                                         # noqa: BLE001
        text = ""
    return {"success": True, "kind": "file", "path": data.get("path"),
            "size": data.get("size", 0), "sha": data.get("sha", ""),
            "encoding": data.get("encoding", ""),
            "truncated": bool(data.get("truncated")),
            "content": text[:60000]}


def github_search_code(query: str = "", limit: int = 10) -> dict:
    if not query.strip():
        return {"success": False, "error": "缺少 query。"}
    r = _request("GET", "/search/code",
                 params={"q": query, "per_page": _limit(limit, 10, 50)})
    if not r.get("success"):
        return r
    d = r["data"]
    return {"success": True, "total": d.get("total_count", 0),
            "items": [{"repo": (i.get("repository") or {}).get("full_name"),
                       "path": i.get("path"), "html_url": i.get("html_url")}
                      for i in (d.get("items") or [])],
            "note": "total 是 GitHub 报的总数，items 只是前几条。"}


def _issue_row(i: dict) -> dict:
    return {"number": i.get("number"), "title": i.get("title"),
            "state": i.get("state"),
            "user": (i.get("user") or {}).get("login"),
            "labels": [l.get("name") for l in (i.get("labels") or [])
                       if isinstance(l, dict)],
            "html_url": i.get("html_url"),
            "comments": i.get("comments", 0)}


def github_list_issues(owner: str = "", repo: str = "", state: str = "open",
                       limit: int = 20) -> dict:
    if not owner or not repo:
        return {"success": False, "error": "要 owner 和 repo。"}
    st = state if state in ("open", "closed", "all") else "open"
    r = _request("GET", _repo_path(owner, repo) + "/issues",
                 params={"state": st, "per_page": _limit(limit, 20)})
    if not r.get("success"):
        return r
    rows = [_issue_row(i) for i in r["data"] if not i.get("pull_request")]
    return {"success": True, "count": len(rows), "issues": rows}


def github_get_issue(owner: str = "", repo: str = "", number: int = 0,
                     comments: int = 10) -> dict:
    if not owner or not repo or not number:
        return {"success": False, "error": "要 owner、repo、number。"}
    r = _request("GET", "%s/issues/%d" % (_repo_path(owner, repo), int(number)))
    if not r.get("success"):
        return r
    out = _issue_row(r["data"])
    out["body"] = (r["data"].get("body") or "")[:20000]
    out["success"] = True
    rc = _request("GET", "%s/issues/%d/comments"
                  % (_repo_path(owner, repo), int(number)),
                  params={"per_page": _limit(comments, 10, 100)})
    if rc.get("success"):
        out["comment_list"] = [{"user": (c.get("user") or {}).get("login"),
                                "body": (c.get("body") or "")[:4000]}
                               for c in rc["data"]]
    return out


def github_create_issue(owner: str = "", repo: str = "", title: str = "",
                        body: str = "", labels=None) -> dict:
    if not owner or not repo or not title.strip():
        return {"success": False, "error": "要 owner、repo、title。"}
    payload: dict[str, Any] = {"title": title, "body": body or ""}
    if isinstance(labels, list) and labels:
        payload["labels"] = [str(x) for x in labels]
    r = _request("POST", _repo_path(owner, repo) + "/issues", body=payload)
    if not r.get("success"):
        return r
    d = r["data"]
    return {"success": True, "number": d.get("number"),
            "html_url": d.get("html_url"), "title": d.get("title")}


def github_list_pulls(owner: str = "", repo: str = "", state: str = "open",
                      limit: int = 20) -> dict:
    if not owner or not repo:
        return {"success": False, "error": "要 owner 和 repo。"}
    st = state if state in ("open", "closed", "all") else "open"
    r = _request("GET", _repo_path(owner, repo) + "/pulls",
                 params={"state": st, "per_page": _limit(limit, 20)})
    if not r.get("success"):
        return r
    return {"success": True, "pulls": [{
        "number": p.get("number"), "title": p.get("title"),
        "state": p.get("state"), "draft": p.get("draft", False),
        "user": (p.get("user") or {}).get("login"),
        "head": (p.get("head") or {}).get("ref"),
        "base": (p.get("base") or {}).get("ref"),
        "html_url": p.get("html_url"),
    } for p in r["data"]]}


def github_get_pull(owner: str = "", repo: str = "", number: int = 0,
                    files: bool = True) -> dict:
    if not owner or not repo or not number:
        return {"success": False, "error": "要 owner、repo、number。"}
    base = "%s/pulls/%d" % (_repo_path(owner, repo), int(number))
    r = _request("GET", base)
    if not r.get("success"):
        return r
    p = r["data"]
    out = {
        "success": True, "number": p.get("number"), "title": p.get("title"),
        "state": p.get("state"), "draft": p.get("draft", False),
        "mergeable": p.get("mergeable"),
        "merged": p.get("merged", False),
        "additions": p.get("additions", 0), "deletions": p.get("deletions", 0),
        "changed_files": p.get("changed_files", 0),
        "head": (p.get("head") or {}).get("ref"),
        "base": (p.get("base") or {}).get("ref"),
        "html_url": p.get("html_url"),
        "body": (p.get("body") or "")[:8000],
    }
    if files:
        rf = _request("GET", base + "/files", params={"per_page": 100})
        if rf.get("success"):
            out["files"] = [{"filename": f.get("filename"),
                             "status": f.get("status"),
                             "additions": f.get("additions", 0),
                             "deletions": f.get("deletions", 0)}
                            for f in rf["data"]]
    return out


def github_create_pull(owner: str = "", repo: str = "", title: str = "",
                       head: str = "", base: str = "", body: str = "") -> dict:
    if not all([owner, repo, title.strip(), head, base]):
        return {"success": False, "error": "要 owner、repo、title、head、base。"}
    r = _request("POST", _repo_path(owner, repo) + "/pulls",
                 body={"title": title, "head": head, "base": base,
                       "body": body or ""})
    if not r.get("success"):
        return r
    d = r["data"]
    return {"success": True, "number": d.get("number"),
            "html_url": d.get("html_url"), "title": d.get("title")}


def github_list_commits(owner: str = "", repo: str = "", branch: str = "",
                        limit: int = 20) -> dict:
    if not owner or not repo:
        return {"success": False, "error": "要 owner 和 repo。"}
    params = {"per_page": _limit(limit, 20)}
    if branch:
        params["sha"] = branch
    r = _request("GET", _repo_path(owner, repo) + "/commits", params=params)
    if not r.get("success"):
        return r
    return {"success": True, "commits": [{
        "sha": (c.get("sha") or "")[:10],
        "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0][:200],
        "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
        "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
        "html_url": c.get("html_url"),
    } for c in r["data"]]}
