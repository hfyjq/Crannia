#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布包：验产物 + 写一条 `version.json`。

    .venv/Scripts/python.exe scripts/make_release.py --check-only
    .venv/Scripts/python.exe scripts/make_release.py --channel stable \
        --url https://cdn.example/crannia-1.0.0.exe

## 它做两件事（顺序不能反）

1. 验产物：安装包在、`runtime.json` 在且 `platform=win32`、依赖清单非空、
   `resources/app` 里那几件东西在。[!] 不能只看 `npm run package` 返回成功 ——
   审计原话：要"检查包里确有 Python 解释器 / 非空依赖清单 / runtime.json"。
2. 写 manifest：算 `sha256` + `size_bytes`，按 `UpdateManifest.from_json`
   认的那个形状落一份 `version-<渠道>.json`。

## 为什么 manifest 是静态文件

GitHub Releases 不能当 manifest 源
（静态文件忽略 `?query`，`latest` 会被 beta 顶掉）-> 两条静态 `version.json`
放 Pages / 对象存储，exe 放 CDN，历史版本走 Releases。

[!] `--url` 必须给真下载地址：manifest 写的是"客户端去哪个 URL 拿安装包"，
   不是本地路径。不给就只验产物、不写 manifest（`--check-only` 的等价行为）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

RELEASE_DIR = _ROOT / "apps" / "desktop" / "release"
UNPACKED = RELEASE_DIR / "win-unpacked"
APP_DIR = UNPACKED / "resources" / "app"
RUNTIME_DIR = UNPACKED / "resources" / "primary-runtime"
OUT_DIR = _ROOT / "dist-release"

#: 安装包的最小体量（字节）。低于它一定是漏了东西（正常约 200 MB）；
#: 给一个宽松但足够抓"空包"的门槛。
MIN_INSTALLER_BYTES = 20 * 1024 * 1024

#: `resources/app`（= 后端那套源码，`extraResources` 的第①条）下必须有的那几件。
#: 缺一个就是后端起不来。
#:
#: 判据是打包配置今天实际写到哪儿（`apps/desktop/electron-builder.config.mjs`）：
#:   壳自己的 `main.cjs` / `package.json` 进的是 `app.asar`（`files` 白名单），
#:   不在 `resources/app` 里 —— 这一格曾经写着它们两个，于是验产物对真产物
#:   永久报"缺文件"，而夹具照着同一份错清单造，测试一直绿。
APP_REQUIRED = ("main.py", "config.yaml", "web/server.py", "core/__init__.py")

#: 壳在 asar 里。它存在且不是空壳就说明 `files` 那一半打进去了；
#: 里面具体有什么由 `tests/test_desktop_package.py` 的白名单读者管。
SHELL_ASAR = "app.asar"
MIN_ASAR_BYTES = 256 * 1024


