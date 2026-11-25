#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TUI 独立包：自带运行时 + 代码树 + 启动器，产出两件 zip。

    .venv/Scripts/python.exe scripts/make_tui_package.py
    .venv/Scripts/python.exe scripts/make_tui_package.py --skip-runtime
    .venv/Scripts/python.exe scripts/make_tui_package.py --check-only

## 两件产物

| 文件 | 给谁 | 里面有什么 |
|---|---|---|
| `crannia-tui-<版本>-win-x64.zip` | 只装 TUI 的人 | `runtime/` + `releases/<版本>/` + `current` + `version.json` + `crannia.cmd` |
| `crannia-tui-<版本>-code.zip` | `crannia update --from` | 只有代码树（根下有 `crannia_tui` / `main.py` / `core` / `web`） |

第一件是安装形态，第二件是更新形态（代码层）。`artifacts.tui`
指向第二件：更新只换代码，不重下几百兆运行时。

## 布局

```
crannia-tui-<版本>-win-x64/
  runtime/                 <- apps/desktop/runtime/primary-runtime 的拷贝
  releases/<版本>/         <- 代码树（tui/crannia_tui 改名为 crannia_tui）
  current                  <- 一行：<版本>
  version.json             <- {"version", "previous", "installed_at"}
  crannia.cmd              <- tui/crannia.cmd
```

`crannia.cmd` 自己认三种布局（见文件头）；这一种与 `crannia update --from`
产出的形状逐字相同，所以启动器与更新器不会各说各的。

## 代码树从哪来

名单是 `apps/desktop/package-tree.json`（GUI 的 `resources/app` 读同一份）。
从当前工作树复制，不从 HEAD 导 —— 发版时由 `release_gate.py --from-clone`
先把整棵树导成干净的，再在这里跑，两边不各造一份。

## 判据

1. 运行时必须有 `python/python.exe` 与 `runtime.json`。
2. 代码树必须有 `crannia_tui` / `main.py` / `core` / `web`（与
   `crannia_tui.update._REQUIRED` 同一条）以及 `locales/en.json`、
   `data/settings.example.json`（缺后者时首启播种读不到出厂基准）。
3. 冒烟：用包里那个解释器 `import flask, textual, rich` 全过。
4. 排除规则只认 `package-tree.json::exclude` 里那三条；多一条当场拒
   （否则新增的排除只对 Electron 生效，TUI 包里会多带东西）。

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
import time
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_TREE_FILE = _ROOT / "apps" / "desktop" / "package-tree.json"
_RUNTIME_SRC = _ROOT / "apps" / "desktop" / "runtime" / "primary-runtime"
_OUT_DIR = _ROOT / "dist-release"

#: 每个 target 的启动器 / 安装器 / 运行时布局 / 归档格式（一处定义）。
#:
#: 判 target，不判构建机的 `os.name`：在 Windows 上给 Linux 组包时
#: `os.name` 会说谎，而包是给谁的由 target 决定。
_TARGETS: dict[str, dict] = {
    "win-x64": {
        "launcher": _ROOT / "tui" / "crannia.cmd",
        "installer": _ROOT / "tui" / "standalone-install.cmd",
        "uninstaller": _ROOT / "tui" / "standalone-uninstall.cmd",
        "launcher_name": "crannia.cmd",
        "installer_name": "install.cmd",
        "uninstaller_name": "uninstall.cmd",
        "python": ("python.exe",),
        "archive": "zip",
    },
    "linux-x64": {
        "launcher": _ROOT / "tui" / "crannia.sh",
        "installer": _ROOT / "tui" / "standalone-install.sh",
        "uninstaller": _ROOT / "tui" / "standalone-uninstall.sh",
        "launcher_name": "crannia.sh",
        "installer_name": "install.sh",
        "uninstaller_name": "uninstall.sh",
        "python": ("bin", "python3"),
        "archive": "tar.gz",
    },
}

#: 兼容旧引用（Windows 那套文件）；新代码用 `_TARGETS[target]`。
_LAUNCHER = _TARGETS["win-x64"]["launcher"]
_INSTALLER = _TARGETS["win-x64"]["installer"]
_UNINSTALLER = _TARGETS["win-x64"]["uninstaller"]


