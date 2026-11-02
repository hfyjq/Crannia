"""
In-memory singleton registry for tools, providers, and channels.

Eight namespaces:
  - tools:             tool_name -> callable handler
  - tool_schemas:      list of JSON Schema dicts (for LLM function calling)
  - tool_perms:        tool_name -> permission tier (read / write / admin)
  - tool_perm_sources: tool_name -> where that tier came from
  - tool_destructive:  tool_name -> bool — 不可逆，要不要问用户
  - tool_destructive_sources: tool_name -> where that came from
  - providers:         provider_name -> client instance
  - channels:          channel_name -> handler callable (async run function)

Permission tiers exist for ASI02 (Tool Abuse) defence.
"""

import functools
import json
from typing import Any, Callable

from core import tool_permission as _TP

_registry: dict[str, dict] = {
    "tools": {},
    "tool_schemas": [],
    "tool_perms": {},
    # 档位从哪来 —— `declared` / `explicit` /
    # `prefix` / `default` 之一（`core/tool_permission.py::SOURCE_*`）。
    # 与 `tool_perms` 同生同死：一个记值、一个记来源；只清一个，
    #    「这个档位是怎么来的」就永远说不清了。`reset()` 两格一起清。
    "tool_perm_sources": {},
    # 第二根轴 —— 不可逆，要不要问用户。
    # 与 `tool_perms` 正交（`add_block` 是 write 而可逆；`run_command` 是 admin
    # 而不可逆），另起一格，不是 `tool_perms` 的第四个取值。
    # 与 `tool_destructive_sources` 同生同死（同 `tool_perms` 那对的理由）。
    "tool_destructive": {},
    "tool_destructive_sources": {},
    # 这个名字是谁注册的。
    #
    # 它是"要不要拒重名"唯一判得了的判据：`_registry["tools"]` 是 `dict[name]`，
    # 而 `register_tool` 的写入是无条件覆盖，后注册的静默顶掉先注册的。
    #   · 同名同源（`/api/restart` 重注册 · `reload_skills` 再来一遍）
    #     = 幂等，必须放行（拒了它会回来一条上游 400）
    #   · 同名异源（skill 抢框架 · skill 抢 skill）= 必须拒
    "tool_owners": {},
    "providers": {},
    "channels": {},
}

#: 框架自己的归属名。所有 `register_*_tool()` 都用它（默认值）。
FRAMEWORK_OWNER = "framework"

# ------------------------------------------------------------------
# Permission classifier (ASI02 — Tool Abuse defence)
# ------------------------------------------------------------------

# Tool naming patterns → permission tier.
#  - read:  inspect / query only, no side-effects.
#  - write: create / modify files or state.
#  - admin: destructive or system-level operations.
_READ_PREFIXES = (
    "parse_", "search_", "get_", "list_", "detect_",
    "pdf_info", "extract_", "summarize_",
    "web_",             # MCP built-in web tools (web_search, web_fetch)
    "get_design_context", # read-only design query
    # 三条显式 read 覆盖曾在这里：`create_document`（写
    #   `_wip_*.docx` + `store.create` 改共享 `_sessions`）·
    #   `set_slide_design`（`session.spec_lock…update()` 改共享会话态）·
    #   `mock_image`（`img.save()` 真写盘）—— 全部已证伪并移走。
    #   它们被写进这张表时，`read`/`write` 只是给模型看的 description 字符串；
    #   而并发预取与 `subagent.is_write_like()`（只读子 agent）
    #   开始拿它当真闸用，表从「描述性」变成「承重」。
    # `read_file` 出现之后才补上这条前缀 —— 在它之前全仓没有
    # 一个 `read_*` 工具，所以 `read_excel_data` 一直挂在 `admin` 档
    # （不可并发 + 每次打一行 `[audit] ADMIN tool called`）。
    # 这是一处有意的行为变化：`read_excel_data` 从 `admin` 变 `read`
    # 所以它现在可以进只读预取批次。它是纯读（读一个 xlsx 返回数据、
    # 不碰任何会话态），所以变档是对的；但有测试钉着，不许当成副作用。
    "read_",
)
_WRITE_PREFIXES = (
    "create_", "add_", "write_", "finalize_", "export_",
    "merge_", "split_", "mock_", "svgs_to_",
    "convert_to_", "convert_from_", "record_",
    "send_",   # send_email, etc.
    "mark_",   # mark_done, etc.
    "todo_",   # todo_write 写的是会话态的任务清单（不是只读查询）
)
# Everything else is admin (e.g. tool_delete, system_reset — future tools)

#: 框架级 + MCP 工具的声明处（权限档的第四步之后的新角色）。
#:
#: ## 它现在是什么
#:
#: 约定：框架级工具在代码的表里声明，技能工具在 manifest 里声明。
#: 两边都是"有人做过决定"，判定来源分别记成 `explicit` / `declared`。
#:
#: 这个角色的价值在于收窄，而不是扩张 —— 它取代的是"兜底不对、只好一条条捞"的补丁表：
#: 兜底是 `admin`（fail-closed，方向对），但 `admin` 的代价是具体的：
#: 不可并发（并发预取整批退回串行）+ 每次打一行 `[audit] ADMIN tool called`；
#: 而它对"只走目录树"的东西是错的档，还会污染审计（噪声会淹掉真信号）。
#: 一条条往外捞，捞了三批（PDF 页面族 11 件 · Excel/Word 表格族 7 件 · 四件反标）。
#:
#: 那些手捞的条目现在全都不在了 —— 它们搬去了各自的 `manifest.yaml`
#: （`scripts/propose_permissions.py` 按 AST 证据逐条生成，9 个包共 115 行）。
#: 留在这里的只有"没有 manifest 的工具"。
#:
#: 今天 16 条里只有 5 条真的需要表（`create_skill` / `edit_file` / `glob` /
#: `grep` / `history_search` / `memory_*` / `run_*` 一族不落任何前缀）——
#: 其余（`read_file` / `write_file` / `search_tools` / `todo_write` /
#: `web_search` / `web_fetch`）靠前缀也判得对。
#: 仍然逐条写出来：只靠前缀，判定来源是 `prefix`，而 `prefix` 的定义是
#: "没人给它定过档"，会被算进那条棘轮。框架工具是有人定过的，来源要如实。
_EXPLICIT_PERMS: dict[str, str] = {
    # 文件原语五件
    "read_file":  "read",     # 只读内容
    "write_file": "write",    # 写盘
    "edit_file":  "write",    # 改文件（`write_file` 靠 `write_` 前缀命中，同档）
    "glob":       "read",     # 只走目录树（连文件内容都不读）
    "grep":       "read",     # 只读内容，不改任何状态
    "code_intel": "read",     # 查符号：只读，零副作用
    "run_program": "admin",   # 子进程里能跑任意 Python（不是沙箱）
    # 工具发现 / 执行 / 子 agent / 造包 / 清单
    "search_tools": "read",   # 检索工具目录，零副作用
    "run_command":  "admin",  # 全仓唯一能执行任意命令的东西（不是沙箱）
    # 接管 —— 它直接控制用户的鼠标键盘。
    #   `admin` 同时让它进并发判定的串行那一支（输入必须串行，没有并发可言）。
    "computer":     "admin",
    "browser":      "admin",
    "run_subagent": "admin",  # 起一个能改任何东西的子 agent
    # 一次起最多 32 个子 agent —— 同 `run_subagent` 同档，
    #   而且它天然是 `admin`，串行，不会和工作流内部自己的并行打架。
    "run_workflow": "admin",
    # 群的成员派活 —— 与 `run_subagent` 同档同理由
    # （它起的那个成员能改任何东西）。
    # 它是 `admin` 而不是 `read` 还有第二个好处：并发判定于是让它串行
    #   （成员之间没有并发安全可言），而这正是当前版本想要的 —— 见
    #   `_EXPLICIT_CONCURRENCY` 那条"别把它顺手加进去"的注释。
    "ask_member":   "admin",
    "todo_write":   "write",  # 写的是会话态的任务清单（不是只读查询）
    # 查/等是只读，但 `cancel` 会真的停掉一个后台执行体，整条按 write。
    #   写成 `read` 会让它被并发预取与只读子 agent 放行 —— 那是危险的一侧。
    "task":         "write",
    # 周期任务三件
    "cron_create": "write",   # 落一条会自己跑起来的任务
    "cron_list":   "read",    # 只读（连 job 都是纯查询）
    "cron_delete": "write",   # 删定义 + 它的台账
    "create_skill": "write",  # 往 `skills/` 落一个草稿包
    # 计划模式的两个工具
    # 两件都是 `write`（不是 `read`），两个理由：
    #   ① 它们改会话态（模式 / slug），不许并发跑；
    #   ② 只读子 agent 会放行 `read` 档的工具，`write` 才拦得住
    #      （让子 agent 改父的模式 = 一次没人看得见的模式切换）。
    "enter_plan_mode": "write",
    "exit_plan_mode":  "write",
    # MCP 内置（`mcp/servers/`）
    "web_search": "read",
    "web_fetch":  "read",
    # 记忆三件 + 查历史
    "memory_read":    "read",   # 纯文件扫描（可进只读并发批）
    "memory_search":  "read",   # 同上
    "memory_write":   "write",  # 写盘（`write_file` 同档）
    # 删除：不可逆（内容不保留），`destructive` 那一轴为真；
    #   权限档照 `write`（它改的是记忆库这一块的会话可见状态）。
    "memory_delete":  "write",
    "history_search": "read",   # 只读 short_term.db，零副作用
    # ICE：把句柄背后的真值放行给模型
    # 它是 `write` 不是 `read`，尽管它一个字节都不改：
    #   ① `read` 那三样含义它一样都不该有 —— 可并发、
    #      可进只读子 agent、计划模式里放行。
    #      一个子 agent 能替父把真值调进上下文，是这里最不该发生的事。
    #   ② 判据：拿不准就写 `write`。
    "pii_reveal": "write",
    # 提问面板
    # 它是 `write` 不是 `read`，尽管它一个字节都不改：
    #   ① 它写会话态（挂起 / 待回答），不许并发跑；
    #   ② `read` 档会被只读子 agent放行 —— 而子 agent 里
    #      "回答变成新的一轮"那一轮不存在，放行 = 父整轮卡死。
    #   与 `pii_reveal` 同一条判据（同一段注释）。
    "ask_user": "write",
    # 上下文压缩的请求口
    # `write`（不是 read）：它改的是这一段历史的形状，不许并发跑，
    #   也不许只读子 agent 替父做（子 agent 里没有"下一轮继续"这回事）。
    "request_compaction": "write",
    # 出站
    # 往会话或外部平台发消息：改的是外部世界的状态，且发出去收不回。
    # `write` 让它串行（同一时刻只发一条），审批由兜底闸按收件人询问。
    "send_outbound": "write",
}
#
# 一个已知的、有意的同档：`delete_files` 现在与 `add_block` 同档
#    （都按 AST 判成 `write`）。它在 `_APPROVAL_CAPABLE_TOOLS` 里，审批照弹，安全侧没变；
#    区分"不可逆"是 `destructive` 轴要做的事：只标删、覆盖、发送三类，
#    共同点是做完之后没有撤销路径。
#    这一行是留给"将来有人以为我们忘了"的。


