#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自带 Python 运行时 —— 下载 / 校验 / 组装 / 事务落地。

    .venv/Scripts/python.exe scripts/prepare_runtime.py            # 备好（需要时下载）
    .venv/Scripts/python.exe scripts/prepare_runtime.py --smoke    # 只跑冒烟
    .venv/Scripts/python.exe scripts/prepare_runtime.py --no-smoke # 备好但不跑

## 一句话

把 `apps/desktop/primary-runtime-lock.json` 里锁住的那个解释器，
逐字节核对哈希之后，组装到 `apps/desktop/runtime/primary-runtime/`，
并写一份 `runtime.json`（谁在里面 + `payloadDigest`）。

## 八条判据（逐条落在这里）

| # | 判据 | 落在哪 |
|---|---|---|
| 1 | 锁文件钉解释器（换版本只改锁，不改代码） | `load_lock` / `asset_*` |
| 2 | 内容寻址下载缓存（键 = sha256，命中就不重下） | `download` |
| 3 | 无执行组装（从不跑 pip / 从不 exec 任何东西） | `assemble` 全程只有解包与写文件 |
| 4 | `runtime.json` + `payloadDigest` | `payload_digest` / `assemble` |
| 5 | 事务式落地（`.previous` 回滚） | `land` |
| 6 | `extraResources`（asar 之外） | 归桌面壳（本脚本只管产出那个目录） |
| 7 | 拒符号链接 | `_tar_filter` |
| 8 | 只读工具 | 全程 `urllib` / `tarfile` / `json`，没有第三方依赖 |

## 两条下载路（都是官方源，不碰第三方代理）

`github.com` 不可达，而 `api.github.com` 与
`release-assets.githubusercontent.com` 可达。-> 先试常规的
`github.com/.../releases/download/...`，不行再走 release-assets API
（`api.github.com/.../releases/assets/<id>` + `Accept: application/octet-stream`，
它 302 到 `release-assets.githubusercontent.com`）。两条路拿的是同一个字节，
而哈希是唯一的验收判据 —— 所以走哪条都不影响正确性。

## 两处如实记

1. 锁里的 wheel 表今天有 135 条（`apps/desktop/primary-runtime-lock.json`）：
   同一条内容寻址 + 同一个无执行解包装出 `site-packages`，`runtime.json` 里
   记着逐包的 `pythonPackages`。表空了就打不出能跑的包（只有解释器，
   没有 `flask` / `mcp` / `openpyxl` 那些）—— 脚本会出声。
2. 本机开发环境是 3.13.6，而 standalone 只出最新补丁（锁里是 3.13.16）——
   跟大版本对齐、不跟补丁号对齐。这一处差异是有意的。

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LOCK = _ROOT / "apps" / "desktop" / "primary-runtime-lock.json"
#: 组装产物（桌面壳的 `extraResources` 就指这里）。
RUNTIME_DIR = _ROOT / "apps" / "desktop" / "runtime" / "primary-runtime"
#: 内容寻址下载缓存（键 = sha256）。
CACHE_DIR = _ROOT / "apps" / "desktop" / ".runtime-cache"

_RELEASE_BASE = "https://github.com/astral-sh/python-build-standalone/releases/download"

#: `锁["target"]` → `runtime.json` 里的 `(platform, arch)`。
#:
#: 只列真有产物的那几格。此前这里是
#: `"win32" if "windows" in pythonTarget else "darwin"` —— 一个 `else` 把
#: 任何未知目标（包括 linux）都标成 `darwin`，而这份 `runtime.json`
#: 只在装好的机器上才被看见（平台矩阵与实际不一致）。
#:
#: 加一格的前提是同时有那三样：`vendor/ripgrep/<平台>-<架构>/` 的二进制 ·
#: 该平台的 Electron 构建 · 该平台的安装器。差一样就别加 ——
#: 说不出来的支持等于没支持，而说了却做不到的东西会一路绿灯发出去。
_TARGET_PLATFORM = {
    "win-x64": ("win32", "x64"),
}


def target_platform(target: str) -> tuple[str, str]:
    """`target` → `(platform, arch)`；不认识就当场拒，不猜。"""
    got = _TARGET_PLATFORM.get(target)
    if got is None:
        raise SystemExit(
            "锁里的 target=%r 没有平台映射（已知：%s）—— 不许猜："
            "猜错的 platform 只会写在装好那台机器的 runtime.json 里。"
            % (target, ", ".join(sorted(_TARGET_PLATFORM))))
    return got


# ----------------------------------------------------------------------
# 1. 锁
# ----------------------------------------------------------------------