def _host_can_run(target: str) -> bool:
    """构建机跑不跑得了 target 的解释器（冒烟的前提）。"""
    if target == "win-x64":
        return sys.platform == "win32"
    return sys.platform.startswith("linux")


def _host_target() -> str:
    """本机该组哪个 target。macOS 还没有运行时锁 —— 明说，不猜。"""
    if sys.platform == "win32":
        return "win-x64"
    if sys.platform.startswith("linux"):
        return "linux-x64"
    raise SystemExit(
        "[tui] 这个平台（%s）还没有随包运行时；今天只支持 win-x64 与 linux-x64。"
        % sys.platform)

#: 代码树在 release 目录里必须有的几样（与 `crannia_tui.update._REQUIRED` 同一条）。
_REQUIRED = ("crannia_tui", "main.py", "core", "web")
#: 另外两样运行期资产，缺了症状要等到首启才出现。
_ASSETS = ("locales/en.json", "data/settings.example.json")

#: `package-tree.json::exclude` 里认识的规则。加一条要同时改
#: `_should_skip`，而且不许只对 Electron 生效（见文件头判据 4）。
_KNOWN_EXCLUDES = frozenset({"**/__pycache__/**", "**/*.pyc", "skills/probe_good/**"})


def _read_version() -> str:
    from core.version import __version__
    return __version__


def _load_tree() -> dict:
    tree = json.loads(_TREE_FILE.read_text(encoding="utf-8"))
    unknown = set(tree.get("exclude") or []) - _KNOWN_EXCLUDES
    if unknown:
        raise SystemExit(
            "[tui] package-tree.json 里有本脚本不认识的排除规则：%s\n"
            "      -> 要么把它加进 _should_skip，要么删掉；"
            "只对 Electron 生效的排除会让 TUI 包多带文件。"
            % ", ".join(sorted(unknown)))
    return tree


def _should_skip(rel: str) -> bool:
    """与 `package-tree.json::exclude` 逐条对应的三条。"""
    posix = rel.replace("\\", "/")
    if "/__pycache__/" in "/" + posix + "/" or posix.startswith("__pycache__/"):
        return True
    if posix.endswith(".pyc"):
        return True
    if posix == "skills/probe_good" or posix.startswith("skills/probe_good/"):
        return True
    return False


def _copy_tree(src: Path, dst: Path) -> int:
    """复制一个目录，跳过 `_should_skip` 认得的那些。回文件数。"""
    n = 0
    for base, dirs, files in os.walk(src):
        rel_base = os.path.relpath(base, src)
        rel_base = "" if rel_base == "." else rel_base
        dirs[:] = [d for d in dirs
                   if not _should_skip(os.path.join(rel_base, d))]
        for name in files:
            rel = os.path.join(rel_base, name)
            if _should_skip(rel):
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(base) / name, target)
            n += 1
    return n


def _stage_code(dest: Path, tree: dict) -> int:
    """把代码树铺到 `dest`（`tui/crannia_tui` 改名为 `crannia_tui`）。"""
    n = 0
    for rel in tree.get("dirs") or []:
        src = _ROOT / rel
        if not src.is_dir():
            raise SystemExit("[tui] package-tree.json 里的目录不存在：%s" % src)
        target = dest / ("crannia_tui" if rel == "tui/crannia_tui" else rel)
        target.mkdir(parents=True, exist_ok=True)
        n += _copy_tree(src, target)
    for rel in tree.get("files") or []:
        src = _ROOT / rel
        if not src.is_file():
            raise SystemExit("[tui] package-tree.json 里的文件不存在：%s" % src)
        if rel in ("tui/crannia.cmd", "tui/crannia.sh"):
            continue  # 启动器放在包根，不重复进代码树
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        n += 1
    return n


