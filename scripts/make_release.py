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
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: 壳签名只此一份实现（客户端与发布脚本都调它）—— 两处各写一份必然漂。
from core.updater import shell_signature  # noqa: E402

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


#: 代码包里必须有的那一件 —— 客户端解包后拿它判"这是不是我们的代码树"
#:   （`core/updater.py::_extract_codepack` 同一条判据）。
CODEPACK_REQUIRED = "core/version.py"


def _zip_tree(src: Path, out: Path) -> None:
    """把 `src` 整棵打进 zip：条目按路径排序、时间戳固定。

    判据是可复现：同一棵树打两次要逐字节一致，否则 sha256 每次发版都不一样，
      而"这版代码包对应哪版源码"就再也对不上了。
    """
    files = sorted(p for p in src.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            arc = p.relative_to(src).as_posix()
            info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, p.read_bytes())


def build_codepack(version: str, unpacked: Path = UNPACKED,
                   out_dir: Path = OUT_DIR, url: str = "") -> dict | None:
    """造代码包（`resources/app` 整棵），返回 manifest 里 `artifacts.codepack` 那一格。

    代码包只在 `primary-runtime` 与壳（`app.asar`）都没变时可用：这一格带
      运行时的 `payloadDigest` 与壳的 sha256，客户端拿本地那两个去比，
      对不上就回退全量（fail-closed）。
    返回 `None` = 造不出（缺 app 树 / runtime.json / asar）—— 那时 manifest
      里不写 codepack，客户端只能走全量。
    """
    app = unpacked / "resources" / "app"
    rt_file = unpacked / "resources" / "primary-runtime" / "runtime.json"
    asar = unpacked / "resources" / "app.asar"
    if not (app / CODEPACK_REQUIRED).is_file() or not asar.is_file():
        return None
    try:
        rt = json.loads(rt_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    runtime = str(rt.get("payloadDigest") or "") if isinstance(rt, dict) else ""
    if not runtime:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("codepack-%s.zip" % str(version))
    _zip_tree(app, out)
    entry = {
        "url": str(url or ""),
        "sha256": sha256_of(out),
        "size_bytes": out.stat().st_size,
        "app_version": str(version),
        "runtime": runtime,
        #: `shell_sha256` 是整包哈希。asar 里带着 `package.json.version`，
        #:   它对任何跨版本更新必然不同，客户端已改认 `shell_sig`
        #:   （与版本号无关的内容签名）。这一格留给老客户端，别拿它判"壳变没变"。
        "shell_sha256": sha256_of(asar),
        "shell_sig": shell_signature(asar),
        "path": str(out),
    }
    #: 发布到 npm 时，真正发出去的是它打的 tarball，不是本地这个 zip —
    #:   两者的 sha256 不同。这时不写哈希，由 `make_npm_package.py
    #:   --stamp-manifest` 从注册中心把 `integrity` 补回来。
    if str(url or "").lower().split("?")[0].endswith((".tar.gz", ".tgz")):
        entry.pop("sha256", None)
        entry.pop("size_bytes", None)
    return entry


def _with_urls(entry: dict, extra: list | None) -> dict:
    """一件产物多条地址时补上 `urls`（主 → 备，第一条与 `url` 相同）。"""
    urls = [str(entry.get("url") or "").strip()]
    urls += [str(u).strip() for u in (extra or []) if str(u).strip()]
    urls = [u for u in urls if u]
    if len(urls) > 1:
        entry["url"] = urls[0]
        entry["urls"] = urls
    return entry


def build_manifest(*, version: str, channel: str, url: str, sha256: str,
                   size_bytes: int, published_at: str = "",
                   release_notes_url: str = "",
                   minimum_client_version: str | None = None,
                   mandatory: bool = False, changelog: dict | None = None,
                   codepack: dict | None = None,
                   tui: dict | None = None,
                   urls: list | None = None,
                   codepack_urls: list | None = None,
                   tui_urls: list | None = None) -> dict:
    """造 manifest —— 形状与 `UpdateManifest.from_json` 逐字对齐。

    [!] 谁改这里都要跑 `tests/test_release_manifest.py` 里那条回环断言：
      写出来的东西必须能被客户端解析回来（否则"发布成功、客户端永远看不到"）。
    """
    asset = _with_urls({
        "url": str(url),
        "sha256": str(sha256),
        "size_bytes": int(size_bytes),
    }, urls)
    artifacts: dict = {"windows-exe": asset}
    if codepack:
        cp = {k: v for k, v in codepack.items() if k != "path"}
        artifacts["codepack"] = _with_urls(cp, codepack_urls)
    #: TUI 的代码包（2026-11-21）：GUI 的更新器不认识这个键（它只读
    #:   `windows-exe` / `codepack`），TUI 的 `crannia update` 只读它。
    if tui:
        artifacts["tui"] = _with_urls({k: v for k, v in tui.items() if k != "path"},
                                      tui_urls)
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
            "artifacts": artifacts,
        },
    }


