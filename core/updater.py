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

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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

# Global state (shared with web/server.py)
_update_state: dict = {
    "status": "idle",        # idle | checking | downloading | verifying | ready | error
    "progress": 0.0,         # 0.0 - 1.0
    "error": None,           # str | None
    "latest_version": None,  # str | None
    "latest_manifest": None, # dict | None (raw manifest JSON)
    "downloaded_path": None, # str | None
    #: 更新源还是占位域名，根本没发请求（与"查过了、没新版本"必须分开）。
    "unconfigured": False,
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

    @classmethod
    def from_json(cls, data: dict) -> UpdateManifest | None:
        """Parse the manifest JSON.  Returns None if the format is invalid."""
        try:
            latest = data.get("latest", data)  # tolerate both wrapped & flat
            artifacts = latest.get("artifacts", {})
            # Prefer windows-exe, fall back to first artifact
            asset = artifacts.get("windows-exe") or artifacts.get("windows_portable")
            if asset is None and artifacts:
                asset = next(iter(artifacts.values()))
            if asset is None:
                return None
            return cls(
                version=latest["version"],
                #: 缺省是空串，不是 `stable` —— "没声明渠道"与"声明了 stable"
                #:   是两件事，而 `check()` 只放行一致的那一种（见下）。
                channel=str(latest.get("channel") or "").strip().lower(),
                published_at=latest.get("published_at", ""),
                release_notes_url=latest.get("release_notes_url", ""),
                download_url=asset["url"],
                sha256=asset.get("sha256", ""),
                size_bytes=asset.get("size_bytes", 0),
                minimum_client_version=latest.get("minimum_client_version"),
                mandatory=latest.get("mandatory", False),
                changelog=latest.get("changelog", {}),
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

#: 两条静态 manifest。放 GitHub Pages / 对象存储 ——
#: GitHub Releases 不能当 manifest 源：静态文件忽略 `?query`，而 `latest`
#:   会被 beta 顶掉，"稳定版检查"拿到测试版。
#:
#: 域名是占位：发布前必须换成真地址。可用配置覆盖（优先级见
#:   `UpdateChecker.manifest_url`）：`update.manifest_urls.stable` / `.beta`，
#:   老的单值 `update.manifest_url` 仍然认（两条都用它，向后兼容）。
DEFAULT_MANIFEST_URLS: dict[str, str] = {
    #:   两份文件在 `gh-pages` 分支根下（`stable/` 与 `beta/`）—— 与源码分支分开，
    #:   免得把整棵源码树当网站发出去。
    "stable": "https://hfyjq.github.io/Crannia/stable/version.json",
    "beta": "https://hfyjq.github.io/Crannia/beta/version.json",
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


def manifest_url_for(channel, config: dict | None = None) -> str:
    """给定渠道，解析它的 manifest URL（纯函数，配置由调用方交进来）。"""
    ch = normalise_channel(channel)
    upd = (config or {}).get("update", {}) if isinstance(config, dict) else {}
    urls = upd.get("manifest_urls") if isinstance(upd.get("manifest_urls"), dict) else {}
    url = str(urls.get(ch) or "").strip()
    if not url:
        url = str(upd.get("manifest_url") or "").strip()
    return url or DEFAULT_MANIFEST_URLS[ch]


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


class UpdateChecker:
    """Check for, download, and verify updates."""
    def __init__(self, manifest_url: str | None = None, channel: str = "stable"):
        self._channel = channel
        self._manifest_url = manifest_url

    # URL resolution

    @property
    def manifest_url(self) -> str:
        """解析这个渠道的 manifest URL。

        优先级：构造参数 > `config.yaml` 的 `update.manifest_urls.<渠道>` >
        老的单值 `update.manifest_url`（向后兼容）> `DEFAULT_MANIFEST_URLS`。

        客户端的渠道只影响查哪一条：`stable` 永远存在；`beta` 只在手动检查里查
          （自动检查只走 stable —— 「beta 出现在可更新列表、不主动提醒」）。
        """
        if self._manifest_url:
            return self._manifest_url
        return manifest_url_for(self.channel, _read_config())

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
            if not is_configured(self.manifest_url):
                _update_state["status"] = "idle"
                _update_state["error"] = None
                _update_state["unconfigured"] = True
                return None
            _update_state["unconfigured"] = False

            url = self._build_check_url()
            from core import net_proxy as _np
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

    def _build_check_url(self) -> str:
        """Append query params for server-side filtering."""
        from core.version import __version__
        url = self.manifest_url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}v={__version__}&channel={self.channel}&arch=win_amd64"

    @staticmethod
    def _is_dev_url(url: str) -> bool:
        """Allow non-HTTPS for localhost dev servers."""
        return url.startswith("http://localhost") or url.startswith("http://127.0.0.1")

    # Download

    def download(
        self, manifest: UpdateManifest, progress_callback: Callable[[float], None] | None = None
    ) -> Path:
        """Download the update asset to data/updates/.  Returns the path to the
        downloaded file.

        The file is written to a .part temporary name first; on success it is
        renamed to its final name.  This prevents orphaned partial downloads.
        """
        global _update_state
        _update_state["status"] = "downloading"
        _update_state["progress"] = 0.0
        _update_state["error"] = None

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

        try:
            from core import net_proxy as _np
            resp = requests.get(
                manifest.download_url,
                stream=True,
                timeout=300,
                headers={"User-Agent": f"Crannia/{manifest.version} (Windows)"},
                proxies=_np.requests_proxies(),
            )
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(dest_part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    progress = downloaded / total if total else 0.0
                    _update_state["progress"] = progress
                    if progress_callback:
                        progress_callback(progress)

            # Verify checksum —— 没给校验和就不投放（同 `_is_bad()` 的"读不到 ≠ 0"）
            #
            # 缺省 `sha256=""` 是 falsy：以前写成 `if manifest.sha256:`，于是
            #   「要不要校验」这个开关落在了 manifest 的提供方手上 —— 不给就跳过，
            #   下载来的 exe 直接进安装器（`-ExecutionPolicy Bypass` + `/S` 静默）。
            # 宁可不更新，也不装一个不知道是什么的东西。
            _update_state["status"] = "verifying"
            if not manifest.sha256:
                dest_part.unlink(missing_ok=True)
                _update_state["status"] = "error"
                _update_state["error"] = ("manifest 没给 sha256 —— 拒绝投放"
                                          "（没有校验和就无法确认这个包是什么）")
                return None
            if not self.verify_checksum(dest_part, manifest.sha256):
                dest_part.unlink(missing_ok=True)
                _update_state["status"] = "error"
                _update_state["error"] = "SHA256 checksum mismatch — download discarded"
                return None

            # 收口用 replace，不用 rename。Windows 上 os.rename 碰到已存在的目标会抛
            #   FileExistsError，于是同一个版本下第二次下载（装失败后点重试，或者上次
            #   下到一半断了）必然失败，而那句错误看起来像磁盘坏了。走到这里时哈希
            #   已经验过，覆盖是事先想清楚的事，不是巧合。
            dest_part.replace(dest_final)
            _update_state["status"] = "ready"
            _update_state["downloaded_path"] = str(dest_final)
            mark_pending(manifest, dest_final)
            return dest_final

        except requests.RequestException as e:
            _update_state["status"] = "error"
            _update_state["error"] = f"Download error: {e}"
            dest_part.unlink(missing_ok=True)
            return None
        except Exception as e:
            _update_state["status"] = "error"
            _update_state["error"] = str(e)
            dest_part.unlink(missing_ok=True)
            return None

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
    Silently skips if network is unavailable or check was done recently.
    """
    from core.version import __version__

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

            settings["update_last_check"] = time.time()

            if manifest is not None:
                ignored = settings.get("update_ignored_version")
                if ignored == manifest.version:
                    return  # user dismissed this version
                _update_state["latest_version"] = manifest.version
                _update_state["latest_manifest"] = manifest.__dict__
                #: 自动下载（用户 2026-11-13 定的那一半）：
                #:   查到就下，下完在磁盘上留一个待安装标记；界面那条黄字与
                #:   下次启动的「正在更新」都读它。失败不抛 —— 网络断着时
                #:   下次启动会再试，而这里抛出去只会把整个检查静默掉。
                #: 源码树里不下：那里装不了（安装包没有安装目录可覆盖），
                #:   白占 263 MB 且没有任何东西会用它。
                try:
                    from core import paths as _paths
                    installed = _paths.is_installed()
                except Exception:                         # noqa: BLE001
                    installed = False
                if installed:
                    try:
                        already = pending_install()
                        if not (already and already.get("version") == manifest.version):
                            checker.download(manifest)
                    except Exception:                     # noqa: BLE001
                        pass

            _write_settings(settings)
        except Exception:
            pass  # silent — network may be down

    t = threading.Thread(target=_run, daemon=True, name="update-check")
    t.start()
