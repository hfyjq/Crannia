#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布门禁：从干净树开始，一路走到"包里东西对不对"。

    .venv/Scripts/python.exe scripts/release_gate.py              # 跑机械部分
    .venv/Scripts/python.exe scripts/release_gate.py --with-npm-ci
    .venv/Scripts/python.exe scripts/release_gate.py --manual-ok  # 人工那步也做过了

## 为什么要有它

审计原话：不能只看 `npm run package` 返回成功。返回 0 只说明"打包工具没崩"，
不说明装完能用 —— 缺解释器、依赖清单是空的、`runtime.json` 写错平台，
三件都不会让打包命令红。

## 七步（前五步机械，第六步只能人做，第七步是它自己）

| # | 做什么 | 不给过就停 |
|---|---|---|
| 1 | 工作树干净（`git status --porcelain` 为空） | 脏树打出来的包无法复现（`--allow-dirty` 可跳过，但要说出来） |
| 2 | 备自带运行时（`prepare_runtime.py`，含冒烟） | 没有运行时 = 装完打不开；依赖表非空 ≠ 真能 import |
| 3 | 前端构建（`npm run build`） | 产物不在 git 里，不 build 会发个旧前端 |
| 4 | 打安装包（`npm run package`） | —— |
| 5 | 验产物（`make_release.py --check-only`） | 解释器 / 依赖清单 / runtime.json / app 里的四件 |
| 6 | 真装一次 + 真启动一次 —— 人做 | 机械判不了"双击之后能不能用" |
| 7 | 打印这一版的 `sha256` + 下一步（传 manifest） | —— |

## 退出码

* `0` —— 全过，而且 `--manual-ok`（人工那一步也确认过了）
* `3` —— 机械部分全过，但第 6 步没做 -> 这不叫通过，它是"等人验收"
* `1` —— 某一步失败（哪一步、为什么，逐条打印）
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _run(step: str, cmd: list, cwd: Path = _ROOT) -> tuple[bool, str]:
    """跑一步。返回 `(ok, 输出尾巴)`。

    [!] 判据是退出码，不是"有没有报错字样"：npm 的警告长得很像错误，
      而"错误字样"更常出现在正常输出里（一次都不该按文本判）。
    """
    exe = cmd[0]
    if shutil.which(exe) is None and not Path(exe).exists():
        return False, "找不到命令 %r（PATH 上没有）" % exe
    print("\n[gate] %s\n[gate]   $ %s" % (step, " ".join(cmd)))
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), text=True,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    if proc.returncode != 0:
        return False, "退出码 %d" % proc.returncode
    return True, ""


def _dirty_files() -> list:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(_ROOT),
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace")
    except OSError:
        return ["<git 跑不起来>"]
    if out.returncode != 0:
        return ["<git status 失败>"]
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="发布门禁")
    ap.add_argument("--with-npm-ci", action="store_true",
                    help="先跑 npm ci（要联网；换锁之后第一次发版必须跑）")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许脏树（产物不可复现 —— 会打印哪些文件脏）")
    ap.add_argument("--skip-build", action="store_true", help="跳过前端构建")
    ap.add_argument("--skip-package", action="store_true", help="跳过打包（复用已有产物）")
    ap.add_argument("--manual-ok", action="store_true",
                    help="第 6 步（真装 + 真启动）已经由人做过")
    ap.add_argument("--channel", default=None, choices=("stable", "beta"),
                    help="默认按版本号推（预发布后缀 ⇒ beta）。预发布版本显式写 "
                         "stable 会被拒绝 —— 那条路会让 Stable 客户端装上 Beta 包。")
    args = ap.parse_args(argv)

    #: 渠道由版本号推（`core.updater.channel_for_version`，唯一一处）。
    #:   这一步以前固定打印 `--channel stable`：照着它操作，`1.0.0-beta.1`
    #:   会生成一份 `channel=stable` 的清单，而 Stable 客户端只读那份。
    from core.version import __version__
    from core.updater import channel_for_version

    derived = channel_for_version(__version__)
    channel = args.channel or derived
    if args.channel == "stable" and derived != "stable":
        print("[gate] 版本 %s 是预发布版本，不能进 stable 渠道。" % __version__)
        print("[gate] 去掉 --channel（= 按版本走 beta），或者先改版本号。")
        return 1

    py = str(_ROOT / ".venv" / "Scripts" / "python.exe")
    npm = "npm.cmd" if sys.platform == "win32" else "npm"

    # 1. 干净树
    dirty = _dirty_files()
    if dirty:
        print("[gate] 1. 工作树**不干净** —— %d 个改动：" % len(dirty))
        for line in dirty[:20]:
            print("        %s" % line)
        if not args.allow_dirty:
            print("[gate] 脏树打出来的包**无法复现**。确认要继续就加 --allow-dirty。")
            return 1
        print("[gate] （--allow-dirty）继续，但这一版**不要当正式版发**。")
    else:
        print("[gate] 1. 工作树干净 [ok]")

    steps: list = []
    if args.with_npm_ci:
        steps.append(("2a. npm ci（锁文件装依赖）", [npm, "ci"]))
    #: 冒烟必须跑：`--check-only` 只验静态文件，"依赖清单非空"不等于
    #:   随包运行时真能 import（Flask / OpenAI / Office / PDF / NumPy）。
    steps.append(("2. 备自带运行时", [py, "scripts/prepare_runtime.py"]))
    if not args.skip_build:
        steps.append(("3. 前端构建", [npm, "run", "build"]))
    if not args.skip_package:
        steps.append(("4. 打安装包", [npm, "run", "package"]))
    steps.append(("5. 验产物", [py, "scripts/make_release.py", "--check-only"]))

    for name, cmd in steps:
        ok, why = _run(name, cmd)
        if not ok:
            print("[gate] [FAIL] %s 失败：%s" % (name, why))
            return 1
        print("[gate] [ok] %s" % name)

    # 6. 人工那一步
    print("\n[gate] 6. **只能人做的一步**：")
    print("        双击 apps/desktop/release/ 下那个 exe 装一次 → 装完**自己启动一次** →")
    print("        看到主窗口、发一句话能回，才算过。")
    print("        [!] 机械判不了这个；**只看打包命令返回 0 是最常见的自欺**。")
    if not args.manual_ok:
        print("\n[gate] 机械部分全过，但**第 6 步没做** —— 这不叫通过。")
        print("[gate] 验收之后重跑一次并加 --manual-ok，或者把这一版标记成'未验'。")
        return 3

    # 7. 下一步
    print("\n[gate] 7. 下一步（发 manifest，仓库 = hfyjq/Crannia 的 GitHub Pages）：")
    print("        # 安装包挂到 Releases，然后：")
    print("        %s scripts/make_release.py --channel %s ^" % (py, channel))
    print("            --url https://github.com/hfyjq/Crannia/releases/download/<tag>/"
          "crannia-%s-win-x64.exe ^" % __version__)
    print("            --out <gh-pages 检出目录>/%s/version.json" % channel)
    print("        # 推到 gh-pages 分支 -> https://hfyjq.github.io/Crannia/%s/version.json"
          % channel)
    print("        [!] 只动**这一条渠道**的 version.json；另一条不要碰。")
    print("        [!] Pages 有缓存（约 10 分钟）—— 没生效先等，别改代码。")
    print("\n[gate] 全过 [ok]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