#: 第二根轴的声明处 ——
#: 与 `_EXPLICIT_PERMS` 同一个角色：框架级 + MCP 工具在代码的表里声明，
#: 技能工具在各自的 `manifest.yaml` 里声明。
#:
#: ## 判据：不可逆，而且限定在「调用者没要求的那部分损失」
#:
#: 这一条决定了 `write_file` / `edit_file` 为什么是 `false`：
#: 覆盖调用者点名要写的那个路径是本意，不是附带损失。
#: 反过来 `delete_files` 是 `true`（删掉就没有了），`run_command` 是 `true`
#: （它能做没人要求过的事）。没有这一条，闸会为每一次写文件弹窗。
#:
#: ## 两条成本不对称，写在这里免得后人以为漏了
#:
#: * `run_command: true` 不会加宽闸的爆炸半径 —— 它在
#:   `core/agent_loop.py::_APPROVAL_CAPABLE_TOOLS` 里（自己的发射点在
#:   `core/shell.py`），闸读的是差集，跳过它。声明 `true` 只是如实。
#: * `run_subagent: false` 是判过的，不是没想到：它自己不删东西；
#:   子 agent 手里每个工具各自按这一根轴判，而审批类的工具在子里一律被拒
#:   （`core/agent_loop.py`）。它这一格写 `true` 只会让闸
#:   为每一次子 agent 调用弹窗，而买不到任何东西。
#:
#: 证据（不是印象）：`scripts/propose_destructive.py` 扫过全部 132 个工具，
#: 框架侧只有 `create_skill` 命中不可逆原语（`shutil.rmtree`，删它自己的
#: `_wip_` 临时目录，误报）。
_EXPLICIT_DESTRUCTIVE: dict[str, bool] = {
    # 文件原语五件
    "read_file":  False,
    "write_file": False,   # 覆盖的是调用者点名的路径 + 覆盖前必须读过（CAS 纪律 ②）
    "edit_file":  False,   # 只 splice 命中那一段，且同样要读
    "glob":       False,
    "grep":       False,
    "code_intel": False,
    "run_program": True,      # 子进程能跑任意 Python，按"改得回来吗"判 True
    # 工具发现 / 执行 / 子 agent / 造包 / 清单
    "search_tools": False,
    "run_command":  True,  # 全仓唯一能执行任意命令的东西（不是沙箱）
    # 接管能做出不可逆的事（删数据 / 付款）。
    #   判据「改得回来吗」，True。它不会因此每次调用都被问：
    #     它已经在 `core/agent_loop._APPROVAL_CAPABLE_TOOLS` 里
    #     （自己的发射点 = 逐应用授权），兜底闸读的是两者的差集。
    "computer":     True,
    "browser":      True,
    "run_subagent": False, # 判过：见上面那段（子里审批类工具一律被拒）
    # 它自己不删东西；每一步的子走的是子 agent 那一套（审批类一律被拒）。
    "run_workflow": False,
    # 派给群成员 —— 同 `run_subagent` 那条判据
    # （它自己不删东西；成员如果要删，走的是成员自己那一轮的审批闸）。
    "ask_member":   False,
    "todo_write":   False, # 改的是会话态清单，`/clear` 就没了
    # `cancel` 停掉的是调用者点名要停的那条（同 `write_file` 覆盖点名路径）——
    #   不算"没人要求的损失"。`false`；否则连 `status` 都会去弹审批。
    "task":         False,
    # 周期任务三件
    # `cron_create` 判 True，因为它要过审批。
    #   不是把它加进 `_APPROVAL_CAPABLE_TOOLS` —— 那个集合的含义
    #     是"它有自己的审批发射点，闸别再问"，方向正好相反。正确的落法是
    #     `destructive=True` 且不在那个集合里，兜底闸在派发前问用户。
    #   判据（「改得回来吗」）：定义本身删得掉，但它造的是一件以后会自己
    #     跑起来的活；真到点时若没人在场，那条任务只会挂起（审批靠"回答变成
    #     新的一轮"，而那条路永不挂起），结果是无事发生，
    #     所以问在创建那一刻才是有效的。
    "cron_create":  True,
    "cron_list":    False, # 纯查询
    "cron_delete":  True,  # 删掉的定义回不来（与 `delete_files` 同族）
    # 出站：发出去收不回，而且是对外可见的动作，True，由兜底闸问一次。
    "send_outbound": True,
    "create_skill": False, # 账本：`shutil.rmtree` 删的是它自己的 `_wip_` 临时目录
    # 计划模式的两个工具
    # 判据是「改得回来吗」：两个都能一键切回去（`/mode` 或再调一次），
    #   而且都不动用户的数据 —— `enter_plan_mode` 只是收紧自己，
    #   `exit_plan_mode` 只申请一次批准。两件都是 `false`。
    "enter_plan_mode": False,
    "exit_plan_mode":  False,
    # MCP 内置
    "web_search": False,
    "web_fetch":  False,
    # 记忆三件 + 查历史
    "memory_read":    False,
    "memory_search":  False,
    "memory_write":   False,  # 同 `write_file`：写的是调用者点名的那个 slug
    "history_search": False,
    # 提问面板
    # `False`：它不改任何东西 —— 只把一个问题挂起来等用户回答。
    #   挂起 ≠ 不可逆（`/clear` / 超时 / 中断都能把它收掉）。
    "ask_user": False,
    # ICE
    # `True`。判据是「改得回来吗」：真值一旦进了这段对话的历史，
    #   就收不回来了 —— 它会落库、并且之后每一次请求都一起发上游。
    #   与 `write_file` 的 `False` 不冲突：那个覆盖的是调用者点名的路径
    #   且写前必读（能 diff、能拿留档回来）；这个没有撤销路径。
    #   兜底方向：拿不准就写 `true`。
    "pii_reveal": True,
    # `False`：压缩不改用户的数据 —— 被压掉的原话仍留在历史库里
    #   （`history_search` 搜得回），只是这一段的活动窗口变小。
    "request_compaction": False,
    # True：内容不保留（封条只挡重生，不恢复内容）。
    "memory_delete": True,
}


#: 第三根轴的声明处 —— 与上面两张表同一个角色：
#: 框架级 + MCP 工具在代码的表里声明，技能工具在各自的 `manifest.yaml` 里声明。
#:
#: ## 判据：能不能和同批别的调用同时跑（与 `permission` / `destructive` 都正交）
#:
#: 这张表几乎为空，而且那是对的 —— 兜底是由权限档派生
#: （`permission == "read"`），与并发预取的原判据等价
#: 所以全仓行为一个字节不变。表里每多一行，都是一次有意的放开。
#:
#: **`run_subagent: true` 是唯一要加的那一行，而且它必须和"特判进预取"同批落地** ——
#: 预取的通用路是 `handler(**args)`，而 `run_subagent` 注册的 handler 是
#: `_not_dispatched`（占位、永不执行，见 `bus/registry.py::register_subagent_tool`）
#: 单独放开它 = 每个子 agent 都跑成一个"没派发"的空回执。两件事拆开做就是 bug。
_EXPLICIT_CONCURRENCY: dict[str, bool] = {
    # 全仓唯一一行：
    # 两个子 agent 互不相干（各自的循环、各自的 `ContextVar` 深度），
    # 而它是 `admin` 档（能起一个"能改任何东西"的子）—— 两根轴的正交性
    # 就靠这一行体现：能改东西 ≠ 不能并发。
    # 它与 `core/agent_loop.py` 里那条"特判进预取"是同一批的事：
    # 单独放开它，预取会去调注册表里那个 `_not_dispatched` 占位，
    # 每个子 agent 都跑成一句"没派发"的空回执（而且不报错）。
    "run_subagent": True,
    # 一个 (服务器, 根) 只有一个客户端会话；放进并发会出现
    #   "同一个请求的两条结果互相顶掉"的窗口 —— 先串行。
    "code_intel": False,
    "run_program": False,     # 一次一个子进程；并行没有收益，串行更稳
    # `ask_member` 有意不在这张表里 —— 别再"顺手"加它：
    #   ① 它没有像 `run_subagent` 那样配"特判进预取"，加进来会去调
    #      `register_group_tool` 注册的那个占位 handler（同 `_not_dispatched`
    #      那条坑：每个成员都跑成一句空回执，而且不报错）；
    #   ② 更要紧的是成本：一批并发的成员调用 = 同时开 N 个完整上下文。
    #      `admin` 档已经让它串行 —— v0 就停在这儿，等有人量过再说。
}


#: 分类器声明的框架侧表 —— 与 `_EXPLICIT_PERMS` /
#: `_EXPLICIT_DESTRUCTIVE` 同一个角色：框架级 + MCP 工具在这里声明，
#: 技能工具在各自的 `manifest.yaml` 里声明（支持技能级默认 + 工具级覆盖）。
#:
#: ## 两个取值
#:
#: * `"none"` —— 显式声明"我没有与分类相关的输入"，第 ④ 层跳过
#: * 一段非空描述 —— 这个动作要过分类器，描述说明它带什么信息
#:
#: 不在表里 ≠ 写了 `none`：前者走 fail-closed（过分类器）。
#: `tests/test_auto_review.py` 有一条读者点名那些"既不只读、
#: 又没声明"的工具 —— 它保证"没人管"这件事看得见，而兜底保证它安全。
#:
#: ## 为什么这几格是这么填的（每条都说得出理由）
#:
#: * `run_command` —— 它就是这个功能存在的主要理由（任意执行）。
#: * `delete_files` —— 不可逆。注意它在 `file_parser` 里，不在这张表。
#: * `send_email` / `deliver_file` —— 对外发送（"回答用户 ≠ 发给别人"）。
#: * `create_skill` —— 写进 `skills/`，能拆掉围栏的东西。
#: * `memory_write` —— 跨会话持久化（错了不会立刻发现）。
#: * `run_subagent` —— 有意填 `none`：子 agent 的每一次工具调用在父的转写里
#:   已经各过一遍闸，这里再判一次就是同一件事判两遍。
#: * `todo_write` —— 会话态清单，`/clear` 就没了。
#: * `write_file` / `edit_file` —— `none`：它们只写得到工作区
#:   （`SecurityGuard` 挡着；唯一例外是本会话的计划文件），越界的那些
#:   压根执行不了，为它们过闸是空转 —— 闸只该花在真能跑的动作上。
#: * `request_cross_zone_search` —— 它有自己的审批发射点，而那正是
#:   "这次搜索该不该放行"的判定，交回分类器会变成两个判据。
_EXPLICIT_REVIEW_INPUT: dict[str, str] = {
    "run_command":   "要执行的命令原文",
    "run_program":   "要执行的编排脚本",
    # 出站：分类器要看的是"发给谁、发了什么"。
    #   它本来就是 `destructive`（收不回来），所以不要 `none` ——
    #   给 `none` 等于把一次对外发送从分类器眼前拿掉。
    "send_outbound": "收件人、正文与要附带的文件",
    "create_skill":  "要创建的技能名与它声明的能力",
    "memory_write":  "要记住的内容、作用域",
    "run_subagent":  "",      # 见上面那张表：有意 none
    # 同 `run_subagent` —— 每一步的子各自过闸，父这里再判就是双问。
    "run_workflow":  "",
    # 同 `run_subagent` 那条判据 ——
    # 成员每一次工具调用各自会过闸，在父这里再判一次就是双问。
    "ask_member":    "",
    "todo_write":    "",
    # `task` = none：task_id 是框架发的句柄，action 是枚举 —— 没有外部内容。
    "task":          "",
    "write_file":    "",
    "edit_file":     "",
    # ICE：`none` —— 它有自己的审批发射点
    #   （handler 发 `pending_ice_reveal`），而那正是"这次该不该把真值给它"的判定
    # 所以交回分类器会变成两个判据（同 `request_cross_zone_search` 那条）。
    #   而且这里判的是用户的态度，不是"安全不安全" ——
    #   交给一个只管安全的分类器去代答，等于替用户答应。
    "pii_reveal":    "",
    # 周期任务三件
    # `cron_create` 给描述（不是 `none`）—— 判据是"分类器该看什么"：
    #   要建的那条任务做什么、什么时候跑正好是"这条会不会出事"的全部依据
    #   （"半夜三点把 .env 发到某处"这种一眼就能判）。这正是分类器存在的理由。
    "cron_create":   "要建的任务做什么（prompt）与它的时间规则（rule）",
    # `cron_list` = `none`：纯查询。
    "cron_list":     "",
    # `cron_delete` = `none`，刻意：交给分类器 = 让它替用户答"这条删得"。
    #   分类器只管安全（"删一条定时任务不安全吗？"——多半答"安全"），
    #   而这里判的是用户要不要。声明 `none`，第 ④ 层跳过，
    #   兜底闸照旧问用户（它 `destructive=True`）。同 `pii_reveal` 那条。
    "cron_delete":   "",
    # 提问面板 = `none`，刻意。
    #   它根本不会走到分类器（`_needs_approval` 只认"有自己的审批发射点"
    #   与 `destructive`，它两样都不是），这里的 `none` 是如实：
    #   它没有"与安全分类相关的输入"。就算哪天它真被送进分类器，
    #   "用户想选哪个方案"也不是一个只管安全的分类器能代答的 ——
    #   那是替用户做决定（同 `pii_reveal` / `cron_delete` 那条判据）。
    "ask_user":      "",
    # 接管 = `none`，刻意。
    #   ① 它只在「完全权限」那一段的 `tools=` 里（`_session_extra_tools`
    #      按会话判，而启用接管要求 `frozen_mode == full_access`），自动模式下
    #      分类器根本碰不到它；这一格是如实，不是"给它开绿灯"。
    #   ② 就算哪天碰得到：这里判的是「用户要不要让 agent 动这台电脑」——
    #      与 `pii_reveal` / `cron_delete` 逐字同一条：替用户答应。
    "computer":      "",
    "browser":       "",
    # 它没有输入（空参数），分类器没什么可判的。
    #   它带来的动作（压缩）本身是只缩活动窗口、不删原话的。
    "request_compaction": "",
    # 交给分类器看的是"要删哪一条"这个名字 —— 它本身就是全部输入。
    "memory_delete": "name 是要删的那一条记忆的标题；没有别的输入。",
}
#
# 上面用空串表示 `none` 是这个字面表的写法（省得每行都写 `"none"`）；
# `get_explicit_review_input` 把它归一成 `"none"` —— 归一只有那一处。
#
# 这张表只管"没有 manifest 的工具"（框架级 + MCP）。
# 技能工具即便写在这里也会被 manifest 盖住（判定次序里 manifest 在前）——
# 所以 `send_email` / `deliver_file` / `delete_files` 这些技能工具
# 一律去各自 `manifest.yaml` 里写，别往这张表里塞：塞了也不生效，
# 而"塞了看着像生效了"正是本仓最贵的那种错。


def get_explicit_review_input(tool_name: str):
    """框架/MCP 侧的分类器声明；没有就返回 `None`（与"声明了 `none`"分得开）。"""
    raw = _EXPLICIT_REVIEW_INPUT.get(str(tool_name))
    if raw is None:
        return None
    return raw or "none"


def _resolve_permission(tool_name: str) -> tuple[str, str]:
    """唯一的判定路径。返回 `(permission, source)`。

    判定次序由 `core/tool_permission.resolve` 写死，不许在这里重排：
    `manifest 声明 -> _EXPLICIT_PERMS -> 前缀猜 -> 兜底 admin`。

    表由这里注入（`core/` 保持叶子、不 import `bus`，否则调用期循环 import）
那个函数是纯的，测试可以喂合成的表去验顺序
    （`tests/test_permission.py::test_a_manifest_permission_beats_the_prefix_guess`）。

    为什么要有 `source` 这一维："读不到"与"读到了是某值"必须分得开
    （同 `window_source` / `read_cache_hit`）。
    `prefix` / `default` 是没人给它定过档，那个数只许降。
    """
    return _TP.resolve(tool_name,
                       explicit=_EXPLICIT_PERMS,
                       read_prefixes=_READ_PREFIXES,
                       write_prefixes=_WRITE_PREFIXES)


def _classify_permission(tool_name: str) -> str:
    """薄壳 —— 只回档位。

    真正干活的是 `_resolve_permission`（它多回一个 `source`）。
    这里是同一条路径，不是"两段等价逻辑" —— 两份判定必然漂移。

    改它的签名/返回值之前先看两处读者：`tests/test_file_tools.py`
    （拿它比字符串）与 `tests/test_permission_tiers.py`（拿它判档）。
    """
    return _resolve_permission(tool_name)[0]


def _resolve_destructive(tool_name: str) -> tuple[bool, str]:
    """第二根轴唯一的判定路径。返回 `(destructive, source)`。

    判定次序由 `core.tool_permission.resolve_destructive` 写死，不许在这里重排：
    `manifest 声明 -> _EXPLICIT_DESTRUCTIVE -> 兜底 False`。

    **兜底是 `False`（fail-open），与 `_resolve_permission` 的 `admin`
    （fail-closed）方向相反** —— 这是一笔算得出来的账，理由在
    `core/tool_permission.py` 的模块 docstring「第二根轴」一节里，动手改之前先读它。
    一句话：闸是「`destructive` 且不在 `_APPROVAL_CAPABLE_TOOLS`，问」，
    而那张能力表只有少数几个名字，兜底取 `True` 会让闸为绝大多数工具弹窗。
    """
    return _TP.resolve_destructive(tool_name, explicit=_EXPLICIT_DESTRUCTIVE)


