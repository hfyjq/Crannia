#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把打出来的 `resources/app` 准备成一个可发布的 npm 包（代码包 + 清单）。

    .venv/Scripts/python.exe scripts/make_npm_package.py
    # 然后按它打印的命令：npm login / npm publish
    # 发布后等镜像同步：
    .venv/Scripts/python.exe scripts/make_npm_package.py --wait-mirror

## 为什么是 npm

国内直连 GitHub 经常超时；npmmirror 有国内 CDN。
4.6 MB 的代码包适合 npm；267 MB 的全量安装包不适合（体积限制）。

## 它做什么

1. 从 `apps/desktop/release/win-unpacked/resources/app` 复制一份到
   `apps/desktop/release/npm-pkg/`；
2. 写 `package.json`（name / version / license / repository）与 `README.md`；
3. 把发布清单写进 `package.json` 的 `crannia` 字段 —— 客户端的检查更新
   读的就是这一格（`core/updater.py::manifest_from_npm_metadata`）。
   清单文件由 `scripts/make_release.py` 产出（`dist-release/version-<渠道>.json`）；
4. 打印发布命令（发布要账号与 2FA，脚本不代做）。

## 两条判据

- 版本号取自 `core/version.py`（唯一真相源），不另抄一份；
  清单里的版本号与它不一致就拒绝 —— 那会让客户端去下一个不存在的包；
- 不许把 `.env` / `data/` 带进去：复制源本身是打包白名单产物，
  这里再查一遍（漏一个就是发用户凭据）。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SRC = _ROOT / "apps" / "desktop" / "release" / "win-unpacked" / "resources" / "app"
DST = _ROOT / "apps" / "desktop" / "release" / "npm-pkg"
NAME = "@hfyjq/crannia"
REPO = "git+https://github.com/hfyjq/Crannia.git"

#: 默认的清单位置（`make_release.py` 的默认产出目录）。
MANIFEST_DEFAULT = _ROOT / "dist-release" / "version-stable.json"

#: 这几个名字出现在复制源里就停下 —— 它们是凭据或用户数据，不是代码。
FORBIDDEN = (".env", "PDwww", "node_modules", ".venv", "tests")

#: `data/` 不能整目录拒：`data/settings.example.json` 是随包的出厂基准，
#: `core/settings_baseline.py` 按代码根去读它，缺了首次播种就失败。
#: 只拒下面这些"里面真会装用户数据"的名字（整个 `data/` 里其余文件照发）。
FORBIDDEN_DATA = (
    ".env", "settings.json", "allowlist.json", "api_keys.json",
    "wechat_credentials.json", "media_gen_key.txt", "media_gen_credentials.json",
    "runtime", "memory", "events", "incidents", "attachments",
    "last_good", "updates", "plans", "workdata", "logs",
)

#: 镜像同步的轮询上限（秒）。npmjs 发布成功到 npmmirror 可见通常几分钟。
MIRROR_TIMEOUT = 900
MIRROR_INTERVAL = 15


