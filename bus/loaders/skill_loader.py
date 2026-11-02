"""
Load a skill from its manifest.yaml, validate JSON Schema tool definitions,
look up handler functions, and register tools in the global registry.

Also supports SKILL.md (agentskills.io standard) for metadata and
ecosystem compatibility.  The manifest.yaml remains the authoritative
source for tool definitions; SKILL.md provides name/description/instructions.
"""

import importlib
import threading
import re
from pathlib import Path

import yaml

from bus.registry import register_tool

# 认得 JSON Schema 里的这几种类型
_VALID_JSON_TYPES = {"string", "number", "integer", "boolean", "array", "object"}

# Known JSON Schema $schema URIs we accept
_VALID_SCHEMA_URIS = {
    "https://json-schema.org/draft/2020-12/schema",
    "https://json-schema.org/draft/2019-09/schema",
    "http://json-schema.org/draft-07/schema#",
}


def _make_importable(skill_dir: Path) -> None:
    """让 `skills.<名字>.handler` 对仓外的技能目录也成立。

    仓内技能本来就在 `skills` 包里，按名字导入天然成立；用户装的
    （技能页「添加技能」→ `data/skills/`，安装版里与安装目录分家）不在包里，
    于是 `importlib.import_module("skills.<名>.handler")` 抛
    `ModuleNotFoundError`，而它发生在第一次调用那一刻 ——
    症状是「装上了、一调就被屏蔽」，开发树里两个根是同一个目录，看不到。

    做法是把那个根挂进 `skills.__path__`。挂上次之后
    `from skills.<名>.storage import ...` 这类包内导入也一起成立
    （`SKILL_SPEC.md` 里就是这么教人写的）。

    失败不抛：挂不上只意味着后面那条导入会照旧报它自己的错，
    在这里额外抛一个只会把原因埋掉。
    """
    try:
        root = str(Path(skill_dir).resolve().parent)
        pkg = importlib.import_module("skills")
        paths = getattr(pkg, "__path__", None)
        if paths is not None and root not in list(paths):
            paths.append(root)
    except Exception:                                          # noqa: BLE001
        pass


def validate_tool_schema(schema: dict) -> None:
    """
    Minimal JSON Schema 2020-12 validation for tool inputSchema.

    Checks:
      - Root type is 'object'
      - Properties are valid dicts with recognized types
      - Required fields exist in properties
      - $schema (if present) is a recognized URI

    Raises ValueError with details if the schema is invalid.
    """
    if not isinstance(schema, dict):
        raise ValueError("inputSchema must be a dict")

    # $schema is optional but recommended — validate if present
    schema_uri = schema.get("$schema")
    if schema_uri is not None:
        if schema_uri not in _VALID_SCHEMA_URIS:
            raise ValueError(
                f"Unrecognized $schema URI: '{schema_uri}'. "
                f"Expected one of: {_VALID_SCHEMA_URIS}"
            )

    if schema.get("type") != "object":
        raise ValueError(
            f"inputSchema root type must be 'object', got '{schema.get('type')}'"
        )

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("inputSchema 'properties' must be a dict")

    for prop_name, prop_def in properties.items():
        if not isinstance(prop_def, dict):
            raise ValueError(
                f"Property '{prop_name}' definition must be a dict, "
                f"got {type(prop_def).__name__}"
            )
        prop_type = prop_def.get("type")
        if prop_type not in _VALID_JSON_TYPES:
            raise ValueError(
                f"Property '{prop_name}' has invalid type '{prop_type}'. "
                f"Must be one of: {_VALID_JSON_TYPES}"
            )

    required = schema.get("required", [])
    if not isinstance(required, list):
        raise ValueError("'required' must be a list")

    for req_prop in required:
        if req_prop not in properties:
            raise ValueError(
                f"Required property '{req_prop}' is not defined in 'properties'"
            )


# ------------------------------------------------------------------
# SKILL.md helpers (agentskills.io standard)
# ------------------------------------------------------------------

