"""skill 级「引导片段」的载体。

## 它治的病

模型在"够不着的时候不去造路、做完不去核"的任务上会失败 ——
同一个 bug 砸在两处，一处被查出来修掉，另一处没人回头看。

判据：给「通用规则」，不给「场景映射」。
「不确定，先查」覆盖所有不确定（✅）；「PDF 乱码，试 `0x303E`」（❌）。

## 载体为什么是这个形状（这是本条最要紧的一段）

`SKILL.md` 根本不进模型上下文 —— 它只是元数据/生态兼容
（`core/skill_doctor.py` 逐字「缺 SKILL.md —— 只影响元数据/生态兼容，
不影响装载」；`core/skill_factory.py` 逐字「SKILL.md 的内容（给人看的说明）」）。
在它之前，我们没有一个"只在那个 skill 被激活时才进上下文"的位置。

三条硬约束把形状钉死了：

1. 不进 system prompt —— 那是纯税里唯一会长胖的一格，而且改它 =
   改写缓存前缀（那一次 45x）。同类实现没有一家把这类
   索引/纪律放 system：它们一律挂在消息侧。
2. 不进技能索引 —— 索引挂在每请求，往那儿加字 =
   每条请求都付。而且索引的职责是"让人知道有什么能力"，不是"教怎么用"。
3. 不做成一个工具 —— 「模型未必找得到这个工具」。
   实证：`format_excel_range` 检索 rank 93/158。
要靠搜才能找到的引导等于没有。

落地形状：挂载点与技能索引逐字相同（`core/image_input.append_text_block`
—— 附在最后一条 user 消息的尾巴上，副本，`messages` 一个字不动，
不进历史、不落库、不参与压缩）。

现在两半分触发：

| 那一半 | 什么时候挂 |
|---|---|
| 通用纪律（`COMMON_DISCIPLINE`） | 常开 —— 从第 0 轮起就在 |
| 领域权威（`skills/<名>/GUIDANCE.md`） | 仍然只在该 skill 被激活时 |

## 「激活」的判据

判据只有一句：

```
激活 = 该 skill 的工具被 search_tools 发现过（bus.registry._discovered）
```

**不能拿 `get_active_tool_names(session_id)`（常驻 ∪ 已发现）当判据** ——
29 个常驻工具里 13 个属于 skill（`office_parser` 4 · `file_parser` 7）
常驻工具从第 0 轮就在那个集合里，那两个 skill 每个会话都算"已激活"
引导变成常开，正是要躲的每请求税。

也不能把"被调用过"算进去 —— 模型第一步调 `list_workspace_files`
（属 `skill:file_parser`、常驻），从那以后每一条请求都挂着PDF 那一片领域纪律，
而那一跑做的是 Excel；而且整跑里模型一次都没调 search_tools
`office_parser`（真正对口那个）从头到尾没激活。
根因：`list_workspace_files` / `deliver_file` / `delete_files` /
`search_chunks` / `summarize_document` / `parse_txt` / `request_cross_zone_search`
这些是工作区管道，不是"PDF 专长" —— 它们常驻正是因为它们通用。
拿"被调用"当判据，等于让每个会话都激活 `file_parser`。

为什么"被调用"那一半整个可以删掉：**要调一个非常驻工具，它必须先在
`_discovered` 里**（没呈现的工具不许跑），`被调用` ⊆ `被发现`，
那一半从来没有独立贡献过，却带来上面那个过火的洞与一格会话态。
删掉它：少一张表、少一次登记、少一个夹具，判据一句话。

这一版的边界：模型若从不 `search_tools`
（拿 `run_command` + 自写 Python 把 36 个 Excel 工具绕过去），
领域片段一次都不会出现。它帮得到"模型已经在用这个 skill 的工具"的任务
（PDF/PPT 那些必须搜的），帮不到"压根不进来"的任务。

通用纪律现在总在（不看激活），"从不 `search_tools`"那种任务也读得到它。
剩下的边界是领域权威那一片（"这个领域的权威在哪"）—— 那一片仍然要激活。
判据没变松，只是通用那一半不再寄存在激活上。

## 与前缀缓存的关系（为什么挂载点必须是"最后一条 user 消息的尾巴"）

本块在第 k 轮引入、之后保持（sticky：`_discovered` 与会话态都不自己过期）
第 k+1 轮的 payload == 第 k 轮的 payload + 后缀，前缀只增不改。
唯一付一次重算的时刻是激活集变化那一轮 —— 那不可避免（状态真的变了），
且分叉点落在那条 user 消息上（技能索引本来就在那儿）。

## 内容分两片

| 片段 | 谁写 | 内容 |
|---|---|---|
| 通用纪律 | 本模块的常量，一处 | 不确定，先查 · 先小样本 · 别动原件 |
| 领域权威 | 每个 skill 自己写（`skills/<名>/GUIDANCE.md`） | 「在这个领域，权威是 X」 |

为什么必须分两片：「权威在哪」是领域知识，通用规则给不出来 ——
Excel 的权威是类型库、企微的是 `A2AGetCard`、PDF 的是规范，每个领域不一样。
（常见做法正是这个结构：通用纪律归一处、领域路由各自一份。）

领域片段只写"权威在哪"，不许写"遇到 X 就做 Y" —— 那是场景映射，
换个任务就哑火，正是要禁的那一种。

机械读者：`tests/test_skill_guidance.py`（本块的形状与触发）·
`tests/test_prompt_block_sanitize.py`（内容来自框架之外，必须打码）·
`tests/test_content_sanitizer.py`（`skill_guidance` 进 `RESERVED_TAGS`，两向）·
`tests/test_stream_brakes.py`（两条路锁步 · 不引入新分叉点 · 中途激活只延长）。
没有会话态，`core/state_registry.py` 里没有它那一行 ——
状态全部住在 `bus.registry._discovered`，而那一格本来就有主
（`forget_discovered`）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from core.content_sanitizer import sanitize_text
# 挂载点与技能索引共用同一个函数（做成独立工具等于没有）。
#   它同时管着"带图那一轮最后一条 user 消息是块列表"那个坑。
from core.image_input import append_text_block

#: 领域片段的文件名 —— 每个 skill 自己写一片，放在它自己的目录里。
#:
#: 与 `SKILL.md` 是两件事：那一份是给人看的说明、不进上下文
#: （`skill_doctor.py`）；这一份进模型上下文，只在那个 skill 被激活时。
#: 所以内容判据也不同：那一份可以长篇大论，这一份越短越好（按条计费）。
DOMAIN_FILE = "GUIDANCE.md"

#: 通用纪律 —— 一处写、所有 skill 共享（定稿的措辞）。
#:
#: 两处刻意的选用：
#: ① 「搜索权威」写成「去找权威来源（…）」—— 前者容易被读成"上网搜"，
#:    而那次实跑里对方查的是本机的 Excel 类型库。**把"权威在哪"列出来，
#:    它才落得了地**（这也是为什么领域那半边必须由 skill 自己写）。
#: ② 「小样本尝试」与「别损坏原件」合成一段 —— 分开写，模型很可能只做前半
#:    （试了，但试在原件的）。这两句是一件事的两半，拆开就失效。
COMMON_DISCIPLINE = """\
不确定的时候，先把它弄清楚再动手。
- 不认识的常量 / 名字 / 行为 —— 先去找权威来源（官方文档 · 类型库 · 规范 · 源码本身），别凭记忆填。
- 拿不准一处改动会怎样 —— 先复制一份，在上面小范围试一次。
- 原件不拿来试错。
- ★ 交出去之前，对**产物**至少**求值**核对一次 —— 不是"文件能打开"，是"里面的东西对不对"。
- ★★ 问环境之前先认环境：路径与命令是平台相关的 —— 拿别处的路径去问
  （`ls /usr/share/fonts` 在 Windows 上）得到的是空结果，而空结果像"没有"。
