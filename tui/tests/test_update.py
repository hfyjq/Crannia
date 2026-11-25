"""`crannia update`：解到新目录、校验、换指针。

钉六件事：

1. 目录包与 zip 包都装得进去；装完 `current` 与 `version.json` 都指到新的。
2. 校验不通过 / 包里缺文件 / 版本名撞车：一律拒，且**现役指针一个字节不动**。
3. 失败不留临时目录（`releases/.incoming-*` 会被清掉）。
4. `--force` 才覆盖同版本。
5. 只留最近两份，当前那份永远不删。
6. `current` 里读出来的名字要合法（不让 `../x` 这类东西指到外面去）。
7. 不带 `--from` 时走清单：已是最新 / 清单出错各有各的话（清单那一半在
   `core.update_tui`，这里只钉这一侧的解析与退出码）。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import update as U  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def make_tree(base: Path, version: str = "", *, name: str = "code") -> Path:
    d = base / name
    for sub in ("crannia_tui", "core", "web"):
        (d / sub).mkdir(parents=True, exist_ok=True)
        # 空目录不会进 zip —— 放一个占位文件，两种包形状才一样。
        (d / sub / "__init__.py").write_text("", encoding="utf-8")
    (d / "main.py").write_text("# fake\n", encoding="utf-8")
    if version:
        (d / "version.json").write_text(
            json.dumps({"version": version}), encoding="utf-8")
    return d


def make_zip(src: Path, dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w") as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src))
    return dest


def _incoming(root: Path) -> list[str]:
    rel = root / U.RELEASES
    if not rel.is_dir():
        return []
    return [p.name for p in rel.iterdir() if p.name.startswith(".incoming-")]


def test_dir_payload_install() -> None:
    with tempfile.TemporaryDirectory() as td:
        base, root = Path(td) / "src", Path(td) / "root"
        src = make_tree(base, "1.1.0")
        name, message = U.install(src, root=root)
        check(name == "1.1.0", name)
        check(U.read_current(root) == "1.1.0", U.read_current(root))
        state = U.read_version(root)
        check(state.get("version") == "1.1.0", state)
        check((root / U.RELEASES / "1.1.0" / "core").is_dir(), "代码树要在位")
        check("下次启动" in message, message)


def test_zip_payload_and_sha() -> None:
    with tempfile.TemporaryDirectory() as td:
        base, root = Path(td) / "src", Path(td) / "root"
        src = make_tree(base, "1.2.0")
        zpath = make_zip(src, Path(td) / "pkg.zip")
        good = hashlib.sha256(zpath.read_bytes()).hexdigest()
        name, _ = U.install(zpath, root=root, sha256=good)
        check(name == "1.2.0", name)
        check(U.read_current(root) == "1.2.0", U.read_current(root))

        # 第二次：错 sha，指针不动
        before = U.read_current(root)
        state_before = U.read_version(root)
        try:
            U.install(zpath, root=root, version="9.9.9", sha256="0" * 64)
        except U.UpdateError as exc:
            check("校验不通过" in str(exc), str(exc))
        else:
            check(False, "错 sha 居然装上了")
        check(U.read_current(root) == before, "失败后指针必须原样")
        check(U.read_version(root) == state_before, "失败后 version.json 必须原样")


def test_missing_markers_refused() -> None:
    with tempfile.TemporaryDirectory() as td:
        base, root = Path(td) / "src", Path(td) / "root"
        bad = base / "bad"
        (bad / "crannia_tui").mkdir(parents=True)
        try:
            U.install(bad, root=root, version="2.0.0")
        except U.UpdateError as exc:
            check("不可解决" in str(exc), str(exc))
        else:
            check(False, "缺 core/web 的包居然装上了")
        check(U.read_current(root) == "", "失败不该写指针")
        check(_incoming(root) == [], "临时目录要清掉：" + repr(_incoming(root)))


def test_same_version_needs_force() -> None:
    with tempfile.TemporaryDirectory() as td:
        base, root = Path(td) / "src", Path(td) / "root"
        src = make_tree(base, "1.1.0")
        U.install(src, root=root)
        try:
            U.install(src, root=root)
        except U.UpdateError as exc:
            check("--force" in str(exc), str(exc))
        else:
            check(False, "同版本没 --force 也装上了")
        name, _ = U.install(src, root=root, force=True)
        check(name == "1.1.0", name)


def test_previous_is_recorded() -> None:
    with tempfile.TemporaryDirectory() as td:
        base, root = Path(td) / "src", Path(td) / "root"
        U.install(make_tree(base, "1.1.0", name="a"), root=root)
        U.install(make_tree(base, "1.2.0", name="b"), root=root)
        state = U.read_version(root)
        check(state.get("previous") == "1.1.0", state)


def test_retention_keeps_two() -> None:
    with tempfile.TemporaryDirectory() as td:
        base, root = Path(td) / "src", Path(td) / "root"
        for i, v in enumerate(("1.1.0", "1.2.0", "1.3.0", "1.4.0")):
            U.install(make_tree(base, v, name="r%d" % i), root=root)
        dirs = sorted(p.name for p in (root / U.RELEASES).iterdir()
                      if p.is_dir() and not p.name.startswith("."))
        check(dirs == ["1.3.0", "1.4.0"], "只留最近两份：" + repr(dirs))
        check(U.read_current(root) == "1.4.0", "当前那份必须留着")


def test_bad_current_name_is_ignored() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / U.CURRENT).write_text("../escape\n", encoding="utf-8")
        check(U.read_current(root) == "", "不合法名字要当没有")


def test_command_surface() -> None:
    with tempfile.TemporaryDirectory() as td:
        base, root = Path(td) / "src", Path(td) / "root"
        root.mkdir()
        import contextlib
        import io
        from types import SimpleNamespace

        def _run(payload_text):
            return lambda *_args: SimpleNamespace(
                stdout=payload_text, stderr="", returncode=0)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = U.command(root=root, payload=None,
                           runner=_run('{"ok": true, "up_to_date": true, '
                                       '"version": "9.9.9"}'))
        check(rc == 0 and "当前版本" in buf.getvalue(), buf.getvalue())
        check("已是最新版本" in buf.getvalue(), buf.getvalue())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = U.command(root=root, payload=None,
                           runner=_run('{"ok": false, "error": "no manifest"}'))
        check(rc == 1 and "检查更新失败" in buf.getvalue(), buf.getvalue())

        src = make_tree(base, "3.0.0")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = U.command(root=root, payload=src)
        check(rc == 0 and U.read_current(root) == "3.0.0", buf.getvalue())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = U.command(root=root, payload=Path(td) / "nope")
        check(rc == 1 and "找不到" in buf.getvalue(), buf.getvalue())


def test_launcher_honours_the_pointer() -> None:
    """启动器要读 `current` 并从那个 release 目录跑 —— 否则"下次启动生效"是空话。"""
    cmd = (HERE / "crannia.cmd").read_text(encoding="utf-8")
    check('set /p CUR=<"%HERE%current"' in cmd, "启动器没读 current")
    check('set "RUN=%HERE%releases\\%CUR%"' in cmd, "启动器没拼 release 目录")
    check('pushd "%RUN%"' in cmd, "启动器没切到 release 目录")
    #: 独立包里运行中的代码在 `releases/<版本>/`，而指针在启动器旁边 ——
    #: 不把"我在哪"传下去，更新会装进 `releases/<版本>/releases/`，
    #: 启动器永远读不到（"下次启动生效"永远不生效）。
    check('set "CRANNIA_LAUNCH_ROOT=%HERE%"' in cmd,
          "启动器没把更新根传下去")


def test_launcher_sh_honours_the_pointer() -> None:
    """Linux 启动器与 `.cmd` 同一套判据，外加一条换行检查。

    CRLF 是这里唯一"看着没事、跑起来全错"的形态：bash 把行尾的 \\r 当成
    命令的一部分，报错是 "No such file or directory"，而那个路径明明存在。
    在 Windows 上编辑这个文件很容易把它变成 CRLF。
    """
    raw = (HERE / "crannia.sh").read_bytes()
    if sys.platform == "win32" and b"\r\n" in raw:
        #: Windows 检出会把工作区副本换成 CRLF（`core.autocrlf=true`），仓库里的
        #: 字节仍是 LF。产物行尾由打包步骤兜（`make_tui_package._copy_posix_script`
        #: 归一 + `verify()` 拦）—— 这里只对内容做断言，跳过也要出声。
        print("SKIP  CRLF 检查：Windows 检出的工作区副本（core.autocrlf），"
              "产物行尾由 make_tui_package 保证")
    else:
        check(b"\r\n" not in raw, "启动器用了 CRLF，bash 会把行尾的 \\r 当成命令")
    sh = raw.replace(b"\r\n", b"\n").decode("utf-8")
    check('if [ -f "$HERE/current" ]' in sh, "启动器没读 current")
    check('RUN="$HERE/releases/$CUR"' in sh, "启动器没拼 release 目录")
    check('cd -- "$RUN"' in sh, "启动器没切到 release 目录")
    check('export CRANNIA_LAUNCH_ROOT=$HERE' in sh, "启动器没把更新根传下去")
    check('export CRANNIA_LAUNCH_CWD' in sh, "启动器没把调用目录传下去")
    check('export CRANNIA_INSTALLED=1' in sh, "独立包没标安装形态")
    check('exec "$PY" -m crannia_tui' in sh, "启动器没 exec 到入口")


def test_update_root_prefers_the_launcher_root() -> None:
    """更新根 = 启动器所在目录（有 releases/ 或 current 时），否则落回代码根。"""
    import os

    from crannia_tui import paths as P

    old = os.environ.get("CRANNIA_LAUNCH_ROOT")
    try:
        with tempfile.TemporaryDirectory() as td:
            install = Path(td) / "install"
            (install / U.RELEASES).mkdir(parents=True)
            os.environ["CRANNIA_LAUNCH_ROOT"] = str(install)
            check(P.update_root() == install.resolve(), repr(P.update_root()))

            plain = Path(td) / "plain"
            plain.mkdir()
            os.environ["CRANNIA_LAUNCH_ROOT"] = str(plain)
            check(P.update_root() == P.crannia_home(),
                  "没有 releases/current 时应当落回代码根")
    finally:
        if old is None:
            os.environ.pop("CRANNIA_LAUNCH_ROOT", None)
        else:
            os.environ["CRANNIA_LAUNCH_ROOT"] = old


def test_auto_update_installs_under_the_install_root() -> None:
    """代码根与更新根不是同一层时，新版本要落到启动器旁边。"""
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        code = base / "releases" / "1.0.0"
        install = base / "install"
        (code / "core").mkdir(parents=True)
        (install / U.RELEASES / "1.0.0").mkdir(parents=True)
        (install / U.CURRENT).write_text("1.0.0\n", encoding="utf-8")
        src = make_tree(base / "src", "2.0.0")

        def newer(*_args):
            return SimpleNamespace(
                stdout='{"ok": true, "up_to_date": false, "version": "2.0.0", '
                       '"path": %s, "sha256": ""}' % _json_str(str(src)),
                stderr="", returncode=0)

        got = U.auto_update(code, runner=newer, install_root=install)
        check(got.get("ready") is True, repr(got))
        check(U.read_current(install) == "2.0.0",
              "指针应当写在更新根：" + repr(U.read_current(install)))
        check(not (code / U.RELEASES).exists(),
              "不该在代码根下面再长一层 releases")


def test_should_auto_update_only_for_the_standalone_layout() -> None:
    """开发树不自动装新版本：那会把版本落进 `<仓库>/releases/`。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        home = base / "repo"
        home.mkdir()
        install = base / "install"
        (install / U.RELEASES).mkdir(parents=True)
        check(U.should_auto_update(home=home, root=install),
              "更新根与代码根不同 -> 应当自动更新")
        check(not U.should_auto_update(home=home, root=home),
              "同一层 -> 不自动更新")
        resources = base / "resources"
        app = resources / "app"
        app.mkdir(parents=True)
        (resources / "primary-runtime").mkdir()
        check(not U.should_auto_update(home=app, root=install),
              "随桌面端安装的 TUI 不自己更新")


