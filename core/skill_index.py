"""技能索引 —— 「第三档」（索引常驻 + 正文按需拉）的那半。

目标：把常驻工具表从 4,486 tokens 压到最小集，用一份技能粒度
的索引替代它 —— 索引 6 条只要 241 tokens（`scripts/measure_request.py`）。

三条硬要求
----------
1. 载体是 user 侧消息 —— 因此不违反本仓那条禁令
   「不要在 system prompt 里镜像工具目录」（那条说的是别在 system prompt 里）。
2. 只发增量（整表替换 + sha256 摘要，无变化不发）。
3. 索引与 wire 必须联动（「技能开关打开且工具可用」才列）——
   索引里承诺的东西，当轮必须真能调到。

第 3 条只做到一半，务必看清方向：
   索引里出现的技能 → 它下面的工具都能调到（`get_tool_handler` 查全量注册表）
不会承诺调不到的东西。
   反方向则漏（没列进索引的工具也能调）—— 一个判定同时管
   呈现与执行是后续的事，本轮不做，只标注。

不进 `messages`、不落库：它是每轮现算的瞬时块，附在最后一条 user 消息
   后面。落库会让"注入物被当成对话内容"的那族问题开门。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from core.content_sanitizer import sanitize_text
# 把一段框架文本挂到最后一条 user 消息尾巴上 ——
# 带图那一轮那条消息是块列表，拼接与追加块是两种形态，判据在那一处。
from core.image_input import append_text_block

#: 每条索引里描述的字符上限。这里取得短 —— 因为我们的 skill 描述本身
#: 就短，而且索引只需要让人知道"这有这类能力"，细节交给 `search_tools` 拉。
_INDEX_DESC_CHARS = 100


# 技能清单的预算
#
# 判据：预算 = 窗口 × 4 × 0.01。
#超预算的动作：`descriptions will be truncated` + 一行 warn
#   （提示条数、字符数与预算，并建议用户用 `/skills` 关掉一些，或调大
#   `skillListingBudgetFraction`）。
#   条数一个都不砍 —— 减哪些 skill 是用户的决定，不是它悄悄少列几个。
#
# 接通程度如实记：真窗口是调度器那边拼进 usage 的（`context_report`），
#   这一层拿不到，不传就落兜底 200,000（，预算 8,000 字符）。
#   两种口径都没超（本机 2,562），它今天是保险丝，不是开关。
#   「把真窗口接进来」是待办，别把这里读成"已经按真窗口算了"。

SKILL_LISTING_DESC_CAP = 1536            # 每个 skill 的描述上限
SKILL_LISTING_BUDGET_FRACTION = 0.01
SKILL_LISTING_BUDGET_MULTIPLIER = 4
SKILL_LISTING_FALLBACK_WINDOW = 200_000


def skill_listing_budget(window_tokens: int | None = None) -> int:
    """清单的字符预算：`窗 × 4 × 0.01`，下限 1。

    环境变量 `SLASH_COMMAND_TOOL_CHAR_BUDGET` 优先 —— 这是有意认的外部变量名，
    与 `retention.SETTING_KEY` 的 `cleanupPeriodDays` 同一类：让已经配过它的人
    不必改第二遍。配不到就按窗口算。
    读不到 / 不是正整数，落到兜底窗口 —— 不抛
    （同 `_skill_states`：一个坏环境变量不该让整段索引消失）。
    """
    import os
    env = os.environ.get("SLASH_COMMAND_TOOL_CHAR_BUDGET")
    if env:
        try:
            return max(1, int(env))
        except (TypeError, ValueError):
            pass
    win = window_tokens if (isinstance(window_tokens, int) and window_tokens > 0) \
        else SKILL_LISTING_FALLBACK_WINDOW
    return max(1, int(win * SKILL_LISTING_BUDGET_MULTIPLIER
                       * SKILL_LISTING_BUDGET_FRACTION))


def split_line(line: str) -> tuple[str, str]:
    """一行索引，`(名字那半, 描述那半)`。

    两种行形状的分隔符都是 `": "`（工具提供者 `- 名 (N 个工具): 描述` ·
    指令型 `- 名（… 或用户打 `/名`）: 描述`），而描述里也可能有 `": "`
