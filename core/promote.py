# -*- coding: utf-8 -*-
"""`/升格` —— 记忆 ↔ 指令文件之间那条单向提议通道。

## 它是什么

它不是工具、不是程序、不执行任何东西 —— 它就是一段提示词。

判据：它就是一个只返回一段文本的斜杠命令。
所以这里没有新机制：`pre_process` 把用户那句 /升格 换成下面这段文本，
不设 `stop`，主循环照常跑 —— 真正干活的是主 agent 自己
（读记忆 → 提议 → 等人逐条批 → 用通用文件工具写）。

## 它解决什么

东西放错层了。

- 事实躺在 `data/memory/facts/` 里，只有碰巧被检索到才生效 ——
  没检索到就等于不存在。
- 反过来，指令文件（`LEAF.md`）里背着「上次那个 PPT 做到第 3 页」，
  就是每一轮付费背一件下周没用的东西。

## 它是双向的（这是最容易漏的一半）

重复的，提议从长期记忆删；矛盾的，提议改旧的那一层。
所以不是"只升格不降级" —— 它本来就把"删旧的"一起提，
单调增长那半边不成立。

## 一条兑现得了的边界（实际，不是猜）

`workspace/LEAF.md` 与 `workspace/LEAF.local.md` 是可写的；
项目根的 `LEAF.md` / `LEAF.local.md` 判在 READ_ONLY 区
（`SecurityGuard.validate_path`，"cross-zone read-only area"，write 拒）。
提示词里明说这件事：项目根那两份的改动只能交给用户自己动手。
（判据：不承诺兑现不了的事 —— 让模型答应一件会失败的事，
比不让它答应更坏。）
"""

from pathlib import Path
from typing import Tuple

#: 命令名。它必须与 `pre_process._COMMANDS` 里那一条逐字相同 ——
#: `handler` 那一支按 `user_input[len(COMMAND):]` 切参数，差一个字就切错。
#: 「同一个命令名写在两张手抄表里」会各自漂移；现在派发面只有一张表
#: （`_COMMANDS`），`_FRAMEWORK_COMMANDS` 从它派生。
#:这里剩下的唯一约束是"这个名字要和表里那条一样"，而它由
#: `tests/test_approval_dispatch.py::test_every_command_points_at_a_real_handler`
#: 那一族兜着（名字在表里，有方法，有实现）。
COMMAND = "/remember"

#: 可写的那两份（在工作区里）与只读的那两份（项目根）。
#:
#: 这份分工由 `tests/test_promote.py` 拿真的
#: `SecurityGuard.validate_path` 逐条核一遍，而且核的是下面这两个常量
#: （不是一个写在提示词里、没人对账的句子）。哪天权限模型变了，那条会红 ——
#: 那时改这里，别改那条断言。
#:
#: 同一条测试还钉着「这两组合起来 == `prompt_assembler._LEAF_MD_PATHS`」——
#: 提示词谈的必须正好是装载器真读的那几份。
#: 这一段工作目录里的哪几个名字可写（名字而不是路径 —— 路径要现算）。
PROMOTE_WRITABLE_NAMES: Tuple[str, ...] = ("LEAF.md", "LEAF.local.md")

#: 上层目录（到项目根为止）里的哪些名字可读；改它们只能请用户自己动手。
PROMOTE_READONLY_NAMES: Tuple[str, ...] = ("LEAF.md", "AGENTS.md",
                                          "LEAF.local.md", "AGENTS.local.md")


def _default_base() -> Path:
    """没给 base 时用哪一段的目录 —— 与装载器同一个口径。"""
    from core.leaf_md import _start_dir
    return _start_dir()


def instruction_paths(base=None) -> Tuple[list, list]:
    """`(可写, 只读)` 两组绝对路径，以 *base*（这一段的工作目录）为准。

    写那一组只在这一段自己的目录里（它就是工作区 / 选定目录）；
      读那一组是上层链（到项目根为止）里的同族文件。
    这个划分必须与权限模型一致（`SecurityGuard.validate_path`）——
      `tests/test_promote.py` 拿真的 guard 逐条验，不承诺兑现不了的事。
    """
    from core.leaf_md import chain

    base_p = Path(base) if base else _default_base()
    writable = [base_p / n for n in PROMOTE_WRITABLE_NAMES]
    readonly = []
    for d in chain(base_p):
        if d == base_p:
            continue
        readonly.extend(d / n for n in PROMOTE_READONLY_NAMES)
    return writable, readonly