def _parse_skill_md(skill_dir: Path, *, on_problem=None) -> dict | None:
    """
    Read and parse a SKILL.md file if it exists.

    Returns a dict with keys:
      - name: str (from YAML frontmatter)
      - description: str
      - body: str (Markdown body after frontmatter)
      - raw_metadata: dict (any extra frontmatter fields)

    Returns None if SKILL.md does not exist or cannot be parsed.

    *on_problem*：由调用方决定怎么出声。给了它，把问题交出去
    （`on_problem("<短标签>")`，调用方按根汇总），这里一个字都不打印；
    不给，逐条打印（今天的默认 —— 工具型 skill 的装载那条路要它）。
    标签：`no-frontmatter` / `yaml` / `not-dict` / `missing-name` /
    `missing-description` / `bad-name`。

    为什么要有这个口子：`discover_instruction_skills` 每次重建索引都扫一遍，
    而共享根里的 `SKILL.md` 是别人写的 —— YAML 未必合法（冒号没加引号、
    中文键名都见过）。逐条打全路径会把控制台刷爆，而"静默跳过"是反模式，
    所以汇总（判据与同文件里 `skipped` 那段逐字一致）。
    """
    skill_md_path = skill_dir / "SKILL.md"
    if not skill_md_path.exists():
        return None

    def _say(key: str, message: str) -> None:
        if on_problem is None:
            print(message)
        else:
            on_problem(key)

    text = skill_md_path.read_text(encoding="utf-8")

    # Parse YAML frontmatter (delimited by ---)
    # SKILL.md format:
    #   ---
    #   name: skill-name
    #   description: ...
    #   ---
    #   Markdown body...
    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if not frontmatter_match:
        _say("no-frontmatter",
             f"[bus] WARNING: {skill_md_path} has no valid YAML frontmatter")
        return None

    try:
        frontmatter = yaml.safe_load(frontmatter_match.group(1))
    except yaml.YAMLError as e:
        _say("yaml", f"[bus] WARNING: {skill_md_path} YAML parse error: {e}")
        return None

    if not isinstance(frontmatter, dict):
        _say("not-dict",
             f"[bus] WARNING: {skill_md_path} frontmatter is not a dict")
        return None

    name = frontmatter.get("name", "").strip()
    description = frontmatter.get("description", "").strip()
    body = frontmatter_match.group(2).strip()

    # Validate required fields (per agentskills.io spec)
    if not name:
        _say("missing-name",
             f"[bus] WARNING: {skill_md_path} missing required 'name' field")
    if not description:
        _say("missing-description",
             f"[bus] WARNING: {skill_md_path} missing required 'description' field")

    # Validate name format: lowercase, digits, hyphens only; 1-64 chars
    if name and not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", name):
        _say("bad-name",
             f"[bus] WARNING: SKILL.md name '{name}' does not match "
             f"agentskills.io spec (lowercase letters, digits, hyphens only, "
             f"no leading/trailing hyphens, no consecutive hyphens)")

    return {
        "name": name,
        "description": description,
        "body": body,
        "raw_metadata": {k: v for k, v in frontmatter.items()
                         if k not in ("name", "description")},
    }


# ------------------------------------------------------------------
# Lazy-loading stub
# ------------------------------------------------------------------

