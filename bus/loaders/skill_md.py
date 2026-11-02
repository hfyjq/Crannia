"""指令型 skill（只有 `SKILL.md`、没有 `manifest.yaml` 的那种）—— 发现 + 解析。

## 它治的是什么

用户往 `skills/` 里放一个只有 `SKILL.md` 的目录，会被静默忽略：
`bus/core.py:326` 与 `bus/loaders/skill_loader.py:611` 都是"没有 `manifest.yaml` 就跳过"。
`reload_skills` 的结果里连 `failed` 都不进，用户看到的是「什么都没发生」。

## 判据 = 有没有 `manifest.yaml` —— 不是"像不像"

    有 `manifest.yaml`，工具提供者（走工具装载那条，一个字不改）
    没有、但有 `SKILL.md`，指令型 skill（本模块）
    两个都没有，跳过（它本来就不是 skill 目录）

判据是一个文件的存在性，可机械核，且不会随内容漂。

## 这不是"两种 skill"，是两层

能注册工具的 skill 不是 skill 的一种：`manifest.yaml` + `handler.py`
在机制上对应的是插件 / MCP；而 skill 层就是纯指令正文。本模块是另一层。

## 为什么是纯函数、没有模块级表

1. 可测 —— 喂一个临时目录就行，不用启动框架。
2. 模块级的可变表会被 census 当场抓住
   （它扫 `core/` + `bus/` 里"import 之后被写过"的名字）。而这里不需要是状态：
   谁要就在那一刻扫一遍。少一格要登记的、要 purge 的东西。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from bus.loaders.skill_loader import _parse_skill_md
from core import skill_sources as _sources
from core.content_sanitizer import sanitize_text
from core.tool_names import normalise_tools as _normalise

#: 跳过前导带这些字符的目录 —— 与 `skill_loader.reload_skills` 同一条规则。
#: `_wip_` 那一类是 `create_skill` 的临时目录：必须跳过，
#: 否则一个"还没体检完"的包会被当成装好的。
_SKIP_PREFIXES = (".", "_", "$")

#: 目录名长得像版本号（`1.0.0` / `5.6.2-wb.…`）。
#: 插件缓存里 `SKILL.md` 直接躺在版本目录下，那种目录名不能当技能名。
_VERSION_DIR_RE = re.compile(r"^\d+\.\d+")


def _skill_name_from_dir(entry: Path) -> str:
    """技能名 = 目录名；目录名是版本号时取上一层的插件名。

    判据没变（只有目录名能 import / 当命令，frontmatter 的 `name` 可以撒谎），
    但插件缓存是一种新形状：`<市场>/<插件>/<版本>/SKILL.md` ——
    那一层的目录名是版本号，直接用它会让所有技能叫 `1.0.0`。
    只认"以数字开头带点"这一种形状，不做通用猜测。
    """
    if _VERSION_DIR_RE.match(entry.name):
        parent = entry.parent.name
        if parent:
            return parent
    return entry.name

_MANIFEST = "manifest.yaml"
_SKILL_MD = "SKILL.md"

#: 共享根里 `SKILL.md` 解析问题的汇总口径（短标签 → 那句话，见
#: `skill_loader._parse_skill_md` 的 `on_problem`）。分两组：
#: · 致命 —— 这个 skill 收不进来（并进 `skipped` 那一行）；
#: · 提示 —— 收进来了，只是某个字段不合规范（不影响使用，名字取目录名）。
_PARSE_FATAL = {
    "no-frontmatter": "SKILL.md 没有 frontmatter",
    "yaml": "SKILL.md 的 YAML 读不出",
    "not-dict": "SKILL.md 的 frontmatter 不是字典",
    "missing-name": "SKILL.md 缺 name",
    "missing-description": "SKILL.md 缺 description",
}
_PARSE_ADVISORY = {
    "bad-name": "name 不符合 agentskills.io 规范",
}


@dataclass(frozen=True)
class InstructionSkill:
    """一个指令型 skill。字段全是"发现那一刻"的事实，没有运行时状态。"""

    #: 命令名 = 目录名。
    #: 理由：目录名是唯一无歧义的；frontmatter 的 `name` 可以撒谎或重复。
    #: （现成的例子：我们自己的 `skills/file_parser/` 里 `name:` 写的是 `file-parser`
    #: —— 同一个东西两个名字，而只有目录名能 `import`。）
    #: 别人源码的 `文件:行号` 不写在这里 —— 源码来源棘轮
    #: 只许降地钉着（它是发布前要清的东西）。
    name: str

    #: 绝对路径。 `Base directory for this skill:` 那行就是它 ——
    #: 模型靠它把正文里的相对路径（`assets/…` / `references/…`）解析成真文件。
    directory: Path

    description: str

    #: 正文（frontmatter 之后的部分）。发现阶段它没有读者 —— 留着是为了渲染，
    #: 也是为了"解析真的成功了"这件事可断言（空正文与解析失败长得像）。
    body: str

    #: 来源。`"repo"` 或 `"external"`（两值，`skill_sources.source_of` 推）。
    source: str = "repo"

    #: 更细的来源（给界面分类用）：`repo` / `agents` / `claude` /
    #: `codex` / `workbuddy` / `explicit`。由根规格给，不由内容推。
    origin: str = "explicit"

    #: frontmatter 里的 `arguments:`（具名参数）。
    #: 纯数字名在替换时会被跳过 —— 否则 `$0` 有两个意思。
    named_args: dict = field(default_factory=dict)

    #: 四态。发现时不判它（`discover_instruction_skills` 是纯发现）；
    #: 过滤在 `instruction_command_map` 里，因为那里才拿得到设置。
    state: str = "on"

    #: `allowed-tools`：这条命令/技能允许调用的工具。None = 不限制。
    #: 名称已归一为框架工具名（`core/tool_names.py`）。
    allowed_tools: object = None


def _as_specs(root) -> list[_sources.SkillRoot]:
    """把 *root* 归一成根规格清单（带深度与来源）。

    · `None`，全部根（`skill_sources.skill_root_specs()` = 仓内 + 共享）
    · 单个 `str` / `Path`，或一个序列，就那几个；深度 1、
      来源按已知根反查（查不到记 `explicit`）

    反查而不是"调用方必须传规格" —— 现有的调用点传的是
    `skill_roots()`（一个路径列表），那条路要继续能用，而且要拿到正确的
    来源与深度（否则插件缓存那种根会被按 1 层扫，一个都发现不了）。
    """
    if root is None:
        return _sources.skill_root_specs()
    if isinstance(root, (str, Path)):
        paths = [Path(root)]
    else:
        items = list(root)
        # 已经是 `SkillRoot` 的原样收（`instruction_command_map` 就是这么传的）。
        if items and all(isinstance(x, _sources.SkillRoot) for x in items):
            return items
        paths = [Path(r) for r in items]
    known: dict[str, _sources.SkillRoot] = {}
    try:
        for s in _sources.skill_root_specs(shared=True):
            try:
                known[str(s.path.resolve())] = s
            except OSError:
                continue
    except Exception:                                          # noqa: BLE001
        known = {}
    out: list[_sources.SkillRoot] = []
    for p in paths:
        try:
            k = str(p.resolve())
        except OSError:
            k = str(p)
        out.append(known.get(k)
                   or _sources.SkillRoot(p, 1, "explicit", "explicit"))
    return out


#: 命令文件：`<插件>/commands/<名>.md` —— 文件名就是命令名。
#: frontmatter 可以只有 `description`（甚至没有），正文就是提示词。
_COMMAND_NAME = "commands"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _walk_command_dirs(base: Path, depth: int) -> list[Path]:
    """在 *base* 下最多走 *depth* 层，返回名为 `commands` 的目录。"""
    out: list[Path] = []
    if depth <= 0:
        return out
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for entry in entries:
        if entry.name.startswith(_SKIP_PREFIXES):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name == _COMMAND_NAME:
            out.append(entry)
            continue
        out.extend(_walk_command_dirs(entry, depth - 1))
    return out


def _parse_command_md(path: Path) -> Optional[dict]:
    """读一个命令文件。没有 frontmatter 也是合法的（整篇就是提示词）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {"description": "", "body": text.strip()}
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        meta = {}
    return {"description": str(meta.get("description") or "").strip(),
            "body": m.group(2).strip(),
            "allowed_tools": meta.get("allowed-tools")}


