#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把发布资产传到 GitCode 的 Releases（国内直链，零成本）。

    set GITCODE_ACCESS_TOKEN=<个人访问令牌>
    .venv/Scripts/python.exe scripts/upload_gitcode.py --dry-run ^
        --repo <owner>/<repo> --tag v1.1.0 ^
        --file apps\\desktop\\release\\crannia-1.1.0-win-x64.exe

    # 真传（release 不存在时自动建）
    .venv/Scripts/python.exe scripts/upload_gitcode.py ^
        --repo <owner>/<repo> --tag v1.1.0 --create-release ^
        --file apps\\desktop\\release\\crannia-1.1.0-win-x64.exe

    # 只测一条已有的直链（不需要令牌）
    .venv/Scripts/python.exe scripts/upload_gitcode.py --check-link <直链>

    # 只改这条 release 的说明（正文从文件读，要 --tag）
    .venv/Scripts/python.exe scripts/upload_gitcode.py ^
        --repo <owner>/<repo> --tag v1.1.0 --edit-body notes.md

## 它走的那条协议

`POST /releases` 先建 release（按 tag 寻址），再 `GET
/releases/{tag}/upload_url?file_name=...` 取预签名地址与要原样带回的
`x-obs-*` 头，最后 `PUT` 字节。GitCode 的 `attach_files` 端点不存在，
别照 Gitee 的文档抄。

改说明走 `PATCH /releases/{tag}`，而且必须把 `name` 一并带上：只发
`body` 会回 400 "name must not be empty"。release 对象里没有数字 id，
路径只能用 tag。

令牌只从环境变量读，不接受命令行参数（命令行会进 shell 历史与进程表）。
脚本不打印预签名地址与它的头。

退出码：0 成功 · 1 失败 · 2 缺令牌或参数。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

API_BASE = "https://api.gitcode.com/api/v5"
DIRECT_BASE = "https://gitcode.com"
TOKEN_ENVS = ("GITCODE_ACCESS_TOKEN", "GITCODE_TOKEN")
HTTP_TIMEOUT = 60
UPLOAD_TIMEOUT = (30, 900)


class GitCodeError(RuntimeError):
    """API 返回不合约定的东西。"""


def token_from_env(env=None) -> str:
    env = os.environ if env is None else env
    for key in TOKEN_ENVS:
        value = str(env.get(key) or "").strip()
        if value:
            return value
    return ""


def split_repo(value: str) -> tuple[str, str]:
    parts = [p for p in str(value or "").strip().strip("/").split("/") if p]
    if len(parts) != 2:
        raise ValueError("--repo 要写成 owner/repo，实得 %r" % value)
    return parts[0], parts[1]


def direct_link(owner: str, repo: str, tag: str, name: str) -> str:
    """公开仓库的匿名直链（302 到对象存储，支持 Range）。"""
    return "%s/%s/%s/releases/download/%s/%s" % (
        DIRECT_BASE, quote(owner), quote(repo), quote(tag), quote(name))


def asset_names(release) -> list:
    assets = (release or {}).get("assets") if isinstance(release, dict) else None
    return [str((a or {}).get("name") or "") for a in (assets or [])
            if isinstance(a, dict)]


def probe_link(session, url: str, timeout: int = 30) -> tuple:
    """匿名探测直链：回 `(HTTP 状态, 服务端报的总字节数)`，读不到总数给 -1。"""
    resp = session.get(url, headers={"Range": "bytes=0-0"},
                       stream=True, timeout=timeout)
    total = -1
    header = str(resp.headers.get("Content-Range") or "")
    if "/" in header:
        tail = header.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            total = int(tail)
    elif str(resp.headers.get("Content-Length") or "").isdigit():
        total = int(resp.headers["Content-Length"])
    return resp.status_code, total


