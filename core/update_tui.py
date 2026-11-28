"""TUI 的更新检查：读同一份清单里的 `artifacts.tui`，下代码包。

    python -m core.update_tui --dest <目录> [--channel stable|beta]

## 它为什么在 core 里

清单的读取（两条静态地址 + npm 源适配）只有一份实现（`core.updater`）。
TUI 的命令行不 import core（那是它的既有约定：动账本、读清单都走子进程），
所以这里做成一个能单独跑的子进程入口，TUI 那一侧只解析这道 JSON：

    {"ok": true, "version": "1.1.0", "path": "...", "sha256": "..."}
    {"ok": true, "up_to_date": true, "version": "1.1.0"}
    {"ok": false, "error": "..."}

## 只认 `artifacts.tui`

GUI 的清单里 `artifacts.windows-exe` 是安装包、`artifacts.codepack` 是
GUI 的代码包；TUI 的代码包是第三件。没有这一件就说清「这一版还没有 TUI
包」，不回退去下那个几百兆的安装包 —— 那些字节对 TUI 没用。

## 版本比较

与客户端同一套（`core.updater._parse_version`）：正式版补位次 1、预发布
补位次 0，所以 `1.1.1-beta < 1.1.1 < 1.1.2-alpha`。渠道默认由当前版本推
（预发布查 beta，其余查 stable）。

## 摘要缺失就拒绝（fail-closed）

清单里的 `artifacts.tui.sha256` 必须存在且是 64 位十六进制；缺了或格式
不对，这一条源就不算可用。理由：没有摘要的下载等于「装什么由网络说了算」，
而安装之后就没有第二次验收。检查在两条路上都做 —— `plan()` 拿不到合法
摘要就不报「有更新」，`_download()` 自己也不接受空摘要。

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
from pathlib import Path

from .updater import (UpdateChecker, _parse_version, channel_for_version,
                      log_update)
from .version import __version__

_TIMEOUT = 60

#: 摘要的形状是判据的一部分：64 位十六进制，别的都不算。
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_UA_PLATFORM = {"win32": "Windows", "linux": "Linux", "darwin": "macOS"}.get(
    sys.platform, platform.system() or sys.platform)


def _valid_sha256(value) -> bool:
    return bool(_SHA256_RE.match(str(value or "").strip()))


def _artifacts_tui(data: dict) -> dict | None:
    latest = data.get("latest") if isinstance(data, dict) else None
    if not isinstance(latest, dict):
        latest = data if isinstance(data, dict) else {}
    art = (latest.get("artifacts") or {}).get("tui")
    if not isinstance(art, dict):
        return None
    urls = art.get("urls")
    url_list = ([str(u).strip() for u in urls if str(u).strip()]
                if isinstance(urls, (list, tuple)) else [])
    if not url_list and str(art.get("url") or "").strip():
        url_list = [str(art["url"]).strip()]
    if not url_list:
        return None
    return {
        "version": str(latest.get("version") or "").strip(),
        "urls": url_list,
        "sha256": str(art.get("sha256") or "").strip(),
        "size_bytes": int(art.get("size_bytes") or 0),
    }


def plan(channel: str | None = None, *, checker=None) -> dict:
    """问一次清单。返回给 CLI 的那份字典（不抛网络异常）。

    `checker` 只给测试用：默认按渠道造一个 `UpdateChecker`。
    """
    ch = channel or channel_for_version(__version__)
    checker = checker or UpdateChecker(channel=ch)
    problems: list = []
    for source in checker.manifest_urls:
        try:
            data = checker._fetch_manifest(source)
        except Exception as exc:  # noqa: BLE001 - 一条源坏了就试下一条
            problems.append("%s: %s" % (source, exc))
            continue
        tui = _artifacts_tui(data)
        if tui is None:
            problems.append("%s: 清单里没有 artifacts.tui" % source)
            continue
        if not _valid_sha256(tui.get("sha256")):
            problems.append("%s: artifacts.tui 没有合法的 sha256（64 位十六进制）"
                            % source)
            continue
        remote = tui["version"]
        if not remote:
            problems.append("%s: 清单没有 version" % source)
            continue
        if _parse_version(remote) <= _parse_version(__version__):
            return {"ok": True, "up_to_date": True, "version": remote}
        return {"ok": True, "up_to_date": False, "version": remote,
                "urls": tui["urls"], "sha256": tui["sha256"],
                "size_bytes": tui["size_bytes"]}
    return {"ok": False,
            "error": "；".join(problems) or "没有可用的清单地址"}


def _download(urls: list, dest: Path, sha256: str) -> tuple[bool, str]:
    """按顺序试每一条地址；下完核哈希。回 `(成功, 说明)`。"""
    import requests

    from core import net_proxy as _np

    #: 摘要缺失/格式不对就不下 —— 下了也没法验收（见模块头）。
    if not _valid_sha256(sha256):
        return False, "清单缺少合法的 sha256 —— 拒绝安装未经校验的包"
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for url in urls:
        try:
            with requests.get(url, timeout=_TIMEOUT, stream=True,
                              headers={"User-Agent": "%s/%s (%s)"
                                       % ("Crannia", __version__, _UA_PLATFORM)},
                              proxies=_np.requests_proxies()) as r:
                r.raise_for_status()
                h = hashlib.sha256()
                with part.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if not chunk:
                            continue
                        f.write(chunk)
                        h.update(chunk)
            got = h.hexdigest()
            if sha256 and got.lower() != sha256.lower():
                part.unlink(missing_ok=True)
                return False, "sha256 对不上（清单 %s，实际 %s）" % (sha256[:16], got[:16])
            part.replace(dest)
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 换下一个地址
            last = exc
            part.unlink(missing_ok=True)
    return False, "下载失败：%s" % last


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TUI 更新检查与下载")
    ap.add_argument("--dest", default="", help="下载到哪个目录（不给则只检查）")
    ap.add_argument("--channel", default="", choices=("", "stable", "beta"))
    args = ap.parse_args(argv)

    result = plan(args.channel or None)
    if not result.get("ok") or result.get("up_to_date") or not args.dest:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    dest = Path(args.dest).expanduser()
    target = dest / ("crannia-tui-%s-code.zip" % result["version"])
    ok, why = _download(result["urls"], target, result.get("sha256", ""))
    if not ok:
        print(json.dumps({"ok": False, "error": why}, ensure_ascii=False))
        return 1
    log_update("tui: downloaded %s from manifest v%s" % (target.name, result["version"]))
    print(json.dumps({"ok": True, "version": result["version"],
                      "path": str(target),
                      "sha256": result.get("sha256", "")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
