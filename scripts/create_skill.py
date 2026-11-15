#!/usr/bin/env python3
"""
Crannia Skill 创建向导 — interactive guided skill generator.

Usage:
    # Interactive mode (recommended)
    python scripts/create_skill.py

    # CLI mode (for automation / scripting)
    python scripts/create_skill.py my_skill --desc "Description" [--with-storage] [--with-scheduler] [--force]
"""

import argparse
import re
import shutil
import sys
import textwrap
from pathlib import Path

# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_DIR = _PROJECT_ROOT / "templates" / "skill"
_SKILLS_DIR = _PROJECT_ROOT / "skills"

_TEMPLATE_FILES = ["__init__.py", "manifest.yaml", "handler.py", "SKILL.md"]

#: 读家族判据 —— 从产品代码取，不抄第二份（`core/tool_output_kind.py`）。
#: `templates/skill/` 那一套是死的（`_TEMPLATE_FILES` 全仓零引用 ——
#: manifest 由 `_build_manifest` 现造）。这里的 import 是为了让"哪个工具必须声明
#: `output_kind`"与产品同源。
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from core.tool_output_kind import is_read_family as _is_read_family  # noqa: E402

# Extra optional templates
_STORAGE_TEMPLATE = '''"""
{skill_title} storage -- SQLite for structured data.

Thread-safe singleton pattern. Import via get_xxx_store().
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path


class {class_prefix}Store:
    """SQLite-backed storage for {skill_title} data. Thread-safe."""

    def __init__(self, db_path: str = "data/runtime/skills/{skill_underscore}/{skill_underscore}.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    tags TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
            conn.close()

    def add(self, content: str, tags: list | None = None) -> str:
        record_id = str(uuid.uuid4())[:8]
        tags_str = ",".join(tags) if tags else ""
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                "INSERT INTO records (id, content, tags) VALUES (?, ?, ?)",
                (record_id, content, tags_str),
            )
            conn.commit()
            conn.close()
        return record_id

    def list_all(self, limit: int = 20) -> list[dict]:
        with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM records WHERE status='active' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]


_store: {class_prefix}Store | None = None


def get_store() -> {class_prefix}Store:
    global _store
    if _store is None:
        _store = {class_prefix}Store()
    return _store
'''

_SCHEDULER_TEMPLATE = '''"""
{skill_title} scheduler -- daemon thread for periodic background tasks.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Deque, Tuple


class {class_prefix}Scheduler:
    """Daemon-thread scheduler. Polls storage, pushes notifications."""

    def __init__(
        self,
        interval_seconds: float = 60.0,
        on_due: object = None,
    ) -> None:
        self._interval = interval_seconds
        self._on_due = on_due
        self._notification_queue: Deque = collections.deque(maxlen=200)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name="{skill_underscore}-scheduler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def poll_notifications(self) -> list:
        out = []
        while self._notification_queue:
            out.append(self._notification_queue.popleft())
        return out

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop_event.wait(self._interval)

    def _tick(self) -> None:
        # TODO: check storage for pending items, push to queue
        pass
'''


# ------------------------------------------------------------------
# Name helpers
# ------------------------------------------------------------------

def _to_underscore(name: str) -> str:
    return name.replace("-", "_").lower()


def _to_hyphens(name: str) -> str:
    return name.replace("_", "-")


def _to_title(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


def _to_class_prefix(name: str) -> str:
    parts = name.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts)


def _validate_name(name: str) -> str | None:
    if not name:
        return "Skill name cannot be empty."
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        return (
            f"Invalid name '{name}'. "
            f"Use lowercase letters, digits, underscores. Must start with a letter."
        )
    if len(name) > 64:
        return "Name must be 64 characters or fewer."
    return None


def _validate_tool_name(name: str, existing: set) -> str | None:
    if not name:
        return "Tool name cannot be empty."
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        return "Tool name must be lowercase letters, digits, underscores. Start with a letter."
    if name in existing:
        return f"Tool '{name}' already defined."
    return None