class GitCode:
    """GitCode v5 的最小客户端：只做发布流程用到的四件事。"""

    def __init__(self, token: str, base: str = API_BASE, session=None):
        self.token = str(token or "")
        self.base = str(base).rstrip("/")
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, **kw):
        headers = {"Authorization": "Bearer %s" % self.token,
                   "Accept": "application/json"}
        headers.update(kw.pop("headers", {}) or {})
        resp = self.session.request(method, self.base + path,
                                    headers=headers, timeout=HTTP_TIMEOUT, **kw)
        if resp.status_code >= 400:
            raise GitCodeError("%s %s -> HTTP %d: %s"
                               % (method, path, resp.status_code,
                                  (resp.text or "")[:300]))
        if not str(resp.text or "").strip():
            return {}
        try:
            return resp.json()
        except ValueError:
            raise GitCodeError("%s %s -> 响应不是 JSON" % (method, path))

    def current_user(self) -> dict:
        return self._request("GET", "/user")

    def repo_info(self, owner: str, repo: str) -> dict:
        return self._request("GET", "/repos/%s/%s" % (owner, repo))

    def release_by_tag(self, owner: str, repo: str, tag: str) -> dict:
        return self._request("GET", "/repos/%s/%s/releases/tags/%s"
                             % (owner, repo, tag))

    def create_release(self, owner: str, repo: str, tag: str,
                       name: str = "", body: str = "") -> dict:
        #: `body` 不能是空串（GitCode 回 400 "body must not be empty"）。
        payload = {"tag_name": tag, "name": name or tag,
                   "body": body or ("Crannia %s" % tag)}
        return self._request("POST", "/repos/%s/%s/releases" % (owner, repo),
                             json=payload)

    def update_release(self, owner: str, repo: str, tag: str,
                       name: str = "", body: str = "") -> dict:
        #: 说明要改就得整条改：`name` 也得带上，否则 400 "name must not be empty"。
        #:   路径用 tag（release 对象里没有数字 id）。
        payload = {"name": name or tag, "body": body}
        return self._request("PATCH",
                             "/repos/%s/%s/releases/%s" % (owner, repo, tag),
                             json=payload)

    def upload_url(self, owner: str, repo: str, tag: str,
                   file_name: str) -> tuple:
        data = self._request("GET",
                             "/repos/%s/%s/releases/%s/upload_url"
                             % (owner, repo, tag),
                             params={"file_name": file_name})
        if not isinstance(data, dict):
            data = {}
        url = str(data.get("url") or "")
        headers = data.get("headers") or {}
        if not url:
            raise GitCodeError("upload_url 响应里没有 url（字段：%s）"
                               % sorted(data.keys()))
        if not isinstance(headers, dict):
            headers = {}
        return url, {str(k): str(v) for k, v in headers.items()}

    def put_file(self, url: str, headers: dict, path: Path) -> None:
        with open(path, "rb") as fh:
            resp = self.session.put(url, data=fh, headers=headers,
                                    timeout=UPLOAD_TIMEOUT)
        if resp.status_code >= 400:
            raise GitCodeError("PUT 上传失败：HTTP %d" % resp.status_code)