class _LazyStub:
    """
    Placeholder handler registered at startup for on_demand skills.

    On first invocation, imports the real handler module, initialises
    it, replaces itself in the registry, and delegates to the real
    handler.  Subsequent calls hit the real handler directly with
    zero overhead.
    """

    def __init__(
        self,
        skill_name: str,
        tool_name: str,
        skill_dir: Path,
        security_guard=None,
    ) -> None:
        self._skill_name = skill_name
        self._tool_name = tool_name
        self._skill_dir = skill_dir
        self._security_guard = security_guard
        self._real_handler = None
        self._loaded = False
        # 每会话一把锁之后，两个会话同时首调
        # 同一个 on_demand skill 是真的可能 —— 不加锁就会 `module.init()` 跑两遍
        # （注册表读改写也重叠）。双检 + 锁，一次且仅一次。
        self._lock = threading.Lock()

    def __call__(self, **kwargs):
        if not self._loaded:
            try:
                self._load()
            except Exception as exc:  # noqa: BLE001
                # **"只有真 import 才知道"的坏法
                #   在首次调用这一刻现形** —— 静态体检（`ast.parse`）抓不到
                #   「handler 里 import 了没装的包」/「`init()` 会抛」这一类。
                #   不捕获的话，异常直接冒进派发栈，变成一句
                #   `Tool execution error: ModuleNotFoundError: …` 让模型自己猜。
                return self._on_load_failure(exc)
        return self._real_handler(**kwargs)  # type: ignore[misc]

    def _on_load_failure(self, exc: BaseException) -> str:
        """首调失败，当场屏蔽这个插件 + 回一句诚实的拒绝（带路线）。

        判据：拒绝必须带路线，否则模型会换个名字接着试。
        这里不抛：抛出去只是一句 Python 异常，模型读不懂"这个插件已经废了"，
        而它下一轮还会再试一次（`_load` 每次都会重新 import，每次都失败）。
        """
        detail = "%s: %s" % (type(exc).__name__, exc)
        print(f"[quarantine] '{self._skill_name}.{self._tool_name}' 首次调用失败"
              f"（{detail}）—— 当场屏蔽该插件")
        try:
            from bus.quarantine import owner_for_skill, quarantine, blocked_message
            entry = quarantine(
                owner_for_skill(self._skill_name), code="lazy_load_failed",
                reason="首次调用时装载失败：%s" % detail,
                path=str(self._skill_dir), detail=detail)
            return blocked_message(self._tool_name, entry["owner"], entry.get("reason", ""))
        except Exception as inner:  # noqa: BLE001
            # 记账那一步自己炸了，也要回一句实话（不许把原始异常冒上去）
            return ("Error: tool '%s' failed to load (%s) and could not be blocked "
                    "(%s) — report this to the user and do not retry."
                    % (self._tool_name, detail, inner))

    def _load(self) -> None:
        """Import the real handler module, init, and self-replace in registry."""
        with self._lock:
            if self._loaded:
                return
            self._load_locked()

    def _load_locked(self) -> None:
        _make_importable(self._skill_dir)
        module_name = f"skills.{self._skill_name}.handler"
        module = importlib.import_module(module_name)

        if hasattr(module, "init") and self._security_guard is not None:
            module.init(self._security_guard)

        handler = getattr(module, self._tool_name, None)
        if handler is None:
            raise AttributeError(
                f"Lazy-loaded tool '{self._tool_name}' not found "
                f"in {module_name}.py"
            )

        self._real_handler = handler
        self._loaded = True

        # Atomically replace stub → real handler in the global registry
        from bus.registry import _registry
        _registry["tools"][self._tool_name] = handler
        print(
            f"[bus]   (lazy) {self._skill_name}.{self._tool_name} "
            f"resolved on first call"
        )


# ------------------------------------------------------------------
# Main loaders
# ------------------------------------------------------------------

def _note_output_kind(tool_name: str, tool_def: dict, skill_name: str) -> None:
    """把 manifest 里的 `output_kind` 登记进声明层。

    两条注册路（eager / lazy）各要调一次 —— 只改一条的话，`on_demand`
    的 skill（本仓 7 个包全是）一个都拿不到声明，而症状是
    "隔离姿态静默回到 fail-closed"（安全方向，所以不会有任何东西红）。

    没声明，出声，但不拒绝装载****（区别见下）：
      · load 这一层是"能用" —— 拒绝装载等于让用户的工具凭空消失，
        而那是看不见的（模型只会说"我没有这个工具"）。
      · `create_skill` 那一层是"能不能落地" —— 那里拒是对的
        （fail-closed，与它已有的两条不变量同一条纪律）。
      · 中间还有体检器（`core/skill_doctor.py`）会报。
三层的分工是：装载出声 · 体检报告 · 工厂拒绝。而默认走安全那一侧
      （`core/tool_output_kind.py::resolved_kind` 把 `None` 兜成 `prose`）。
    """
    from core.tool_output_kind import declare, is_read_family, declared_kind

    kind = tool_def.get("output_kind")
    declare(tool_name, kind)
    if kind is None and is_read_family(tool_name):
        print(
            f"[bus] WARNING: skill '{skill_name}' 的工具 '{tool_name}' 是读家族，"
            f"却没声明 output_kind —— 按 fail-closed 当 prose 处置"
            f"（多付约 20 tokens/次）。请在 manifest 里补 "
            f"`output_kind: prose|structured`（见 SKILL_SPEC.md）"
        )
    elif kind is not None and declared_kind(tool_name) is None:
        print(
            f"[bus] WARNING: skill '{skill_name}' 的工具 '{tool_name}' 的 "
            f"output_kind={kind!r} 不是合法值（只认 prose / structured）"
            f"—— 按 fail-closed 当 prose 处置"
        )


