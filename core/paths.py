# -*- coding: utf-8 -*-
"""运行期数据根与只读资源根的唯一口径。

安装版的代码在 <安装目录>/resources/app，而安装、升级、卸载会把那个目录整棵删掉
（NSIS 的 RMDir /r $INSTDIR），所以聊天记录、记忆、工作区、配置必须放在用户目录。
开发树不受影响：数据仍在 <项目>/data，相对路径照旧。

三个根：
  app_root()     代码在哪。只读资产（skills/、web/、vendor/），安装版在
                 <安装>/resources/app，开发树是项目根。
  data_root()    数据放哪。安装版是 %APPDATA%\\Crannia（mac、linux 各有约定），
                 开发树是项目根。
  config_path()  用户可改的 config.yaml。

prepare() 由入口调用一次：建数据根；首次运行把安装目录里的 data/、logs/、
workspace/、config.yaml 复制过来（目标已存在就跳过，永不覆盖）；最后 chdir 到
数据根。仓里绝大多数运行期路径是相对路径，改 cwd 一处等于改全部。

只读资产不跟着 cwd 走，skills/ 那几处改用 asset_dir()。prepare() 只在入口调用，
不在 import 时执行：测试夹具靠 chdir 换目录，import 期 chdir 会把它们全打乱。

环境变量（诊断与测试）：CRANNIA_DATA_DIR 改数据根（优先于安装判定），
CRANNIA_APP_ROOT 改代码根（模拟安装树），CRANNIA_INSTALLED 用 1/0 强制安装判定。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

__all__ = ["app_root", "is_installed", "data_root", "config_path",
           "user_skills_dir", "default_workspace_dir",
           "asset_dir", "prepare", "migration_report"]

# 首次运行要搬过来的东西（相对安装目录）。只读资产不在其中，它们本来就该跟着
# 安装包更新。
_MIGRATE = ("data", "logs", "workspace", "config.yaml")

_MARKER = ".migration.json"

#: 数据根的格式版本。
#:
#: 什么时候 +1：只有形状变了才加 —— 旧程序读新数据会把它写坏的那种。
#:   · `settings.json` 某个键改名 / 含义变（纯加键不算：`api_save_settings`
#:     是整份读回来再整份写回，不认识的键原样留着）
#:   · `short_term.db` 增删列 / 改列含义 / 改 `kind` 的语义
#:   · facts 的 frontmatter、`model_configs.json` 的条目形状、`.tombstones.json`
#:     （这三份都会被"读成已知字段 → 整份重写"，未知字段会被吃掉）
#: 不是安装包版本号：装个新皮、改个文案都不该动它。
DATA_SCHEMA = 1

#: 数据根里记这个版本的文件（点开头 = 我们的账，不是给模型看的）。
SCHEMA_FILE = ".data-schema.json"


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def app_root() -> Path:
    """代码根。`CRANNIA_APP_ROOT` 可覆盖（测试模拟安装树）。"""
    override = _env("CRANNIA_APP_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def is_installed() -> bool:
    """是不是安装版。判据：代码根在 `<…>/resources/app` 下，或环境变量强制。"""
    forced = _env("CRANNIA_INSTALLED")
    if forced in ("0", "1"):
        return forced == "1"
    return app_root().parent.name.lower() == "resources"


def data_root() -> Path:
    """数据根。`CRANNIA_DATA_DIR` > 安装版约定 > 开发树（= 代码根）。"""
    override = _env("CRANNIA_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if not is_installed():
        return app_root()
    if sys.platform == "win32":
        base = _env("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Crannia"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Crannia"
    base = _env("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "crannia"


def config_path() -> Path:
    """用户可改的 `config.yaml`。安装版读数据根那份（迁移过来的）。"""
    return data_root() / "config.yaml"


def load_env_files() -> list:
    """把 `.env` 读进环境变量。返回真读过的那几份（给启动日志用）。

    两份，顺序承重：先 代码根那份（随包/开发树），再 数据根那份 ——
      后者 `override=True` 压过前者。
    为什么数据根那份必须存在：安装目录升级时整棵被换掉
      用户把 `DEEPSEEK_API_KEY` 填在安装目录里，
      升级一次就没了，而症状是"更新之后模型连不上"。
      数据根（`%APPDATA%\\Crannia`）升级不动，才放得住。
    两份同一路径时只读一次（开发树里 `data_root() == app_root()`）。
    读不到 / 装了坏文件都不抛 —— 这条在导入期跑，抛一次整个后端起不来。
    """
    out: list = []
    seen: set = set()
    paths = (app_root() / ".env", data_root() / ".env")
    try:
        from dotenv import load_dotenv
    except Exception:                                        # noqa: BLE001
        return out
    for p in paths:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        try:
            if p.is_file():
                load_dotenv(p, override=True)
                out.append(str(p))
        except Exception:                                    # noqa: BLE001
            pass
    return out


def user_skills_dir() -> Path:
    """用户自己装的技能放在数据根下的 skills/。

    与 asset_dir("skills") 方向相反：那个是随包发布的只读资产（安装版在安装目录，
    升级会整棵删），这个是用户装的，升级不动它。装载时两边叠加扫描，仓内的在前，
    同名的自带技能优先。

    开发树里保持相对路径，与 asset_dir 同一条判据：测试夹具靠 chdir 换目录，
    这里返回绝对路径的话，测试会把技能写进真实仓库（app_root() 由 __file__ 推出，
    不跟 cwd 走）。安装版里两个根才真正分开。
    """
    override = _env("CRANNIA_DATA_DIR")
    if override:
        return Path(override).expanduser() / "skills"
    if not is_installed():
        return Path("skills")
    return data_root() / "skills"


def default_workspace_dir() -> Path:
    r"""默认工作区的落点（onboarding 预填、`primary_path` 的最后回退）。

    开发树：`workspace`（相对路径 —— 测试夹具靠 chdir，一个字节不改）。
    安装版：安装盘上的兄弟目录 `<盘符>\Crannia\workspace`；
      那块盘写不了 / 不是本地盘符时，退回 `<用户>\Documents\Crannia\workspace`。

    为什么是兄弟目录、不是安装目录里面：
      升级/卸载对 `$INSTDIR` 是 `RMDir /r`，放里面等于"用户的工作区产物
      随程序一起没" —— 与把数据根挪出安装目录同一条判据。
    为什么跟着盘走：用户挑安装位置时挑的就是盘（C: 满了才装到 D:），
      产物不该反手又落回 C:。
    为什么也不是数据根（`%APPDATA%\Crannia`）：那是隐藏目录，
      用户在资源管理器里翻不到自己交出去的产物。

    这里只判断、不创建：`os.access` 不落盘，真正的建目录发生在
      第一次写入 / 第一次 `run_command`（那时才知道用户真的要在这儿干活）。
    """
    if not is_installed():
        return Path("workspace")
    return _workspace_for_root(Path(str(app_root())))


def _workspace_for_root(root: Path, *, writable=None) -> Path:
    """按安装位置决定默认工作区（:func:`default_workspace_dir` 的那一半）。

    默认安装（`%LOCALAPPDATA%\\Programs\\…`）落在「文档」—— 那是 Windows 上
      用户找自己文件的地方，而且盘没得选（都在 C:）。
    用户自己挑过安装位置（`D:\\cai\\Crannia` 这种）就跟着那块盘走 ——
      他挑盘时挑的就是空间。
    那块盘写不了（只读 / 不是本地盘符）或者不是 `X:` 形状（UNC 路径），
      退回「文档」。

    *writable* 只是给测试注入判据用的；不传就 `os.access`（不落盘）。
    """
    docs = Path.home() / "Documents" / "Crannia" / "workspace"
    try:
        local = str(os.environ.get("LOCALAPPDATA") or "")
        if local and str(root).lower().startswith(local.lower()):
            return docs
        drive = root.drive
    except Exception:                                          # noqa: BLE001
        return docs
    if not (drive and re.fullmatch(r"[A-Za-z]:", drive)):
        return docs
    probe = writable or (lambda p: os.access(p, os.W_OK))
    try:
        ok = bool(probe(drive + os.sep))
    except OSError:
        ok = False
    return (Path(drive + os.sep) / "Crannia" / "workspace") if ok else docs


def asset_dir(name: str) -> Path:
    """只读资产目录。开发树保持相对路径，测试夹具靠 chdir 换目录。"""
    return (app_root() / name) if is_installed() else Path(name)


def _app_version() -> str:
    try:
        from core.version import __version__
        return str(__version__)
    except Exception:                                        # noqa: BLE001
        return ""


def data_schema_status() -> dict:
    """读数据格式账本，只读：不备份、不建文件。给界面与启动日志用。

    返回的 found 是数据那边的格式号；compat 为真表示数据比本程序新，当前以兼容
    模式运行。读不到不等于 0：文件不在时 found 给 None（从没记过），不是“版本 0”。
    """
    try:
        raw = json.loads((data_root() / SCHEMA_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ours": DATA_SCHEMA, "found": None, "compat": False,
                "backup": "", "complete": True}
    st = raw if isinstance(raw, dict) else {}
    try:
        found = int(st.get("schema") or 0) or None
    except (TypeError, ValueError):
        found = None
    compat = st.get("compat") if isinstance(st.get("compat"), dict) else {}
    return {
        "ours": DATA_SCHEMA,
        "found": found,
        "compat": bool(compat.get("found")),
        "backup": str(compat.get("backup") or ""),
        "complete": bool(compat.get("complete", True)),
        "at": str(compat.get("at") or ""),
    }


def _copy_tree(src: Path, dst: Path) -> dict:
    """逐文件拷贝，单个失败不拖垮整次。返回 {copied, failed: [(路径, 为什么)]}。

    不用 shutil.copytree：它在第一个失败就抛，已拷一半的目录既不算成功也不算失败，
    而这条路上“备份是否完整”是要说给用户听的话。
    """
    copied = 0
    failed: list = []
    for dirpath, _dirs, files in os.walk(src):
        base = Path(dirpath)
        try:
            rel = base.relative_to(src)
        except ValueError:
            continue
        target_dir = dst / rel
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            failed.append((str(rel), str(exc)))
            continue
        for name in files:
            try:
                shutil.copy2(base / name, target_dir / name)
                copied += 1
            except OSError as exc:
                failed.append((str(rel / name), str(exc)))
    return {"copied": copied, "failed": failed}


def _write_schema_ledger(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _check_data_schema(root: Path) -> dict:
    """读到比本程序新的数据格式，备份 + 兼容继续。

    判据是「备份 + 继续 + 大声说」。

    * `found <= DATA_SCHEMA`，只把账推到当前版本（老数据由既有迁移处理）；
    * `found >  DATA_SCHEMA`，把整个数据根备份到同级目录，然后继续跑；
    * 备份不完整，账里 `complete=false` —— 不谎称已备份，下次启动重试。

    数据本身一个字节都不碰：兼容模式下我们只写这份账。
    已经完整备份过同一个 `found`，不再重复拷（一次冲突只留一份备份）。
    任何异常都不抛 —— 它在入口路径上，抛一次整个后端起不来。
    """
    ours = DATA_SCHEMA
    ledger = root / SCHEMA_FILE
    state: dict = {}
    try:
        raw = json.loads(ledger.read_text(encoding="utf-8"))
        state = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        state = {}
    try:
        found = int(state.get("schema") or 0)
    except (TypeError, ValueError):
        found = 0

    info: dict = {"ours": ours, "found": found or None, "compat": False,
                  "backup": "", "complete": True, "failed": []}
    if not found or found <= ours:
        try:
            _write_schema_ledger(ledger, {
                "schema": ours,
                "app_version": _app_version(),
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except OSError as exc:
            info["error"] = "账本写不进去：%s" % exc
        return info

    # 数据比程序新：进兼容模式，先备份一次。
    info["compat"] = True
    prev = state.get("compat") if isinstance(state.get("compat"), dict) else {}
    prev_backup = str(prev.get("backup") or "")
    if (prev.get("found") == found and prev.get("complete")
            and prev_backup and Path(prev_backup).is_dir()):
        info.update(backup=prev_backup, complete=True, reused=True)
        return info

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = root.parent / ("%s-backup-%s" % (root.name or "Crannia", stamp))
    try:
        backup.mkdir(parents=True, exist_ok=False)
        res = _copy_tree(root, backup)
    except OSError as exc:
        info["complete"] = False
        info["failed"] = [["<备份目录>", str(exc)]]
    else:
        info["backup"] = str(backup)
        info["failed"] = [list(x) for x in res["failed"]]
        info["complete"] = not res["failed"]

    try:
        _write_schema_ledger(ledger, {
            # 账本不降回 ours：数据确实还是新的，下次启动仍要判成兼容。
            "schema": max(found, ours),
            "app_version": _app_version(),
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "compat": {"found": found, "backup": info["backup"],
                       "complete": info["complete"],
                       "failed": len(info["failed"]),
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        })
    except OSError as exc:
        info["error"] = "账本写不进去：%s" % exc
    return info


_TMP_PREFIX = ".migrating-"


def _remove_path(p: Path) -> None:
    try:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    except OSError:
        pass


def _copy_atomically(src: Path, dst: Path, tmp: Path) -> None:
    """先复制到 tmp，再原子改名成 dst —— 失败不留半截目标。"""
    _remove_path(tmp)
    if src.is_dir():
        shutil.copytree(src, tmp)
    else:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _migrate(root: Path) -> dict:
    src_root = app_root()
    out = {"migrated": [], "skipped": [], "errors": []}
    for name in _MIGRATE:
        src = src_root / name
        dst = root / name
        tmp = root / (_TMP_PREFIX + name)
        if not src.exists() or src == dst:
            continue
        if dst.exists():
            out["skipped"].append(name)
            continue
        try:
            _copy_atomically(src, dst, tmp)
            out["migrated"].append(name)
        except OSError as exc:
            _remove_path(tmp)
            if dst.exists():
                # 复制期间另一个进程先搬完了 —— 目标说了算，不算失败
                out["skipped"].append(name)
            else:
                out["errors"].append("%s: %s" % (name, exc))
    return out


def prepare() -> dict:
    """入口调用一次。返回一份可打印的报告（开发树 = `{"installed": False}`）。

    数据格式比本程序新时在这里备份 + 继续（见 `_check_data_schema`）。
    只在安装版或显式设了 `CRANNIA_DATA_DIR` 时做 —— 开发树的"数据根"
      就是仓库本身，往那儿写一个 `.data-schema.json` 是污染（测试会当场抓到）。
    """
    report = {"installed": is_installed(), "root": str(data_root())}
    if report["installed"] or _env("CRANNIA_DATA_DIR"):
        try:
            _root = data_root()
            _root.mkdir(parents=True, exist_ok=True)
            report["data_schema"] = _check_data_schema(_root)
        except OSError as exc:
            report["data_schema"] = {"error": str(exc)}
    if not report["installed"]:
        return report
    root = data_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report["error"] = "建数据根失败：%s" % exc
        return report
    report.update(_migrate(root))
    try:
        (root / _MARKER).write_text(json.dumps({
            "kept": "target files are never overwritten by a later upgrade",
            "app_root": str(app_root()),
            "data_root": str(root),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_run": report.get("migrated") or report.get("skipped") or [],
            "errors": report.get("errors") or [],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    try:
        os.chdir(root)
    except OSError as exc:
        report["chdir_error"] = str(exc)
    _note_incidents(report, root)
    return report


def _note_incidents(report: dict, root: Path) -> None:
    """把这一趟的失败记进 `#02` 账本（横幅与 `build_note()` 的共同数据源）。

    记在这里而不是某个入口里：三个入口（`main.py` / `crannia_tray.py` /
    `web/__main__.py`）都调 `prepare()`，写三遍就会漂。

    两条前提：
      · 只在 chdir 成功之后记 —— 没切过去时相对路径还指着安装目录，
        写进去等于把证据扔在会被卸载的地方；
      · 绝不抛 —— 记账失败不许挡住启动。
    """
    if report.get("chdir_error"):
        return
    try:
        from core import incidents
        incidents.note_data_migration(
            report, app_root=str(app_root()), data_root=str(root))
    except Exception:                                          # noqa: BLE001
        pass


def migration_report() -> dict:
    """上一次迁移留下了什么（没有就空字典）。只读。"""
    try:
        return json.loads((data_root() / _MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
