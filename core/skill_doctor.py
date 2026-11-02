"""skill 兼容性体检 —— 静态、只读、不执行任何东西。

## 它是什么 / 不是什么

是：一个初筛器。往 `skills/` 里放一个包，它扫一遍，把机械可判的不兼容
立刻报出来 —— 不占 API（零 LLM 调用）、不改运行态（不注册、不进索引）、
不执行第三人的代码。

不是：装载器。它不 import、不注册、不写任何文件。
「生效」仍然要人按一下（`/skill reload` 或 `/skill approve`）——
体检 ≠ 生效，这是本条最要紧的一条分工。

## 判据：`ast.parse` 而不是 `import`

「不执行任何东西」不是靠沙箱做到的，是靠根本不跑它 ——
`ast.parse` 只把源码解析成语法树，连一行都不执行。

这比"在沙箱里 import 一下"更隔离：沙箱要先把代码跑起来才能沙它，
而这里连跑都不跑。机械读者钉着它：
本模块的 AST 里不许出现 `import_module` / `exec` / `eval` / `compile` /
`__import__` / `exec_module` / `spec_from_file_location`。

## 它抓不到什么（必须说清，否则有人会以为"体检过了 = 装得上"）

下面这些只有真 import 才知道，静态体检一律看不见：

- `handler.py` 里 `import` 了一个没装的第三方包（`ModuleNotFoundError`）
- `init(security_guard)` 自己会抛
- 模块级代码要连网 / 要读一个不存在的文件
- 运行时才会暴露的逻辑错

它是「快速筛掉显而易见的坏」，不是"保证装得上"。
同族判据：检测可以错，打码不能错 ——
这里是「体检可以漏，但不能改任何东西」。

## 三种严重度

| | 含义 | 例子 |
|---|---|---|
| `fail` | 装不上，或装上了会让整个框架出问题 | 工具重名（上游硬 400）· 语法错 · `inputSchema` 非法 |
| `warn` | 装得上，但大概率不是你要的 | `draft: "false"`（字符串是真值，被静默跳过）· 声明的工具在 handler 里找不到 |
| `info` | 提示，不影响 | 缺 `SKILL.md` · 缺 `__init__.py` |

## 会话态：无

本模块没有任何可变状态 —— 全是纯函数。所以它**不进 `MemoryManager.new_session`
的那串 purge**（`_discovered` / `_todos` / `_reads` 那一串）。
这是刻意的：一个"每次调用都从磁盘现算"的体检器，缓存它只会引入
"我改了文件但它还说没事"这种最坏的一类假绿。
"""

from __future__ import annotations

import ast
import threading
import os
from dataclasses import dataclass
from pathlib import Path

# 读家族与合法形态的唯一来源。
# 体检器只读 manifest，不 import 那个包的 handler（本模块的立身之本），
# 所以它判的是声明，不是"注册表里现在是什么样"。
from core.tool_output_kind import VALID_KINDS, is_read_family  # noqa: E402
from core.tool_permission import VALID_PERMS  # noqa: E402

# 结果形状

#: 严重度（字符串常量而不是 Enum：它要跨 JSON/前端/日志三处，字符串最省事）
SEV_FAIL = "fail"
SEV_WARN = "warn"
SEV_INFO = "info"

#: 体检没跑的项。判据同 `None != 0`：
#: 「没查」和「查了没问题」是两件事 —— 缺了 `registered_names` 的时候，
#: 重名那一项是没查，不是"没有重名"。塌成"没发现"就是一句假话。
SKIP_NO_REGISTRY = "tool_name_collision"

#: 同上：`handler.py` 里有 `from X import *`，「声明的工具在不在」静态判不了
#: （还牵涉被导入模块的 `__all__`）。不登记这件事的话，`office_parser`
#: 会报 54 条假阳 —— 那条护栏看着有保护，其实在撒谎。
SKIP_STAR_IMPORT = "handler_tool_presence"

#: `load_strategy` 认这两个；不认的会落到 `load_skill`（eager） ——
#: 也就是启动时就 import handler，与 `on_demand` 的行为完全不同。
_LOAD_STRATEGIES = ("eager", "on_demand")


