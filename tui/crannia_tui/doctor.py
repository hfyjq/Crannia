"""只读自检与口径查询：`doctor` / `config path` / `--version`。

三条命令都不启动后端。TUI 与 crannia 是两套独立更新的安装，所以这里
**不 import core**：数据根与版本都走一次子进程查询，父子编码在调用方
钉死（UTF-8、严格解码），不依赖当前控制台的编码。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.parse
from pathlib import Path
from typing import Callable

from . import paths
from .i18n import t

_MARKERS = ("main.py", "core", "web")
_DATA_CODE = "from core.paths import data_root; print(data_root())"

#: 子进程要动 `data/…` 之前先把数据根准备好。
#:
#: `core` 里的账本 / 设置路径都是**相对路径**，安装版靠 `paths.prepare()`
#: 在入口处 chdir 到 `%APPDATA%\Crannia`。TUI 的子进程不是入口，不调它的话
#: 读到的是**安装目录里那份模板**（开发树两者重合，所以测不出来）。
#: 开发树里 `prepare()` 是空操作，无副作用。
PREPARE = "from core import paths as _paths; _paths.prepare()\n"
_VERSION_CODE = "from core.version import __version__; print(__version__)"
#: 技能声明的配置需求：`requires_config` 里缺了哪些。只读，不装任何东西。
_CONFIG_CODE = (PREPARE + "import json\n"
                "from core import config_requirements as cr\n"
                "print(json.dumps(cr.warnings(), ensure_ascii=False))\n")
#: 默认那条模型配置有没有 API Key（只回布尔与名字，**不回密钥**）。
_KEY_CODE = (PREPARE + "import json\n"
             "from core import model_configs as mc\n"
             "cid = mc.default_id()\n"
             "entry = mc.get(cid) if cid else None\n"
             "try:\n"
             "    api = mc.api_for(cid) if cid else ''\n"
             "except Exception:\n"
             "    api = ''\n"
             "print(json.dumps({'id': cid or '',"
             " 'name': (entry or {}).get('name', ''),"
             " 'base_url': (entry or {}).get('base_url', ''),"
             " 'has_key': bool(api)}, ensure_ascii=False))\n")
_SETTINGS_REL = Path("data") / "settings.json"
_PROBE_PREFIX = ".crannia-doctor-probe-"


def query(python: Path, home: Path, code: str,
          timeout: float = 20.0) -> str:
    """在 crannia 根下问一句，拿最后一行 stdout。

    编码两边都钉成 UTF-8：子进程默认跟控制台编码走，父进程按 UTF-8 严格解。
    解不开就是出了意外，按失败处理，不悄悄用 "ignore" 混过去。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([str(python), "-c", code], cwd=str(home), env=env,
                       capture_output=True, timeout=timeout)
    try:
        out = r.stdout.decode("utf-8")
        err = r.stderr.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(t("子进程输出不是 UTF-8：%s") % exc) from exc
    if r.returncode != 0:
        raise RuntimeError(
            t("子进程查询失败（%s）：%s") % (r.returncode, err.strip()[-300:]))
    lines = out.strip().splitlines()
    if not lines:
        raise RuntimeError(t("子进程查询没有输出"))
    return lines[-1].strip()


def make_data_root_query(python: Path, home: Path) -> Callable[[], Path]:
    """把子进程的 data_root() 口径绑成一个无参 callable，便于注入。"""

    def get() -> Path:
        raw = query(python, home, _DATA_CODE)
        if not raw:
            raise RuntimeError(t("data_root() 返回空值"))
        return Path(raw).expanduser()

    return get


def settings_path(data_root_fn: Callable[[], Path] | None = None,
                  home: Path | None = None,
                  python: Path | None = None) -> Path:
    """设置文件的绝对路径；一行打印由调用方负责，这里只算。"""
    if data_root_fn is None:
        home = home or paths.crannia_home()
        python = python or paths.python_exe()
        data_root_fn = make_data_root_query(python, home)
    return (Path(data_root_fn()) / _SETTINGS_REL).resolve()


def app_version(python: Path | None = None, home: Path | None = None,
                query_fn: Callable[[str], str] | None = None) -> str:
    """查版本；查不到给"(版本未知)"，不失败、不启动任何东西。"""
    try:
        if query_fn is None:
            home = home or paths.crannia_home()
            python = python or paths.python_exe()
            query_fn = lambda code: query(python, home, code)
        version = query_fn(_VERSION_CODE).strip()
        if not version:
            raise RuntimeError(t("core.version 没有输出"))
    except Exception:                                        # noqa: BLE001
        return t("crannia (版本未知)")
    return f"crannia {version}"


def _port_is_free(port: int) -> bool:
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _probe_writable(root: Path) -> str:
    """写一个探针再删掉；返回空串表示可写，否则给一句原因。"""
    if not root.exists():
        return t("目录不存在")
    if not root.is_dir():
        return t("非目录")
    probe = root / f"{_PROBE_PREFIX}{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return t("写入失败：%s") % exc
    try:
        probe.unlink()
    except OSError as exc:
        return t("探针删除失败：%s") % exc
    return ""