def _note_review_input(tool_name: str, tool_def: dict, skill_name: str,
                           skill_default=None) -> None:
    """把 manifest 里的 `review_input` 登记进声明层。

    两条注册路（eager / lazy）各要调一次 —— 同 `_note_output_kind` 那条注释。

    ## 两级：技能级默认 + 工具级覆盖

    ```yaml
    # manifest 头部（技能级）
    review_input: none
    tools:
      - name: delete_files
        review_input: "要删除的文件路径列表"   # 工具级覆盖
    ```
    判据是爆炸半径：`office_parser` 70 个非只读工具全都是"在工作区里
    造用户要的文档"，逐个抄 70 遍 `none` 不是做过 70 个决定，
    是把同一个决定抄 70 遍。
    而 `file_parser` 里 `delete_files` 与其余 17 件判据完全不同，单独覆盖。

    没声明 ≠ 声明了 `none`：前者走 fail-closed（过分类器），后者是
    一个做过的决定。两者的差别由读者保证（`tests/test_auto_review.py`）。
    """
    from core.auto_review import declare

    value = tool_def.get("review_input")
    if value is None:
        value = skill_default
    if value is None:
        return              # 没声明：不出声（读者会点名，见测试那条）
    if not declare(tool_name, value):
        print(
            f"[bus] WARNING: skill '{skill_name}' 的工具 '{tool_name}' 的 "
            f"review_input={value!r} 不是合法值（只认 `none` 或一段非空描述）"
            f"—— 按「未声明」处置（fail-closed，过分类器）"
        )


def _note_permission(tool_name: str, tool_def: dict, skill_name: str) -> None:
    """把 manifest 里的 `permission` 登记进声明层。

    两条注册路（eager / lazy）各要调一次 —— 只改一条的话，`on_demand`
    的 skill（本仓 7 个包全是）一个都拿不到声明，而症状是
    "档位静默回落到前缀猜"。

    与 `_note_output_kind` 两处刻意不同：

    ① 「没声明」不在这一层出声 —— 出声的规格是
       「未定档 _且_ 猜成 `read` 才逐条」（`bus/registry.py::_note_undeclared`），
       而那要等判定做完才知道；这里只看得到"声明"这一半。
       非法值在这里出声（它不会自己好）。
    ② **调用点紧挨着 `register_tool`，不在 `skip_existing` 早退之前。**
       理由：那两格必须同生同死。`skip_existing` 跳过的是注册；
       若只记声明不注册，`_declared` 说 `write`，而 `_registry["tool_perms"]`
       还是 `read`、description 上挂的也是 `[READ]` —— 同一个决定两套说法，
       而三处真闸读的是后者。
「reload 后改 permission，档位不动」本来就是对的：
       它属于 `reload_skills` 有意跳过重注册，不是这里能修的。
    """
    from core.tool_permission import declare, VALID_PERMS

    perm = tool_def.get("permission")
    if not declare(tool_name, perm) and perm is not None:
        print(
            f"[bus] WARNING: skill '{skill_name}' 的工具 '{tool_name}' 的 "
            f"permission={perm!r} 不是合法值（只认 {' / '.join(VALID_PERMS)}）"
            f"—— 按「未声明」处置（前缀猜 + 出声）"
        )


