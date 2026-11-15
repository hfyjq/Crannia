"""工具输出形态的声明与分类。

## 它治什么 —— 一个守卫被热加载开了一个洞

隔离面是一张硬编码名单（`core/agent_loop.py` 的
`_ISOLATED_DOC_TOOLS` / `_TRUSTED_OUTPUT_TOOLS`），它的前提是
「读家族的工具集构建期就固定，谁加工具谁就去 `core/agent_loop.py` 做一次分类决策」。

`create_skill` + 热加载把那个前提弄坏了：**任何人（含模型）都能往
`skills/` 放包、声明任意工具名**。于是有人造了一个 `get_todo_items`，
`tests/test_content_sanitizer.py` 的守卫红，而那个人没有权限、也不该
去改 `core/agent_loop.py`。

两种结局都不好：测试长期红着，人开始忽略它（护栏腐烂）·
或者图省事塞进豁免表，隔离被绕过。

这是结构性的，不是"测试写错了"。

## 修法：形态由 manifest 声明，按声明分类

```yaml
tools:
  - name: get_todo_items
    output_kind: structured   # 结构化短数据（路径/计数/短语）—— 装不下一条指令
    # 或 output_kind: prose   # 可能含第三人自由文本（网页/邮件/文档正文）
```

| `output_kind` | 落到哪 | 代价 |
|---|---|---|
| `prose` | `_ISOLATED_DOC_TOOLS` 那一侧（sanitize + 包 `<user_document trust="untrusted">`） | 约 20 tokens/次 |
| `structured` | `_TRUSTED_OUTPUT_TOOLS` 那一侧（显式豁免，manifest 里要写得出理由） | 0 |

## 外部 MCP：没有 manifest，按同一条 fail-closed 处置

`mcp__<服务器>__<工具>` 的返回是第三方写的自由文本，而 MCP 工具没有
manifest 可以声明形态。判据与下面 ① 同向：没声明就按 `prose`（隔离）处置。
唯一的例外是用户在 `data/mcp_servers.json` 里给那台服务器写的
`trusted_output: true` —— 它落到的仍是同一条 `declare`，不另开一个
"可信服务器"集合。不看服务器自己的注解：那是被保护对象自己说的话。

## 三条不许回退的规矩

### ① 没声明，按 `prose` 处置（fail-closed），且出声

判据：「读不到，默认；读到了但做不到，拒」。这里是"读不到，走安全那一侧"。
这一条不是"读家族默认隔离"那种单靠兜底的方案 —— 区别在
声明是强制的：`create_skill` 没声明就拒（`core/skill_factory.py`）、
体检器会报（`core/skill_doctor.py`）、机械读者会红
（`tests/test_content_sanitizer.py`）。「没做决定」与「决定隔离」
分得开 —— 而单靠兜底会让守卫变成恒真。

### ② 声明是给老实作者的，不是安全边界

恶意 skill 可以声明 `structured` 而实际返回网页正文，绕过隔离。
但这不是新问题：skill 是可信写的（人要按 `/skill approve`），
而 `draft` / approve 挡的是「没人按过就生效」，不是「作者是恶意的」。
真恶意的 skill，`import` 之后本来就是进程内的 Python，隔离拦不住它。
本模块解决的是「让守卫继续有意义」，不是「让 skill 变安全」。

### ③ 隔离的方向必须有机械读者

「没声明走哪一侧」这件事改动一下就静默改变安全姿态，
`tests/test_content_sanitizer.py` 有一条专门钉它（负对照：把默认翻成
`structured`，红）。
"""

from __future__ import annotations

from typing import Dict, Optional

#: 输出形态的两个取值。只有两个 —— 判据是"装不装得下一条可执行的指令"，
#: 不是"它安不安全"（任何第三方字符串都能藏指令，见模块 docstring ②）。
KIND_PROSE = "prose"
KIND_STRUCTURED = "structured"
VALID_KINDS = (KIND_PROSE, KIND_STRUCTURED)

#: "读文本"家族 —— 名字以这些前缀开头的工具必须被分类过。
#:
#: 它一处定义：产品代码（体检器 / 工厂）与测试都从这里取，
#: 不在测试里另留一份。
#:
#: 它是一张"够用"的表，不是穷举 —— `run_command` / `grep` / `run_subagent`
#: 都不落在任何一个前缀上，所以它们只有手动登记（那条路的名字与理由
#: 写在 `core/agent_loop.py` 的 `_ISOLATED_DOC_TOOLS` 里）。别把这张表读成全集。
READ_FAMILIES = ("get_", "read_", "search_", "summarize_", "parse_",
                 "list_", "web_")