def load_lock(path: Path = _LOCK) -> dict:
    """读锁。校验它是这个脚本认得的形状（`format`），别拿一份陌生的当自己的。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("format") != 1:
        raise SystemExit("锁文件 format=%r —— 这个脚本只认 1" % raw.get("format"))
    for key in ("target", "pythonVersion", "pythonRelease", "pythonTarget",
                "flavor", "sha256"):
        if not raw.get(key):
            raise SystemExit("锁文件缺 %r" % key)
    return raw


def asset_filename(lock: dict) -> str:
    return "cpython-%s+%s-%s-%s.tar.gz" % (
        lock["pythonVersion"], lock["pythonRelease"],
        lock["pythonTarget"], lock["flavor"])


def asset_urls(lock: dict) -> list[str]:
    """两条官方路，按顺序试（第一条不成再第二条）。"""
    name = asset_filename(lock)
    urls = ["%s/%s/%s" % (_RELEASE_BASE, lock["pythonRelease"], name)]
    if lock.get("assetId"):
        urls.append("https://api.github.com/repos/astral-sh/"
                    "python-build-standalone/releases/assets/%s" % lock["assetId"])
    return urls


# ----------------------------------------------------------------------
# 2. 下载（内容寻址 + 逐字节核对）
# ----------------------------------------------------------------------

def _fetch(url: str, *, accept: str | None = None, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "crannia-prepare-runtime"})
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(lock: dict, cache_dir: Path = CACHE_DIR) -> Path:
    """把锁住的那个资产交出来（必要时先下）。

    内容寻址：缓存文件名就是它的 sha256 -> 命中即用，且照样再核对一遍
    （缓存被谁改过、被磁盘坏了，都当场现形）。
    哈希是唯一的验收判据 —— 走哪条路、经过谁，都不影响正确性。
    """
    want = lock["sha256"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / want
    if dest.exists():
        got = _sha256_of(dest.read_bytes())
        if got == want:
            print("[runtime] 缓存命中：%s" % dest.name[:16])
            return dest
        print("[runtime] 缓存内容与锁不符（%s）—— 丢掉重下" % got[:16])
        dest.unlink()

    last_err = None
    for url in asset_urls(lock):
        host = url.split("/")[2]
        try:
            print("[runtime] 下载 %s …" % host)
            accept = ("application/octet-stream" if "api.github.com" in host else None)
            data = _fetch(url, accept=accept)
        except Exception as exc:                                # noqa: BLE001
            last_err = "%s: %s" % (host, exc)
            print("[runtime]   %s 不成（%s）" % (host, type(exc).__name__))
            continue
        got = _sha256_of(data)
        if got != want:
            raise SystemExit("[runtime] **哈希不符**！\n  想要 %s\n  拿到 %s\n"
                             "（来源：%s）—— 这一份不许用。" % (want, got, url))
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        print("[runtime] 下好并核对通过：%.1f MB" % (len(data) / 1048576.0))
        return dest

    raise SystemExit("[runtime] 两条官方路都下不来：%s" % last_err)


# ----------------------------------------------------------------------
# 3. 无执行组装
# ----------------------------------------------------------------------

def _tar_filter(member, dest: str):
    """拒符号链接（判据 7）。

    一个 tar 里的软链接可以把"解包出来的东西"指到包外去 —— 组装阶段**没有执行
    任何东西**，所以它是这条路线上唯一能翻出围栏的动作。
    -> 链接一律拒（不是"跳过"：跳过会让产物静默少文件，那更难查）。
    绝对路径与 `..` 由 `tarfile` 的 `data` 过滤器挡（Python 3.12+），
      这里只补它不挡的那一条（链接）。
    """
    if member.issym() or member.islnk():
        raise SystemExit("[runtime] 压缩包里带链接，拒：%s" % member.name)
    return member


def extract_python(archive: Path, dest: Path) -> Path:
    """把解释器解到 *dest*。返回那个 python.exe 的路径。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(dest, filter=_tar_filter)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise SystemExit("[runtime] 压缩包里应当**恰好一个**顶层目录，实得 %d 个"
                         % len(roots))
    exe = roots[0] / ("python.exe" if os.name == "nt" else "bin/python")
    if not exe.exists():
        raise SystemExit("[runtime] 解出来的目录里没有解释器：%s" % exe)
    return roots[0]


