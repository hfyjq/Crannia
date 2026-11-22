"""
Crannia auto-updater — check, download, verify, install.

Architecture:
  - UpdateChecker: fetches manifest, compares versions, downloads, verifies
  - UpdateInstaller: generates PowerShell self-replace script + triggers install
  - Global state: _update_state dict shared with Web API for progress polling

Flow:
  check() -> UpdateManifest | None
  download(manifest, progress_cb) -> Path  (saves to data/updates/)
  install(manifest, downloaded_path)       (spawns PS script + exits)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

import requests

# Project-root-relative; adjust if updater is imported from a different CWD.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = Path("data")
_UPDATES_DIR = _DATA_DIR / "updates"

#: 下好、等着下次启动装上的那一份（`{version, path, sha256, at}`）。
#: 壳在起后端之前读它 —— 那一刻还没有任何接口可问，所以必须是磁盘上的
#: 一个文件，不是 `_update_state`（那个活在进程里）。
_PENDING_FILE = _UPDATES_DIR / "pending.json"

#: 更新链的事件日志（检查失败 / 下载重试 / 装不上 —— 都落这里）。
#: 判据：`data/updates/` 已经是这条链的家（`pending.json` 与 `.part` 都在），
#:   而"为什么没更新"必须读得到 —— 2026-11-14 真机那次失败一个字都没留下。
_LOG_FILE = _UPDATES_DIR / "update.log"

#: 日志有界（留尾部）。它是排障用的"最近一次为什么失败"，不是归档 ——
#:   无界日志会长成用户盘上第二份没人清理的东西。
_MAX_LOG_BYTES = 256 * 1024

#: 两级更新里的代码包：下好、解到 `<resources>/app.new`，等下次启动由壳换目录。
#:   与 `pending.json`（全量安装包）分开两个文件 —— 壳在启动最前面按 kind 分派，
#:   而两种的落地动作完全不同（一个是跑安装器，一个是 rename 目录）。
_PENDING_CODEPACK_FILE = _UPDATES_DIR / "pending_codepack.json"

# Global state (shared with web/server.py)
_update_state: dict = {
    "status": "idle",        # idle | checking | downloading | verifying | ready | error
    "progress": 0.0,         # 0.0 - 1.0
    "error": None,           # str | None
    "latest_version": None,  # str | None
    "latest_manifest": None, # dict | None (raw manifest JSON)
    "downloaded_path": None, # str | None
    #: 当前这一段（downloading / verifying）从什么时候开始 —— 前端给校验阶段计时用。
    "phase_started": 0.0,
    #: 这一轮下的是哪一级：`codepack`（几 MB，下次启动换目录）或 `full`（安装包）。
    "kind": None,
    #: 更新源还是占位域名，根本没发请求（与"查过了、没新版本"必须分开）。
    "unconfigured": False,
    #: 这次检查实际用的是哪一条源（主源失败自动切备源时，用户要看得见）。
    "source": None,
}

_UPDATING = False  # set to True before triggering install; health-check daemon respects this


# Data classes

@dataclass
class UpdateManifest:
    """Parsed update manifest from the remote version.json."""
    version: str
    channel: str
    published_at: str
    release_notes_url: str
    download_url: str
    sha256: str
    size_bytes: int
    minimum_client_version: str | None = None
    mandatory: bool = False
    changelog: dict = field(default_factory=dict)
    #: 同一件产物的候选地址（主 → 备）。空的 = 只有 `download_url` 一条。
    download_urls: list = field(default_factory=list)
    #: 两级更新里的"代码包"那一截（`artifacts.codepack`）。
    #: 空 dict = 这一版没有代码包，只能走全量安装包。
    codepack: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> UpdateManifest | None:
        """Parse the manifest JSON.  Returns None if the format is invalid."""
        try:
            latest = data.get("latest", data)  # tolerate both wrapped & flat
            artifacts = latest.get("artifacts", {})
            #: 代码包是可选的一件：它不参与 `asset` 的挑选 ——
            #:   没有一个叫 windows-exe 的安装包时仍然不能把 codepack 当它。
            raw_cp = artifacts.get("codepack")
            codepack = dict(raw_cp) if isinstance(raw_cp, dict) else {}
            # Prefer windows-exe, fall back to first artifact
            asset = artifacts.get("windows-exe") or artifacts.get("windows_portable")
            if asset is None and artifacts:
                #: 挑第一个"不是代码包"的（上面那两种情况之外）。
                others = [v for k, v in artifacts.items()
                          if k != "codepack" and isinstance(v, dict)]
                asset = others[0] if others else None
            if asset is None:
                return None
            #: 一件产物可以给多个地址（主源被墙时自动切备源）。
            #:   单数 `url` 继续认 —— 老清单与手写的清单都只有它。
            raw_urls = asset.get("urls")
            url_list = ([str(u).strip() for u in raw_urls if str(u).strip()]
                        if isinstance(raw_urls, (list, tuple)) else [])
            if not url_list:
                url_list = [str(asset["url"])]
            return cls(
                version=latest["version"],
                #: 缺省是空串，不是 `stable` —— "没声明渠道"与"声明了 stable"
                #:   是两件事，而 `check()` 只放行一致的那一种（见下）。
                channel=str(latest.get("channel") or "").strip().lower(),
                published_at=latest.get("published_at", ""),
                release_notes_url=latest.get("release_notes_url", ""),
                download_url=url_list[0],
                sha256=asset.get("sha256", ""),
                size_bytes=asset.get("size_bytes", 0),
                minimum_client_version=latest.get("minimum_client_version"),
                mandatory=latest.get("mandatory", False),
                changelog=latest.get("changelog", {}),
                download_urls=url_list,
                codepack=codepack,
            )
        except (KeyError, TypeError):
            return None


# Helpers

#: 版本串：`v1.1.1` / `1.1.1-beta` / `1.1.1-beta.2` / `1.1.1+build5`
_VERSION_RE = re.compile(
    r"^v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z][0-9A-Za-z.-]*))?(?:\+[0-9A-Za-z.-]+)?$")


def _parse_version(v: str) -> tuple[int, ...]:
    """版本号 → 可比较的元组。预发布排在正式版之前。

    判据：
      `1.1.1-beta`  <  `1.1.1`        （同一个号，测试版先于正式版）
      `1.1.1-beta.2` > `1.1.1-beta`   （同一版本的递增测试版）
      `1.1.1-beta`  <  `1.1.2-alpha`  （数字位更高就一定更新）

    实现：数字段逐个 `int`，然后追加一个位次标记：

        正式版   `1.1.1`        → (1, 1, 1, 1)
        预发布   `1.1.1-beta`   → (1, 1, 1, 0, 0)
        预发布   `1.1.1-beta.2` → (1, 1, 1, 0, 0, 2)

    不能只追加一个更小的数（比如 `-1`）：元组比较里"更短的前缀更小"，
      `(1,1,1)` 是 `(1,1,1,-1)` 的前缀，那样算出来 beta 大于 正式版，正好反了。
