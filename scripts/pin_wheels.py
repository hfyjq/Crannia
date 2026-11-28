#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 `requirements.txt` 与 `tui/requirements.txt` 解析成锁文件里那张 wheel 表。

    .venv/Scripts/python.exe scripts/pin_wheels.py            # 写进锁文件
    .venv/Scripts/python.exe scripts/pin_wheels.py --check     # 只看差多少，不写
    .venv/Scripts/python.exe scripts/pin_wheels.py --report r.json --mirror-host HOST

## 平台与镜像（2026-11-28）

pip 的 `--platform` 不改环境标记：在 Linux 上解析 `win_amd64` 时，
`sys_platform == "linux"` 仍成立，Windows 专用的传递依赖会被漏掉（反向同理）。
一份锁必须在它自己的平台上解析。解析时若官方主机不可达，可以用镜像
（`PIP_INDEX_URL=...` + `--mirror-host <host>`）：镜像保存的 `/packages/...`
路径与文件名和官方逐字相同（85 条同版本 wheel 逐条比过），所以落表时把
主机换回 `files.pythonhosted.org`，路径不动。字节仍由 `sha256` 在组装时复核 ——
哈希是唯一的验收判据。

`--report` 读一份先前 `pip install --dry-run --report` 的产物（这样解析可以在
正确的平台上单独跑）。目标平台与 wheel 文件名对不上会当场拒：Linux 锁里出现
`win_amd64`（或反过来）是"解析跑错了机器"，不修就发一个装不上的运行时。

## 为什么两份需求文件都进同一份锁

GUI 与 TUI 共用一套解释器与 site-packages（2026-11-21）：独立包
不再维护第二份运行时锁，也就不存在"两份锁各说自己是最新"的问题。
代价是 GUI 安装包多几 MB（textual / rich 及其传递依赖）。

## 只想加依赖、不想顺手升级大件

把自己要钉的版本放进一个约束文件，用环境变量交给 pip：

    PIP_CONSTRAINT=C:/Temp/constraints.txt .venv/Scripts/python.exe scripts/pin_wheels.py

约束内容可以是 `锁里每一格 name==version` —— 这样已有版本一个都不动，
只解析新加的那几条。两条坑：pip 把 `PIP_CONSTRAINT` 按空格拆成多个
文件，所以路径不能带空格；解析期仍会下载被选中的 wheel（哪怕只是取哈希），
网络不好时大件会卡住 —— 这正是需要约束的原因。

## 它解决什么

`apps/desktop/primary-runtime-lock.json` 的 `wheels` 表由这个脚本填。
填充机制（内容寻址 + 无执行解包）在 `scripts/prepare_runtime.py` 里。
空表意味着打出来的包跑不起框架（只有解释器，
没有 `flask` / `openpyxl` / `mcp` 那些）。

## 怎么解析的（这一条是判据，别换成自己写一个）

不自己实现解析器 —— 用 `pip` 自己去解析（它是唯一权威）：

    pip install --dry-run --ignore-installed --report <文件> -r requirements.txt

`--dry-run`，什么都不装；`--report`，每个包给一份
`{download_info.url, archive_info.hashes.sha256, metadata.name}`。
那份 sha256 就是 PyPI 自己公布的那个（TLS 上拿的元数据），
而真正下载时 `prepare_runtime` 会再核一遍 —— 哈希是唯一的验收判据。

`--dry-run` 仍然可能执行 sdist 的 `setup.py` 来取元数据（pip 的既知行为）。
这里只解析、不下载，而解析期碰到 sdist 时 pip 会去取它的元数据。
本脚本只在构建机上跑（那是"我们自己的机器"这一侧），
   产物侧的 `prepare_runtime` 一行代码都不执行（判据 3）。

## 两个字段

- `wheels`: `[{name, version, url, sha256}]`（按名字排序，表是确定的）
- `pythonPackages`: `{名字: 版本}`（人读的那一份）

`url` 必须是官方主机（`files.pythonhosted.org`）。这条当场拒：
   一个第三方镜像/代理的地址进了锁，就等于把供应链换成它了。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = _ROOT / "apps" / "desktop" / "primary-runtime-lock.json"