def _is_local_url(url: str) -> bool:
    """本机地址 —— 那些模型通常不需要密钥（ollama / lmstudio 一类）。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def _live_endpoint(python: Path, home: Path):
    """活着的后端端点（没有就 None）—— 与 `profile` / 接入那条路同一条判据。"""
    from .profile import endpoint

    return endpoint(python, home)


def doctor(home: Path | str | None = None,
           python: Path | str | None = None,
           data_root_fn: Callable[[], Path] | None = None,
           port: int | None = None,
           port_probe: Callable[[int], bool] | None = None,
           skill_warnings_fn: Callable[[], list] | None = None,
           backend_fn: Callable[[], dict | None] | None = None,
           key_fn: Callable[[], dict] | None = None
           ) -> tuple[int, list[str]]:
    """逐项自检，返回 (退出码, 输出行)。全部通过 0，任一硬失败 1。

    路径与可查询对象都能显式传入：测试用临时目录和假实现就能覆盖每条判据。
    """
    lines: list[str] = []
    failed = False

    try:
        home = Path(home) if home is not None else paths.crannia_home()
    except RuntimeError as exc:
        lines.append(t("[FAIL] crannia 根：%s") % exc)
        failed, home = True, None
    else:
        missing = [m for m in _MARKERS if not (home / m).exists()]
        if missing:
            lines.append(t("[FAIL] crannia 根：%s 缺少 %s")
                         % (home, t("、").join(missing)))
            failed, home = True, None
        else:
            lines.append(t("[ok ] crannia 根：%s") % home)

    try:
        python = Path(python) if python is not None else paths.python_exe()
    except RuntimeError as exc:
        lines.append(t("[FAIL] 解释器：%s") % exc)
        failed, python = True, None
    else:
        lines.append(t("[ok ] 解释器：%s") % python)

    root: Path | None = None
    if home is not None and python is not None:
        try:
            getter = data_root_fn or make_data_root_query(python, home)
            root = Path(str(getter()).strip()).expanduser()
        except Exception as exc:                             # noqa: BLE001
            lines.append(t("[FAIL] 数据根：查询失败：%s: %s")
                         % (type(exc).__name__, exc))
            failed = True
        else:
            reason = _probe_writable(root)
            if reason:
                lines.append(t("[FAIL] 数据根：%s %s") % (root, reason))
                failed = True
            else:
                lines.append(t("[ok ] 数据根：%s 存在且可写") % root)
    else:
        lines.append(t("[FAIL] 数据根：根或解释器不可用，跳过查询"))
        failed = True

    if root is not None:
        settings = root / _SETTINGS_REL
        if not settings.exists():
            # 尚未播种是正常态（首次启动会写），只提醒、不算失败。
            lines.append(t("[warn] 设置文件：%s 尚未播种") % settings)
        else:
            try:
                json.loads(settings.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                lines.append(
                    t("[FAIL] 设置文件：%s 读取失败：%s: %s")
                    % (settings, type(exc).__name__, exc))
                failed = True
            else:
                lines.append(t("[ok ] 设置文件：%s") % settings)

    # 后端可达：有没有人在跑都不算失败 —— 没有时首次启动会拉起一个。
    if root is not None and python is not None and home is not None:
        try:
            ep = (backend_fn or (lambda: _live_endpoint(python, home)))()
        except Exception as exc:                             # noqa: BLE001
            lines.append(t("[warn] 后端：探测失败（%s）—— 跳过")
                         % type(exc).__name__)
        else:
            if ep:
                lines.append(t("[ok ] 后端：已有一个在跑（端口 %s）")
                             % ep.get("port"))
            else:
                lines.append(t("[ok ] 后端：未运行（首次启动会拉起一个）"))

    # 技能声明的配置需求：缺了只报 WARN（装得上，只是用不了那几个工具）。
    # 根或解释器不可用时跳过 —— 上面已经有 FAIL 了，这一项查不了不是新问题。
    if root is not None and python is not None and home is not None:
        try:
            warns = (skill_warnings_fn
                     or (lambda: json.loads(query(python, home, _CONFIG_CODE))))()
        except Exception as exc:                             # noqa: BLE001
            lines.append(t("[warn] 技能配置：检查失败（%s）—— 跳过")
                         % type(exc).__name__)
        else:
            for item in warns or []:
                if not isinstance(item, dict):
                    continue
                lines.append(t("[warn] 技能配置：%s 需要 %s（%s）")
                             % (item.get("skill") or "?",
                                item.get("scope") or "?",
                                item.get("where") or "?"))

    # 密钥存在：默认那条配置有没有 API Key（只回布尔，不回密钥本身）。
    # 本机模型通常不需要密钥，不算失败。
    if root is not None and python is not None and home is not None:
        try:
            got = (key_fn
                   or (lambda: json.loads(query(python, home, _KEY_CODE))))()
        except Exception as exc:                             # noqa: BLE001
            lines.append(t("[warn] 密钥：检查失败（%s）—— 跳过")
                         % type(exc).__name__)
        else:
            name = str((got or {}).get("name") or (got or {}).get("id") or "?")
            if not (got or {}).get("id"):
                lines.append(t("[warn] 密钥：还没有默认模型配置（运行 crannia setup）"))
            elif (got or {}).get("has_key"):
                lines.append(t("[ok ] 密钥：默认配置「%s」已填") % name)
            elif _is_local_url(str((got or {}).get("base_url") or "")):
                lines.append(t("[ok ] 密钥：本机模型，无需密钥"))
            else:
                lines.append(t("[FAIL] 密钥：默认配置「%s」没有 API Key") % name)
                failed = True

    if port is None:
        lines.append(t("[info] 端口：由系统分配（未传 --port）"))
    else:
        probe = port_probe or _port_is_free
        try:
            free = bool(probe(port))
        except Exception as exc:                             # noqa: BLE001
            lines.append(t("[FAIL] 端口：探测失败：%s: %s")
                         % (type(exc).__name__, exc))
            failed = True
        else:
            if free:
                lines.append(t("[ok ] 端口：%s 空闲") % port)
            else:
                lines.append(t("[FAIL] 端口：%s 已被占用") % port)
                failed = True

    return (1 if failed else 0), lines