def _copy_posix_script(src: Path, dest: Path) -> None:
    """把 POSIX 脚本铺进包，顺手把 CRLF 归一成 LF。

    Windows 检出（`core.autocrlf=true`）会把工作区副本换成 CRLF；原样拷进
    tar.gz 后 bash 把行尾的 `\\r` 当成命令的一部分，报错却是
    「No such file or directory」，而那个文件明明在。仓库里的字节是 LF，
    这里只是把它还原回去；真改了就说一声，不静默。
    """
    data = src.read_bytes()
    fixed = data.replace(b"\r\n", b"\n")
    if fixed != data:
        print("[tui] 行尾归一（CRLF -> LF）：%s" % src.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(fixed)


def _zip_dir(src: Path, out_zip: Path) -> None:
    """把 `src` 的内容打成 zip（根下就是内容，不多一层）。"""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in src.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src).as_posix())


def _tar_gz_dir(src: Path, out_tar: Path) -> None:
    """把 `src` 的内容打成 tar.gz（根下就是内容，不多一层）。

    Linux 用 tar.gz 而不是 zip：zip 不保留可执行位，解出来 `crannia.sh`
    与 `python/bin/python3` 都是 644，安装脚本第一步就报「不是完整包」。
    """
    out_tar.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_tar, "w:gz") as tf:
        for path in sorted(src.rglob("*")):
            if path.is_file() or path.is_symlink():
                tf.add(path, arcname=path.relative_to(src).as_posix(),
                       recursive=False)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(root: Path, *, smoke: bool = True, target: str = "win-x64") -> list:
    """验一棵设备好的独立包（或解开的归档）。回问题清单（空 = 过）。"""
    bad: list = []
    spec = _TARGETS[target]
    runtime = root / "runtime"
    py = runtime / "python" / Path(*spec["python"])
    if not py.is_file():
        bad.append("运行时缺解释器：%s" % py)
    if not (runtime / "runtime.json").is_file():
        bad.append("运行时缺 runtime.json")
    current = (root / "current").read_text(encoding="utf-8").strip() \
        if (root / "current").is_file() else ""
    if not current:
        bad.append("缺 current 指针")
    code = root / "releases" / current if current else None
    if code is None or not code.is_dir():
        bad.append("current 指向的 release 目录不存在：%r" % current)
        code = None
    if code is not None:
        for name in _REQUIRED:
            if not (code / name).exists():
                bad.append("代码树缺 %s（%s）" % (name, code))
        for name in _ASSETS:
            if not (code / name).is_file():
                bad.append("代码树缺 %s" % name)
    if not (root / "version.json").is_file():
        bad.append("缺 version.json")
    if not (root / spec["launcher_name"]).is_file():
        bad.append("缺 %s" % spec["launcher_name"])
    for key in ("installer_name", "uninstaller_name"):
        name = spec[key]
        if not (root / name).is_file():
            bad.append("缺 %s（独立包靠它装入口）" % name)
    if target != "win-x64":
        #: POSIX 脚本带 CRLF 时 bash 会把 `\r` 当成命令的一部分，症状是
        #: 「No such file or directory」而后端明明在包里 —— 装完才暴露，这里先拦。
        for key in ("launcher_name", "installer_name", "uninstaller_name"):
            p = root / spec[key]
            if p.is_file() and b"\r\n" in p.read_bytes():
                bad.append("%s 用了 CRLF（bash 会把 \\r 当成命令）" % spec[key])
    if smoke and py.is_file() and not _host_can_run(target):
        #: 交叉组包（在 Windows 上给 Linux 组，或反过来）跑不了冒烟 ——
        #: 明说跳过，别让「没跑」长得像「通过」。
        print("[tui] [!] 冒烟跳过：构建机（%s）执行不了 target=%s 的解释器"
              % (sys.platform, target))
    elif smoke and py.is_file():
        r = subprocess.run([str(py), "-c", "import flask, textual, rich"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        if r.returncode != 0:
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
            bad.append("冒烟失败（包里那个解释器 import flask / textual / rich）：%s"
                       % " | ".join(tail))
    return bad


def build(out_dir: Path, *, skip_runtime: bool, target: str | None = None) -> int:
    target = target or _host_target()
    spec = _TARGETS[target]
    version = _read_version()
    tree = _load_tree()
    prefix = "crannia-tui-%s-%s" % (version, target)
    stage = out_dir / prefix

    if not _RUNTIME_SRC.is_dir():
        raise SystemExit(
            "[tui] 找不到自带运行时：%s\n"
            "      -> 先跑 scripts/prepare_runtime.py" % _RUNTIME_SRC)

    stage.mkdir(parents=True, exist_ok=True)
    releases = stage / "releases"
    code = releases / version
    if code.exists():
        shutil.rmtree(code)
    code.mkdir(parents=True)
    n = _stage_code(code, tree)
    print("[tui] 代码树：%d 个文件 -> %s" % (n, code))

    runtime = stage / "runtime"
    if skip_runtime and runtime.is_dir():
        print("[tui] 运行时：复用已有拷贝（--skip-runtime）")
    else:
        if runtime.exists():
            shutil.rmtree(runtime)
        print("[tui] 运行时：%s -> %s（这一步最慢）" % (_RUNTIME_SRC, runtime))
        shutil.copytree(_RUNTIME_SRC, runtime)

    (stage / "current").write_text(version + "\n", encoding="utf-8")
    (stage / "version.json").write_text(
        json.dumps({"version": version, "previous": "",
                    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime())},
                   indent=2) + "\n", encoding="utf-8")
    for key, name_key in (("launcher", "launcher_name"),
                          ("installer", "installer_name"),
                          ("uninstaller", "uninstaller_name")):
        dest = stage / spec[name_key]
        if target == "win-x64":
            shutil.copy2(spec[key], dest)
        else:
            _copy_posix_script(spec[key], dest)
            os.chmod(dest, 0o755)

    bad = verify(stage, target=target)
    if bad:
        print("[tui] 设备好的包没通过自查：")
        for line in bad:
            print("      - " + line)
        return 1

    archive = spec["archive"]
    full = out_dir / (prefix + ("." + archive if archive != "tar.gz" else ".tar.gz"))
    code_zip = out_dir / ("crannia-tui-%s-code.zip" % version)
    print("[tui] 打全文包：%s" % full)
    if archive == "tar.gz":
        _tar_gz_dir(stage, full)
    else:
        _zip_dir(stage, full)
    print("[tui] 打代码包：%s" % code_zip)
    _zip_dir(code, code_zip)

    for path in (full, code_zip):
        print("[tui] %s  %s  %d B" % (path.name, _sha256(path)[:16], path.stat().st_size))
    print("[tui] 产物在 %s" % out_dir)
    return 0


def check(path: Path, *, target: str | None = None) -> int:
    """`--check-only <路径>`：目录直接验；zip / tar.gz 先解到临时目录再验。"""
    target = target or _host_target()
    if path.is_dir():
        bad = verify(path, target=target)
    elif path.is_file() and path.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="tui-check-") as raw:
            with zipfile.ZipFile(path) as zf:
                zf.extractall(raw)
            bad = verify(Path(raw), target=target)
    elif path.is_file() and path.name.endswith((".tar.gz", ".tgz")):
        with tempfile.TemporaryDirectory(prefix="tui-check-") as raw:
            with tarfile.open(path) as tf:
                tf.extractall(raw)
            bad = verify(Path(raw), target=target)
    else:
        print("[tui] 不认识的目标：%s" % path)
        return 1
    if bad:
        print("[tui] 没通过：")
        for line in bad:
            print("      - " + line)
        return 1
    print("[tui] 通过：%s" % path)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TUI 独立包")
    ap.add_argument("--out", default=str(_OUT_DIR), help="产物目录（默认 dist-release）")
    ap.add_argument("--skip-runtime", action="store_true",
                    help="复用已拷好的 runtime/（开发迭代用；发版不要加）")
    ap.add_argument("--target", choices=sorted(_TARGETS), default=None,
                    help="组哪个平台的包（默认按本机；macOS 还没有运行时锁）")
    ap.add_argument("--check-only", nargs="?", const="", default=None,
                    metavar="PATH",
                    help="只验：给目录 / zip / tar.gz；不给路径则验默认产物")
    args = ap.parse_args(argv)

    if args.check_only is not None:
        target = args.target or _host_target()
        if args.check_only:
            return check(Path(args.check_only), target=target)
        version = _read_version()
        return check(Path(args.out) / ("crannia-tui-%s-%s" % (version, target)),
                     target=target)

    return build(Path(args.out), skip_runtime=args.skip_runtime,
                 target=args.target)


if __name__ == "__main__":
    sys.exit(main())