def _instruction_paths_block(base=None) -> str:
    """提示词里那段路径清单 —— 现算（跟着这一段的工作目录走），不手抄。"""
    w, r = instruction_paths(base)
    w_txt = " · ".join("`%s`" % p.as_posix() for p in w)
    r_txt = " · ".join("`%s`" % p.as_posix() for p in r) or "（这一层没有上层目录）"
    return ("  · %s（**这几份你能写**）\n"
            "  · %s（上层目录里的这几份**只能读** —— 改动只能请用户自己动手）"
            % (w_txt, r_txt))


_PROMPT_TEMPLATE = """[升格] 框架展开的命令：把「长期记忆」和「项目指令文件」之间**放错层**的东西理一遍。
**全程只提议 —— 在你把提案全部摆出来之前，一个字节都不许改。**

## 一、两个层
- **事实层**（长期记忆）：`memory_read` / `memory_search` 读得到的事实文件。
  ⚠️ 它**只能碰巧被检索到** —— 没被检索到，就等于不存在。
- **指令层**（项目指令文件）：用 `read_file` 读下面这几份，没有的就当空 ——
<<PATHS>>
  ⚠️ 指令层**每一轮都在**（就是 `<leaf_context>` 那一段）⇒
  放错东西的代价是**每一轮都在付**。

## 二、每一条该在哪（只有三格）
| 目的地 | 什么该放这儿 |
|---|---|
| `workspace/LEAF.md` | **在这个项目里干活的人都该遵守**的约定：非标准命令、与语言默认不同的风格、测试门道、坑 |
| `workspace/LEAF.local.md` | **只对这个用户 / 这台机器**成立的个人偏好 |
| **留在事实层** | 工作笔记、临时上下文、上一次做到哪、说不清放哪的 |

判据一句话：**「删掉这一行，我下次会不会做错事？」** —— 不会，就不该在指令层。

## 三、四类处置
- **升格** —— 事实层里有「每次都该这么做」的东西 ⇒ 提议写进指令层，
  **逐字给出要写进去的那段话**（不是"建议补充测试命令"，而是直接写出那行）
- **清理（重复）** —— 两层说了同一件事 ⇒ 提议删**事实层**那份
  （指令层那份每轮都生效，留着才安全）
- **清理（矛盾）** —— 两层对不上 ⇒ 提议改**旧的那一层**（按时间判断哪个新），
  并注明哪个更新
- **说不清** ⇒ 归 Ambiguous，**别替用户决定**

## 四、怎么报（四组，没内容的那组也要写「无」）
- **Promotions**：拟写进指令文件的**原话** + 一句「为什么它属于指令文件」
- **Cleanup**：拟删 / 拟改的条目 + 为什么要动它
- **Ambiguous**：一句话说清你分不清的是哪一点，**直接问用户**
- **No action**：看过但不用动的（**没动也是一个结论**，别省这一组）

## 五、三条硬规矩
1. **不许改任何文件**（记忆、指令文件都不行）—— 提案要用户**逐条点头**；
   点头之后你再用通用文件工具写，而且**只写 `workspace/` 下那两份**。
   项目根那两份在只读区、你改不了 —— 那两份的改动**只能请用户自己动手**。
2. **凭据一个字都不许出现**：口令 / API key / token / 连接串 ——
   既不许写进记忆，也不许写进指令文件。
3. 用户**在这次对话里**说过「别记 / 忘掉」的，一个字都不许提。
"""

#: 不在 import 期展开 —— 路径那段跟着"这一段的工作目录"走，
#: 在 import 时算死就等于钉在进程启动时那个目录上（之后会撒谎）。
PROMPT_HEAD = _PROMPT_TEMPLATE.split("<<PATHS>>")[0]
PROMPT_TAIL = _PROMPT_TEMPLATE.split("<<PATHS>>")[1]


def expand(rest: str = "", base=None) -> str:
    """/升格 的载荷 —— 提示词本体（`rest` = 用户在命令后面补的叮嘱）。

    *base* = 这一段对话的工作目录（`pre_process` 从会话里取）。
    用户补的话原样附在最后，不改写、不丢：例如
    /升格 重点看看 PPT 那几条，那半句就是这次整理的焦点。
    """
    prompt = PROMPT_HEAD + _instruction_paths_block(base) + PROMPT_TAIL
    tail = (rest or "").strip()
    if not tail:
        return prompt
    return (prompt
            + "\n## 六、用户这次特别叮嘱的\n"
            + tail + "\n")