#: 技能可以 import 的框架面。不在这里的模块属于内部实现，升级时可能变化。
#: 体检按 AST 检查，命中只报 WARN，不拒装。
STABLE_SURFACE = frozenset({
    "core.extension",
    "core.workspace",
    "core.artifacts",
    "core.jobs",
    "core.i18n",
    "core.security",
    "core.chunker",
    "core.pdf_text_repair",
})


@dataclass(frozen=True)
class Finding:
    """一条体检结论。*code* 是稳定的机读码（UI / 反馈邮件 / 测试都认它）。"""

    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class Diagnosis:
    """一个 skill 包的体检结果。"""

    skill: str                       # 目录名（装载真的用它，不是 manifest 里的 name）
    path: str
    findings: tuple[Finding, ...] = ()
    #: 没跑的检查（见 `SKIP_NO_REGISTRY` 的注释）——
    #: 「没查」不许塌成「没问题」。
    skipped: tuple[str, ...] = ()
    #: manifest 里声明的工具名（诊断/反馈邮件里要带）
    tools: tuple[str, ...] = ()
    #: 包目录的指纹 `(mtime_ns, size)` —— 给"变了才体检"用
    stamp: tuple[int, int] = (0, 0)

    @property
    def ok(self) -> bool:
        """只有 `fail` 会让它不通过 —— `warn`/`info` 不算不兼容。"""
        return not any(f.severity == SEV_FAIL for f in self.findings)

    def by_severity(self, sev: str) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == sev)


# 单项检查（每个都是纯函数）

def _read_manifest(skill_dir: Path) -> tuple[dict | None, list[Finding]]:
    """读并粗验 `manifest.yaml`。返回 `(manifest, findings)`。"""
    out: list[Finding] = []
    path = skill_dir / "manifest.yaml"
    if not path.is_file():
        # 有 `SKILL.md` 的那种不是"看不见"。
        #   判据：只有 `SKILL.md` 的目录开机会被点名列出来
        #   （`discover_instruction_skills` + `instruction_skill_count`），
        #   而 `bus/loaders/skill_md.py` 的头一行就写着"只有 `SKILL.md`、没有
        #   `manifest.yaml` 的那种"是一等公民。
        #「没有它，框架看不见这个包」对"当工具包装"这个问题成立，
        #   对指令型 skill 不成立，降成 `warn`，话也说准。
        #   两条一起才对称：缺 `SKILL.md` 本来只判 `info`（"只影响元数据，不影响装载"）。
        if (skill_dir / "SKILL.md").is_file():
            out.append(Finding(SEV_WARN, "manifest_missing",
                               "没有 manifest.yaml —— 它是**指令型 skill**"
                               "（只有 SKILL.md），走的是另一条装载路，"
                               "**不能当工具包装**"))
        else:
            out.append(Finding(SEV_FAIL, "manifest_missing",
                               "缺 manifest.yaml —— 没有它，框架看不见这个包"))
        return None, out

    try:
        import yaml
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 坏 YAML 的异常类型很多
        out.append(Finding(SEV_FAIL, "manifest_unparsable",
                           "manifest.yaml 解不开（%s: %s）" % (type(exc).__name__, exc)))
        return None, out

    if not isinstance(manifest, dict):
        out.append(Finding(SEV_FAIL, "manifest_not_mapping",
                           "manifest.yaml 的顶层不是映射（是 %s）"
                           % type(manifest).__name__))
        return None, out

    if manifest.get("kind") != "skill":
        out.append(Finding(SEV_FAIL, "manifest_kind",
                           "kind 必须是 'skill'，实得 %r" % (manifest.get("kind"),)))

    name = manifest.get("name")
    if not name:
        out.append(Finding(SEV_WARN, "manifest_no_name",
                           "没有 name 字段（框架用**目录名** %r 装载，所以能跑；"
                           "但日志里会少一个名字）" % skill_dir.name))
    elif str(name) != skill_dir.name:
        out.append(Finding(SEV_WARN, "manifest_name_mismatch",
                           "manifest.name=%r 与目录名 %r **不一致** —— "
                           "框架按**目录名**装载（`load_skill_lazy` 用的是 "
                           "`skill_dir.name`），所以这个 name 只在日志里出现"
                           % (str(name), skill_dir.name)))

    # `draft: "false"` 是真值 —— 这是一个只会在静态检查里现形的坑。
    # `load_skill_lazy` 的调用方写的是 `if manifest.get("draft"): continue`，
    # 而 YAML 里带引号的 "false" 解出来是字符串，`bool("false") is True`
    #这个 skill 被当成草稿静默跳过，磁盘上躺着、框架里没有，
    # 而作者以为自己写的是"不要草稿"。
    draft = manifest.get("draft", False)
    if not isinstance(draft, bool):
        out.append(Finding(
            SEV_FAIL, "draft_not_bool",
            "draft 必须是**布尔**，实得 %r（%s）—— YAML 里带引号的 \"false\" "
            "解出来是**字符串**，而字符串是真值 ⇒ 这个 skill 会被当成草稿"
            "**静默跳过**" % (draft, type(draft).__name__)))

    strategy = manifest.get("load_strategy", "eager")
    if strategy not in _LOAD_STRATEGIES:
        out.append(Finding(
            SEV_WARN, "load_strategy_unknown",
            "load_strategy=%r 不认（只认 %s）—— 不认的值会**落到 eager**，"
            "也就是**启动时就 import handler**，与 on_demand 的行为不同"
            % (strategy, list(_LOAD_STRATEGIES))))

    return manifest, out