def discover_plugin_commands(root=None) -> list[InstructionSkill]:
    """扫插件里的 `commands/<名>.md`，文件名就是命令名。

    这是另一层：命令是"一段提示词模板"，不是技能正文 ——
    所以 `origin` 记 `"command"`（不进模型面，只进 `/` 命令面）。
    与技能同一个判据来源（`_as_specs` 的根 + 深度），不另建一套根。
    """
    out: list[InstructionSkill] = []
    seen: set[str] = set()
    for spec in _as_specs(root):
        base = spec.path
        if not base.is_dir():
            continue
        for cmd_dir in _walk_command_dirs(base, spec.depth):
            try:
                files = sorted(cmd_dir.glob("*.md"), key=lambda p: p.name)
            except OSError:
                continue
            for f in files:
                try:
                    real = str(f.resolve())
                except OSError:
                    real = str(f)
                if real in seen:
                    continue
                seen.add(real)
                meta = _parse_command_md(f)
                if meta is None:
                    print("[skill] 跳过命令文件（读不出 frontmatter）：%s" % f)
                    continue
                out.append(InstructionSkill(
                    name=f.stem,
                    directory=f.parent,
                    description=meta["description"],
                    body=meta["body"],
                    source=_sources.source_of(base),
                    origin="command",
                    allowed_tools=_normalise(meta.get("allowed_tools")),
                ))
    return out