#: wheel 里 `*.data/<scheme>/` 那几种安装路径 -> 各自该落到哪儿。
#:
#: 不能只认 `scripts`：把 161 个 wheel 摆出来一看，
#:   **`fonttools` 带 `*.data/data/share/man/man1/ttx.1`**
#:「拒」的那条路会当场拦住整条组装链。
#: 判据是先数分支（161 个 wheel 里只有两种 scheme：`scripts` 6 处 · `data` 1 处）
#:   —— 而不是"把规则放宽到能过为止"。所以这里是把 wheel 规范里那四种补齐，
#:   而且各自落到它该去的地方（`sys.prefix` 那几个标准位置），
#:   不认识的 scheme 仍然拒（那才是"装它要执行点什么"的那一类）。
#:
#: `scripts` 必须落到 `sys.prefix/Scripts` —— 丢进 `site-packages`
#:   会变成 `…data/scripts/x` 这样的垃圾路径。
#:   这条语义进 `payload_digest`（有读者钉着）。
_WHEEL_SCHEMES = ("purelib", "platlib", "scripts", "data")


def _wheel_dest(scheme: str, rest: str, site_packages: Path, prefix: Path) -> Path:
    """wheel 里那一段该落到哪儿（一处定义）。"""
    if scheme in ("purelib", "platlib"):
        return site_packages / rest
    if scheme == "scripts":
        #: Windows 上 `sys.prefix/Scripts`；POSIX 上是 `sys.prefix/bin`。
        return prefix / ("Scripts" if os.name == "nt" else "bin") / rest
    return prefix / rest                      # `data` -> 相对 `sys.prefix`


