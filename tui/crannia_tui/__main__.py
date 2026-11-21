"""入口：`python -m crannia_tui`。"""

from __future__ import annotations

import argparse
import os
import sys

from . import term
from .app import CranniaApp
from .backend import Backend
from .headless import run_once
from .i18n import t
from .paths import crannia_home, python_exe, update_root

#: 问后端"现在界面用哪一门"。判据只住 `core/i18n.py` 一处：设置里的
#: `interface_language`、"跟随系统"、系统探测、坏值回落全在那边 ——
#: TUI 抄一份就是第二套真相，而两边答得不一样时没人看得出。
#:
#: 先 `paths.prepare()`：`i18n` 读的是相对路径 `data/settings.json`，安装版
#: 靠入口那一下 chdir 才指到用户数据根；子进程不是入口，不 prepare 就读到
#: 安装目录里那份模板（开发树两者重合，看不出来）。
_LANG_CODE = ("from core import paths as _paths; _paths.prepare()\n"
              "from core import i18n; print(i18n.current())")


def _use_saved_language(python, home) -> None:
    """把界面语言定下来；问不到就保持源码语言（原文照常显示）。"""
    try:
        from . import i18n
        from .doctor import query

        i18n.configure(query(python, home, _LANG_CODE).strip(), home=home)
    except Exception:                                    # noqa: BLE001
        pass


