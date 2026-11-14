# -*- coding: utf-8 -*-
"""技能的框架上下文。

一个技能通过 `get_context(name)` 取得 `SkillContext`，包含四项能力：

    ctx.config    配置，来自 manifest.settings.storage 指向的文件，每次访问重新读取
    ctx.data_dir  可写目录：data/runtime/skills/<name>/
    ctx.store     键值存储，原子写入，单文件上限 1 MB，带版本号
    ctx.jobs      后台任务，写入统一任务表，类别为 skill

约束：

- 代码目录只读。需要写文件时使用 `ctx.data_dir`。
- 配置由 `core.skill_settings` 读写。`ctx.config` 返回原样配置，其中可能含明文
  凭据，不要写入工具回执或日志。
- `get_context` 每次返回新对象，不做缓存。配置在访问时读取，修改后立即生效。
- 存储文件记录的版本高于当前框架支持的版本时，读取正常，写入抛出
  `StoreVersionError`。

示例::

    from core.extension import get_context

    def my_tool(text: str) -> dict:
        ctx = get_context("my_skill")
        ctx.store.set("last_run", 1700000000)
        return {"success": True, "text": text}
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "SkillContext", "StoreFacade", "JobsFacade", "StoreVersionError",
    "get_context", "STORE_MAX_BYTES", "RUNTIME_ROOT",
]

#: 技能数据的根目录。使用相对路径，测试通过切换工作目录隔离。
RUNTIME_ROOT = Path("data") / "runtime" / "skills"

#: 单个技能的键值文件上限。超过时拒绝写入。需要更大的数据时直接写入 ctx.data_dir。
STORE_MAX_BYTES = 1024 * 1024

_STORE_NAME = "store.json"

#: 技能名取自目录名，只允许字母、数字、下划线、点和连字符，且不以符号开头。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: 写键值文件的互斥锁。
_IO_LOCK = threading.Lock()

#: 技能数据目录里的版本文件。内容是一行整数。
DATA_VERSION_FILE = ".data-version"


class StoreVersionError(RuntimeError):
    """存储文件由更高版本的框架写入，只允许读取。"""


class StoreFacade:
    """一个技能一份的 JSON 键值存储。

    文件结构为 {"_version": 1, "data": {...}}。版本与数据存放在同一个文件，
    避免出现版本已升级而数据未写入的中间状态。
    """

    def __init__(self, name: str, data_dir: Path, version: int = 1) -> None:
        self.name = name
        self.dir = Path(data_dir)
        self.version = int(version)
        self.path = self.dir / _STORE_NAME

    def _load(self) -> tuple[int, dict]:
        """返回文件中的版本与数据。

        文件不存在、无法解析或结构不符时返回当前版本与空字典，并输出一行提示。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self.version, {}
        except (OSError, ValueError) as exc:
            print("[skill-store] %s 读取失败（%s: %s），按空处理"
                  % (self.path, type(exc).__name__, exc))
            return self.version, {}
        if not isinstance(raw, dict):
            print("[skill-store] %s 不是对象，按空处理" % self.path)
            return self.version, {}
        ver = raw.get("_version")
        # bool 是 int 的子类，需要单独排除。
        if isinstance(ver, bool) or not isinstance(ver, int):
            ver = self.version
        data = raw.get("data")
        return ver, (data if isinstance(data, dict) else {})

    def _save(self, data: dict) -> None:
        payload = json.dumps({"_version": self.version, "data": data},
                             ensure_ascii=False, sort_keys=True)
        size = len(payload.encode("utf-8"))
        if size > STORE_MAX_BYTES:
            raise ValueError(
                "键值存储超过上限：%d B > %d B；更大的数据请写入 ctx.data_dir"
                % (size, STORE_MAX_BYTES))
        tmp = self.path.with_name(_STORE_NAME + ".tmp")
        with _IO_LOCK:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)

    def _writable(self) -> dict:
        ver, data = self._load()
        if ver > self.version:
            raise StoreVersionError(
                "存储文件版本为 %d，高于当前框架的 %d；请先升级，或删除 %s"
                % (ver, self.version, self.path))
        return data

    def get(self, key: str, default: Any = None) -> Any:
        _, data = self._load()
        return data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("键必须是非空字符串")
        data = self._writable()
        data[key] = value
        self._save(data)

    def delete(self, key: str) -> bool:
        data = self._writable()
        if key not in data:
            return False
        del data[key]
        self._save(data)
        return True

    def all(self) -> dict:
        """返回数据的副本，修改副本不会写入文件。"""
        _, data = self._load()
        return json.loads(json.dumps(data))

    def __repr__(self) -> str:
        return "<StoreFacade %s v%d %s>" % (self.name, self.version, self.path)