def _note_undeclared(tool_name: str, perm: str, source: str) -> None:
    """「没人给它定过档」出声。

    分两档，判据是猜错的代价方向：

    * `perm == "read"`，逐条 print。这是唯一"可能把危险当安全"的一侧 ——
      `read` 会被并发预取与只读子 agent 当真闸用。
    * 其余（猜成 `write` / `admin`），只进汇总行（`undeclared_report`）。
      它们 fail-closed，猜错的方向是"多问一次 / 少并发一次"，代价看得见。

    不去重：`/api/restart` 会在同一进程里重注册一遍，于是再打一遍
    —— 那正是它该有的样子（那是一次真重启，不是噪声）。代价是不引入一格新的
    进程级状态（`core/state_registry.py` 的 census 会当场抓）。
    """
    if source not in (_TP.SOURCE_PREFIX, _TP.SOURCE_DEFAULT):
        return
    if perm == _TP.PERM_READ:
        print(f"[perm] 未声明 -> 猜成 read: {tool_name}"
              f"（并发与只读子 agent 拿它当真闸）")


def undeclared_report() -> str:
    """启动汇总行 —— 还有多少个工具「没人定过档」。

    这个数是权限档的验收行：`undecided = 前缀猜 + 兜底`，
    两个都只许降（最终为空集）。
    调用点在全部装载之后（`bus/core.py` 打印 `Initialization complete`
    那一行旁边）—— 放早了会漏掉 MCP 那两个。
    """
    sources = _registry["tool_perm_sources"]
    perms = _registry["tool_perms"]
    prefix = sum(1 for s in sources.values() if s == _TP.SOURCE_PREFIX)
    default = sum(1 for s in sources.values() if s == _TP.SOURCE_DEFAULT)
    declared = sum(1 for s in sources.values() if s == _TP.SOURCE_DECLARED)
    explicit = sum(1 for s in sources.values() if s == _TP.SOURCE_EXPLICIT)
    read_guessed = sum(
        1 for name, s in sources.items()
        if s in (_TP.SOURCE_PREFIX, _TP.SOURCE_DEFAULT)
        and perms.get(name) == _TP.PERM_READ
    )
    return (
        f"[perm] 未定档 {prefix + default} 个"
        f"（前缀猜 {prefix} + 兜底 {default}）"
        f" · 已定档 {declared} 声明 + {explicit} 代码表"
        f" · 其中猜成 read 的 {read_guessed} 个已逐条列出"
    )


_PERM_LABEL: dict[str, str] = {
    "read":  "[READ]",
    "write": "[WRITE]",
    "admin": "[ADMIN ⚠️]",
}


def _wire_schema(input_schema):
    """`inputSchema` -> wire 上的 `parameters`。剥掉 `$schema`。

    `$schema` 是每一条工具 schema 里逐字相同的一个常量 URI
    （`https://json-schema.org/draft/2020-12/schema`）—— 19 tokens × 14 个
    常驻 = 266/请求，全部是纯税。

    自检没有被拿掉：`skill_loader.validate_tool_schema()` 在装载时已经校验过
    它（认不出的 URI 直接 raise），那才是它该在的地方。到了 wire 上它不再承载任何
    给模型看的信息 —— 收它的那一侧（function calling 的 `parameters`）只是一个
    JSON Schema 对象，OpenAI 兼容端点不按 meta-schema 校验它。

    判据：全量重发的东西，看的不是"有用吗"，是"每一轮都有用吗" ——
    「这些工具用的是同一个方言」对模型的每一次决策都没有贡献。
    """
    if not isinstance(input_schema, dict) or "$schema" not in input_schema:
        return input_schema
    out = dict(input_schema)
    out.pop("$schema", None)
    return out


#: 单条工具 schema 的序列化字节报警线（不是截断线）。
#:
#: 判据是量出来的：有函数调用提供方按单条 schema 的序列化大小硬拒整条请求
#: —— 同一家客户端 13.7 KB 能过、约 15 KB 被拒。所以这条线定在两者之间。
#: 我们自己的工具今天最大 5.3 KB，离坎还远；这条线真正要拦的是接进来的外部工具
#: 把某一条顶穿（顶穿的症状是整轮请求 400，而不是"那个工具有问题"）。
#: 到线只出声、不改 schema —— 广告面与执行面必须逐字同形。
TOOL_SCHEMA_BYTE_CAP = 13312


