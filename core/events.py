"""Progress event stream — pipeline 活动的结构化事件总线。

背景
----
Web UI / TUI / IM 通道都需要知道 "agent 正在做什么"。若唯一的事实通道是
`print()` → `_LogCapture` → `_LOG_BUFFER` → `/api/logs` 轮询，
它没有会话维度、没有结构化字段，而且切片赋值不是原子的。

本模块提供一条 channel-agnostic 的事件流：pipeline 在天然节点上 `emit()`，
任意消费者 `subscribe()`。当前主要由 Web UI 消费；TUI / IM 通道可复用
同一套订阅接口。

四条设计规矩
------------
1. 不维护"进度状态"，只维护事件 + 纯折叠函数。ui 侧的"当前在做什么"
   应该是 fold(state, event) 的产物，不是一个可变的 status 字段。
2. 时间戳一次写死，多粒度都用它推。不为"实时显示"新增事件。
3. 存活性只能来自当前进程的观察。不得从持久日志重放存活状态 ——
   否则进程死掉后重放会留下一个永远在转的幽灵指示器。
4. 瞬时数据走独立通道。高频的流式 delta 不落盘。

线程模型
--------
::

    pipeline 侧（任意线程 / 任意 event loop）   web 侧（waitress worker 线程）
    ──────────────────────────────────────     ────────────────────────────
    emit()                                     subscribe() -> queue.Queue
      ├─ Lock 内: seq++, replay 环                └─ 阻塞 get(timeout=N)
      ├─ 每个订阅者 put_nowait()                        └─ yield SSE 帧
      └─ 落盘队列 put_nowait()  ← 非阻塞

必须用 `queue.Queue`，不能用 `asyncio.Queue`。原因：飞书/钉钉通道从
*非 async 线程* 调用 `run_coroutine_threadsafe(scheduler.execute(...))`，
而 `/api/chat` 每个请求都新建一个 event loop 且从不关闭。任何"记住某个
loop 然后 call_soon_threadsafe"的方案都会踩空。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

# ------------------------------------------------------------------
# 事件类型
# ------------------------------------------------------------------

# 回合 / 阶段生命周期
EVT_TASK_START = "task.start"     # 收到一条用户消息，一轮开始
EVT_TASK_END = "task.end"         # 一轮结束（含最终回复）
EVT_STEP_START = "step.start"     # 计划执行的某一步开始
EVT_STEP_END = "step.end"         # 某一步结束
EVT_TURN_START = "turn.start"     # agent_loop 内的一次 LLM 往返开始
EVT_TURN_END = "turn.end"         # 一次 LLM 往返 + 其工具调用结束

# 活动
EVT_THINKING = "thinking"         # LLM 调用前/后（phase=start|end）
#: 模型在带工具调用的那一轮里说的话。
#:
#: 为什么需要它：模型本来就在中途说话 —— 实际真实会话的 75 条 assistant 消息里，
#: 29 条同时带工具调用与可见文本（"I need to create the file first."、
#: "当前工具集没有直接写 .txt 的接口…"）。这些文本一直被写进 `messages`
#: （模型看得到），但没有任何事件把它带给用户 —— 用户只看到"正在读取 xxx"。
#:
#: 后果不只是"话少"：双方没有共享的意图。agent 有一个私有的任务状态（它说过的话），
#: 用户只有一个私有的任务状态（屏幕上的步骤行），中间没有可对齐的东西，
#: 用户中途插话时没有共同参照，于是"撤回 / 纠正 / 补充"三种意图无法区分。
EVT_SAY = "say"
EVT_TOOL_START = "tool.start"     # 工具即将执行
EVT_TOOL_END = "tool.end"         # 工具执行完毕
EVT_DELTA = "delta"               # 流式文本增量（本阶段不发射，预留）

# 审批
#
# 这两个事件只是"失效信号"，不是渲染源。
#
# 浮窗从 state 渲染（`GET /api/approval` → `scheduler.pending_approval()`），
# 事件只负责说"去重取一次"。这是 `core/events.py` 规矩 3 的直接应用：
# 存活性只能来自当前进程的观察，不得从持久日志重放 —— 如果浮窗从事件流
# 重建，进程重启后日志里那两条 `approval.request` 会复活成一个幽灵浮窗，
# 而对应的 pending 早就不在了。
#
# 于是这两个事件刻意不带选项表、不带文件列表 —— 只带身份。
# 带内容就等于开了第二条渲染源，两条迟早不一致。
EVT_APPROVAL_REQUEST = "approval.request"    # 有新的待审批：{request_id,kind,ask_index}
EVT_APPROVAL_RESOLVED = "approval.resolved"  # 已结算/已失效：{request_id,choice}

# 异常
EVT_RETRY = "retry"               # 通用重试（仍未用；LLM 连接重试走下面那一对）
EVT_ERROR = "error"               # 阶段抛异常
EVT_RESYNC = "resync"             # 客户端 seq 缺口过大，必须重建

# 模型连接重试（界面上的红色横幅）。
#
# 这一对不进 `DURABLE_TYPES`：它们是"进行中"的噪声（每次失败一条，
# 最多 10 条），不是离散的历史事实 —— 与 `delta` 同一条判据。
# 界面靠它们立刻翻红 / 变绿；`/api/status.llm_retry` 是重连后的兜底读数
# （状态在 `core/llm_retry.py`，事件只是"叫你读一次"）。
EVT_LLM_RETRY = "llm.retry"       # {retry, max, delay_ms, kind, text}
EVT_LLM_RETRY_OK = "llm.retry_ok"  # 连接恢复：横幅变绿、取消常驻

# 压缩进行中 / 结果—— 界面据此把横幅变黄、并挡住发送框。
#
# 两类触发源共用这一条：① 用户手动 `/compact`；② 收到「请求超长」后的自救
# （`AgentLoopStage` 强制压一次再重发）。
# 必须带收尾事件（`state` 最终落到 `ok` / `fail`）—— 与 `sealHead()` 那条
#   判据同族：一条"开始"而没有任何后续事件的流，界面会永远停在"进行中"
#   （转轮一直转、输入框一直灰），而用户不知道发生了什么。
# 不进 `DURABLE_TYPES`（与 `llm.retry` 同族）：它是进行中的噪声，
#   不是离散的历史事实。
EVT_COMPACT_PROGRESS = "compact.progress"   # {state: "start"|"ok"|"fail", reason?: str}

# 上下文状态
#
# 一条每轮一条的读数：当前占用 / 压缩阈值 / 窗口来源 / 是否已进预警区 /
# （仅当本轮真的压过时）压缩前后的 token。
#
# 它进 `DURABLE_TYPES` —— 判据与 `turn.end` 同：一轮一条的离散事实，
# 不是进行中的噪声（对比 `delta`：每 token 一条）。而它比 `turn.end` 更该
# 落盘的一点：它是「先观测、再定阈值」那类工作唯一的原始素材 ——
# 落盘之后"压缩多久发生一次"才是一个能数出来的数。
#
# 压缩那一段只在"本轮真的压过"时才带。`compressor.last_compression`
# 是粘的（一直留到下一次压缩为止），每轮都塞进载荷，读日志的人会看到
# "压了 100 次"，而真实可能是 1 次 —— 粘住的旧值不许伪装成新事实。
EVT_CONTEXT_STATE = "context.state"

# 记忆保存
#
# 后台抽取器写进 N 条长期记忆时发一条（落成事件流一条，
# 进 UI、不进上下文）。
#
# 进 `DURABLE_TYPES`：它是"后台抽取真的发生过、写了几条"唯一可数的记录
#   （判据同 `context.state` —— 先观测、才谈得上调那个节流旋钮）。
# 它不是每轮都有的：只有真写了才发（没有新记忆时一声不响）。
EVT_MEMORY_SAVED = "memory.saved"

# 记忆巩固完成
#
# 巩固器（`core/memory_organize.py`）跑完一次时发一条 —— **与 `memory.saved`
# 分开：那一条说的是"抽取往库里加了几条"，这一条说的是"整理改动**了几条
# （合并/改写/精简）"。两者的触发频率差两个数量级（每 3 个合格轮 vs ≥24h 且
# ≥5 会话），混成一个类型之后，「整理到底跑过没有」就没法从日志里数出来了。
#
# 进 `DURABLE_TYPES`：同 `memory.saved` 的判据 —— 一个罕见的、花了 LLM 钱的
#   后台动作，落盘之后"它多久真跑一次、改动多少"才是可数的。
EVT_MEMORY_ORGANIZED = "memory.organized"

# 后台活的时间跨度
#
# 为什么要有它：抽取 fork / 整理都是后台线程，与用户轮并发跑。
#   只有"它写了几条"（`memory.saved`）时，**看不到它什么时候在跑、
#   跑了多久。于是「后台活跟哪一轮撞过」这个问题没有任何读数能回答**
#   （后台线程里的模块级状态会串到别的会话的并发轮上）。
#
# 一对，不是一条：`bg.start` 与 `bg.end` 成对，落盘之后
#   「有 start 无 end」就是可检测的孤儿（同 `task.start` 的判据）——
#   而更重要的用法是算区间重叠：与 `task.start`/`task.end` 一起读，
#   就能回答"这一轮和那次后台抽取重叠了多久"。
#
# 刻意不进前端（v0）：`EVT_TYPES` 里不注册它，浏览器收不到
#   （命名事件只派发给注册了该类型名的监听器）。两个理由：
#   ① 它的用途是观测与事后计算，不是给用户看进度；
#   ② 它在 `task.end` 之后到达（后台活自成一段），若在前端开活动块，
#      就会留下一块永远没人收口的活动块。要用 UI 呈现，得先解决"谁关它"。
EVT_BG_START = "bg.start"
EVT_BG_END = "bg.end"

# 本轮在等并发名额
#
# 为什么要有它：并发策略从"一次只跑一轮"换成了「每会话一把 +
#   进程级上限 3」，到顶时 `scheduler._acquire_turn_slot` 阻塞排队。
#   若那时只 `print("[sched] … 排队中")`，用户那边一片空白，
#   界面看起来就是"发了消息没反应"。
#判据：不许静默排队。
#
# 位置：它到达在 `task.start` 之前（名额没拿到，那一轮还没开始）
#前端按 `receiptBlock` 画（开一块 + 当场收口）。
#   不能开活动块：`task.start` 随后会开自己那块，两块会打架；
#   而收口人也不存在（会留下一块永远没人收口的活动块）。
#
# 不进 `DURABLE_TYPES`：它是瞬时的（拿到名额就过去了），
#   落盘之后重放只会得到一条过期的"在排队"。
EVT_TURN_QUEUED = "turn.queued"

#: 跑着的时候用户补的那句话，已经被模型收下了：
#: `{items: [client_msg_id, …]}`。
#:
#: 前端据此做三件事（缺一件就是"看着像没生效"）：
#:   ① 把那条气泡的「排队中」摘掉；
#:   ② 上一个活动块标 done 并收起（块是分片的，不是一块长到底）；
#:   ③ 在这条 user 消息之后新开一块 —— 判据是  修好的那个落点。
#:
#: 不进 `DURABLE_TYPES`：它是"这一步收下了哪几句"的瞬时回执，
#:   落盘重放只会得到一条过期提示（同 `turn.queued` 的判据）。
#: 同样不碰 `busy` —— 绿横幅的计时只认 `busy` 的假→真（
#:   「绿横幅计算不断」）。
EVT_STEER_DELIVERED = "steer.delivered"

#: 排队那句提示的文案，唯一一处。
#:
#: 为什么它住在这里而不是各前端各写一份：它是一句用户可见的事实，而
#: 两个前端（`web/templates/chat.html` 与 `packages/ui` 的 React 壳）
#: 都要说同一句话 —— 各写一份就是"同一个量两处各算各的"，
#: 而漂移的后果是"同一个事实两种说法"（用户无从判断哪个是真的）。
#: 它随事件载荷发出去（`scheduler._acquire_turn_slot`），前端渲染 `d.message`。
TURN_QUEUED_MESSAGE = "已达并发上限，此消息将在前序任务结束后开始"

# 本轮改动过的文件
#
# 数据从哪来：`SecurityGuard.validate_path` 的写类 op（一处钩子，
#   不是一张"写工具"名单）—— 理由与边界写在 `core/touched_files.py` 的 docstring 里。
# 一处如实标的边界：记的是"验证过写权限"，不是"写成功了"
#   （`validate_path` 跑在落盘之前）。措辞是「本轮改过」，
#   不是「本轮写成功」（回执不许比事实更自信）。
#
# 进 `DURABLE_TYPES`：它是一轮一条的离散事实（同 `context.state`）。
#
# 它进 durable 的理由是"事后可查/可数"（`data/events/*.jsonl`），
#   不是"刷新后界面还在"。这个块是活动块，而
#   `web/templates/chat.html::saveChatHistory` 明确**只持久化
#   `kind === 'msg'` 的节点**（瞬时节点描述的是"正在发生"，刷新后重放一个
#   已经结束的转轮是撒谎），刷新之后它确实没了。
#
# 同族的 `context.state` 之所以刷新后还在，是因为它另有一条重新拉取的路
#   （`refreshContextOnLoad()` → `/api/context`）—— 不是靠重放事件。
#判据：durable ≠ 界面上会回来；要让某个块活过刷新，得给它一条
#   "重新拉"或"随历史持久化"的路（交付节点的 `o.files` 就是后者）。
EVT_FILES_CHANGED = "files.changed"

# 旧工具结果被清 / 被压 —— "上下文自己少了一点"的唯一可数记录
#
# 两种触发源共用这一条，靠 `kind` 区分：
#   · `kind="idle"`     —— `_prune_tool_results`：空闲 ≥60 分钟，原地改 messages
#                          （连落库的历史一起改；落过盘的换成指针，其余换成占位）
#   · `kind="pressure"` —— `trim_for_pressure`：占用 ≥80% 窗口，只改发出去的副本
#
# 进 `DURABLE_TYPES`：它是"上下文为什么变小"唯一能事后对账的东西
#   （判据同 `context.state` —— 先观测、才谈得上解释一次掉量）。
# 不进前端 `EVT_TYPES`：它不是给用户看的过程，是给排查用的记账
#   （与 `ice.canary` 那条"刻意不进前端"同一个形状）。
EVT_TOOL_RESULTS_PRUNED = "tool_results.pruned"

# ICE 金丝雀触发
#
# 出站那一刻发现诱饵值（`core/ice/canary.py`），不给发 + 这一条报警。
# 它是安全事件，不是进度读数：用户必须看得见 ——
#   只打控制台的时候，WebUI 看不到。
# 进 `DURABLE_TYPES`：一次触发是一个离散的安全事实，事后翻日志要数得出来
#   （"这周金丝雀响过几次"是这份记录唯一能回答的问题）。
EVT_ICE_CANARY = "ice.canary"

# ICE 网络侧的弱信号
#
# 出站那一刻按本地启发式看这个 URL「有没有正常站点上不该出现的形态」
# （明文 http / 脚本混用 / 高熵域名），只报、绝不拦
# （`core/ice/net_heuristics.py` 的 docstring 写了为什么只报：
# "误报会消耗信任"，而拦住一个正经站点用户会去关掉整个网络层）。
#
# 刻意不进 `DURABLE_TYPES`（这是它与 `ice.canary` 唯一的分歧）：
#   金丝雀是离散的安全事实（一次阻断），而弱信号是瞬时的提示 ——
#   重连时把过去每一次"那个 URL 看着有点怪"再刷一遍，正好把
#   "误报消耗信任"从误报变成噪声。落盘那份仍然有（控制台/事件日志）。
EVT_ICE_WEAK = "ice.weak"

# ICE 会话级降级
#
# 用户在放行那个弹窗上选了「同意，并且本次对话内不再做这项保护」，本会话内
# 信息层关掉。它是授权不是数据：`/clear` 对它的处置是
# 回收权限（`core/ice/reveal.py::recover`），不是删数据。
#
# 进 `DURABLE_TYPES`：界面必须一直看得见"保护已降级"，
#    否则用户以为还护着。刷新之后那一行也得在。
EVT_ICE_DOWNGRADED = "ice.downgraded"

# 被中和的待审批调用
#
# 一轮里两条调用都撞审批时，只能挂一条（同一条 assistant 消息里的
# 所有 `tool_call` 都必须拿到结果 —— 少一条上游 400），另一条当场被换成一句实话
# （`NEUTRALIZED_PENDING_NOTE`）。
#
# 但那件事只有模型看得见：用户侧本来没有任何信号 —— 界面上只有一张浮窗，
# 而"另一件被取消了"全靠模型的嘴在说（它完全可以说漏）。判据：
# 用户侧的事实不许凭空消失；这一条由框架发，不是模型的转述。
#
# 进 `DURABLE_TYPES`：一次中和是一个离散事实 —— 落盘之后
# "审批撞车的频率"才是可数的（同 `ice.canary` / `files.changed`）。
EVT_TOOL_NEUTRALIZED = "tool.neutralized"

#: 只有这些类型落盘。
#:
#: 判据是"每个离散动作一条"，而不是"是不是进行中"：
#:   - 落盘：task / step / tool 的边界（一次工具调用 = 2 条，与调用次数同阶）
#:   - 不落盘：`delta`（每 token 一条）、`thinking`（每轮 2 条，内容是原样的
#:     reasoning 文本，已经在消息历史里了）、`turn.start`（`turn.end` 已带
#:     时长与 token，start 无额外信息）
#:
#: `tool.start` 必须落盘 —— 它携带 `activity`（"正在读取 报告.docx"），
#: 是事后翻日志时唯一能看懂 agent 在干什么的字段。
#: `task.start` 落盘是为了让"有 start 无 end"成为可检测的孤儿。
# 离开一段时间回来时的「我们到哪了」
#
# 判据：做了什么 + 当前状态 + 需要用户做什么 + 可关闭。
#
# 它与 `/recap` 命令不是一件事：那个用户手打、花一次 LLM；
# 这个由框架自己在"离开够了"时给，零 LLM（读会话笔记拼一段话）。
#
# 进 `DURABLE_TYPES`：它是一轮一条的离散事实（同 `context.state`）——
# 而且刷新之后那一块还得在（用户回来看到的第一件东西，刷新就没了很荒唐）。
# 载荷字段是 `recap`（不是 `text`）：`_TEXT_CAPS` 里那个 `text` 是 400，
# 装不下它，别挤进 `text`，那会静默截断。
EVT_RECAP = "recap"

# 周期任务的一次运行。
#   判据同 `context.state` / `memory.saved` —— 一轮一条的离散事实，
#   而且它是"定时任务到底跑没跑"唯一能被数出来的来源。
#   「有变化」是结果，「跑过了」是事实 —— 两件事都得说。
#   载荷：`{job_id, title, trigger, status, next_run?}`（`trigger` = schedule|manual）
EVT_CRON_START = "cron.start"
EVT_CRON_END = "cron.end"

# 一个插件被屏蔽了 —— 用户面那条弹窗的信号。
#
# 载荷：`{owner, name, code, reason, path, since}`（`owner` = `skill:<名>` / `mcp:<名>`）
#
# 它是"叫你来读"的信号，不是状态本身 —— 状态在账本里
#   （`data/runtime/quarantine.json`，`GET /api/quarantine` 现读现给）。
#   判据：「信号必须在"它让你去读的那份 state"可读之后才发」。
#所以发事件之前账本已经落盘了；前端拿到事件之后再拉一次，
#   两边算出来的东西一定一致（中间那次网络抖动也不会让它看见一件没有的事）。
#
# 只在"第一次"发（`hits == 1` 那一次）：一个坏包每轮都被体检一次、
#   每轮都弹一遍 —— 那正是"弹窗疲劳"的制造法。重复发现记在账本的 `hits` 上，
#   报数用，不弹窗。
#
# 进 `DURABLE_TYPES`：一次屏蔽是一个离散事实（同 `ice.canary` / `files.changed`），
#   落盘之后"这台机器上屏蔽过几次、都是谁"才是能查出来的。
#   系统级（不是某个会话）—— 所以 `session_key` 留空。
EVT_PLUGIN_BLOCKED = "plugin.blocked"

DURABLE_TYPES: frozenset[str] = frozenset({
    EVT_TASK_START,
    EVT_TASK_END,
    EVT_STEP_START,
    EVT_STEP_END,
    EVT_TURN_END,
    EVT_SAY,
    EVT_TOOL_START,
    EVT_TOOL_END,
    EVT_RETRY,
    EVT_ERROR,
    # 审批：进 DURABLE_TYPES 是因为它们成对出现
    # （request/resolved）。落盘之后"有 request 无 resolved"是可检测的孤儿 ——
    # 而浮窗的渲染源仍然是 state，不是这两条。
    EVT_APPROVAL_REQUEST,
    EVT_APPROVAL_RESOLVED,
    # 上下文状态：一轮一条的离散读数，与 `turn.end` 同一量级。
    # 它是"压缩多久发生一次"唯一能被数出来的来源（见上面那段）。
    EVT_CONTEXT_STATE,
    # 记忆保存：抽取写了几条的唯一可数记录（见上面那段）。
    EVT_MEMORY_SAVED,
    # 记忆巩固完成：整理改了几条的唯一可数记录（见上面那段）。
    EVT_MEMORY_ORGANIZED,
    # 本轮改过的文件：一轮一条的离散事实 —— 用于事后可查/可数，
    # 不是"刷新后界面还在"（见上面那段）。
    EVT_FILES_CHANGED,
    # 旧工具结果被清 / 被压：上下文"为什么变小"的唯一可数记录（见上面那段）。
    EVT_TOOL_RESULTS_PRUNED,
    # 后台活的时间跨度：一对，落盘，可算区间重叠、可查孤儿。
    EVT_BG_START,
    EVT_BG_END,
    # ICE 金丝雀触发：离散的安全事实 —— 见上面那段。
    EVT_ICE_CANARY,
    # 被中和的待审批调用：一次中和是一个离散事实 —— 见上面那段。
    EVT_TOOL_NEUTRALIZED,
    # 回来时的「到哪了」：一轮一条的离散事实，且刷新之后得还在。
    EVT_RECAP,
    # 会话级降级：界面必须一直看得见"保护已降级"。
    EVT_ICE_DOWNGRADED,
    # 周期任务的一次运行：一轮一条的离散事实 —— 见上面那段。
    EVT_CRON_START,
    # 插件被屏蔽：一个离散事实，而且是系统级的那一种。
    EVT_PLUGIN_BLOCKED,
    EVT_CRON_END,
})

#: 事件 data 里的长文本字段上限。SSE / JSONL 都不该被一个超长 payload 撑爆。
_TEXT_CAPS: dict[str, int] = {
    "message": 500,
    "error": 200,
    "text": 400,
    "response": 4000,
    # 回顾单独一个字段，不许挤进 `text` ——
    #   那个 400 会把一段正常的回顾静默腰斩。上限与
    #   `session_recap.MAX_CHARS`（600）成对，有断言钉着这层关系。
    "recap": 800,
    # `plugin.blocked` 那两条 —— 理由同 `recap`：
    #   体检说明动辄上百字，不许复用 `text`（=400）（那会把话腰斩成半句，
    #   而用户看到的正是这半句）。`path` 是文件路径，300 够了（Windows 上限 260 的一倍多）。
    "reason": 600,
    "path": 300,
    # 圆桌「说完就发」—— `group.member.done` 带着成员的
    #   正文（前端立刻落一条气泡）。这里必须登记一个上限：`_cap_data` 对
    #   未登记的键原样透传，不登记 = 一条长回复整段进 SSE。
    #   截断版只是直播预览：整轮结束时的 `group_replies` 是权威那份，
    #     前端会整批替换（`pushGroupReplies`）。与 `response` 同量级（4000）。
    "content": 4000,
}


@dataclass(slots=True)
class Event:
    """一条进度事件。`seq` 由总线在 emit 时填入，会话内单调递增。"""

    type: str
    session_key: str = ""
    seq: int = 0
    ts: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)

    def to_sse(self) -> str:
        """渲染成 SSE 帧。`default=str` 兜底：一个坏 payload 绝不打断整条流。"""
        payload = json.dumps(
            {
                "type": self.type,
                "seq": self.seq,
                "ts": self.ts,
                "session": self.session_key,
                "data": self.data,
            },
            ensure_ascii=False,
            default=str,
        )
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n"

    def to_jsonl(self) -> str:
        """渲染成一行 JSONL。信封字段在前，data 嵌套在 `data` 键下。

        刻意不展开 `**self.data` —— 否则 data 里的 `type` / `ts`
        会覆盖信封字段。
        """
        return json.dumps(
            {
                "seq": self.seq,
                "ts": self.ts,
                "type": self.type,
                "session": self.session_key,
                "data": self.data,
            },
            ensure_ascii=False,
            default=str,
        )


def _cap_data(data: dict) -> dict:
    """截断已知的长文本字段，其余原样保留。"""
    out: dict[str, Any] = {}
    for k, v in data.items():
        cap = _TEXT_CAPS.get(k)
        if cap is not None and isinstance(v, str) and len(v) > cap:
            out[k] = v[:cap] + "…"
        else:
            out[k] = v
    return out


# ------------------------------------------------------------------
# 订阅者
# ------------------------------------------------------------------


class Subscriber:
    """一个有界的事件投递队列。一个 SSE 连接对应一个订阅者。"""

    __slots__ = ("sid", "session_key", "q", "dropped", "types")

    def __init__(self, sid: int, session_key: str, maxsize: int,
                 types: "frozenset[str] | None" = None) -> None:
        self.sid = sid
        #: 空字符串 = 订阅全部会话
        self.session_key = session_key
        #: 只收这几类；`None` = 全收（既有行为）。
        #: 它不只是省内存 —— 这个队列是有界的，满了会丢最旧，
        #: 而一轮 agent 会长出成百上千个 `delta` 分片，全收的订阅者
        #: **会把 `tool.*` 挤掉**，于是"这一次跑了哪些工具"就数不全了。
        self.types = types
        self.q: "queue.Queue[Event]" = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, ev: Event) -> None:
        """非阻塞投递；队列满时丢最旧的一条。

        丢最旧而不是丢最新：对"实时活动显示"而言，最新状态严格优于陈旧
        状态。否则消费端会卡在 30 秒前的步骤上，而转轮还在转。
        """
        while True:
            try:
                self.q.put_nowait(ev)
                return
            except queue.Full:
                try:
                    self.q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    # 被真正的消费者线程抢走了，槽位已释放，重试即可。
                    pass

    def __repr__(self) -> str:
        return (f"Subscriber(sid={self.sid}, "
                f"session={self.session_key or '*'!r}, "
                f"qsize={self.q.qsize()}, dropped={self.dropped})")


# ------------------------------------------------------------------
# 落盘线程
# ------------------------------------------------------------------


class _DurableWriter(threading.Thread):
    """把 durable 事件批量写成 JSONL。

    存在的唯一理由：把 `json.dumps` 与磁盘 IO 挪出 pipeline 主路径。
    `emit()` 在这个队列上的成本 = 一次 Lock + 一次 append（微秒级）。

    队列是无界的 —— 生产者绝不能因为写盘慢而阻塞。durable 类型的事件
    密度实际是每轮几次到几十次，1 秒 flush 周期下队列几乎总是空的。
    """

    #: 队列长度硬上限（防磁盘满/权限错时无界增长）。超出则静默丢弃并计数。
    MAX_QUEUED = 10_000

    def __init__(self, out_dir: Path, *, flush_interval: float = 1.0,
                 flush_batch: int = 64, retain_days: int = 14) -> None:
        super().__init__(daemon=True, name="lv-event-writer")
        self._q: "queue.Queue[Event | None]" = queue.Queue()
        self._dir = out_dir
        self._interval = flush_interval
        self._batch = flush_batch
        self._retain_days = retain_days
        self._buf: list[str] = []
        self._fp = None
        self._day = ""
        self.dropped = 0
        self._stop = threading.Event()

    def offer(self, ev: Event) -> None:
        if self._q.qsize() >= self.MAX_QUEUED:
            self.dropped += 1
            return
        self._q.put_nowait(ev)

    def stop(self) -> None:
        self._stop.set()
        self._q.put_nowait(None)

    # -- thread body ------------------------------------------------

    def run(self) -> None:
        self._prune_old()
        # 注意：不要用 while not self._stop.is_set() 做循环条件。
        # 那样 stop() 一置位就会立刻跳出，把队列里还没落盘的事件全丢掉。
        # 正确做法是靠哨兵值退出 —— Queue 是 FIFO 的，所以哨兵之前入队的
        # 事件必定已经处理完。
        while True:
            try:
                ev = self._q.get(timeout=self._interval)
            except queue.Empty:
                self._flush()
                continue
            if ev is None:
                break
            self._buf.append(ev.to_jsonl())      # 编码在写线程里做
            # task.end 是一轮的收尾标记 —— 立刻落盘，方便事后立刻查看。
            if len(self._buf) >= self._batch or ev.type == EVT_TASK_END:
                self._flush()

        # 尽力清空哨兵之后可能溜进来的事件（stop 与并发 emit 竞态）。
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            if ev is not None:
                self._buf.append(ev.to_jsonl())
        self._flush()
        self._close()

    def _file_for_today(self):
        day = time.strftime("%Y-%m-%d")
        if self._fp is None or day != self._day:
            self._close()
            self._dir.mkdir(parents=True, exist_ok=True)
            self._fp = open(self._dir / f"{day}.jsonl", "a", encoding="utf-8")
            self._day = day
        return self._fp

    def _flush(self) -> None:
        if not self._buf:
            return
        try:
            fp = self._file_for_today()
            fp.write("\n".join(self._buf) + "\n")
            fp.flush()
        except Exception as exc:                 # 磁盘满 / 权限错都不该弄挂框架
            print(f"[events] persist failed: {type(exc).__name__}: {exc}")
        finally:
            self._buf.clear()

    def _close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    def _prune_old(self) -> None:
        """启动时清理超过保留期的日志文件。"""
        try:
            if not self._dir.is_dir():
                return
            cutoff = time.time() - self._retain_days * 86400
            for p in self._dir.glob("*.jsonl"):
                if p.stat().st_mtime < cutoff:
                    p.unlink()
        except Exception:
            pass


# ------------------------------------------------------------------
# 总线
# ------------------------------------------------------------------


class EventBus:
    """线程安全的事件总线。单例即可，无需多实例。"""

    def __init__(
        self,
        *,
        queue_size: int = 256,
        replay_size: int = 512,
        persist: bool = True,
        persist_dir: Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._subs: dict[int, Subscriber] = {}
        self._nid = 0
        self._seq = 0
        #: 环形回放缓冲，供 SSE 的 Last-Event-ID 补洞。
        self._replay: deque[Event] = deque(maxlen=replay_size)
        self._queue_size = queue_size
        self._writer: _DurableWriter | None = None
        #: 落盘目录。None = 关闭落盘。写线程懒启动 —— 构造总线不应该
        #: 产生线程副作用（PipelineScheduler.__init__ 会建总线，测试会建
        #: 调度器）。没有 durable 事件时就永远不启动。
        self._persist_dir: Path | None = (
            Path(persist_dir) if persist_dir is not None
            else Path("data/events") if persist
            else None
        )

    # -- 生产端（pipeline 调用）-------------------------------------

    def emit(self, type: str, session_key: str = "", **data: Any) -> None:
        """发布一条事件。绝不阻塞、绝不抛异常。

        事件发不出去（订阅者慢、payload 序列化失败）是消费者的问​题，
        绝不能反过来弄挂 pipeline。
        """
        try:
            ev = Event(
                type=type,
                session_key=session_key,
                data=_cap_data(data),
            )
            with self._lock:
                self._seq += 1
                ev.seq = self._seq
                self._replay.append(ev)
                subs = list(self._subs.values())

            for sub in subs:
                if sub.types is not None and type not in sub.types:
                    continue
                if not sub.session_key or sub.session_key == session_key:
                    sub.offer(ev)

            if type in DURABLE_TYPES:
                writer = self._get_writer()
                if writer is not None:
                    writer.offer(ev)
        except Exception as exc:                 # noqa: BLE001 — 兜底是刻意的
            print(f"[events] emit failed for {type!r}: "
                  f"{type(exc).__name__}: {exc}")

    def _get_writer(self) -> _DurableWriter | None:
        """懒启动落盘线程。没有 durable 事件就永远不启动。"""
        if self._persist_dir is None:
            return None
        if self._writer is None:
            with self._lock:
                if self._writer is None:
                    w = _DurableWriter(self._persist_dir)
                    w.start()
                    self._writer = w
        return self._writer

    # -- 消费端（waitress 线程调用）---------------------------------

    def subscribe(self, session_key: str = "",
                  types: "frozenset[str] | None" = None) -> Subscriber:
        """注册一个订阅者。

        `session_key` 为空表示订阅全部会话；`types` 为空表示收全部类型。
        给了 `types` 就只收那几类 ——
        用途见 `Subscriber.types` 那段（有界队列 + `delta` 会挤掉 `tool.*`）。
        """
        with self._lock:
            self._nid += 1
            sub = Subscriber(self._nid, session_key, self._queue_size,
                             frozenset(types) if types else None)
            self._subs[sub.sid] = sub
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.pop(sub.sid, None)

    def replay_after(self, last_seq: int,
                     session_key: str = "") -> Iterator[Event]:
        """补发 seq > last_seq 的事件（客户端重连用）。

        调用方必须先 subscribe 再调用本方法，否则补发与实时推送之间
        会有一个丢事件的窗口。

        若缺口已超出环形缓冲，则产出一条 `resync` 事件并停止 —— 客户端
        据此提示用户重建，而不是拿到一段有洞的历史。
        """
        with self._lock:
            ring = list(self._replay)
            latest = self._seq

        if ring and last_seq + 1 < ring[0].seq:
            yield Event(
                type=EVT_RESYNC,
                session_key=session_key,
                seq=latest,
                data={"reason": "gap", "from": last_seq,
                      "oldest_available": ring[0].seq},
            )
            return

        for ev in ring:
            if ev.seq > last_seq and (not session_key
                                      or ev.session_key == session_key):
                yield ev

    # -- 运维 -------------------------------------------------------

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def close(self) -> None:
        """停掉落盘线程。主要用于测试与优雅退出。"""
        if self._writer is not None:
            self._writer.stop()
            self._writer.join(timeout=3.0)
            self._writer = None


# ------------------------------------------------------------------
# 单例 + 便利工厂
# ------------------------------------------------------------------

_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """进程级单例。沿用 `bus/registry.py` 的 `get_workspace_manager()` 范式。"""
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = EventBus()
    return _bus


def set_event_bus(bus: EventBus | None) -> None:
    """替换单例。仅供测试（注入替身或关闭落盘）。"""
    global _bus
    _bus = bus


def make_emitter(bus: EventBus | None, session_key: str
                 ) -> Callable[..., None] | None:
    """把 (bus, session_key) 绑成一个 emit(type, **data) 回调。

    这样 `core/agent_loop.py` 只需要接受一个 `_emit` 回调，不必 import
    本模块 —— 它的可单测性（用假 provider 驱动）因此得以保持。

    返回 `None` 表示不发事件（`bus` 为 None），调用方据此跳过。
    """
    if bus is None:
        return None

    def _emit(type_: str, **data: Any) -> None:
        bus.emit(type_, session_key=session_key, **data)

    return _emit


def bg_start(kind: str, session_key: str) -> float:
    """后台活开始 —— 返回 `t0`，交给 :func:`bg_end`。

    刻意的形状：一对裸函数，不是上下文管理器 —— 两处调用点
    （`memory_extract.run_extraction` / `memory_organize.run_organize`）
    各自已经有 `try/finally`，`bg_end` 直接落进那个既有的 `finally`
    即可，零重排。用 `with` 反而要把整段重排一次缩进，
    而那正是"为了观测去改被测代码"——代价不该这么付。

    用法::

        t0 = bg_start("extract", session_id)
        try:
            ... 重活 ...
        finally:
            bg_end("extract", session_id, t0)      # 失败也收口

    永不抛、永不阻断：这是观测，不是功能（同 `memory.saved` 的判据）。
    `kind` 是开放词表（`extract` / `organize` / …）—— 不建枚举：
    加一个新后台活的人不必回来改这里（改这里 = 又一个"要记得改的清单"）。
    `bg.end` 必须在 `finally` 里 —— 否则"有 start 无 end"的孤儿
    分不清是"还在跑"还是"炸了没报"。
    """
    t0 = time.time()
    _emit_bg(EVT_BG_START, kind, session_key, t0)
    return t0


def bg_end(kind: str, session_key: str, t0: float) -> None:
    """后台活结束 —— 与 :func:`bg_start` 成对。要放进 `finally`。"""
    _emit_bg(EVT_BG_END, kind, session_key, time.time(), started=t0)


def _emit_bg(evt_type: str, kind: str, session_key: str, ts: float,
             started: float | None = None) -> None:
    """发一条后台活事件 —— 吞掉所有异常（观测不许弄挂被观测的东西）。"""
    try:
        data: dict[str, Any] = {"kind": kind, "at": ts}
        if started is not None:
            data["duration_ms"] = int((ts - started) * 1000)
        get_event_bus().emit(evt_type, session_key=session_key, **data)
    except Exception as exc:  # noqa: BLE001
        print(f"[bg] {evt_type} 事件发不出去（{type(exc).__name__}: {exc}）")


__all__ = [
    "Event", "EventBus", "Subscriber",
    "get_event_bus", "set_event_bus", "make_emitter",
    "bg_start", "bg_end",
    "EVT_TASK_START", "EVT_TASK_END", "EVT_STEP_START", "EVT_STEP_END",
    "EVT_TURN_START", "EVT_TURN_END", "EVT_THINKING", "EVT_SAY",
    "EVT_TOOL_START", "EVT_TOOL_END", "EVT_DELTA",
    "EVT_RETRY", "EVT_ERROR", "EVT_RESYNC",
    "EVT_LLM_RETRY", "EVT_LLM_RETRY_OK",
    "EVT_APPROVAL_REQUEST", "EVT_APPROVAL_RESOLVED",
    "EVT_CONTEXT_STATE",
    "EVT_MEMORY_SAVED",
    "EVT_MEMORY_ORGANIZED",
    "EVT_STEER_DELIVERED",
    "EVT_TOOL_NEUTRALIZED",
    "EVT_TOOL_RESULTS_PRUNED",
    "EVT_PLUGIN_BLOCKED",
    "EVT_BG_START", "EVT_BG_END",
    "DURABLE_TYPES",
]