def find_installer(release_dir: Path = RELEASE_DIR) -> Path | None:
    """找那个安装包（`crannia-*-win-x64.exe`）。多个取最新的一个。"""
    if not release_dir.is_dir():
        return None
    cands = [p for p in release_dir.glob("crannia-*-win-x64.exe") if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def check_artifact(unpacked: Path = UNPACKED,
                   release_dir: Path = RELEASE_DIR) -> list:
    """只读地验一遍产物。返回 `[(问题, 说明), ...]`；空 = 通过。

    [!] 每一条都写成"会怎么坏"，因为这份清单是给发版那一刻看的。
    """
    problems: list = []
    installer = find_installer(release_dir)
    if installer is None:
        problems.append(("安装包", "在 %s 下没找到 crannia-*-win-x64.exe" % release_dir))
    else:
        size = installer.stat().st_size
        if size < MIN_INSTALLER_BYTES:
            problems.append(("安装包太小",
                             "%s 只有 %d 字节（< %d）—— 一定是漏了东西"
                             % (installer.name, size, MIN_INSTALLER_BYTES)))

    runtime = unpacked / "resources" / "primary-runtime"
    #: 解释器在 `primary-runtime/python/` 底下（`prepare_runtime.py` 摊出来的
    #:   那个目录），不是直接躺在 `primary-runtime/` 里。
    py = runtime / "python" / "python.exe"
    if not py.is_file():
        problems.append(("没有 Python 解释器",
                         "缺 %s —— 装完之后后端起不来" % py.relative_to(unpacked)))
    rt_file = runtime / "runtime.json"
    if not rt_file.is_file():
        problems.append(("没有 runtime.json",
                         "缺 %s —— 平台/依赖清单都没写" % rt_file.relative_to(unpacked)))
    else:
        try:
            rt = json.loads(rt_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(("runtime.json 坏了", "%s: %s" % (type(exc).__name__, exc)))
            rt = {}
        if isinstance(rt, dict) and rt:
            if rt.get("platform") != "win32":
                problems.append(("平台不对",
                                 "runtime.json 写着 platform=%r —— 装到 Windows 上是错的"
                                 % (rt.get("platform"),)))
            pkgs = rt.get("pythonPackages")
            if not isinstance(pkgs, dict) or not pkgs:
                problems.append(("依赖清单是空的",
                                 "runtime.json 的 pythonPackages 为空 —— "
                                 "**打出来的包装完跑不起框架**（审计原话）"))

    app = unpacked / "resources" / "app"
    for rel in APP_REQUIRED:
        if not (app / rel).exists():
            problems.append(("resources/app 缺文件", rel))

    asar = unpacked / "resources" / SHELL_ASAR
    if not asar.is_file():
        problems.append(("没有 %s" % SHELL_ASAR,
                         "缺 resources/%s —— 壳（main.cjs / 前端产物）没打进包里，"
                         "双击只会弹报错" % SHELL_ASAR))
    else:
        size = asar.stat().st_size
        if size < MIN_ASAR_BYTES:
            problems.append(("%s 太小" % SHELL_ASAR,
                             "%d 字节（< %d）—— 前端产物大概率没进去"
                             % (size, MIN_ASAR_BYTES)))

    #: 版本号对账读仓根的 `package.json`：它是打进 asar 的那一份的输入
    #:   （asar 里读不出来，也不该为了读它去解 asar）。
    pkg = _ROOT / "package.json"
    if pkg.is_file():
        try:
            want = json.loads(pkg.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            want = None
        try:
            from core.version import __version__
        except Exception:                                     # noqa: BLE001
            __version__ = ""
        if want and __version__ and want != __version__:
            problems.append(("版本号不一致",
                             "package.json=%r vs core/version.py=%r" % (want, __version__)))
    return problems


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(*, version: str, channel: str, url: str, sha256: str,
                   size_bytes: int, published_at: str = "",
                   release_notes_url: str = "",
                   minimum_client_version: str | None = None,
                   mandatory: bool = False, changelog: dict | None = None) -> dict:
    """造 manifest —— 形状与 `UpdateManifest.from_json` 逐字对齐。

    [!] 谁改这里都要跑 `tests/test_release_manifest.py` 里那条回环断言：
      写出来的东西必须能被客户端解析回来（否则"发布成功、客户端永远看不到"）。
    """
    return {
        "latest": {
            "version": str(version),
            "channel": str(channel),
            #: `Z` 是 UTC 的声明，所以时间必须真是 UTC —— 本地时间加 `Z`
            #:   在 UTC+8 上会让清单与 Release 相差 8 小时。
            "published_at": published_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "release_notes_url": release_notes_url,
            "minimum_client_version": minimum_client_version,
            "mandatory": bool(mandatory),
            "changelog": dict(changelog or {}),
            "artifacts": {
                "windows-exe": {
                    "url": str(url),
                    "sha256": str(sha256),
                    "size_bytes": int(size_bytes),
                },
            },
        },
    }


def manifest_path(channel: str, out_dir: Path = OUT_DIR) -> Path:
    return out_dir / ("version-%s.json" % channel)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="验发布产物 + 写 version.json")
    ap.add_argument("--check-only", action="store_true", help="只验产物，不写 manifest")
    ap.add_argument("--channel", default=None, choices=("stable", "beta"),
                    help="默认按版本号推：带预发布后缀（-beta / -rc …）的进 beta，"
                         "否则 stable。预发布版本显式写 --channel stable 会被拒绝。")
    ap.add_argument("--url", default="", help="安装包**对外**的下载地址（https）")
    ap.add_argument("--notes-url", default="")
    ap.add_argument("--minimum-client-version", default=None)
    ap.add_argument("--mandatory", action="store_true")
    ap.add_argument("--published-at", default="",
                    help="这条版本的发布时间（UTC，YYYY-MM-DDTHH:MM:SSZ）。"
                         "默认取当前 UTC；重发清单时给当初发布的那一刻。")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--out", default="",
                    help="直接指定 manifest 路径（例如 gh-pages/stable/version.json）")
    args = ap.parse_args(argv)

    #: 显式给的时间也必须是 UTC 形状 —— 这个字段就是被误用成「本地时间 + Z」的。
    published_at = (args.published_at or "").strip()
    if published_at and not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", published_at):
        print("[release] --published-at 要写成 `YYYY-MM-DDTHH:MM:SSZ`（UTC），实得 %r"
              % published_at)
        return 1

    #: 渠道默认由版本号推（判据在 `core.updater.channel_for_version`，唯一一处）。
    #:   预发布版本显式要求 stable 就拒绝：那正是「Stable 客户端把 Beta 包
    #:   当稳定更新」的入口，不靠人记得改哪个默认值。
    from core.version import __version__
    from core.updater import channel_for_version

    derived = channel_for_version(__version__)
    channel = args.channel or derived
    if args.channel == "stable" and derived != "stable":
        print("[release] 版本 %s 是预发布版本，不能进 stable 渠道。" % __version__)
        print("[release] 去掉 --channel 让它按版本走（= beta），或者先改版本号。")
        return 1

    problems = check_artifact()
    if problems:
        print("[release] 产物不合格 —— %d 条：" % len(problems))
        for what, detail in problems:
            print("  - %s：%s" % (what, detail))
        print("[release] 先修产物，再谈发布。")
        return 1
    installer = find_installer()
    assert installer is not None
    size = installer.stat().st_size
    digest = sha256_of(installer)
    print("[release] 产物 OK：%s（%d 字节）" % (installer.name, size))
    print("[release] sha256 = %s" % digest)

    if args.check_only or not args.url:
        if not args.check_only:
            print("[release] 没给 --url —— 只验产物，不写 manifest。")
        return 0

    man = build_manifest(
        version=__version__, channel=channel, url=args.url,
        sha256=digest, size_bytes=size, release_notes_url=args.notes_url,
        minimum_client_version=args.minimum_client_version,
        mandatory=args.mandatory, published_at=published_at,
    )
    out = (Path(args.out) if args.out
           else manifest_path(channel, Path(args.out_dir)))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[release] 写了 %s（渠道 %s，由版本号推出：%s）"
          % (out, channel, "否" if args.channel else "是"))
    print("[release] 把它传到那一条静态地址上；[!] 另一条渠道的 manifest **不要动**。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