def _note_schema_size(name: str, tool_def: dict) -> None:
    """注册那一刻量一次这条 schema 的字节数，过线就出声。

    放在注册期而不是每请求：注册是稀疏事件（启动 / 接一台 MCP / 热加载），
    而每请求量一遍是把一次性的成本摊到每一天的每一次对话上。
    """
    try:
        size = len(json.dumps(tool_def, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return
    if size <= TOOL_SCHEMA_BYTE_CAP:
        return
    print("[schema] WARNING: 工具 '%s' 字节超限 —— 可能存在请求拒绝的运行情况。"
          % (name,))


def register_tool(name: str, handler: Callable, schema: dict,
                  owner: str = FRAMEWORK_OWNER) -> bool:
    """Register a tool with its handler function and JSON Schema definition.

    Auto-classifies the tool's permission tier (read/write/admin)
    and injects a `[READ]` / `[WRITE]` / `[ADMIN]` label into the
    description so the LLM can make informed decisions about which tools
    are safe to call.

    两件事：

    ① 档位不再"猜完就完了" —— 走 `_resolve_permission`，连来源一起记
       （`tool_perm_sources`），未定档的按代价分档出声。
    ② 同名异源，拒绝登记（判据见 `_refuse_duplicate`）。

    *owner* 是"这个名字是谁注册的"：框架工具用默认的 `FRAMEWORK_OWNER`，
    技能包用 `skill:<目录名>`，MCP 用 `mcp:<server 名>`。

    Returns:
        `True` = 登记成功；`False` = 被拒（名字被别人占着）。
        拒的时候一个字节都不写（旧的那份还在），而且只拒这一个名字，
        不牵连同批的其它工具 —— 拒绝整个包会让用户的工具凭空消失。
    """
    existing = _registry["tool_owners"].get(name)
    if (name in _registry["tools"] and existing != owner
            and owner != FRAMEWORK_OWNER):
        _refuse_duplicate(name, existing, owner)
        return False

    _registry["tools"][name] = handler
    _registry["tool_owners"][name] = owner

    perm, source = _resolve_permission(name)
    _registry["tool_perms"][name] = perm
    _registry["tool_perm_sources"][name] = source
    _note_undeclared(name, perm, source)

    # 第二根轴，两格一起写（同上面那对的理由）。
    dest, dest_source = _resolve_destructive(name)
    _registry["tool_destructive"][name] = dest
    _registry["tool_destructive_sources"][name] = dest_source

    description = schema.get("description", "")
    label = _PERM_LABEL[perm]

    # Inject the permission label at the start of the description so
    # the LLM sees it before deciding to call the tool.
    tool_def = {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{label} {description}",
            "parameters": _wire_schema(schema.get("inputSchema", {})),
        },
    }

    # 注册那一刻量一次这条 schema 的字节（过线才出声；见常量处的判据）。
    _note_schema_size(name, tool_def)

    # 幂等：同名就地替换，不是 append
    #
    # 上面两格（`tools` / `tool_perms`）是 dict，重复注册天然无害；
    # 只有 schema 这一格是 list，于是重复注册会真的塞两份。
    #
    # 而"重复注册"是会发生的：`/api/restart` 直接 `_init_framework()`
    # 重来一遍，而 `_init_framework` 开头那句
    # `if _FRAMEWORK_RUNNING: return` 挡的是"已在运行"（并发），
    # 不是"同一进程里再来一次" —— `/api/restart` 恰好先把
    # `_FRAMEWORK_RUNNING` 置回 `False` 再调它。闸装错了轴。
    #
    # 同一进程连续 boot 三次，wire 上 14 → 27 → 40 条，其中 13 个重名，
    # 所以上游 400 `Tool names must be unique`。顺带每次重启都让每条请求变贵
    # （工具表 2,837 → 5,485 → 8,133 tokens）。
    for _idx, _existing in enumerate(_registry["tool_schemas"]):
        if _existing["function"]["name"] == name:
            _registry["tool_schemas"][_idx] = tool_def
            return True
    _registry["tool_schemas"].append(tool_def)
    return True


def _refuse_duplicate(name: str, prev_owner: str | None, owner: str) -> None:
    """同名异源 —— 拒绝登记 + 出声，且带路线。

    ## 它挡的是什么（两个面，同一个根）

    * skill 抢框架：模型调 `run_command` 会走 skill 的代码 ——
      `core/command_policy.py`（deny 档 `curl`/`wget`）与 `core/shell.py`
      （全仓唯一碰 `subprocess` 的地方）一点都不经手；而 `_APPROVAL_CAPABLE_TOOLS`
      （`core/agent_loop.py:325`）按名字命中，审批浮窗照弹、上面写着 `run_command`
用户以为在放行一条被围栏保护的命令，实际执行的是 skill 的实现。
    * skill 抢 skill：装载顺序是 `sorted(skills_dir.iterdir())`，
      目录名字母序靠后的赢，先注册的静默失效。

    ## 为什么"拒"是对的

    `mcp__` 前缀承重：不加前缀的话，外部服务器的工具和框架工具可以同名，
    而同名意味着"谁赢取决于装载顺序" —— 一次调用被路由到另一个权限和 handler。
    所以重名在这里是拒，不是静默顶掉，也不是 last-write-wins。

    话术不许借 `compat_message`（那是"我们不兼容这个插件"，读者是终端用户，
    正文还写着"我们仍努力为它添加支持"）：撞名是"换个名字就好"，
    读者是写包的人。两条通道不许合并。

    ## 判据是「谁有资格覆盖」，不是「重名就拒」

    | 现在谁占着 -> 谁来注册 | 判 |
    |---|---|
    | 框架 -> 框架 | 放行（幂等；`/api/restart` 靠它） |
    | 技能 -> 技能 | 拒（危害②：字母序决定谁赢） |
    | 技能 -> 框架 | 拒（危害①） |
    | 框架 -> 技能 | 放行 —— 只有框架自己能覆盖别人的名字 |

    最后一行是刻意的，不是漏了：威胁模型是「外来登记不许顶掉任何东西」，
    而在仓内直接调 `register_tool` 的都是可信代码（框架自己的 14 个注册函数、
    以及测试里"注入替身"那套既有用法 —— `test_chunk_tools_guard` 拿桩替
    `parse_csv`、`test_subagent` 替 `run_command`、`test_content_sanitizer` 替
    `get_workbook_metadata`，8 个文件）。
把"框架也能被拒"写进去，等于为了一个不存在的攻击面把一套既有测试全打断，
    而换来的保护是零（框架代码要作恶，根本不需要走这条路）。
    """
    who = prev_owner or "（未知来源）"
    print(f"[bus] 拒绝注册工具 '{name}'：这个名字已经被 {who} 占着，"
          f"而 '{owner}' 是另一个来源。")
    print(f"[bus]   -> 换个名字，或把 manifest 里这一条删掉。"
          f"框架保留的名字不许被第三方覆盖。")


def reserved_tool_names() -> set[str]:
    """框架自己占着的工具名 —— 派生的视图，不是手抄的清单。

    为什么是函数而不是常量表：它是 `tool_owners` 的一个投影，
    抄成常量就会漂（没有读者的清单会烂）。
    框架工具先注册（`bus/core.py` 在技能之前），技能看它就是一张保留名表。

    它不是 `_EXPLICIT_PERMS`：那张表里**有相当一部分是 skill 自己的
    工具名** —— 拿它当保留名表会把 `office_parser` 自己的 69 个工具全拒掉。
    """
    return {n for n, o in _registry["tool_owners"].items() if o == FRAMEWORK_OWNER}


def unregister_owner(owner: str) -> int:
    """把一个 owner 的全部工具从注册表里摘掉。返回摘掉了几个。

    「卸载技能」必须有它 —— 没有它的话，把 `skills/<名>/`
       挪进回收站只动了目录，而 `register_tool` 是就地替换、从不删除
       -> 界面说「已卸载」而工具还能被调（标签在说谎），
       而且没有任何东西会红。

    [!] 只摘 `owner` 相等的那些 —— 不匹配的一个都不许动。
       （第一版很容易写成"清空整表"，那是把所有人的工具一起废掉。）
    [!] `framework` 拒摘 —— 那是框架自己的工具，摘掉等于把框架拆了。
    [!] 六格同生同死（`tools` / `tool_owners` / `tool_perms` /
       `tool_perm_sources` / `tool_destructive` / `tool_destructive_sources`）
       ＋ 那个 list（`tool_schemas`）。只清一半 -> 剩下那半会继续出现在
       `tools=` 参数里（"删了还在"）。
    """
    if not owner or owner == FRAMEWORK_OWNER:
        return 0
    names = [n for n, o in _registry["tool_owners"].items() if o == owner]
    if not names:
        return 0
    for n in names:
        for key in ("tools", "tool_owners", "tool_perms", "tool_perm_sources",
                    "tool_destructive", "tool_destructive_sources"):
            _registry[key].pop(n, None)
    drop = set(names)
    _registry["tool_schemas"][:] = [
        s for s in _registry["tool_schemas"]
        if ((s.get("function") or {}).get("name") or s.get("name")) not in drop]
    # 发现集也得摘 —— 这一步是写读者时才发现漏的：
    #   `tools=` 那一侧是「常驻 ∪ 已发现」（`get_active_tool_names`），
    #   而 `_discovered` 是按 session 存的、只会加。
    #   -> 只清注册表的话，那些名字还在发现集里 -> 上游照样收到一批
    #   没有 handler 的工具名（模型调它 → 派发时说"不存在"，而 `tools=` 里明明有）。
    #   [!] 这是 `_discovered` 上第一处按名字排除 —— 所以写在这里、而不是别处。
    for bucket in _discovered.values():
        bucket -= drop
    return len(names)


def get_tool_owner(name: str) -> str | None:
    """这个名字是谁注册的。没登记过，`None`。

    与 `reserved_tool_names()` 一样是 `tool_owners` 的视图 ——
    `core/skill_doctor.py` 判"skill 抢框架名"靠的就是这一格。
    """
    return _registry["tool_owners"].get(name)


def get_tool_schemas() -> list[dict]:
    """Return all registered tool schemas for LLM function calling."""
    return _registry["tool_schemas"]


def get_tool_summaries() -> list[str]:
    """Return name: one-line-description strings for each tool.

    Used by the prompt assembler to generate the <available_tools>
    section — the model sees tool names and short descriptions in
    the system prompt, while full JSON Schemas are sent via the
    `tools` parameter of the chat API.
    """
    summaries: list[str] = []
    for tool_def in _registry["tool_schemas"]:
        name = tool_def["function"]["name"]
        desc = tool_def["function"].get("description", "")
        # Collapse multi-line descriptions into a single line
        one_liner = " ".join(desc.split())
        summaries.append(f"{name}: {one_liner}")
    return summaries


def _core_first(schemas: list[dict]) -> list[dict]:
    """把一组 schema 排成 wire 序：常驻在前 + 其余追加在后。

    两组内部各保持注册序（输入本就是这个序，两次列表推导是稳定划分）。

    为什么不能"按注册序整体过滤" —— 提示缓存按前缀匹配，而"已发现的工具"
    在注册序里和常驻工具交错：

        注册序：            [0] search_tools(core)  …  [3] parse_pdf  [4] add_block(core) …
        只发常驻（旧）→ wire：[0] search_tools     …                 [4] add_block …
                            ^^^ 这一段字节本来是对的、已经被缓存住
        发现 parse_pdf 后（旧）：[0] search_tools …  [3] parse_pdf  [4] add_block …
                            ^^^ 从 parse_pdf 插进来的那一字节起，整段改写
缓存断在这里，后面全部重算

    （`parse_pdf` 落在第 4 位、`copy_to_workspace`
    第 9 位、`send_email` 第 11 位。）排成 core-first 之后，
    发现工具只延长前缀，不改写它 —— 与 `<git_status>` 冻结同族：
    前缀只许增长，不许改写。

    不许用 `set` 的迭代序：字符串哈希逐进程随机化
    （`PYTHONHASHSEED`），那样连同一份 `_CORE_TOOLS` 在不同进程里都会排出
    不同的序 —— 跨会话那次的逐字命中就没了。
    这里锚的是 `_registry["tool_schemas"]` 的注册序（确定性），
    输入只用来判成员，不参与定序。

    唯一能证伪它的仪器是上游那行 `[cache]` ——
    下次真实请求即可对照；机械读者在 `tests/test_wire_order.py`。
    """
    core = [td for td in schemas if td["function"]["name"] in _CORE_TOOLS]
    rest = [td for td in schemas if td["function"]["name"] not in _CORE_TOOLS]
    return core + rest


def get_tool_schemas_filtered(names: list[str] | None = None) -> list[dict]:
    """Return tool schemas, optionally filtered to the given names.

    When *names* is None, behaves identically to :func:`get_tool_schemas`.

    返回顺序是 wire 序，不是注册序：常驻在前、
    其余追加在后（各组内部保注册序）—— 见 :func:`_core_first`。
    **与传入 *names* 的顺序无关**（它只用来判成员），
    所以调用方拿到的序是确定的、跨进程一致的。
    """
    if names is None:
        return _registry["tool_schemas"]

    # 按名字去重（保留首次出现的位置）。
    #
    # 这是发上 wire 的那条路：上游对重名是硬 400
    # （`Tool names must be unique`），一旦重名，那个会话直接不可用。
    # 根因已在 `register_tool` 里修掉（幂等替换），这里是第二道 ——
    # 且不静默：真丢掉了就吼一行，因为"兜底悄悄生效"正是一种静默失效。
    out: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for td in _registry["tool_schemas"]:
        nm = td["function"]["name"]
        if nm not in names:
            continue
        if nm in seen:
            dropped += 1
            continue
        seen.add(nm)
        out.append(td)
    if dropped:
        print(f"[registry] WARNING: wire 上有 {dropped} 个重名工具被丢掉 —— "
              f"注册表里出现了重复注册（`register_tool` 应当幂等，去核 "
              f"`/api/restart` 那条路），否则上游会 400 `Tool names must be unique`")
    # 出口一律是 wire 序（常驻在前 + 其余追加在后）。
    # 见 `_core_first` —— 发现一个工具只许延长前缀，不许改写它。
    return _core_first(out)


def get_tool_handler(name: str) -> Callable | None:
    """Return the handler callable for a given tool name, or None."""
    return _registry["tools"].get(name)


def list_tools() -> list[str]:
    """Return list of registered tool names."""
    return list(_registry["tools"].keys())


# ------------------------------------------------------------------
# Context-aware tool filtering
# ------------------------------------------------------------------

# 关键词触发表（_CONTEXT_TRIGGERS）与三个 bucket 已删除。
# 它们实现的是"按用户消息关键词挑选工具"的旧机制，已被下面的
# 模型驱动发现取代。保留它们只会留下第二套过滤路径。
# 详见本文件末尾「模型驱动的工具发现」一节。

# 常驻工具集（由数据重定，不是拍脑袋）
#
# 依据（两条，都可复跑）：
#
# 1. 真实频次 —— 事件日志 `data/events/*.jsonl` 的 `tool.start` 全量 135 次：
#       add_block 40 · list_workspace_files 18 · create_document 17 ·
#       get_document_info 13 · search_chunks 11 · summarize_document 8 ·
#       deliver_file 6 · parse_docx 5 · parse_txt 5 · search_tools 3
#    前 9 个占 126/135 = 93%；98 个工具里只有 17 个被用过。
# 2. 边际成本 —— `scripts/measure_request.py` 的留一法：
#       22 个常驻工具 = 4,486 tokens；其中 create_document 单个就 703。
#
# （数字是那一轮的量法；要现状就当场重跑 `scripts/measure_request.py`。）
#
# 入选标准（两类）：
#   a. 高频：上面前 9 个 —— 常走的路不该多一次往返；
#   b. "卡住时才需要"的兜底：模型不知道它存在就没法搜它。
#      `request_cross_zone_search` 正是这一类（工作区找不到文件时的唯一出路）；
#      `delete_files` 同理（删除有审批流程）。
#   c. 机制本身：`search_tools` —— 没有它，整套按需发现就断了。
#      `tests/test_tool_discovery.py:168` 断言它必须在常驻集里。
#
# 其余 ~86 个工具不是删掉，而是改由 `search_tools` 按需拉 ——
# 技能索引（`core/skill_index.py`，6 条 ≈ 241 tokens）负责让模型知道有什么，
# 索引出现在请求里但不进 messages、不落库（载体是 user 侧消息的副本）。
#
_CORE_TOOLS: set[str] = {
    # (c) 机制本身 —— 没有它就没有按需发现
    "search_tools",
    # (a) 高频（占 135 次调用里的 126 次）
    "add_block",              # 40
    "list_workspace_files",   # 18
    "create_document",        # 17  ← 也是最贵的一个（703 tokens）
    "get_document_info",      # 13
    "search_chunks",          # 11
    "summarize_document",     # 8
    "deliver_file",           # 6   ← 交付出口，任务多以它收尾
    "parse_docx",             # 5
    "parse_txt",              # 5
    # (b) 兜底：模型不知道它存在就没法搜它
    "request_cross_zone_search",  # 工作区找不到时的唯一出路（原注释：always visible）
    "delete_files",               # 删除是破坏性操作，且带审批流程
    # (b) 同上 —— 这两个虽然频次低，但必须常驻：
    # 15 种自然说法里 11 种搜不到 web_search（「上网搜一下今天有什么新闻」
    # → 返回 search_song/add_block/create_document…），因为它的中文检索面只有
    # `describe_tool` = "正在联网搜索" 一句，口语化的"搜一下"只靠单字兜底拿到 2 分，
    # 同分时按字母序输给 add_block/create_document，被挤出 top-5。
    # 模型只能回答"我没有联网能力"。而这正是 (b) 类要防的：
    # 它不知道自己有什么，就没法搜。
    # 成本（留一法）：这两个 schema 合计 295 tokens。
    # 更根子的洞见：**技能索引只扫 skills/*/manifest.yaml，MCP 内置工具不在里面** ——
    # 所以这两个连"索引里提一句"都没有。
    "web_search", "web_fetch",
    # (b) 同上 —— `run_command`。
    # 它必须常驻，理由与上面那两个 web 工具逐字相同：它是"框架会不会干活"
    # 这件事的唯一入口，而模型不知道它存在就没法搜它。
    # 技能索引救不了它 —— 索引只扫 `skills/*/manifest.yaml`，
    # 框架级工具不在里面。
    # 代价：一个 schema 的常驻税，见 `scripts/measure_request.py`。
    "run_command",
    # (b) 同上 —— `run_subagent`。
    # 它也必须常驻，判据与上面那几个逐字相同：模型不知道它存在就没法搜它，
    # 而它是"把活分出去"的唯一入口。技能索引同样救不了它（框架级工具不在索引里）。
    # 代价：schema 是每请求的永久税 —— 按规矩改描述必须重量一次。
    "run_subagent",
    # (b) 同上 —— `todo_write`。
    # 清单读写是框架的一等能力，不是某类技能的附属品，所以工具在框架层。
    # 判据：只有给用户看的活动行、没有给模型的计划载体时，
    # 多步任务里"做到哪一步"全靠上下文自己拼。
    # 常驻的判据与上面几个逐字相同：模型不知道它存在就不会去用它，
    # 而技能索引救不了它（索引只扫 `skills/*/manifest.yaml`，框架级工具不在里面）。
    # 代价：一个 schema 的常驻税。
    "todo_write",
    # (b)+(a) 同上 —— 文件原语五件套。
    # 五件全部常驻，这是这一批里最要紧的一条判断：
    # 不常驻的话模型会继续用 `run_command` 干同样的事 —— 而它常驻、能干、且
    # 代价是每次过审批 + 输出 4000 字符封顶 + 没有写前读。那正是要消除的东西，
    # 只把新工具放进"可搜索"里等于没换掉旧路（模型没有理由去找一个它不知道的替代）。
    # 这是本仓单笔最大的一笔常驻税（五个 schema）——
    # 改这五段 description 必须重量一次。
    "read_file", "write_file", "edit_file", "glob", "grep",
    # (b) 同上 —— `create_skill`。
    # 它必须常驻，判据与上面几个逐字相同：模型不知道它存在就没法搜它，
    # 而它是"给模型一条造 skill 的路"的唯一入口 —— 造 skill 这件事
    # 正是这个能力存在的理由（按流程操作可以自己制作 skill）。
    # 技能索引救不了它：索引只扫 `skills/*/manifest.yaml`，框架级工具不在里面。
    "create_skill",
    # (b) 同上 —— 记忆三件套。
    # 三件全部常驻（读+搜约 420 tokens；写是专用写通道 —— 三者同一条判据）：
    # 模型不知道它存在就没法用，而"记住这个"正是这个能力存在的理由；
    # 技能索引救不了它们（框架级工具不进索引）。写侧还有一条独立的理由：
    # tombstone 只有挂在专用写工具上才挂得住（`write_file` 是通用路径）。
    # 代价：三个 schema 的常驻税，改描述必须重量一次。
    "memory_read", "memory_search", "memory_write",
    # (b) 同上 —— `history_search`。
    # 判据与上面几个逐字相同，而且这里还多一层：模型知道自己少看了
    # （会话里就写着"共 N 条，已加载最近 M 条"）—— 告诉它门在哪，它才能补。
    # 代价：一个 schema 的常驻税。
    "history_search",
    # (b) 同上 —— 计划模式那两件。
    # 判据与本条里那一串逐字相同，而这一对还多两条更硬的理由：
    #   ① 它们是一对：只让模型拿到 `enter_plan_mode` 就等于造一个
    #      "进得去出不来"的陷阱（只有进没有出）。要么两个都在，要么都不在。
    #   ② `exit_plan_mode` 是计划模式的唯一出口，而计划模式下能用的工具
    #      只剩只读那些 —— 让出口走"先 search_tools 再发现"这条路，
    #      等于在最需要一个确定出口的时候给一个不确定的。
    # 代价：两个 schema 的常驻税。
    "enter_plan_mode", "exit_plan_mode",
    # (b) 同上 —— `pii_reveal`。
    # 判据与本条里那一串逐字相同，而这一件还多一层：
    #   模型手上看到的是 `【PII:1】`，而它的默认做法
    #   是停下来问用户：「这是脱敏标记，我不知道真实内容」——
    #   也就是说，不知道这扇门存在，它就会去打扰用户，而那正是
    #   「用户目的第一」要避免的那件事。
    # 代价：一个 schema 的常驻税。
    "pii_reveal",
    # (b) 同上 —— 周期任务三件。
    # 判据与本条里那一串逐字相同（模型不知道它存在就没法用它）——
    #   而这三件还多一条：用户会直接用大白话要（"每天早上八点半给我总结新闻"），
    #   那句话里没有"定时/任务/cron"任何一个词能拿去检索。
    # 代价：三个 schema 的常驻税。
    "cron_create", "cron_list", "cron_delete",
    # (b) 同上 —— `ask_user`。
    # 判据与本条里那一串逐字相同：模型不知道它存在，就只能用"在回复里
    #   写一段话问用户"那条路 —— 而这正是提问面板要换掉的东西。
    # 技能索引救不了它（框架级工具不进索引）。
    # 代价：一个 schema 的常驻税。
    "ask_user",
}
# 注：`parse_pdf` / `parse_csv` / `merge_pdfs` / `split_pdf` / `add_slide` /
# `export_document` / `extract_archive` / `web_search` / `web_fetch` 等移出常驻，
# 改由技能索引 + `search_tools` 按需拉。它们的频次都是 0~1 次（见上面第 1 条依据）。
# 若哪天发现某个被移出的工具反复搜不到，正确的修法是改索引的措辞，
# 不是把它塞回常驻 —— 塞回去就退回"用常驻表硬扛"的老路。


# ------------------------------------------------------------------
# 模型驱动的工具发现（取代关键词匹配）
# ------------------------------------------------------------------
#
# 关键词匹配的问题：它判的是主题（"这是关于文档的吗"），而任务需要什么
# 工具取决于任务形状（"要产出文件"）。后者没有可靠关键词。泄漏长这样：
#
#     用户："帮我读一下 workspace 里的《…》，输出一份摘要到 txt"
#     触发词 office/word/docx/文档/doc —— 一个都不含
#     → create_document / add_block / export_document 全被剔除
#     → 模型想写文件却没有写工具，只能反复 parse_txt / get_document_info
#        空转，最后说 "I'm stuck in a loop"
#
# 猜错的代价不只是少几个工具，而是模型没有任何补救手段。
#
# 现在：
#     常驻 _CORE_TOOLS  +  模型用 search_tools 自己检索
#     命中即标记为"已发现"，从下一轮起随 tools= 一起发出
#
# 检索不看用户消息：搜什么由模型自己写。用户那句话只是背景，
# 决定需求的是"下一步要干什么"，而那只有模型自己知道 ——
# 拿用户消息去猜工具，判的是主题，不是任务形状。

#: session_id → 模型已通过 search_tools 发现的工具名
_discovered: dict[str, set[str]] = {}

#: 被屏蔽的插件 —— 键是归属（`skill:<目录名>` /
#: `mcp:<服务器名>`），不是工具名。
#:
#: 为什么是归属而不是工具名：屏蔽的粒度是插件。
#: 按工具名屏蔽会长成一张没人维护的名单 ——
#: 而归属格（`tool_owners`）本来就有、且注册时写死。
#:
#: 它是这个仓库里唯一一个"负向"的活跃集：`_discovered` 只会加、
#: `forget_discovered` 只会整会话清 —— 除此之外没有"按名字排除"的机制。
#:
#: 判据落在 `get_active_tool_names` 里（不是 `get_tool_schemas_active`，
#: 也不是派发那两处）—— 因为 `tests/test_tool_gate.py` 按 AST 钉着
#: 「呈现与执行必须同经 `get_active_tool_names`」。加在任何一个视图上，
#: 都会造出第二个判定点。
_QUARANTINED: set[str] = set()

#: 用户主动停用的 MCP 服务器（`mcp:<服务器名>`）。
#:
#: 与 `_QUARANTINED` 分开两个集合，因为它们是两件事：
#:   · `_QUARANTINED` = 框架判的"这个插件坏了"
#:   · 这一个 = 用户点的"我不想让它跑"（MCP 管理浮窗）
#: 混成一个集合的话，「解封」会顺手把用户停用的也放出来
#:     —— 那正是「标签在说谎」的形状。
#: 但判定点只有一个：下面 `get_active_tool_names` 那一条并集 —— 见那里的注释。
_MCP_DISABLED: set[str] = set()

#: 用户主动停用的技能（`skill:<技能名>`）。状态来自设置里的
#: `skill_configs.<名>.state == "off"`，写入入口是 `bus/skill_state.py`。
#:
#: 与上面两个集合分开，理由相同：屏蔽是框架的决定，停用是用户的决定，
#: 撤销方式与拒绝时说的话都不一样；混在一起会让"解封"放出用户停用的东西。
#: 判定点仍然只有 `get_active_tool_names` 那一行。
_SKILL_DISABLED: set[str] = set()


def note_discovered(session_id: str, names) -> int:
    """记录模型发现的工具。返回本次新增数量（0 = 没有新东西）。

    会话条件工具拿不到时不收 —— 这是第二道。
    第一道在 `search_tools`（不露出来），但那里挡不住"模型自己报出一个名字"
    之后由别的路径进来；而 `_discovered` 只会加、`/clear` 之前不会清，
    收进来就等于把这个会话剩下的时间全卖给一份调不动的 schema。
    """
    if not session_id or not names:
        return 0
    names = [n for n in names if n in _registry["tools"]]
    if not names:
        return 0
    # 只有名字里真出现会话条件工具时才去算那一步 —— `_session_extra_tools`
    #   会碰档位/群账本（`current_spec` 会冻结档位），不该在常态路径上白付。
    hidden = _hidden_session_tools(session_id) if (_SESSION_TOOLS & set(names)) else set()
    bucket = _discovered.setdefault(session_id, set())
    before = len(bucket)
    bucket.update(n for n in names if n not in hidden)
    return len(bucket) - before


def get_discovered(session_id: str) -> set[str]:
    """某会话已发现的工具名。"""
    return set(_discovered.get(session_id, ()))


def forget_discovered(session_id: str) -> None:
    """会话结束时清理，防 `_discovered` 无界增长。"""
    _discovered.pop(session_id, None)


def _gated_tools() -> set[str]:
    """注册了、但当前不该发给模型的工具。

    与"被屏蔽的插件"同一个位置生效（`:func:`get_active_tool_names` 那一行），
    所以呈现与执行自动同时生效，不用第二处判定。

    今天只有 `code_intel`：它的 schema 是纯税，而没配语言服务器的机器用不到它。
    "配没配"是每进程算一次的（见 `core.code_intel.config_present`）——
    新增/删除 `lsp.json` 之后要重启框架。
    读不到 / 报错，不发（方向同 `blocked` 其余两源：拿不准就不给）。
    """
    try:
        from core import code_intel
        return set() if code_intel.config_present() else {code_intel.TOOL_NAME}
    except Exception:                                  # noqa: BLE001
        return set()


def get_active_tool_names(session_id: str = "") -> set[str]:
    """本轮该发给模型的工具 = 常驻工具 ∪ 已发现工具 − 被屏蔽/被停用插件的那一批。

    屏蔽那一步就加在这里 —— 于是"呈现"（`tools=`）
    与"执行"（`is_tool_active`）自动同时生效，不用在别处再写一遍判定。
    常驻集里的名字一般不属于任何插件（`tool_owners` 里没有它们），
    `get(...) is None` 那一支必须放行，否则一次屏蔽会把框架自己的工具带走。
    两个集合都空的时候一次字典都不用查（常态路径零代价）。

    多了一个来源（用户停用的 MCP）——
    但它不是第二个判定点：两者在这一行并成一个 `blocked`，
    而那一条集合差全仓仍然只有一处（`tests/test_plugin_quarantine.py` 钉着）。
谁要再加第三个来源（插件开关 / 档位…），加到这一行，不要另起一个 `if`。
    """
    names = (set(_CORE_TOOLS) | get_discovered(session_id)
             | _session_extra_tools(session_id))
    blocked = _QUARANTINED | _MCP_DISABLED | _SKILL_DISABLED | _gated_tools()
    if not blocked:
        return names
    return {n for n in names if _registry["tool_owners"].get(n) not in blocked}


def set_quarantined(owners) -> set[str]:
    """整体替换屏蔽集合（唯一写入口 —— 账本那一侧调它）。返回新集合。"""
    global _QUARANTINED
    _QUARANTINED = {str(o) for o in (owners or ()) if o}
    return set(_QUARANTINED)


def set_mcp_disabled(owners) -> set[str]:
    """整体替换用户停用的 MCP 集合（唯一写入口 —— `bus/mcp_disabled` 调它）。

    与 `set_quarantined` 同形：整体替换，不是增量。
      （增量写入口会造出第二个"当前是什么"的真相源。）
    """
    global _MCP_DISABLED
    _MCP_DISABLED = {str(o) for o in (owners or ()) if o}
    return set(_MCP_DISABLED)


def set_skill_disabled(owners) -> set[str]:
    """整体替换用户停用的技能集合（唯一写入口 —— `bus/skill_state` 调它）。"""
    global _SKILL_DISABLED
    _SKILL_DISABLED = {str(o) for o in (owners or ()) if o}
    return set(_SKILL_DISABLED)


def mcp_disabled_owners() -> set[str]:
    """当前被用户停用的 MCP 归属集合（只读副本）。"""
    return set(_MCP_DISABLED)


def skill_disabled_owners() -> set[str]:
    """当前被用户停用的技能归属集合（只读副本）。"""
    return set(_SKILL_DISABLED)


def quarantined_owners() -> set[str]:
    """当前被屏蔽的归属集合（只读副本）。"""
    return set(_QUARANTINED)


def is_owner_quarantined(owner: str) -> bool:
    """这个归属（`skill:<名>` / `mcp:<名>`）被屏蔽了没有。"""
    return str(owner) in _QUARANTINED


def is_owner_skill_disabled(owner: str) -> bool:
    """这个归属是不是被用户停用的技能。

    与 `is_owner_disabled`（MCP）分开，因为拒绝时的路线不同：
    技能去技能页启用，MCP 去 MCP 管理启用。
    """
    return str(owner) in _SKILL_DISABLED


def is_owner_disabled(owner: str) -> bool:
    """这个归属被用户主动停用了吗。

    与 `is_owner_quarantined` 必须是两个函数，即便两个集合参与
    同一条集合差：因为它们拒绝时说的话不一样 ——
    屏蔽那条的路线是「去修它」（框架说了算），
    停用这条的路线是「去启用它」（用户一键就能做到，在「MCP 管理」里）。
    合并成一个 `is_blocked` 的话，模型拿到的路线就会有一半是假的。
    """
    return str(owner) in _MCP_DISABLED


def get_tool_schemas_active(session_id: str = "") -> list[dict]:
    """`get_active_tool_names` 的 schema 视图（wire 上的 `tools=`）。"""
    return get_tool_schemas_filtered(list(get_active_tool_names(session_id)))


def is_tool_active(session_id: str, name: str) -> bool:
    """`get_active_tool_names` 的 执行视图 —— 同一个判定。

    呈现与执行必须是同一个函数，不是同一段逻辑。只藏 schema 是建议，
    不是强制：模型报出一个没发给它的名字，`get_tool_handler` 照样从全量注册表
    里把它捞出来执行。只藏工具 schema 是不够的 —— 呈现侧的单边过滤挡不住
    伪造调用：模型报出一个提示里说不存在的名字，执行侧照样会把它捞出来。
    所以呈现与执行必须由同一个判定管辖，而且是同一个函数，不是两段等价逻辑。

    它是"活"集合，不是"本轮逐字发过"的快照 —— 模型刚用 `search_tools`
    发现的工具立刻算数。理由是自家 `search_tools` 的契约：它的返回值只带
    `name` + 140 字符 description（不带 schema），而它的 description 写着
    「检索到的工具会立刻可用」。判定必须跟那句话一致，否则模型会撞进
    "刚照你说的做、你又说不行"的死循环，而那条话是自己写的。

    反向的不变量也在这条上：一份"禁用一个工具"的意图（权限层、
    将来的 enable/disable）只有在这一个函数里成立时才是真的 ——
    任何"在别处藏起来"的做法都是往"分级不强制"那条上再添一扇门。
    """
    return name in get_active_tool_names(session_id)


# search_tools：模型自己检索工具目录

def _query_terms(query: str) -> list[str]:
    """把查询切成检索词，按权重从高到低排列去重后的词表。

    中文没有空格，所以用 jieba 分词（本仓已有这个依赖，文档检索在用）。
    但 jieba 会切出"来首"/"首歌"这种组合，而工具描述里只写了"歌" ——
    所以额外补一层单字兜底（权重最低，只用于召回）。

    英文标识符再按 `_` 拆一遍："create document" 要能命中 "create_document"。
    """
    q = (query or "").strip()
    if not q:
        return []

    terms: list[str] = []
    try:
        import jieba
        terms = [w.strip() for w in jieba.cut(q) if w.strip()]
    except Exception:
        pass
    if not terms:
        terms = [t for t in q.replace("/", " ").split() if t] or [q]

    # 标识符拆词（create_document → create / document）
    for t in list(terms):
        if "_" in t:
            terms.extend(p for p in t.split("_") if p)

    # 单字兜底：只对中日韩字符做，避免英文单字母污染
    chars = [c for c in q if "一" <= c <= "鿿"]

    seen: set[str] = set()
    out: list[str] = []
    for t in terms + chars + [q]:
        tl = t.lower()
        if tl and tl not in seen:
            seen.add(tl)
            out.append(tl)
    return out


@functools.lru_cache(maxsize=2048)
def _word_tokens(text: str) -> frozenset:
    """把一段文本切成词集 —— 给 `description` / 中文活动行的"整词命中"用。

    为什么要有它：`t in desc_l` 是裸子串判定，
    而本仓的 `description` 是长段中文散文（为了把边界讲清楚，刻意写得长）
任何二字片段都能撞上，而且撞上的是散文里的半个词：
    `一下` ⊂「编号可以自动排序和更新」· `这个` ⊂「正是这个工具最常见的用途」。
    查询「读一下这个 pdf」，`add_word_caption` 靠 `一下` 拿了 +2，
    把真该第一的 `parse_pdf` 挤到第 5（`limit=5`，模型刚好差一点看不见）。

    只对 `desc` / `cn` 用整词。`hint`（`_SEARCH_HINTS`）刻意仍用子串 ——
    它是人工压过的短语表（`列宽调大调小` 这种按对写法的串），
    在那里子串命中是本意：jieba 把 `列宽调大调小` 切成
    `列/宽/调/大调/小`，整词判定会让 `列宽`、`调大` 两个都落空。
两处该用两把尺子，不是"统一成一种更严的"。

    键是文本本身（不是工具名），`register_tool` 就地换掉 description
    时缓存自己失效（键变了），不会留一份旧词集。
    """
    if not text:
        return frozenset()
    try:
        import jieba
        return frozenset(w.strip().lower() for w in jieba.cut(text) if w.strip())
    except Exception:  # jieba 没装/坏了，退化成按空格切，别让检索整个塌掉
        return frozenset(text.split())


def _has_cjk(text: str) -> bool:
    """查询里有没有中日韩表意文字 —— 只用来判「这条查询是不是中文」。"""
    return any("㐀" <= ch <= "鿿" for ch in (text or ""))


def _external_tool_servers() -> dict:
    """`{服务器名: 工具数}` —— 注册表里挂在外部 MCP 服务器下的工具。"""
    out: dict = {}
    for name in _registry["tools"]:
        if not name.startswith("mcp__"):
            continue
        parts = name.split("__")
        if len(parts) >= 3:
            out[parts[1]] = out.get(parts[1], 0) + 1
    return out


def _external_search_hint(query: str, top) -> str:
    """中文查询、且一个外部工具都没命中时，给一条「换英文再搜」的路线。

    外部 MCP 服务器的工具描述由服务器自己提供，通常是英文；中文关键词与它们
    在分词面上没有交集。症状不是「没装」—— 服务器接得好好的，工具也注册了，
    只是模型找不到。不替服务器翻译（我们不知道它的词表），只把这条路指出来。
    """
    if not _has_cjk(query):
        return ""
    if any(name.startswith("mcp__") for _, name, _ in top):
        return ""
    servers = _external_tool_servers()
    if not servers:
        return ""
    listed = "、".join("%s（%d 个）" % (k, servers[k]) for k in sorted(servers))
    return ("另有 %d 个外部工具，按各自的英语描述登记：%s。"
            "中文关键词通常匹配不到它们 —— 换英文能力词再检索一次，"
            "例如 open / analyze / capture / extract。"
            % (sum(servers.values()), listed))


def search_tools(query: str, limit: int = 5) -> dict:
    """按自然语言检索工具目录。

    打分权重：名字命中远重于描述命中 —— 名字是模型自己挑的，描述是我们写的：
    完全等于名字 +12，命中名字的某个片段 +8，名字里包含 +5，
    策展过的能力短语 +4，描述里包含 +2，中文活动行 +2。
    单字兜底命中只给 +1（"歌" → "搜索歌曲"）。

    两个"中文面"不是一回事：
    `core/activity.py::describe_tool` 那句话是给界面看的，用词偏书面
    （「正在设置单元格格式」）；用户会说「把这列居中」—— 那个词两处都没有，
    于是 `format_excel_range` 掉到 93 / 158（`limit` 默认 5）。
补的是 `_SEARCH_HINTS`（只给检索、不渲染），不是改写那句活动行。
    """
    terms = _query_terms(query)
    if not terms:
        return {"success": False, "error": "empty query", "tools": [],
                "total": 0, "remaining": 0}

    scored = _score_tools(terms)

    # 会话条件工具（`_SESSION_TOOLS`）
    #   在这条会话拿不到时不许露出来。检索面扫的是全注册表，不设这道闸
    #   就会造出「搜得到、调不动」——正是 `_SESSION_TOOLS` 那段注释里
    #   ②那条假承诺，只不过载体从索引换成了 search_tools。
    sid = _current_session_id()
    hidden = _hidden_session_tools(sid) | _gated_tools()
    if hidden:
        scored = [row for row in scored if row[1] not in hidden]

    # 还剩多少工具尚未加载 —— 只数注册表里真实存在的，
    # 不能拿 _CORE_TOOLS 的条目数去减（里面有已废弃的名字）。
    # 隐藏的那些也不算 remaining：它们不是"搜一下就能拿到"的东西。
    active = get_active_tool_names(sid)
    remaining = sum(1 for td in _registry["tool_schemas"]
                    if td["function"]["name"] not in active
                    and td["function"]["name"] not in hidden)

    top = scored[:max(1, int(limit or 5))]
    result = {
        "success": True,
        "tools": [{"name": n, "description": d[:140]} for _, n, d in top],
        "total": len(scored),
        "remaining": remaining,
    }
    hint = _external_search_hint(query, top)
    if hint:
        result["hint"] = hint
    return result


#: 单字兜底的区分力闸。
#:
#: 判据是量出来的（本机 146 个工具，`description` + 中文活动行）：
#: ```
#:   拆 1%  宽 5%  点 3%  复 8%  制 8%  读 10% 大 11%     ← 携带信息
#:   这 14% 区 17% 中 19% 工 22% 个 23% 列 24% 件 27%     ← 边界
#:   文 48% 下 60% 到 64% 一 65%  把/的/是 ≈ 随处可见      ← 零信息
#: ```
#: 一个字若出现在一成以上的工具里，它就不足以区分（"哪个工具没提过'的'"）。
#: 这不只是省几分：原来的地板对所有人一样，所以它既抬不高真答案、
#: 也分不开并列 —— 「把这个 pdf 拆开」里 7 个 pdf 工具并列 13，
#: 而 `split_pdf` 唯一多出来的正是那个 1% 的 `拆`。
#: 阈值是拍的、分布是量的 —— 12% 落在上面 11% 与 14% 之间那道缝里。
_COMMON_CHAR_DF = 0.12


def _common_chars(chars) -> frozenset:
    """这些词里哪些遍地都是 —— 单字与多字词同一把尺子。

    只治单字不够：同一个病长在多字词上 —— 查询里的 `工作`（`工作区`）
    命中了 `保护工作表` / `重命名工作表` 的提示，而那些工具与"复制文件"毫无关系。
判据一样：弱源（desc / 活动行 / 提示）里，遍地都是的词不携带信息。

    只闸弱源，不闸名字 —— 名字是强信号（`+12/+8/+5`），
    且按名字搜是"总不变量"那条兜底（`test_every_tool_is_either_resident_or_findable`），
    把它闸掉会让某些工具彻底不可达。
    """
    chars = list(chars)
    if not chars:
        return frozenset()
    n = len(_registry["tool_schemas"])
    if not n:
        return frozenset()
    limit = max(1, int(n * _COMMON_CHAR_DF))
    counts = dict.fromkeys(chars, 0)
    for tool_def in _registry["tool_schemas"]:
        fn = tool_def["function"]
        text = " ".join(fn.get("description", "").split()).lower()
        try:
            from core.activity import describe_tool
            text += " " + describe_tool(fn["name"]).lower()
        except Exception:
            pass
        for c in chars:
            if c in text:
                counts[c] += 1
    return frozenset(c for c, k in counts.items() if k > limit)


def _score_tools(terms: list[str]) -> list[tuple[int, str, str]]:
    """给注册表里每个工具打分并排序 —— 打分面只有这一处。

    抽出来是给仪器用的：`search_tools` 只回名字与描述，
    不带分数，调参时只能看见"名次"，看不见"差多少分"，
    而改噪声地板这类活必须看得见分数（否则"名次变了"是好是坏全靠猜）。
    调用方只有两个：`search_tools` 与 `scripts/check_skill_reach.py` 式的探针。
    """
    scored: list[tuple[int, str, str]] = []
    common = _common_chars(terms)
    for tool_def in _registry["tool_schemas"]:
        fn = tool_def["function"]
        name = fn["name"]
        name_l = name.lower()
        name_parts = set(name_l.split("_"))
        desc_l = " ".join(fn.get("description", "").split()).lower()
        #: 描述按整词命中（裸子串会撞上散文里的半个词，见 `_word_tokens`）。
        desc_w = _word_tokens(desc_l)

        # 中文检索面
        #
        # 问题：大量工具的 description 只有英文（"Merge multiple PDF files…"），
        # 而查询是中文 —— jieba 切出来的中文词永远不可能匹配英文文本。
        # 工具一旦移出常驻集，这个语言缺口就变成"不可达"：
        # 10 个被移出的工具里 3 个中文查不到（模型想干的事没有工具）。
        #
        # 修法：把 `core/activity.py` 那张现成的中文表接进打分面 ——
        # 它本来只用于活动显示，但每个工具都有（含前缀族与兜底）。
        # 不新建同义词表（那就是第二套真相源）。
        try:
            from core.activity import describe_tool
            cn_l = describe_tool(name).lower()
        except Exception:
            cn_l = ""
        cn_w = _word_tokens(cn_l)

        # 策展过的检索面
        #
        # 它比 description 与 `_VERBS` 那句话都重（4 vs 2）—— 因为它是
        # 人工写的、用户自己的说法，而那两个一个是机器给的英文、一个是
        # 给界面看的句子。
        #
        # 没写的工具一分不少 —— 它在 `core/activity.py` 里是可选的
        # （`_VERBS` 仍然在打分面上），所以这张表可以慢慢补，补一条赚一条，
        # 不存在"漏了谁就静默降级"（那正是本仓最怕的"两套真相源"）。
        try:
            from core.activity import search_hint
            hint_l = search_hint(name).lower()
        except Exception:
            hint_l = ""

        score = 0
        for t in terms:
            if len(t) == 1:
                if t.isascii():
                    # 单个 ASCII 字母零信息，直接丢掉。
                    #
                    # 查询「把这张表的 A 到 Y 列居中」里，jieba 把 `A`、`Y`
                    # 当词切出来（它们是列号，不是词），于是 `t in desc_l`
                    # 对 几乎每一个英文 description 都成立（谁没有字母 a / y？）
                    # 每个工具白拿 1~2 分，噪声地板抬到 8 分，
                    # 而一条 +4 的能力短语永远浮不上来（`format_excel_range`
                    # 排 15 / 158，前 12 名全是靠 `a`/`y`/`把`/`的` 刷上来的）。
                    #
                    # 这不是判据变了 —— `_query_terms` 的 docstring 早就写着
                    # 「单字兜底：只对中日韩字符做，避免英文单字母污染」，
                    # 而它只过滤了自己加的那份 `chars`，jieba 自己切出来的
                    # `a` / `y` 照样进了词表，这里又把它们当"单字兜底"处理。
                    # 补的是那句话没被执行到的地方。
                    continue
                # 单字兜底：只认描述，避免 "文" 这类字在工具名里乱命中。
                # 遍地都是的字不算数 ——
                # 若每个命中的字各 +1，`一`(65%)/`到`(64%)/`文`(48%)
                # 这种字谁都有，中文句子越长、描述越长，白拿的分越多：
                # 「把列宽调大一点」`plot_excel_data` / `read_excel_data`
                # 靠 `把/列/调/一/点` 纯凑单字拿到 5，把真答案的 4 反超。
                # 不是"单字一律不算" —— 兜底要留着（`歌` → `search_song`），
                # 只是说话要大点声：见 `_COMMON_CHAR_DF` 那张分布。
                if t in common:
                    continue
                if t in desc_l or t in cn_l:
                    score += 1
                continue
            if t == name_l:
                score += 12
            elif t in name_parts or (t + "s") in name_parts:
                # `t + "s"`：词形缺口。名字片段是复数时，查询里的单数形式
                # 只会落到下面的 `t in name_l` 拿 5 分，而单数片段的工具拿 8 分 ——
                # 同一个词两种分：`merge_pdfs` 的片段是
                # `pdfs`，查询「把几个 pdf 合成一个」里 `pdf` 只给 5 分，它被
                # `parse_pdf` / `pdf_info` / `split_pdf`（片段是 `pdf`）挤到第 6，
                # `test_skill_index` 的"移出的工具还搜得到"当场变红。
                # 只认"单数查询 vs 复数片段"这一种，不做任意前缀匹配 ——
                # 前缀会把 "con" 这类碎片也算进 `convert`，那是新的乱命中。
                # 同族受益：`search_emails`（片段 `emails`）现在认查询里的 `email`。
                score += 8
            elif t in name_l:
                score += 5
            # 弱源：遍地都是的词不算数（见 `_common_chars`）。
            if t in common:
                continue
            # 描述/活动行：整词命中 —— 见 `_word_tokens` 的 docstring。
            if t in desc_w:
                score += 2
            if t in cn_w:
                score += 2
            # 策展提示：子串命中（有意，别"统一"成整词 —— 见 `_word_tokens`）。
            if t in hint_l:
                score += 4
        if score > 0:
            scored.append((score, name, desc_l))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored


#: search_tools 的 schema。由 init_bus 调 register_search_tool() 注册。
SEARCH_TOOLS_SCHEMA: dict = {
    "description": (
        "检索工具目录，找出完成当前任务所需的工具。"
        "当你需要的能力不在当前可用工具里时调用它 —— 按自然语言描述你要做什么即可。"
        "检索到的工具会立刻可用。"
    ),
    "inputSchema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "你要做什么，自然语言即可，例如 '把内容写进一个文本文件'",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回几个工具（默认 5）",
            },
        },
        "required": ["query"],
    },
}