def _walk_skill_dirs(base: Path, depth: int,
                     skipped: dict[str, int]) -> list[Path]:
    """在 *base* 下最多走 *depth* 层，返回含 `SKILL.md` 的目录（找到就停）。

    找到就停：一个插件要么根上有 `SKILL.md`、要么在 `skills/<名>/` 下，
    两种不会同时是"技能"；继续深入只会把同一份技能再收一次
    （然后撞重名拒）。

    序是确定的（每层按名字排）—— 顺序漂了，缓存前缀就跟着漂。
    """
    out: list[Path] = []
    if depth <= 0:
        return out
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        skipped["读不到（%s）" % type(exc).__name__] = \
            skipped.get("读不到（%s）" % type(exc).__name__, 0) + 1
        return out
    for entry in entries:
        if entry.name.startswith(_SKIP_PREFIXES):
            continue
        why = _sources.skip_reason(entry, base)
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if (entry / _SKILL_MD).is_file():
            out.append(entry)
            continue
        out.extend(_walk_skill_dirs(entry, depth - 1, skipped))
    return out


def discover_instruction_skills(root=None) -> list[InstructionSkill]:
    """扫 *root*，返回指令型 skill，按目录名排序。*root* 三态见 `_as_specs`
    （`None` = 仓内 + 共享；给了具体路径就只扫那一个）。

    序是确定的：顺序漂了，缓存前缀就跟着漂。

    两条降权在这里生效（共享目录的包）：
      ① 目录本身是符号链接，跳（`skill_sources.skip_reason`）
      ② `SKILL.md` > 128 KB，跳（`skill_sources.skill_md_skipped`；仓内不限）
      两条都出声（`print`）—— 「静默跳过」是反模式。
      ①在本机会命中：`~/.claude/skills/*` 那 27 项全是指向
      `~/.agents/skills/*` 的符号链接（见 `core/skill_sources` 的 docstring）。

    共享根里的解析问题也按根汇总 ——
      那里会出现非法 YAML（`description_en:` 里一个裸冒号）与中文 `name:`；
      逐条打全路径会把启动日志刷爆，而这些不是我们能修的。
      · 致命（拿不到 frontmatter / YAML 坏 / 不是字典 / 缺字段），并进 `skipped`；
      · 提示（`name` 不符合 agentskills.io），单列，并说清不影响使用
        （命令名取目录名，那个字段我们根本不读）。
      仓内那一个根仍然逐条打 —— 那里坏了是我们的 bug，需要精确的路径。

    它一个字节都不写（不注册、不落盘、不缓存）。调用方拿到的是那一刻的事实；
    要长期持有就自己存，并去 `core/state_registry.py` 登记一行。
    """
    out: list[InstructionSkill] = []
    seen_dirs: set[str] = set()
    for spec in _as_specs(root):
        base = spec.path
        if not base.is_dir():
            continue

        #: 跳过的按原因计数，末尾汇总成一行打出。
        #: [!] 逐条 `print` 会把控制台刷爆（本机 `~/.claude/skills` 那 27 条 junction
        #:   逐条一行、而这一段每次重建索引都跑）—— 但不许静默
        #:   （静默跳过是反模式），所以是汇总而不是删掉。
        skipped: dict[str, int] = {}
        #: 解析问题（同一条判据）：致命的并进 `skipped`，提示性的单列。
        _problems: dict[str, int] = {}
        _external = _sources.is_external(base)

        def _note(key: str) -> None:
            _problems[key] = _problems.get(key, 0) + 1

        # 同一个插件在缓存里可能留着多个版本（`<插件>/<版本>/SKILL.md`）
        #同一根内先按技能名去重，只留 mtime 最新的那个，并出声。
        # 只在本根内去重：跨根同名是真的冲突，交给
        #   `instruction_command_map` 拒（那条判据不许在这里提前吞掉）。
        _found = _walk_skill_dirs(base, spec.depth, skipped)
        if _found:
            _by_name: dict[str, Path] = {}
            _old = 0
            for e in _found:
                n = _skill_name_from_dir(e)
                cur = _by_name.get(n)
                if cur is None:
                    _by_name[n] = e
                    continue
                _old += 1
                try:
                    if e.stat().st_mtime > cur.stat().st_mtime:
                        _by_name[n] = e
                except OSError:
                    continue
            if _old:
                skipped["同一插件的旧版本（只留最新）"] = _old
            _found = [_by_name[k] for k in sorted(_by_name)]

        for entry in _found:
            if (entry / _MANIFEST).is_file():
                continue                # 工具型 —— 不归这里，它走 `skill_loader` 那条
            md = entry / _SKILL_MD
            if not md.is_file():
                continue                # 两个都没有：它本来就不是 skill 目录，静默跳过是对的
            if _sources.skill_md_skipped(md, base):
                why = "SKILL.md 超过 %d KB（§5.2 共享目录的上限）" % (
                    _sources.MAX_EXTERNAL_SKILL_MD_BYTES // 1024)
                skipped[why] = skipped.get(why, 0) + 1
                continue

            # 跨根去重按真实路径 —— 同一个包经两个根解析到同一个目录算一份
            #   （名字相同而真实路径不同，才是 `instruction_command_map` 要拒的那种）。
            try:
                real = str(entry.resolve())
            except OSError:
                real = str(entry)
            if real in seen_dirs:
                continue
            seen_dirs.add(real)

            # 复用已有的那个解析器（别写第二个）。
            #   共享根：把问题交给下面的汇总（逐条打会刷屏，且那些文件不归我们修）；
            #   仓内：照旧逐条出声 —— 那里坏了是我们自己的 bug，路径要精确。
            meta = _parse_skill_md(entry, on_problem=None if not _external else _note)
            if meta is None:
                continue                # 致命解析问题：已计入 `_problems`（或已自己出声）

            # 具名参数只认"字典形"的 `arguments:`（`{名: 值}`）。
            #   它是列表形（`[name1, name2]` —— 只声明名字不给值）时不填 ——
            #   那种形状下「`$name` 的值从哪来」我们没有核实过
            #   （只记了"`arguments:` frontmatter 声明的"，没记值怎么绑）。
            #不猜（不许承诺兑现不了的）：宁可 `$name` 保持原样，
            #   也不要按一个猜的语义去替换。
            raw_args = (meta.get("raw_metadata") or {}).get("arguments")
            named = dict(raw_args) if isinstance(raw_args, dict) else {}

            out.append(InstructionSkill(
                name=_skill_name_from_dir(entry),
                directory=entry.resolve(),
                description=(meta.get("description") or "").strip(),
                body=(meta.get("body") or "").strip(),
                # 来源从根推（不硬编码 `"repo"`）。
                source=_sources.source_of(base),
                origin=spec.origin,
                named_args=named,
                allowed_tools=_normalise(
                    (meta.get("raw_metadata") or {}).get("allowed-tools")),
            ))

        # 解析问题并进同一套汇总（判据见 `_parse_skill_md` 的 `on_problem`）。
        advisory: dict[str, int] = {}
        for _key, _n in _problems.items():
            _label = _PARSE_FATAL.get(_key)
            if _label is None:
                advisory[_PARSE_ADVISORY.get(_key, _key)] = _n
            else:
                skipped[_label] = skipped.get(_label, 0) + _n

        for why, n in sorted(skipped.items()):
            print("[skill] 跳过 %d 个（%s）—— 根：%s" % (n, why, base))
        for why, n in sorted(advisory.items()):
            # 点破"不影响使用"：命令名取目录名，frontmatter 那个 `name`
            #   我们根本不读 —— 不说的话，用户会以为这个技能坏了。
            print("[skill] %d 个 %s（不影响使用：命令名取目录名）—— 根：%s"
                  % (n, why, base))
    return out


# 参数替换 · 四态 · 命令表

#: 四态：
#:
#:   on                    模型看得见 + `/名字` 能用（默认）
#:   name-only             列出来但不带描述（省钱）
#:   user-invocable-only   模型看不见，但 `/名字` 还在
#:   off                   两边都藏
#:
#: 判据：按 skill 名覆盖；缺席 = on。
#:读不到不是错误，是默认（同一句判据也适用于坏掉的设置文件）。
SKILL_STATES = ("on", "name-only", "user-invocable-only", "off")

#: 内联 shell 关闭。
#:
#: 是换成占位符，不是删掉 —— 删掉会让正文读起来断片
#: （模型看到前后不接，会自己去猜那里原本是什么）。
#: 判据：外来 skill 的正文是「别人写的提示词进你的 agent」，
#: 只让它说话，不让它动手。
SHELL_DISABLED_PLACEHOLDER = "[shell command execution disabled by policy]"

#: 内联 shell 的形状：`` !`cmd` ``。
_INLINE_SHELL_RE = re.compile(r"!`[^`]*`")

_SETTINGS_PATH = Path("data/settings.json")


def _skill_states() -> dict:
    """`{skill 名: 四态}`。读不到 / 坏 JSON，`{}`（= 全默认 `on`）。

    不抛：一个坏掉的设置文件不该让所有 skill 一起消失
    （同 `core/mail_config` 那条：读不到，`{}`）。
    未知的态，当 `on`（读到了但读不懂时，默认方向是"照常"，
    因为这一根是呈现轴不是安全轴）。
    """
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cfg = (raw or {}).get("skill_configs") or {}
    out = {}
    for name, entry in cfg.items():
        if isinstance(entry, dict):
            st = entry.get("state")
            if isinstance(st, str) and st.strip():
                out[name] = st.strip()
    return out


def substitute_args(text: str, args: str, named=None) -> str:
    """参数替换。

    | 写法 | 换成 |
    |---|---|
    | `$ARGUMENTS` | 全部参数 |
    | `$ARGUMENTS[n]` / `$n` | 第 n 个（0 基） |
    | `$name` | `arguments:` frontmatter 里声明的具名参数（纯数字名不算 —— 否则和 `$0` 撞） |
    | 一个占位符都没有、但有参数 | 尾部追加 `\\n\\nARGUMENTS: <全部>` |
    | 没传参 | 全部占位符换成空串 |

    具名参数先替换：`$ARGUMENTS` 是 `$A…` 形的，若先做 `$0..$n` 那一轮
    不会有问题，但先替换具名能让"名字恰好是 `ARGUMENTS`"这种边角也落对。
    """
    named = named or {}
    parts = args.split() if args else []
    # 必须在替换之前判 —— 替换之后占位符都被吃掉了，那时再判就是恒假
    #   （否则"没占位符"那条路永远走，尾注每次都追加）。
    had_placeholder = bool(_HAS_PLACEHOLDER.search(text))

    def _pos(i):
        return parts[i] if 0 <= i < len(parts) else ""

    # ① 具名（纯数字名不算 —— 那是 `$0` 的写法）
    for k, v in named.items():
        key = str(k)
        if key.isdigit():
            continue
        text = text.replace("${%s}" % key, str(v)).replace("$%s" % key, str(v))

    # ② `$ARGUMENTS[n]` 与 `$ARGUMENTS`
    text = re.sub(r"\$ARGUMENTS\[(\d+)\]", lambda m: _pos(int(m.group(1))), text)
    text = text.replace("$ARGUMENTS", args)

    # ③ `$0` `$1` …（只认 0-9：单位数）
    text = re.sub(r"\$([0-9])", lambda m: _pos(int(m.group(1))), text)

    # ④ 一个占位符都没有过、但用户给了参数，尾部追加
    if args and not had_placeholder:
        text = text.rstrip() + "\n\nARGUMENTS: " + args
    return text


#: `substitute_args` 第 ④ 步的判据。它问的是「原始正文里有没有过占位符」，
#: 所以必须在替换之前查（替换之后一律为假 —— 见上面那行注释）。
_HAS_PLACEHOLDER = re.compile(r"\$ARGUMENTS|\$[0-9]|\$\{[A-Za-z_]")


def render_skill_content(skill, args: str = "") -> str:
    """把正文包成那个壳（外壳逐字固定）。

    ```
    <skill_content name="…">
    <skill_resources>
    Base directory for this skill: <绝对路径>
    Resolve relative paths mentioned by this skill against the base directory before using them. Load referenced resources only as needed.
    </skill_resources>
    <skill_instructions>
    …正文（过参数替换 + 内联 shell 关掉）…
    </skill_instructions>
    </skill_content>
    ```

    外壳形状与那两句英文与 dsh 的 `renderSkillContent` 相同（MIT，见
    `THIRD-PARTY.md` 第 17 节）。

    `Base directory for this skill:` 那两句逐字固定 ——
    这是「别人的 skill 拿过来就能用」的关键：正文里的相对路径
    （`assets/template.html` / `references/themes.md`）模型靠这一句才解析得对。
    `references/` / `scripts/` 不枚举，正文自己点名，模型用自己的工具读。
    不做资源加载器。

    正文过 `sanitize_text`：
    与「框架拼进 prompt 的块」同一条不变量。
包裹照发、只打码内容（打码只打内容这条不变量）。
    这是我们的加强，可推翻。
    """
    body = substitute_args(skill.body, args, getattr(skill, "named_args", None))
    # 内联 shell 关闭：换成占位符，不是删掉。
    body = _INLINE_SHELL_RE.sub(SHELL_DISABLED_PLACEHOLDER, body)
    body = sanitize_text(body)
    return (
        '<skill_content name="%s">\n'
        "<skill_resources>\n"
        "Base directory for this skill: %s\n"
        "Resolve relative paths mentioned by this skill against the base directory "
        "before using them. Load referenced resources only as needed.\n"
        "</skill_resources>\n"
        "<skill_instructions>\n"
        "%s\n"
        "</skill_instructions>\n"
        "</skill_content>"
    ) % (skill.name, skill.directory.as_posix(), body)


#: 进程级缓存。
#:
#: 为什么必须有它：扫一遍 `skills/` 要读每一个 `SKILL.md`
#: （真实的 skill 那个 `SKILL.md` 有 36 KB），每条消息扫一次是不可接受的 ——
#: 而 `_is_framework_command` 正是每条消息都会走的那条路。
#:
#: 它必须能被失效 —— 否则「用户放一个 skill 进去就能用」这句话就是假的。
#: 失效点只有一处：`skill_loader.reload_skills`
#: （生效要人按一下，缓存跟着那一下走）。
#:
#: 它是进程级，登记在 `core/state_registry.py` 的 `PROCESS_SHARED`，
#: 并明说 `/clear` 不清它 —— 清了不是错，只是白扫一遍（与 `_sibling_cache` 同族）。
#: 显式传 `states` 时不走缓存（测试与一次性诊断要的是"此刻的事实"）。
_map_cache: dict = {}


def invalidate_command_map() -> None:
    """清掉 `instruction_command_map` 的缓存。 `/skill reload` 调它（唯一失效点）。"""
    _map_cache.clear()


def instruction_command_map(root=None, reserved=(), states=None):
    """`{命令名: InstructionSkill}`，外加被拒的。

    Returns:
        `(visible, rejected)`：
          · *visible* —— 可以当 `/命令` 用的（含 `user-invocable-only`：
            它只是不进模型面，`/` 照样敲得到）
          · *rejected* —— `[(名字, 原因)]`。出声用，绝不静默 ——
            判据：在有权限含义的地方必须拒，
            不采用「静默跳过 + 后者覆盖」那种做法。

    ## 两条拒法

    1. 与内置命令撞（`/clear` 之类）—— `reserved` 由调用方注入
       （`core/` 与 `bus/` 的叶子规矩：这里不 import `pre_process`）。
    2. 两个 skill 同目录名 —— 同一目录里不可能，但多 root 时可能
       （`~/.agents/skills` 等），判据写在这儿，到时不用改。

    ## 四态

    `off`，不进 / 列表（也不进模型面）；其余三态都进来。
    `off` 不算"被拒" —— 那是用户的选择，不是冲突（两者不许同形：
    一个是"你得处理"，一个是"就这样"）。
    """
    cacheable = states is None
    #: 缓存键必须是根清单（不是那个可能为 `None` 的入参）—— 两串不同的根
    #: 解析出同一份清单时，键也该相同；而 `None` 每次现算 cwd/home（`skill_sources` 那条）。
    specs = _as_specs(root)
    #: 缓存键要含深度与来源 —— 只拿路径当键的话，
    #: 同一个根换个深度扫出来的清单会串（后一次静默拿到前一次的结果）。
    ck = (tuple((str(s.path), s.depth, s.origin) for s in specs),
          tuple(sorted(str(r) for r in reserved)))
    if cacheable and ck in _map_cache:
        return _map_cache[ck]

    st = _skill_states() if states is None else states
    reserved = {r.lower() for r in reserved}
    visible, rejected, seen = {}, [], set()

    for s in list(discover_instruction_skills(specs)) + list(
            discover_plugin_commands(specs)):
        key = ("/" + s.name).lower()
        state = st.get(s.name, "on")
        if state not in SKILL_STATES:
            state = "on"                      # 读不懂，照常（呈现轴，见 `_skill_states`）
        if state == "off":
            continue
        if key in reserved:
            rejected.append((s.name, "与内置命令 %s 撞名" % key))
            continue
        if key in seen:
            rejected.append((s.name, "与另一个 skill 撞名（%s）" % key))
            continue
        seen.add(key)
        visible[key] = s
    if cacheable:
        _map_cache[ck] = (visible, rejected)
    return visible, rejected


def report_line(skills: list) -> str:
    """一行汇总（给启动日志 / `/skill` 回执用）。0 个时返回空串。

    判据："不再静默" —— 有指令型 skill 时必须
    说得出有几个、分别是谁。

    0 个时返回空串是有意的：一个每次启动都打的「指令型 skill: 0 个」
    会变成背景噪声，而噪声里的真信号不会被看见
    （一个"什么都没发生"的行会让下一条真的"什么都没发生"消失）。
    """
    if not skills:
        return ""
    names = ", ".join(s.name for s in skills)
    return "instruction-type skills: %d [%s]" % (len(skills), names)