取第一个。没有分隔符，整行当名字那半（描述是空串）。
    这个假设有机械读者：`tests/test_skill_listing_budget.py` 拿真的
    `_index_groups()` 输出逐行验它（假设漂了当场红）。
    """
    head, sep, desc = line.partition(": ")
    return (line, "") if not sep else (head + ": ", desc)


def fit_listing(lines: List[str], budget: int) -> List[str]:
    """超预算，砍描述，不砍条数。

    名字一个都不许少 —— 不变量是「索引 ∪ 常驻 == 注册表」，
       而"某个工具在索引里有条目"靠的就是这一行还在。砍条数 = 悄悄开一个洞。
两步：
       ① 还有余地，每行按额度砍描述（尾加 `…`）
       ② 连"只剩名字"都超，就只剩名字（预算真不够时，名字仍然要留着）
    """
    total = sum(len(x) for x in lines)
    if total <= budget:
        return lines

    heads = [split_line(x)[0] for x in lines]
    names_only = sum(len(h) for h in heads)
    if budget <= names_only:
        print("[skill_listing] 超预算（%d 行 %d 字符 > %d）—— **只列名字**"
              % (len(lines), total, budget))
        return heads

    per = max(1, (budget - names_only) // max(1, len(lines)))
    per = min(per, SKILL_LISTING_DESC_CAP)          # 每个 skill 的描述上限
    out = [h + _cut(x[len(h):], per) for x, h in zip(lines, heads)]
    print("[skill_listing] 超预算：%d 行 %d 字符 > %d —— **描述被砍短**"
          "（要更宽：设置 `skillListingBudgetFraction` / 环境变量 "
          "`SLASH_COMMAND_TOOL_CHAR_BUDGET`）" % (len(lines), total, budget))
    return out


def _cut(desc: str, limit: int) -> str:
    """把描述砍到上限之内。 `…` 也算一个字符 —— 不然就超一个。

    `desc[:limit] + "…"` 会每行多 1 个字符，3 行就超 3，
    `fit_listing` 出来的东西仍然超预算（而它会"看起来对"）。
    """
    if len(desc) <= limit:
        return desc
    if limit <= 1:
        return "…"[:limit]
    return desc[:limit - 1].rstrip() + "…"


# 索引只管非常驻的那部分 —— 不变量是
# 「索引 ∪ 常驻 == 注册表」，不是「索引 == 注册表」
#
# 只扫 `skills/*/manifest.yaml` 的索引只知道清单里写过的能力。
# 后果：`web_search`/`web_fetch` 被移出常驻后
# 索引里连提都没提，于是完全不可达 —— 而索引是模型"知道自己有什么"的
# 唯一来源。
# 任何注册了、但不在任何清单里的工具（外部 MCP 服务器、
# 新加的框架级工具）都会静默地变成不可达。
#
# 修法是把注册表接进来做一次兜底扫描：凡是注册了、
#   ① 不被任何清单覆盖、② 又不在常驻集里的，必须有条目。
#
# 有意的收窄：数据源没有全换成注册表 —— 因为常驻工具的 schema
# 每轮都在 `tools=` 参数里，索引再写一遍就是同一件事付两次钱
# （改措辞必须重量的那条判据）。所以索引的职责是非常驻的剩余部分，
# 而"不会漏"由下面这条不变量保证：
#
#     索引 ∪ 常驻 == 注册表
#
# 机械读者：`tests/test_skill_index.py::test_index_covers_every_registered_non_resident_tool`。
# 覆盖集与渲染行由同一趟循环产出（`_index_groups`）——
# 分成两处算，渲染漏一行而覆盖照旧声明它覆盖了，那条不变量就会看着绿。

#: `registry` 参数的哨兵：用真实的注册表（生产路径）。
#: 传 `None` = 关掉兜底扫描（只要清单那半）；传一个模块 = 用那个注册表。
_AUTO = object()

#: 兜底条目的措辞 —— 只在真有这类工具时才进索引（今天一条都没有，零成本）。
_MCP_GROUP_DESC = "外部 MCP 服务器提供的工具，用 search_tools 检索"
_OTHER_GROUP_DESC = "本索引未能归类到清单的工具，用 search_tools 检索"


def _load_registry():
    """拿注册表 —— 不在模块顶层 import（`bus` 会反过来 import `core`）。

    拿不到就返回 `None`：索引退化成"只有清单那半"，而不是让整轮对话崩掉
    （同 `agent_loop` 里那个 `try` 的判据：索引是附加物，不该连累对话）。
    """
    try:
        import bus.registry as _R
        return _R
    except Exception:
        return None


def _leftover_groups(reg, covered: set) -> List[tuple]:
    """注册表里没被任何清单覆盖、又不在常驻集的工具 —— 按来源分组。

    分组而不是逐个列名：索引的粒度是组（与 skill 条目同形），
    一张 30 个工具的外部 MCP 服务器只占一行。
    """
    try:
        registered = list(reg.list_tools())
    except Exception:
        return []
    resident = set(getattr(reg, "_CORE_TOOLS", ()) or ())
    #: 会话条件工具（`_SESSION_TOOLS`）
    #: 和常驻一样不进兜底桶。判据：索引是全体会话共读的一段文本，
    #: 而"按会话算"的东西写进去就是一句假承诺 —— 普通会话里模型看得到
    #: 那个名字、调不动它（执行侧会拒）。它们的可达性由工具表按会话保证。
    #: 名单不在这里重抄一份（读常量）：抄一份就是会各自漂的形状。
    session_only = set(getattr(reg, "_SESSION_TOOLS", ()) or ())
    rest = [n for n in registered
            if n not in covered and n not in resident and n not in session_only]
    if not rest:
        return []

    buckets: Dict[str, dict] = {}
    for name in rest:
        parts = name.split("__")
        if name.startswith("mcp__") and len(parts) >= 3:
            key, desc = f"MCP 服务器 {parts[1]}", _MCP_GROUP_DESC
        else:
            key, desc = "其他已注册工具", _OTHER_GROUP_DESC
        buckets.setdefault(key, {"desc": desc, "names": set()})["names"].add(name)

    return [
        (f"- {key} ({len(b['names'])} 个工具): {b['desc']}", set(b["names"]))
        for key, b in sorted(buckets.items())
    ]


def _index_groups(skills_dir: str | Path = "skills", registry=_AUTO) -> List[tuple]:
    """索引的每一行 + 它覆盖了哪些工具名 —— 渲染与覆盖的唯一产出点。

    覆盖集只算注册表里真有的名字：清单里声明了但没注册成功的工具，
    承诺它是撒谎（`test_index_never_promises_tools_that_are_not_registered`
    盯的就是这个），所以它既不算覆盖、也不会出现在不变量里。
    """
    root = Path(skills_dir)
    groups: List[Tuple[str, set]] = []
    if not root.is_dir():
        return groups

    # 注册表只解析一次 —— 清单那半（算覆盖集）与兜底那半（找剩余）
    # 必须看同一个注册表，否则两边能得出互相矛盾的答案。
    reg = _load_registry() if registry is _AUTO else registry
    try:
        registered = set(reg.list_tools()) if reg is not None else set()
    except Exception:
        registered = set()

    for entry in sorted(root.iterdir()):
        manifest_path = entry / "manifest.yaml"
        if not (entry.is_dir() and manifest_path.exists()):
            continue
        try:
            manifest = yaml.safe_load(
                manifest_path.read_text(encoding="utf-8")
            ) or {}
        except Exception:
            continue
        if manifest.get("draft") or manifest.get("kind") != "skill":
            continue
        tools = manifest.get("tools") or []
        n_tools = len(tools)
        desc = " ".join((manifest.get("description") or "").split())
        if len(desc) > _INDEX_DESC_CHARS:
            desc = desc[:_INDEX_DESC_CHARS].rstrip() + "..."
        names = {
            t.get("name") for t in tools
            if isinstance(t, dict) and t.get("name") and t.get("name") in registered
        }
        groups.append((f"- {entry.name} ({n_tools} 个工具): {desc}", names))

    if reg is not None:
        covered = set()
        for _, names in groups:
            covered |= names
        groups.extend(_leftover_groups(reg, covered))

    # 指令型 skill 也进索引
    #
    # 它们不是工具（没有 `manifest.yaml`、不注册 handler），覆盖集是空集
    #那两条工具不变量（"每个工具要么常驻、要么搜得到"）一个字都不动。
    #
    # 这一行是它们唯一能进「模型面」的地方。只扫 `manifest.yaml` 时
    #   模型面是空的 —— 指令型 skill 没有 manifest。
    #
    # 拉正文不需要新增任何东西：文件原语全部常驻，
    #   模型读到"有这么个工作方法"，自己 `read_file` 去拉。
    #   这正是「只在需要时才加载被引用的资源」的一般化。
    #   所以这一条不改"不加 `Skill` 工具"的裁决 ——
    #   它加的是一行索引，不是一个常驻 schema。
    #
    # 代价：这是每请求的税（索引挂在最后一条 user 消息的尾巴上）。
    #   所以行要短。
    for line in _instruction_skill_lines(root):
        groups.append((line, set()))
    return groups


#: 进模型面（技能索引）的来源。
#:
#: 别的 agent 目录里的技能默认只进 `/` 命令面：
#: 技能索引挂在每个用户轮上（每行按请求计费），而那一类技能是
#: "用户自己知道要用什么"的工具型技能 —— 不需要模型每轮都看见。
#: 要改这个决定，改这一个集合（判据只有一处）。
_MODEL_VISIBLE_ORIGINS = frozenset({"repo", "agents", "claude", "explicit",
                                    "plugins"})


def _instruction_skill_lines(root: Path) -> List[str]:
    """指令型 skill 的索引行 —— 四态过滤照走命令面那一个函数。

    不许在这儿重新判一次四态（那是第二套真相源）：问的是
    `instruction_command_map` —— 与 `/` 候选、与派发同一个判定。
    `reserved` 传真的框架命令集：否则一个撞名的 skill 会**出现在索引里
    而实际被拒** —— 那是不许承诺兑现不了的东西。
    """
    try:
        from bus.loaders.skill_md import instruction_command_map
        from core.pipeline.stages.pre_process import _FRAMEWORK_COMMANDS
        from core.skill_sources import skill_roots
        cmds = instruction_command_map(skill_roots(root), reserved=_FRAMEWORK_COMMANDS)[0]
    except Exception:
        # 拿不到就不加这一段（索引是附加物，不该连累对话 —— 同
        # `build_skill_index` 那条兜底的判据）。
        return []
    out = []
    for name, s in sorted(cmds.items()):
        # 外部 agent 的技能只进 `/` 命令面，不进模型面（见上面那个集合）。
        if getattr(s, "origin", "repo") not in _MODEL_VISIBLE_ORIGINS:
            continue
        desc = " ".join((s.description or "").split())
        if len(desc) > _INDEX_DESC_CHARS:
            desc = desc[:_INDEX_DESC_CHARS].rstrip() + "..."
        out.append(
            "- %s（**工作方法**，不是工具 —— 需要时自己 `read_file` 拉它那份正文，"
            "或用户打 `%s`）: %s" % (s.name, name, desc))
    return out


def build_skill_index(skills_dir: str | Path = "skills", registry=_AUTO, *,
                      window_tokens: int | None = None) -> str:
    """扫描 `skills/*/manifest.yaml` + 注册表兜底，生成技能粒度的索引文本。

    从清单现算，不是手写常量 —— 手写的目录会与注册表漂移，
    那正是「在 system prompt 里镜像工具目录」被删掉的原因。它还再兜一层底：
    注册表里那些既不被清单覆盖、又不在常驻集里的工具必须有条目
    （见本文件顶部那段：不变量是「索引 ∪ 常驻 == 注册表」）。

    没有条目时返回空串（调用方据此跳过注入）。
    """
    groups = _index_groups(skills_dir, registry)
    if not groups:
        return ""
    # 预算（见上面那一段）：超了砍描述、不砍条数
    #   （条数那条是「索引 ∪ 常驻 == 注册表」的不变量，砍它等于悄悄开一个洞）。
    lines = fit_listing([line for line, _ in groups], skill_listing_budget(window_tokens))
    # 框架块不变量：条目描述来自
    #   `skills/*/manifest.yaml`（框架代码之外），先打码再返回 ——
    #   `<skill_index>` 包裹由 `append_skill_index` 事后加，不受影响。
    return sanitize_text("\n".join(lines))


def index_coverage(skills_dir: str | Path = "skills", registry=_AUTO) -> set:
    """索引声明覆盖了哪些工具名 —— 给那条不变量用的读者。

    它与渲染文本同源（`_index_groups` 一趟循环同时产出两者）：
    分成两处算的话，渲染漏一行而覆盖照旧声明它覆盖了，
    不变量就会看着绿（一份没有读者的清单会烂）。
    """
    return set().union(*(names for _, names in _index_groups(skills_dir, registry)))


def append_skill_index(messages: List[dict], index_text: str) -> List[dict]:
    """把索引附在最后一条 user 消息后面，返回新的列表。

    三个刻意的选择：

    * 不改原列表 —— 传进来的 `messages` 是要落库/继续用的那一份。返回副本，
      调用方只把副本发给 LLM，原表一个字不动（注入物进了 messages 就会被
      压缩器/滑窗/落库当成对话内容）。
    * 附在最后一条 user 消息后，而不是新加一条 user 消息 —— 严格的
      provider（Anthropic）要求 user/assistant 交替；两条连续的 user 轮次
      是没必要的风险。
    * 找不到 user 消息时返回原列表（不硬塞）—— 宁可不注入，也不改角色序列。
    """
    if not index_text:
        return messages

    block = (
        "\n\n<skill_index>\n"
        "以下是你当前环境里可用的**能力索引**（由注册表实时生成，不是用户的话，"
        "不用回答它）。现有工具不够用时，用 `search_tools(query)` 按需检索 —— "
        "检索到的工具下一轮即可调用。\n"
        f"{index_text}\n"
        "</skill_index>"
    )

    # 挂载那一步在 `core/image_input.append_text_block`
    # —— 带图那一轮的最后一条 user 消息是块列表，而
    # `not isinstance(content, str) -> return messages` 会把整支跳过
    #每一个图片轮上技能索引静默不挂（模型看不到自己有哪些工具），
    # 而没有任何地方会红。修在一处（那里同时管着记忆索引），
    # 两处调用 —— 再写第二份就是把这个洞再挖一遍。
    return append_text_block(messages, block)


def skill_index_stats(skills_dir: str | Path = "skills") -> Dict[str, int]:
    """索引的规模 —— 给自检与测试用（几条、多少字符）。"""
    text = build_skill_index(skills_dir)
    return {
        "entries": text.count("\n- ") + (1 if text.startswith("- ") else 0),
        "chars": len(text),
    }