#: 外部 MCP 工具的公开名前缀（`mcpx/client.py::_public_tool_name`）。
#:
#: 内置的 `web_search` / `web_fetch` 用直接名注册，不落在这里 ——
#: 它们本来就在 `core/agent_loop.py::_ISOLATED_EXTERNAL_TOOLS` 里，
#: 由框架自己点名，不需要靠前缀。
MCP_PUBLIC_PREFIX = "mcp__"


def is_read_family(tool_name: str) -> bool:
    """名字落不落在"读文本"家族里。"""
    return tool_name.startswith(READ_FAMILIES)


def is_external_mcp(tool_name: str) -> bool:
    """这是不是一个外部 MCP 服务器的工具（按公开名前缀判）。"""
    return str(tool_name).startswith(MCP_PUBLIC_PREFIX)


#: 已声明的形态：`tool_name -> KIND_*`。由 loader 在注册时填
#: （`bus/loaders/skill_loader.py` 的两条注册路各一处）。
#:
#: 是运行时可变状态，不是 import 期常量 —— 这正是本模块存在的理由：
#: manifests 是装载时读的，而名单原本是import 期冻死的。
_declared: Dict[str, str] = {}


def declare(tool_name: str, kind: Optional[str]) -> None:
    """登记一个工具的声明形态。`None` / 非法值不登记（调用方自己决定怎么处置）。

    幂等：与 `register_tool` 同规矩 —— 重启框架会在同一进程里
    重新注册一遍，所以这里必须是就地覆盖，不是 append。
    """
    if kind in VALID_KINDS:
        _declared[tool_name] = kind


def declared_kind(tool_name: str) -> Optional[str]:
    """这个工具声明了什么。没声明（或声明值非法），`None`。

    与 `resolved_kind` 分开是刻意的：体检器要报"没声明"这件事，
    而它不能从 `resolved_kind` 读出来（那里已经把 `None` 兜成了 `prose`）。
判据同 `window_source`："读不到"与"读到了是 0"必须分得开。
    """
    return _declared.get(tool_name)


def resolved_kind(tool_name: str) -> Optional[str]:
    """实际按哪个形态处置。非读家族、也不是外部 MCP，`None`（不适用）。

    `None`（没声明）兜到 `prose` —— 见模块 docstring ①（fail-closed）。

    外部 MCP 按同一条 fail-closed 处置：它的返回是第三方写的自由文本，
    而我们读不到它的 manifest（MCP 工具没有 manifest）。判据是"这段正文
    是谁能写的"，不是"它是不是文件"。

    信任判据在本地：某个服务器确实只回结构化结果，由用户在
    `data/mcp_servers.json` 里写 `trusted_output: true`，注册时把它的工具
    声明成 `structured` —— 走的是同一条 `declare`，不另开一个集合。
    不看服务器自己的注解（那是被保护对象自己说的话）。
    """
    if not is_read_family(tool_name):
        if is_external_mcp(tool_name):
            return _declared.get(tool_name) or KIND_PROSE
        return None
    return _declared.get(tool_name) or KIND_PROSE


def needs_isolation(tool_name: str) -> bool:
    """这个工具的结果要不要走 `sanitize + wrap_as_user_document`。

    与 `core/agent_loop.py::_ISOLATED_DOC_TOOLS` 是两层，不是替代：
    那一层是框架级的硬编码名单（含 `run_command`/`grep` 这些不落读家族、
    只能手动登记的名字），这一层是声明驱动的。任一命中即隔离。
    """
    return resolved_kind(tool_name) == KIND_PROSE


# **不要加 `declares_structured(tool_name)` 这类便利包装**：
# 它想表达的那一问（"作者显式声明了 `structured` 吗"）已经有人答：
# 判"要不要隔离"的是 `needs_isolation`（`resolved_kind == prose`，**含没声明时的
# fail-closed 兜底），体检器要报"没声明**"那件事则问 `declared_kind`。
#再加一层只会变成没有一行代码走到它的死接口。
def snapshot() -> Dict[str, str]:
    """声明表的副本 —— 给测试用。

    不要做成 `reset()`（清空）：声明表是进程级全局，
    清空之后同一批里后跑的测试就看不到任何声明了 ——
    先跑的用例清一次，后跑的用例就会报 `get_todo_items` 没人分类。
测试的规矩是存了再还原（下一条），不是清空 ——
    改全局状态要能还回去。
    """
    return dict(_declared)


def restore(snap: Dict[str, str]) -> None:
    """把声明表还原到 `snapshot()` 的那一刻。"""
    _declared.clear()
    _declared.update(snap)