def register_search_tool() -> None:
    """注册 search_tools 工具。由 `init_bus` 调用（幂等）。"""
    if "search_tools" in _registry["tools"]:
        return
    register_tool("search_tools", search_tools, SEARCH_TOOLS_SCHEMA)


def register_subagent_tool() -> None:
    """注册 `run_subagent`。由 `init_bus` 调用（幂等）。

    注册的 handler 不会被调用 —— 它是一根占位，只为让 schema 出现在
    `tools=` 里、让执行侧门禁认得出这个名字。真正的派发在
    `core/agent_loop.py::_dispatch_subagent` 那一支（与 `search_tools` 同形：
    循环手里才有请求快照 / provider / 那条 assistant 消息，而它们是 fork 的全部输入）。
谁要是在这里 `handler(kwargs)` 调它，那是个错误路径：它只回一句说明**。
    """
    if "run_subagent" in _registry["tools"]:
        return
    from core.subagent import SUBAGENT_TOOL_SCHEMA

    # 有 agents/*.md 定义时，schema 追加 agent 参数与可用清单；没有则原样使用。
    schema = SUBAGENT_TOOL_SCHEMA
    try:
        from core import subagent_defs
        schema = subagent_defs.tool_schema(SUBAGENT_TOOL_SCHEMA)
    except Exception as exc:                              # noqa: BLE001
        print("[bus] 读取子 agent 定义失败：%s: %s" % (type(exc).__name__, exc))

    def _not_dispatched(**kwargs) -> dict:
        return {
            "success": False,
            "error": ("run_subagent 必须由 agent 循环特判派发（它需要请求快照）—— "
                      "直接调用它没有意义，这是一个接线错误。"),
        }

    register_tool("run_subagent", _not_dispatched, schema)