def manifest_path(channel: str, out_dir: Path = OUT_DIR) -> Path:
    return out_dir / ("version-%s.json" % channel)


def _tree_version(unpacked: Path = UNPACKED) -> str:
    """打包树里 `resources/app/core/version.py` 的版本；读不到回空串。

    清单的版本来自项目根的 `core/version.py`，与打包树里那一份不是同一个
    文件。版本号改过而没重新打包时两者会不同，那正是"清单描述的不是要发的
    那个包"。
    """
    p = unpacked / "resources" / "app" / "core" / "version.py"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)', text, re.M)
    return m.group(1) if m else ""


def _artifact_local_path(art: dict, out_dir: Path, fallback: str) -> Path:
    """清单里那件产物在本机的落点：先按地址的文件名找，再试约定名。

    两个都可能对：上传时改名是允许的，而 npm tarball 那条地址与本地 zip
    本来就不是同一个文件（本地那份只用来算哈希）。
    """
    url = str((art or {}).get("url") or "")
    names = []
    if url:
        names.append(PurePosixPath(urlsplit(url).path).name)
    names.append(fallback)
    for name in names:
        if name and (out_dir / name).is_file():
            return out_dir / name
    return out_dir / (names[0] or fallback)


def _https_ok(url: str) -> bool:
    return (str(url or "").startswith("https://")
            or str(url or "").startswith("http://localhost")
            or str(url or "").startswith("http://127.0.0.1"))