class JobsFacade:
    """把后台工作提交到统一任务表。

    后台任务是一次性的执行体，会话清理时会终止。需要长期运行的服务不在这个接口内。
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def start(self, fn: Callable[[], Any], *, name: str = "",
              session_key: str = "", storage_id: str = "",
              description: str = "", notify: bool = False) -> str:
        """在新线程中运行 *fn*，返回任务 id。

        *fn* 不接收参数。默认不发送完成通知，结果由调用方自行处理。
        """
        from core import jobs

        label = (name or description or self.name).strip()
        job = jobs.start_thread(
            lambda _job: fn(),
            kind=jobs.KIND_SKILL,
            command=label,
            session_key=session_key,
            storage_id=storage_id,
            description=description or label,
            notify=bool(notify),
        )
        return job.id

    def status(self, job_id: str) -> Optional[dict]:
        from core import jobs
        job = jobs.get(job_id)
        return job.snapshot() if job is not None else None

    def cancel(self, job_id: str) -> bool:
        from core import jobs
        return jobs.cancel(job_id)


class MemoryFacade:
    """长期记忆：读、搜、写。与模型的记忆三件走同一套存储与上限。"""

    def read(self, name: str = "") -> str:
        from core.memory_store import memory_read
        return memory_read(name)

    def search(self, query: str = "", limit: int = 5) -> str:
        from core.memory_store import memory_search
        return memory_search(query, limit=limit)

    def write(self, name: str = "", description: str = "",
              content: str = "") -> dict:
        from core.memory_store import memory_write
        return memory_write(name=name, description=description, content=content)


class LLMFacade:
    """模型调用：不带工具、不计入会话的工具循环。

    默认使用 `config.yaml` 里 `model.provider` 那一条；也可以显式指定。
    """

    def chat(self, messages, *, system=None, temperature=None,
             provider=None) -> str:
        from bus.registry import get_provider
        name = str(provider or "").strip() or _default_provider()
        try:
            client = get_provider(name)
        except KeyError as exc:
            raise RuntimeError("没有这个 provider：%s（%s）" % (name, exc)) from exc
        response = client.chat(messages, system=system, temperature=temperature)
        return getattr(response, "content", "") or ""


class ApprovalFacade:
    """在工具里向用户要一次授权。

    工具返回 `request()` 的结果之后，这一轮挂起；用户同意则重放这次调用，
    第二次进来用 `granted()` 判断是否已经批过。`subject` 是被审批物，
    同一个 subject 在本次对话里批过一次就不再问。
    """

    def request(self, subject: str, body: str = "") -> dict:
        from bus.registry import get_workspace_manager
        from core.approval import pending_payload
        ask = 1
        wm = get_workspace_manager()
        if wm is not None:
            try:
                ask = int((wm.approval_ask_counts or {}).get("destructive", 0)) + 1
            except Exception:                                 # noqa: BLE001
                pass
        return pending_payload(
            "destructive",
            body=body or "这个技能想执行一次不可逆的操作：%s" % subject,
            ask_index=ask, footer=True, subject=str(subject),
            status="pending_destructive_approval")

    def granted(self, subject: str) -> bool:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm is None:
            return False
        try:
            return bool(wm.has_target_grant("destructive", str(subject)))
        except Exception:                                     # noqa: BLE001
            return False


def _default_provider() -> str:
    """配置文件里 `model.provider`；读不到就报错，不猜。"""
    try:
        import yaml
        from core import paths
        data = yaml.safe_load(Path(paths.config_path()).read_text(encoding="utf-8")) or {}
        name = str((data.get("model") or {}).get("provider") or "").strip()
    except Exception as exc:                                  # noqa: BLE001
        raise RuntimeError("读不到默认 provider（%s: %s）" % (type(exc).__name__, exc))
    if not name:
        raise RuntimeError("配置里没有 model.provider，请显式指定 provider")
    return name


@dataclass(frozen=True)
class SkillContext:
    """一个技能的框架上下文。"""

    name: str
    root: Path
    data_dir: Path
    store: StoreFacade
    jobs: JobsFacade
    memory: MemoryFacade
    llm: LLMFacade
    approval: ApprovalFacade
    logger: logging.Logger
    data_version: int = 1

    def has(self, capability: str) -> bool:
        """本框架是否提供某项能力（名字与 manifest 的 `requires` / `optional` 同一套）。

        技能据此写降级：`optional` 列了某项、这里返回 False，走简版路，
        而不是等一次调用失败。它答的是「框架有没有这项能力」，
        不是「这一次调用此刻能不能成功」—— 后者取决于运行期状态。
        """
        from core.capabilities import CAPABILITIES
        return str(capability).strip() in CAPABILITIES

    def has_external(self, name: str) -> bool:
        """技能声明的某个外部程序在这台机器上能不能用。

        与技能页同源（都走 `core.externals`）：manifest 里那条怎么找，
        这里就怎么找 —— 所以界面上写着「缺 X」的时候，代码里这支也是假。
        没声明的名字按 PATH 找。
        """
        from core import externals as _ext
        want = str(name).strip()
        for entry in _ext.normalise(_declared_external(self.root), root=self.root):
            if entry["name"] == want:
                return _ext.ready(entry)["ok"]
        return _ext.ready({"name": want})["ok"]

    def check_data(self, migrate=None) -> dict:
        """核对数据版本。

        文件不存在，按当前版本建立。文件版本更高，拒绝（不静默改坏）。
        更低，调 *migrate(ctx, found, ours)*；没有迁移器则拒绝。
        返回 `{"ok": bool, "found": int, "ours": int, ...}`。
        """
        path = Path(self.data_dir) / DATA_VERSION_FILE
        ours = int(self.data_version or 1)
        try:
            found = int(path.read_text(encoding="utf-8").strip() or ours)
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(ours), encoding="utf-8")
            return {"ok": True, "found": ours, "ours": ours}
        except (OSError, ValueError):
            return {"ok": True, "found": ours, "ours": ours}
        if found == ours:
            return {"ok": True, "found": found, "ours": ours}
        if found > ours:
            return {"ok": False, "found": found, "ours": ours,
                    "error": "数据版本 %d 高于本框架支持的 %d；请升级后再用"
                             % (found, ours)}
        if callable(migrate):
            try:
                migrate(self, found, ours)
            except Exception as exc:                          # noqa: BLE001
                return {"ok": False, "found": found, "ours": ours,
                        "error": "迁移失败：%s: %s"
                                 % (type(exc).__name__, exc)}
            path.write_text(str(ours), encoding="utf-8")
            return {"ok": True, "found": found, "ours": ours, "migrated": True}
        return {"ok": False, "found": found, "ours": ours,
                "error": "数据版本 %d 低于 %d，且没有提供 migrate()"
                         % (found, ours)}

    def progress(self, text: str, level: str = "info") -> bool:
        """记一条状态，显示在技能卡片上。技能没有在跑的服务时返回 False。

        只保留最近一条，不落盘；正文截到 200 字符。
        """
        from core import services
        return services.note(self.name, text, level)

    @property
    def config(self) -> Mapping[str, Any]:
        """配置的实时视图。未声明 settings 或读取失败时返回空字典。

        返回值可能包含明文凭据，仅用于工具实现，不要写入回执或日志。
        """
        from core import skill_settings
        try:
            return skill_settings.load(self.name) or {}
        except Exception as exc:                          # noqa: BLE001
            self.logger.warning("配置读取失败，按空处理：%s: %s",
                                type(exc).__name__, exc)
            return {}


def _skill_dir(name: str) -> Optional[Path]:
    """在扫描根中查找同名且包含 manifest.yaml 的目录。

    只处理深度为 1 的根，即 <根>/<名字>/manifest.yaml 这种结构。
    """
    try:
        from core.skill_sources import skill_root_specs
        specs = skill_root_specs()
    except Exception:                                     # noqa: BLE001
        specs = []
    for spec in specs:
        if spec.depth != 1:
            continue
        cand = Path(spec.path) / name
        try:
            if (cand / "manifest.yaml").is_file():
                return cand
        except OSError:
            continue
    return None


def _declared_external(root) -> list:
    """读技能目录 `manifest.yaml` 的 `external:` —— 读不到就是空。"""
    try:
        import yaml
        data = yaml.safe_load(
            (Path(root) / "manifest.yaml").read_text(encoding="utf-8")) or {}
    except Exception:                                         # noqa: BLE001
        return []
    value = data.get("external") if isinstance(data, dict) else None
    return value if isinstance(value, (list, tuple, str, dict)) else []


def _manifest_version(manifest_path: Path, key: str) -> int:
    """读取 manifest 里某个整数版本字段，缺省或非法时返回 1。"""
    try:
        import yaml
        m = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception:                                     # noqa: BLE001
        return 1
    if not isinstance(m, dict):
        return 1
    v = m.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return 1
    return int(v)


def _store_version(manifest_path: Path) -> int:
    """读取 manifest 中的 store_version，缺省或非法时返回 1。"""
    return _manifest_version(manifest_path, "store_version")


def get_context(name: str) -> SkillContext:
    """取得技能上下文。

    每次调用新建对象。技能名非法时抛出 `ValueError`，技能不存在时抛出
    `LookupError`。
    """
    key = (name or "").strip()
    if not _NAME_RE.match(key):
        raise ValueError(
            "技能名不合法：%r；只允许字母、数字、下划线、点和连字符，且不以符号开头"
            % (name,))
    root = _skill_dir(key)
    if root is None:
        raise LookupError(
            "没有这个技能：%s；在 skills/ 与共享根中均未找到包含 manifest.yaml 的"
            "同名目录" % key)
    data_dir = RUNTIME_ROOT / key
    data_dir.mkdir(parents=True, exist_ok=True)
    version = _store_version(root / "manifest.yaml")
    data_version = _manifest_version(root / "manifest.yaml", "data_version")
    return SkillContext(
        name=key,
        root=root,
        data_dir=data_dir,
        store=StoreFacade(key, data_dir, version),
        jobs=JobsFacade(key),
        memory=MemoryFacade(),
        llm=LLMFacade(),
        approval=ApprovalFacade(),
        logger=logging.getLogger("crannia.skill." + key),
        data_version=data_version,
    )