- ★ 说结论要**指回出处**（哪份文件 / 哪一项 / 哪一段）；**没读到的那些也要说**
  ——「这份里没有 X」本身就是结论的一部分。
- ★ 模拟的 / 近似的 / 没做成的，都要**写明**，别用「完成」盖住；做不到的子项要
  **走到用户面前**，不是只留在自己的脚本注释里。"""

#: `<skill_guidance>` 包裹里的头一句 —— 纯框架文案（告诉模型它是谁在说话）。
_HEADER = "以下是当前所用技能自带的工作纪律（框架给的，不是用户的话，不用复述它）。"

#: 领域片段的字符上限。超了截断并出声 —— 它是按请求计费的，
#: 而"某个 skill 的 GUIDANCE.md 写了五千字"这件事没有任何东西会红。
_MAX_DOMAIN_CHARS = 1200

#: **不要加一格 `_used_skills`（"本会话调用过哪些 skill 的工具"）**：
#: ① 它让 `list_workspace_files`（`skill:file_parser`、常驻、每个会话第一步都调）
#: 把 PDF 那一片纪律挂到每一个会话上；
#: ② 它是冗余的 —— 要调一个非常驻工具，它必须先在 `_discovered` 里。
#:判据回到一句话：被 `search_tools` 发现过。理由与边界见模块 docstring。


def _load_registry():
    """拿注册表 —— 不在模块顶层 import（`bus` 会反过来 import `core`）。

    拿不到就返回 `None`：本块退化成"没有激活的 skill"（一个字都不挂），
    而不是让整轮对话崩掉（同 `build_skill_index` 那个 `try` 的判据：
    引导是附加物，不该连累对话）。
    """
    try:
        import bus.registry as _R
        return _R
    except Exception:
        return None


def _skills_of(names) -> Set[str]:
    """把一批工具名映射成 skill 名（只认 `skill:` 前缀）。

    框架工具（owner = `framework`）与 MCP（`mcp:`）不触发任何片段 ——
    它们的纪律不在这里（框架自己的话在 `_CORE_INSTRUCTIONS`；
    MCP 服务器没有 `GUIDANCE.md` 这个概念，硬编一个等于替别人写纪律）。
    """
    reg = _load_registry()
    if reg is None:
        return set()
    out: Set[str] = set()
    for n in names or ():
        try:
            owner = reg.get_tool_owner(n)
        except Exception:
            continue
        if isinstance(owner, str) and owner.startswith("skill:"):
            out.add(owner[len("skill:"):])
    return out


def active_skills(session_id: str) -> List[str]:
    """本会话已激活的 skill 名（有序 —— 缓存前缀要跨进程一致）。

    判据只有一句：该 skill 的工具被 `search_tools` 发现过。
    它不含"被调用过"那一半，而且那是有意的 —— 理由与那条边界
    （模型若从不 `search_tools`，这里就永远为空）见模块 docstring。
    """
    reg = _load_registry()
    if reg is None:
        return []
    try:
        return sorted(_skills_of(reg.get_discovered(session_id)))
    except Exception:
        return []


def _skills_root() -> Path:
    """`skills/` 的根 —— 与 `core/skill_index.py` 看同一个目录（相对 cwd）。

    相对路径是继承来的约定：`build_skill_index()` 也是这么找的，
    而生产里 cwd 就是仓库根。写成 `Path(__file__).parent.parent` 会多出
    第二套"仓库根在哪"的口径（一个口径一个地方）。
    """
    from core import paths as _paths
    return _paths.asset_dir("skills")


def _read_domain(skill: str) -> str:
    """读一个 skill 的领域片段。没有 / 读不动，空串（不阻断、只少一片）。

    不去猜、不去兜底内容 —— 「权威在哪」是领域知识，
    框架替它编一句就变成"框架在写它不懂的领域"，那正是场景映射的入口。
    """
    path = _skills_root() / skill / DOMAIN_FILE
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8").strip()
    except Exception as exc:  # 判据 4：不静默，但也不阻断对话
        print(f"[skill_guidance] 读不动 {path}（{type(exc).__name__}: {exc}）"
              f" —— 这个 skill 本轮不带领域片段")
        return ""
    if len(text) > _MAX_DOMAIN_CHARS:
        print(f"[skill_guidance] {path} 有 {len(text)} 字符，超过 "
              f"{_MAX_DOMAIN_CHARS} —— 已截断（它是按条计费的，请写短些）")
        text = text[:_MAX_DOMAIN_CHARS].rstrip() + "..."
    return text


def build_skill_guidance(session_id: str) -> str:
    """生成包裹里面那一份文本。

    通用纪律常开 —— 它不依赖"有 skill 被激活"。

    为什么：常驻的技能工具永远不进 `_discovered` —— 它们本来就在
    `tools=` 里，模型可以直接调，从头到尾不碰 `search_tools`
    （那道闸只拦"没呈现的"）。实际常驻 28 个里 11 个属于 skill
    （`file_parser` 7 · `office_parser` 4），**走常见路径（读 PDF / 写文档 / 写 Excel）
    的会话，那段纪律一个字都挂不上，而通用纪律正属于「通用**」那一类。