def unpack_wheel(path: Path, site_packages: Path, prefix: Path | None = None) -> None:
    """把一个 wheel 解到 *site-packages* —— 不跑 pip、不 exec（判据 3）。

    三种成员分别落位（见 `_WHEEL_SCHEMES`）：
      普通成员 -> `site-packages`；`*.data/{purelib,platlib}/` -> `site-packages`；
      `*.data/scripts/` -> `prefix/Scripts`；`*.data/data/` -> `prefix/`。
    认不出的 scheme 一律拒 —— 别的方案意味着"装它要执行点什么"，
      而这条路从不执行任何东西。拒，而不是装作没看见。

    *prefix* 不给就按 `site_packages` 推（`<prefix>/Lib/site-packages` -> `<prefix>`）。
    """
    prefix = prefix or site_packages.parent.parent
    site_packages.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            parts = info.filename.split("/")
            if len(parts) > 1 and parts[0].endswith(".data"):
                if parts[1] not in _WHEEL_SCHEMES:
                    raise SystemExit("[runtime] wheel 要求不支持的安装路径：%s"
                                     % info.filename)
            if (info.external_attr >> 16) & 0xA000 == 0xA000:    # 符号链接
                raise SystemExit("[runtime] wheel 里带链接，拒：%s" % info.filename)
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/")
            if len(parts) > 1 and parts[0].endswith(".data"):
                dest = _wheel_dest(parts[1], "/".join(parts[2:]), site_packages, prefix)
            else:
                dest = site_packages / info.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def unpack_sdist(path: Path, site_packages: Path, packages: list[str]) -> None:
    """把一个 sdist 里点名的那几个顶层目录取进 *site-packages*。

    为什么是"点名"而不是"通用地解"：通用地解一个 sdist 等于跑它的构建脚本
      （`setup.py`），而这条路从不执行任何东西（判据 3），那就只能靠声明：
      锁里那一条自带 `"packages": [...]`，说的是"这个压缩包里哪几个目录是 import 包"。
      没声明就拒（不是猜、不是跳过）—— 猜错的症状是"打包之后某个功能才崩"。

    取的时候剥掉第一层目录（sdist 的约定：`jieba-0.42.1/jieba/...`），
      并且只搬声明过的那几个（`setup.py` / `test/` / `PKG-INFO` 一律不要 ——
      它们不是运行期需要的东西，进 site-packages 只会被 `import` 撞上）。
    同 `unpack_wheel`：带链接就拒。
    """
    if not packages:
        raise SystemExit("[runtime] sdist 那一条没有声明 `packages`：%s" % path.name)
    site_packages.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path) as tf:
        members = tf.getmembers()
        for m in members:
            if m.issym() or m.islnk():
                raise SystemExit("[runtime] sdist 里带链接，拒：%s" % m.name)
        tops = {m.name.split("/")[0] for m in members if m.name}
        if len(tops) != 1:
            raise SystemExit("[runtime] sdist 的第一层不是一个目录：%r" % sorted(tops))
        root = tops.pop() + "/"
        picked = [m for m in members
                  if m.name.startswith(root)
                  and m.name[len(root):].split("/")[0] in packages]
        found = {m.name[len(root):].split("/")[0] for m in picked if m.name != root}
        missing = sorted(set(packages) - found)
        if missing:
            #: 声明的目录在包里不存在，拒。装上了而少一个包 =
            #:   运行期 `ImportError`，而那时已经离出错点很远了。
            raise SystemExit("[runtime] sdist 里没有声明的那几个目录：%r（有：%r）"
                             % (missing, sorted(found)))
        for m in picked:
            tf.extract(m, site_packages, filter="data")
            moved = site_packages / m.name
            dest = site_packages / m.name[len(root):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            if moved != dest:
                os.replace(moved, dest)
        #: 剥完第一层之后那个空壳（`jieba-0.42.1/`）—— 留着它也没有害处，
        #: 但 site-packages 里多一个不是包名的目录会让人以为"少装了什么"。
        shutil.rmtree(site_packages / root.rstrip("/"), ignore_errors=True)


def payload_digest(lock: dict) -> str:
    """这份产物是什么的哈希 —— 由锁住的输入算出来（不是结果目录的哈希）。

    为什么不用"结果目录的哈希"：那样每次重建都会变（mtime / 顺序），
      而它要回答的是"输入变了没有" -> 变了就该重装。
    蓝本逐字：改了组装方式但锁没变时，要把 `format` 加一。

    摘要必须覆盖两处容易漏掉的输入，漏了会让摘要变假：
      ① 每一条不再只取 `sha256` —— 连 `kind` / `packages` / `url` 一起进摘要。
         只取哈希的话，「把一条从 wheel 改成 sdist（或反过来）」摘要不变，
         而产物的字节已经不同了（`payloadDigest` 就成了一句假话）。
      ② 加 `pythonPackages`（那张人读的版本表）—— 它是锁的一部分，
         进摘要才能保证"锁里写的版本"与"装进去的字节"是同一次事实。
    """
    return hashlib.sha256(json.dumps({
        "format": lock["format"],
        "target": lock["target"],
        "pythonVersion": lock["pythonVersion"],
        "pythonRelease": lock["pythonRelease"],
        "pythonTarget": lock["pythonTarget"],
        "flavor": lock["flavor"],
        "sha256": lock["sha256"],
        "wheels": [{k: w.get(k) for k in ("sha256", "kind", "packages", "url")}
                   for w in (lock.get("wheels") or [])],
        "pythonPackages": lock.get("pythonPackages") or {},
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def fetch_wheels(lock: dict, cache_dir: Path = CACHE_DIR) -> int:
    """把 `wheels` 表里那些字节取进缓存（键 = sha256）—— 回新下几个。

    与解释器那条同一台机器：内容寻址 + 逐字节核对 + 命中也再核一遍。
      走哪条路、经过谁都不影响正确性 —— 哈希是唯一的验收判据。

    只认官方主机（`files.pythonhosted.org`）—— 一个第三方镜像的地址进了锁，
      就等于把供应链换成它了。这条在取的那一侧再判一次（不只靠
      `scripts/pin_wheels.py` 那一侧判）：锁是可以被人手改的，
      而"手改过的锁"正是这条判据要防的那一种。
    """
    wheels = lock.get("wheels") or []
    if not wheels:
        print("[runtime] wheel 表是空的 —— 跳过（先跑 `scripts/pin_wheels.py`）")
        return 0
    cache_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for w in wheels:
        want = str(w.get("sha256") or "")
        url = str(w.get("url") or "")
        if not want or not url:
            raise SystemExit("[runtime] 锁里这一条缺 sha256/url：%r" % (w,))
        host = url.split("/")[2] if "://" in url else ""
        if host not in _ALLOWED_WHEEL_HOSTS:
            raise SystemExit("[runtime] **wheel 地址不是官方主机**（%s）：%s"
                             % (host, url))
        dest = cache_dir / want
        if dest.exists():
            if _sha256_of(dest.read_bytes()) == want:
                continue
            print("[runtime] 缓存内容与锁不符（%s）—— 丢掉重下" % w.get("name"))
            dest.unlink()
        data = _fetch(url)
        got = _sha256_of(data)
        if got != want:
            raise SystemExit("[runtime] **哈希不符**！%s\n  想要 %s\n  拿到 %s\n"
                             "（来源：%s）—— 这一份不许用。"
                             % (w.get("name"), want, got, url))
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        fetched += 1
    print("[runtime] wheel 缓存：%d 条（这次新下 %d 条）" % (len(wheels), fetched))
    return fetched


def assemble(lock: dict, archive: Path, out_dir: Path = RUNTIME_DIR,
             *, prune: bool = True, tiktoken: bool = True) -> dict:
    """组装到临时目录，再事务式落地（判据 5）。返回那份 `runtime.json`。"""
    staging = Path(tempfile.mkdtemp(prefix="lv-runtime-"))
    try:
        root = extract_python(archive, staging / "unpacked")
        payload = staging / "payload"
        payload.mkdir(parents=True)
        target = payload / "python"
        shutil.move(str(root), str(target))

        wheels = lock.get("wheels") or []
        site = target / "Lib" / "site-packages"
        n_sdist = 0
        for w in wheels:
            blob = CACHE_DIR / w["sha256"]
            if not blob.exists():
                #: 出声且拒 —— 静默跳过会让产物少文件而看不出来
                #:   （静默少一个 = 让人以为装上了）。缺的先跑一次下载。
                raise SystemExit("[runtime] 锁里的 wheel 不在缓存里：%s(%s)"
                                 % (w.get("name"), w["sha256"][:16]))
            if w.get("kind") == "sdist":
                unpack_sdist(blob, site, w.get("packages") or [])
                n_sdist += 1
            else:
                unpack_wheel(blob, site)
        if wheels:
            print("[runtime] 解包 %d 个依赖（无执行；其中 sdist %d 条）"
                  % (len(wheels), n_sdist))

        #: 两件都在落地之前做（`prune_runtime` 判据 ②）：
        #:   ① 词表随包走（否则安装版第一次用模型要联网下载）
        #:   ② 瘦身（装出来的包因此少三成文件 —— 安装速度按文件数付费）
        token_info = vendor_tiktoken_cache(payload) if tiktoken else {"skipped": True}
        prune_info = prune_runtime(payload) if prune else {"skipped": True}

        plat, arch = target_platform(lock["target"])
        manifest = {
            "format": lock["format"],
            "target": lock["target"],
            "platform": plat,
            "arch": arch,
            "pythonVersion": lock["pythonVersion"],
            "pythonRelease": lock["pythonRelease"],
            "flavor": lock["flavor"],
            "payloadDigest": payload_digest(lock),
            "pythonPackages": lock.get("pythonPackages") or {},
            #: 这张表空着是个事实，不是没写 —— 下游（桌面壳 / 冒烟）据此出声。
            "wheelCount": len(wheels),
            "readyToRunFramework": bool(wheels),
            #: 「这个包里删掉了什么、为什么」—— 没有这一格，下一次有人问
            #:   "包里怎么少了个 idlelib" 就没人答得上来。
            "prune": prune_info,
            "tiktokenCache": token_info,
        }
        (payload / "runtime.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        land(payload, out_dir)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def land(payload: Path, out_dir: Path) -> None:
    """事务式落地（判据 5）—— 旧的那份改名成 `.previous`，不是删掉。

    判据是"换到一半失败时不许两边都没有"：先 `rm -rf` 再拷的话，
      中途断电/报错就什么都没有了（而用户那边正开着一个应用）。
    天上掉下来的好处：运行中的 exe 不能删、但能改名（Windows）——
      这正是桌面壳那边"应用在跑也换得掉"的同一条判据。
    """
    prev = out_dir.with_name(out_dir.name + ".previous")
    if prev.exists():
        shutil.rmtree(prev)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    if out_dir.exists():
        os.replace(out_dir, prev)
    try:
        shutil.move(str(payload), str(out_dir))
    except Exception:
        if prev.exists() and not out_dir.exists():
            os.replace(prev, out_dir)          # 回滚
        raise
    #: 成了才清（失败那条路上它是唯一的一份）。
    if prev.exists():
        shutil.rmtree(prev, ignore_errors=True)


# ----------------------------------------------------------------------
# 4. 瘦身（构建期删；落地前跑，所以装出来的包从一开始就少这些文件）
# ----------------------------------------------------------------------

#: 打了但运行期没有任何代码路径会碰的东西。
#:   判据不是"看着像垃圾"，而是能说出谁在用；说不出来的，删之前先问。
#:   数字是在真产物上当场数的（`win-unpacked`）。
PRUNE_DIRS = (
    ("Lib/idlelib", "IDLE 是编辑器，运行期不加载（1.2 MB / 90 文件）"),
    ("Lib/turtledemo", "Turtle 的演示脚本"),
    ("Lib/ensurepip", "自带一份 pip 的 wheel，只有『首次引导虚拟环境』用"),
    ("Lib/venv", "我们不创建虚拟环境"),
    ("Lib/pydoc_data", "`pydoc` 的文档数据"),
    ("Lib/test", "CPython 自带测试套件"),
    ("include", "C 头文件 —— 只有编译扩展才要（1.8 MB / 264 文件）"),
    ("libs", "Windows 的 .lib 导入库，同理"),
    ("Lib/site-packages/pythonwin", "pywin32 的 IDE；我们要的是它的 `win32*` API"),
)
#: 点名的二进制：`magika.exe` 是命令行工具，docvortex 走的是它的 Python API（24.4 MB）。
PRUNE_FILES = ("Scripts/magika.exe",)
#: site-packages 里这些名字的子目录整个删（包自带的测试；scipy/numpy/sklearn 是大头）。
PRUNE_PKG_SUBDIRS = ("tests", "test")
#: pip 不删：`run_command` 允许模型跑 `pip install`（服务端有专门的审批规则），
#:   而装进自带运行时是它唯一的一条路。874 个文件（10.7 MB）买这个能力，值。


def _measure(path: Path) -> tuple[int, int]:
    n = s = 0
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 0, 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                s += os.path.getsize(os.path.join(dp, f))
                n += 1
            except OSError:
                pass
    return n, s


def prune_runtime(payload: Path) -> dict:
    """删掉运行期用不到的东西，回一份读数（进了 `runtime.json`）。

    三条判据：
      ① 只删"说得清谁不用它"的 —— 说不出理由的先留着。
         反例是"看着没 import、其实是别的包的声明依赖"那一类：先查反向依赖再动。
         `kubernetes` / `scipy` / `scikit-learn` 今天不在 `requirements.txt` 里，
         是因为没人用它们 —— 不是因为体积。
      ② 在落地之前删 —— 删完的东西不经过 `land()` 的事务，少一次失败面。
      ③ 删了什么要写进产物（`runtime.json.prune`）—— 否则下一次有人问
         "包里怎么少了个 idlelib"，没人答得上来。
    """
    py = payload / "python"
    detail: list[dict] = []
    removed_files = removed_bytes = 0

    def _rm(path: Path, why: str) -> None:
        nonlocal removed_files, removed_bytes
        if not path.exists():
            return
        n, s = _measure(path)
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
        except OSError:
            return
        removed_files += n
        removed_bytes += s
        detail.append({"path": str(path.relative_to(payload)).replace("\\", "/"),
                       "files": n, "bytes": s, "why": why})

    for rel, why in PRUNE_DIRS:
        _rm(py / rel, why)
    for rel in PRUNE_FILES:
        _rm(py / rel, "命令行二进制，运行期不走它")

    site = py / "Lib" / "site-packages"
    if site.is_dir():
        for pkg in sorted(site.iterdir()):
            if not pkg.is_dir():
                continue
            for name in PRUNE_PKG_SUBDIRS:
                _rm(pkg / name, "包自带的测试")

    #: `.pyc` 单独数（一万多个，逐条进 detail 会把 runtime.json 撑成大文件）。
    pyc_n = pyc_s = 0
    for dp, dn, fn in os.walk(py):
        for f in fn:
            if f.endswith(".pyc"):
                p = Path(dp) / f
                try: pyc_s += p.stat().st_size; pyc_n += 1
                except OSError: pass
    if pyc_n:
        for dp, dn, fn in os.walk(py, topdown=False):
            for f in fn:
                if f.endswith(".pyc"):
                    try: (Path(dp) / f).unlink()
                    except OSError: pass
            if Path(dp).name == "__pycache__":
                try: Path(dp).rmdir()
                except OSError: pass
        removed_files += pyc_n
        removed_bytes += pyc_s
        detail.append({"path": "**/*.pyc", "files": pyc_n, "bytes": pyc_s,
                       "why": "编译缓存，首次 import 会自己生成"})

    print("[runtime] 瘦身：删 %d 个文件 / %.1f MB"
          % (removed_files, removed_bytes / 1e6))
    return {"files": removed_files, "bytes": removed_bytes, "rules": detail}


# ----------------------------------------------------------------------
# 4b. tiktoken 词表（随包走，否则装出来的应用第一次用模型要联网下载）
# ----------------------------------------------------------------------

#: `tiktoken.get_encoding("cl100k_base")` 第一次会去这里取 BPE 文件。
#:   安装版必须自带 —— 断网/被墙的机器上，少了它 provider 构造那一刻就抛。
#:   消费者是 `core/tokenizer.py`（它把 `TIKTOKEN_CACHE_DIR` 指到 `<runtime>/tiktoken-cache`）。
#: 这是第二条外部主机（第一条是 `files.pythonhosted.org`）。加它的理由：
#:   这个词表不在任何 wheel 里，而锁住的 sha256 使它仍是内容寻址（哈希是验收判据）。
_TIKTOKEN_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
_TIKTOKEN_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
#: tiktoken `load.py::read_file_cached` 的缓存键 = sha1(URL)（不是文件名）。
_TIKTOKEN_CACHE_KEY = hashlib.sha1(_TIKTOKEN_URL.encode()).hexdigest()


def vendor_tiktoken_cache(payload: Path, cache_dir: Path = CACHE_DIR) -> dict:
    """把词表放进 `payload/tiktoken-cache/<sha1(url)>`，回一份读数。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    blob = cache_dir / _TIKTOKEN_SHA256
    if not blob.exists():
        data = _fetch(_TIKTOKEN_URL)
        got = _sha256_of(data)
        if got != _TIKTOKEN_SHA256:
            raise SystemExit("[runtime] tiktoken 词表哈希对不上：期望 %s，实得 %s"
                             % (_TIKTOKEN_SHA256[:16], got[:16]))
        blob.write_bytes(data)
    data = blob.read_bytes()
    if _sha256_of(data) != _TIKTOKEN_SHA256:
        raise SystemExit("[runtime] 缓存里的 tiktoken 词表被改过：%s" % blob)
    out = payload / "tiktoken-cache"
    out.mkdir(parents=True, exist_ok=True)
    (out / _TIKTOKEN_CACHE_KEY).write_bytes(data)
    print("[runtime] tiktoken 词表：%.1f MB -> tiktoken-cache/%s"
          % (len(data) / 1e6, _TIKTOKEN_CACHE_KEY))
    return {"url": _TIKTOKEN_URL, "sha256": _TIKTOKEN_SHA256,
            "cacheKey": _TIKTOKEN_CACHE_KEY, "bytes": len(data)}


# ----------------------------------------------------------------------
# 4. 冒烟（真跑一次，不是"装上了就算"）
# ----------------------------------------------------------------------

#: 这几样是逐个 import + 真算一次 ——
#:   只 import 不算的话，一个"装上了但算不出来"的解释器照样能过。
SMOKE_CODE = (
    "import decimal, xml.parsers.expat, lzma, uuid, sqlite3, json, ssl; "
    "assert sum(range(5)) == 10; "
    "assert decimal.Decimal('1.1') + decimal.Decimal('2.2') == decimal.Decimal('3.3'); "
    #: `ZoneInfo` 在 Windows 上没有 tzdata 就直接抛 ——
    #:   import 得过不代表能用（`tzdata` 是数据包，不是模块）。
    "from zoneinfo import ZoneInfo; "
    "ZoneInfo('Asia/Shanghai'); "
    "print('smoke ok')"
)

#: 框架真用得到的那些，逐个 import。
#:
#: 为什么非要有这一条：只用标准库那一段过得了一个什么依赖都没装的运行时
#:   —— 而那种运行时跑不起框架。表填上之后，"装上了"与"真能用"是两件事
#:   （一个解包错层、缺 `.pyd`、或者少一层目录的 wheel，都能让 import 当场炸）。
#: 名字取的是import 名不是发行名（`PIL` / `pypdf` / `docx` / `yaml`）。
#: 换掉 PDF 栈之后必须同步这一行：这里曾写着 `fitz`（PyMuPDF），
#:   换栈后它让冒烟假红了一次 —— 冒烟清单自己也是「会腐坏的锚点」。
#: 这几样故意挑的是"框架启动就要用、或常驻工具就要用"的：
#:   太长的全量清单会让冒烟变成一次完整启动（那是另一个更贵的验收）。
SMOKE_IMPORTS = ("flask", "waitress", "yaml", "httpx", "openai", "anthropic",
                 "openpyxl", "docx", "pptx", "pypdf", "pdfplumber", "jieba",
                 "tiktoken", "rank_bm25", "numpy", "PIL", "dotenv", "psutil",
                 "resvg_py",
                 #:  补的三条 —— 它们正是"开发机有、锁里没有"的那一类：
                 #:   `matplotlib`（`plot_excel_data`）· `curl_cffi`（`web_search`
                 #:   传输层）· `tzdata`（`ZoneInfo` 的数据，见上面 `SMOKE_CODE`）。
                 "matplotlib", "curl_cffi")


def python_exe(out_dir: Path = RUNTIME_DIR) -> Path:
    return out_dir / "python" / ("python.exe" if os.name == "nt" else "bin/python")


def _run_child(exe: Path, code: str, timeout: int = 300):
    """在随包那个解释器上跑一段代码，回 `(rc, 输出)`。

    两处都不是好看的，是判据：

    ① `-X utf8` —— **`-I` 自己带 `-E`，所以 `PYTHONIOENCODING` 在子进程里是被忽略的**
想让子进程按 UTF-8 说话，只能用命令行开关。少了它，子进程的
       `SyntaxWarning` 里那串含中文的路径会按 GBK 打出来。
    ② `errors="replace"` —— 少了它，上面那串 GBK 字节会让父进程的读取线程抛异常
       并静默死掉（`subprocess` 的 `_readerthread` 不把异常传出来）
我们丢掉的是子进程的 stderr，而失败路径上那段正是唯一的线索。
       （`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb8`，
        而那一趟照样报"冒烟通过" —— 因为 stdout 是干净的。）
    """
    return subprocess.run(
        [str(exe), "-I", "-X", "utf8", "-B", "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)


def smoke(out_dir: Path = RUNTIME_DIR) -> None:
    """在刚组装出来的那个解释器上真跑一次。"""
    exe = python_exe(out_dir)
    if not exe.exists():
        raise SystemExit("[runtime] 冒烟：没有解释器 %s（先跑一次组装）" % exe)
    r = _run_child(exe, SMOKE_CODE, timeout=120)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0 or "smoke ok" not in out:
        raise SystemExit("[runtime] 冒烟**没过**：\n%s" % out[-500:])
    print("[runtime] 冒烟通过（%s）" % out.splitlines()[-1])

    manifest = json.loads((out_dir / "runtime.json").read_text(encoding="utf-8"))
    if not manifest.get("readyToRunFramework"):
        #: 出声，不装作好了（跑了 / 有变化 / 缺了什么，三件事都要说）。
        print("[runtime] [!] 注意：**wheel 表是空的**（wheelCount=%d）"
              "-> 这个运行时**跑不起框架**（没有 flask / mcp / openpyxl 那些）。"
              "先跑 `scripts/pin_wheels.py` 把表填上。"
              % manifest.get("wheelCount", 0))
        return
    smoke_imports(out_dir)


def smoke_imports(out_dir: Path = RUNTIME_DIR) -> None:
    """真 import 一遍框架要用的那些。

    判据是"装上了"与"真能用"是两件事：一个解包错层、少一层目录、
      或者缺 `.pyd` 的 wheel，解包那一步全都成功，而 `import` 当场炸。
只报 `wheelCount` 是没有验收到的。

    `-I`（隔离）是承重的：它同时屏蔽 `PYTHONPATH` 与用户 site-packages
这几行 import 只可能来自随包那份运行时自己的 `site-packages`。
      少了它，冒烟会借用开发机上装好的包，"通过"是一句假话。
    """
    exe = python_exe(out_dir)
    code = ("import %s; print('framework imports ok')" % ", ".join(SMOKE_IMPORTS))
    r = _run_child(exe, code, timeout=300)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0 or "framework imports ok" not in out:
        raise SystemExit(
            "[runtime] **框架依赖 import 没过**（%d 条里有一条装不上）：\n%s\n"
            "  -> 表填了但产物是坏的 —— 别把它当「能跑」。"
            % (len(SMOKE_IMPORTS), out[-800:]))
    print("[runtime] 框架依赖 import 通过（%d 个）" % len(SMOKE_IMPORTS))


# ----------------------------------------------------------------------

#: 只认这一个主机（与 `scripts/pin_wheels.py::ALLOWED_HOSTS` 同一条判据，
#:   这里是取的那一侧再判一次 —— 锁是可以被人手改的）。
_ALLOWED_WHEEL_HOSTS = ("files.pythonhosted.org",)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Crannia 自带 Python 运行时")
    ap.add_argument("--smoke", action="store_true", help="只跑冒烟，不下载/不组装")
    ap.add_argument("--no-smoke", action="store_true", help="备好但跳过冒烟")
    ap.add_argument("--fetch-only", action="store_true",
                    help="只把 wheel 取进缓存，不组装（先取再组装，好在断了能接着来）")
    ap.add_argument("--no-prune", action="store_true",
                    help="跳过瘦身（调试用；正常组装**一定**瘦身）")
    ap.add_argument("--no-tiktoken", action="store_true",
                    help="不把 tiktoken 词表带进包（调试用；正常组装**一定**带）")
    args = ap.parse_args(argv)

    lock = load_lock()
    print("[runtime] 锁：python %s+%s · %s · %s · 依赖 %d 条"
          % (lock["pythonVersion"], lock["pythonRelease"],
             lock["pythonTarget"], lock["flavor"], len(lock.get("wheels") or [])))
    if args.smoke:
        smoke()
        return 0

    if args.fetch_only:
        fetch_wheels(lock)
        return 0

    fetch_wheels(lock)
    archive = download(lock)
    manifest = assemble(lock, archive,
                        prune=not args.no_prune,
                        tiktoken=not args.no_tiktoken)
    print("[runtime] 组装好了：%s" % RUNTIME_DIR)
    print("[runtime] payloadDigest = %s" % manifest["payloadDigest"][:16])
    if not args.no_smoke:
        smoke()
    return 0


if __name__ == "__main__":
    sys.exit(main())
