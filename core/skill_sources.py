"""技能从哪些根来、每个根意味着什么。

## 一根 → 一串根

仓内 `skills/` 与共享目录：别人（别的 agent）放的 skill
我们也认 —— 「来源」因此有真值
（`bus/loaders/skill_md.py::InstructionSkill.source`）。

判据：

| 来源 | 不跟符号链接 | `SKILL.md` 上限 |
|---|---|---|
| 仓内 `skills/` | ✔ | 无（待定） |
| 本机共享目录 | ✔ | 128 KB（超过跳过） |

## 「不跟符号链接」

同一个 skill 可能同时住在两个扫描根里，而其中一个是指向另一个的符号链接。
跟过去的话它在两个根里各出现一次，撞上下一条「重名一律拒」，
一台新机开机就报一串无意义的冲突。

跳过符号链接目录之后，每个 skill 只从一个根进来，那串冲突不会发生 ——
而"生效的是哪一个"也因此不必取决于目录遍历顺序。

两条判据成对（只写一条的话，"全部跳过"也全绿）：
  · 同一个包经不同根解析到同一个真实路径，算一份（不算重名）
  · 名字相同但真实路径不同，才是真冲突（`instruction_command_map` 那条拒）

## 为什么是纯函数、没有模块级表

同 `bus/loaders/skill_md`：① 可测（喂临时目录）；② **模块级的可变表会被
census 当场抓住**（它扫 `core/` + `bus/` 里
"import 之后被写过"的名字），而这里不需要是状态 —— 谁要就在那一刻算一遍。
同理：`Path.cwd()` / `Path.home()` 一律在函数里现算，不在模块顶算
（import 期常量会把 cwd 冻住，而测试是换 cwd 跑的 —— `sandbox()`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

#: 来源的两个值。`InstructionSkill.source` 只许是这两个之一。
SOURCE_REPO = "repo"
SOURCE_EXTERNAL = "external"

#: 仓内那一个根。相对路径（cwd）—— 与 `skill_loader.reload_skills` 同口径。
from core import paths as _paths

REPO_ROOT = _paths.asset_dir("skills")

#: 仓内那个根的绝对路径 —— 只回答一个问题：「调用方给我的 *repo_root*
#:   是不是仓内那一个？」（`skill_roots` 的 `shared=None` 那一支）。
#:
#: 为什么不能拿 `REPO_ROOT.resolve()` 去比：
#:   `REPO_ROOT` 是相对的，那个比较是"拿 cwd 比 cwd"，恒真。
#:   后果：`sandbox()`（换 cwd）里跑的测试照样会扫进用户家目录
#:   （`~/.agents/skills` + `~/.claude/skills` 那几个共享根），也就是那条 docstring
#:   明说要避免的事根本没生效。
#: 从 `__file__` 推，不随 cwd 变（`Path.cwd()` 会冻、`Path(__file__)` 不会）。
_REPO_SKILLS_ABS = Path(__file__).resolve().parent.parent / "skills"

#: 共享根 —— `(标签, 路径, 深度, 来源)`，顺序 = 优先级。
#: · `~/.agents/skills`  跨 agent 的共享约定，几个 agent 工具都往这里放
#: · 项目级 `.agents/skills/`  同族，跟着项目走
#: · `~/.claude/skills/`  别人的目录；我们只读，不写
#: · `~/.codex/skills/.system`  另一家的内置技能（点开头是根的一部分，
#:   所以不经过"跳过点开头条目"那条规则）
#: · `~/.workbuddy/plugins/cache`  另一家的已安装插件
#:   （深度 5：`<市场>/<插件>/<版本>/SKILL.md` 与
#:   `<市场>/<插件>/<版本>/skills/<技能>/SKILL.md` 两种布局都在里面）
#:
#: 深度 = 从根往下最多走几层去找 `SKILL.md`（找到就停，不再深入）。
#: 默认 `1` = 根的直接子目录就是技能目录（现有全部共享根的形状）。
_SHARED_SPECS: tuple[tuple[str, tuple[str, ...], int, str], ...] = (
    # 用户自己装的（数据根下）—— 排在仓内之后、其他共享根之前：
    #   同名时自带的那一个赢（稳定预期），用户装的顶不掉随包发布的。
    #   开发树里它与仓内是同一个目录（会去重成一条）；安装版里才分开。
    ("user", ("data", "skills"), 1, "user"),
    ("agents", ("home", ".agents", "skills"), 1, "agents"),
    ("agents-project", ("cwd", ".agents", "skills"), 1, "agents"),
    # 插件包（ C 路线）：一个包里可能同时有 `skills/<名>/SKILL.md`
    #   与 `commands/*.md`，深度 3 = 根 → 包 → skills/commands → 条目。
    #   用户主动放进来的包默认生效。
    ("plugins", ("cwd", "plugins"), 3, "plugins"),
    ("agents-plugins", ("cwd", ".agents", "plugins"), 3, "plugins"),
    ("home-plugins", ("home", ".agents", "plugins"), 3, "plugins"),
    ("claude", ("home", ".claude", "skills"), 1, "claude"),
    ("codex", ("home", ".codex", "skills", ".system"), 1, "codex"),
    ("workbuddy", ("home", ".workbuddy", "plugins", "cache"), 5, "workbuddy"),
    # 另一个 agent 的已安装插件缓存：`<市场>/<插件>/<版本>/` 下可能是
    #   `SKILL.md`（扁平）或 `skills/<技能>/SKILL.md`（两层）—— 深度 5 两种都覆盖。
    ("claude-plugins", ("home", ".claude", "plugins", "cache"), 5, "claude"),
)

#: 共享目录里 `SKILL.md` 的上限（`> 128 KB 跳过`）。
MAX_EXTERNAL_SKILL_MD_BYTES = 128 * 1024

#: 「工作区插件被挡住」这个提示只出一次。
#: `skill_root_specs` 每轮会被调好几次（指令映射、技能索引、扩展页），
#: 每条都 print 会把控制台刷满，而用户只需要知道一次。
_WS_PLUGIN_NOTE_SHOWN = False


def _workspace_plugins_ok() -> bool:
    """工作区里的插件包能不能贡献技能。默认关。

    判据与钩子、MCP 那两半同一条（`core.plugin_packages.
    workspace_plugins_allowed`）—— 技能不是纯文本：`SKILL.md` 旁边的
    handler 会在首次调用时 `import_module` 并执行，那和起一个 MCP 进程、
    跑一条钩子命令是同一件事。同一个开关管住其中两样、漏掉第三样，
    "关掉工作区插件"就成了一句说得比做得多的话。

    用户自己机器上的包根（`~/.agents/plugins` 与两个缓存目录）不看这个开关
    —— 它们不是跟着别人的仓库进来的。
    """
    global _WS_PLUGIN_NOTE_SHOWN
    try:
        from core.plugin_packages import workspace_plugins_allowed
        ok = workspace_plugins_allowed()
    except Exception as exc:                                  # noqa: BLE001
        print("[skills] 读工作区插件开关失败（按关处理）：%s: %s"
              % (type(exc).__name__, exc))
        return False
    if not ok and not _WS_PLUGIN_NOTE_SHOWN:
        _WS_PLUGIN_NOTE_SHOWN = True
        print("[skills] 工作区插件包里的技能没有加载：要先在"
              "「设置 → 安全设置 → 工作区插件」里打开（与钩子、MCP 服务器同一个开关）。")
    return ok


#: 框架自带的技能 —— 随仓库发布的那一批（技能页不显示它们）。
#:
#: 判据（真值怎么来的）：`git ls-files skills/` 的顶层目录名 —— 随仓库走的 = 自带。
#: 读者 `tests/test_builtin_skills.py` 两向钉着（名单里的都要真存在 ·
#: 跟踪的都要在名单里）· git 拿不到时明说跳过、不算通过。
#:
#: [!] 它不是 `source` 那一根轴上的第三个值：一个包可以既是 `repo` 又是内置。
#: 「技能页不显示」= `source == SOURCE_REPO` 且 `is_builtin_skill(name)`。
#: [!] 加一个自带 skill 却忘了登记 -> 它会显示成"用户装的"（绿标说谎）——
#: 那正是这条读者要挡的；反向（名单里点一个不存在的名字）= 改名后名单烂掉。
#: 只列随框架发布的那些。测试夹具（draft、从不装载、不进安装包）不在这张表里 ——
#: 登记一个包里没有的名字，会让用户自己装的同名技能被标成"内置"而藏起来。
BUILTIN_SKILLS: frozenset[str] = frozenset({
    "entertainment", "file_parser", "github", "mail_parser", "media_gen",
    "office_parser", "ppt-master",
})


def is_builtin_skill(name: str) -> bool:
    """这个名字是不是框架自带的（见 `BUILTIN_SKILLS`）。读不到 -> `False`。"""
    return (name or "") in BUILTIN_SKILLS


@dataclass(frozen=True)
class SkillRoot:
    """一个扫描根：路径 + 往下走几层 + 来源 + 标签。

    `depth` 是"从根往下最多走几层去找 `SKILL.md`"（找到就停）。
    默认 1 = 根的直接子目录就是技能目录 —— 那是 `~/.agents/skills` 这类
    共享根的形状；插件缓存那类要走 5 层（见 `_SHARED_SPECS`）。

    `origin` 是给人和界面看的来源（`repo` / `agents` / `claude` /
    `codex` / `workbuddy`），比 `source`（两值）细一档 ——
    技能配置页要按它分类。
    """

    path: Path
    depth: int
    origin: str
    label: str


def skill_root_specs(repo_root: str | Path = REPO_ROOT, *,
                     shared: bool | None = None) -> list[SkillRoot]:
    """全部根（带深度与来源），按优先级、去重、保序。

    *shared* 三态与 `skill_roots` 逐字同义（那里是它的判据原文）。
    """
    repo = Path(repo_root)
    if shared is None:
        try:
            shared = repo.resolve() == _REPO_SKILLS_ABS
        except OSError:
            shared = False
    cands = [SkillRoot(repo, 1, SOURCE_REPO, "repo")]
    if shared:
        # 只在真的要扫共享根时读一次开关 —— `shared=False`（沙箱 / 临时夹具）
        #   不该因为一个它用不上的开关去碰 `data/settings.json`。
        ws_plugins_ok = _workspace_plugins_ok()
        for label, parts, depth, origin in _SHARED_SPECS:
            # 工作区里的插件包根：技能是代码，和钩子、MCP 同一条判据。
            #   `home-plugins` 不受影响（不是跟着仓库进来的）。
            if origin == "plugins" and parts[0] == "cwd" and not ws_plugins_ok:
                continue
            if parts[0] == "home":
                base = Path.home()
            elif parts[0] == "cwd":
                base = Path.cwd()
            else:                      # "data" —— 用户数据根
                base = _paths.data_root()
            cands.append(SkillRoot(base.joinpath(*parts[1:]), depth, origin, label))

    seen: set[str] = set()
    out: list[SkillRoot] = []
    for s in cands:
        try:
            key = str(s.path.resolve())
        except OSError:
            key = str(s.path)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def skill_roots(repo_root: str | Path = REPO_ROOT, *,
                shared: bool | None = None) -> list[Path]:
    """全部根，按优先级（仓内最先）。去重、保序 —— 同 `core/workspace.lookup_roots`。

    *shared* 三态：
      · `None`（默认）—— **只在 *repo_root* 就是仓内那一个时才带上共享根**。
        这条不是"魔法"，是判据：调用方指到别处（临时夹具 / `sandbox()` 里的
        临时目录）时，他要的是「就这一个」，不是「顺带把这台机器上的共享根
        也扫进来」—— 后者会让每一个用临时目录的测试都吃进用户的家目录。
        比的是 `_REPO_SKILLS_ABS`（绝对），不是 `REPO_ROOT.resolve()` ——
        后者是"拿 cwd 比 cwd"，恒真（见那个常量的注释）。
      · `True` / `False` —— 显式要 / 显式不要（测试与将来的"只看仓内"开关用）。

    深度与来源在 `skill_root_specs` 里；这个函数是它的路径视图
    （调用方只要路径时用这个，判据不重复写第二份）。
    """
    return [s.path for s in skill_root_specs(repo_root, shared=shared)]


def source_of(root: str | Path, repo_root: str | Path = REPO_ROOT) -> str:
    """某个根，来源。仓内那一个才是 `repo`，其余一律 `external`。"""
    try:
        same = Path(root).resolve() == Path(repo_root).resolve()
    except OSError:
        same = False
    return SOURCE_REPO if same else SOURCE_EXTERNAL


def is_external(root: str | Path, repo_root: str | Path = REPO_ROOT) -> bool:
    return source_of(root, repo_root) != SOURCE_REPO


def is_link(entry: Path) -> bool:
    """这个目录是不是重解析点（符号链接 或 junction）—— 要跳的那一类。

    两个都要判（本机过）：
    本机 `~/.claude/skills/lark-base` 的 `is_symlink()` 是 False、`is_junction()`
    是 True —— 它是个 junction（`resolve()` 照样指到 `~/.agents/skills/...`）。