def test_auto_update_downloads_and_installs() -> None:
    """界面启动时的自动更新：查到就下、下完就装；其余一律静默。"""
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "root"
        root.mkdir()

        def up_to_date(*_args):
            return SimpleNamespace(
                stdout='{"ok": true, "up_to_date": true, "version": "9.9.9"}',
                stderr="", returncode=0)

        check(U.auto_update(root, runner=up_to_date) == {}, "已是最新就不动")

        def boom(*_args):
            raise OSError("no network")

        check(U.auto_update(root, runner=boom) == {}, "查不到就不出声")

        #: 真装一次：payload 是一棵合法的 release 树。
        src = make_tree(base / "src", "2.0.0")

        def newer(*_args):
            return SimpleNamespace(
                stdout='{"ok": true, "up_to_date": false, "version": "2.0.0", '
                       '"path": %s, "sha256": ""}' % _json_str(str(src)),
                stderr="", returncode=0)

        got = U.auto_update(root, runner=newer)
        check(got.get("ready") is True and U.read_current(root) == "2.0.0",
              repr(got))

        #: 下载失败：报出来（界面用一句话让人手工重试），但不抛。
        def failed(*_args):
            return SimpleNamespace(
                stdout='{"ok": true, "up_to_date": false, "version": "3.0.0", '
                       '"error": "boom"}',
                stderr="", returncode=0)

        got2 = U.auto_update(root, runner=failed)
        check(got2.get("version") == "3.0.0" and "error" in got2, repr(got2))
        #: ★ 空路径 / 相对路径都不许当包：`Path("")` 是当前目录，拷它会把
        #:   整棵工作树装成一个版本（跑起来几十秒到几分钟，装进去的还是错的）。
        check(U.read_current(root) == "2.0.0" and
              not (root / U.RELEASES / "3.0.0").exists(),
              "下载失败那条什么都没装：" + repr(U.read_current(root)))

        #: 随桌面端安装的那一份：连查都不查（提示会指向一个注定被拒的命令）。
        resources = base / "resources"
        app = resources / "app"
        app.mkdir(parents=True)
        (resources / "primary-runtime").mkdir()
        called: list = []

        def never(*_args):
            called.append(1)
            return newer()

        check(U.auto_update(app, runner=never) == {} and not called,
              "随桌面端安装的那一份不查")