def _configure_language() -> None:
    """在构造 argparse 之前定语言 —— help 也是解析参数之前就拼好的。

    问不到就留在源码语言：解析器照常能用，只是那一屏是中文。
    `--version` 跳过（那一路只打一个 ASCII 版本号，不值得多两次子进程）。
    """
    try:
        _use_saved_language(python_exe(), crannia_home())
    except Exception:                                    # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--version" not in raw:
        _configure_language()
    ap = argparse.ArgumentParser(
        prog="crannia-tui",
        description=t("crannia 的终端界面"))
    ap.add_argument("--once", metavar="TEXT",
                    help=t("发送一条消息而不做交互（脚本 / CI 用）"))
    ap.add_argument("--port", type=int, default=None,
                    help=t("固定后端端口。默认让系统分配空闲端口"))
    ap.add_argument("--keys", action="store_true",
                    help=t("按键诊断：只显示收到的按键，不启动后端"))
    # 三个只读命令：都不启动后端，所以要在构造 Backend 之前分流。
    # 用可选位置参数而不是改老参数的形状，--once / --port / --keys 原样不动。
    ap.add_argument("command", nargs="?",
                    choices=("doctor", "config", "profile", "channel", "setup",
                             "update"),
                    help=t("doctor：逐项自检；config：配合 path 打印设置文件路径；"
                           "profile：列出或切换模型配置；channel：通道的配置与启停；"
                           "setup：配置一条模型 API（首次引导）；update：安装一个新版本"))
    ap.add_argument("subcommand", nargs="?",
                    help=t("config 后接 path；profile 后接配置名或 id；"
                           "channel 后接 list / add / config / remove / default / "
                           "start / stop / login"))
    ap.add_argument("rest", nargs="*", help=t("channel 子命令的参数"))
    ap.add_argument("--version", action="store_true",
                    help=t("打印 crannia 版本后退出"))
    # update 的三个参数（其它命令不用它们）。
    ap.add_argument("--from", dest="update_from", metavar="PATH", default="",
                    help=t("update：从本地 zip 或从目录装载"))
    ap.add_argument("--to", metavar="VERSION", default="",
                    help=t("update：当前版本版本名"))
    ap.add_argument("--sha256", metavar="HEX", default="",
                    help=t("update：zip 的 sha256（对不上就拒）"))
    ap.add_argument("--force", action="store_true",
                    help=t("update：覆盖已存在的同版本目录"))
    ap.epilog = t("附加命令：doctor（自检） · config path（打印设置文件绝对路径）"
                  " · profile [名字]（列出或切换模型配置）"
                  " · channel （通道的配置与启停）"
                  " · --version（打印版本）——都不启动界面")
    args = ap.parse_args(raw)

    if args.version:
        from .doctor import app_version

        print(app_version())
        return 0

    if args.command == "doctor":
        from .doctor import doctor

        code, lines = doctor(port=args.port)
        for line in lines:
            print(line)
        return code

    if args.command == "config":
        from .doctor import settings_path

        if args.subcommand != "path":
            ap.error(t("config 后需要 path"))
        try:
            print(settings_path())
        except (OSError, RuntimeError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    if args.command == "profile":
        from .profile import profile_command

        try:
            return profile_command(args.subcommand,
                                   python=python_exe(), home=crannia_home())
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 2

    if args.command == "update":
        from pathlib import Path as _Path

        from .update import command as update_command

        payload = _Path(args.update_from).expanduser() if args.update_from else None
        return update_command(root=crannia_home(), payload=payload,
                              version=args.to, sha256=args.sha256,
                              force=args.force, install_root=update_root())

    if args.command == "setup":
        from .setup import setup_command

        try:
            python, home = python_exe(), crannia_home()
            return setup_command(python=python, home=home)
        except KeyboardInterrupt:
            print("\n" + t("已取消。"))
            return 1
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 2

    if args.command == "channel":
        from .channel import channel_command

        argv = ([args.subcommand] if args.subcommand else []) + list(args.rest)
        try:
            return channel_command(argv, python=python_exe(),
                                   home=crannia_home())
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 2

    if args.keys:
        from .probe import KeyProbeApp

        KeyProbeApp().run()
        return 0

    # 首次引导：还没有默认模型配置时，进界面前先配一条。
    # 只在交互终端里做 —— `--once` 是脚本入口，不能卡在提示上。
    if not args.once and sys.stdin.isatty():
        try:
            from .setup import needs_setup, setup_command

            python, home = python_exe(), crannia_home()
            if needs_setup(python, home):
                print(t("[setup] 未识别的可用配置，进入启动引导"))
                if setup_command(python=python, home=home) != 0:
                    print(t("[setup] 引导未完成"))
        except Exception as exc:                            # noqa: BLE001
            print(t("[setup] 检查失败（%s）—— 继续启动") % exc)

    # 设置文件：首次启动播种、坏文件先备份再重建、坏值出声。
    # 失败只打一行 —— 一个读不出来的设置文件不该挡住用户用程序。
    try:
        from . import settings as _settings
        from .doctor import settings_path

        for _line in _settings.ensure(python_exe(), crannia_home()):
            print(_line)
        _values, _problems = _settings.read(
            settings_path(home=crannia_home(), python=python_exe()),
            _settings.load_baseline(crannia_home()))
        for _line in _problems:
            print(t("[settings] %s") % _line)
    except Exception as exc:                                # noqa: BLE001
        print(t("[settings] 检查失败：%s") % exc)

    try:
        backend = Backend(crannia_home(), python_exe(), port=args.port)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.once:
        return run_once(backend, args.once)

    # 全屏布局靠光标定位和整屏重绘，跳不出 ANSI。cmd 的旧控制台不解释转义，
    # 在那里进去只会看到一片乱码 —— 提前说清楚，而不是让人对着花屏猜。
    if sys.stdout.isatty() and not term.ansi_passthrough_ok():
        term.warn_no_ansi()

    # 呼出的目录 = 这一次会话的工作目录。启动器在 pushd 之前把它放进
    # `CRANNIA_LAUNCH_CWD`；直接 `python -m crannia_tui` 时就是进程 cwd。
    # 不能用后端报的 `project_root`：那是代码根，不是用户站在哪。
    launch_cwd = (os.environ.get("CRANNIA_LAUNCH_CWD") or "").strip() or os.getcwd()

    CranniaApp(backend, launch_cwd=launch_cwd, check_updates=True).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