def _validate_param_def(text: str) -> list[str] | str:
    """Parse 'name:type, name:type' into list. Return error string on failure."""
    if not text.strip():
        return []
    params = []
    valid_types = {"str", "int", "float", "bool", "list", "dict",
                   "string", "number", "integer", "boolean", "array", "object"}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            return f"Parameter '{part}' missing type (use 'name:type' format)"
        pname, ptype = part.split(":", 1)
        pname = pname.strip()
        ptype = ptype.strip().lower()
        if not re.match(r"^[a-z][a-z0-9_]*$", pname):
            return f"Invalid parameter name: '{pname}'"
        if ptype not in valid_types:
            return f"Unknown type '{ptype}' for '{pname}'. Valid types: {', '.join(sorted(valid_types))}"
        params.append((pname, ptype))
    return params  # list of (name, type) tuples


def _type_to_json(ptype: str) -> str:
    """Normalize user-friendly type name to JSON Schema type."""
    mapping = {"str": "string", "int": "integer", "float": "number",
               "bool": "boolean", "list": "array", "dict": "object"}
    return mapping.get(ptype, ptype)


def _type_to_python(ptype: str) -> str:
    """Normalize user-friendly type name to Python type annotation."""
    mapping = {"str": "str", "int": "int", "float": "float",
               "bool": "bool", "list": "list", "dict": "dict",
               "string": "str", "integer": "int", "number": "float",
               "boolean": "bool", "array": "list", "object": "dict"}
    return mapping.get(ptype, "str")


# ------------------------------------------------------------------
# Interactive prompts (Windows-safe)
# ------------------------------------------------------------------

def _print_banner() -> None:
    print()
    print("=" * 52)
    print("   Crannia Skill 创建向导 v1.0")
    print("=" * 52)
    print()
    print("本向导将引导你逐步创建一个新的原生 Skill。")
    print("生成的 Skill 完全符合 SKILL_SPEC.md 规范，")
    print("可被 bus 自动发现并加载。")
    print()


def _prompt(prompt_text: str, default: str = "") -> str:
    """Prompt for input with optional default."""
    if default:
        result = input(f"  {prompt_text} [{default}]: ").strip()
        return result if result else default
    return input(f"  {prompt_text}: ").strip()