def verify_manifest(man: dict, *, unpacked: Path = UNPACKED,
                    out_dir: Path = OUT_DIR) -> list[str]:
    """清单与产物的逐项一致性。返回问题列表，空列表 = 通过。

    判据是"清单描述的就是即将上传的那几件文件"。仓库里同时躺着多份不同构建
    是常态（2026-11-22 有三份不同的 1.1.0 安装包），所以这一条不能靠人看。
    """
    problems: list[str] = []
    latest = (man or {}).get("latest") or {}
    version = str(latest.get("version") or "")
    if not version:
        return ["清单里没有 latest.version"]
    arts = latest.get("artifacts") or {}

    exe = arts.get("windows-exe") or {}
    exe_path = _artifact_local_path(exe, out_dir,
                                    "crannia-%s-win-x64.exe" % version)
    if not exe_path.is_file():
        problems.append("找不到安装包：%s" % exe_path)
    else:
        if str(exe.get("sha256") or "") != sha256_of(exe_path):
            problems.append("安装包的 sha256 与清单不一致：%s" % exe_path.name)
        if int(exe.get("size_bytes") or 0) != exe_path.stat().st_size:
            problems.append("安装包的 size_bytes 与清单不一致：%s" % exe_path.name)

    tree_ver = _tree_version(unpacked)
    if tree_ver and tree_ver != version:
        problems.append("打包树里的版本是 %s，清单写的是 %s"
                        % (tree_ver, version))

    cp = arts.get("codepack")
    if isinstance(cp, dict):
        if str(cp.get("app_version") or "") != version:
            problems.append("代码包的 app_version 与清单版本不一致")
        rt_file = unpacked / "resources" / "primary-runtime" / "runtime.json"
        try:
            rt = json.loads(rt_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rt = {}
        want_rt = str((rt or {}).get("payloadDigest") or "") if isinstance(rt, dict) else ""
        if str(cp.get("runtime") or "") != want_rt:
            problems.append("代码包的 runtime 与打包树的 runtime.json 不一致")
        sig = shell_signature(unpacked / "resources" / "app.asar")
        if not sig:
            problems.append("算不出打包树的壳签名（app.asar 读不到或不是 asar）")
        elif str(cp.get("shell_sig") or "") != sig:
            problems.append("代码包的 shell_sig 与打包树的 asar 不一致")
        #: 目标是 npm tarball 时本地没有那份哈希（由注册中心补），有就核。
        if cp.get("sha256"):
            pack = _artifact_local_path(cp, out_dir,
                                        "codepack-%s.zip" % version)
            if not pack.is_file():
                problems.append("找不到代码包：%s" % pack)
            elif sha256_of(pack) != str(cp.get("sha256")):
                problems.append("代码包的 sha256 与清单不一致：%s" % pack.name)

    tui = arts.get("tui")
    if isinstance(tui, dict):
        p = _artifact_local_path(tui, out_dir,
                                 "crannia-tui-%s-code.zip" % version)
        if not p.is_file():
            problems.append("找不到 TUI 代码包：%s" % p)
        else:
            if sha256_of(p) != str(tui.get("sha256") or ""):
                problems.append("TUI 代码包的 sha256 与清单不一致：%s" % p.name)
            if int(tui.get("size_bytes") or 0) != p.stat().st_size:
                problems.append("TUI 代码包的 size_bytes 与清单不一致：%s" % p.name)

    for key, art in arts.items():
        if not isinstance(art, dict):
            continue
        for u in [art.get("url")] + list(art.get("urls") or []):
            if u and not _https_ok(str(u)):
                problems.append("%s 的地址不是 https：%s" % (key, u))
    return problems


def _http_json(url: str) -> dict:
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": "Crannia-release-gate",
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def verify_published(man: dict, fetch=None) -> list[str]:
    """上传之后回读发布资产，核对 size/digest 与清单一致。

    只认 GitHub Releases 的资产地址（当前的发布形态）。别的宿主返回一条
    "没法核对"，不假装通过。`fetch` 只给测试注入。
    """
    problems: list[str] = []
    latest = (man or {}).get("latest") or {}
    arts = latest.get("artifacts") or {}
    exe = arts.get("windows-exe") or {}
    candidates = ([str(exe.get("url") or "")]
                  + [str(u) for u in (exe.get("urls") or [])])
    url = next((u for u in candidates
                if (urlsplit(u).hostname or "").lower() == "github.com"), "")
    parts = urlsplit(url)
    m = re.match(r"^/([^/]+)/([^/]+)/releases/download/([^/]+)/", parts.path or "")
    if not url or not m:
        return ["清单里没有 GitHub Releases 地址，没法回读：%r" % (candidates,)]
    api = "https://api.github.com/repos/%s/%s/releases/tags/%s" % (
        m.group(1), m.group(2), m.group(3))
    try:
        data = (fetch or _http_json)(api)
    except Exception as exc:                              # noqa: BLE001
        return ["回读失败：%s: %s" % (type(exc).__name__, exc)]
    assets = {str(a.get("name")): a for a in (data.get("assets") or [])
              if isinstance(a, dict)} if isinstance(data, dict) else {}
    for key, art in arts.items():
        if not isinstance(art, dict):
            continue
        for u in [art.get("url")] + list(art.get("urls") or []):
            if not u:
                continue
            name = PurePosixPath(urlsplit(str(u)).path).name
            got = assets.get(name)
            if got is None:
                continue
            if int(got.get("size") or 0) != int(art.get("size_bytes") or 0):
                problems.append("%s：发布资产的 size 与清单不一致（%s vs %s）"
                                % (name, got.get("size"), art.get("size_bytes")))
            digest = str(got.get("digest") or "").lower()
            want = ("sha256:%s" % str(art.get("sha256") or "")).lower()
            if digest and want != "sha256:" and digest != want:
                problems.append("%s：发布资产的 digest 与清单不一致" % name)
            break
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="验发布产物 + 写 version.json")
    ap.add_argument("--check-only", action="store_true", help="只验产物，不写 manifest")
    ap.add_argument("--channel", default=None, choices=("stable", "beta"),
                    help="默认按版本号推：带预发布后缀（-beta / -rc …）的进 beta，"
                         "否则 stable。预发布版本显式写 --channel stable 会被拒绝。")
    ap.add_argument("--url", default="", help="安装包**对外**的下载地址（https）")
    ap.add_argument("--codepack-url", default="",
                    help="代码包（几 MB）**对外**的下载地址（https）。给了才在 manifest "
                         "里写 `artifacts.codepack`；不给就只有全量安装包。")
    ap.add_argument("--url-fallback", action="append", default=[],
                    help="安装包的备用地址（可重复）。客户端主源失败时按序切。")
    ap.add_argument("--codepack-url-fallback", action="append", default=[],
                    help="代码包的备用地址（可重复）。")
    ap.add_argument("--tui-url", default="",
                    help="TUI 代码包（几 MB）的对外下载地址。给了才在 manifest "
                         "里写 `artifacts.tui`。")
    ap.add_argument("--tui-url-fallback", action="append", default=[],
                    help="TUI 代码包的备用地址（可重复）。")
    ap.add_argument("--tui-file", default="",
                    help="本地 TUI 代码包（默认 dist-release/crannia-tui-<版本>-code.zip）")
    ap.add_argument("--notes-url", default="")
    ap.add_argument("--minimum-client-version", default=None)
    ap.add_argument("--mandatory", action="store_true")
    ap.add_argument("--published-at", default="",
                    help="这条版本的发布时间（UTC，YYYY-MM-DDTHH:MM:SSZ）。"
                         "默认取当前 UTC；重发清单时给当初发布的那一刻。")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--out", default="",
                    help="直接指定 manifest 路径（例如 gh-pages/stable/version.json）")
    ap.add_argument("--verify-published", nargs="?", const="__default__",
                    default="",
                    help="发布之后：读这份清单，回读 GitHub Release 资产的 "
                         "size/digest 与它比对（不给路径则读 <out-dir>/version-<channel>.json）")
    args = ap.parse_args(argv)

    if args.verify_published:
        path = Path(args.verify_published)
        if args.verify_published == "__default__":
            from core.version import __version__ as _ver
            from core.updater import channel_for_version as _chan
            path = manifest_path(args.channel or _chan(_ver),
                                 Path(args.out_dir))
        try:
            man = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print("[release] 读不到清单 %s：%s" % (path, exc))
            return 1
        problems = verify_published(man)
        if problems:
            print("[release] 发布资产与清单对不上：")
            for p in problems:
                print("  - " + p)
            return 1
        print("[release] 发布资产与清单一致：%s" % path)
        return 0

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

    codepack = None
    if args.codepack_url:
        codepack = build_codepack(__version__, UNPACKED, Path(args.out_dir),
                                  args.codepack_url)
        if codepack is None:
            print("[release] 给了 --codepack-url，但造不出代码包"
                  "（缺 resources/app/core/version.py · runtime.json · app.asar）")
            return 1
        print("[release] 代码包：%s（%d 字节）" % (codepack["path"],
                                                 Path(codepack["path"]).stat().st_size))
        if "sha256" in codepack:
            print("[release] sha256 = %s" % codepack["sha256"])
            print("[release] 把它传到 --codepack-url 那个地址上。")
        else:
            #: 目标是 npm tarball：真正发出去的是 npm 打的包，本地这个 zip
            #:   与它字节不同，哈希/大小由 `make_npm_package.py --stamp-manifest`
            #:   发布后从注册中心补。
            print("[release] 目标是 npm tarball —— 哈希与大小发布后由 "
                  "`make_npm_package.py --stamp-manifest` 补进备源清单。")

    tui = None
    if args.tui_url:
        tui_path = (Path(args.tui_file) if args.tui_file
                    else Path(args.out_dir) / ("crannia-tui-%s-code.zip" % __version__))
        if not tui_path.is_file():
            print("[release] 给了 --tui-url，但找不到 TUI 代码包：%s" % tui_path)
            print("[release] 先跑 scripts/make_tui_package.py，或用 --tui-file 指过去。")
            return 1
        tui = {"url": args.tui_url, "sha256": sha256_of(tui_path),
               "size_bytes": tui_path.stat().st_size}
        print("[release] TUI 代码包：%s（%d 字节）"
              % (tui_path.name, tui["size_bytes"]))
        print("[release] sha256 = %s" % tui["sha256"])
        print("[release] 把它传到 --tui-url 那个地址上。")

    man = build_manifest(
        version=__version__, channel=channel, url=args.url,
        sha256=digest, size_bytes=size, release_notes_url=args.notes_url,
        minimum_client_version=args.minimum_client_version,
        mandatory=args.mandatory, published_at=published_at,
        codepack=codepack,
        tui=tui,
        urls=args.url_fallback,
        codepack_urls=args.codepack_url_fallback,
        tui_urls=args.tui_url_fallback,
    )
    mismatches = verify_manifest(man, unpacked=UNPACKED,
                                 out_dir=Path(args.out_dir))
    if mismatches:
        print("[release] 清单与产物对不上，不写：")
        for p in mismatches:
            print("  - " + p)
        print("[release] 这一条挡的是"
              "「清单描述的不是即将上传的那个包」。")
        return 1

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