def upload_asset(api: GitCode, owner: str, repo: str, tag: str,
                 path: Path, name: str, create: bool = False,
                 force: bool = False, body: str = "", log=print) -> str:
    """传一件资产，回直链。同名资产已存在时默认跳过。"""
    release = None
    try:
        release = api.release_by_tag(owner, repo, tag)
    except GitCodeError:
        if not create:
            raise
        log("release %s 不存在，创建它" % tag)
        release = api.create_release(owner, repo, tag, body=body)
    if not force and name in asset_names(release):
        log("同名资产已存在，跳过上传（--force 可强传）")
        return direct_link(owner, repo, tag, name)

    url, headers = api.upload_url(owner, repo, tag, name)
    log("上传 %s（%.1f MB）…" % (name, path.stat().st_size / (1024 * 1024)))
    started = time.time()
    api.put_file(url, headers, path)
    elapsed = time.time() - started
    link = direct_link(owner, repo, tag, name)
    log("上传完成：%.1f 秒，直链 %s" % (elapsed, link))

    status, total = probe_link(api.session, link)
    local = path.stat().st_size
    if total < 0:
        log("直链探测：HTTP %d，服务端没报总长度（可能是源码头，不是附件）"
            % status)
    elif total != local:
        log("直链探测：服务端报 %d 字节，本地 %d 字节" % (total, local))
    else:
        log("直链探测：HTTP %d，%d 字节，与本地一致" % (status, total))
    return link


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把发布资产传到 GitCode Releases")
    ap.add_argument("--repo", default="", help="owner/repo（GitCode 上的公开仓库）")
    ap.add_argument("--tag", default="", help="release 的 tag，例如 v1.1.0")
    ap.add_argument("--file", default="", help="本地文件路径")
    ap.add_argument("--name", default="", help="资产名（默认取文件名）")
    ap.add_argument("--create-release", action="store_true",
                    help="release 不存在时创建（否则报错）")
    ap.add_argument("--body", default="",
                    help="创建 release 时的说明（GitCode 不接受空说明）")
    ap.add_argument("--force", action="store_true",
                    help="同名资产已存在也重传（GitCode 可能改名成 name (1)）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只验令牌与仓库，不做任何写入")
    ap.add_argument("--list", action="store_true", help="列出该 release 的资产")
    ap.add_argument("--edit-body", default="", metavar="FILE",
                    help="只改这条 release 的说明：从文件读正文（要 --tag）")
    ap.add_argument("--check-link", default="",
                    help="只探测一条直链（不需要令牌）")
    args = ap.parse_args(argv)

    if args.check_link:
        session = requests.Session()
        try:
            status, total = probe_link(session, args.check_link)
        except requests.RequestException as exc:
            print("探测失败：%s: %s" % (type(exc).__name__, exc))
            return 1
        if total < 0:
            print("HTTP %d，服务端没报总长度" % status)
            return 1 if status >= 400 else 0
        print("HTTP %d，%d 字节（%.1f MB）" % (status, total, total / (1024 * 1024)))
        return 0

    token = token_from_env()
    if not token:
        print("找不到令牌：先设环境变量 %s（GitCode 设置里的个人访问令牌）"
              % " 或 ".join(TOKEN_ENVS))
        return 2
    if not args.repo or not args.tag:
        print("--repo 与 --tag 都是必填")
        return 2
    try:
        owner, repo = split_repo(args.repo)
    except ValueError as exc:
        print(str(exc))
        return 2

    api = GitCode(token)
    try:
        user = api.current_user()
        info = api.repo_info(owner, repo)
    except GitCodeError as exc:
        print("令牌或仓库有问题：%s" % exc)
        return 1
    print("令牌属于：%s" % (user.get("login") or user.get("username") or "?"))
    print("仓库：%s（%s）" % (info.get("full_name") or args.repo,
                            "私有" if info.get("private") else "公开"))

    if args.list:
        try:
            release = api.release_by_tag(owner, repo, args.tag)
        except GitCodeError as exc:
            print("读 release 失败：%s" % exc)
            return 1
        names = asset_names(release)
        print("资产：%s" % (", ".join(n for n in names if n) or "（空）"))
        return 0

    if args.dry_run:
        print("dry-run：不写任何东西。真传去掉 --dry-run。")
        return 0

    if args.edit_body:
        path = Path(args.edit_body)
        if not path.is_file():
            print("找不到文件：%s" % path)
            return 2
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            print("说明不能是空串")
            return 2
        try:
            api.update_release(owner, repo, args.tag, body=text)
        except GitCodeError as exc:
            print("改说明失败：%s" % exc)
            return 1
        print("已更新说明：%s @ %s" % (args.repo, args.tag))
        return 0

    if not args.file:
        print("--file 是必填")
        return 2
    path = Path(args.file)
    if not path.is_file():
        print("找不到文件：%s" % path)
        return 2
    name = args.name or path.name
    try:
        upload_asset(api, owner, repo, args.tag, path, name,
                     create=args.create_release, force=args.force,
                     body=args.body)
    except GitCodeError as exc:
        print("上传失败：%s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
