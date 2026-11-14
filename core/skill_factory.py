"""给模型的 meta-skill —— 「帮我做个 skill」那条路。

与 `core/skill_doctor.py` 是一对：那边读，这边写。
（刻意分成两个模块：体检器有一条机械读者钉着"它不写任何东西"，
把写操作混进去等于把那条读者作废。）

## 两条不变量 —— 安全全押在它们上面

```
① 拒绝已存在的名字      —— 模型不许改现成的 skill
② 强制 draft: true      —— 模型设不了这个字段
```

这两条撑着的是同一件事：模型写进去的东西不会自己生效。

- `draft` 的 skill 根本不会被装载 —— `load_skill_lazy` 的调用方写着
  `if manifest.get("draft"): continue`（`scripts/measure_request.py:90` 逐字同一条）
后果是「磁盘上多一个不生效的目录」，不是"执行了模型的代码"。
- 通用 `write_file` 碰不到 `skills/`（`SecurityGuard` 的 `allowed_dirs=["./workspace"]`）
这里是唯一一条能往 `skills/` 写东西的路。

生效那一步永远是人按的：`/skill check` → `/skill approve`
（与"人手工放包走 `/skill reload`"是同一条纪律：没有一条路能让新代码自己走进来）。

## 三道闸，顺序是刻意的

```
① 名字合法 + 目录不存在        （不变量 ①）
② 先写进 skills/_wip_<随机>/   —— 在临时目录里跑体检
③ 体检过了才 move 成 skills/<name>/
```

先体检、后落盘：写坏了不留痕迹 —— 体检不过时 `skills/` 下一个字节都没多。
临时目录用 `_wip_` 前缀，与 `office_parser` 的工作文件同一个前缀 ——
`core/skill_doctor.discover()` 会跳过 `_` 开头的条目，所以它不会被当成一个包，
也不会污染"兄弟包"的重名判断。
（代价：进程中途死掉会留一个 `_wip_*` 空目录。看得见，且 `discover` 会跳过它。）

## 这不是安全边界

它挡的是"模型顺手造了个 skill"，不是"模型决意要往 `skills/` 写代码"。
真正的强制力是：模型没有别的写入口 + draft 不会被 import + 人要按一下。
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import uuid
from pathlib import Path

# 不变量 ③ 的判据来源（读家族 + 合法取值）。
# 与体检器看同一处 —— 两份会漂移，而两边看着都"对"。
from core.tool_output_kind import VALID_KINDS, is_read_family  # noqa: E402
from core.tool_permission import VALID_PERMS  # noqa: E402

#: skill 目录名：小写字母开头，只含小写/数字/下划线，3–40 字符。
#: 与 `scripts/create_skill.py::_validate_name` 同一条精神（那是个交互式脚手架，
#: 它校验的是人输入的名字；这里校验的是模型给的）。
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")

#: 不许用的名字 —— 与框架自己的目录/机制撞名会让事情变得很难查。
_RESERVED = frozenset({"skills", "__pycache__", "_mcp_builtin_"})

#: 单个文件的字节上限。超了拒，不截断 —— 截断出来的是语法不通的 Python，
#: 而那正是体检器要抓的东西（让它自己去抓，不如在这里就说清楚）。
_MAX_FILE_BYTES = 64 * 1024

#: 一个 skill 最多几个工具。`office_parser` 是 69 —— 给足余量，
#: 但它挡住"模型一口气生成五百个工具"那种。
_MAX_TOOLS = 80


def _err(message: str, hint: str = "", **extra) -> dict:
    out = {"success": False, "error": message}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def _skills_dir() -> Path:
    """用户自己装的技能目录（数据根下的 `skills/`）。

    草稿写这里，不写随包的只读资产（安装版里那个在安装目录，
    升级/卸载会整棵删掉 —— 写进去等于用户的心血下次升级就没了）。
    开发树里两个根是同一个目录，行为不变；安装版里才分开。
    """
    from core import paths as _paths
    return _paths.user_skills_dir().resolve()


def _validate_name(name: str) -> str | None:
    """返回错误说明；`None` = 通过。"""
    if not name:
        return "没有给 name"
    if not isinstance(name, str):
        return "name 要是字符串，实得 %s" % type(name).__name__
    if name in _RESERVED:
        return "`%s` 是框架自己用的名字" % name
    if not _NAME_RE.match(name):
        return ("名字要**小写字母开头**、只含小写/数字/下划线、3–40 字符"
                "（实得 %r）—— 它是 Python 包名，装的时候会被 import" % name)
    return None


def create_skill(name: str = "", manifest_yaml=None, handler_py=None,
                 skill_md=None) -> dict:
    """造一个草稿 skill。它不会当场生效 —— 要人 `/skill approve`。

    参数：
      · `name`          目录名（= Python 包名）
      · `manifest_yaml` `manifest.yaml` 的全文（`kind: skill` + `tools: [...]`）
      · `handler_py`    `handler.py` 的全文（每个工具一个顶层 `def`）
      · `skill_md`      可选；`SKILL.md` 的内容

    `draft`、`kind`、`name` 三个字段由本工具写死，模型给什么都会被覆盖 ——
    这不是"不信任格式"，是不变量 ②（模型设不了 `draft`）。

    用之前请先 `read_file("SKILL_SPEC.md")` 看清格式；也先 `search_tools`
    看看有没有现成的 —— 别造重复的。
    """
    from core import skill_doctor as SD

    bad = _validate_name(name)
    if bad:
        return _err(bad, hint="换一个名字再试。")

    base = _skills_dir()
    if not base.is_dir():
        return _err("找不到 skills/ 目录（%s）" % base,
                    hint="这个工具要在框架的工作目录里跑。")

    # 不变量 ①：不许改现成的
    final = base / name
    if final.exists():
        return _err(
            "`skills/%s/` **已经存在** —— 这个工具只造新的，不改现成的。"
            % name,
            hint=("要改一个已有的 skill，让**人**去改它的文件"
                  "（模型没有 `skills/` 的通用写权限，这是刻意的）。"
                  "要造新的，换个名字。"))

    if not manifest_yaml or not handler_py:
        return _err("manifest_yaml 与 handler_py 都是必填的（给全文，不是摘要）",
                    hint="先 read_file(\"SKILL_SPEC.md\") 看格式。")

    for label, text in (("manifest_yaml", manifest_yaml),
                        ("handler_py", handler_py),
                        ("skill_md", skill_md or "")):
        n = len(str(text).encode("utf-8"))
        if n > _MAX_FILE_BYTES:
            return _err("%s 有 %.1f KB，超过 %d KB 上限" %
                        (label, n / 1024, _MAX_FILE_BYTES // 1024),
                        hint="拆成更小的 skill，或者把大块内容留给工具的参数。")

    # 不变量 ②：强制 draft（以及 kind/name —— 那两个是它自己的一部分）
    try:
        import yaml
        manifest = yaml.safe_load(str(manifest_yaml))
    except Exception as exc:  # noqa: BLE001
        return _err("manifest_yaml 不是合法的 YAML（%s: %s）"
                    % (type(exc).__name__, exc),
                    hint="注意缩进；字符串里的冒号要加引号。")
    if not isinstance(manifest, dict):
        return _err("manifest_yaml 的顶层要是映射，实得 %s"
                    % type(manifest).__name__)

    tools = manifest.get("tools")
    if not isinstance(tools, list) or not tools:
        return _err("manifest 里没有 tools 列表 —— 一个工具都没有的 skill 等于不存在")
    if len(tools) > _MAX_TOOLS:
        return _err("%d 个工具超过上限 %d 个" % (len(tools), _MAX_TOOLS))

    # 不变量 ③：读家族工具必须声明输出形态
    #
    # 前两条（拒绝重名 / 强制 draft）撑的是「模型写进去的东西不会自己生效」。
    # 这一条撑的是另一件事：模型造的工具必须能被隔离面分类。
    #
    # 背景（`core/tool_output_kind.py` 有完整版）：隔离面原本是 `core/agent_loop.py`
    # 的硬编码名单，前提是"谁加工具谁去那儿登记"—— 而造包的人
    # （含模型）没有那个权限，也不该有。决定权搬进 manifest，
    # 这里就是那个决定的守门人。
    #
    # fail-closed：没声明就拒，不是"兜个默认值放它过去"。
    # 与体检器故意不同：那边同一条只报 `WARN`（不阻塞加载），
    #    因为既有包不该因为一条新规矩就装载不了。三层分工：
    #    装载出声 · 体检报告 · 工厂拒绝。
    undeclared = [
        t.get("name") for t in tools
        if isinstance(t, dict) and t.get("name")
        and is_read_family(t["name"])
        and t.get("output_kind") not in VALID_KINDS
    ]
    if undeclared:
        return _err(
            "这些读家族工具**没声明 `output_kind`**：%s" % ("、".join(undeclared)),
            hint=("读家族 = 名字以 get_/read_/search_/summarize_/parse_/list_/web_ 开头。"
                  "每一个都要声明它**返回什么形态**：\n"
                  "  · `output_kind: prose`      —— 可能含**第三人自由文本**"
                  "（网页/邮件/文档正文/命令输出）⇒ 会被隔离\n"
                  "  · `output_kind: structured` —— 只回路径/计数/短语/短串"
                  "⇒ 豁免隔离，**你要能说得出为什么**\n"
                  "整条工具定义像这样：\n"
                  "  - name: get_todo_items\n"
                  "    output_kind: structured\n"
                  "    description: ...\n"
                  "⚠️ 没声明**不是**「默认安全」—— 是「**没做决定**」，所以这里拒。"))
    # 不变量 ④：每个工具都必须声明权限档
    #
    # ③ 撑的是"模型造的工具必须能被隔离面分类"；④ 撑的是它必须能被
    # 那两处真闸分类（并发预取 · 只读子 agent）。
    #
    # 为什么 ④ 比 ③ 宽（③ 只管读家族）：`read` 是唯一有闸含义的那一档，
    # 而一个名字恰好落 `get_` / `list_` / `parse_` 的写工具会被前缀猜成 `read`
    #并发跑 + 只读子 agent 放行它，而不会有任何东西红。
    # 别的档猜错了只是"少并发一次 / 多问一次"，代价看得见。每一个工具都要表态。
    #
    # 与体检器故意不同：那边同一条报 `SEV_FAIL`（但不阻塞加载 ——
    #    既有包不该因为一条新规矩就装载不了）。三层分工同 ③：
    #    装载出声 · 体检报告 · 工厂拒绝。
    bad_perm = [
        t.get("name") for t in tools
        if isinstance(t, dict) and t.get("name")
        and t.get("permission") not in VALID_PERMS
    ]
    if bad_perm:
        return _err(
            "这些工具**没声明 `permission`（或值不合法）**：%s" % ("、".join(bad_perm)),
            hint=("每个工具都要表态它**能不能改东西**，三选一：\n"
                  "  · `permission: read`  —— 只查，**不改任何东西**\n"
                  "  · `permission: write` —— 会改东西（写盘 / 改共享状态）\n"
                  "  · `permission: admin` —— 系统级（今天只有 run_command / "
                  "run_subagent / request_cross_zone_search 三个）\n"
                  "⚠️ **拿不准写 `write`** —— `read` 会被两处真闸当真判据用"
                  "（并发预取 + 只读子 agent），写错成 `read` 是**危险的那一侧**；\n"
                  "   写错成 `write` 只是「少并发一次」，代价看得见。\n"
                  "整条工具定义像这样：\n"
                  "  - name: get_todo_items\n"
                  "    permission: read\n"
                  "    description: ...\n"
                  "⚠️ 没声明**不是**「默认安全」—— 是「**没做决定**」，所以这里拒。"))
    # 不变量 ⑤：每个工具都必须声明 destructive
    #
    # ④ 撑的是"模型造的工具必须能被那两处真闸分类"；⑤ 撑的是它能被
    # 兜底闸分类（`core/agent_loop.py::_destructive_gate_needed`：
    # `destructive` 且不在 `_APPROVAL_CAPABLE_TOOLS`，先问用户）。
    #
    # 为什么这一条在工厂里非拒不可（比 ③④ 更要紧）：
    # 那一根轴的兜底是 `false`（fail-open） —— 一个没人看过的工具会
    # 静默地不算不可逆，闸连问都不会问。承重的只有三道：
    # AST 反查（`tests/test_destructive_declared.py`）· 棘轮 · 这里。
    # 而 ③④ 的兜底都是 fail-closed（`prose` / `admin`），漏了只是"多付一层代价"。
    #
    # **`isinstance(..., bool)` 不是 `in (True, False)`** —— 前者才会拒掉
    # `"true"` / `1` / `yes`（`yes` 会被 PyYAML 读成 `True` 是碰巧对，
    # 不是保证）。与 `core/tool_permission.py::declare_destructive` 同一条判据。
    #
    # 与体检器故意不同：那边同一条报 `SEV_FAIL`（但不阻塞加载）。
    #    三层分工同 ③④：装载出声 · 体检报告 · 工厂拒绝。
    bad_dest = [
        t.get("name") for t in tools
        if isinstance(t, dict) and t.get("name")
        and not isinstance(t.get("destructive"), bool)
    ]
    if bad_dest:
        return _err(
            "这些工具**没声明 `destructive`（或值不是布尔）**：%s" % ("、".join(bad_dest)),
            hint=("每个工具都要表态它**改得回来吗**（与 `permission` 正交："
                  "一个工具可以 `write` 而完全可逆）：\n"
                  "  · `destructive: true`  —— **不可逆**（删掉就没有了 / 发出去收不回来 /\n"
                  "     结果里有一部分是**调用者没要求的**损失）\n"
                  "  · `destructive: false` —— 改得回来\n"
                  "⚠️ **拿不准写 `true`** —— 这一根的兜底是 `false`（**fail-open**）⇒\n"
                  "   一个没人看过的工具会**静默地不算不可逆**，闸连问都不会问；\n"
                  "   写错成 `true` 只是「多问一次」，代价看得见。\n"
                  "★ 写到 `true` 而它**没有自己的审批发射点**的话，框架会**先问用户**。\n"
                  "整条工具定义像这样：\n"
                  "  - name: get_todo_items\n"
                  "    permission: read\n"
                  "    destructive: false\n"
                  "    description: ...\n"
                  "⚠️ 没声明**不是**「默认可逆」—— 是「**没做决定**」，所以这里拒。"))
    forced = []
    for key, want in (("kind", "skill"), ("name", name), ("draft", True)):
        if manifest.get(key) != want:
            forced.append("%s: %r -> %r" % (key, manifest.get(key), want))
            manifest[key] = want
    manifest.setdefault("load_strategy", "on_demand")
    manifest.setdefault("version", "0.1.0")

    manifest_text = yaml.safe_dump(manifest, allow_unicode=True,
                                   default_flow_style=False, sort_keys=False)

    # ② 先写进临时目录，在那里跑体检
    #
    # 临时目录放在 `skills/` 里面：体检器的"兄弟包重名"判据是
    #   `skill_dir.parent` 下的其余包 —— 放外面就找不到兄弟，那一项会假绿。
    # 名字用 `_wip_` 前缀：`SD.discover()` 跳过 `_` 开头的条目，
    #   它不会被当成一个包，也就不会污染别人（更不会污染它自己）的重名判断。
    tmp_dir = base / ("_wip_%s_%s" % (name, uuid.uuid4().hex[:8]))
    try:
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "manifest.yaml").write_text(manifest_text, encoding="utf-8")
        (tmp_dir / "handler.py").write_text(str(handler_py), encoding="utf-8")
        (tmp_dir / "__init__.py").write_text("", encoding="utf-8")
        if skill_md:
            (tmp_dir / "SKILL.md").write_text(str(skill_md), encoding="utf-8")

        registered = None
        try:
            from bus.registry import list_tools
            registered = set(list_tools())
        except Exception:  # noqa: BLE001
            registered = None      #体检器登记成「没查」，不塌成「没问题」

        diag = SD.examine(tmp_dir, registered_names=registered)
        if not diag.ok:
            # 回执里必须写模型给的那个名字，不是临时目录名。
            # 体检器按 `skill_dir.name` 取名，而这里传进去的是
            # `_wip_ghost_2bbdd243` —— 直接回给模型的话，用户看到的是
            # 「错误插件：_wip_ghost_2bbdd243」。
            diag = dataclasses.replace(diag, skill=name, path=str(final))
            return _err(
                "这个 skill **没通过体检**，没有落盘（`skills/` 下**一个字节都没多**）。",
                hint="按下面几条改，再调一次 `create_skill`。",
                report=SD.compat_message(diag, ui=False),
                findings=[{"code": f.code, "message": f.message}
                          for f in diag.by_severity(SD.SEV_FAIL)])

        # ③ 过了才搬过去
        os.replace(tmp_dir, final)
    except OSError as exc:
        return _err("写文件失败（%s: %s）" % (type(exc).__name__, exc))
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print("[skill] create_skill 造了草稿 %s（%d 个工具）%s"
          % (name, len(tools), ("，已强制 " + "；".join(forced)) if forced else ""))

    return {
        "success": True,
        "skill": name,
        "path": str(final).replace("\\", "/"),
        "tools": [t.get("name") for t in tools if isinstance(t, dict)],
        "forced": forced,
        "draft": True,
        "note": ("**它现在不生效** —— `draft: true` 的 skill 不会被装载。"
                 "让人在对话里发 `/skill check %s`，看过之后再 `/skill approve %s`。"
                 % (name, name)),
        "warnings": [{"code": f.code, "message": f.message}
                     for f in diag.by_severity(SD.SEV_WARN)],
    }


#: `create_skill` 的 schema。常驻工具（`_CORE_TOOLS`）。
#: 它每个字都按每条请求计费（改措辞必须重量一次）。
CREATE_SKILL_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "create_skill",
    "description": (
        "造一个新的 skill（技能）。★ 它只产出**草稿**：人看过 `/skill check` 再 "
        "`/skill approve` 才生效 —— 所以你不会当场改变框架的行为。"
        "★ 造之前先 `read_file('SKILL_SPEC.md')` 看格式，再 `search_tools` 确认没有现成的。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "目录名，也是 Python 包名：小写字母开头，3–40 字符",
            },
            "manifest_yaml": {
                "type": "string",
                "description": "manifest.yaml 全文（kind: skill + tools: [...]）",
            },
            "handler_py": {
                "type": "string",
                "description": "handler.py 全文（每个工具一个顶层 def）",
            },
            "skill_md": {
                "type": "string",
                "description": "可选：SKILL.md 的内容（给人看的说明）",
            },
        },
        "required": ["name", "manifest_yaml", "handler_py"],
    },
}