def _json_str(value: str) -> str:
    import json
    return json.dumps(value)


def test_bundled_tui_refuses_to_self_update() -> None:
    """随桌面端安装的那一份不自己更新 —— 否则会装到启动器不读的目录里。"""
    with tempfile.TemporaryDirectory() as td:
        resources = Path(td) / "resources"
        app = resources / "app"
        app.mkdir(parents=True)
        (resources / "primary-runtime").mkdir()
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = U.command(root=app)
        check(rc == 1 and "随桌面端一起更新" in buf.getvalue(), buf.getvalue())

        src = make_tree(Path(td) / "src", "9.9.9")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = U.command(root=app, payload=src)
        check(rc == 1 and "随桌面端一起更新" in buf.getvalue(), buf.getvalue())


def test_interrupt_during_install_leaves_no_incoming() -> None:
    """Ctrl+C（BaseException）也要把 `.incoming-*` 清掉。

    一次更新可能在拷几个 GB；只兜 `Exception` 的话，打断后整包暂存区留在盘上
    （真机 2026-11-21：3 个共 8.7 GB）。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        (root / "releases").mkdir(parents=True)
        payload = Path(td) / "pkg"
        payload.mkdir()
        (payload / "crannia_tui").mkdir()

        orig = U._extract

        def boom(src, dest):
            raise KeyboardInterrupt()

        U._extract = boom
        try:
            try:
                U.install(payload, root=root, version="2.0.0")
            except KeyboardInterrupt:
                pass
            else:
                check(False, "KeyboardInterrupt should pass through")
            left = [p.name for p in (root / "releases").iterdir()]
            check(not any(n.startswith(".incoming-") for n in left),
                  "no .incoming left after interrupt: %r" % left)
        finally:
            U._extract = orig


def main() -> int:
    for fn in [test_dir_payload_install, test_zip_payload_and_sha,
               test_interrupt_during_install_leaves_no_incoming,
               test_missing_markers_refused, test_same_version_needs_force,
               test_previous_is_recorded, test_retention_keeps_two,
               test_bad_current_name_is_ignored, test_command_surface,
               test_launcher_honours_the_pointer,
               test_launcher_sh_honours_the_pointer,
               test_update_root_prefers_the_launcher_root,
               test_auto_update_installs_under_the_install_root,
               test_should_auto_update_only_for_the_standalone_layout,
               test_auto_update_downloads_and_installs,
               test_bundled_tui_refuses_to_self_update]:
        fn()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
