"""`crannia update`：新版本解到新目录、校验、换指针，下次启动生效。

布局：

    <安装根>/
      releases/<version>/     每一版一份完整的代码树（`crannia_tui/` + `core/` …）
      current                 一行文本：当前生效的那个目录名
      version.json            本地安装状态（版本 / 时间 / 上一版）

两条硬规矩：

1. **换指针之前一个字节都不动现役版本。** 解压与校验全在临时目录里做，
   失败就删掉临时目录、`current` 原样留着。
2. **指针只写一次，且是原子写**（同目录临时文件 + replace）——
   写到一半断电时读到的要么是旧的、要么是新的，不会是半行。

网络那一半（读清单、下代码包）在 `core.update_tui`：清单的两条静态地址与
npm 源适配只有一份实现，TUI 不重新写一遍，走子进程让它自己跑。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from .i18n import t

#: 指针文件（一行版本名，相对 `releases/`）。
CURRENT = "current"
#: 本地安装状态。
VERSION_FILE = "version.json"
RELEASES = "releases"

#: 一份能跑的代码树至少要有这几样 —— 拿它判"解出来的东西是不是 release"。
#:
#: 后三样是 `paths.crannia_home()` 的判据（它认 `main.py` + `core` + `web`）：
#: 缺了它们，从 release 目录起来的这一份会找不到自己的根。
_REQUIRED = ("crannia_tui", "main.py", "core", "web")

#: 保留几份（含当前这份）。旧目录定期清，但**绝不删当前**。
KEEP = 2

_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")


class UpdateError(RuntimeError):
    """更新没能完成 —— 消息是给人看的（带下一步）。"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_current(root: Path) -> str:
    """当前生效的版本名；没有 / 读不出 / 名字不合法都给空串。"""
    try:
        name = (Path(root) / CURRENT).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return name if _NAME_RE.match(name) else ""