def register_workflow_tool() -> None:
    """注册 `run_workflow`。由 `init_bus` 调用（幂等）。

    与 `run_subagent` 逐字同形：handler 是一根占位，真正的派发在
    `core/agent_loop.py::_dispatch_workflow`（循环手里才有请求快照 / provider /
    那条 assistant 消息 —— 而它们是 fork 的全部输入）。

    注册 ≠ 每次都发：它只在 ultra 档进 `tools=`
    （`_SESSION_TOOLS` + `_session_extra_tools`），其余档位一分钱不付。
    """
    if "run_workflow" in _registry["tools"]:
        return
    from core.workflow import WORKFLOW_TOOL_SCHEMA

    def _not_dispatched(**kwargs) -> dict:
        return {
            "success": False,
            "error": ("run_workflow 必须由 agent 循环特判派发（它需要请求快照）—— "
                      "直接调用它没有意义，这是一个接线错误。"),
        }

    register_tool("run_workflow", _not_dispatched, WORKFLOW_TOOL_SCHEMA)


#: 会话条件工具 —— 只有某些会话才发的工具。
#: 今天三个：`ask_member`（群会话）· `run_workflow`（ultra 档）· `computer`（接管开启）。
#:
#: ## 它是第三类，别并进那两类
#: · 常驻（`_CORE_TOOLS`）：每轮都在 `tools=` 里 —— 代价是每条请求的税。
#: · 索引（`core/skill_index`）：非常驻的靠一行索引"被知道"，再 `search_tools` 去拉。
#: · 本类：两边都不进。理由成对：
#:   ① 它不该让普通对话付税（所以不常驻）；
#:   ② 它更不该被索引承诺 —— 索引是全体会话共读的一段文本，
#:      而"按会话算"的东西写进去就是一句假承诺：普通会话里模型看得到它、
#:      调不动它，执行侧会拒（不许承诺兑现不了的东西）。
#:
#: 可达性由 `get_active_tool_names` 按会话保证（`_session_extra_tools`）。
#: 加一个新成员时必须同时在 `core/skill_index._leftover_groups` 的
#:   排除名单里出现（它读的就是这个常量，不是自己再抄一份名单）——
#:   漏了的话它会掉进「其他已注册工具」那一桶，又变回全体的税，
#:   而两条索引不变量照样绿（那一桶本来就在覆盖里）。
#: 加成员时最容易漏的就是这个常量：只补 `_session_extra_tools`
#: 所以它同时出现在索引兜底桶和 `search_tools` 的检索面里
#:   （`_score_tools` 扫全注册表）——「打开记事本」这类普通请求能把它排到第 1，
#:   然后 `note_discovered` 把它永久塞进那个会话的 `tools=`。
#: 检索面也读这个常量（`_hidden_session_tools`），且 `note_discovered`
#:   第二道拒收 —— 三条路（呈现 / 检索 / 发现）必须同口径。
_SESSION_TOOLS: frozenset[str] = frozenset(
    {"ask_member", "run_workflow", "computer", "browser",
     "request_compaction"})