只看 `is_symlink()` 的话，那 27 条一条都拦不住，
    开机就会撞 27 条「重名」假冲突。
    `Path.is_dir()` 会跟重解析点（`skill_loader.reload_skills` 今天用的就是它）
必须显式判，不能指望 `is_dir` 帮我们区分。
    `is_junction()` 是 Python 3.12+ 才有的（本仓要求 3.12+，实际 3.13.6）；
    老版本上 `hasattr` 兜一下 —— 非 Windows 上它本来就恒 `False`。
    """
    try:
        if entry.is_symlink():
            return True
    except OSError:
        return False
    try:
        return bool(hasattr(entry, "is_junction") and entry.is_junction())
    except OSError:
        return False


def skip_reason(entry: Path, root: str | Path, repo_root: str | Path = REPO_ROOT) -> str:
    """这个 skill 目录该不该跳过？返回原因（空串 = 不跳）。

    只有一条（「不跟符号链接」）：目录本身是重解析点就跳
    （符号链接 或 junction —— 判据见 `is_link`）。
    """
    try:
        if is_link(entry):
            return "符号链接/junction（不跟）"
    except OSError as exc:                                    # noqa: BLE001
        return "读不到（%s）" % type(exc).__name__
    return ""


def skill_md_skipped(md: Path, root: str | Path, repo_root: str | Path = REPO_ROOT) -> bool:
    """共享根里超上限的 `SKILL.md`，跳过；仓内不限。"""
    if not is_external(root, repo_root):
        return False
    try:
        return md.stat().st_size > MAX_EXTERNAL_SKILL_MD_BYTES
    except OSError:
        return False


def describe_roots(roots: Sequence[str | Path], repo_root: str | Path = REPO_ROOT) -> str:
    """一行账（给启动日志用）：`repo=skills · external=…`。空的时候返回空串。"""
    parts = []
    for r in roots:
        tag = source_of(r, repo_root)
        parts.append("%s=%s" % (tag, r))
    return " · ".join(parts)


__all__ = [
    "SOURCE_REPO", "SOURCE_EXTERNAL", "REPO_ROOT", "MAX_EXTERNAL_SKILL_MD_BYTES",
    "BUILTIN_SKILLS", "is_builtin_skill",
    "skill_roots", "source_of", "is_external", "is_link", "skip_reason", "skill_md_skipped",
    "describe_roots",
]