正式版要给一个比预发布标记更大的位次，两侧都带尾巴才比得对。
    后缀里的数字再按段追加（`beta.2` > `beta.1`）。
    认不出，`(0,)`（与旧行为一致）：读不到 ≠ 报错，也 ≠ 当成最新
    —— 后者会让一个手滑的串把"有新版本"永远点亮。
    """
    try:
        m = _VERSION_RE.match(str(v or "").strip())
    except TypeError:
        return (0,)
    if not m:
        return (0,)
    try:
        nums = tuple(int(x) for x in m.group(1).split("."))
    except ValueError:
        return (0,)
    pre = m.group(2)
    if not pre:
        return nums + (1,)          # 正式版：位次 1
    parts = pre.split(".")
    #: 预发布标签的先后（标准 semver 顺序）。认不出的标签按 `beta` 算 ——
    #: 写下来而不是静默：它决定"两个不同标签谁先"，而我们只打算用 `beta`。
    rank = {"alpha": 0, "pre": 0, "beta": 1, "rc": 2}.get(
        re.match(r"[A-Za-z]+", parts[0]).group(0).lower()
        if re.match(r"[A-Za-z]+", parts[0]) else "", 1)
    tail = [0, rank]                # 预发布：位次 0（比正式版小）+ 标签序
    m2 = re.match(r"^[A-Za-z]+(\d+)$", parts[0])
    if m2:                          # `beta1` 这种连写
        tail.append(int(m2.group(1)))
    for part in parts[1:]:
        tail.append(int(part) if part.isdigit() else 0)
    return nums + tuple(tail)


def _read_settings() -> dict:
    """Read data/settings.json (lightweight, no dependency on web.server)."""
    settings_path = _DATA_DIR / "settings.json"
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_settings(settings: dict) -> None:
    """Write data/settings.json."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_DATA_DIR / "settings.json", "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def log_update(message: str) -> None:
    """往 `data/updates/update.log` 追一行（带时间戳；写不进去静默）。

    读它的人：设置页「更新日志」与用户自己。不抛异常 ——
      这条链的任何一处都不该因为"日志写不了"把更新本身拦下来。
    """
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                        str(message).replace("\n", " ").strip())
    try:
        _UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        try:
            size = _LOG_FILE.stat().st_size
        except OSError:
            size = 0
        if size > _MAX_LOG_BYTES:
            #: 只留尾部一半再追加。每写一行就砍一次的话，截断点会一直往前挪。
            tail = _LOG_FILE.read_bytes()[-(_MAX_LOG_BYTES // 2):]
            _LOG_FILE.write_bytes(tail + line.encode("utf-8"))
        else:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def read_update_log(max_chars: int = 20000) -> str:
    """日志尾部（给设置页看）。读不到给空串 —— 界面据此说"还没有"。"""
    try:
        text = _LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:] if max_chars else text


def _mark_phase(status: str) -> None:
    """改状态的同时记下"这一段从什么时候开始"（界面给校验阶段计时）。"""
    _update_state["status"] = status
    _update_state["phase_started"] = time.time()


def _replace_with_retry(src: Path, dst: Path) -> None:
    """`Path.replace` 带重试 —— Windows 上杀软会短暂占住刚下好的文件。

    现场形状（2026-11-22 报障）：状态已经是 `verifying`，`replace` 抛
    `PermissionError`（WinError 32/5），下载线程直接死掉，界面就永远停在
    「正在校验安装包…」，日志里一个字都没有。重试三次，每次失败都写日志。
    """
    delays = (0.5, 2.0, 5.0)
    for i in range(len(delays) + 1):
        try:
            src.replace(dst)
            return
        except PermissionError as exc:
            log_update("replace attempt %d/%d failed: %s"
                       % (i + 1, len(delays) + 1, exc))
            if i >= len(delays):
                raise
            time.sleep(delays[i])


#: 同一时刻只许一个下载：两个线程写同一个 `.part` 会互相抢文件
#:   （WinError 32 的一种来源），也会让进度与状态互相覆盖。
_DOWNLOAD_LOCK = threading.Lock()


def download_thread_entry(manifest, checker=None) -> None:
    """后台下载线程的唯一入口：任何异常都落成 `error`。

    此前 `web/server.py` 的线程体是裸调用 —— 校验/落地那一抛，线程死掉，
    `_update_state["status"]` 永远停在 `verifying`，而界面只看这个状态。
    自动检查与手动下载都可能起线程，所以这里还要串行化。
    """
    checker = checker or UpdateChecker()
    if not _DOWNLOAD_LOCK.acquire(blocking=False):
        log_update("download already running; this request is skipped")
        return
    try:
        checker.download_best(manifest)
    except Exception as exc:                              # noqa: BLE001
        _update_state["status"] = "error"
        _update_state["error"] = "下载线程异常：%s" % exc
        log_update("download thread crashed: %s: %s"
                   % (type(exc).__name__, exc))
    finally:
        _DOWNLOAD_LOCK.release()


def _read_config() -> dict:
    """Read config.yaml (lightweight)."""
    import yaml
    from core import paths as _paths
    config_path = _paths.config_path()
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


# UpdateChecker

#: 每条渠道一组候选源，按顺序试，第一个成功即用。
#:
#: 主源是 npm metadata（`npm:<包名>@<dist-tag>`）：清单写在包 `package.json`
#:   的 `crannia` 字段里。走 `registry.npmmirror.com` —— 国内可达；
#:   GitHub Pages 在国内经常超时（2026-11-15 两次真机检查都失败）。
#: 备源是 `gh-pages` 分支根下的静态 JSON —— 与源码分支分开，
#:   免得把整棵源码树当网站发出去（GitHub Releases 不能当清单源：
#:   静态文件忽略 `?query`，而 `latest` 会被 beta 顶掉）。
#:
#: 可用配置覆盖（优先级见 `UpdateChecker.manifest_urls`）：
#:   `update.manifest_urls.<渠道>`（字符串或列表），老的单值
#:   `update.manifest_url` 仍然认（两条渠道都用它，向后兼容）。
DEFAULT_MANIFEST_URLS: dict[str, tuple[str, ...]] = {
    "stable": (
        "npm:@hfyjq/crannia@latest",
        "https://hfyjq.github.io/Crannia/stable/version.json",
    ),
    "beta": (
        "npm:@hfyjq/crannia@beta",
        "https://hfyjq.github.io/Crannia/beta/version.json",
    ),
}

#: 只有这两条渠道。"认不出的值按 stable 算" —— 写下来，不静默。
VALID_CHANNELS: tuple[str, ...] = ("stable", "beta")


def is_configured(url: str) -> bool:
    """这个 URL 是不是真的更新源。

    占位域名（`*.example`）等于"还没配"，不发请求。
      否则每台新装机器每天都会向一个不存在的域名发一次请求，静默失败
      —— 那看起来像"检查过了，没有新版本"（两者长得一模一样）。
    """
    u = str(url or "").strip().lower()
    return bool(u) and ".example" not in u


def _is_local_dev_url(url: str) -> bool:
    """本机调试地址（唯一允许非 HTTPS 的地方）。全仓只此一处判据。"""
    return (str(url or "").startswith("http://localhost")
            or str(url or "").startswith("http://127.0.0.1"))


def _is_dev_https_ok(url: str) -> bool:
    """HTTPS 是底线；只有本机调试地址能用 http。"""
    return str(url or "").startswith("https://") or _is_local_dev_url(url)


def normalise_channel(value) -> str:
    """渠道名归一：只认 `stable` / `beta`，其余（空 / 拼错 / None），`stable`。"""
    v = str(value or "").strip().lower()
    return v if v in VALID_CHANNELS else "stable"


def channel_for_version(version: str) -> str:
    """版本串 → 该进哪条渠道：带预发布后缀（`-beta` / `-rc` …）的进 `beta`。

    判据只此一处 —— `scripts/make_release.py` 与 `scripts/release_gate.py`
    都调它。「版本是 `1.0.0-beta.1`、渠道却写成 `stable`」这条，
    没有第二个能写反的地方（写反的后果：Stable 客户端把 Beta 包当稳定更新）。
    """
    m = _VERSION_RE.match(str(version or "").strip())
    if m and m.group(2):
        return "beta"
    return "stable"


def manifest_urls_for(channel, config: dict | None = None) -> list[str]:
    """给定渠道，解析它的候选源列表（纯函数，配置由调用方交进来）。

    配置可以写字符串，也可以写列表（主 → 备），空项丢掉。
    一条都没有时落回 `DEFAULT_MANIFEST_URLS`。
    """
    ch = normalise_channel(channel)
    upd = (config or {}).get("update", {}) if isinstance(config, dict) else {}
    urls = upd.get("manifest_urls") if isinstance(upd.get("manifest_urls"), dict) else {}
    raw = urls.get(ch)
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        out = [str(u).strip() for u in raw if str(u).strip()]
    elif raw:
        out = [str(raw).strip()]
    if not out:
        legacy = str(upd.get("manifest_url") or "").strip()
        if legacy:
            out = [legacy]
    return out or list(DEFAULT_MANIFEST_URLS[ch])


def manifest_url_for(channel, config: dict | None = None) -> str:
    """主源（列表的第一条）。留给只关心一条的调用方。"""
    return manifest_urls_for(channel, config)[0]


#: `npm:<包名>@<dist-tag>` —— 清单住在 npm 包的 `package.json.crannia` 里。
_NPM_SOURCE_PREFIX = "npm:"

#: npm 的国内镜像（发布仍走官方源，读取走镜像）。
_NPM_REGISTRY = "https://registry.npmmirror.com"


def parse_npm_source(url: str) -> tuple[str, str] | None:
    """`npm:@scope/name@tag` → `(包名, dist-tag)`；不是这种形状返回 None。

    取最后一个 `@`：作用域包名自己以 `@` 开头。
    """
    text = str(url or "").strip()
    if not text.lower().startswith(_NPM_SOURCE_PREFIX):
        return None
    body = text[len(_NPM_SOURCE_PREFIX):].strip()
    pkg, sep, tag = body.rpartition("@")
    if not sep or not pkg or not tag or "/" in tag:
        return None
    return pkg, tag


def npm_registry_url(pkg: str, tag: str) -> str:
    """npm metadata 的读取地址（国内镜像）。"""
    return "%s/%s/%s" % (_NPM_REGISTRY, pkg.lstrip("/"), tag)


def manifest_from_npm_metadata(meta: dict, channel: str) -> dict | None:
    """npm metadata → 清单 JSON；这个包没带清单时返回 None。

    清单写在 `package.json` 的 `crannia` 字段里。npm 保留自定义顶层字段
      （2026-11-15 拿 `vue` 的 `unpkg` / `jsdelivr` 在两个源上都验过）。
    代码包的哈希不从清单来：npm 的 `dist.integrity` 是 sha512、由注册中心给，
      而清单装在自己的 tarball 里 —— 让它自己写自己的哈希是循环的。
    """
    if not isinstance(meta, dict):
        return None
    body = meta.get("crannia")
    if not isinstance(body, dict):
        return None
    latest = dict(body)
    latest.setdefault("version", str(meta.get("version") or ""))
    latest.setdefault("channel", channel)
    artifacts = latest.get("artifacts")
    artifacts = dict(artifacts) if isinstance(artifacts, dict) else {}
    #: 清单可以只写"这一版是什么"，代码包那一格由注册中心补齐：
    #:   tarball 地址与 integrity 都是 npm 给的，写进清单反而会漂。
    cp = artifacts.get("codepack")
    cp = dict(cp) if isinstance(cp, dict) else {}
    dist = meta.get("dist") if isinstance(meta.get("dist"), dict) else {}
    tarball = str(dist.get("tarball") or "").strip()
    if tarball:
        cp.setdefault("url", tarball)
        cp.setdefault("urls", [tarball])
    if dist.get("integrity"):
        cp.setdefault("integrity", str(dist["integrity"]))
    try:
        if dist.get("size"):
            cp.setdefault("size_bytes", int(dist["size"]))
    except (TypeError, ValueError):
        pass
    if cp:
        artifacts["codepack"] = cp
    latest["artifacts"] = artifacts
    return {"latest": latest}


def pending_install() -> dict | None:
    """已经下好、等着下次启动装上的那一份；没有就 `None`。

    判据有三条，缺一条都当"没有"：
      ① 标记文件读得出且是个对象；
      ② 它记的那个安装包还在（用户可能手动清了 `data/updates`）；
      ③ 它的版本比当前新（装过一个更高版本之后留下的旧标记不能再来一次）。
    读不到一律当没有 —— 这是启动路径，不能因为一个坏 JSON 拦住开机。
    """
    try:
        raw = json.loads(_PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    version = str(raw.get("version") or "").strip()
    path = str(raw.get("path") or "").strip()
    if not version or not path or not Path(path).is_file():
        return None
    try:
        from core.version import __version__
        if _parse_version(version) <= _parse_version(__version__):
            return None
    except Exception:                                     # noqa: BLE001
        return None
    return {"version": version, "path": str(Path(path).resolve()),
            "sha256": str(raw.get("sha256") or ""),
            "at": raw.get("at")}


def mark_pending(manifest, downloaded_path) -> None:
    """把"下次启动装上这一份"记到磁盘上（壳在起后端之前读它）。"""
    try:
        _UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        #: 存绝对路径：读它的是桌面壳（`main.cjs::pendingUpdate`），
        #:   它的 cwd 不是数据根。相对路径在那里解不出来，而症状是
        #:   「下载完成了、黄字也亮了，重启却什么都没发生」。
        target = Path(downloaded_path).resolve()
        _PENDING_FILE.write_text(json.dumps({
            "version": str(manifest.version),
            "path": str(target),
            "sha256": str(manifest.sha256 or ""),
            "at": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print("[update] 记不下待安装标记：%s" % exc)


def clear_pending(remove_file: bool = False) -> None:
    """清掉待安装标记；`remove_file` 时连安装包一起删（装完之后）。"""
    path = ""
    try:
        path = str(json.loads(_PENDING_FILE.read_text(encoding="utf-8")).get("path") or "")
    except (OSError, ValueError, TypeError):
        path = ""
    try:
        _PENDING_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    if remove_file and path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- 代码包（两级更新的小那一级）

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _sha512_integrity(path: Path) -> str:
    """文件 → `sha512-<base64>`（npm 的 `dist.integrity` 形状）。

    读不到返回空串 —— 调用方按"不匹配"处理，与 `_sha256_file` 同一口径。
    """
    h = hashlib.sha512()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    return "sha512-" + base64.b64encode(h.digest()).decode("ascii")


def install_resources_dir() -> Path | None:
    """安装版的 `<…>/resources`；开发树返回 `None`。

    判据只用 `paths.is_installed()` —— 与数据根、`.env` 读取是同一个判据，
      不另造一个"看起来像安装版"的推断。
    """
    try:
        from core import paths as _paths
        if not _paths.is_installed():
            return None
        return _paths.app_root().parent
    except Exception:                                     # noqa: BLE001
        return None


def local_runtime_digest() -> str:
    """本地自带运行时的内容摘要（`runtime.json` 的 `payloadDigest`）。"""
    res = install_resources_dir()
    if res is None:
        return ""
    try:
        rt = json.loads((res / "primary-runtime" / "runtime.json")
                        .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(rt.get("payloadDigest") or "") if isinstance(rt, dict) else ""


def local_shell_sha256() -> str:
    """本地壳（`app.asar`）的整包哈希。

    这一格不能用来判"壳变没变"：asar 里的 `package.json` 带着 `version`，
      每发一版都变，整包哈希对任何跨版本更新必然不同，代码包永远用不上
      （2026-11-22 现场）。新清单用 `shell_sig`，这一格只留给老客户端与
      老清单。
    """
    res = install_resources_dir()
    if res is None:
        return ""
    return _sha256_file(res / "app.asar")


def _asar_layout(blob: bytes) -> tuple[dict, int]:
    """解析 asar 头，回 `(header, 数据区起点)`；解不开抛 `ValueError`。

    格式（`@electron/asar`）：u32@4 = pickle 大小，u32@12 = header JSON 长度，
    JSON 从 16 开始，文件内容从 `8 + pickle 大小` 开始。
    """
    if len(blob) < 16:
        raise ValueError("asar too short")
    pickle_size = struct.unpack("<I", blob[4:8])[0]
    json_len = struct.unpack("<I", blob[12:16])[0]
    if (pickle_size < json_len or 16 + json_len > len(blob)
            or 8 + pickle_size > len(blob)):
        raise ValueError("asar header sizes out of range")
    header = json.loads(blob[16:16 + json_len].decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("asar header is not an object")
    return header, 8 + pickle_size


def _package_json_without_version(raw: bytes) -> bytes:
    """`package.json` 去掉 `version` 再规范化 —— 别让版本号进壳签名。

    其余字段照算：依赖或入口变了，代码包仍然要回退全量。
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return raw
    if not isinstance(obj, dict):
        return raw
    obj.pop("version", None)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def shell_signature(asar_path) -> str:
    """壳的「内容签名」—— 与版本号无关的那一半。

    为什么不能用整包 sha256：asar 里装着壳自己的 `package.json`，而它带着
      `version`，每发一版都变，整包哈希对任何跨版本更新必然不同，代码包
      永远用不上，用户每次都退回 268 MB 全量
      （实测 1.0.0 / 1.0.3 / 1.1.0 三个哈希互不相同）。

    判据：除 `package.json` 外，每个文件的 `(路径, 内容)` 都进哈希；
      `package.json` 去掉 `version` 之后再进。`unpacked` 条目只记名字
      （它们的字节不在 asar 里）。读不到 / 解不开回空串 —— 调用方 fail-closed。
    """
    try:
        blob = Path(asar_path).read_bytes()
        header, data_off = _asar_layout(blob)
    except (OSError, ValueError):
        return ""
    entries: list[tuple[str, dict]] = []

    def walk(node: dict, prefix: str) -> None:
        for name, ent in (node or {}).items():
            if not isinstance(ent, dict):
                continue
            full = prefix + name
            children = ent.get("files")
            if isinstance(children, dict):
                walk(children, full + "/")
            else:
                entries.append((full, ent))

    walk(header.get("files") or {}, "")
    h = hashlib.sha256()
    for full, ent in sorted(entries, key=lambda kv: kv[0]):
        h.update(full.encode("utf-8"))
        h.update(b"\0")
        if ent.get("unpacked"):
            h.update(b"<unpacked>")
            h.update(b"\0")
            continue
        start = data_off + int(ent.get("offset") or 0)
        payload = blob[start:start + int(ent.get("size") or 0)]
        if full == "package.json":
            payload = _package_json_without_version(payload)
        h.update(payload)
        h.update(b"\0")
    return h.hexdigest()


def local_shell_sig() -> str:
    """本地壳的内容签名（判据同 `shell_signature`）。"""
    res = install_resources_dir()
    if res is None:
        return ""
    return shell_signature(res / "app.asar")


def codepack_applicable(manifest) -> bool:
    """这一版能不能只下代码包。

    fail-closed：清单没给、运行时摘要对不上、壳哈希对不上、
      读不到任何一格 —— 一律回退全量安装包。
    """
    cp = getattr(manifest, "codepack", None) or {}
    if not isinstance(cp, dict):
        return False
    #: 与全量安装包同一个口径：必须 HTTPS，只有本机调试地址例外
    #:   （`_is_dev_url` 只放 localhost / 127.0.0.1，不是"任意 http"）。
    raw_urls = cp.get("urls")
    first = ([str(u).strip() for u in raw_urls if str(u).strip()]
             if isinstance(raw_urls, (list, tuple)) else [])
    if not _is_dev_https_ok(first[0] if first else str(cp.get("url") or "")):
        return False
    #: npm 那条只有注册中心给的 `integrity`（sha512），没有清单自己写的 sha256。
    if not (str(cp.get("sha256") or "") or str(cp.get("integrity") or "")):
        return False
    runtime = local_runtime_digest()
    if not runtime or str(cp.get("runtime") or "") != runtime:
        return False
    #: 新清单带 `shell_sig`（与版本号无关的内容签名）；老清单只有整包
    #:   `shell_sha256` —— 那个对任何跨版本更新都必然不同，只留给老客户端。
    sig = str(cp.get("shell_sig") or "")
    if sig:
        return sig == local_shell_sig()
    shell = str(cp.get("shell_sha256") or "")
    if not shell:
        return False
    return shell == local_shell_sha256()


def mark_pending_code_pack(manifest, new_dir, app_dir, backup_dir) -> None:
    """记下"下次启动把 `app` 换成 `app.new`"（壳在起后端之前读它）。"""
    try:
        _UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        _PENDING_CODEPACK_FILE.write_text(json.dumps({
            "kind": "codepack",
            "version": str(manifest.version),
            "new_dir": str(Path(new_dir).resolve()),
            "app_dir": str(Path(app_dir).resolve()),
            "backup_dir": str(Path(backup_dir).resolve()),
            "sha256": str(getattr(manifest, "codepack", {}).get("sha256") or ""),
            "at": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print("[update] 记不下代码包待安装标记：%s" % exc)


def pending_code_pack() -> dict | None:
    """已经解好、等着下次启动换目录的代码包；没有就 `None`。

    三条判据：标记读得出 · 新目录里有 `core/version.py` · 版本比当前新。
    """
    try:
        raw = json.loads(_PENDING_CODEPACK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("kind") != "codepack":
        return None
    version = str(raw.get("version") or "").strip()
    new_dir = str(raw.get("new_dir") or "").strip()
    app_dir = str(raw.get("app_dir") or "").strip()
    if not version or not new_dir or not app_dir:
        return None
    if not (Path(new_dir) / "core" / "version.py").is_file():
        return None
    try:
        from core.version import __version__
        if _parse_version(version) <= _parse_version(__version__):
            return None
    except Exception:                                     # noqa: BLE001
        return None
    return {
        "kind": "codepack",
        "version": version,
        "new_dir": str(Path(new_dir).resolve()),
        "app_dir": str(Path(app_dir).resolve()),
        "backup_dir": str(raw.get("backup_dir") or "").strip(),
        "sha256": str(raw.get("sha256") or ""),
    }


def clear_pending_code_pack(remove_dirs: bool = False) -> None:
    """清掉代码包标记；`remove_dirs` 时把 `app.new` / `app.old` 一并删掉。"""
    raw: dict = {}
    try:
        raw = json.loads(_PENDING_CODEPACK_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, TypeError):
        raw = {}
    try:
        _PENDING_CODEPACK_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    if remove_dirs:
        for key in ("new_dir", "backup_dir"):
            p = str(raw.get(key) or "").strip()
            if not p:
                continue
            try:
                shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass


def _finish_codepack(tmp: Path, resources_dir: Path) -> Path:
    """解完之后的共同收口：验代码树、改名成 `app.new`。"""
    new_dir = resources_dir / "app.new"
    if not (tmp / "core" / "version.py").is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("code pack has no core/version.py")
    if new_dir.exists():
        shutil.rmtree(new_dir, ignore_errors=True)
    tmp.rename(new_dir)
    return new_dir


def _extract_zip_codepack(zip_path: Path, tmp: Path) -> None:
    """zip 那一档（GitHub / 自建源）。"""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            p = PurePosixPath(name.replace("\\", "/"))
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("unsafe path in code pack: %r" % name)
        zf.extractall(tmp)


def _extract_tar_codepack(tar_path: Path, tmp: Path) -> None:
    """`.tar.gz` 那一档（npm tarball）。

    npm 的 tarball 所有条目都在 `package/` 前缀下；这里把它剥掉。
    另外跳过我们发布时加的 `package.json` / `README.md`，不让它们落进应用树。
    拒绝符号链接 / 硬链接 / 设备文件 —— 解包只该产出普通文件与目录。
    """
    with tarfile.open(tar_path, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            p = PurePosixPath(m.name.replace("\\", "/"))
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("unsafe path in code pack: %r" % m.name)
            if m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
                raise ValueError("unsupported entry in code pack: %r" % m.name)
        for m in members:
            parts = list(PurePosixPath(m.name.replace("\\", "/")).parts)
            if parts and parts[0] == "package":
                parts = parts[1:]
            if not parts:
                continue
            if len(parts) == 1 and parts[0] in ("package.json", "README.md"):
                continue
            target = tmp.joinpath(*parts)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fobj = tf.extractfile(m)
            if fobj is None:
                continue
            with open(target, "wb") as out:
                shutil.copyfileobj(fobj, out)


def _extract_codepack(pack_path: Path, resources_dir: Path) -> Path:
    """把代码包解到 `<resources>/app.new`（先解到 `.tmp`，验过再改名）。

    两档：`.zip`（自建源）与 `.tar.gz` / `.tgz`（npm tarball），按文件名分派。
    两条拒绝：`..` / 绝对路径条目 · 解完找不到 `core/version.py`。
    """
    tmp = resources_dir / "app.new.tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    name = pack_path.name.lower()
    if name.endswith((".tar.gz", ".tgz")):
        _extract_tar_codepack(pack_path, tmp)
    else:
        _extract_zip_codepack(pack_path, tmp)
    return _finish_codepack(tmp, resources_dir)


class UpdateChecker:
    """Check for, download, and verify updates."""
    def __init__(self, manifest_url: str | None = None, channel: str = "stable"):
        self._channel = channel
        self._manifest_url = manifest_url

    # URL resolution

    @property
    def manifest_urls(self) -> list[str]:
        """解析这个渠道的候选源（主 → 备，按序试）。

        优先级：构造参数 > `config.yaml` 的 `update.manifest_urls.<渠道>` >
        老的单值 `update.manifest_url`（向后兼容）> `DEFAULT_MANIFEST_URLS`。

        客户端的渠道只影响查哪一条：`stable` 永远存在；`beta` 只在手动检查里查
          （自动检查只走 stable —— 「beta 出现在可更新列表、不主动提醒」）。
        """
        if self._manifest_url:
            return [self._manifest_url]
        return manifest_urls_for(self.channel, _read_config())

    @property
    def manifest_url(self) -> str:
        """主源（第一条）。留给只关心一个地址的调用方。"""
        return self.manifest_urls[0]

    @property
    def channel(self) -> str:
        if self._channel:
            return normalise_channel(self._channel)
        config = _read_config()
        return normalise_channel(config.get("update", {}).get("channel", "stable"))

    # Check

    def check(self) -> UpdateManifest | None:
        """Fetch the remote manifest and return an UpdateManifest if a newer
        version is available.  Returns None if up-to-date or on error.
        """
        global _update_state
        _update_state["status"] = "checking"
        _update_state["error"] = None

        try:
            from core.version import __version__

            # 占位域名 = 还没配，不发请求（别把"没配"演成"检查过了"）。
            sources = [u for u in self.manifest_urls if is_configured(u)]
            if not sources:
                _update_state["status"] = "idle"
                _update_state["error"] = None
                _update_state["unconfigured"] = True
                return None
            _update_state["unconfigured"] = False

            #: 逐个源试，第一个成功即用。一条源坏了不许把整次检查判死 ——
            #:   那正是「国内连不上 GitHub 就永远收不到更新」的形状。
            #:   全失败才报错，且把每个源的错误都留在日志里（不许塌成"没有新版本"）。
            data = None
            used = None
            failures: list[str] = []
            for source in sources:
                try:
                    data = self._fetch_manifest(source)
                    used = source
                    break
                except Exception as exc:  # noqa: BLE001
                    failures.append("%s: %s" % (source, exc))
                    log_update("manifest source failed: %s (%s)" % (source, exc))
            if data is None:
                _update_state["status"] = "error"
                _update_state["error"] = "All update sources failed: " + "; ".join(failures)
                return None
            _update_state["source"] = used
            #: 成功也留一行：否则"国内源到底生效没"只能靠猜 ——
            #:   失败有日志、成功没有，读起来就像那次检查没发生。
            log_update("manifest ok from %s" % used)

            manifest = UpdateManifest.from_json(data)
            if manifest is None:
                _update_state["status"] = "error"
                _update_state["error"] = "Invalid manifest format"
                return None

            #: 渠道必须一致：这份清单是从本渠道的静态地址读来的，所以它
            #:   声明的 `channel` 必须就是这一条。不一致通常意味着发布时写反了
            #:   （Beta 版的清单进了 `stable/version.json`）—— 放过去的话，
            #:   Stable 客户端会把 Beta 包当成稳定更新装上去。
            #:   "没声明渠道"同样拒绝：那是清单坏了，不是"默认 stable"。
            #: 这里逐个字比，不借 `normalise_channel` —— 那个函数是给
            #:   "配置读坏了回退到 stable" 用的（宽松归一），拿它校验输入
            #:   会把 `stable1` / `STABLE` 之类的坏值悄悄收成 `stable`。
            want = self.channel
            raw = str(manifest.channel or "").strip().lower()
            if raw not in VALID_CHANNELS or raw != want:
                _update_state["status"] = "error"
                _update_state["error"] = (
                    "Manifest declares channel %r but this client asked for %r — "
                    "refusing it (a beta manifest must not land in the stable "
                    "channel, and a channel outside %s is malformed)."
                    % (manifest.channel or "", want, "/".join(sorted(VALID_CHANNELS))))
                return None

            _update_state["latest_version"] = manifest.version
            _update_state["latest_manifest"] = data

            current_tuple = _parse_version(__version__)
            latest_tuple = _parse_version(manifest.version)

            if latest_tuple <= current_tuple:
                _update_state["status"] = "idle"
                return None

            # Check minimum upgrade path
            if manifest.minimum_client_version:
                min_tuple = _parse_version(manifest.minimum_client_version)
                if current_tuple < min_tuple:
                    _update_state["status"] = "error"
                    _update_state["error"] = (
                        f"Current version {__version__} is too old. "
                        f"Please upgrade to at least v{manifest.minimum_client_version} first."
                    )
                    return None

            # Validate download URL scheme
            if not manifest.download_url.startswith("https://") and not self._is_dev_url(
                manifest.download_url
            ):
                _update_state["status"] = "error"
                _update_state["error"] = "Download URL must use HTTPS"
                return None

            _update_state["status"] = "idle"
            return manifest

        except requests.RequestException as e:
            _update_state["status"] = "error"
            _update_state["error"] = f"Network error: {e}"
            return None
        except Exception as e:
            _update_state["status"] = "error"
            _update_state["error"] = str(e)
            return None

    def _fetch_manifest(self, source: str) -> dict:
        """读一条源，返回清单 JSON（`{latest: {...}}` 形状）。

        两种形状：`npm:<包名>@<dist-tag>` 读注册中心 metadata 的 `crannia` 字段；
        其余按静态 JSON 读（并带上服务端过滤用的查询参数）。
        """
        from core.version import __version__
        from core import net_proxy as _np

        spec = parse_npm_source(source)
        if spec is not None:
            #: npm 那条不拼查询参数：`/latest` 是按 dist-tag 取的，多带的参数
            #:   没有意义，而某些注册中心实现会把它当成路径的一部分。
            url = npm_registry_url(*spec)
        else:
            sep = "&" if "?" in source else "?"
            url = f"{source}{sep}v={__version__}&channel={self.channel}&arch=win_amd64"

        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": f"Crannia/{__version__} (Windows)"},
            # `None` = 跟随环境变量（`requests` 对 `proxies=None` 就是这个意思）；
            #   `direct` 档给的是显式的空表。一处口径，见 `core/net_proxy.py`。
            proxies=_np.requests_proxies(),
        )
        resp.raise_for_status()
        data = resp.json()
        if spec is not None:
            data = manifest_from_npm_metadata(data, self.channel)
            if data is None:
                raise ValueError("this npm package carries no update manifest")
        return data

    @staticmethod
    def _is_dev_url(url: str) -> bool:
        """Allow non-HTTPS for localhost dev servers."""
        return _is_local_dev_url(url)

    # Download

    #: 下载失败后的重试间隔（秒）—— 三次重试、指数退避。
    #: 读者把它改成 `(0, 0, 0)`：一条"断了会重试"的用例不该真睡 65 秒。
    DOWNLOAD_RETRY_DELAYS: tuple[int, ...] = (5, 15, 45)

    def download(
        self, manifest: UpdateManifest, progress_callback: Callable[[float], None] | None = None
    ) -> Path:
        """Download the update asset to data/updates/.  Returns the path to the
        downloaded file.

        The file is written to a .part temporary name first; on success it is
        renamed to its final name.  This prevents orphaned partial downloads.
        """
        global _update_state
        _mark_phase("downloading")
        _update_state["progress"] = 0.0
        _update_state["error"] = None
        _update_state["kind"] = "full"

        _UPDATES_DIR.mkdir(parents=True, exist_ok=True)

        filename = f"crannia-{manifest.version}.exe"
        dest_part = _UPDATES_DIR / f"{filename}.part"
        dest_final = _UPDATES_DIR / filename

        #: 已经下过同一版（上次下完没装、或自动下载跑过一遍）就不再下一次。
        #:   263 MB 重下一遍是几分钟的带宽，而"下过了"这件事磁盘上写着。
        already = pending_install()
        if already and already.get("version") == str(manifest.version):
            _update_state["status"] = "ready"
            _update_state["progress"] = 1.0
            _update_state["downloaded_path"] = already["path"]
            _update_state["latest_version"] = manifest.version
            return Path(already["path"])

        #: 同一件产物可以给多条地址（GitHub 主源 → 备用直链）。
        #:   一次尝试里把每条都试一遍，再进下一轮退避。
        urls = list(manifest.download_urls or []) or [manifest.download_url]
        attempts = len(self.DOWNLOAD_RETRY_DELAYS) + 1
        last_error: Exception | None = None
        download_started = time.time()
        for attempt in range(1, attempts + 1):
            for url in urls:
                try:
                    self._fetch_url_to_part(url, manifest.version, dest_part,
                                            progress_callback)
                    last_error = None
                    break
                except (requests.RequestException, OSError) as exc:
                    #: `OSError` 也要接：`.part` 被别的进程占着时 `open()` 抛
                    #:   `PermissionError: [WinError 32]`（杀软 / 并发下载），
                    #:   原来它越过这条重试直接冲进线程体 —— 2026-11-22 真机
                    #:   日志里那一行就是它。放过这一支等于"重试机制本身会被
                    #:   一次瞬时占用打断"。
                    last_error = exc
                    log_update("download attempt %d/%d failed (%s): %s"
                               % (attempt, attempts, url, exc))
            if last_error is None:
                break
            if attempt < attempts:
                delay = self.DOWNLOAD_RETRY_DELAYS[attempt - 1]
                if delay:
                    time.sleep(delay)

        if last_error is not None:
            #: `.part` 留着：下次启动从它的长度续传。
            #:   只有校验失败那一支才丢 —— 半成品接着用比重下更糟。
            _update_state["status"] = "error"
            _update_state["error"] = "Download error: %s" % last_error
            return None

        # Verify checksum —— 没给校验和就不投放（同 `_is_bad()` 的"读不到 ≠ 0"）
        #
        # 缺省 `sha256=""` 是 falsy：以前写成 `if manifest.sha256:`，于是
        #   「要不要校验」这个开关落在了 manifest 的提供方手上 —— 不给就跳过，
        #   下载来的 exe 直接进安装器（`-ExecutionPolicy Bypass` + `/S` 静默）。
        # 宁可不更新，也不装一个不知道是什么的东西。
        try:
            size = dest_part.stat().st_size
        except OSError:
            size = 0
        log_update("download complete: %s (%d bytes) in %.1fs -> verifying"
                   % (dest_part.name, size, time.time() - download_started))
        _mark_phase("verifying")
        try:
            if not manifest.sha256:
                dest_part.unlink(missing_ok=True)
                _update_state["status"] = "error"
                _update_state["error"] = ("manifest 没给 sha256 —— 拒绝投放"
                                          "（没有校验和就无法确认这个包是什么）")
                log_update("refused: manifest has no sha256")
                return None
            if not self.verify_checksum(dest_part, manifest.sha256):
                dest_part.unlink(missing_ok=True)
                _update_state["status"] = "error"
                _update_state["error"] = "SHA256 checksum mismatch — download discarded"
                log_update("refused: sha256 mismatch")
                return None
            log_update("verify ok: %s" % dest_part.name)

            # 收口用 replace，不用 rename。Windows 上 os.rename 碰到已存在的目标会抛
            #   FileExistsError，于是同一个版本下第二次下载（装失败后点重试，或者上次
            #   下到一半断了）必然失败，而那句错误看起来像磁盘坏了。走到这里时哈希
            #   已经验过，覆盖是事先想清楚的事，不是巧合。
            #   杀软可能正扫着这个刚下好的 exe，重试在 `_replace_with_retry`。
            _replace_with_retry(dest_part, dest_final)
        except OSError as exc:
            _update_state["status"] = "error"
            _update_state["error"] = ("安装包落地失败（文件可能被杀毒软件占用）：%s"
                                      % exc)
            log_update("finalize failed: %s" % exc)
            return None
        _update_state["status"] = "ready"
        _update_state["downloaded_path"] = str(dest_final)
        mark_pending(manifest, dest_final)
        log_update("download ok: v%s -> %s" % (manifest.version, dest_final.name))
        return dest_final

    def download_best(self, manifest: UpdateManifest,
                      progress_callback: Callable[[float], None] | None = None):
        """下一级能用的那一级：代码包优先，不适用/失败就回退全量。"""
        if codepack_applicable(manifest):
            out = self.download_code_pack(manifest, progress_callback)
            if out is not None:
                return out
            log_update("code pack failed; falling back to the full installer")
        return self.download(manifest, progress_callback)

    def download_code_pack(self, manifest: UpdateManifest,
                           progress_callback: Callable[[float], None] | None = None):
        """下载代码包（几 MB），验 sha256，解到 `<resources>/app.new`。

        返回 `app.new` 的路径；任何一格失败返回 `None`（调用方回退全量）。
        判据与全量那份逐条对齐：Range 续传 · 三次重试 · sha256 不符就丢 ·
          失败留 `.part` 给下次续传。
        """
        global _update_state
        cp = dict(getattr(manifest, "codepack", None) or {})
        raw_urls = cp.get("urls")
        urls = ([str(u).strip() for u in raw_urls if str(u).strip()]
                if isinstance(raw_urls, (list, tuple)) else [])
        if not urls and cp.get("url"):
            urls = [str(cp["url"])]
        url = urls[0] if urls else ""
        expected = str(cp.get("sha256") or "")
        #: npm 那条给的哈希是 `dist.integrity`（sha512）：清单装在自己的
        #:   tarball 里，让它自己写自己的 sha256 是循环的 —— 注册中心给的那份
        #:   才是权威。两种哈希认一种即可，两样都没有就拒绝。
        integrity = str(cp.get("integrity") or "")
        _update_state["kind"] = "codepack"
        _mark_phase("downloading")
        _update_state["progress"] = 0.0
        _update_state["error"] = None

        res = install_resources_dir()
        if res is None:
            _update_state["status"] = "error"
            _update_state["error"] = "code pack needs an installed copy"
            log_update("code pack skipped: not an installed copy")
            return None
        if not url or not _is_dev_https_ok(url) or not (expected or integrity):
            _update_state["status"] = "error"
            _update_state["error"] = "code pack entry is incomplete"
            log_update("code pack refused: url/checksum missing")
            return None

        _UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        #: 后缀随 URL 走：npm tarball（`.tgz` / `.tar.gz`）与自建 zip 两档。
        suffix = (".tar.gz" if url.lower().split("?")[0].endswith((".tar.gz", ".tgz"))
                  else ".zip")
        dest_part = _UPDATES_DIR / f"codepack-{manifest.version}{suffix}.part"
        dest_final = _UPDATES_DIR / f"codepack-{manifest.version}{suffix}"

        attempts = len(self.DOWNLOAD_RETRY_DELAYS) + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            for candidate in urls:
                try:
                    self._fetch_url_to_part(candidate, manifest.version, dest_part,
                                            progress_callback)
                    last_error = None
                    break
                except (requests.RequestException, OSError) as exc:
                    last_error = exc
                    log_update("codepack attempt %d/%d failed (%s): %s"
                               % (attempt, attempts, candidate, exc))
            if last_error is None:
                break
            if attempt < attempts:
                delay = self.DOWNLOAD_RETRY_DELAYS[attempt - 1]
                if delay:
                    time.sleep(delay)
        if last_error is not None:
            _update_state["status"] = "error"
            _update_state["error"] = "Code pack download error: %s" % last_error
            return None

        try:
            size = dest_part.stat().st_size
        except OSError:
            size = 0
        log_update("code pack download complete: %s (%d bytes) -> verifying"
                   % (dest_part.name, size))
        _mark_phase("verifying")
        try:
            verified = (_sha256_file(dest_part) == expected) if expected \
                else (_sha512_integrity(dest_part) == integrity)
            if not verified:
                dest_part.unlink(missing_ok=True)
                _update_state["status"] = "error"
                _update_state["error"] = ("Code pack %s mismatch — discarded"
                                          % ("sha256" if expected else "integrity"))
                log_update("code pack refused: %s mismatch"
                           % ("sha256" if expected else "integrity"))
                return None
            log_update("code pack verify ok: %s" % dest_part.name)
            _replace_with_retry(dest_part, dest_final)
        except OSError as exc:
            _update_state["status"] = "error"
            _update_state["error"] = ("代码包落地失败（文件可能被杀毒软件占用）：%s"
                                      % exc)
            log_update("code pack finalize failed: %s" % exc)
            return None

        try:
            new_dir = _extract_codepack(dest_final, res)
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            try:
                dest_final.unlink(missing_ok=True)
            except OSError:
                pass
            _update_state["status"] = "error"
            _update_state["error"] = "Code pack could not be unpacked: %s" % exc
            log_update("code pack unpack failed: %s" % exc)
            return None

        mark_pending_code_pack(manifest, new_dir, res / "app", res / "app.old")
        _update_state["status"] = "ready"
        _update_state["progress"] = 1.0
        _update_state["downloaded_path"] = str(new_dir)
        _update_state["latest_version"] = manifest.version
        log_update("code pack ready: v%s -> %s"
                   % (manifest.version, new_dir.name))
        return new_dir

    def _fetch_url_to_part(self, url: str, version: str, dest_part: Path,
                           progress_callback: Callable[[float], None] | None = None) -> None:
        """一次下载尝试（`.part` 已在就从它的长度续传）。

        Range 续传的判据是响应码：`206` = 服务端接着给（追加写）；
        `200` = 它不认 Range（整份重发，从头写）。不能假定 206 ——
        GitHub Releases 的 CDN 不保证每段都支持；假定错了会把半成品接在
        整份文件后面，而 sha256 会"晚一步"才发现它。
        """
        from core import net_proxy as _np
        offset = dest_part.stat().st_size if dest_part.exists() else 0
        headers = {"User-Agent": f"Crannia/{version} (Windows)"}
        if offset:
            headers["Range"] = "bytes=%d-" % offset
        resp = requests.get(
            url,
            stream=True,
            timeout=300,
            headers=headers,
            proxies=_np.requests_proxies(),
        )
        status = getattr(resp, "status_code", 200)
        if status == 416 and offset:
            #: `.part` 比服务端那份还长（上一版残留 / 服务端换了文件）：
            #:   丢掉重来，而不是原地重试同一条 Range（那条永远 416）。
            dest_part.unlink(missing_ok=True)
            raise requests.RequestException(
                "range not satisfiable; restarting from zero")
        resp.raise_for_status()

        if offset and status == 206:
            mode, start = "ab", offset
        else:
            mode, start = "wb", 0

        total = int(resp.headers.get("content-length", 0) or 0) + start
        downloaded = start
        if total:
            _update_state["progress"] = downloaded / total
        with open(dest_part, mode) as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                progress = downloaded / total if total else 0.0
                _update_state["progress"] = progress
                if progress_callback:
                    progress_callback(progress)
        if total and downloaded < total:
            #: 断在半路：`iter_content` 正常结束也可能只是连接关了。
            #:   抛出去交给重试 / 续传；不抛就会拿一个半成品去校验。
            raise requests.RequestException(
                "incomplete download: %d of %d bytes" % (downloaded, total))

    @staticmethod
    def verify_checksum(file_path: Path, expected_sha256: str) -> bool:
        """Verify the SHA256 digest of *file_path*."""
        if not expected_sha256:
            # 纵深：调用方已经不投放空校验和的包了（见 `download`）。这里再守一道，
            #   免得以后多一个调用点就悄悄把 fail-open 请回来。
            return False
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        actual = sha.hexdigest()
        # Support both "sha256:hex" and bare hex formats
        expected = expected_sha256.replace("sha256:", "").strip().lower()
        return actual == expected

    # Cleanup

    @staticmethod
    def cleanup_backups(max_age_days: int = 30) -> None:
        """Remove old backups and orphaned .part files."""
        if not _UPDATES_DIR.exists():
            return
        now = time.time()
        for f in _UPDATES_DIR.iterdir():
            if f.suffix in (".backup", ".part"):
                try:
                    age_days = (now - f.stat().st_mtime) / 86400
                    if age_days > max_age_days:
                        f.unlink(missing_ok=True)
                except OSError:
                    pass


# UpdateInstaller

class UpdateInstaller:
    """跑安装包（NSIS `/S`），不再自替换。

    判据：桌面版的代码在
      `<安装目录>/resources/app` 里，而后端是个独立的 Python 进程
      （`sys.exe` 指的是它，不是 app 的 exe），原来那套"备份旧 exe、覆盖、再启动"
      在桌面版上根本不成立（`sys.frozen` 也不成立）。改成：
      下载 → 等后端退出 → `安装包 /S` → 结束。
    `/S` 全程无界面：没有进度条、没有目录选择、没有结束页 ——
      界面必须事前说清"应用会自己关闭，装完请重新打开"（那一句在设置页）。
    装完不自动拉起：安装路径由 NSIS 决定，我们猜不准；让用户自己点开
      （猜错 = 启动一个不存在的路径，而症状是"更新之后打不开了"）。
    """

    @staticmethod
    def install(downloaded_path: Path) -> None:
        """起一个后台 PowerShell：等我们退出 → 静默跑安装包。本函数不返回。"""
        global _UPDATING
        _UPDATING = True

        if sys.platform != "win32":
            _update_state["status"] = "error"
            _update_state["error"] = "更新只支持 Windows 安装包。"
            _UPDATING = False
            return

        if not getattr(sys, "frozen", False):
            # 源码跑：我们的进程不属于安装目录，装安装包没有意义（也不该替用户装）。
            _update_state["status"] = "error"
            _update_state["error"] = (
                "当前是源码运行 —— 请下载安装包后自己双击安装。")
            _UPDATING = False
            return

        installer = Path(downloaded_path).resolve()
        if not installer.is_file():
            _update_state["status"] = "error"
            _update_state["error"] = "安装包不见了，请重新下载。"
            _UPDATING = False
            return

        old_pid = os.getpid()
        ps_script = tempfile.gettempdir() / "crannia_update.ps1"
        ps_script.write_text(_build_powershell_script(old_pid, str(installer)),
                             encoding="utf-8")

        try:
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-WindowStyle", "Hidden",
                    "-ExecutionPolicy", "Bypass",
                    "-File", str(ps_script),
                    "-OldPid", str(old_pid),
                    "-Installer", str(installer),
                ],
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                    if sys.platform == "win32" else 0
                ),
            )
        except Exception as e:
            _update_state["status"] = "error"
            _update_state["error"] = "启动安装程序失败：%s" % e
            _UPDATING = False
            return

        os._exit(0)


def _build_powershell_script(old_pid: int, installer: str) -> str:
    """后台脚本：等我们退出（装安装包时那些文件是被占着的），再 `/S` 静默装。

    只有 120 秒：超过就放弃并留一句日志 —— 不无限等（用户可能把窗口挂在那儿）。
    不杀进程树：先把我们自己的 PID 等掉；壳（Electron）看到后端退出会自己关。
    """
    return f'''param($OldPid, $Installer)

$ErrorActionPreference = "Continue"
$log = Join-Path $env:TEMP "crannia_update.log"

# ── 等后端退出（最多 120 秒）──
for ($i = 0; $i -lt 120; $i++) {{
    $p = Get-Process -Id $OldPid -ErrorAction SilentlyContinue
    if (-not $p) {{ break }}
    Start-Sleep -Seconds 1
}}

$p = Get-Process -Id $OldPid -ErrorAction SilentlyContinue
if ($p) {{
    Add-Content -Path $log -Value "$(Get-Date -Format o) 后端没有在 120 秒内退出，放弃安装"
    Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
    [Environment]::Exit(1)
}}

# ── 静默安装（`/S`：没有任何界面）──
try {{
    $proc = Start-Process -FilePath $Installer -ArgumentList "/S" -Wait -PassThru
    Add-Content -Path $log -Value "$(Get-Date -Format o) 安装包退出码 $($proc.ExitCode)"
    Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
    [Environment]::Exit(0)
}} catch {{
    Add-Content -Path $log -Value "$(Get-Date -Format o) 安装失败：$($_.Exception.Message)"
    Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
    [Environment]::Exit(1)
}}
'''


# 手动检查：两条渠道都查

def check_all_channels() -> dict:
    """手动检查用：stable 与 beta 各查一次，取版本更高的那条。

    返回 `{"best": UpdateManifest | None, "stable": ..., "beta": ...,
    "errors": {渠道: 错误}}`。

    判据：beta 出现在可更新列表里（手动查得到），
      但不主动提醒（自动检查只查 stable，见 `background_startup_check`）。
    两条共用同一个全局 `_update_state`，查完要把胜出的那条写回去，
      否则状态里留的是"最后查的那条"（beta），而界面读的是它。
    一条挂了不影响另一条：错误按渠道分开报（读不到 ≠ 没有新版本）。
    """
    global _update_state
    found: dict = {"best": None, "stable": None, "beta": None, "errors": {}}
    #: 每条渠道原始的 manifest JSON（带 `artifacts` 那一截）。
    #: 不能拿 `UpdateManifest.__dict__` 顶上去 —— 那是字段视图，
    #: 没有 `artifacts`，而 `/api/update/download` 要靠它取下载地址。
    raw_by_channel: dict = {}
    for ch in VALID_CHANNELS:
        try:
            m = UpdateChecker(channel=ch).check()
        except Exception as exc:                              # noqa: BLE001
            found["errors"][ch] = "%s: %s" % (type(exc).__name__, exc)
            log_update("channel %s check failed: %s" % (ch, found["errors"][ch]))
            continue
        raw_by_channel[ch] = _update_state.get("latest_manifest")
        if m is not None:
            found[ch] = m
            if found["best"] is None or \
                    _parse_version(m.version) > _parse_version(found["best"].version):
                found["best"] = m
    best = found["best"]
    if best is not None:
        _update_state["latest_version"] = best.version
        _update_state["latest_manifest"] = (raw_by_channel.get(best.channel)
                                            or _update_state.get("latest_manifest"))
        _update_state["error"] = None
        _update_state["status"] = "idle"
    return found


# Convenience: background check suitable for startup

def background_startup_check() -> None:
    """Run an update check in the background (called from crannia_tray.py).

    时间戳只在「确认没有新版本」或「下载成功」之后写。
      `check()` 在网络上出错时也返回 `None`（与"没有新版本"同形），
      原来那版照样写 `update_last_check`，于是一次超时等于接下来 24 小时
      不再自动重试（2026-11-14 真机现场：00:19 / 00:31 两次启动都只写了
      时间戳、没有下载文件）。失败要留痕并下次还试。
    """
    def _run():
        global _update_state
        try:
            settings = _read_settings()
            if not settings.get("update_auto_check", True):
                return

            last = settings.get("update_last_check", 0)
            config = _read_config()
            interval_hours = config.get("update", {}).get("check_interval_hours", 24)
            if time.time() - last < interval_hours * 3600:
                return

            #: 自动检查只查 stable：
            #:   「beta 出现在可更新列表，不主动提醒」。
            checker = UpdateChecker(channel="stable")
            manifest = checker.check()

            #: 失败 ≠ 已检查。两种都在 `check()` 里返回 `None`，唯一的区分
            #:   是它留在状态里的那格 - 读状态，不读返回值。
            if _update_state.get("status") == "error":
                log_update("check failed: %s"
                           % (_update_state.get("error") or "unknown"))
                return
            if _update_state.get("unconfigured"):
                return  # 根本没发请求，不是"检查过了"

            if manifest is not None:
                ignored = settings.get("update_ignored_version")
                if ignored == manifest.version:
                    #: 用户说过"这个版本不再提醒" —— 没有可重试的事，记下时间。
                    settings["update_last_check"] = time.time()
                    _write_settings(settings)
                    return
                _update_state["latest_version"] = manifest.version
                #: 不覆写 `latest_manifest`：`check()` 已经放了原始 JSON，
                #:   而 `manifest.__dict__` 是字段视图、没有 `artifacts` 那一截 ——
                #:   写进去之后手动下载读它，会回 "Invalid manifest"（真机撞过）。
                #: 自动下载（用户 2026-11-13 定的那一半）：
                #:   查到就下，下完在磁盘上留一个待安装标记；界面那条黄字与
                #:   下次启动的「正在更新」都读它。失败不抛 —— 下次启动会再试。
                #: 源码树里不下：那里装不了（安装包没有安装目录可覆盖），
                #:   白占 263 MB 且没有任何东西会用它。
                try:
                    from core import paths as _paths
                    installed = _paths.is_installed()
                except Exception:                         # noqa: BLE001
                    installed = False
                if installed:
                    already = pending_install()
                    if not (already and already.get("version") == manifest.version):
                        #: 走同一条入口：任何异常都落成 `error`。此前这里是
                        #:   裸调用 + 只记日志 —— 状态停在 `downloading`，
                        #:   界面一直转，而下面那格还照样写 24h 时间戳
                        #:   （2026-11-22 报障日志的现场）。
                        download_thread_entry(manifest, checker)
                        if _update_state.get("status") == "error":
                            #: 下载失败：不写时间戳 —— 下次启动从 `.part` 续传。
                            log_update("download failed: %s"
                                       % (_update_state.get("error") or "unknown"))
                            return

            settings["update_last_check"] = time.time()
            _write_settings(settings)
        except Exception as exc:                          # noqa: BLE001
            #: 走到这里说明不是网络问题（读配置 / 写设置炸了）——
            #:   同样不许静默：它会让"自动更新从来没生效"看起来像"没有新版本"。
            log_update("startup check crashed: %s: %s" % (type(exc).__name__, exc))

    t = threading.Thread(target=_run, daemon=True, name="update-check")
    t.start()