def register_group_tool() -> None:
    """注册 `ask_member`。由 `init_bus` 调用（幂等）。

    注册 ≠ 每次都发 —— `get_active_tool_names` 只在一个条件成立时把它
    加进去：这条会话真的是一个群会话、而且那个群还在（见 `_session_extra_tools`）。
普通对话的 `tools=` 里没有它（schema 是每条请求的税，
    不该让所有人替群聊付钱）。

    与 `run_subagent` 同形：这个 handler 不会被调用（它只回一句说明），
    真正的派发在 `core/agent_loop.py` 那一支（循环手里才有请求快照 / provider /
    那条 assistant 消息 —— 而它们是 fork 的全部输入）。
    """
    if "ask_member" in _registry["tools"]:
        return
    from core.group_dispatch import ASK_MEMBER_TOOL_SCHEMA

    def _not_dispatched(**kwargs) -> dict:
        return {
            "success": False,
            "error": ("ask_member 必须由 agent 循环特判派发（它需要请求快照）—— "
                      "直接调用它没有意义，这是一个接线错误。"),
        }

    register_tool("ask_member", _not_dispatched, ASK_MEMBER_TOOL_SCHEMA)


def _session_extra_tools(session_id: str) -> set[str]:
    """这条会话额外发哪些工具。

    每个判据都是"在会话内稳定"：

    | 工具 | 条件 |
    |---|---|
    | `ask_member` | 会话键形状是群聊（`group_id_of`）且那个群真的还在（`groups.get`） |
    | `run_workflow` | 本会话冻在 ultra 档（第 6 档） |

    为什么要这个机制而不是常驻/索引：两者都是"按会话算"的——
      写进常驻 = 所有会话替少数会话付税；写进索引 = 对普通会话承诺一个调不动的工具
      （不许承诺兑现不了的东西）。由 `get_active_tool_names` 按会话保证。
    失败一律不发（`except`，空集）：工具表是每条请求都要算的东西，
      它不能因为一个读不到的账本而炸掉整轮（这里"读不到"的正确方向
      是少发一个工具，不是"崩"）。
    """
    if not session_id:
        return set()
    out: set[str] = set()
    if "ask_member" in _registry["tools"]:
        try:
            from core.session_key import group_id_of
            gid = group_id_of(session_id)
            if gid:
                from core import groups
                if groups.get(gid):
                    out.add("ask_member")
        except Exception as exc:                                # noqa: BLE001
            print(f"[registry] 算群工具失败（{type(exc).__name__}: {exc}）—— 这一轮不发它")
    if "request_compaction" in _registry["tools"]:
        # 只在压力窗口（≥80%）里发；读数由上一次请求记下（一步滞后，可接受）。
        #   判据：常驻 = 所有人替它付 schema 税，而它一年用不了几次。
        try:
            from core import context_pressure
            if context_pressure.armed(session_id):
                out.add("request_compaction")
        except Exception as exc:                                # noqa: BLE001
            print(f"[registry] 算压缩工具失败（{type(exc).__name__}: {exc}）"
                  f" —— 这一轮不发它")
    if "computer" in _registry["tools"]:
        # 接管工具只发给持有接管的那个会话。
        #   判据与上面两条同一条（"按会话算、且在会话内稳定"）：
        #   写进常驻 = 所有会话替它付 schema 税；只靠索引 = 对普通会话
        #   承诺一个调不动的工具。
        try:
            from core.computer import session as _cu
            if _cu.enabled_for(session_id):
                out.add("computer")
        except Exception as exc:                                # noqa: BLE001
            print(f"[registry] 算接管工具失败（{type(exc).__name__}: {exc}）"
                  f" —— 这一轮不发它")
    if "browser" in _registry["tools"]:
        # 浏览器接管与 `computer` 同一个持有者 ——
        #   同一次接管里的另一条通道，不是第二个开关。
        try:
            from core.computer import session as _cu
            if _cu.enabled_for(session_id):
                out.add("browser")
        except Exception as exc:                                # noqa: BLE001
            print(f"[registry] 算浏览器工具失败（{type(exc).__name__}: {exc}）"
                  f" —— 这一轮不发它")
    if "run_workflow" in _registry["tools"]:
        try:
            from core import tiers
            #: ultra = 第 6 档。`current_spec` 会冻结档位——而这一趟
            #:   本来就在一轮里（`AgentLoopStage` 已经读过同一个档），值不会变。
            if tiers.current_spec(session_id).level >= 6:
                out.add("run_workflow")
        except Exception as exc:                                # noqa: BLE001
            print(f"[registry] 算工作流工具失败（{type(exc).__name__}: {exc}）"
                  f" —— 这一轮不发它")
    return out


def is_session_only_tool(name: str) -> bool:
    """这个工具是不是「只能按会话放行」的那一类（`_SESSION_TOOLS` 的公开读法）。

    给 `agent_loop._not_offered_message` 用：拒绝时要区分「搜一下就能拿到」
    与「搜了也拿不到」（后者不许承诺 `search_tools('<name>')` —— 那是空头支票）。
    """
    return str(name or "") in _SESSION_TOOLS


def _hidden_session_tools(session_id: str) -> set[str]:
    """这条会话现在拿不到的会话条件工具。

    这一道是`search_tools` 与 `note_discovered` 共同的落点 ——
    检索面扫的是全注册表，不设这道闸的话，「打开记事本」这类普通请求
    能把 `computer` 搜到第 1 名，然后它经 `note_discovered` 永久进入那个会话的
    `tools=`（一份 schema 税 + 每次都调不动）。
    空会话号（仪器 / 无请求上下文），全部隐藏：「没人持有」就是全部拿不到。
    """
    return set(_SESSION_TOOLS) - _session_extra_tools(session_id)


def _current_session_id() -> str:
    """正在跑的工具调用属于哪条会话；取不到 = 空串（同 `todo_write` 那条）。"""
    try:
        from core.request_snapshot import current_session_id
        return current_session_id()
    except Exception:                                          # noqa: BLE001
        return ""


def register_command_tool() -> None:
    """注册 `run_command`。由 `init_bus` 调用（幂等）。

    这里只是一根接线 —— 判定与执行分别在 `core/command_policy.py`
    （纯函数）与 `core/shell.py`（唯一碰 subprocess 的地方）。放在 `bus/`
    是因为它是框架级工具（和 `search_tools` 一样不属于任何 skill），
    不是因为它有什么总线逻辑。

    「写了」≠「会调它」：声明了但没人读的配置有几种常见形状 ——
    高风险命令表零 ask 零 deny、错误码零构造点、`sandbox` 字段零读者。
    所以这条路径有两处锚点：这个函数存在，且 `init_bus` 真的调用它。
    """
    if "run_command" in _registry["tools"]:
        return
    from core.shell import COMMAND_TOOL_SCHEMA, run_command
    register_tool("run_command", run_command, COMMAND_TOOL_SCHEMA)


def register_computer_tool() -> None:
    """注册 `computer`。由 `init_bus` 调用（幂等）。

    框架级工具；注册 ≠ 每次发 —— 只有持有接管的那个会话的 `tools=` 里
      才有它（`_session_extra_tools`），平时零 schema 税。
    handler 是真的：会话从请求快照上取（同 `todo_write`）。
    """
    if "computer" in _registry["tools"]:
        return
    from core.computer.tools import COMPUTER_TOOL_SCHEMA, computer
    register_tool("computer", computer, COMPUTER_TOOL_SCHEMA)


def register_browser_tool() -> None:
    """注册 `browser`。由 `init_bus` 调用（幂等）。

    与 `computer` 同一条判据：注册 ≠ 每次发 —— 只有持有接管的那个会话的
      `tools=` 里才有它（`_session_extra_tools`），平时零 schema 税。
    """
    if "browser" in _registry["tools"]:
        return
    from core.browser.tools import BROWSER_TOOL_SCHEMA, browser
    register_tool("browser", browser, BROWSER_TOOL_SCHEMA)


def register_todo_tool() -> None:
    """注册 `todo_write`。由 `init_bus` 调用（幂等）。

    放在这里是因为它是框架级工具（不属于任何 skill），不是因为它有总线
    逻辑 —— 状态与渲染都在 `core/todos.py`，这一句只是接线。

    与 `register_subagent_tool` 不同：那个的 handler 是占位（真派发在
    agent 循环里，因为 fork 需要请求快照 / provider / 那条 assistant 消息）；
    本工具的 handler 是真的，它要的东西（会话号）能从请求快照上取到。
    """
    if "todo_write" in _registry["tools"]:
        return
    from core.todos import TODO_TOOL_SCHEMA, todo_write
    register_tool("todo_write", todo_write, TODO_TOOL_SCHEMA)


def register_outbound_tool() -> None:
    """注册 `send_outbound`。由 `init_bus` 在技能装载之后调用（幂等）。

    描述里列出已注册的动作名；动作是插件在装载时注册的，因此这个函数要排在
    技能之后。热加载进来的动作不在描述里，但调用时会列出可用名字。
    """
    if "send_outbound" in _registry["tools"]:
        return
    from core import outbound

    actions = outbound.available_actions()
    extra = ("；已注册的动作：" + "、".join(actions)) if actions else ""
    schema = {
        "description": (
            "给指定会话发一条消息或文件（可以是另一个平台、另一段对话）。"
            "收件人格式 platform:kind:id，例如 telegram:private:123456。"
            "发出去收不回，调用前会请用户确认收件人。" + extra),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string",
                       "description": "收件人，格式 platform:kind:id"},
                "text": {"type": "string", "description": "消息正文"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "要附带的文件路径（必须在工作区内）"},
                "action": {"type": "string",
                           "description": "出站动作，默认 chat（用框架通道发）"},
                "params": {"type": "object",
                           "description": "动作参数（非 chat 动作时使用）"},
            },
            "required": ["to"],
        },
    }

    def _handler(**kwargs) -> dict:
        return outbound.send(**kwargs)

    register_tool("send_outbound", _handler, schema)


def register_task_tool() -> None:
    """注册 `task`（查 / 等 / 取消后台任务）。由 `init_bus` 调用（幂等）。

    按需发现：它不在 `_CORE_TOOLS` —— 只有这个会话真产生过后台任务
    （`core/shell.py::_to_background` 调 `note_discovered`）时才挂进工具表。
    注册了但没发现，会拒；这是对的（没产生任务就不该付它的 schema 税）。
    """
    if "task" in _registry["tools"]:
        return
    from core.task_tool import TASK_TOOL_SCHEMA, task_tool
    register_tool("task", task_tool, TASK_TOOL_SCHEMA)


def register_ask_user_tool() -> None:
    """注册 `ask_user`。由 `init_bus` 调用（幂等）。

    与 `register_todo_tool` 同类：框架级工具，handler 是真的
    （它只做校验 + 造挂起载荷；挂起与结算在管线里）。这一句只是接线。
    """
    from core.ask_user import SCHEMA, ask_user
    register_tool("ask_user", ask_user, SCHEMA)


def register_cron_tools() -> None:
    """注册周期任务三件。由 `init_bus` 调用（幂等）。

    放在这里是因为它们是框架级工具（不属于任何 skill），不是因为有总线
    逻辑 —— 规则解释、两张表、执行与投递都在 `core/cron/` 下，这一句只是接线。
    与 `register_subagent_tool` 不同：那个的 handler 是占位（真派发在
    agent 循环里）；本组三件的 handler 是真的 —— 它们要的"从哪个渠道来的"
    从请求快照上取（`request_snapshot.current_channel()`）。
    """
    from core.cron.tools import (CRON_CREATE_SCHEMA, CRON_DELETE_SCHEMA,
                                 CRON_LIST_SCHEMA, cron_create, cron_delete,
                                 cron_list)
    for name, fn, schema in (("cron_create", cron_create, CRON_CREATE_SCHEMA),
                             ("cron_list", cron_list, CRON_LIST_SCHEMA),
                             ("cron_delete", cron_delete, CRON_DELETE_SCHEMA)):
        if name in _registry["tools"]:
            continue
        register_tool(name, fn, schema)