def _note_destructive(tool_name: str, tool_def: dict, skill_name: str) -> None:
    """把 manifest 里的 `destructive` 登记进声明层。

    两条注册路（eager / lazy）各要调一次 —— 与 `_note_permission` 同一条理由
    （只改一条的话，`on_demand` 的包 —— 本仓 7 个包全是 —— 一个都拿不到声明）。

    与 `_note_permission` 两处刻意不同：

    ① 非法值在这里出声，而且这条比 `permission` 那边要紧得多。
       `permission` 的非法值落回 `admin`（fail-closed，安全侧）；
       `destructive` 的非法值落回兜底的 `False`（fail-open）。
一个拼错的 `destructive: ture` 会让一个不可逆的工具静默变回「可逆」，
       而不会有任何东西红。所以下面那行 print 不是装饰。
    ② 「没声明」不在这一层出声 —— 出声的规格是
       `bus/registry.py::undeclared_destructive_report` 那一行汇总，
       故意不逐条：这一根"未声明"本身就是危险侧，逐条 = 129 行，
       而洪水会把真信号淹掉。这里只看得到"声明"这一半。

    调用点与 `_note_permission` 同一处（紧挨 `register_tool`、不在
    `skip_existing` 早退之前）—— 那几格必须同生同死，
    理由见 `_note_permission` 的 ②。
    """
    from core.tool_permission import declare_destructive

    value = tool_def.get("destructive")
    if not declare_destructive(tool_name, value) and value is not None:
        print(
            f"[bus] WARNING: skill '{skill_name}' 的工具 '{tool_name}' 的 "
            f"destructive={value!r} 不是布尔值 —— 按「未声明」处置。"
            f" ⚠️ 这一根**没有 fail-closed 兜底**：它会静默变回「可逆」"
        )


def _note_concurrency_safe(tool_name: str, tool_def: dict, skill_name: str) -> None:
    """把 manifest 里的 `concurrency_safe` 登记进声明层。

    两条注册路（eager / lazy）各要调一次 —— 同 `_note_permission` 那条理由。

    与 `_note_destructive` 的三点不同，都是判据不是笔误：

    ① 非法值落回"由权限档派生"，不是一个固定的 fail-open 值 ——
       而派生值就是权限档判据本身（`permission == "read"`），一个拼错的
       `concurrency_safe: ture` 让这个工具回到由权限档派生的行为，不是"静默放开"。
       但仍然出声：未声明与声明错了必须分得开。
    ② 「没声明」不在这一层出声、也没有汇总行 —— 这一根的默认值是派生的，
       "没人定过"就是 145 个里的绝大多数，数它没有意义
       （判据同 `resolve_concurrency_safe` 里那段"不返回 source"的话）。
    ③ 它不新增任何危险方向：兜底那侧是"不并发"（fail-closed），
       而并发不是安全边界（本来就只放只读；子 agent 那边另有两道闸）。

    调用点与 `_note_permission` 同一处（紧挨 `register_tool`、不在
    `skip_existing` 早退之前）—— 那几格必须同生同死。
    """
    from core.tool_permission import declare_concurrency

    value = tool_def.get("concurrency_safe")
    if not declare_concurrency(tool_name, value) and value is not None:
        print(
            f"[bus] WARNING: skill '{skill_name}' 的工具 '{tool_name}' 的 "
            f"concurrency_safe={value!r} 不是布尔值 —— 按「未声明」处置"
            f"（落回由权限档派生的默认值）"
        )