def _check_tools(skill_dir: Path, manifest: dict, registered: set[str] | None,
                 skipped: list[str]) -> tuple[list[Finding], tuple[str, ...]]:
    """`tools[]` 那一节。返回 `(findings, 声明的工具名)`。"""
    from bus.loaders.skill_loader import validate_tool_schema

    out: list[Finding] = []
    tools = manifest.get("tools")
    if not isinstance(tools, list) or not tools:
        out.append(Finding(SEV_FAIL, "tools_missing",
                           "没有 tools 列表（或它是空的）—— 一个工具都不注册的 "
                           "skill 等于不存在"))
        return out, ()

    names: list[str] = []
    seen: set[str] = set()
    for idx, tool in enumerate(tools):
        if not isinstance(tool, dict):
            out.append(Finding(SEV_FAIL, "tool_not_mapping",
                               "第 %d 个工具不是映射（是 %s）"
                               % (idx + 1, type(tool).__name__)))
            continue
        name = tool.get("name")
        if not name or not isinstance(name, str):
            out.append(Finding(SEV_FAIL, "tool_no_name",
                               "第 %d 个工具没有 name" % (idx + 1)))
            continue
        names.append(name)

        if name in seen:
            out.append(Finding(SEV_FAIL, "tool_duplicate_in_manifest",
                               "工具名 %r 在这个 manifest 里出现了**两次** —— "
                               "第二个会覆盖第一个（`register_tool` 是就地替换）"
                               % name))
        seen.add(name)

        try:
            validate_tool_schema(tool.get("inputSchema", {}))
        except ValueError as exc:
            out.append(Finding(SEV_FAIL, "tool_schema_invalid",
                               "工具 %r 的 inputSchema 不合法：%s" % (name, exc)))

        # 读家族工具必须声明输出形态
        #
        # 判据是 `core/tool_output_kind.py` 那个模块的整段理由，一句话：
        # 任何人（含模型）都能造包，而隔离面原本要求
        # "谁加工具谁去改 core/agent_loop.py" —— 那个人没有权限、也不该。
        #决定权搬进 manifest，这条就是逼人做那个决定的读者。
        #
        # 严重度是 WARN 不是 FAIL，而这是刻意的：
        #   · 装载不拒（拒了等于用户的工具凭空消失，而且看不见）
        #   · 创建拒绝（`core/skill_factory.py`，fail-closed —— 那里是"能不能落地"）
        #   · 体检报警（这里，人看得见）
        # 三层分工写在 `skill_loader._note_output_kind` 的 docstring 里。
        if is_read_family(name):
            _k = tool.get("output_kind")
            if _k is None:
                out.append(Finding(
                    SEV_WARN, "tool_output_kind_missing",
                    "工具 %r 是**读家族**（`get_/read_/search_/parse_/list_/…`），"
                    "却没声明 `output_kind` —— 会按 fail-closed 当 `prose` 处置"
                    "（隔离 + 多付约 20 tokens/次）。请声明：返回可能含**第三人自由文本**"
                    "（网页/邮件/文档正文）写 `prose`；只回路径/计数/短语写 `structured`"
                    % name))
            elif _k not in VALID_KINDS:
                out.append(Finding(
                    SEV_FAIL, "tool_output_kind_invalid",
                    "工具 %r 的 output_kind=%r 不是合法值（只认 %s）"
                    % (name, _k, " / ".join(VALID_KINDS))))

        # 每个工具都必须声明权限档
        #
        # 严重度是 FAIL 不是 WARN，而与上面那条 `output_kind` 故意不同。
        # 判据是猜错的代价方向：
        #   · `output_kind` 没声明，兜到 `prose`（fail-closed），方向是安全的
        #报 WARN 就够（多付约 20 tokens/次）。
        #   · `permission` 没声明，落到前缀猜，而一个名字像 `get_` / `list_` /
        #     `parse_` 的写工具会被猜成 `read`，并发跑 + 只读子 agent 放行它
        #     （两处真闸都拿 `read` 当真闸），而不会有任何东西红。
        #一个"猜错方向是坏的那一侧"的东西，不能只报 WARN。
        #
        # 与工厂故意不同：那边同一条直接拒（`skill_factory` 不变量④）。
        # 三层分工同 `output_kind`：装载出声 · 体检报告 · 工厂拒绝。
        _p = tool.get("permission")
        if _p is None:
            out.append(Finding(
                SEV_FAIL, "tool_permission_missing",
                "工具 %r **没声明 `permission`** —— 装载时它会落到**前缀猜**，"
                "而一个名字像 `get_`/`list_`/`parse_` 的写工具会被猜成 `read`："
                "那会让它**并发跑**、并被 `run_subagent(readonly=true)` **放行**"
                "（两处真闸都拿 `read` 当判据）。请三选一："
                "拿不准写 `write`。" % name))
        elif _p not in VALID_PERMS:
            out.append(Finding(
                SEV_FAIL, "tool_permission_invalid",
                "工具 %r 的 permission=%r 不是合法值（只认 %s）—— "
                "**非法值会被丢掉**（`core/tool_permission.py::declare`），"
                "于是它按「没声明」处置（前缀猜）。"
                % (name, _p, " / ".join(VALID_PERMS))))

        # 每个工具都必须声明 `destructive`
        #
        # 严重度是 FAIL —— 理由与 `permission` 那条同族，但更硬：
        # `permission` 的兜底是 `admin`（fail-closed，方向是安全的）；
        # 而 `destructive` 的兜底是 `false`（fail-open），没声明的工具会
        # 静默地不算不可逆，兜底闸
        # （`core/agent_loop.py::_destructive_gate_needed`）连问都不会问。
        #猜错的方向就是坏的那一侧，与 `output_kind`（兜 `prose`，安全侧，
        # 只报 WARN）是相反的。
        #
        # 与工厂故意不同：那边同一条直接拒（`skill_factory` 不变量⑤）。
        # 三层分工同 ③④：装载出声 · 体检报告 · 工厂拒绝。
        _d = tool.get("destructive")
        if _d is None:
            out.append(Finding(
                SEV_FAIL, "tool_destructive_missing",
                "工具 %r **没声明 `destructive`** —— 兜底闸读它判"
                "「要不要**先问用户**」，而这一根的兜底是 **`false`**（fail-open）："
                "没声明 = **静默地不算不可逆**，闸连问都不会问。"
                "（拿不准写 `true`：多问一次，代价看得见。）" % name))
        elif not isinstance(_d, bool):
            out.append(Finding(
                SEV_FAIL, "tool_destructive_invalid",
                "工具 %r 的 destructive=%r **不是布尔** —— "
                "非布尔值不会被收下"
                "（`core/tool_permission.py::declare_destructive`），"
                "于是它按「没声明」处置（fail-open）。"
                "⚠️ `\"true\"` 是字符串、`1` 是整数 —— **都不算**。"
                % (name, _d)))

    # 工具名撞车 —— 同一个名字被两个包声明
    #
    # 后果是静默顶掉：`register_tool` 是幂等替换，
    # 所以第二个包注册时不会报错、不会 400 —— 它只是把先注册的那份
    # 悄悄换掉，于是模型调到的可能是另一个包的实现。
    # （同名重复注册曾把 wire 撑成 40 条，上游硬 400；
    #   修完之后失败从"响亮"变成了"安静" —— 这才是现在要查它的理由。）
    #
    # 这一条不需要注册表 —— 两个 manifest 都躺在磁盘上，静态就判得了。
    #它覆盖的正是最常见的那种错：照模板拷一个包、只改了一半名字。
    #
    # 两条弯路，都记在这儿：
    #   ① 只看"注册表里有这个名"，体检一个已经生效的包时，
    #      它自己的工具都在注册表里，每个工具都与自己重名
    #      （`office_parser` 报 69 条假阳）。
    #   ② 改成"把本包声明的名字全排除"，排过头了 ——
    #      一个包抢别人的名字（它也声明 `add_block`）被一起放过，
    #      而那恰恰就是要抓的东西。
    #判据要看「别人声明了什么」，不是「我声明了什么」。
    siblings = _sibling_declared_names(skill_dir)
    for name in names:
        if name in siblings:
            out.append(Finding(
                SEV_FAIL, "tool_name_collision",
                "工具名 %r **另一个 skill 包也声明了** —— 两个包抢同一个名字，"
                "后注册的会**就地顶掉**先注册的（`register_tool` 是幂等替换，"
                "**不报错**），于是模型调到的可能是另一个包的实现" % name))

    # 注册表这一路只在调用方给了才查；没给，登记成"没跑"。
    # 判据：「没查」不许塌成「没问题」。
    #
    # 有一项是判不了的，如实写在这里（不塞进 `skipped`，因为它
    #    永远判不了，塞进去只会把那个列表变成噪声）：
    #    「skill 抢了一个框架级工具名（`run_command` / `read_file` …）」
    #    —— 注册表不记归属（`_LazyStub` 首次调用后把自己换成真 handler，
    #    那点信息就没了），所以分不出"这个名字是本包自己注册的"还是
    #    "框架早就注册的"。要真判得了，得在 `bus/registry.py` 里加一格
    #    "这个名字是谁注册的" —— 那是另一个改动，不塞进这里。
    if registered is None:
        if SKIP_NO_REGISTRY not in skipped:
            skipped.append(SKIP_NO_REGISTRY)

    return out, tuple(names)