def register_plan_tools() -> None:
    """注册计划模式的两个工具。由 `init_bus` 调用（幂等）。

    放在这里是因为它们是框架级工具（不属于任何 skill），不是因为有总线
    逻辑 —— 判定、状态与话术都在 `core/plan.py`，这一句只是接线。
    两个一起注册（它们是一对）：只注册进入工具，计划模式就成了模型
    进得去、出不来的陷阱 —— 出口必须和入口同时可用。
    """
    from core.plan import (
        ENTER_TOOL_NAME, ENTER_TOOL_SCHEMA, EXIT_TOOL_NAME, EXIT_TOOL_SCHEMA,
        enter_plan_mode, exit_plan_mode,
    )
    if ENTER_TOOL_NAME not in _registry["tools"]:
        register_tool(ENTER_TOOL_NAME, enter_plan_mode, ENTER_TOOL_SCHEMA)
    if EXIT_TOOL_NAME not in _registry["tools"]:
        register_tool(EXIT_TOOL_NAME, exit_plan_mode, EXIT_TOOL_SCHEMA)


def register_file_tools() -> None:
    """注册文件原语五件套。由 `init_bus` 调用。

    与 `register_todo_tool` 同类：框架级工具，handler 是真的
    （它们要的东西 —— 会话号、安全核 —— 分别从请求快照与已注册的 workspace
    manager 上取得到），这一句只是接线。

    **这里刻意没有 `if ... in _registry["tools"]: return` 那道早退** ——
    `register_tool` 现在幂等（同名就地替换），而「重启框架」会在
    同一进程里重注册一遍。留着早退 = 重启后还挂着旧 handler。
直接重注册，让刷新真的发生。
    """
    from core.file_tools import (
        EDIT_FILE_SCHEMA, GLOB_SCHEMA, GREP_SCHEMA,
        READ_FILE_SCHEMA, WRITE_FILE_SCHEMA,
        edit_file, glob, grep, read_file, write_file,
    )

    register_tool("read_file", read_file, READ_FILE_SCHEMA)
    register_tool("write_file", write_file, WRITE_FILE_SCHEMA)
    register_tool("edit_file", edit_file, EDIT_FILE_SCHEMA)
    register_tool("glob", glob, GLOB_SCHEMA)
    register_tool("grep", grep, GREP_SCHEMA)


def register_program_tool() -> None:
    """注册 `run_program`（子进程 + 桥的工具编排）。由 `init_bus` 调用。"""
    from core.tool_program import SCHEMA, TOOL_NAME, run_program

    register_tool(TOOL_NAME, run_program, SCHEMA)


def register_code_intel_tool() -> None:
    """注册 `code_intel` —— 只有配置了语言服务器时才注册。由 `init_bus` 调用。

    与其余框架级工具不同的一条：没配置就不注册。理由是这个工具的
    schema 是纯税（每请求都交），而没装语言服务器的机器一次都用不到它。
    配置在注册时读一次（项目根 / 全局）；之后改 `lsp.json` 需要重启框架
    才重新判定"要不要出现" —— 但服务器表在每次调用时重读，改条目不用重启。
    没有早退 —— 同 `register_file_tools` 的理由（`register_tool` 幂等，
    而「重启框架」会在同一进程里重注册，留着早退就还挂着旧 handler）。
    """
    from core import code_intel

    register_tool(code_intel.TOOL_NAME, code_intel.code_intel,
                  code_intel.SCHEMA)
    if code_intel.config_present():
        print("[bus] code intelligence registered (code_intel)")
    else:
        print("[bus] code_intel 已注册但**不发给模型**（没有配置语言服务器）")


def register_skill_factory_tool() -> None:
    """注册 `create_skill`。由 `init_bus` 调用（幂等）。

    与 `register_file_tools` 同类：框架级工具，handler 是真的
    （它要的只是 `skills/` 那一条相对路径），这一句只是接线。
    没有早退 —— 同 `register_file_tools` 的理由（`register_tool` 幂等，
    而「重启框架」会在同一进程里重注册，留着早退就还挂着旧 handler）。
    """
    from core.skill_factory import CREATE_SKILL_SCHEMA, create_skill

    register_tool("create_skill", create_skill, CREATE_SKILL_SCHEMA)


def register_memory_tools() -> None:
    """注册记忆三件套。由 `init_bus` 调用（幂等）。

    与 `register_file_tools` 同类：框架级工具，handler 是真的
    （存储 / 索引 / tombstone 的真身都在 `core.memory_store`），这一句只是接线。
    没有早退 —— 同 `register_file_tools` 的理由（`register_tool` 幂等，
    而「重启框架」会在同一进程里重注册，留着早退就还挂着旧 handler）。
    """
    from core.memory_store import (
        MEMORY_DELETE_SCHEMA, MEMORY_READ_SCHEMA, MEMORY_SEARCH_SCHEMA,
        MEMORY_WRITE_SCHEMA, memory_delete, memory_read, memory_search,
        memory_write,
    )

    register_tool("memory_read", memory_read, MEMORY_READ_SCHEMA)
    register_tool("memory_search", memory_search, MEMORY_SEARCH_SCHEMA)
    register_tool("memory_write", memory_write, MEMORY_WRITE_SCHEMA)
    register_tool("memory_delete", memory_delete, MEMORY_DELETE_SCHEMA)


def register_ice_reveal_tool() -> None:
    """注册 `pii_reveal`。由 `init_bus` 调用（幂等）。

    与 `register_memory_tools` 同类：框架级工具，真身 `core/ice/reveal.py`
    （handler 自己发那个三态审批载荷），这一句只是接线。
    没有早退 —— 同 `register_file_tools` 的理由（`register_tool` 幂等，
    而「重启框架」会在同一进程里重注册）。
    它在 `_APPROVAL_CAPABLE_TOOLS` 里（`core/agent_loop.py`）——
    那个集合的含义正是"它自己会碰到一次审批，兜底闸别再问"。
    不在就是双问：`destructive=True`，兜底闸先问，用户同意之后
    handler 再发一次载荷。
    """
    from core.ice.reveal import TOOL_NAME, TOOL_SCHEMA, reveal_handler

    register_tool(TOOL_NAME, reveal_handler, TOOL_SCHEMA)


def register_compaction_tool() -> None:
    """注册 `request_compaction`。由 `init_bus` 调用（幂等）。

    发不发由 `_session_extra_tools` 按压力窗口决定，注册≠每次发。
    """
    from core.compaction_tool import REQUEST_COMPACTION_SCHEMA, request_compaction
    register_tool("request_compaction", request_compaction, REQUEST_COMPACTION_SCHEMA)


def register_history_tool() -> None:
    """注册 `history_search`。由 `init_bus` 调用。

    框架级工具，handler 是真的（真身在 `core.history_tools`），这一句只是接线。
    没有早退 —— 同 `register_file_tools`。
    """
    from core.history_tools import HISTORY_SEARCH_SCHEMA, history_search

    register_tool("history_search", history_search, HISTORY_SEARCH_SCHEMA)


# ------------------------------------------------------------------
# Permission queries (ASI02)
# ------------------------------------------------------------------

def get_tool_permission(name: str) -> str:
    """Return the permission tier for *name* (read / write / admin)."""
    return _registry["tool_perms"].get(name, "admin")


# 这里曾有一个 `get_tool_permissions()`（复数，返回全量 `{名: 档}`），
# 因全仓零调用者而删除。
# 判据：「检测有了、但没人调」是一种沉默的失效 ——
# 两选一，不许挂着：接上，或者删掉。单数版 `get_tool_permission`
# 覆盖了所有真实用法（逐工具问），复数版只是"顺手也能给"，于是没人用。


def get_tool_destructive(name: str) -> bool:
    """这个工具是不是不可逆（第二根轴）。

    查不到回 `False` —— 与 `get_tool_permission` 查不到回 `admin`
    方向相反，而这不是笔误：两根轴的兜底方向本来就相反
    （理由见 `core/tool_permission.py` 的「第二根轴」一节）。
    """
    return bool(_registry["tool_destructive"].get(name, False))


def get_tool_concurrency_safe(name: str) -> bool:
    """这个工具能不能和同批别的调用同时跑（第三根轴）。

    兜底是由权限档派生的（`permission == "read"`）——
    那正是并发预取的原判据，调用方换到本函数 = 零行为变化，
    而"放开某个非 `read` 工具"从此是一条声明，不再改判定逻辑。

    与 `get_tool_destructive` 的兜底方向相反：那根是 `False`（fail-open），
    这根在"拿不准"时落的是派生值（`read`，`True`、其余，`False`），
    而显式声明时才认。判据见 `core/tool_permission.py` 的「第三根轴」一节。
    """
    return _TP.resolve_concurrency_safe(
        name,
        permission=get_tool_permission(name),
        explicit=_EXPLICIT_CONCURRENCY,
    )


def undeclared_destructive_report() -> str:
    """启动汇总行 —— 还有多少个工具「没人给过 `destructive` 声明」。

    这是第二根轴的验收行：未声明的个数只许降（最终为空集）。
    与 `undeclared_report`（第一根轴）故意不同：那边逐条 print 猜成 `read` 的，
    这里一条都不逐条。理由：第一根轴只有 `read` 那些是"危险的一侧"，
    值得逐条看；第二根轴未声明本身就是"危险的一侧"，逐条 = 上百行
    （日志洪水会淹掉真信号）。
承重的是这个数 + 棘轮（`tests/test_destructive.py`）+ AST 反查
    （真正逐条点名的是它，而且它是构建期的门，不是一行会滚掉的日志）。

    与 `undeclared_report` 一样，调用点在全部装载之后（`bus/core.py`）。
    """
    sources = _registry["tool_destructive_sources"]
    perms = _registry["tool_perms"]
    default = sum(1 for s in sources.values() if s == _TP.SOURCE_DEFAULT)
    declared = sum(1 for s in sources.values() if s == _TP.SOURCE_DECLARED)
    explicit = sum(1 for s in sources.values() if s == _TP.SOURCE_EXPLICIT)
    undeclared_nonread = sum(
        1 for name, s in sources.items()
        if _TP.is_undecided(s) and perms.get(name) != _TP.PERM_READ
    )
    truthy = sum(1 for v in _registry["tool_destructive"].values() if v)
    return (
        f"[destructive] 未声明 {default} 个"
        f"（其中非 read 的 {undeclared_nonread} 个 —— 那一侧是 fail-open）"
        f" · 已声明 {declared} + 代码表 {explicit}"
        f" · 判为不可逆 {truthy} 个"
    )


# ------------------------------------------------------------------


def register_provider(name: str, client: Any) -> None:
    """Register a provider client instance."""
    _registry["providers"][name] = client


def get_provider(name: str) -> Any:
    """Return a provider client instance by name. Raises KeyError if not found."""
    if name not in _registry["providers"]:
        raise KeyError(f"Provider '{name}' not registered. Available: {list(_registry['providers'].keys())}")
    return _registry["providers"][name]


def register_channel(name: str, handler) -> None:
    """Register a Channel instance."""
    _registry["channels"][name] = handler


def get_channel(name: str):
    """Return a Channel instance by name. Raises KeyError if not found."""
    if name not in _registry["channels"]:
        raise KeyError(f"Channel '{name}' not registered. Available: {list(_registry['channels'].keys())}")
    return _registry["channels"][name]


# ------------------------------------------------------------------
# Provider metadata
# ------------------------------------------------------------------

def register_provider_metadata(name: str, metadata: dict) -> None:
    """Store provider metadata dict keyed by provider name."""
    _registry.setdefault("provider_metadata", {})[name] = metadata


def get_provider_metadata(name: str) -> dict | None:
    """Return stored provider metadata for *name*, or None."""
    return _registry.get("provider_metadata", {}).get(name)


def list_providers() -> list[str]:
    """Return list of registered provider names."""
    return list(_registry.get("provider_metadata", {}).keys())


def get_provider_metadatas() -> dict[str, dict]:
    """Return all provider metadata (shallow copy)."""
    return dict(_registry.get("provider_metadata", {}))


# Workspace Manager access

def register_workspace_manager(mgr) -> None:
    """Register the WorkspaceManager instance for API / skill access."""
    _registry["workspace_manager"] = mgr


def get_workspace_manager():
    """Return the registered WorkspaceManager, or None."""
    return _registry.get("workspace_manager")


def register_memory_manager(mgr) -> None:
    """注册 MemoryManager 实例（查历史要用它的 `short_term`）。

    与 `register_workspace_manager` 同形。生产两个入口都要接
    （`web/server.py` 与 `main.py` 各构造一份）—— 工具拿不到它时**如实报
    "没接线"**，不假装"没搜到"。
    """
    _registry["memory_manager"] = mgr


def get_memory_manager():
    """Return the registered MemoryManager, or None."""
    return _registry.get("memory_manager")


def reset() -> None:
    """Clear all registries. Useful for testing.

    必须清 `tool_perms` —— 若漏掉它，`reset()` 之后
    `get_tool_permission(name)` 还会回上一批的档位，
    而 `_registry["tools"]` 是空的 —— 一个看着像结论的旧值。
    `tool_perm_sources` 与它同生同死，一起清：只清一个，
    "这个档位是怎么来的"就永远说不清。
    第二根轴的两格（`tool_destructive` / `tool_destructive_sources`）
    同样两格一起清 —— 同一条理由。
    """
    _registry["tools"].clear()
    _registry["tool_schemas"].clear()
    _registry["tool_perms"].clear()
    _registry["tool_perm_sources"].clear()
    _registry["tool_destructive"].clear()
    _registry["tool_destructive_sources"].clear()
    _registry["tool_owners"].clear()
    _registry["providers"].clear()
    _registry["channels"].clear()
    _registry["provider_metadata"] = {}
    _registry.pop("workspace_manager", None)