def read_version(root: Path) -> dict:
    """本地 `version.json`；读不出给空 dict（不是"没有更新"）。"""
    try:
        data = json.loads((Path(root) / VERSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def release_dir(root: Path, name: str) -> Path:
    return Path(root) / RELEASES / name


def _looks_like_release(path: Path) -> bool:
    # `main.py` 是文件，`core` / `web` / `crannia_tui` 是目录 —— 一律用存在性判。
    return all((path / m).exists() for m in _REQUIRED)


def _version_from_payload(payload: Path) -> str:
    """包里的 `version.json` 读版本；没有就空串。"""
    try:
        data = json.loads((payload / VERSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str((data or {}).get("version") or "").strip() if isinstance(data, dict) else ""


def _extract(payload: Path, dest: Path) -> None:
    """把 zip 解到 dest（目录包直接拷贝）。"""
    if payload.is_dir():
        shutil.copytree(payload, dest, dirs_exist_ok=True)
        return
    try:
        with zipfile.ZipFile(payload) as zf:
            zf.extractall(dest)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(t("解包失败：%s") % exc) from exc


def install(payload: Path, *, root: Path, version: str = "",
            sha256: str = "", force: bool = False) -> tuple[str, str]:
    """把 *payload*（zip 或目录）装成新版本，成功后换指针。

    返回 `(新版本名, 给人看的一句话)`；失败抛 `UpdateError`，
    且**现役版本与指针一个字节都不动**。
    """
    root = Path(root)
    payload = Path(payload)
    if not payload.exists():
        raise UpdateError(t("找不到此包：%s") % payload)
    if payload.is_file() and sha256:
        got = _sha256_file(payload)
        if got.lower() != sha256.strip().lower():
            raise UpdateError(t("校验不通过： %s…， %s…")
                              % (got[:12], sha256.strip()[:12]))
    if payload.is_file() and payload.suffix.lower() != ".zip":
        raise UpdateError(t("只认 .zip 或目录（收到 %s）")
                          % (payload.suffix or t("无扩展名")))

    releases = root / RELEASES
    releases.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=str(releases)))
    try:
        _extract(payload, tmp)
        #: 有的包会把内容套一层目录 —— 只有一层且它像 release 时抹平。
        entries = [p for p in tmp.iterdir()]
        if len(entries) == 1 and entries[0].is_dir() and _looks_like_release(entries[0]):
            inner = entries[0]
            for item in inner.iterdir():
                os.replace(item, tmp / item.name)
            inner.rmdir()
        if not _looks_like_release(tmp):
            raise UpdateError(t("此包存在一些不可解决的问题"))
        name = version or _version_from_payload(tmp)
        if not name:
            raise UpdateError(t("此版本版本号未知，使用 --version 指定，"
                                "或在包里放一份 version.json"))
        if not _NAME_RE.match(name):
            raise UpdateError(t("版本名不合法：%r（只许字母数字和 . _ + -）")
                              % name)
        dest = release_dir(root, name)
        if dest.exists() and not force:
            raise UpdateError(t("releases/%s 已存在，使用 --force 尝试覆盖")
                              % name)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(tmp, dest)
    except BaseException:
        #: `Exception` 兜不住 Ctrl+C / `SystemExit`（都是 `BaseException`），
        #: 而一次更新可能在拷几个 GB —— 被打断就留下整包暂存区
        #: （真机 2026-11-21：3 个 `.incoming-*` 共 8.7 GB）。清理只做这一步，
        #: 异常照样原样抛出去。
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    previous = read_current(root)
    _atomic_write(root / CURRENT, name + "\n")
    _atomic_write(root / VERSION_FILE, json.dumps({
        "version": name,
        "release": "%s/%s" % (RELEASES, name),
        "previous": previous,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, ensure_ascii=False, indent=2) + "\n")
    _prune(root, name)
    return name, (t("已安装 %s（上一版：%s）。下次启动生效。")
                  % (name, previous or t("无")))


def _prune(root: Path, keep: str) -> None:
    """只留最近的 KEEP 份（当前那份永远不删）。

    按目录 mtime 排；不认识的名字（`.incoming-*` 之外）不碰 ——
    用户自己放进去的东西不替她删。
    """
    releases = root / RELEASES
    try:
        dirs = [p for p in releases.iterdir()
                if p.is_dir() and not p.name.startswith(".") and _NAME_RE.match(p.name)]
    except OSError:
        return
    # mtime 相同的目录（同一秒装进来的两个版本）再按名字定序 ——
    # 否则次序看 readdir，测试会随机红，而真机上的"保留哪两份"也会抖。
    dirs.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    alive = [p.name for p in dirs[:KEEP]]
    if keep in alive:
        alive = [keep] + [n for n in alive if n != keep]
    else:
        alive = [keep] + alive[:KEEP - 1]
    for p in dirs:
        if p.name not in alive:
            shutil.rmtree(p, ignore_errors=True)


def _run_helper(root: Path, dest: str = ""):
    """跑一次 `core.update_tui`：清单与下载都在那边，这里只要那道 JSON。

    `dest` 为空时只检查、不下载（界面启动时那一次用这个形状）。
    """
    import subprocess

    from .paths import python_exe

    cmd = [str(python_exe()), "-m", "core.update_tui"]
    if dest:
        cmd += ["--dest", dest]
    return subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)


def _parse_helper(r) -> dict:
    """从子进程结果里取那道 JSON（最后一行 `{...}`）。"""
    import json

    for ln in reversed((getattr(r, "stdout", "") or "").strip().splitlines()):
        if ln.strip().startswith("{"):
            try:
                return json.loads(ln.strip())
            except ValueError:
                return {}
    return {}


def should_auto_update(*, home: Path | None = None,
                       root: Path | None = None) -> bool:
    """这一份 TUI 会不会自己装新版本。

    只有独立包会：它的更新根（启动器旁边）与代码根不是同一层。
    开发树里自动装会把版本落进 `<仓库>/releases/`，留一堆没用的目录；
    整合包随桌面端更新（`_updated_with_gui`）。
    """
    from .paths import crannia_home, update_root

    try:
        home = Path(home) if home is not None else crannia_home()
        target = Path(root) if root is not None else update_root()
    except RuntimeError:
        return False
    if _updated_with_gui(home):
        return False
    return target != home


def auto_update(root: Path, *, runner=None, install_root: Path | None = None) -> dict:
    """静默自动更新（界面启动时用）：查到就下，下完就装，下次启动生效。

    * 下载与校验交给 `core.update_tui`（清单只有一份实现）；
    * 装进 `releases/<版本>` 并换指针 —— 与 `crannia update --from` 同一条路；
    * 全程不打印：它跑在界面进程的后台线程里，往 stdout 写字会搅乱全屏。

    *root* 是**代码根**（`-m core.update_tui` 要在那里跑）；
    *install_root* 是**装新版本的那个根**（独立包里是启动器旁边，
    见 `paths.update_root()`）。不给就与 *root* 相同（测试与开发树）。

    返回三种之一：
        `{}`                                  —— 已是最新 / 查不到 / 随桌面端安装
        `{"version": v, "ready": True}`       —— 已装好，重启生效
        `{"version": v, "error": "..."}`      —— 有新版本但没装成
    """
    if _updated_with_gui(root):
        return {}
    target = Path(install_root) if install_root is not None else Path(root)
    import tempfile

    version = ""
    try:
        with tempfile.TemporaryDirectory(prefix="crannia-tui-auto-") as raw:
            r = (runner or _run_helper)(root, raw)
            data = _parse_helper(r)
            version = str(data.get("version") or "")
            if not data.get("ok") or data.get("up_to_date") or not version:
                return {}
            raw_path = str(data.get("path") or "").strip()
            #: 空路径不叫"找不到" —— `Path("")` 是当前目录，拷它等于把整棵
            #:   工作树当成新版装进去（几十秒到几分钟，且装的是错的东西）。
            #:   辅助进程只会给绝对路径，其余一律当下载失败。
            if not raw_path:
                return {"version": version,
                        "error": str(data.get("error") or "download failed")}
            payload = Path(raw_path)
            if not payload.is_absolute():
                return {"version": version, "error": "download failed"}
            if not payload.exists():
                return {"version": version,
                        "error": str(data.get("error") or "download failed")}
            install(payload, root=target, version=version,
                    sha256=str(data.get("sha256") or ""))
            return {"version": version, "ready": True}
    except Exception as exc:  # noqa: BLE001 - 后台更新不许把界面带崩
        return {"version": version, "error": str(exc)} if version else {}


def _network_update(root: Path, *, runner=None,
                    install_root: Path | None = None) -> int:
    """从清单装新版本：检查与下载交给 `core.update_tui`（清单只有一份实现）。"""
    import tempfile

    target = Path(install_root) if install_root is not None else Path(root)
    run = runner or _run_helper
    print(t("正在检查更新…"))
    with tempfile.TemporaryDirectory(prefix="crannia-tui-update-") as raw:
        try:
            r = run(root, raw)
        except Exception as exc:  # noqa: BLE001 - 子进程起不来也要给一句人话
            print(t("检查更新失败：%s") % exc)
            return 1
        data = _parse_helper(r)
        if not data:
            detail = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1]
            print(t("检查更新失败：%s") % (detail.strip() or "?"))
            return 1
        if not data.get("ok"):
            print(t("检查更新失败：%s") % (data.get("error") or "?"))
            return 1
        version = str(data.get("version") or "")
        if data.get("up_to_date"):
            print(t("已是最新版本：%s") % version)
            return 0
        path = Path(str(data.get("path") or ""))
        if not path.is_file():
            print(t("下载失败：%s") % (data.get("error") or "?"))
            return 1
        print(t("发现新版本 %s，正在安装…") % version)
        try:
            message = install(path, root=target, version=version,
                              sha256=str(data.get("sha256") or ""))[1]
        except UpdateError as exc:
            print(t("未更新：%s") % exc)
            return 1
        print(message)
        return 0


def _updated_with_gui(root: Path) -> bool:
    """这一份 TUI 是不是随桌面端安装的（`resources/app` 那一份）。

    判据是同级目录里的 `primary-runtime`：整合包的代码树在
    `<安装>/resources/app`，运行时在 `<安装>/resources/primary-runtime`。
    那一份**不自己更新**（它的代码随 GUI 的代码包层换），否则会装到一个
    启动器根本不读的目录里，而用户以为已经升级了。
    """
    return (root.parent / "primary-runtime").is_dir()


def command(*, root: Path, payload: Path | None = None, version: str = "",
            sha256: str = "", force: bool = False, runner=None,
            install_root: Path | None = None) -> int:
    """`crannia update` 的入口。返回退出码。

    *root* 是代码根（判"随桌面端安装"与跑辅助进程都看它）；
    *install_root* 是读/写 `current` 与 `releases/` 的那个根
    （独立包里两者不是同一层，见 `paths.update_root()`）。

    `runner` 只给测试用：默认走 `core.update_tui` 子进程（见 `_run_helper`）。
    """
    target = Path(install_root) if install_root is not None else Path(root)
    if _updated_with_gui(root):
        print(t("这个 TUI 随桌面端一起更新：请在桌面端里检查更新，或运行安装包。"))
        return 1
    cur = read_current(target)
    state = read_version(target)
    if payload is None:
        print(t("当前版本：%s")
              % (state.get("version") or cur or t("（未记录）")))
        return _network_update(root, runner=runner, install_root=target)
    try:
        message = install(payload, root=target, version=version,
                          sha256=sha256, force=force)[1]
    except UpdateError as exc:
        print(t("未更新：%s") % exc)
        return 1
    print(message)
    return 0
