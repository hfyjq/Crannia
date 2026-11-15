"""
Startup scanner: discovers and loads all modules (skills, providers, channels).

No hot-reload — this runs once at startup.
"""

import os
import re
from pathlib import Path

import yaml

from bus.loaders.skill_loader import load_skill
from bus.loaders.provider_loader import load_provider
from bus.loaders.channel_loader import load_channel
from core.security import SecurityGuard


def _register_provider_metadata(provider_dir: Path) -> None:
    """Read manifest and register metadata WITHOUT instantiating the provider."""
    with open(provider_dir / "manifest.yaml", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    cfg = manifest.get("config", {})
    from bus.registry import register_provider_metadata
    register_provider_metadata(manifest["name"], {
        "name": manifest["name"],
        "api_mode": manifest.get("api_mode", "openai_compat"),
        "description": manifest.get("description", ""),
        "version": manifest.get("version", "0.1.0"),
        "default_model": cfg.get("default_model", ""),
        "base_url": cfg.get("base_url", ""),
        "context_window": cfg.get("context_window", 65536),
        "supports_function_calling": cfg.get("supports_function_calling", True),
        "supports_streaming": cfg.get("supports_streaming", True),
        "website": cfg.get("website", ""),
        "creator": cfg.get("creator", ""),
        "loaded": False,
    })

# Pattern for ${VAR_NAME} in config string values
_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


_env_warned: set[str] = set()  # 每个环境变量只警告一次

def _resolve_env_vars(value):
    """Recursively resolve ${VAR_NAME} placeholders in a config value."""
    if isinstance(value, str):
        def _replace(match):
            var_name = match.group(1)
            env_val = os.environ.get(var_name)
            if env_val is None:
                if var_name not in _env_warned:
                    _env_warned.add(var_name)
                    print(f"[bus] WARNING: env var '{var_name}' is not set — "
                          f"using empty value")
                return ""
            return env_val
        return _ENV_VAR_PATTERN.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def load_config(config_path: str | Path) -> dict:
    """
    Read config.yaml, resolve ${VAR_NAME} environment variable references.

    If the config file is missing, falls back to an embedded minimal
    default so the framework can still start.  A warning is printed so
    the user knows to restore their config.

    Returns the fully-resolved config dict.
    """
    path = Path(config_path)
    if not path.exists():
        print(f"[bus] WARNING: Config file not found: {path}")
        print("[bus] Using embedded default config — some features"
              " may be unavailable.")
        return _resolve_env_vars(_DEFAULT_CONFIG)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return _resolve_env_vars(raw)


# ------------------------------------------------------------------
# 内置兜底配置 —— 最小可运行默认值。
# ------------------------------------------------------------------

_DEFAULT_CONFIG: dict = {
    "model": {
        "provider": "deepseek",
        "model_name": "deepseek-v4-flash",
        "api_key": "${DEEPSEEK_API_KEY}",
        "base_url": "https://api.deepseek.com/v1",
        "temperature": 0.3,
        "max_tokens": 4096,
        "context_window": 65536,
        "request_timeout": 120,
        # 默认不限流（详见 config.yaml 的注释）
        "rate_limit": {"rpm": 0, "tpm": 0},
    },
    "security": {
        "allowed_directories": ["./workspace"],
        "command_blacklist": ["sudo", "rm -rf", "del /f", "format"],
        "audit_log_path": "./logs/audit.log",
    },
    "chunking": {"max_chars": 1500},
    "retrieval": {"top_k": 3},
    "summarization": {
        "chunk_summary_max_chars": 200,
        "merge_batch_size": 3,
    },
    "tool_result_max_chars": 50_000,   # 与 core/tiers.py 的常量同值；真源是档表
    "memory": {
        "enabled": True,
        "storage_path": "./data/memory",
        # max_messages（死键）/ long_term / semantic 三个键已删 ——
        # 对应模块一并删除，留着只会让人以为它们还有人读。
        "short_term": {"max_history": 100},
        "compression": {"token_threshold": 0.85, "keep_recent_turns": 3},
        "session_memory": {"sessions_dir": "./data/memory/sessions"},
    },
    "channel": {
        "default": "cli_channel",
    },
}


# 启动耗时统计 —— /api/health 读它。
_startup_stats: dict[str, float] = {}


def _tool_skill_entries(primary: Path) -> list[Path]:
    """工具型技能的候选目录：随包的只读资产 + 用户自己装的。

    仓内排前 —— 同名时自带的那一个赢（用户装的顶不掉随包发布的）。
    开发树里两个根是同一个目录，第二个会被真实路径去重掉；
    安装版里才真正分开（仓内在安装目录、用户的在数据根）。
    """
    from core import paths as _p
    out: list[Path] = []
    seen: set[str] = set()       # 真实路径（开发树里两个根是同一目录）
    named: set[str] = set()      # 技能名（安装版里两个根才是两个目录）
    for root in (primary, _p.user_skills_dir()):
        try:
            if not root.is_dir():
                continue
            for e in sorted(root.iterdir()):
                key = str(e.resolve())
                if key in seen:
                    continue
                seen.add(key)
                # 同名以先出现的为准（随包优先），后面那个不试着装载。
                # 装进去只会拿到随包那份的 handler：`skills.<名>` 按包路径解析，
                # 而包路径里排在前面的正是随包目录 —— 症状是"同名工具用了内置的
                # 实现，用户独有的工具在装载或首次调用时 AttributeError"。
                if e.name in named:
                    print("[bus] 跳过用户技能 %r：与随包技能同名（随包优先）"
                          % e.name)
                    continue
                named.add(e.name)
                out.append(e)
        except OSError:
            continue
    return out


def get_startup_stats() -> dict:
    """Return startup timing stats (seconds per phase, tool count)."""
    from bus.registry import list_tools
    return {
        **_startup_stats,
        "tool_count": len(list_tools()) if list_tools() else 0,
    }


def init_bus(config: dict, **channel_deps) -> SecurityGuard:
    """
    Initialize the module bus:
    1. Create SecurityGuard (v2) from config.
    2. Create WorkspaceManager and wire cross-zone notification.
    3. Load all providers from providers/.
    4. Load all skills from skills/.
    5. Load MCP built-in servers + external connections.
    6. Load all channels from channels/.

    Extra keyword arguments are forwarded to Channel constructors
    (e.g. `scheduler=…`, `session_pool=…`).

    Returns the SecurityGuard instance (needed by skills).
    The WorkspaceManager is accessible via bus.registry.get_workspace_manager().

    Logs each successful load; skips and warns on failures so a
    single broken skill does not crash the framework.
    """
    import time as _time
    import json as _json
    global _startup_stats
    _startup_stats = {}

    # 先把屏蔽账本装进闸门 —— 必须早于任何装载。
    #   否则"上次启动屏蔽过的包"这一次会先被装进注册表（然后才轮到闸生效），
    #   而中途那一段时间里模型已经看得见、也调得动它了。
    #   读不进来，不屏蔽任何插件（这一格是"屏蔽"名单，方向是宁少不多，
    #   同 `retention.py` 那条"删的动作，真值是不动"）。
    try:
        from bus.quarantine import load as _load_quarantine
        _q = _load_quarantine()
        if _q:
            print(f"[bus] 屏蔽账本：{len(_q)} 个插件仍在屏蔽中"
                  f"（{'、'.join(sorted(_q))}）")
        #: 紧接着重判一次（只判静态那几条）：包被修好之后就应当能回来 ——
        #:   账本是持久的，而"修好也没救"是这条机制最容易长出来的死结。
        from bus.quarantine import review_static_quarantines as _review
        _review(skills_dir="skills")
    except Exception as _e:                                         # noqa: BLE001
        print(f"[bus] WARNING: 屏蔽账本读不进来（{type(_e).__name__}: {_e}）"
              f" —— 本次不屏蔽任何插件")

    # 用户停用的 MCP 也在这里进闸门 —— 与上面那段同一处。
    #   它与屏蔽是两个集合、一个判定点（`registry.get_active_tool_names` 那一行并集）。
    #   读不进来，不停用任何一个（同方向：宁可多放一个工具出来）。
    try:
        from bus.mcp_disabled import load as _load_mcp_disabled
        _d = _load_mcp_disabled()
        if _d:
            print(f"[bus] MCP 停用账本：{len(_d)} 个服务器已停用"
                  f"（{'、'.join(sorted(_d))}）")
    except Exception as _e:                                         # noqa: BLE001
        print(f"[bus] WARNING: MCP 停用账本读不进来（{type(_e).__name__}: {_e}）"
              f" —— 本次不停用任何一个")

    # 模型的「配置条目」账本 —— 这是它唯一一次带播种的装载。
    #   理由见 `core/model_configs.load()` 的 docstring（一句话：播种挂在"读"上会
    #   变成动作顺序依赖，而 `config.yaml` 一时读不动会永久写坏迁移）。
    #   必须在这里（而不是某个端点里）：此处跑在
    #   `load_dotenv(override=True)` 之后，老配置里那些
    #   `${DEEPSEEK_API_KEY}` 才解得开。
    #   读不进来，按空账本走（下游 fail-open 退回 `config.yaml` 的 `model:`）。
    try:
        from core import model_configs as _model_configs
        _model_configs.load(seed=True)
    except Exception as _e:                                         # noqa: BLE001
        print(f"[bus] WARNING: 模型配置账本读不进来（{type(_e).__name__}: {_e}）"
              f" —— 本次沿用 config.yaml 的 model:")
    t0 = _time.time()

    # -- Load settings for workspace config --
    settings: dict = {}
    settings_path = Path("data/settings.json")
    if settings_path.exists():
        try:
            settings = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # -- Security guard (v2) --
    sec_cfg = config.get("security", {})
    cross_zone_cfg = sec_cfg.get("cross_zone", {})
    security = SecurityGuard(
        allowed_dirs=sec_cfg.get("allowed_directories", ["./workspace"]),
        command_blacklist=sec_cfg.get("command_blacklist", []),
        audit_log_path=sec_cfg.get("audit_log_path", "./logs/audit.log"),
        cross_zone_enabled=settings.get("cross_zone_enabled",
                      cross_zone_cfg.get("enabled", True)),
        auto_approve=settings.get("auto_approve",
                    cross_zone_cfg.get("auto_approve", False)),
        forbidden_dirs=settings.get("forbidden_directories",
                       cross_zone_cfg.get("forbidden_directories", [])),
        # 设置页那个「关闭安全询问」开关的启动值。
        # 默认 `False`（= 照常问）—— 缺配置、配置坏了、键名写错，
        # 都要朝"问"那边倒。
        relax_command_tier=settings.get("relax_command_tier", False),
        # 设置页「只读命令免问」的启动值。
        # 与上面那条同一个形状、同一个默认方向（缺配置 / 坏了 / 键名写错
        #一律朝"问"倒）。它放宽的是询问，属于安全轴，默认 False。
        no_ask_readonly=settings.get("no_ask_readonly", False),
    )
    print("[bus] SecurityGuard v2 initialized")

    # -- search_tools --
    # 框架级工具，不属于任何 skill，所以在总线初始化时直接注册。
    # 模型用它按需发现其它工具。
    from bus.registry import register_search_tool
    register_search_tool()
    print("[bus] search_tools registered (model-driven tool discovery)")

    # -- run_command --
    # 同为框架级工具。判定在 `core/command_policy.py`、执行在 `core/shell.py`，
    # 这里只接线。它不是一个可以悄悄没接上的可选件 ——
    # 少一根线等于这条能力不存在。
    from bus.registry import (register_command_tool, register_group_tool,
                              register_subagent_tool, register_workflow_tool)
    register_command_tool()
    register_subagent_tool()
    # 工作流编排 —— 只在 ultra 档进 `tools=`
    #   （判据在 `_session_extra_tools`：其余档位不为它付税）。
    register_workflow_tool()
    # 群成员派活。
    # 注册 ≠ 每次都发 —— 只有群会话的 `tools=` 里才有它
    #   （判据在 `bus/registry._session_extra_tools`：普通对话不该为它付税）。
    register_group_tool()
    from core.shell import bash_path as _bash_path
    print(f"[bus] run_command registered (bash: {_bash_path() or '未找到'})")

    # -- todo_write --
    # 同为框架级工具。模型侧的计划载体：多步任务里「做到哪一步」
    # 不再全靠上下文自己拼。
    from bus.registry import register_task_tool, register_todo_tool
    register_todo_tool()
    register_task_tool()          # 查/等/取消后台任务（按需发现，不常驻）
    print("[bus] todo_write registered (model-facing task list)")

    # -- computer --
    # 接管：截图 / 无障碍树 / 点击 / 输入。注册 ≠ 每次发 —— 只有
    # 持有接管的那个会话的 `tools=` 里才有它（`_session_extra_tools`）。
    from bus.registry import register_computer_tool, register_browser_tool
    register_computer_tool()
    register_browser_tool()
    print("[bus] computer + browser registered (takeover, opt-in per session)")

    # -- 文件原语五件套 --
    # 同为框架级工具。这是编程的底座：否则碰代码的唯一通路是 `run_command`
    # （每次过审批 · 输出 4000 字符封顶 · 没有"改前先读" · 没有只读探索档）。
    # 五件全部常驻（见 `_CORE_TOOLS`）：不常驻的话模型会继续走 run_command。
    # `read_file` / `grep` 的回执是纯文本，不是 JSON —— 循环里 `as_text()`
    # 认那个名单（JSON 信封被字符截断就是半截 JSON）。
    from bus.registry import register_file_tools
    register_file_tools()
    print("[bus] file tools registered (read_file/write_file/edit_file/glob/grep)")

    # -- 代码符号查询（按配置注册）--
    # 没配语言服务器时不注册：这个工具的 schema 一个字节都不发（每请求零成本）。
    # 声明文件是项目根的 `lsp.json`，没有就用 `data/lsp.json`。
    from bus.registry import register_code_intel_tool
    register_code_intel_tool()
    # 工具编排（子进程 + 桥）：没有配置也可以注册，按需发现。
    from bus.registry import register_program_tool
    register_program_tool()

    # -- 计划模式两件 --
    # 两个一起接：它们是一对（判据：只给入口会让计划模式变成
    # 模型进得去、出不来的陷阱）。
    # 判定、状态与话术都在 `core/plan.py`，这里只是接线 —— 但"接线"这一步
    # 是承重的：写好了不接 = 模型手里没有这两个工具。
    from bus.registry import register_plan_tools
    register_plan_tools()
    print("[bus] plan mode tools registered (enter_plan_mode/exit_plan_mode)")

    # -- 周期任务三件 --
    # 三件一起接，且三件都常驻（见 `_CORE_TOOLS`）：用户会用大白话提
    # （"每天早上八点半给我总结新闻"）—— 那句话里没有一个词能拿去检索，
    # 所以"先 search_tools 再发现"这条路对它们不成立。
    # `cron_create` 是 `destructive=True`，兜底闸会在派发前问用户一次。
    # 它不在 `_APPROVAL_CAPABLE_TOOLS` 里 ——
    # 那个集合是"自己有发射点、闸别再问"，方向相反。
    from bus.registry import register_cron_tools
    register_cron_tools()
    print("[bus] cron tools registered (cron_create/cron_list/cron_delete)")

    # -- ask_user --
    # 框架级：模型主动向用户出一个结构化选择题。
    # 挂起/结算/回填走审批那套地基，但语义不同（问意见 ≠ 求授权）——
    #    见 `core/ask_user.py` 的头。
    from bus.registry import register_ask_user_tool
    register_ask_user_tool()
    print("[bus] ask_user registered (structured question to the user)")

    # -- create_skill --
    # 给模型的 meta-skill。它只产草稿（强制 `draft: true`），
    # 人 `/skill check` + `/skill approve` 之后才生效 —— 见 `core/skill_factory.py`。
    from bus.registry import register_skill_factory_tool
    register_skill_factory_tool()
    print("[bus] create_skill registered (draft-only skill authoring)")

    # -- 记忆三件套 --
    # 长期记忆的专用读写通道：
    # `memory_read` / `memory_search`（读，纯文件扫描）+ `memory_write`（写，挂 tombstone）。
    # 三件全部常驻；读件的回执是纯文本。
    from bus.registry import register_memory_tools
    register_memory_tools()
    print("[bus] memory tools registered (memory_read/memory_search/memory_write)")

    # -- 查历史 --
    # 只读 short_term.db 的单件工具（独立、零风险）。
    # 它要的 MemoryManager 由 `web/server.py` 与 `main.py` 登记
    # （`register_memory_manager`）—— 拿不到时 handler 如实报"没接线"。
    from bus.registry import register_history_tool
    register_history_tool()

    # 压缩请求口（只在压力窗口里发给模型）
    from bus.registry import register_compaction_tool
    register_compaction_tool()

    # -- ICE：放行句柄背后的真值 --
    # 它声明了 `destructive=True`（真值进上下文不可撤回），而它不在
    # `_APPROVAL_CAPABLE_TOOLS` 里，兜底闸也会问它一次。
    #两条路都会弹：兜底闸先问（派发前），用户同意之后进了 handler，
    # handler 自己再发一次载荷。这一处尚未验证 ——
    # 第一次实跑要盯的是"是不是弹了两次"。
    from bus.registry import register_ice_reveal_tool
    register_ice_reveal_tool()
    print("[bus] pii_reveal registered (ICE 放行真值)")
    print("[bus] history_search registered (read-only short_term.db)")

    # -- Workspace Manager --
    from core.workspace import WorkspaceManager
    workspace_mgr = WorkspaceManager(security, settings)
    if workspace_mgr.workspaces:
        workspace_mgr.build_index()
        print(f"[bus] WorkspaceManager: {len(workspace_mgr.workspaces)} workspace(s), "
              f"index={workspace_mgr.index_stats['file_count']} files")

    # Register for API access
    from bus.registry import register_workspace_manager
    register_workspace_manager(workspace_mgr)

    # Auto-start watchers
    for wc in workspace_mgr.watchers:
        if wc.enabled and wc.auto_start:
            ok = workspace_mgr.start_watcher(wc.id)
            status = "started" if ok else "FAILED"
            print(f"[bus] Watcher '{wc.id}' ({wc.watch_path}): {status}")
            # 起不来时那条事故由 `WorkspaceManager.start_watcher` 自己记 ——
            #   三条起法（自启动 / 开关 / 这里）都要记，写在三个地方就是三套口径。

    # -- Providers (v1.0.0: lazy — only instantiate the active provider) --
    # `providers/` / `channels/` 是代码，不是用户数据，按安装根找、不按 cwd 找。
    #   安装版里 `paths.prepare()` 会把 cwd 挪到数据根（`%APPDATA%\Crannia`），
    #   那里没有这两个目录，相对路径在装出来的包里必然落空，而开发树全绿
    #   （症状：`Agent 初始化失败：[WinError 3] … : 'providers'`）。
    #   与 `skills` 同一口径（`_paths.asset_dir`），见 `core/paths.py` 的表。
    from core import paths as _paths
    active_provider = config.get("model", {}).get("provider", "deepseek")
    providers_dir = _paths.asset_dir("providers")
    if providers_dir.is_dir():
        for entry in sorted(providers_dir.iterdir()):
            if entry.is_dir() and (entry / "manifest.yaml").exists():
                # Only instantiate the active provider; register metadata for UI
                if entry.name == active_provider:
                    try:
                        load_provider(entry, config)
                    except Exception as e:
                        print(f"[bus] WARNING: Failed to load provider '{entry.name}': {e}")
                else:
                    # Register metadata only (for Web UI catalog) — no instantiation
                    try:
                        _register_provider_metadata(entry)
                    except Exception as e:
                        # 这里不许静默：供应商目录里少一项，界面上
                        # 看不出少了什么（一个偏小的数看起来完全像一个结论）。
                        print(f"[bus] WARNING: Failed to register provider metadata "
                              f"'{entry.name}': {type(e).__name__}: {e}")

    # MCP 排在技能之前 —— 顺序承重，别挪回去。
    #   内置 MCP 那两个工具的归属是 `mcp:web_search`，而 `register_tool` 那条
    #   「外来登记不许顶掉」的放行条件是 `owner == FRAMEWORK_OWNER` ——
    #   归属不是 framework 时，先注册的赢。
    #   技能在前的话，一个叫 `web_search` 的包会先把名字占了，
    #   内置 MCP 于是静默注册不上，模型调到的是那个包的实现。
    #   外部 MCP 服务器（owner `mcp:<名>`）同理：也会输给同名技能 —— 一并堵上。
    # -- MCP (v1.0.0) --
    try:
        from mcpx.manager import init_mcp
        mcp_tools = init_mcp(config)
        _startup_stats["mcp_tool_count"] = mcp_tools
    except Exception as e:
        print(f"[bus] WARNING: Failed to init MCP: {e}")

    # -- Skills --
    from core import paths as _paths
    from core import skill_load_report as _report
    _report.clear()
    _loaded_names: list[str] = []
    skills_dir = _paths.asset_dir("skills")
    if skills_dir.is_dir():
        for entry in _tool_skill_entries(skills_dir):
            if entry.is_dir() and (entry / "manifest.yaml").exists():
                # Read manifest for draft check + load_strategy
                try:
                    with open(entry / "manifest.yaml", "r", encoding="utf-8") as mf:
                        manifest = yaml.safe_load(mf)
                except Exception as e:
                    # 这是一条会静默吃掉一整个 skill 的路：manifest 读不动
                    #那个 skill 的全部工具凭空消失，而模型只会说
                    # "我没有这个工具"（拒绝装载 = 用户的工具消失，而那是看不见的）。
                    #装载可以继续，但必须出声。
                    print(f"[bus] WARNING: Failed to read manifest of "
                          f"'{entry.name}': {type(e).__name__}: {e} —— "
                          f"该 skill 的所有工具本次**不会注册**")
                    _report.note(entry.name, "discover", False,
                                 "manifest 读不出来：%s: %s"
                                 % (type(e).__name__, e))
                    continue

                if manifest.get("draft"):
                    print(f"[bus]   (draft) {entry.name} — use /skill check to review")
                    continue

                # 能力/版本闸：skill 要的框架能力没有、
                #  或者它的 `api` 比本框架新，不装载，并说出缺什么。
                #  判据：不半残。装了但一半坏，用户只会得到一堆
                #    "这工具怎么不行"，而没有人告诉他"你的 Crannia 太旧"。
                try:
                    from core.capabilities import gate as _cap_gate
                    _why = _cap_gate(manifest)
                except Exception as e:                          # noqa: BLE001
                    # 这道闸自己不许拦路（同屏蔽闸那条判据）
                    _why = ""
                    print(f"[bus] WARNING: 能力闸出问题（{type(e).__name__}: {e}）"
                          f" —— '{entry.name}' 照常装载")
                if _why:
                    print(f"[bus]   (skip) {entry.name} —— {_why}")
                    _report.note(entry.name, "compatibility", False, _why)
                    continue
                # `optional` 缺能力不拦：技能自带降级路，这里只记一行（卡片/诊断看得到）。
                try:
                    from core.capabilities import optional_missing as _opt_missing
                    _reduced = _opt_missing(manifest)
                except Exception:                                # noqa: BLE001
                    _reduced = []
                if _reduced:
                    _note = "能力缺失：" + "、".join(_reduced) + "（当前为兼容状态运行）"
                    print(f"[bus]   (degraded) {entry.name} —— {_note}")
                    _report.note(entry.name, "compatibility", True, _note)

                # 装载前的一道闸。
                #  判据是"`skill_doctor` 能发现坏包，但只 print 一行" ——
                #  检测 ≠ 阻止。命中 `QUARANTINE_CODES` 的包本次不装，
                #  并记进账本（可逆，用户的文件一个字节不动）。
                #  它必须排在 `draft` 之后：draft 的包本来就不装，
                #  对它体检然后把"屏蔽"记进账本 = 记一件没发生的事。
                try:
                    from bus.quarantine import gate_skill_dir
                    if gate_skill_dir(entry) is not None:
                        continue
                except Exception as e:                          # noqa: BLE001
                    # 这道闸自己不许拦路 —— 它出问题照旧装（同体检器那条判据）
                    print(f"[bus] WARNING: 屏蔽闸出问题（{type(e).__name__}: {e}）"
                          f" —— '{entry.name}' 照常装载")

                strategy = manifest.get("load_strategy", "eager")
                try:
                    if strategy == "on_demand":
                        from bus.loaders.skill_loader import load_skill_lazy
                        load_skill_lazy(entry, security_guard=security)
                    else:
                        load_skill(entry, security_guard=security)
                except Exception as e:
                    print(f"[bus] WARNING: Failed to load skill '{entry.name}': {e}")
                    _report.note(entry.name, "import", False,
                                 "%s: %s" % (type(e).__name__, e))
                else:
                    _loaded_names.append(entry.name)
                    _report.note(entry.name, "discover", True)

        # 只有 `SKILL.md` 的目录不再静默。
        #   上面那个循环会跳过它们（没有 `manifest.yaml`），于是"用户往 `skills/`
        #   放了一个 skill"看起来像"什么都没发生"。
        #这里把它们点出来 —— 装载是另一条路的事，看见是这一条的事。
        #   判据是"有没有 `manifest.yaml`"，不是"像不像 skill"。
        from bus.loaders.skill_md import discover_instruction_skills, report_line
        from core.skill_sources import skill_roots
        #: 一根 → 一串根。`skill_roots` 只在 `skills_dir`
        #:   就是仓内那一个时才带上共享根（见那个函数的判据）。
        instruction_skills = discover_instruction_skills(skill_roots(skills_dir))
        if instruction_skills:
            print(f"[bus] {report_line(instruction_skills)}")
        _startup_stats["instruction_skill_count"] = len(instruction_skills)

        # 把设置里标为停用的技能同步到注册表（不摘注册表，只进集合差）。
        try:
            from bus import skill_state
            _off = skill_state.refresh()
            if _off:
                print("[bus] 已停用的技能：%s" % ", ".join(sorted(_off)))
        except Exception as exc:                                # noqa: BLE001
            print("[bus] WARNING: 读取技能启用状态失败（%s: %s）"
                  % (type(exc).__name__, exc))

    # 技能的后台服务：只有声明了 start(ctx) 的技能会被导入并启动。
    try:
        from core import services as _services
        _svc = _services.start_for(_loaded_names)
        for _name in _svc.get("started", ()):
            _report.note(_name, "start", True)
        for _name, _err in _svc.get("failed", ()):
            _report.note(_name, "start", False, _err)
    except Exception as exc:                                    # noqa: BLE001
        print("[service] 启动技能服务失败（不影响启动）：%s: %s"
              % (type(exc).__name__, exc))

    # 出站工具排在技能之后：描述里要列出技能注册的动作名。
    try:
        from bus.registry import register_outbound_tool
        register_outbound_tool()
    except Exception as exc:                                    # noqa: BLE001
        print("[bus] WARNING: 注册出站工具失败（%s: %s）"
              % (type(exc).__name__, exc))

    # 把"真的坏了"的那两类同步进事故账本（`#02` 的数据源）。
    #   `compatibility` / `discover` 不进 —— 那是"配不上"，技能页已经写了原因、
    #   体检器也管得着。把它们也弹出来，等于每次启动都喊一遍用户改不了的话。
    try:
        from core import incidents as _inc
        for _name, _item in _report.all().items():
            if _item.get("ok"):
                continue
            _stage = _item.get("stage")
            if _stage == "import":
                _inc.note(_name, "skill_import", "技能模块导入失败",
                          _item.get("message", ""))
            elif _stage == "start":
                _inc.note(_name, "skill_start", "技能的后台服务启动失败",
                          _item.get("message", ""))
    except Exception as exc:                                    # noqa: BLE001
        print("[incidents] 同步技能装载结果失败（%s: %s）"
              % (type(exc).__name__, exc))

    # -- Channels --
    channels_dir = _paths.asset_dir("channels")
    if channels_dir.is_dir():
        for entry in sorted(channels_dir.iterdir()):
            if entry.is_dir() and (entry / "manifest.yaml").exists():
                try:
                    load_channel(entry, config=config, **channel_deps)
                except Exception as e:
                    print(f"[bus] WARNING: Failed to load channel '{entry.name}': {e}")

    _startup_stats["total_seconds"] = round(_time.time() - t0, 2)
    # 这里不许裸 `iterdir()`：目录找不到时抛出来的那句话
    #   （`[WinError 3] … : 'providers'`）会被上层显示成「Agent 初始化失败」，
    #   看起来像配置坏了，而真因是"按 cwd 找代码资产"。复用上面那个目录，
    #   找不到就出声（一个偏小的数看起来完全像一个结论）。
    if providers_dir.is_dir():
        _startup_stats["provider_count"] = sum(
            1 for p in providers_dir.iterdir()
            if p.is_dir() and (p / "manifest.yaml").exists())
    else:
        _startup_stats["provider_count"] = 0
        print(f"[bus] WARNING: 供应商目录不存在：{providers_dir} "
              f"—— 本次一个 provider 都没有装载")
    skill_count = 0
    skills_d = _paths.asset_dir("skills")
    if skills_d.is_dir():
        skill_count = sum(1 for s in skills_d.iterdir() if s.is_dir() and (s / "manifest.yaml").exists())
    _startup_stats["skill_count"] = skill_count
    # 权限档「没人定过档」的汇总行。
    # 位置要在全部装载之后 —— 放早了会漏掉 MCP 那两个，
    #    而漏掉的数看起来完全像一个结论
    #    （`measure_request.boot_registry` 的漏登记自检记过同一个坑）。
    #    这里取的是最后一次那行 `Initialization complete` 之前的位置。
    try:
        from bus.registry import undeclared_report
        print(undeclared_report())
    except Exception as e:
        print(f"[bus] WARNING: 权限档汇总没跑成（{type(e).__name__}: {e}）")
    # 第二根轴（`destructive`）的同一行。
    # 它可能报"一个都没声明" —— 那行不是故障，是"还没做决定"。
    try:
        from bus.registry import undeclared_destructive_report
        print(undeclared_destructive_report())
    except Exception as e:
        print(f"[bus] WARNING: destructive 汇总没跑成（{type(e).__name__}: {e}）")
    print(f"[bus] Initialization complete ({_startup_stats['total_seconds']}s)")
    return security