#: `_sibling_declared_names` 的缓存：`(父目录, 包名) -> (指纹, 名字集合)`。
#: 每次体检都要读一遍兄弟包的 manifest（7 个小 YAML），缓存它让
#: `examine_all` 的代价从 O(n²) 回到 O(n)。
_sibling_cache: dict[tuple[str, str], tuple[tuple, frozenset]] = {}


def _sibling_declared_names(skill_dir: Path) -> frozenset:
    """别的 skill 包声明了哪些工具名（不含本包）。

    判据是它们的 manifest，不是注册表 —— 因为注册表分不出
    "这个名字是哪个包注册的"（`_LazyStub` 在首次调用后会把自己换成真 handler，
    归属信息就没了）。
    """
    key = (str(skill_dir.parent), skill_dir.name)
    stamp = tuple(sorted(_stamp(p) for p in discover(skill_dir.parent)
                         if p.name != skill_dir.name))
    hit = _sibling_cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]

    names: set[str] = set()
    try:
        import yaml
    except Exception:  # noqa: BLE001
        return frozenset()
    for p in discover(skill_dir.parent):
        if p.name == skill_dir.name:
            continue
        try:
            m = yaml.safe_load((p / "manifest.yaml").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(m, dict):
            for t in (m.get("tools") or []):
                if isinstance(t, dict) and isinstance(t.get("name"), str):
                    names.add(t["name"])
    out = frozenset(names)
    _sibling_cache[key] = (stamp, out)
    return out


def _module_names(tree: ast.Module) -> tuple[set[str], bool]:
    """handler.py 顶层能拿到哪些名字（静态；不执行任何东西）。

    返回 `(名字集合, 有没有 `from X import *`)`。

    为什么必须收 import 带进来的名字 —— 只看 `def` 的话，
    `office_parser` 会报 54 条假阳（它的 `handler.py` 用
    `from .word_tools import *` 把名字带进来，而 `_LazyStub` 的
    `getattr(module, name)` 是拿得到的）。
判据："这个名字在模块命名空间里吗"，不是"这个文件里有没有 `def` 它"。
    这是「选错了观测量」的又一次实例。

    `from X import *`，静态不可判（还牵涉被导入模块的 `__all__`），
    返回 `True`，调用方把它登记成「没查」，不是"找不到"。
    """
    names: set[str] = set()
    has_star = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    has_star = True
                else:
                    names.add(alias.asname or alias.name)
    return names, has_star


def _check_handler(skill_dir: Path, declared: tuple[str, ...],
                   skipped: list[str]) -> list[Finding]:
    """`handler.py`：在不在、`ast.parse` 通不通、声明的工具在不在里面。

    全程只用 `ast` —— 不 import、不 exec、不 compile。
    """
    out: list[Finding] = []
    path = skill_dir / "handler.py"
    if not path.is_file():
        out.append(Finding(SEV_FAIL, "handler_missing",
                           "缺 handler.py —— 工具注册得进去，但**第一次调用就会炸**"))
        return out

    try:
        source = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        out.append(Finding(SEV_FAIL, "handler_unreadable",
                           "handler.py 读不出来（%s: %s）"
                           % (type(exc).__name__, exc)))
        return out

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        out.append(Finding(SEV_FAIL, "handler_syntax_error",
                           "handler.py **语法都不通**（第 %s 行：%s）—— "
                           "`import` 它的那一刻就会 `SyntaxError`"
                           % (exc.lineno, exc.msg)))
        return out

    defined, has_star = _module_names(tree)

    if has_star:
        # 没查，不是"没问题"（同 `SKIP_NO_REGISTRY` 那条判据）。
        if SKIP_STAR_IMPORT not in skipped:
            skipped.append(SKIP_STAR_IMPORT)
        out.append(Finding(
            SEV_INFO, "handler_star_import",
            "handler.py 有 `from X import *` ⇒ 「声明的工具在不在」这一项"
            "**静态判不了**（已登记为未检查，**不是**「没问题」）。"
            "★ 想让它可判：把 `import *` 换成逐个具名 import"))
    else:
        for name in declared:
            if name not in defined:
                out.append(Finding(
                    SEV_FAIL, "handler_tool_not_found",
                    "manifest 声明了工具 %r，但 handler.py 的顶层**既没有它的 `def`、"
                    "也没有 import 它** —— `_LazyStub._load` 会抛 `AttributeError`。"
                    "★ 这条专抓「照模板改了名、漏改一处」" % name))

    if "init" not in defined:
        out.append(Finding(SEV_INFO, "handler_no_init",
                           "没有 `init(security_guard)` —— 能跑，"
                           "但 handler 拿不到安全核（要用到路径分级就得加）"))

    return out


def _check_stable_surface(skill_dir: Path) -> list[Finding]:
    """`handler.py` 引用了非稳定面的框架模块，WARN（不拒装）。"""
    path = skill_dir / "handler.py"
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return []          # 语法错误由别的检查报，这里不重复
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    bad = sorted({
        m for m in mods
        if m.split(".")[0] in ("bus", "channels", "providers")
        or ((m == "core" or m.startswith("core.")) and m not in STABLE_SURFACE)
    })
    if not bad:
        return []
    return [Finding(SEV_WARN, "unstable_import",
                    "引用了非稳定面的模块：%s。技能只应使用 %s"
                    % (", ".join(bad), ", ".join(sorted(STABLE_SURFACE))))]


def _check_files(skill_dir: Path) -> list[Finding]:
    out: list[Finding] = []
    if not (skill_dir / "SKILL.md").is_file():
        out.append(Finding(SEV_INFO, "skill_md_missing",
                           "缺 SKILL.md —— 只影响元数据/生态兼容，不影响装载"))
    if not (skill_dir / "__init__.py").is_file():
        out.append(Finding(SEV_INFO, "package_init_missing",
                           "缺 __init__.py —— Python 3 的命名空间包仍然能 import，"
                           "所以不致命"))
    return out


# 对外：体检一个包 / 体检全部

def _stamp(skill_dir: Path) -> tuple[int, int]:
    """包目录指纹 `(mtime_ns, size)` —— 只 stat，不读内容（"变了才体检"用）。"""
    latest, total = 0, 0
    try:
        for name in ("manifest.yaml", "handler.py", "SKILL.md", "__init__.py"):
            p = skill_dir / name
            try:
                st = p.stat()
            except OSError:
                continue
            latest = max(latest, st.st_mtime_ns)
            total += st.st_size
    except OSError:
        pass
    return latest, total


def examine(skill_dir: str | Path, *, registered_names: set[str] | None = None,
            ) -> Diagnosis:
    """体检一个 skill 包。*registered_names* 为 `None`，重名那一项没查。

    只读：本函数不写任何文件、不动注册表、不 import 任何 skill 代码。
    """
    path = Path(skill_dir)
    skipped: list[str] = []
    findings: list[Finding] = []
    tools: tuple[str, ...] = ()
    stamp = _stamp(path)

    if not path.is_dir():
        return Diagnosis(skill=path.name, path=str(path), stamp=stamp, findings=(
            Finding(SEV_FAIL, "not_a_directory", "不是一个目录"),
        ))

    manifest, manifest_findings = _read_manifest(path)
    findings.extend(manifest_findings)
    if manifest is not None:
        tool_findings, tools = _check_tools(path, manifest, registered_names,
                                            skipped)
        findings.extend(tool_findings)
        findings.extend(_check_handler(path, tools, skipped))
    findings.extend(_check_files(path))
    findings.extend(_check_stable_surface(path))

    # 顺序稳定：严重度优先，其次按 code —— 只为了让输出可复现（测试要钉它）
    rank = {SEV_FAIL: 0, SEV_WARN: 1, SEV_INFO: 2}
    findings.sort(key=lambda f: (rank.get(f.severity, 9), f.code, f.message))
    return Diagnosis(skill=path.name, path=str(path),
                     findings=tuple(findings), skipped=tuple(skipped),
                     tools=tools, stamp=stamp)


def discover(skills_dir: str | Path = "skills") -> list[Path]:
    """列出 `skills/` 下的候选包目录（已排序，可复现）。"""
    base = Path(skills_dir)
    if not base.is_dir():
        return []
    out = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith((".", "_", "$")):
            continue
        try:
            if entry.is_dir(follow_symlinks=False) and \
                    (Path(entry.path) / "manifest.yaml").is_file():
                out.append(Path(entry.path))
        except OSError:
            continue
    return out


def examine_all(skills_dir: str | Path = "skills", *,
                registered_names: set[str] | None = None) -> list[Diagnosis]:
    """体检全部包。排序与 `discover` 一致。"""
    return [examine(p, registered_names=registered_names)
            for p in discover(skills_dir)]


def fingerprint(skills_dir: str | Path = "skills") -> dict[str, tuple[int, int]]:
    """`{包名: 指纹}` —— 便宜的"变了没有"判据。

    与 `core/skill_index.build_skill_index()` 看同一个目录（它就是每轮
    现扫的那个），所以"发现更新"这件事不需要额外的 watcher 线程。
    它只 stat，不读内容 —— 每轮都要跑的东西，代价必须是真的便宜。
    """
    return {p.name: _stamp(p) for p in discover(skills_dir)}


#: 「上次看到的样子」—— 这不是会话态（所以它不进
#: `MemoryManager.new_session` 那串 purge：`_discovered` / `_todos` / `_reads`）。
#: 它是进程级的一次性缓存，唯一的用处是让"每轮比对"这件事便宜到可以忽略。
_last_seen: dict[str, tuple[int, int]] = {}
#: 每轮体检与 `/skill` 命令可能来自不同会话，互斥。
#: 锁只护"比指纹 / 更新表"这一小段（快活儿）；体检本体不在这里。
_watch_lock = threading.Lock()


def reset_watch() -> None:
    """清掉「上次看到的样子」—— 测试用，也给运维一个"下次全部重体检"的口子。"""
    _last_seen.clear()


def watch(skills_dir: str | Path = "skills", *,
          registered_names: set[str] | None = None) -> list[Diagnosis]:
    """每轮调的那一个：只体检变了的包，回不通过的和消失了的。

    判据是 `fingerprint()` 的前后对比 —— 没变就一个包都不体检
    （代价：一次 `scandir` + 每个包几次 `stat`）。
「发现更新」这件事不需要 watcher 线程，挂在本来就每轮跑的
    `build_skill_index()` 旁边就行（它扫的是同一个目录）。

    它不打印、不弹窗 —— 谁调谁决定怎么把结果说出去
    （日志是 `log_line`，UI 是 `compat_message(ui=True)`）。
    这样分的理由：「报到哪儿看」是 UI 层的事。先把判据做对，通道后接。
    """
    now = fingerprint(skills_dir)
    with _watch_lock:
        stale = [name for name, stamp in now.items() if _last_seen.get(name) != stamp]
        gone = [name for name in _last_seen if name not in now]
        _last_seen.clear()
        _last_seen.update(now)

    out: list[Diagnosis] = []
    for name in sorted(stale):
        d = examine(Path(skills_dir) / name, registered_names=registered_names)
        if not d.ok:
            out.append(d)
    for name in gone:
        # 消失了也要出声 —— "skill 没了"与"skill 没问题"长得一模一样，
        # 而那是最坏的一种静默（`convert_to_pdf` 就是这么丢的：文件没了，
        # 而没有任何东西在看）。
        out.append(Diagnosis(
            skill=name, path=str(Path(skills_dir) / name),
            findings=(Finding(SEV_WARN, "skill_disappeared",
                              "这个包**不见了**（上一轮还在）—— "
                              "是搬走了还是误删了？"),)))
    return out


# 文案（这段文案逐字固定；只有 `{name}` 是接进去的）

#: 这是给 UI 的文案，不许进 `print`。
#: `U+274C` 在 GBK 控制台上会 `UnicodeEncodeError`（`scripts/check_gbk_safe.py`
#: 的基线 60 不许增长，而它点名 `U+274C -> FAIL`）。
#:一份文案两种形态：UI 用下面这段（含符号），日志用 `log_line()`（纯 ASCII 码）。
_UI_BADGE = "❌️"


def compat_message(diag: Diagnosis, *, ui: bool = True) -> str:
    """体检不通过时的用户可见文案（原样照用户给的稿）。

    只在 `not diag.ok` 时才该被调用 —— 没问题就静默。

    两个形态，一个来源（"一份文案两种通道"）：
      · `ui=True`（默认）—— 带 `❌️`，给 UI / JSON 响应
      · `ui=False` —— 徽章换成 ASCII `[!]`，给控制台 / 日志
    带符号那个绝不能进 `print`：`U+274C` 在 GBK 控制台会
    `UnicodeEncodeError`，而 `check_gbk_safe.py` 的基线60 不许增长
    （它自己点名 `U+274C -> FAIL`）。
    代码里只有 `_UI_BADGE` 一处带符号 —— 想换符号只改那一个常量。
    """
    badge = _UI_BADGE if ui else "[!]"
    reasons = "\n".join("· %s" % f.message for f in diag.by_severity(SEV_FAIL))
    return (
        "Crannia 兼容性  {badge}\n"
        "我们的测试显示此插件在 Crannia 上的兼容性情况为[不支持]，"
        "此插件的某些或全部功能当前无法在 Crannia 上运行。\n"
        "\n"
        "{reasons}\n"
        "\n"
        "我们仍努力在 Crannia 上为此插件添加支持，错误插件：{name}\n"
        "\n"
        "对此结果有反馈？↗"
    ).format(badge=badge, reasons=reasons, name=diag.skill)


def log_line(diag: Diagnosis) -> str:
    """日志那一行 —— 纯 ASCII 码 + 中文，不含符号。

    与 `compat_message` 是同一条结论的两种形态，不是两份真相：
    码（`code`）是两者共用的那一份，谁也不用抄谁的措辞。
    """
    if diag.ok:
        return "[skill-doctor] OK   %s (%d tool(s))" % (diag.skill, len(diag.tools))
    codes = ",".join(f.code for f in diag.by_severity(SEV_FAIL))
    note = ""
    if diag.skipped:
        note = "  [skipped: %s]" % ",".join(diag.skipped)
    return "[skill-doctor] FAIL %s (%s)%s" % (diag.skill, codes, note)


def summary_line(diag: Diagnosis) -> str:
    """`/skill list` 那种一行摘要（不带符号）。"""
    if diag.ok:
        warn = len(diag.by_severity(SEV_WARN))
        return "OK" + ("（%d 条提示）" % warn if warn else "")
    return "不兼容：" + "、".join(f.code for f in diag.by_severity(SEV_FAIL))