def _prompt_yn(prompt_text: str, default: bool = False) -> bool:
    yn = "Y/n" if default else "y/N"
    default_str = "Y" if default else "N"
    while True:
        answer = input(f"  {prompt_text} [{yn}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _section(title: str) -> None:
    print()
    print(f"--- {title} ---")


def _info(text: str) -> None:
    print(f"  {text}")


# ------------------------------------------------------------------
# Interactive flow
# ------------------------------------------------------------------

def _collect_tools_interactive() -> list[dict]:
    """Interactively collect tool definitions. Returns list of tool dicts."""
    tools = []
    names_seen: set = set()

    print()
    print("  现在定义 Skill 提供的工具 (Tools)。")
    print("  每个工具 = 一个 LLM 可调用的函数。")
    print("  至少需要 1 个工具。输入空名称结束定义。")
    print()

    while True:
        # Tool name
        while True:
            name = input(f"  工具 {len(tools) + 1} 名称 (小写下划线, 如 'parse_file'): ").strip().lower()
            if not name and tools:
                return tools
            if not name:
                print("    至少需要一个工具。")
                continue
            err = _validate_tool_name(name, names_seen)
            if err:
                print(f"    {err}")
                continue
            break

        # Description
        desc = input(f"  描述 (一句话): ").strip()
        if not desc:
            desc = f"Handle {name} operation"

        # Parameters
        print(f"  参数 (格式: name:type, name:type)")
        print(f"  类型: str / int / float / bool / list")
        print(f"  示例: location:str, days:int, use_fahrenheit:bool")
        print(f"  留空 = 无参数")
        while True:
            params_raw = input(f"  > ").strip()
            result = _validate_param_def(params_raw)
            if isinstance(result, str):
                print(f"    {result}")
                continue
            break

        # Required params
        required = []
        if result:
            param_names = {p[0] for p in result}
            req_input = input(f"  哪些是必填参数? (参数名逗号分隔, 留空=全部可选): ").strip()
            if req_input:
                for r in req_input.split(","):
                    r = r.strip()
                    # Strip accidental type suffix (user typed "name:type")
                    if ":" in r:
                        r = r.split(":")[0].strip()
                    if r not in param_names:
                        print(f"    警告: '{r}' 不是已定义的参数，已跳过")
                        continue
                    required.append(r)
            else:
                required = []

        # 权限档是必填的，而不替人决定 ——
        # 直接回车 = 留 `TODO`（非法值），装载时落进「未定档」+ 棘轮当场红。
        # 不拿 `read` 当默认：写错成 read 是危险的那一侧（并发跑 +
        #   只读子 agent 放行它）；也不拿 `write` 当默认（对纯读工具是假话）。
        while True:
            perm = _prompt("权限档 read/write/admin（拿不准写 write；留空=以后再说）").lower()
            if perm in ("read", "write", "admin", ""):
                break
            _info("只认 read / write / admin")

        tools.append({
            "name": name,
            "description": desc,
            "params": result,
            "required": required,
            "permission": perm,
        })
        names_seen.add(name)
        _info(f"  已添加: {name} ({len(result)} params)")

    return tools


# ------------------------------------------------------------------
# File generation
# ------------------------------------------------------------------

def _build_manifest(
    skill_underscore: str,
    description: str,
    tools: list[dict],
) -> str:
    """Build manifest.yaml content."""
    lines = [
        "kind: skill",
        f"name: {skill_underscore}",
        'version: "0.1.0"',
        "draft: true",
        f'description: "{description}"',
        "",
        "# ==================================================================",
        "# v0.1.0: Initial tools",
        "# ==================================================================",
        "",
        "tools:",
    ]

    for i, t in enumerate(tools):
        lines.append(f"  - name: {t['name']}")
        # `permission` 是必填，而这里不给安全默认 ——
        # 没定就写 `TODO`，那是个非法值，`declare()` 不收，装载时该工具
        # 落进「未定档」，`tests/test_permission.py` 的棘轮当场红，而且
        # 启动会打一行 WARNING 把非法值印出来。
        # 不许默认 `read`：那是危险的那一侧（写错成 read = 并发跑 + 只读
        #   子 agent 放行它）。也不许默认 `write` —— 那对纯读工具是句假话。
        #   宁可留一个响亮的空。
        lines.append(f"    permission: {t.get('permission') or 'TODO'}")
        # `destructive` 同样必填，同样不给默认。
        # 与上一行方向相反的坑：`permission` 拼错落 `admin`（fail-closed，
        #   安全侧）；`destructive` 拼错落 `false`（fail-open），一个不可逆的
        #   工具会静默变回「可逆」，闸连问都不会问。
        #留 `TODO`（非法值，不收，按「没声明」处置）比留一个 `false` 响亮。
        lines.append(f"    destructive: {t.get('destructive') if isinstance(t.get('destructive'), bool) else 'TODO'}")
        # 必须发 `output_kind` —— 漏了它时，新造的读家族工具
        # 会落进 fail-closed，且体检器报 WARN。
        # 只对读家族发：对其它工具它不适用，而无条件发一个 `TODO`
        # 会让 `_note_output_kind` 对每个工具都打一条「非法值」警告（噪声）。
        _ok = t.get("output_kind")
        if _ok or _is_read_family(t["name"]):
            lines.append(f"    output_kind: {_ok or 'TODO'}")
        lines.append(f"    description: >")
        lines.append(f"      {t['description']}")
        lines.append(f"    inputSchema:")
        lines.append(f'      $schema: "https://json-schema.org/draft/2020-12/schema"')
        lines.append(f"      type: object")
        lines.append(f"      properties:")

        if not t["params"]:
            lines.append("        {}")
        else:
            for pname, ptype in t["params"]:
                json_type = _type_to_json(ptype)
                lines.append(f"        {pname}:")
                lines.append(f"          type: {json_type}")
                lines.append(f'          description: "{pname} parameter"')

        if t["required"]:
            req_str = ", ".join(t["required"])
            lines.append(f"      required: [{req_str}]")
        else:
            lines.append(f"      required: []")

        if i < len(tools) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"


def _build_handler(
    skill_title: str,
    skill_underscore: str,
    tools: list[dict],
    has_storage: bool,
    has_scheduler: bool,
) -> str:
    """Build handler.py content."""
    tool_names = ", ".join(t["name"] for t in tools)
    first_tool = tools[0]["name"] if tools else "example_action"

    lines = [
        f'"""',
        f"{skill_title} skill handlers: {tool_names}.",
        f"",
        f"Hard constraints enforced here:",
        f"  1. Streaming: iterator-based processing --no bulk reads.",
        f"  2. Security: validate_path() BEFORE any file is opened.",
        f"  3. Chunking: all text flows through core.chunker (max 1500 chars).",
        f'"""',
        f"",
        f"from pathlib import Path",
        f"",
        f"from core.security import SecurityGuard",
        f"",
    ]

    if has_storage:
        lines.append(f"from skills.{skill_underscore}.storage import get_store")
        lines.append("")

    if has_scheduler:
        lines.append(f"from skills.{skill_underscore}.scheduler import get_scheduler")
        lines.append("")

    # Optional import hints
    # Check if any tool likely needs chunk_stream
    needs_file_ops = any(
        "file" in t["name"].lower() or "parse" in t["name"].lower()
        or "path" in t["name"].lower()
        for t in tools
    )
    if needs_file_ops:
        lines.append("# Optional: from core.chunker import chunk_stream")
        lines.append("")

    lines += [
        "# ------------------------------------------------------------------",
        "# Injected by skill_loader at startup",
        "# ------------------------------------------------------------------",
        "",
        "_security: SecurityGuard | None = None",
        "",
        "",
        "def init(security: SecurityGuard) -> None:",
        '    """Called by skill_loader after module import. Sets the shared security guard."""',
        "    global _security",
        "    _security = security",
    ]

    if has_scheduler:
        lines += [
            "",
            "    # Start background scheduler",
            "    sched = get_scheduler()",
            "    sched.start()",
        ]

    lines += [
        "",
        "",
        "# ------------------------------------------------------------------",
        "# Tool handlers",
        "# ------------------------------------------------------------------",
        "",
    ]

    # Generate each tool handler
    for t in tools:
        params = t["params"]
        param_list = ["self"] if False else []  # no self, flat functions
        for pname, ptype in params:
            default = '""' if ptype in ("str", "string") else "None"
            param_list.append(f"{pname}: {_type_to_python(ptype)} = {default}")

        sig = ", ".join(param_list) if param_list else ""
        lines.append(f"def {t['name']}({sig}) -> dict:")
        lines.append(f'    """')
        lines.append(f"    {t['description']}")
        lines.append(f"")
        lines.append(f"    Args:")
        for pname, ptype in params:
            lines.append(f"        {pname}: Parameter description.")
        if not params:
            lines.append(f"        (no parameters)")
        lines.append(f"")
        lines.append(f"    Returns:")
        lines.append(f'        dict: {{"success": bool, ...}} --unified return format.')
        lines.append(f'    """')
        lines.append(f"    _require_security()")
        lines.append(f"")
        if has_storage:
            lines.append(f"    store = get_store()")
            lines.append(f"")
        lines.append(f"    # TODO: Implement {t['name']} logic")
        lines.append(f"")
        lines.append(f"    return {{")
        lines.append(f'        "success": True,')
        if params:
            first_param = params[0][0]
            lines.append(f'        "message": f"Executed {t["name"]} with {{{first_param}}}",')
        else:
            lines.append(f'        "message": "Executed {t["name"]}",')
        lines.append(f"    }}")
        lines.append("")
        lines.append("")

    lines += [
        "# ------------------------------------------------------------------",
        "# Internal helpers",
        "# ------------------------------------------------------------------",
        "",
        "def _require_security() -> None:",
        '    """Raise RuntimeError if SecurityGuard hasn\'t been injected yet."""',
        "    if _security is None:",
        "        raise RuntimeError(",
        '            "SecurityGuard not initialized. "',
        '            "The skill_loader must call init() before any handler is invoked."',
        "        )",
        "",
        "",
        "def _resolve_path(file_path: str) -> Path:",
        '    """Try file_path as-is; if not found, try under workspace/."""',
        "    p = Path(file_path)",
        "    if p.exists():",
        "        return p.resolve()",
        '    alt = Path("workspace") / p.name',
        "    if alt.exists():",
        "        return alt.resolve()",
        "    return p.resolve()",
    ]

    return "\n".join(lines) + "\n"


def _build_skill_md(
    skill_underscore: str,
    skill_hyphens: str,
    skill_title: str,
    description: str,
    tools: list[dict],
    has_storage: bool,
    has_scheduler: bool,
) -> str:
    """Build SKILL.md content."""
    tool_names = ", ".join(t["name"] for t in tools)
    first_tool = tools[0]["name"] if tools else "example_action"

    lines = [
        "---",
        f"name: {skill_hyphens}",
        f"description: {description} Use when the user needs to ...",
        "metadata:",
        "  kind: skill",
        "  loader: manifest.yaml",
        f"  tools: {tool_names}",
        "  mcp_note: >",
        f"    Directory name is {skill_underscore} (underscore) for Python",
        f"    import compatibility.  SKILL.md name uses {skill_hyphens}",
        f"    (hyphen) per agentskills.io standard.",
        "---",
        "",
        f"# {skill_title}",
        "",
        "## When to use",
        "Use this skill when the user needs to ...",
        "",
        "## Tools",
        "",
    ]

    for t in tools:
        lines.append(f"### {t['name']}")
        lines.append(f"{t['description']}")
        lines.append("")

    notes = [
        "## Notes",
        "- File path resolution: bare filenames are auto-resolved under `workspace/`",
        "- All paths validated by SecurityGuard before any file is opened",
        '- All tool results follow the unified `{"success": bool, ...}` format',
        "- See SKILL_SPEC.md for the complete skill development specification",
    ]
    if has_storage:
        notes.append(f"- Storage: SQLite database at `data/runtime/skills/{skill_underscore}/{skill_underscore}.db`")
    if has_scheduler:
        notes.append(f"- Background scheduler: daemon thread for periodic tasks")
    lines += notes

    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------
# Core generation
# ------------------------------------------------------------------

def create_skill(
    name: str,
    description: str,
    *,
    tools: list[dict] | None = None,
    with_storage: bool = False,
    with_scheduler: bool = False,
    force: bool = False,
    skills_dir: "Path | str | None" = None,
) -> int:
    """Create a new skill directory from templates or interactive definitions.

    Returns 0 on success, 1 on error.

    `skills_dir` 是给测试留的口：不传 = 生产行为
    （写到仓根那个 `skills/`）；传了 = 写到别处。

    为什么非要有这个口：`_SKILLS_DIR` 是模块加载时算出来的绝对路径,
光靠 `chdir` 进临时目录拦不住它（`tests/_sandbox.py` 那套对相对默认值有效，
    对它无效）。而测试要往真 `skills/` 里建又删、净变化为零，
    端到端污染自查（比前后快照）看不见。这个口就是为它留的。
    """
    skill_underscore = _to_underscore(name)
    skill_hyphens = _to_hyphens(skill_underscore)
    skill_title = _to_title(skill_underscore)
    class_prefix = _to_class_prefix(skill_underscore)

    # Validate
    err = _validate_name(skill_underscore)
    if err:
        print(f"Error: {err}")
        return 1

    target_dir = (Path(skills_dir) if skills_dir is not None else _SKILLS_DIR) / skill_underscore
    if target_dir.exists() and not force:
        print(f"Error: Directory already exists: {target_dir}")
        print(f"Use --force to overwrite.")
        return 1

    if not _TEMPLATE_DIR.exists():
        print(f"Error: Template directory not found: {_TEMPLATE_DIR}")
        return 1

    # Handle legacy mode: if tools not provided, use template placeholder
    if tools is None:
        tools = [{
            "name": f"{skill_underscore}_action",
            "description": f"Example tool for {skill_title} skill --replace with real functionality.",
            "params": [("input_param", "string")],
            "required": ["input_param"],
        }]

    # Create directory
    target_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n  正在创建 Skill: skills/{skill_underscore}/")

    # -- __init__.py: copy empty file --
    (target_dir / "__init__.py").write_text("", encoding="utf-8")
    print(f"  WRITE __init__.py")

    # -- manifest.yaml: custom generation based on tool definitions --
    manifest_content = _build_manifest(skill_underscore, description, tools)
    (target_dir / "manifest.yaml").write_text(manifest_content, encoding="utf-8")
    print(f"  WRITE manifest.yaml ({len(tools)} tool(s))")

    # -- handler.py: custom generation --
    handler_content = _build_handler(skill_title, skill_underscore, tools, with_storage, with_scheduler)
    (target_dir / "handler.py").write_text(handler_content, encoding="utf-8")
    print(f"  WRITE handler.py ({len(tools)} handler(s))")

    # -- SKILL.md: custom generation --
    skill_md_content = _build_skill_md(
        skill_underscore, skill_hyphens, skill_title,
        description, tools, with_storage, with_scheduler,
    )
    (target_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    print(f"  WRITE SKILL.md")

    # -- Optional: storage.py --
    if with_storage:
        storage_content = _STORAGE_TEMPLATE.format(
            skill_title=skill_title,
            skill_underscore=skill_underscore,
            class_prefix=class_prefix,
        )
        (target_dir / "storage.py").write_text(storage_content, encoding="utf-8")
        print(f"  WRITE storage.py")

    # -- Optional: scheduler.py --
    if with_scheduler:
        sched_content = _SCHEDULER_TEMPLATE.format(
            skill_title=skill_title,
            skill_underscore=skill_underscore,
            class_prefix=class_prefix,
        )
        (target_dir / "scheduler.py").write_text(sched_content, encoding="utf-8")
        print(f"  WRITE scheduler.py")

    # Summary
    print()
    print(f"  [OK] Skill '{skill_underscore}' 创建成功!")
    _todo = [t["name"] for t in tools if not t.get("permission")]
    if _todo:
        print()
        print(f"  [!] {len(_todo)} 个工具还没定权限档：{', '.join(_todo)}")
        print("      manifest 里写的是 `permission: TODO` —— 那是个**非法值**"
              "（`core/tool_permission.py::declare` 不收）")
        print("      => 装载时它们落进「未定档」，`tests/test_permission.py` 的棘轮当场红。")
        print("      => 拿不准写 `write`：写错成 `read` 是**危险的那一侧**"
              "（会并发跑、会被只读子 agent 放行）。")
    _todo_d = [t["name"] for t in tools if not isinstance(t.get("destructive"), bool)]
    if _todo_d:
        print()
        print(f"  [!] {len(_todo_d)} 个工具还没定 `destructive`：{', '.join(_todo_d)}")
        print("      manifest 里写的是 `destructive: TODO` —— 那**不是布尔**"
              "（`core/tool_permission.py::declare_destructive` 不收）。")
        print("      => 它按「没声明」处置，而这一根的兜底是 **false（fail-open）**："
              "一个不可逆的工具会**静默变回「可逆」**。")
        print("      => 拿不准写 `true`（多问一次，代价看得见）。")
    print()
    print("  下一步:")
    print(f"    1. 编辑 skills/{skill_underscore}/handler.py 实现工具逻辑")
    print(f"    2. 编辑 skills/{skill_underscore}/SKILL.md 完善文档")
    print(f"    3. 运行 .venv/Scripts/python tests/test_skill_spec.py 验证格式")
    print(f"    4. 输入 /skill check {skill_underscore} 让 Agent 审查并补齐")
    print(f"    5. 审查通过后 /skill approve {skill_underscore} 进入加载区")
    print()
    return 0


# ------------------------------------------------------------------
# Interactive mode
# ------------------------------------------------------------------

def interactive_mode() -> int:
    """Run the interactive skill creation wizard."""
    _print_banner()

    # Step 1: Name
    _section("Step 1: Skill 名称")
    while True:
        name = _prompt("Skill 名称 (小写下划线, 如 my_parser)")
        err = _validate_name(name)
        if err:
            print(f"  {err}")
            continue
        skill_dir = _SKILLS_DIR / name
        if skill_dir.exists():
            overwrite = _prompt_yn(f"  目录 'skills/{name}/' 已存在。覆盖?", default=False)
            if not overwrite:
                continue
        break

    # Step 2: Description
    _section("Step 2: 描述")
    _info("好的描述会被注入 system prompt，帮助 LLM 决定何时使用这个 Skill。")
    _info("例如: 'Parse PDF files with streaming chunking. Use when working with PDF documents.'")
    desc = _prompt("一句话描述")
    if not desc:
        desc = f"Skill for {name}"

    # Step 3: Features
    _section("Step 3: 可选功能")
    has_storage = _prompt_yn("是否需要本地数据存储 (SQLite)?", default=False)
    has_scheduler = _prompt_yn("是否需要后台定时任务 (daemon thread)?", default=False)

    # Step 4: Tools
    _section("Step 4: 工具定义")
    tools = _collect_tools_interactive()

    # Step 5: Preview & Confirm
    _section("Step 5: 确认")
    print()
    print(f"  名称:       {name}")
    print(f"  描述:       {desc}")
    print(f"  数据存储:   {'Yes' if has_storage else 'No'}")
    print(f"  定时任务:   {'Yes' if has_scheduler else 'No'}")
    print(f"  工具数量:   {len(tools)}")
    for t in tools:
        param_str = ", ".join(f"{p[0]}:{p[1]}" for p in t["params"]) if t["params"] else "none"
        req_str = f" [required: {', '.join(t['required'])}]" if t["required"] else ""
        print(f"    - {t['name']}({param_str}){req_str}")
    print()
    print(f"  将生成以下文件:")
    print(f"    skills/{name}/")
    print(f"      __init__.py")
    print(f"      manifest.yaml   ({len(tools)} tools)")
    print(f"      handler.py       ({len(tools)} handlers)")
    print(f"      SKILL.md")
    if has_storage:
        print(f"      storage.py       (SQLite)")
    if has_scheduler:
        print(f"      scheduler.py     (daemon thread)")
    print()

    if not _prompt_yn("确认创建?", default=True):
        print("  已取消。")
        return 1

    return create_skill(
        name=name,
        description=desc,
        tools=tools,
        with_storage=has_storage,
        with_scheduler=has_scheduler,
        force=True,  # already checked in step 1
    )


# ------------------------------------------------------------------
# CLI entry
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crannia Skill 创建向导",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python scripts/create_skill.py                       # Interactive mode
              python scripts/create_skill.py my_parser --desc "Parse custom files"
              python scripts/create_skill.py my_skill --desc "Desc" --with-storage --force
        """),
    )
    parser.add_argument(
        "name", nargs="?",
        help="Skill name (lowercase, underscores). Omit for interactive mode.",
    )
    parser.add_argument(
        "--desc", "--description", dest="description",
        help="One-line description",
    )
    parser.add_argument(
        "--with-storage", action="store_true",
        help="Generate storage.py skeleton",
    )
    parser.add_argument(
        "--with-scheduler", action="store_true",
        help="Generate scheduler.py skeleton",
    )
    parser.add_argument(
        "--force", "-f", action="store_true",
        help="Overwrite existing skill directory",
    )

    args = parser.parse_args()

    # Interactive mode when no name provided
    if not args.name:
        exit_code = interactive_mode()
        sys.exit(exit_code)

    # CLI mode
    if not args.description:
        print("Error: --desc is required in CLI mode")
        sys.exit(1)

    exit_code = create_skill(
        name=args.name,
        description=args.description,
        with_storage=args.with_storage,
        with_scheduler=args.with_scheduler,
        force=args.force,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