只常开「通用纪律」（+176 tokens/请求），两片领域段仍按需。

    激活判据一个字没改（还是"被 `search_tools` 发现过"）—— 改的只是
    "通用那一半要不要等激活"。`active_skills()` 为空时，领域片段仍然不挂。

    返回的是内层（不含 `<skill_guidance>` 包裹），与
    `core/skill_index.build_skill_index` 同形 —— 打码只打内容、
    包裹由 `append_skill_guidance` 事后加（框架块不变量）。

    内容里有框架之外来的东西（`skills/<名>/GUIDANCE.md` 是包作者写的）
必须过 `sanitize_text`。机械读者按 AST 查本函数里有没有这个调用
    （`tests/test_prompt_block_sanitize.py` 的分类普查）。

    缓存前缀：通用纪律从第 0 轮就在，它那一段字节
    每条请求都一样；领域片段是追加在它后面的，所以第 k 轮新挂上领域片段时，
    前面的字节一个不动。

    如实记一条 6 个 token 的代价：领域片段插在闭合标签之前，
    新挂上那一刻，上一轮缓存里那 6 个 token 的 `\\n</skill_guidance>` 会被改写
    （不是严格的"只增不改"）。一次会话每个新激活的 skill 只付一次，且付的
    只有尾巴那几个字节 —— 没做成两块包裹，是因为块普查按 tag 认产者函数
    （`tests/test_prompt_block_sanitize.py` 的 `_SANITIZED_BLOCKS`），第二个产者
    会让那条不变量变模糊，而省下的是 6 个 token。这是一次明算过的取舍，不是没想到。
    多个领域片段同时激活时按名字序排 —— 若后激活的那个名字更靠前，
    它的那一段会被插在中间（这是既有边界）。
    """
    names = active_skills(session_id)
    parts = [COMMON_DISCIPLINE]
    for name in names:
        domain = _read_domain(name)
        if domain:
            parts.append(f"【{name} 这个领域的权威在哪】\n{domain}")
    return sanitize_text("\n\n".join(parts))


def append_skill_guidance(messages: List[dict], inner: str) -> List[dict]:
    """把引导片段附在最后一条 user 消息后面，返回新的列表。

    与 `core/skill_index.append_skill_index` 逐字同判据（三条刻意的选择：
    不改原列表 / 附在最后一条 user 消息后而不是新加一条 / 找不到就返回原列表），
    落点也共用 `core/image_input.append_text_block`。
    """
    if not inner:
        return messages
    block = (
        "\n\n<skill_guidance>\n"
        f"{_HEADER}\n"
        f"{inner}\n"
        "</skill_guidance>"
    )
    return append_text_block(messages, block)


def guidance_stats(session_id: str) -> Dict[str, int]:
    """本块的规模（给自检与测试用）—— 与 `skill_index_stats` 同形。"""
    text = build_skill_guidance(session_id)
    return {"chars": len(text), "skills": len(active_skills(session_id))}


# ----------------------------------------------------------------------
# 自检：包作者写歪了要看得见（不求全，只求"别静默"）
# ----------------------------------------------------------------------

#: 场景映射的常见口吻 —— 只用来提醒，不做判定。
#:
#: 它是一条"出声器"，不是一条闸。判据：
#: `检测可以错，打码不能错` —— 这里错了的代价是一行提示，
#: 而它要是去改正文或拒装载，代价就是包作者的正常文字被我们弄坏/挡掉。
#: 所以它只 print，返回值也只是给测试看的。
_SCENE_MAPPING_HINTS = re.compile(r"(遇到.{0,12}(就|则|要)|如果.{0,12}(就|则))")


def scan_domain_hints(skills_dir: Optional[Path] = None) -> List[str]:
    """扫一遍所有 `GUIDANCE.md`，把读起来像场景映射的行列出来。

    写给下一个写 `GUIDANCE.md` 的人看的：不是报错，是「这一句是不是
    "遇到某数就试某数"？如果是，换成"权威在哪"」。
    """
    root = skills_dir or _skills_root()
    out: List[str] = []
    if not Path(root).is_dir():
        return out
    for entry in sorted(Path(root).iterdir()):
        path = entry / DOMAIN_FILE
        try:
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            if _SCENE_MAPPING_HINTS.search(line):
                out.append(f"{entry.name}/{DOMAIN_FILE}:{i}: {line.strip()}")
    return out