REQUIREMENTS = _ROOT / "requirements.txt"
#: TUI 的依赖也进同一份运行时（2026-11-21）：两件产物共用一套
#:   解释器与 site-packages，独立包不必再维护第二份锁。代价是 GUI 安装包
#:   多几 MB（textual / rich 及其传递依赖）。
TUI_REQUIREMENTS = _ROOT / "tui" / "requirements.txt"

#: 只认这一个主机（判据见文件头）。加白名单 = 一次供应链变更，要单独说。
ALLOWED_HOSTS = ("files.pythonhosted.org",)

#: sdist 的显式声明表：`{PyPI 名字: [要从它的压缩包里取出来的顶层目录]}`
#:
#: 为什么要这张表而不是"通用地解 sdist"：解一个 sdist 通用地等于跑它的构建脚本
#: （`setup.py`）。我们这条路从不执行任何东西（判据 3），那就只能点名：
#: "这一条不是 wheel，而我们知道它的 import 包叫什么"。
#:
#: 当前只有一条（`requirements.txt` 37 个直接依赖里
#: 只有 `jieba` 在 PyPI 上没有 wheel，它只发 `jieba-0.42.1.tar.gz`，
#: 里面是 `jieba-0.42.1/{jieba/,setup.py,PKG-INFO,test/}`）。
#: 表里没登记的 sdist 会让本脚本直接失败 —— 不是跳过、不是"回头再说"：
#: 静默跳过会让打出来的包少一个依赖，而症状是"打包之后某个功能才崩"
#: （静默少一个 = 让人以为装上了）。
SDIST_PACKAGES: dict[str, list[str]] = {
    "jieba": ["jieba"],
}