def _load_manifest(path: Path, version: str) -> dict | None:
    """读清单；版本号对不上就拒绝（返回 None 由调用方记一句）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    latest = data.get("latest") if isinstance(data, dict) else None
    if not isinstance(latest, dict):
        return None
    if str(latest.get("version") or "") != str(version):
        print("[npm] manifest version %r != app version %r -- refusing"
              % (latest.get("version"), version))
        return None
    return data


def _mirror_meta(version: str) -> dict | None:
    """等镜像到这一版，返回它的 metadata；等不到返回 None。"""
    import requests

    url = "https://registry.npmmirror.com/%s/latest" % NAME
    deadline = time.time() + MIRROR_TIMEOUT
    while True:
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            seen = str((data or {}).get("version") or "")
            if seen == version:
                print("[npm] mirror is in sync: %s" % version)
                return data
            print("[npm] mirror still at %r (want %r), waiting..." % (seen, version))
        except Exception as exc:  # noqa: BLE001
            print("[npm] mirror poll failed: %s" % exc)
        if time.time() >= deadline:
            print("[npm] mirror did not catch up within %d s -- do not publish "
                  "a manifest pointing at it yet" % MIRROR_TIMEOUT)
            return None
        time.sleep(MIRROR_INTERVAL)


def _stamp(manifest_path: Path, version: str) -> int:
    """把注册中心给的 tarball / integrity 写进静态备源那份清单。

    静态清单是 npm 那条读不到时的备胎；它的代码包那一格必须与真正发出去的
    tarball 对得上，而那个字节只有发布之后才存在。
    """
    data = _load_manifest(manifest_path, version)
    if data is None:
        print("[npm] cannot stamp %s" % manifest_path)
        return 1
    meta = _mirror_meta(version)
    if meta is None:
        return 1
    dist = meta.get("dist") if isinstance(meta.get("dist"), dict) else {}
    tarball = str(dist.get("tarball") or "").strip()
    integrity = str(dist.get("integrity") or "").strip()
    if not tarball or not integrity:
        print("[npm] registry metadata has no tarball/integrity -- refusing")
        return 1
    latest = data["latest"]
    artifacts = latest.setdefault("artifacts", {})
    cp = artifacts.setdefault("codepack", {})
    cp["url"] = tarball
    cp["urls"] = [tarball] + [u for u in (cp.get("urls") or []) if u and u != tarball]
    cp["integrity"] = integrity
    try:
        if dist.get("size"):
            cp["size_bytes"] = int(dist["size"])
    except (TypeError, ValueError):
        pass
    cp.pop("sha256", None)
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print("[npm] stamped %s (codepack integrity from the registry)" % manifest_path)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="准备 npm 代码包（含清单）")
    ap.add_argument("--manifest", default=str(MANIFEST_DEFAULT),
                    help="发布清单（make_release.py 的产物）")
    ap.add_argument("--wait-mirror", action="store_true",
                    help="不打包，只等 npmmirror 同步到 core/version.py 的版本")
    ap.add_argument("--stamp-manifest", default="",
                    help="发布之后：等镜像同步，再把 tarball / integrity 写进这份"
                         "静态清单（备源那一份）")
    args = ap.parse_args(argv)

    from core.version import __version__

    if args.stamp_manifest:
        return _stamp(Path(args.stamp_manifest), str(__version__))
    if args.wait_mirror:
        return _mirror_meta(str(__version__)) is not None and 0 or 1

    if not (SRC / "core" / "version.py").is_file():
        print("[npm] 找不到 %s —— 先 npm run package" % SRC)
        return 1

    bad = [n for n in FORBIDDEN if (SRC / n).exists()]
    bad += ["data/" + n for n in FORBIDDEN_DATA if (SRC / "data" / n).exists()]
    if bad:
        print("[npm] 复制源里有不该发的东西：%r —— 拒绝" % bad)
        return 1

    manifest_path = Path(args.manifest)
    manifest = _load_manifest(manifest_path, str(__version__))
    if manifest is None:
        print("[npm] 读不到可用的清单：%s" % manifest_path)
        print("[npm] 先跑 scripts/make_release.py --url ... --codepack-url ...")
        return 1

    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)
    #: 清单里代码包那一格的地址与哈希在发布之前不存在（tarball 由 npm 打）。
    #:   删掉它们，客户端会从 metadata 的 `dist` 里补齐。
    latest = json.loads(json.dumps(manifest["latest"]))
    cp = (latest.get("artifacts") or {}).get("codepack")
    if isinstance(cp, dict):
        for volatile in ("url", "urls", "sha256", "integrity", "size_bytes"):
            cp.pop(volatile, None)

    pkg = {
        "name": NAME,
        "version": __version__,
        "description": "Crannia application code pack for automatic updates",
        "license": "MIT",
        "repository": {"type": "git", "url": REPO},
        "publishConfig": {"access": "public"},
        #: 清单就在这一格。客户端读 `<注册中心>/<包名>/<tag>` 的 metadata，
        #:   取 `crannia` —— 一次发布同时更新清单与代码包。
        "crannia": latest,
    }
    (DST / "package.json").write_text(
        json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    (DST / "README.md").write_text(
        "Crannia code pack - used by the desktop app's automatic updater.\n"
        "Source: https://github.com/hfyjq/Crannia\n"
        "License: MIT\n", encoding="utf-8")

    print("[npm] 包目录：%s" % DST)
    print("[npm] 版本：%s" % __version__)
    print("[npm] 清单：%s（已写进 package.json 的 crannia 字段）" % manifest_path)
    print("[npm] 下一步（发布必须走官方源）：")
    print('      cd "%s"' % DST)
    print("      npm login --registry=https://registry.npmjs.org")
    print("      npm publish --access public --registry=https://registry.npmjs.org")
    print("[npm] 新包首次发布会进 staged 审核；到 npmjs.com 的 Staged Packages 批准。")
    print("[npm] 发布后等镜像同步（清单指向它之前必须看到新版本）：")
    print("      .venv/Scripts/python.exe scripts/make_npm_package.py --wait-mirror")
    return 0


if __name__ == "__main__":
    sys.exit(main())