def load_skill(skill_dir: Path, security_guard=None) -> None:
    """
    1. Read <skill_dir>/manifest.yaml.
    2. Validate kind == "skill".
    3. For each tool: validate inputSchema + register.
    4. Resolve handler functions from handler.py.
    5. Optionally parse SKILL.md for metadata/ecosystem compatibility.
    """
    manifest_path = skill_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Skill manifest not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    if manifest.get("kind") != "skill":
        raise ValueError(
            f"Expected kind='skill' in {manifest_path}, got '{manifest.get('kind')}'"
        )

    manifest_name = manifest["name"]

    # --- Import the handler module ---
    _make_importable(skill_dir)
    module_name = f"skills.{skill_dir.name}.handler"
    module = importlib.import_module(module_name)

    # If the handler module has an init() function, call it with security guard
    if hasattr(module, "init") and security_guard is not None:
        module.init(security_guard)

    # --- Register tools from manifest ---
    tools_registered = 0
    for tool_def in manifest.get("tools", []):
        tool_name = tool_def["name"]
        tool_description = tool_def.get("description", "")
        input_schema = tool_def.get("inputSchema", {})

        # Validate schema before registering
        validate_tool_schema(input_schema)

        # Look up handler function (must match tool name exactly)
        handler = getattr(module, tool_name, None)
        if handler is None:
            raise AttributeError(
                f"Tool '{tool_name}' defined in {manifest_path} "
                f"but no function '{tool_name}' found in {module_name}.py"
            )

        combined_schema = {
            "description": tool_description,
            "inputSchema": input_schema,
        }

        _note_output_kind(tool_name, tool_def, skill_dir.name)
        _note_review_input(tool_name, tool_def, skill_dir.name,
                               manifest.get("review_input"))
        _note_permission(tool_name, tool_def, skill_dir.name)
        _note_destructive(tool_name, tool_def, skill_dir.name)
        _note_concurrency_safe(tool_name, tool_def, skill_dir.name)
        # 归属 = 目录名（技能的身份就是它 ——
        # `importlib.import_module(f"skills.{skill_dir.name}.handler")` 用的也是它）。
        # 返回值要判：`False` = 名字被别人占着，这个工具没登记。
        #    不判的话 `tools_registered` 会把它算进去，回执上写"装了 25 个"
        #    而实际只有 24 个 —— 一个偏大的数、看起来像结论。
        if register_tool(tool_name, handler, combined_schema,
                         owner=f"skill:{skill_dir.name}"):
            tools_registered += 1

    # --- Parse SKILL.md for ecosystem compatibility ---
    skill_md = _parse_skill_md(skill_dir)
    skill_md_name = None
    if skill_md and skill_md["name"]:
        skill_md_name = skill_md["name"]
        print(
            f"[bus] Skill loaded: {manifest_name} "
            f"(SKILL.md: {skill_md_name}, {tools_registered} tools)"
        )
    else:
        print(f"[bus] Skill loaded: {manifest_name} ({tools_registered} tools)")

    if skill_md and skill_md.get("raw_metadata", {}).get("mcp_note"):
        print(f"[bus]   MCP note: {skill_md['raw_metadata']['mcp_note'][:120]}...")

    return tools_registered