def _pip_report(reqs: list[Path]) -> list[dict]:
    """跑一次 `pip` 的解析（不装东西），回那份 report 的 `install[]`。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv-pin-"))
    report = tmp / "report.json"
    cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
           "--report", str(report)]
    for req in reqs:
        cmd += ["-r", str(req)]
    print("[pin] %s" % " ".join(cmd[1:]))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-8:]
        raise SystemExit("[pin] pip 解析失败：\n  " + "\n  ".join(tail))
    return json.loads(report.read_text(encoding="utf-8"))["install"]


def _platform_matches(filename: str, target: str) -> bool:
    """wheel 文件名与目标平台是否自洽（纯 Python 轮子两边都算）。"""
    low = filename.lower()
    if "none-any" in low:
        return True
    if target == "win-x64":
        return ("win_amd64" in low) or ("win32" in low)
    if target == "linux-x64":
        return ("manylinux" in low) or ("-linux" in low) or ("linux_x86_64" in low)
    return True


def build_table(items: list[dict], *, target: str = "",
                mirror_host: str = "") -> tuple[list[dict], dict]:
    """`install[]` → `(wheels, pythonPackages)`。不合规的当场拒。

    `mirror_host`：解析时用的镜像主机。它保存的 `/packages/...` 路径与官方
    逐字相同，所以这里只把主机换回 `files.pythonhosted.org`（见文件头）。
    """
    wheels: list[dict] = []
    packages: dict[str, str] = {}
    unknown_sdist: list[str] = []
    for it in items:
        meta = it.get("metadata") or {}
        name = str(meta.get("name") or "").strip()
        version = str(meta.get("version") or "").strip()
        dl = it.get("download_info") or {}
        url = str(dl.get("url") or "")
        sha = str(((dl.get("archive_info") or {}).get("hashes") or {})
                  .get("sha256") or "").strip()
        if not name or not url or not sha:
            raise SystemExit("[pin] 这一条缺名字/地址/哈希：%r" % (it,))
        host = url.split("/")[2] if "://" in url else ""
        if mirror_host and host == mirror_host:
            rest = url.split(host, 1)[1]
            url = "https://files.pythonhosted.org" + rest
            host = "files.pythonhosted.org"
        if host not in ALLOWED_HOSTS:
            raise SystemExit(
                "[pin] **地址不是官方主机**（%s）：%s\n"
                "  -> 一个第三方镜像进了锁，就是把供应链换成它了。"
                "\n     （用镜像解析时加 `--mirror-host <host>`，路径会照搬、主机换回官方。）"
                % (host, url))
        if url.endswith(".whl") and not _platform_matches(url.rsplit("/", 1)[-1], target):
            raise SystemExit(
                "[pin] **wheel 与目标平台对不上**（target=%s）：%s\n"
                "  -> pip 的 `--platform` 不改环境标记，在别的平台上解析就会"
                "混进这一平台的轮子；这份锁必须在目标平台上解析。" % (target, url))
        packages[name] = version
        if url.endswith(".whl"):
            wheels.append({"name": name, "version": version, "url": url, "sha256": sha})
        elif name in SDIST_PACKAGES:
            wheels.append({"name": name, "version": version, "url": url,
                           "sha256": sha, "kind": "sdist",
                           "packages": list(SDIST_PACKAGES[name])})
        else:
            unknown_sdist.append("%s==%s" % (name, version))
    if unknown_sdist:
        raise SystemExit(
            "[pin] **这些依赖没有 wheel**，而 `SDIST_PACKAGES` 里没有登记它们：\n  "
            + "\n  ".join(unknown_sdist)
            + "\n\n-> 要么给它们各写一行 `SDIST_PACKAGES`（**点名**要从压缩包里取哪个目录），"
              "\n   要么把那个依赖从 `requirements.txt` 里去掉。**不许静默跳过** ——"
              "\n   那会让打出来的包少一个依赖，而症状是「打包之后某个功能才崩」。")
    wheels.sort(key=lambda w: (w["name"].lower(), w["version"]))
    return wheels, dict(sorted(packages.items(), key=lambda kv: kv[0].lower()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="pin requirements into the runtime lock")
    ap.add_argument("--lock", default=str(DEFAULT_LOCK))
    ap.add_argument("--requirements", action="append", default=None,
                    help="可重复；默认 requirements.txt + tui/requirements.txt")
    ap.add_argument("--report", default="",
                    help="读一份现成的 pip --report（不在本机重新解析）")
    ap.add_argument("--mirror-host", default="",
                    help="解析用的镜像主机；落表时主机换回官方，路径照搬")
    ap.add_argument("--check", action="store_true",
                    help="只报告差异，不写文件（收尾/CI 用）")
    args = ap.parse_args(argv)

    reqs = ([Path(p) for p in args.requirements] if args.requirements
            else [REQUIREMENTS, TUI_REQUIREMENTS])
    lock_path = Path(args.lock)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    items = (json.loads(Path(args.report).read_text(encoding="utf-8"))["install"]
             if args.report else _pip_report(reqs))
    wheels, packages = build_table(items, target=str(lock.get("target") or ""),
                                   mirror_host=args.mirror_host)

    old = [w.get("sha256") for w in (lock.get("wheels") or [])]
    new = [w["sha256"] for w in wheels]
    added = [w["name"] for w in wheels
             if w["sha256"] not in set(old)]
    gone = [w.get("name") or w["sha256"][:12] for w in (lock.get("wheels") or [])
            if w.get("sha256") not in set(new)]
    print("[pin] %d 条（其中 sdist %d 条）· 新增 %d · 移除 %d"
          % (len(wheels), sum(1 for w in wheels if w.get("kind") == "sdist"),
             len(added), len(gone)))
    if added:
        print("      新增：%s" % ", ".join(added[:12]) + (" …" if len(added) > 12 else ""))
    if gone:
        print("      移除：%s" % ", ".join(str(g) for g in gone[:12]))

    if args.check:
        same = (old == new and (lock.get("pythonPackages") or {}) == packages)
        print("[pin] %s" % ("一致" if same else "**不一致**（跑一次不带 --check 的）"))
        return 0 if same else 1

    lock["wheels"] = wheels
    lock["pythonPackages"] = packages
    lock_path.write_text(json.dumps(lock, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    print("[pin] 写好了：%s" % lock_path)
    print("      ★ 下一步：`scripts/prepare_runtime.py --fetch` 把字节取进缓存，"
          "再 `--assemble` 组装并冒烟。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