def load_skill_lazy(skill_dir: Path, security_guard=None, *,
                    skip_existing: bool = False) -> int:
    """
    Register tool stubs for an on-demand skill WITHOUT importing handler.py.

    This is the fast-path for skills marked load_strategy: on_demand.
    Only the manifest is read (cheap YAML).  Real handler modules are
    imported on first invocation via `_LazyStub`.

    *skip_existing*：注册表里已经有这个名字就跳过。

    为什么需要它 —— 一条降级：
    `/skill approve X` 走 `reload_skills`（整目录重扫），于是它把其它六个
    已经装好的 skill 也重新注册了一遍。而 `register_tool` 是就地替换，
    替换进去的是一个新的 `_LazyStub` —— 那个 skill 的 handler
    若已经解析过（真函数替代了桩），这一下就把它换回成了桩，下次调用白走
    一遍懒加载。用户看到的回执是「已当场装载：六个无关的 + 你要的那个」，
    而那六个不是"装载"，是"降级"。

`reload_skills` 用它把"只装新的"这件事做实（启动时注册表是空的，
      走默认 `False`，行为一个字节不变）。
    """
    manifest_path = skill_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Skill manifest not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    if manifest.get("kind") != "skill":
        raise ValueError(
            f"Expected kind='skill' in {manifest_path}, got '{manifest.get('kind')}'"
        )

    skill_name = skill_dir.name

    existing: set[str] = set()
    if skip_existing:
        from bus.registry import list_tools
        existing = set(list_tools())

    tools_registered = 0
    for tool_def in manifest.get("tools", []):
        tool_name = tool_def["name"]
        tool_description = tool_def.get("description", "")
        input_schema = tool_def.get("inputSchema", {})

        # `validate_tool_schema` 仍然要跑（即使要跳过）——
        # 它是这个 skill 的格式体检，与"装不装"是两件事。
        validate_tool_schema(input_schema)

        if skip_existing and tool_name in existing:
            continue

        # Register a stub that auto-imports on first call
        stub = _LazyStub(skill_name, tool_name, skill_dir, security_guard)
        combined_schema = {
            "description": tool_description,
            "inputSchema": input_schema,
        }
        _note_output_kind(tool_name, tool_def, skill_name)
        _note_review_input(tool_name, tool_def, skill_name,
                               manifest.get("review_input"))
        _note_permission(tool_name, tool_def, skill_name)
        _note_destructive(tool_name, tool_def, skill_name)
        _note_concurrency_safe(tool_name, tool_def, skill_name)
        # 归属 = 目录名；返回值要判（见 eager 那条的注释）。
        if register_tool(tool_name, stub, combined_schema,
                         owner=f"skill:{skill_name}"):
            tools_registered += 1

    # Parse SKILL.md for metadata
    skill_md = _parse_skill_md(skill_dir)
    if skill_md and skill_md["name"]:
        print(
            f"[bus] Skill stubbed: {manifest['name']} "
            f"(on_demand, {tools_registered} tools — "
            f"handler imported on first call)"
        )
    else:
        print(
            f"[bus] Skill stubbed: {manifest['name']} "
            f"(on_demand, {tools_registered} tools)"
        )

    return tools_registered


# ------------------------------------------------------------------
# 热加载 —— 不重启让新 skill 生效
# ------------------------------------------------------------------
#
# 它与 `core/skill_doctor`（体检）是两台机器、一条纪律：
#     · 体检是自动的（只读、不执行、不改运行态）
#     · 生效要人按一下（`/skill reload` 或 `/skill approve`）
#「没有一条路能让新代码自己走进来」。
#   模型造的那条路走 `draft` + approve，人放的那条走 reload —— 两条都要人按。
#
# 为什么不挂 watcher 自动装：信任边界其实一样（谁能写 `skills/`，
#    谁就拿到代码执行 —— 重启也会装），差别只在生效时机与谁在场。
#    而「谁在场」正是要保住的那点：自动生效意味着没有任何人按下过一次。

def reload_skills(skills_dir=("skills"), security_guard=None) -> dict:
    """重新扫一遍 *skills_dir*，把非草稿的按各自 `load_strategy` 装进来。

    幂等：`register_tool` 是就地替换，所以重复调用
    不会把工具表撑胖 —— 这是本函数敢被反复调的前提。
    草稿（`draft: true`）一个都不装 —— 判据是 `if manifest.get("draft"): continue`
    （`scripts/measure_request.py:90` 逐字同一条）。
    单个包炸了不影响其余的：记进 `failed` 继续下一个。

    返回 `{"loaded": [...], "skipped_draft": [...], "failed": [...]}`。
    """
    base = Path(skills_dir)
    # `loaded` 只列"真的装进去了东西"的；本来就在的进 `already`。
    # 混在一起的话，`/skill approve X` 会回一句「已当场装载：七个」
    # —— 而其中六个不是装载，是降级（见 `load_skill_lazy` 的 `skip_existing`）。
    out = {"loaded": [], "skipped_draft": [], "failed": [], "already": [],
           # 只有 `SKILL.md` 的目录不再静默。
           #   一个裸 `continue` 会让那种目录连 `failed` 都不进，
           #   于是用户看到的是"什么都没发生"。
           #   它进的是另一个桶，不是 `failed`：那不是失败，
           #   是"它属于另一层"（skill 层，没有工具）。混进 `failed` 会让
           #   `/skill` 的回执说"装载失败"，而它根本没打算被装载成工具。
           "instructions": []}
    if not base.is_dir():
        return out

    # 指令型 skill 由 `bus/loaders/skill_md.py` 一个纯函数扫出来
    #   （判据只有一处，别在这里再写一遍"有没有 manifest.yaml"）。
    from bus.loaders.skill_md import (
        discover_instruction_skills, invalidate_command_map,
    )
    # 这里是命令缓存唯一的失效点。
    #   判据是那条纪律 —— 「体检是自动的；生效要人按一下」：
    #   缓存跟着那一下走，所以"用户放一个 skill 进去就能用"只在按下之后成立
    #   （那正是本仓对"新代码什么时候开始跑"的一贯口径）。
    invalidate_command_map()
    from core.skill_sources import skill_roots
    out["instructions"] = [s.name for s in discover_instruction_skills(skill_roots(base))]

    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith((".", "_", "$")):
            continue
        manifest_path = entry / "manifest.yaml"
        if not manifest_path.is_file():
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = yaml.safe_load(f) or {}
        except Exception as exc:  # noqa: BLE001
            out["failed"].append((entry.name, "%s: %s" % (type(exc).__name__, exc)))
            continue
        if manifest.get("draft"):
            out["skipped_draft"].append(entry.name)
            continue
        # 能力/版本闸—— 与启动那条同一个函数、同一条判据。
        #  热加载这条路同样要拦：`/skill approve` 之后正是走这里，
        #  而"用户自己装的包"恰恰是最可能要求新框架的那一类。
        try:
            from core.capabilities import gate as _cap_gate
            _why = _cap_gate(manifest)
        except Exception as e:                                  # noqa: BLE001
            _why = ""
            print(f"[bus] WARNING: 能力闸出问题（{type(e).__name__}: {e}）"
                  f" —— '{entry.name}' 照常装载")
        if _why:
            print(f"[bus]   (skip) {entry.name} —— {_why}")
            out.setdefault("skipped_capability", []).append(entry.name)
            continue
        # `optional` 缺能力 ≠ 拦下来：技能自己写好了降级路，这里只记账 + 出声。
        try:
            from core.capabilities import optional_missing as _opt_missing
            _reduced = _opt_missing(manifest)
        except Exception:                                       # noqa: BLE001
            _reduced = []
        if _reduced:
            print(f"[bus]   (degraded) {entry.name} —— 能力缺失："
                  f"{'、'.join(_reduced)}（当前为兼容状态运行）")
            out.setdefault("degraded_capability", []).append(entry.name)
        # 热加载这条路上也有一道同样的闸。
        #  只做启动那一处是不够的 —— `/skill approve` 之后正是走这条路，
        #  而"用户自己装的包"恰恰是最该拦的那一类。
        try:
            from bus.quarantine import gate_skill_dir
            if gate_skill_dir(entry) is not None:
                out.setdefault("quarantined", []).append(entry.name)
                continue
        except Exception as e:                                  # noqa: BLE001
            print(f"[bus] WARNING: 屏蔽闸出问题（{type(e).__name__}: {e}）"
                  f" —— '{entry.name}' 照常装载")
        try:
            if manifest.get("load_strategy", "eager") == "on_demand":
                n = load_skill_lazy(entry, security_guard, skip_existing=True)
                (out["loaded"] if n else out["already"]).append(entry.name)
            else:
                # eager 那条路不跳过：它 import handler，跳过等于不 import。
                # 生产里 eager 只在启动时走（那时注册表是空的），
                # 但 `reload` 也能碰到它 —— 如实记进 `loaded`。
                load_skill(entry, security_guard)
                out["loaded"].append(entry.name)
        except Exception as exc:  # noqa: BLE001
            out["failed"].append((entry.name, "%s: %s" % (type(exc).__name__, exc)))
    return out
