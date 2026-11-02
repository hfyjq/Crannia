"""
MemoryManager — 记忆层编排入口。

管四件事（按职责，不按层号）：
短期（`short_term`，SQLite）· 压缩（`compressor`）· 会话笔记（`session_memory`）
· 日报（`daily_log`）。长期记忆不在这里 —— 它是 `core/memory_store.py`
的自留地（事实文件 + 派生索引 + 记忆三件工具）。记忆层只有这一套真相源。
"""

import json as _std_json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.content_sanitizer import sanitize_text
from core.daily_log import log_turn
# 消息 `content` 的纯文本视图（全仓唯一转换处）。
from core.image_input import content_text
from core.memory.short_term import COMPACTION_BOUNDARY_KIND, ShortTermMemory
from core.memory.compressor import Compressor
from core.memory.session_memory import (
    NOTE_INIT_TOKENS,
    NOTE_UPDATE_TOKENS,
    NOTE_UPDATE_TOOL_CALLS,
    SessionMemory,
    note_gate_met,
)

#: 唯一允许的「token 预算 → 字符额度」折算常数。见 core/summarizer.py
#: 里那段实际依据（中文 ≈1.9 字符/token，保守取 1.5；cl100k 的 0.86 是
#: 高估 2.2 倍，不能用）。
from core.summarizer import CONSERVATIVE_CHARS_PER_TOKEN

#: 窗口来源是"兜底"时行为要降级 —— 见 `get_context` 里那一段。
from core import window_estimate

#: 装载历史时每条注入内容的截断长度。预算必须按这个长度扣，
#: 否则"扣了多少"与"发了多少"不同口径。
_INJECTED_CONTENT_MAX_CHARS = 2000

#: 冷闸滑窗的默认值。默认开 · 默认 90 分钟。
#: 90 是独立取的：那个 60 绑的是它自己的 1h TTL，我们是多 provider，
#: 不做逐 provider 校准。
COLD_WINDOW_DEFAULT_ON = True
COLD_WINDOW_DEFAULT_GAP_MIN = 90
COLD_WINDOW_GAP_MIN_LIMIT = 1
COLD_WINDOW_GAP_MAX_LIMIT = 1440

#: 「记忆」档的装载上限 = 窗口的 45%。
#: 这条上限真正的理由不是"加强短期记忆"，是它避开了一个陷阱：
#: 不设上限时实际装载 = `0.85 × window − 非历史`，
#: window = 1,048,576 下 884,303 = 84.33%，加上非历史 0.67%
#: 正好落在压缩阈值 0.85 上 —— 一装载完就踩在闸门上。
#: 45% 避开它，且相比不设上限是收紧一半。
MEMORY_TIER_WINDOW_RATIO = 0.45

#: 均衡档的装载上限。
BALANCED_TIER_CEILING = 16384
#: 更新档的上限与 reserve（"~4 short turns max"）。
FRESH_TIER_CEILING = 2048


def _tier_params(continuity: str, ctx_window: int, window_source: str):
    """记忆接续档 → `(reserve_ratio, ceiling_tokens)`。纯函数（读者直接测它）。

    单拎出来是为了可测：这两个数决定的是装载预算，是最该被钉住的东西之一；
    埋进调用方的话，只能靠"塞一堆消息再看装进来多少"间接断言。

    两条判据都在这一个函数里：
      · 「记忆」档的上限 = 窗口的 45% —— 不设上限会让这一档
        一装载完就正好踩在压缩阈值（0.85）上（实际 884,303 = 84.33% + 0.67%）；
      · 窗口来源是"兜底"时行为降级 —— 「记忆」档自动降均衡档（宁可少装，不可爆）。
    """
    if continuity == "memory" and window_estimate.is_fallback(window_source):
        continuity = "balanced"
    if continuity == "fresh":
        return 0.35, FRESH_TIER_CEILING
    if continuity == "memory":
        return 0.15, int(ctx_window * MEMORY_TIER_WINDOW_RATIO)
    return 0.35, BALANCED_TIER_CEILING      # balanced（默认）


def _cold_window_settings() -> Tuple[bool, int]:
    """读 `(enabled, gap_minutes)`。坏值逐个回默认（"读不到，默认"）。

    文件名与读法同 `memory_store._memory_write_enabled` / 阶段里的 `_stream_enabled`：
    每轮现读（改完设置当场生效，不用重启）· 读坏不炸（滑窗是兜底网，
    不许因为一行设置把一轮对话弄挂）。
    `isinstance(True, int)` 是 True，gap 的 bool 必须单独拒。
    """
    enabled, gap = COLD_WINDOW_DEFAULT_ON, COLD_WINDOW_DEFAULT_GAP_MIN
    try:
        # 本模块的 json 是 `_std_json`（顶层 `import json as _std_json`）——
        #    这里若误写 `json.loads`，NameError 会被下面的 except 一口吞掉，
        #    设置永远是默认值而"看起来一切正常"（设置页怎么点都不生效）。
        #    这个形状值得留一行：兜底 except 会把拼写错误变成静默降级。
        data = _std_json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
    except Exception:
        return enabled, gap
    v = data.get("context_sliding_window", enabled)
    if isinstance(v, bool):
        enabled = v
    g = data.get("sliding_window_gap_minutes", gap)
    if not isinstance(g, bool) and isinstance(g, int) \
            and COLD_WINDOW_GAP_MIN_LIMIT <= g <= COLD_WINDOW_GAP_MAX_LIMIT:
        gap = g
    return enabled, gap


def count_tool_calls(messages: List[Dict]) -> int:
    """这一批消息里有几次工具调用。

    判据取 `role == "tool"` 的条数：本仓每个工具调用都回填一条
    `role="tool"` 消息，条数就是调用数。
    数的是传进来那一段（这一轮的增量），不是全会话 ——
    全会话的计数早就越过 3 了，那道量闸会恒开。

    单拎成模块级纯函数是为了能直接测（同 `collect_anchor_rows`）：
    这段判据埋在 `record_turn` 里的话，只有"跑一整轮"才碰得到它。
    """
    return sum(1 for m in messages if m.get("role") == "tool")


def collect_anchor_rows(ids, msgs, out) -> None:
    """把「新落的那几行」按角色收成 {"user": <行号>, "assistant": <行号>}。

    前端靠它对上"内存里那条消息"与"库里那一行"。

    后写的覆盖先写的是有意的：一轮里可能落好几条 `assistant`
      （工具轮），而锚点要的是这一轮最后那条。
    只收 `user` / `assistant`：`tool` 行的行号没有下游 ——
      收藏存的是那一轮提问的行号（`stars.message_id`）。
    `users` 是全部 user 行的行号（按落库顺序） —— 一轮里
      可能有两条 user 行（用户跑着的时候补了一句）。只发最后一条的
      `user` 不够：前端要每一条都有身份（收藏 / 行号引用 / "哪一轮"）。
      它是加性的：老前端只读 `user`，行为一个字节不变。
    `ids` 拿不到（`None` 或空），什么都不写、也不抛：
      测试替身里有 `save_messages` 返回 `None` 的
      （`tests/test_daily_log.py:154`），而 `zip(None, …)` 会当场 `TypeError`。
      停在空 dict，调用方（`post_process`）发 `None`，不发 `{}`
      （读不到 ≠ 空）。

    :param ids: `ShortTermMemory.save_messages` 的返回值（与 *msgs* 同序同长）
    :param msgs: 刚落的那些消息 dict
    :param out: 要填的 dict；传 `None` 表示"这次不要"（什么都不做）
    """
    if out is None or not ids:
        return
    out.clear()
    for m, rid in zip(msgs, ids):
        role = str((m or {}).get("role") or "")
        if role in ("user", "assistant"):
            out[role] = int(rid)
            if role == "user":
                out.setdefault("users", []).append(int(rid))


class MemoryManager:
    """
    Orchestrates the four-layer memory system.

    Usage in agent_loop:

        # Before building messages (inject memory context)
        memory_ctx = manager.get_context(session_id, user_message)

        # After final response (persist turn)
        manager.record_turn(
            session_id, user_message, assistant_message,
            token_count=response.total_tokens,
            messages=messages,
        )
    """

    def __init__(
        self,
        config: dict,
        provider,  # BaseProvider for compression + fact-extraction LLM calls
    ) -> None:
        mem_cfg = config.get("memory", {})

        storage_path = mem_cfg.get("storage_path", "data/memory")
        short_term_cfg = mem_cfg.get("short_term", {})
        compression_cfg = mem_cfg.get("compression", {})

        # 三个废弃配置键不再有消费者 —— 出声，不许静默忽略
        # 判据：一个用户改过的键被无声忽略，比明说"它不生效了"更坏。
        # `max_messages` 更是从来没被读过（死键）。
        _dead = [k for k in ("long_term", "semantic") if k in mem_cfg]
        if "max_messages" in short_term_cfg:
            _dead.append("short_term.max_messages")
        if _dead:
            print(f"[memory] config 里 {'、'.join(_dead)} 已不再读取"
                  f"（这些键已没有消费者）")

        # Sliding window: hard cap on in-memory message count.
        # This is a safety net — compression should normally keep the list
        # well under this limit.
        #
        # The threshold (window x 0.85 = 111,411) and the load ceiling
        # below (16,384) are on two different chains — see
        # describe_thresholds() and tests/test_compression_reachability.py.
        self._max_history = short_term_cfg.get("max_history", 100)

        # Initialize sub-modules
        self.short_term = ShortTermMemory(
            db_path=f"{storage_path}/short_term.db",
        )
        self.compressor = Compressor(
            threshold=compression_cfg.get("token_threshold", 0.85),
            keep_recent_turns=compression_cfg.get("keep_recent_turns", 3),
        )
        # 长期记忆的正主是 `core/memory_store.py`（事实文件 + 派生索引 +
        #   `memory_read/search/write` 三件）；后台补抽取是 `core/memory_extract.py`。
        #   未来若给本类加带 fallback 的存储路径，必须按 `storage_path` 推导：
        #   硬编码 fallback 会让测试夹具直接写生产目录。

        session_memory_cfg = mem_cfg.get("session_memory", {})
        # 整理要按"会话笔记文件的 mtime"数
        #   「上次整理之后碰过几个会话」，把这条推导留在一处（同一个值
        #   喂给 `SessionMemory` 与巩固器，不抄第二份）。
        self._sessions_dir = Path(session_memory_cfg.get(
            "sessions_dir", f"{storage_path}/sessions"))
        self.session_memory = SessionMemory(sessions_dir=str(self._sessions_dir))

        # 日报目录：与 `sessions_dir` 同一条推导 —— 带 `storage_path`
        # 的测试自动落自己的临时目录（路径推导源换了，靠它落位的人就全变了）。
        # 日报与会话笔记不合并：笔记随会话消失，日报按日期活过 `/clear`。
        self._logs_dir = Path(f"{storage_path}/logs")

        self._provider = provider
        self._token_total: Dict[str, int] = {}
        self._saved_count: Dict[str, int] = {}
        self._session_note_cache: Dict[str, str] = {}
        # 会话笔记那道量闸的累计器 ——
        #    `{sid: {"tokens": 攒了多少, "tools": 攒了多少次工具调用}}`。
        #    它是一份会话态（按 `session_id` 键、活过一次请求），
        #    必须跟着另外四格一起在 `new_session()` 里清（下面那段 purge）。
        self._note_gate: Dict[str, Dict[str, int]] = {}
        # 上界与锁：只进不出的表在 CLI 不跑（纯 Web 部署）时会无界增长；
        # 并发下追加/冲刷也要互斥。
        self._bg_status: List[str] = []  # pending status from background threads
        self._bg_status_lock = threading.Lock()
        self._BG_STATUS_MAX = 200

        # Track which sessions need memory injected into the system
        # prompt.  We only inject on the first turn after restore or
        # compression — injecting every turn causes the LLM to treat
        # it as "recovered memory" and greet the user like a reunion.
        self._pending_memory_inject: Dict[str, bool] = {}

        # Measured non-history cost
        # No token-count cache here.  The arithmetic belongs to the provider
        # (`estimate_request_tokens`); a cached count would be a second
        # instrument and can give a different answer for one quantity.  A
        # cached system-prompt measurement would also describe a prompt that
        # may no longer exist — the session note file is rewritten by the
        # note thread only when its volume gate opens.
        # The expensive parts stay
        # memoized where they belong — `prompt_assembler` caches the two
        # subprocess/YAML reads (`_get_git_status`, `_get_agent_mode`).

    # ------------------------------------------------------------------
    # Measured non-history cost
    # ------------------------------------------------------------------

    def _provider_for(self, session_key: str = ""):
        """这一段对话该用的 provider 实例。

        这是「改默认要不要重启」那条的正面答复 ——
          不是"要不要热插拔"，是"一个进程只有一个 provider"这个前提本来就不对。
          三层指向要求每段对话各有各的，取实例这件事必须在会话维度上问，
          而不是读一个进程级的字段。

        于是"改默认"根本不替换任何实例：它只改还没冻过的那几段
          下一次捕获到的东西。跑着的对话各自读各自冻的那条，
          不存在"半新半旧"（同段的轮次本来就串行；跨段本来就该不同）。

        入参两种形状（通道键 / 存储 id）都收 —— 归一那一步在
          `core/session_model._norm` 里（一处定义，别在这儿再写一遍）。
          （本类的方法两半各收一种：`load_session` 收存储 id，
          `get_context_usage` 收通道键 —— 那是既有事实，不是这里的选择。）
        失败一律回 `self._provider`（= 开机时接的默认那条 /
          `config.yaml` 那个），fail-open。
        """
        try:
            from core import model_configs as _mc
            from core import session_model as _sm
            cid = _sm.resolve(session_key) if session_key else ""
            if not cid:
                return self._provider
            return _mc.instance_for(cid, fallback=self._provider)
        except Exception as exc:                                # noqa: BLE001
            # 出声但不换口径：读不到就沿用开机那条（老行为一个字节不变）。
            print(f"[memory] 取会话模型失败（沿用进程默认）："
                  f"{type(exc).__name__}: {exc}")
            return self._provider

    def _non_history_cost(self, session_id: str) -> Tuple[str, list]:
        """(system_text, tool_schemas) —— 请求里除 `messages` 以外的两部分。

        返回对象，不返回 token 数。计数在 provider 里做
        （`BaseProvider.estimate_request_tokens`）：一个仪器、一个因子、
        一处应用。在这里先数一遍再看谁怎么用，会让压缩闸门把
        「已乘因子的 messages」和「没乘因子的 system」加到一起，再漏掉 tools。

        两部分都必须是实际发出的那份，不能是猜的常量：

        * 工具数量若取 `len(list_tools())`，那是全部注册工具（~100），
          而线上只带 _CORE_TOOLS ∪ _discovered（22 个起）：约 4.5 倍高估；
        * 单工具成本若写 `60`，与 `bus/registry.py` 自己的 `150` 矛盾：
          2.5 倍低估。

        两个误差方向相反，净效应无法靠读代码算出。发送实际发出的那份。

        失败时打印异常并返回空的两部分：闸门这一轮只量 `messages`，
        而打印出来的原因就是线索（兜底不能静默失效）。
        """
        try:
            from core.prompt_assembler import assemble_system_prompt, model_name_of
            from bus.registry import get_tool_schemas_active

            # 要和真正发出去的那份逐字一致 —— 闸门量的是它。
            # 漏掉 model_name 会让这里比生产少几个 token（身份行退化成
            # 无模型名那句）—— 两个数对不上也不会报错。
            # 身份行取的也得是这一段那个模型名
            #   （不同配置的模型名不一样，拿进程默认那个量出来的字数就不对了）。
            return assemble_system_prompt(
                "", model_name=model_name_of(self._provider_for(session_id)),
            ), get_tool_schemas_active(
                session_id
            )
        except Exception as e:
            print(f"[memory] non-history cost unavailable "
                  f"({type(e).__name__}: {e}) — the compression gate will "
                  f"measure `messages` alone this turn")
            return "", []

    def measure_system(self, memory_context: str, session_id: str = "") -> int:
        """`assemble_system_prompt(memory_context)` 的 token 数 —— 走同一个仪器。

        存在的理由：`_non_history_cost` 量的是
        `assemble_system_prompt("")` —— 不带记忆注入，而 `agent_loop`
        真正发出去的是带记忆的那份，记忆非空时两者不等，
        而前者被压缩闸门当成了"当前开销"。

        这里不替它下结论（面板是读数，不是优化器）：这个方法的唯一用途是让
        `core/context_report.py` 把差额摆出来。要不要据此改闸门，是另一件事
        —— 那会改变压缩判定。

        传的是文本，不是 token 数（调用方交对象，不交数字）。
        """
        prov = self._provider_for(session_id)
        if prov is None:
            return 0
        from core.prompt_assembler import assemble_system_prompt, model_name_of

        text = assemble_system_prompt(
            memory_context or "", model_name=model_name_of(prov),
        )
        return int(prov.estimate_request_tokens([], system=text)["system"])

    def context_breakdown(
        self, session_id: str, messages: List[Dict], memory_context: str = ""
    ) -> dict:
        """会话构成面板的取数 —— 薄委托到 `core/context_report.py`。

        放在类里而不是让那个模块直接拿 `self`：取数要用到 `_provider`
        这类私有面，而"谁能碰私有"的边界值得有一条线。
        """
        from core.context_report import breakdown

        return breakdown(self, session_id, messages, memory_context)

    def describe_thresholds(self) -> Dict[str, Dict[str, object]]:
        """每个触发阈值的 当前/阈值/还差 —— 启动自检用。

        目的：让"阈值算出来了、但它到底会不会触发"这个问题看得见。
        触发式机制若从不验证触发条件，它可能永远不触发而没人发现。

        它刻意把"装载上限"和"压缩阈值"分开列：两者不在同一条链上
        （装载上限只约束恢复，压缩跑在活消息表上），当成一条链比较会得出
        跨量纲的「差 6.8 倍」。

        本类里只有这一处（以及它的邻居 `report_thresholds`）读
        进程级的 `self._provider` —— 它是开机自检，手上没有任何一段
        对话可问（`initialize()` 调它）。这不是漏改：这条读数说的是
        "这个进程默认那条的窗口是多少"。
        每一段自己的窗口在 `get_context_usage` 那条路上（顶栏读数）。
        """
        window = self._provider.context_window if self._provider else 0
        # 窗口来源由 provider 自己带出来 —— `声明(config.yaml)` /
        # `声明(manifest 兜底)` / `保守兜底(未声明)`。
        # 来源随值走：出错时能看出是谁给的。
        if self._provider is None:
            window_source = "无 provider"
        else:
            window_source = getattr(self._provider, "window_source", "未知")

        report: Dict[str, Dict[str, object]] = {
            "context_window": {
                "value": window,
                "source": window_source,
                "model": getattr(self._provider, "_model", "?"),
                "note": "全仓无探测；来源随值走，声明错了由 providers/*/ 的"
                        "_check_window_claim 在实际超过声明时报出来",
            },
            "compression_threshold": {
                "value": int(window * self.compressor._threshold),
                "ratio": self.compressor._threshold,
                "source": f"window × {self.compressor._threshold}",
            },
            "history_ceiling_balanced": {
                "value": 16384,
                "note": "装载期上限；只约束恢复，不约束压缩",
            },
            "max_history_messages": {
                "value": self._max_history,
                "note": "活消息表硬上限（滑窗）",
            },
        }

        system_text, tools = self._non_history_cost("")
        if self._provider is not None:
            bd = self._provider.estimate_request_tokens(
                [], system=system_text, tools=tools
            )
        else:
            bd = {"system": 0, "tools": 0, "raw": 0, "factor": 1.0, "total": 0}
        report["non_history_measured"] = {
            "tool_tokens": bd["tools"],
            "system_tokens": bd["system"],
            "raw": bd["raw"],
            "factor": bd["factor"],
            "tool_count": len(tools),
            "value": bd["total"],
            "source": "本轮 wire 工具集 + 组装后的系统提示"
                      "，与压缩闸门同源同量纲（R3）",
        }
        return report

    def report_thresholds(self) -> None:
        """打印 `describe_thresholds()` —— 启动时调一次。"""
        r = self.describe_thresholds()
        print(f"[threshold] context_window   {r['context_window']['value']:,} "
              f"（来源: {r['context_window']['source']}，"
              f"model={r['context_window'].get('model', '?')})")
        print(f"[threshold] 压缩阈值         "
              f"{r['compression_threshold']['value']:,} "
              f"= window × {r['compression_threshold']['ratio']}")
        print(f"[threshold] 装载上限(balanced) "
              f"{r['history_ceiling_balanced']['value']:,}  ← 只约束恢复，"
              f"不约束压缩：两者不在同一条链上")
        nh = r["non_history_measured"]
        print(f"[threshold] 非历史实际开销   {nh['value']:,} "
              f"({nh['raw']:,} x factor {nh['factor']:.4f} —— "
              f"工具 {nh['tool_tokens']:,} [{nh['tool_count']} 个] + "
              f"系统 {nh['system_tokens']:,})  ← 这三部分都进压缩闸门")

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def storage_id_for(self, channel_key: str) -> str:
        """通道键 → 当前的存储 id。

        没绑过，铸一个新 id 并绑上（不是回落到通道键自己）。

        为什么"总是铸新"而不是"先按通道键、`/clear` 才绑"：
        后者在绑定丢失时回落到按通道键加载，读到的正是第一条线程
        —— 那就是「旧对话复活」，只是换了一扇门。
        「没有绑定，没有线程，从空开始」这个退化方向唯一且安全，
        同 `core/session_key.py` 那条「宁可从空开始」。

        它会写（铸 + 绑）。只问"在不在"的地方走
        `short_term.bound_storage_id()`（纯查询）—— 别让一次判断
        凭空建出一个会话。
        空键是退化调用（`session_key_for(None)` 的契约），
        行为是 `init_session("")`，一条稳定的空行。
        """
        key = str(channel_key or "")
        if not key:
            return self.short_term.init_session(key)
        sid = self.short_term.bound_storage_id(key)
        if sid:
            # 老线程补登记 —— 名册出现之前铸出来的线程不在里面，
            #   而它恰恰是用户正在用的那条，不补的话他一打开列表就看不到自己。
            #   幂等（`INSERT OR IGNORE`）。
            self.short_term.open_thread(key, sid)
            return sid
        sid = self.short_term.init_session(None)
        self.short_term.bind_session(key, sid)
        self.short_term.open_thread(key, sid)
        return sid

    def fork_thread(self, channel_key: str, src_sid: str,
                    *, keep: int | None = None) -> str:
        """从 *src_sid* 的前 *keep* 条分出一条新线程并切过去。

        * `keep=None`，整条照抄（`/fork`：不分段，把现有这一段原样复制一份）
        * `keep=N`，退到第 N 轮之前（`/rewind`）

        与 `new_session` 的关系：那一条是"从空开始一段新的"，
        这一条是"从已有的某一段分出去" —— 两者共用同一套收尾
        （名册记账 · 绑定指过去 · 清会话级内存态），差别只在有没有那次复制。

        源线程一条都不删（`copy_messages` 是复制）——
        `/resume` 随时能切回去。

        `keep` 的合法性由调用方保证（它手上才有 `messages` 列表，
        也只有它能判"这个切点平不平衡"）—— 这里只管复制。
        """
        new_sid = self.short_term.init_session(None)
        n = self.short_term.copy_messages(src_sid, new_sid, keep=keep)
        if src_sid and src_sid != new_sid:
            self.short_term.end_thread(src_sid)
        self.short_term.bind_session(channel_key, new_sid)
        self.short_term.open_thread(channel_key, new_sid)

        # 会话级内存态：与 `/clear` 同一套判据 ——
        # 换了线程就是换了一段对话，发现集 / 待办 / 取消 / ICE / 计划都不该带过来。
        from core.state_registry import purge_session as _purge_session
        _purge_session(channel_key)

        # 本实例的那四格：新线程从已经复制过来的条数起算。
        self._session_note_cache[new_sid] = ""
        self._token_total[new_sid] = self.short_term.get_token_total(new_sid)
        self._saved_count[new_sid] = n
        self._pending_memory_inject[new_sid] = True
        print(f"[memory] 分出线程 {new_sid[:14]}…"
              f"（从 {str(src_sid)[:14]}… 复制 {n} 条，keep={keep}）")
        return new_sid

    def load_session(
        self, session_id: str | None = None
    ) -> Tuple[str, List[Dict]]:
        """
        Initialize or restore a session.

        If *session_id* is None, attempts to restore the most recently
        active session.  Falls back to creating a new one.

        Returns (session_id, messages_list).
        """
        # If no session_id given, try to restore a previous session
        if session_id is None:
            session_id = self.short_term.get_last_session_id()

        sid = self.short_term.init_session(session_id)

        # Load session note first — it is cached and later handed to the
        # compressor as a zero-API-cost summary (see record_turn).
        note = self.session_memory.load(sid)
        if note:
            self._session_note_cache[sid] = note
            print(f"[memory] Loaded session note for {sid} "
                  f"({len(note)} chars)")
        else:
            self._session_note_cache[sid] = ""

        # Budget-based message loading
        # Calculate available space AFTER reserving room for tools + system
        # prompt + conversation turn.  Loads history newest-first until
        # the budget is exhausted.  Older messages drop off silently.
        #
        # Three memory-continuity tiers (fresh / balanced / memory)
        # control the budget ceiling and injection format.
        existing_count = self.short_term.count_messages(sid)
        messages: List[Dict] = []

        # 窗口取自这一段它自己那条配置（不是进程默认）——
        #    三层指向之后每一段的窗口可以不同，而装载预算是按窗口算的。
        #    拿默认那条的窗口去装载另一段的对话 = 装多或装少，而两者都不出声。
        prov = self._provider_for(sid)
        ctx_window = prov.context_window if prov else 65536

        # Read memory continuity setting
        continuity = "balanced"
        try:
            settings_path = Path("data/settings.json")
            if settings_path.exists():
                settings = _std_json.loads(settings_path.read_text(encoding="utf-8"))
                continuity = settings.get("memory_continuity", "balanced")
        except Exception:
            pass

        # Tier-specific parameters
        # 两条判据（45% 上限 / 兜底降档）都在 `_tier_params` 里 ——
        #   搬出去是为了它能被直接测，否则只能间接断言。
        reserve_ratio, ceiling_tokens = _tier_params(
            continuity, ctx_window, getattr(prov, "window_source", ""))

        # Cost of non-history components — MEASURED, not guessed.
        # Guessed constants were wrong in opposite directions: the tool count
        # was the whole registry rather than this session's wire set, and the
        # per-tool constant contradicted bus/registry.py's own 150.
        # See _non_history_cost.
        #
        # This subtraction is a dimensional operation: it takes
        # non-history cost away from `ctx_window`, which is denominated in
        # the API's own tokens.  So the two parts go through
        # `estimate_request_tokens` — the same instrument, and therefore the
        # same units, as the compression gate and the calibrator.
        # Measuring them with raw `count_tokens` would subtract cl100k tokens
        # from API tokens.
        non_history_tokens = 0
        if prov is not None:
            system_text, tool_schemas = self._non_history_cost(sid)
            non_history_tokens = prov.estimate_request_tokens(
                [], system=system_text, tools=tool_schemas
            )["total"]

        # Tier-specific parameters
        # （参数由 `_tier_params` 一处给出。这里不许多一份：
        #   两处各写一次就是两个真相源，而它们会看着都对。）
        reserve_tokens = int(ctx_window * reserve_ratio)
        history_budget_tokens = ctx_window - non_history_tokens - reserve_tokens
        history_budget_tokens = max(history_budget_tokens, 1024)  # floor: 1K tokens
        if ceiling_tokens is not None:
            history_budget_tokens = min(history_budget_tokens, ceiling_tokens)
        # 折算必须用中文比例。本仓实际中文 ≈1.9 字符/token。用 cl100k
        # 的英文口径 4 会把历史装载量按 token 计放大约 2.7 倍（相对保守值 1.5），
        # 即"以为装了 N，其实装了 2.7N"。
        budget_chars = int(history_budget_tokens * CONSERVATIVE_CHARS_PER_TOKEN)

        summary_msg: Optional[Dict] = None

        if existing_count > 0:
            # 装载只看活动窗口 —— 从最后一条压缩边界起
            #    （`live_message_rows`）。被压掉的原话仍在库里
            #    （`history_search` 搜得到），但不进上下文 —— 不进的话
            #    压缩等于没压。没有边界行，全表原样（老库/新会话
            #    即等于默认行为，不需要迁移）。
            raw_messages = self.short_term.live_message_rows(sid)
            raw_count = sum(
                1 for m in raw_messages
                if m.get("role") in ("user", "assistant")
                and m.get("kind") != "history_summary"
            )
            # A compaction summary is a stored message marked with
            # kind="history_summary".  It is a *user* message, so it is
            # identified by `kind`, never by role.
            # 表里可以有多条（每次压缩追加一条，一条都不删）——
            #    这里取最后一条（`live_message_rows` 是按 `id` 升序给的）
            #    所以拿到的永远是最新一代；更早的那些留在库里当历史档案。
            # 行里带的 `id`/`ts` 是给判据用的（比 `kept_from_id`），
            #    不许跟着进 `messages`：那些 dict 会被原样喂给 provider
            #    （`load_message_rows` 的 docstring 里那条，同一族）。
            for msg in raw_messages:
                if msg.get("kind") == "history_summary":
                    summary_msg = {k: v for k, v in msg.items()
                                   if k not in ("id", "ts")}
            if raw_messages:
                loaded_chars = 0
                history = []
                # Only real user/assistant turns.  Excluded here:
                #   * tool messages — carry stale artifacts;
                #   * unmarked system messages — transient injections
                #     (<background_context>, restored-history markers) that
                #     get rebuilt; and
                #   * the marked summary — it is inserted at index 0 below,
                #     so admitting it here would inject it twice, once as a
                #     history entry and once as the summary.
                for msg in reversed(raw_messages):
                    role = msg.get("role", "")
                    if role not in ("user", "assistant"):
                        continue
                    if msg.get("kind") == "history_summary":
                        continue
                    content = msg.get("content", "")
                    # 预算按实发长度扣：注入时会截到
                    # _INJECTED_CONTENT_MAX_CHARS（见下方两处 `[:2000]`）。
                    # 按未截断的 len(content) 扣会"扣得多、发得少"，
                    # 预算被系统性高估，历史装载得比该装的少。
                    msg_chars = min(len(content), _INJECTED_CONTENT_MAX_CHARS)
                    if loaded_chars + msg_chars > budget_chars and history:
                        # 装载预算裁掉了更早的历史 —— 记下原因（轮末读数要报）
                        try:
                            from core import context_pressure as _cp
                            _cp.note_trim(sid, "reload")
                        except Exception:              # noqa: BLE001
                            pass
                        break
                    history.insert(0, {"role": role, "content": content})
                    loaded_chars += msg_chars

                if history:
                    if continuity == "memory":
                        # Memory tier: inject as independent messages
                        # This lets the compressor recognise turn boundaries
                        # and progressively archive old turns instead of
                        # treating the whole history as one monolithic block.
                        messages.append({
                            "role": "system",
                            "content": (
                                "<history_start>\n"
                                "以下为从上次会话恢复的历史记录。"
                                "这些是已发生的对话，请基于它们继续。\n"
                                "</history_start>"
                            ),
                        })
                        for m in history:
                            messages.append({
                                "role": m["role"],
                                "content": m["content"][:_INJECTED_CONTENT_MAX_CHARS],
                                "name": "history",  # marker for compressor
                            })
                        messages.append({
                            "role": "system",
                            "content": (
                                "<history_end>\n"
                                "以上为历史记录。以下为当前对话。\n"
                                "</history_end>"
                            ),
                        })
                    else:
                        # Fresh / Balanced: inject as single system block
                        #
                        # 框架块不变量：这一块把历史内容拼进
                        #   同一个字符串（默认档就走这条），内容里一个伪
                        #   `</conversation_history>` 就能越出包裹。逐条打码。
                        #   （记忆档重放的逐条消息不套新包裹、原样保真 ——
                        #   包裹只在下面这个整块形态里。）
                        prefix = (
                            f"<conversation_history>\n"
                            f"以下是本次会话的最近 {len(history)} 条历史消息 "
                            f"（共 {existing_count} 条，已加载最近 {len(history)} 条）：\n"
                        )
                        history_text = prefix + "\n".join(
                            f"[{m['role']}]: "
                            f"{sanitize_text(m['content'][:_INJECTED_CONTENT_MAX_CHARS])}"
                            for m in history
                        ) + "\n</conversation_history>"
                        messages.append({"role": "system", "content": history_text})
                    print(f"[memory] Loaded {len(history)}/{raw_count} "
                          f"historical messages ({loaded_chars} chars, "
                          f"~{int(loaded_chars / CONSERVATIVE_CHARS_PER_TOKEN)} "
                          f"tokens, tier={continuity})"
                          + (f" + summary "
                             f"({len(summary_msg.get('content', ''))} chars)"
                             if summary_msg is not None else ""))

        # The compaction summary must sit BEFORE the reconstructed
        # <conversation_history> block: it covers exactly the turns that
        # block no longer contains.  The summary is a normal stored
        # message, so it survives a restart on its own.
        if summary_msg is not None:
            messages.insert(0, summary_msg)
            print(f"[memory] Restored compaction summary "
                  f"({len(summary_msg.get('content', ''))} chars)")

        if existing_count > 0 and not messages:
            print(f"[memory] Session {sid[:12]}... has {existing_count} "
                  f"historical messages in DB (budget exhausted)")

        # Restore token counter from SQLite
        if sid not in self._token_total:
            self._token_total[sid] = self.short_term.get_token_total(sid)

        # Track the in-memory list's save position.  Set to current
        # length so record_turn() won't double-save messages that are
        # already in SQLite.  The in-memory list starts with the restored
        # history; record_turn() will only persist new messages added
        # during this session.
        self._saved_count: dict[str, int] = {}
        self._saved_count[sid] = len(messages)

        # Mark for memory injection on the first turn after restore
        self._pending_memory_inject[sid] = True

        return sid, messages

    def open_detached_thread(self, channel_key: str, seed: str = "") -> str:
        """铸一条不与通道绑定的新线程，返回它的存储 id。

        周期任务的 `deliver="new"`（"开一段新对话交付"）走这里。

        它与 :meth:`new_session` 正相反，别混：
        `new_session` 会把通道键重绑过去（= `/clear` 的语义）——
        定时任务照它做，就等于偷偷替用户清了一次对话（用户当前那段被换掉）。
        所以这里只铸 id + 登记名册：`session_bindings` 与 `SessionPool._loaded`
        一个字节都不动，用户当前那段完全不受影响。

        名册那条（`open_thread`）要留 —— 它是任务列表看得见这条线程的唯一理由
        （IM 里默认不推送，但要能翻到）。
        """
        if seed:
            # 同一个 seed 算出同一个 id ——
            #   同批的几条任务各自并行，谁都不能先问别人"我们用哪个 id"。
            #   确定性 = 它们不必协调；`init_session` 是 `INSERT OR IGNORE`（幂等）。
            import hashlib
            sid = "session_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
            self.short_term.init_session(sid)
        else:
            sid = self.short_term.init_session()
        self.short_term.open_thread(channel_key, sid)
        return sid

    def new_session(
        self, old_session_id: str = "", *, channel_key: str = ""
    ) -> Tuple[str, List[Dict]]:
        """
        Force creation of a brand-new session that REPLACES *old_session_id*.

        The old thread is archived, not deleted: its rows stay on disk and
        only the binding moves.  Memory injection is suppressed — the agent
        starts silently.  Long-term facts are preserved.

        铸一个新的存储 id，而旧行原样留着（＝留档）。
        "下一段看不见它"这件事由 `session_bindings` 那张绑定表保证 ——
        通道键指向新线程，旧线程没有任何东西指过去（除非 `/resume` 来指）。

        所以不变量：一条新线程看不见上一段对话。
        （`tests/test_session_binding.py` 用两个场景各钉一遍 ——
        注意它钉的是结果，不是"删了"。）

        会话笔记仍然归档（不销毁）：文件名带时间戳，天然逃出会话键。
        `purge_session` 按 `channel_key` 走 —— 会话级内存态
        （发现集 / 待办 / 取消 / ICE / 计划…）全是通道作用域的，
        `/clear` 该清掉它们。
        """
        # 旧那一段留档，不删
        #
        # 只数一数它有多少行，然后把绑定挪开 ——
        # "看不见"由没有指针保证，而不是由"删掉"保证。
        # `short_term.delete_session()` 在生产路径上没有调用者
        #    （留给"回退 / 清空记忆库"那两条）。这里确实不删，
        #    下面那行日志也就不再说"purged"。
        purged = 0
        if old_session_id:
            try:
                purged = self.short_term.count_messages(old_session_id)
            except Exception as e:
                # 不能静默：裸 `except: pass` 会让"数失败"与"本来就没东西"
                # 看起来完全一样。
                print(f"[memory] /clear 数旧线程失败 for {old_session_id!r}: "
                      f"{type(e).__name__}: {e}")
                purged = -1
            # 会话笔记：归档，不销毁（判据：「`/clear` = 这段对话及其派生物；
            # 清 ≠ 删」）。
            # 文件名带时间戳，逃出会话键，新会话不会读到它，但它留在盘上
            # 当 `memory_organize` 的原料（`RETENTION_DAYS` 到期才回收）。
            archived = self.session_memory.archive(old_session_id)
            if archived is not None:
                print(f"[memory] Archived old session note: {archived.name}")
            # 归档的生命周期：`/clear` 是归档唯一会增长的时刻，
            # 就趁这一下顺手剪（同 `daily_log._prune` 的"顺手剪"形状，不新起线程）。
            # 读不到保留期，一个都不删（判据见 `core/retention.py` 的 docstring）。
            # 只碰归档件 —— 活的 `<键>.md` 是下面那个新会话要用的，不许动。
            from core.retention import prune_archived_notes
            prune_archived_notes(self._sessions_dir)
            # 未被引用的旧图片附件也趁这一下顺手剪 —— 与上面同一条
            #   "顺手剪、不新起线程"的形状。判据：没有任何文本引用 + 够老
            #   （读不到保留期，一个都不删）。
            try:
                from core.attachment_gc import prune as _gc_attach
                _st = _gc_attach()
                if _st.get("deleted"):
                    print(f"[attachments] 回收 {_st['deleted']} 个未被引用的附件"
                          f"（{_st['freed']} 字节）")
                elif _st.get("skipped") == "db_unreadable":
                    print("[attachments] 库读不到 ⇒ 本次一个都没删")
            except Exception as _gc_exc:                 # noqa: BLE001
                print(f"[attachments] 回收失败（不影响会话）："
                      f"{type(_gc_exc).__name__}: {_gc_exc}")
            # 改前留档也趁这一下顺手剪
            #   （同一条"顺手剪、不新起线程"的形状）。
            #   它用的是另一把旋钮（`file_history_days`）—— 那些是
            #   用户文件的一份副本，与框架自己的留档分开控。
            try:
                from core.file_history import prune as _prune_fh
                _prune_fh()
            except Exception as _fh_exc:  # noqa: BLE001
                print(f"[file_history] /clear 顺手剪失败（不影响会话）："
                      f"{type(_fh_exc).__name__}: {_fh_exc}")

        # 总是铸一个新的。
        # 通道键与存储 id 是两件事：`bind_session` 把通道键挪到新线程上，
        # 旧线程从此没有指针（但它的消息行一条都没动）。
        #
        # 不许回落到 `get_last_session_id()`：认领任意一条全局最近的
        # 会话就是"接上了别的通道的对话"。
        sid = self.short_term.init_session(None)
        if channel_key:
            self.short_term.bind_session(channel_key, sid)
            # 旧那条退役（它还留在名册里，只是不再是当前那条），
            #   新的登记进名册并成为当前。
            if old_session_id:
                self.short_term.end_thread(old_session_id)
            self.short_term.open_thread(channel_key, sid)

        # 会话级内存态：走唯一登记表
        #
        # 不要在这里逐个 import + forget_***：那是一串地方而不是不变量，
        # 加一个新的会话级状态、忘了加清点，就会漏清。
        # 统一收在 `core.state_registry`：加新的会话级模块态 → 去那张表登记
        # （census 读者漏一条就红），不要回到这里加第六个 import。
        #
        # Keyed on the RETURNED `sid`, not on *old_session_id*: the
        # invariant is about the session that is fresh now.  When no old
        # id was given there is nothing under the minted one anyway.
        from core.state_registry import purge_session as _purge_session

        # 按 `channel_key` 清，不是按新铸的 `sid` ——
        #   会话级内存态全是通道作用域的（发现集 / 待办 / 取消 /
        #   ICE / 计划 / 审批投影…），它们不保证等于 `sid`。
        #   没给 `channel_key` 的调用（手搭的老测试）落回 `sid` ——
        #   那是兼容不是判据；生产路径只有 `SessionPool` 一家，它一定给。
        _purge_session(channel_key or sid)

        # 下面这几格是本实例的会话态（够不到模块级表，就在自己的拥有者
        # 这里清）。它们逐条登记在 `core.state_registry.SESSION_SCOPED_BY_OTHER`
        # —— 那个模块的实例 census 对着 `MemoryManager.__init__` 反查，
        # 新加实例态忘了登记一样会红。
        # 旧线程那几格顺手摘掉。它们按存储键记
        #   （说的是"这条线程的记账到哪儿了"），而旧线程现在没有任何指针指过去。
        #   不摘 = 每按一次 `/clear` 漏几格。摘掉是安全的：真要读回旧线程时
        #   `load_session` 会把它们重算出来。
        if old_session_id and old_session_id != sid:
            for _d in (self._session_note_cache, self._token_total,
                       self._saved_count, self._pending_memory_inject,
                       self._note_gate):
                _d.pop(old_session_id, None)
        self._session_note_cache[sid] = ""
        self._token_total[sid] = 0
        self._saved_count[sid] = 0
        self._pending_memory_inject[sid] = True
        # 闸门计数一起归零（`/clear` 之后是新的一段对话，
        #   攒够量的那笔账不该跨过去 —— 同 `_token_total` 那条）。
        self._note_gate[sid] = {"tokens": 0, "tools": 0}

        # Report what actually happened: an unconditional
        # "old session purged" sentence makes "nothing was deleted" look
        # like a purge, and the log then hides the difference.
        if old_session_id:
            state = "purged %d rows" % purged if purged >= 0 else "PURGE FAILED"
        else:
            state = "no old session given — NOTHING purged"
        print(f"[memory] New session created: {sid} "
              f"(profile-only injection, {state})")
        return sid, []

    def compact(
        self,
        session_id: str,
        messages: List[Dict],
        channel_key: str = "",
    ) -> Tuple[List[Dict], bool]:
        """
        Manually trigger compression (for `/compact` command).

        它真的强制压 —— check_and_compress(..., force=True)
        跳过阈值那一句。`/compact` 是手动逃生口：它问的是"我要你现在压"，
        不是"你觉得该不该压"。

        `force` 只跳过阈值 —— 压不动的情形（轮次不够 / 没有可压的 /
        摘要没生成出来）仍然照判，并且由 `last_skip_note` 照实回话。

        摘要一律由模型写（手动与自动同一条路）：笔记只作为输入参与，
        并在模型失败时兜底。

        Returns (compressed_messages, was_compressed).
        """
        #: 手动压缩 = 用户在说"再试一次"：熔断清零，前两级重新可用。
        try:
            from core.memory.compressor import forget_breaker
            forget_breaker(session_id)
        except Exception:                                      # noqa: BLE001
            pass

        current_note = self._session_note_cache.get(session_id, "")

        old_count = len(messages)
        # 手动压缩也按这一段的窗口判（同自动那条）。
        prov = self._provider_for(session_id)
        system_text, tool_schemas = self._non_history_cost(session_id)
        compressed, was_compressed = self.compressor.check_and_compress(
            session_id,
            messages,
            prov,
            prov.context_window,
            session_note=current_note,
            mode="full",  # /compact → full compression
            force=True,   # 手动逃生口 —— 跳过阈值那一句
            system=system_text,
            tools=tool_schemas,
            channel_key=channel_key,
        )

        if was_compressed:
            self._token_total[session_id] = 0
            self._persist_compaction(session_id, compressed)
            self._pending_memory_inject[session_id] = True
            print(f"[memory] Manual compaction: "
                  f"{old_count} → {len(compressed)} messages")

        return compressed, was_compressed

    def _emit_compaction_progress(self, session_key: str,
                                  window: int | None,
                                  blocked: bool = False) -> None:
        """压缩收尾那一条（自动压缩走这里；手动那条在命令处理处发）。

        观测不许阻断一轮：坏掉只说一声。
        """
        try:
            from core.events import (EVT_COMPACT_PROGRESS, get_event_bus,
                                     make_emitter)
            from core.memory.compressor import progress_payload
            _emit = make_emitter(get_event_bus(), session_key)
            if _emit is None:
                return
            payload = progress_payload(self.compressor.last_compression, window,
                                       payload_blocked=blocked)
            _emit(EVT_COMPACT_PROGRESS, **payload)
        except Exception as exc:                               # noqa: BLE001
            print("[memory] 压缩进度事件发不出去（不影响本轮）：%s: %s"
                  % (type(exc).__name__, exc))

    def repersist_messages(self, session_id: str, messages: List[Dict]) -> None:
        """把已落库的行整表重写成 *messages*。

        为什么必须有它：常规落库是只追加 —— `record_turn` 里写的是
        `new_msgs = messages[saved_count:]`。而审批重放改的是
        一条已经落库的旧消息（那条 `pending_*` 就地换成真结果）
        所以只追加的路径永远不会写它。

        不写回去的后果：库里那两条 `pending_command_approval` 原样留着，
        真实结果一行都没有，当轮内正确，但下一轮从库里装载时
        那两条永不会被回答的 pending 会一起回来。

        代价如实记：`replace_messages` 是 DELETE + INSERT 整表，
        一次审批重放付一次全表重写。会话行是几十到几百的量级，可以接受；
        别把它挂到每轮都走的那条路上（那条路本来就靠"只追加"省 fsync）。
        """
        self.short_term.replace_messages(session_id, messages)
        self._saved_count[session_id] = len(messages)

    def _persist_compaction(
        self,
        session_id: str,
        compressed: List[Dict],
    ) -> None:
        """把一次压缩落库 —— 只追加：`[摘要行, 边界行]`。

        不要改成「把整表重写成 `compressed`」（`repersist_messages` →
        `replace_messages` = DELETE + INSERT）—— 那会真的删掉被压掉的原话，
        于是 `history_search` 再也搜不回来。

        现在做两件事：
        ① 追加新摘要（摘要是新的，append 就能写进去）；
        ② 追加一条边界行，记住"活动窗口从哪一行起"。边界在下次装载时
           切掉被取代的轮次，所以不需要整表重写。

        所以被压掉的原话一行不删，`history_search` 因此真能搜回。

        保留的那几轮不重复追加：它们本来就在库里（压缩发生在落库之后）。

        `_saved_count` 仍然诚实地等于 `len(compressed)` —— 它数的是
        「内存里前多少条已经落过库」，而内存里那几条（摘要 + 保留轮）确实
        都有对应行了。库里多出来的那些行（被压掉的原话）是历史档案，
        不是"还没存的内存消息"。
        """
        if not compressed:
            return
        summary_msg = compressed[0]
        if summary_msg.get("kind") != "history_summary":
            # 压缩器给不出摘要时压根不会走到这里（那时 `was_compressed=False`）。
            # 真走到了就如实出声，别把一条普通消息当摘要钉进库里。
            print("[memory] 压缩落库跳过：compressed[0] 不是摘要行 "
                  "(kind=%r)" % (summary_msg.get("kind"),))
            return
        self.short_term.append_compaction(
            session_id,
            summary_msg,
            kept_from_id=self._kept_region_start(session_id, compressed),
        )
        self._saved_count[session_id] = len(compressed)

    def _kept_region_start(
        self, session_id: str, compressed: List[Dict],
    ) -> int:
        """算「活动窗口的第一行行号」—— `append_compaction` 的 `kept_from_id`。

        判据：保留的轮 = 库里最后 `kept_turns` 个轮首行。
        为什么可以从库尾数、不必知道每条内存消息对应哪一行：
        装载按预算丢的永远是最旧的（`load_session` 从新往旧装），
        内存里的轮永远是库里那些轮的一个后缀，最后 N 个一定对得上，
        与"装载时丢了前面多少轮"无关。

        前提：压缩发生在落库之后
        （`record_turn` 是先存后压 · `/compact` 走 pre_process，上一轮早已落库）。
        库里比内存少轮首时，说不上是哪几行，取最早那一行 ——
        多留 = 安全方向（顶多多看几轮），反过来会把还活着的轮标成死的。

        返回值单调：边界只许往后（`max(..., prev)`）—— 往回会让已经
        出局的那些行复活成活动窗口。
        """
        n_kept = sum(
            1 for m in compressed
            if m.get("role") == "user"
            and m.get("kind") != "history_summary"
        )
        if not n_kept:
            return 0
        rows = self.short_term.load_message_rows(session_id)[0]
        prev = 0
        for r in rows:
            if r.get("kind") == COMPACTION_BOUNDARY_KIND:
                prev = int(r.get("kept_from_id") or 0)
        starts = [
            int(r.get("id") or 0) for r in rows
            if int(r.get("id") or 0) >= prev
            and r.get("role") == "user"
            and r.get("kind") != "history_summary"
        ]
        if not starts:
            return 0
        if len(starts) < n_kept:
            print(f"[memory] 压缩边界：库里只有 {len(starts)} 个轮首、"
                  f"内存里有 {n_kept} 个 —— 保守取最早那一行（多留不丢）")
            return starts[0]
        return max(starts[len(starts) - n_kept], prev)
    def _apply_sliding_window(
        self, session_id: str, messages: List[Dict],
    ) -> Tuple[List[Dict], bool]:
        """把消息表硬压到 *max_history* 以内。

        保留表头前导块（压缩摘要 / 恢复期的历史标记）与最近的轮，
        并在截断点贴一行说明（`<window_note>`，告诉模型更早的怎么找回）。
        最后一道兜底网 —— 正常情况压缩早就把表压下去了。

        返回 (truncated_messages, was_truncated)。纯函数（谁调用谁负责
        同步 `_saved_count` —— 生产里是 `maybe_trim_cold_window`）。

        ## 两条承重规则

        ① 截断点对齐轮边界：按下标切（`messages[-keep_tails:]`）
           可能一刀落在工具序列中间，表尾以孤儿 `role: tool` 结果开头
           所以上游硬 400（坏历史不许送去上游）。现在从 `len-keep_tails`
           起向后找到第一条 `role=user`（且不是摘要）的消息才下刀；找不到
           就不剪（宁可超限，不造坏历史）。
        ② 截断点贴一行说明（`window_note`）：模型看不到被移走的那些消息，
           但"更早的还在、可以用 `history_search` 找回"这件事必须让它知道 ——
           不然一节流它就会以为"我们从没聊过 X"。说明行占一个保留位
           （上限仍是 max_history 条，不额外超）。

        被切掉的消息留在 SQLite 里（`history_search` / 重启装载都找得到）
        —— 这也是说明行敢写"可找回"的底气。
        """
        if len(messages) <= self._max_history:
            return messages, False

        # Count the leading preamble: the blocks that carry session state and
        # must never be cut — the compaction summary, and on a restored
        # session the history markers (<history_start> / <history_end> /
        # <conversation_history>).
        #
        # The summary is a `user` message (kind="history_summary"), so a
        # restored session whose first row is the summary would count zero
        # heads → keep_heads = 0 → the sliding window would silently
        # discard the summary.  The test is "is this preamble", not "is
        # this a system message".
        #
        # The cap stays 3: the memory tier still uses summary +
        # <history_start> + <history_end>.
        preamble_max = 3
        head_count = 0
        for m in messages:
            if m.get("role") == "system" or m.get("kind") == "history_summary":
                head_count += 1
            else:
                break

        keep_heads = min(head_count, preamble_max)
        # 说明行占一个位子（总数仍 ≤ max_history）；上一轮的旧说明行不占位 ——
        # 先剔掉旧的，免得剪第二次时越攒越多。
        body = [m for m in messages if m.get("kind") != "window_note"]
        if len(body) <= self._max_history:
            return messages, False
        keep_tails = self._max_history - keep_heads - 1
        if keep_tails < 1:
            return messages, False

        # ① 对齐轮边界：从 len-keep_tails 向后找第一条"真轮首"（role=user 且非摘要）
        start = len(body) - keep_tails
        while start < len(body):
            m = body[start]
            if m.get("role") == "user" and m.get("kind") != "history_summary":
                break
            start += 1
        if start >= len(body):
            print(f"[memory] 滑窗跳过：表尾找不到轮边界（{len(messages)} 条，"
                  f"更愿意超限也不切坏历史）")
            return messages, False

        heads, tail = body[:keep_heads], body[start:]
        dropped = len(body) - keep_heads - len(tail)
        note = {
            "role": "system",
            "kind": "window_note",
            "content": (
                "<window_note>\n"
                f"更早的 {dropped} 条消息已移出本会话的上下文窗口"
                f"（这里保留最近的 {len(tail)} 条）。\n"
                "它们仍在历史库里 —— 需要时用 history_search 按关键词找回。\n"
                "</window_note>"
            ),
        }
        truncated = heads + [note] + tail

        print(
            f"[memory] 冷闸滑窗：{len(messages)} → {len(truncated)} 条"
            f"（移出 {dropped} 条，max_history={self._max_history}）"
        )
        return truncated, True

    def maybe_trim_cold_window(
        self, session_id: str, messages: List[Dict],
    ) -> List[Dict]:
        """滑窗的冷闸 —— 发请求前调用，只在"冷"时剪。

        把"每轮无条件剪"改成"发请求前的懒检查"。
        三道闸按序：

          ① 设置 `context_sliding_window`（默认开）—— 关了 从不剪；
          ② 超 `max_history` 才谈得上剪（`_apply_sliding_window` 自会判）；
          ③ 冷：`now − last_turn_at > sliding_window_gap_minutes`（默认 90）。
             判据不是"性能"，是缓存：滑窗从表头丢，前缀改写，缓存断代。
             只有"上次活动已过去很久"时前缀缓存本来就不在了，剪才不额外付
             这份代价。（默认 90，设置可改。）

        `last_turn_at` 是 `None`（本进程没见过这个会话的轮末），按"不冷"：
        可能刚重启、上游的前缀缓存还在 —— 保守方向是不剪（本会话的历史本来
        也可能一条没超限）。这与 `core/memory_extract.last_turn_at()` 的契约一致。

        剪成之后同步 `_saved_count`（同旧行为：后续落库相对保留的那份）。
        """
        if not messages or len(messages) <= self._max_history:
            return messages

        enabled, gap_min = _cold_window_settings()
        if not enabled:
            return messages

        from core.memory_extract import last_turn_at
        t = last_turn_at(session_id)
        if t is None:
            return messages
        if time.time() - t <= gap_min * 60:
            print(f"[memory] 滑窗跳过：会话仍热（上次活动 {int((time.time()-t)/60)} "
                  f"分钟前 ≤ 冷闸 {gap_min} 分钟）—— 不剪，护前缀缓存")
            return messages

        trimmed, was_trimmed = self._apply_sliding_window(session_id, messages)
        if was_trimmed:
            try:
                from core import context_pressure as _cp
                _cp.note_trim(session_id, "cold_window")
            except Exception:                      # noqa: BLE001
                pass
            # 后续落库相对保留的那份
            self._saved_count[session_id] = len(trimmed)
        return trimmed

    # ------------------------------------------------------------------
    # Per-turn context injection
    # ------------------------------------------------------------------

    def get_context(
        self,
        session_id: str,
        user_query: str,
        messages: List[Dict] | None = None,
    ) -> str:
        """Build a memory context string to inject into the system prompt.

        Memory is ONLY injected when the session context is thin —
        after session restore or compression.  During normal
        conversation the full message history already provides all
        the context the model needs; injecting extra context on
        every turn causes the "reunion greeting" problem.

        这里只有会话笔记一段 —— 长期记忆改走 `<memory_index>`，
          挂在消息侧由 `agent_loop` 注入（见 `core/memory_store.py`）。

        Args:
            session_id: Session identifier.
            user_query: 不再使用 —— 参数保留仅供
                调用方 API 兼容（同 `messages`）。
            messages: Current message list (unused, kept for API compat).
        """
        # Only inject memory on turns where context is genuinely thin.
        # After the first injection, the flag is cleared and normal
        # turns get an empty context — the LLM relies on the full
        # message history for continuity.
        if not self._pending_memory_inject.get(session_id, True):
            return ""

        # Clear the flag — inject only once until next restore/compress
        self._pending_memory_inject[session_id] = False

        parts: List[str] = []

        # Session note —— 模型自己上一轮的笔记。
        note = self._session_note_cache.get(session_id, "")
        if note:
            note_lines = [
                # 「your own notes from earlier turns」是来源描述，
                # 不是不可信降级 —— 它就是模型自己上一轮的自述。
                "## SESSION STATE (your own notes from earlier turns "
                "in this conversation — internal tracker, do NOT mention "
                "or reference this section in your replies.  Use it "
                "only for background awareness of the ongoing "
                "conversation.)",
                note,
            ]
            parts.append("\n".join(note_lines))
            print(f"[memory] get_context: session note injected "
                  f"({len(note)} chars)")
        else:
            print(f"[memory] get_context: no memory context available yet")

        print(f"[memory] get_context: memory injected "
              f"({sum(len(p) for p in parts)} chars — one-shot, "
              f"will be silent until next restore/compress)")

        # Wrap in XML tags so the LLM can structurally distinguish
        # background reference from the current conversation task.
        # Without this boundary, the model treats injected memory as
        # part of the live conversation and feels compelled to
        # acknowledge it ("I remember you!", recaps, etc.).
        #
        # 框架块不变量：会话笔记来自框架之外（模型自述），
        #   先打码再进包裹 —— 产者就这一处。
        content = sanitize_text("\n\n".join(parts))
        return (
            "<background_context>\n"
            + content
            + "\n</background_context>"
        )

    # ------------------------------------------------------------------
    # Per-turn recording
    # ------------------------------------------------------------------

    def record_turn(
        self,
        session_id: str,
        user_message: Dict,
        assistant_message: Dict,
        token_count: int,
        messages: List[Dict] | None = None,
        channel_key: str = "",
        rows: Dict | None = None,
    ) -> List[Dict] | None:
        """
        Called after each conversation turn.

        1. Save user + assistant messages to SQLite.
        2. Accumulate token count, check compression threshold.
        3. 后台会话笔记 + 日报 + 后台记忆抽取。

        本函数里参与请求的三处（笔记那条后台 LLM 调用 ·
          压缩闸门 · 后台抽取）全部问 `self._provider_for(session_id)`
          —— 它们都是这一段对话的辅助调用，用别的段的模型是错的
          （而症状是"窗口/账目对不上"，看不出跟模型选择有关系）。

        Args:
            session_id: Session identifier.
            user_message: The user's message dict.
            assistant_message: The assistant's response message dict.
            token_count: Tokens consumed this turn (prompt + completion).
            messages: The full message list (background hooks read it).
                If None, the background hooks are skipped.
            rows: 传一个 dict 进来 —— 本函数会把它填成
                {"user": <行号>, "assistant": <行号>}。
                不是返回值：本函数已有返回值（那份 messages），
                而调用方（`post_process`）要的是结构化的两个行号，
                不是"自己再从 messages 尾巴上数一遍"——数尾巴会随压缩/重放漂。
                拿不到（没落库）时它停在空 dict，调用方据此发 `None`。

        Returns:
            The (possibly compressed) messages list — 注意没压缩时也返回
            那份列表（不是 None；`post_process` 按 `is not ctx.messages` 判）。
        """
        # 把"新落那几行的行号"按角色收起来。
        #    逻辑本身在模块级 `collect_anchor_rows`（可直接测 ——
        #    `tests/test_turn_rows.py`），这里只负责两条分支都调它。
        def _collect(ids, msgs) -> None:
            collect_anchor_rows(ids, msgs, rows)

        # 本段的 provider 在这里取一次，下面三处共用
        #    （笔记 / 压缩闸门 / 后台抽取）—— 取三次就是三次 sqlite 查表 +
        #    三次可能分叉的答案（而它们说的必须是同一件事）。
        prov = self._provider_for(session_id)

        # 1. Persist ALL new messages since last save (not just user+assistant)
        # 这一轮跑了几个工具（会话笔记那道量闸的一条输入）。
        #   判据与"为什么只数这一轮"在模块级 `count_tool_calls` 的 docstring 里。
        turn_tools = 0
        if messages is not None:
            saved_count = self._saved_count.get(session_id, 0)
            # Guard against saved_count drift — compression, /clear, and
            # in-place list replacement can leave the counter out of sync.
            if saved_count > len(messages):
                saved_count = 0
            new_msgs = messages[saved_count:]  # only unsaved messages
            turn_tools = count_tool_calls(new_msgs)
            # 一次事务，不是每条一次 `commit`（= 每条一次 fsync）。
            # 一轮要落 3 条以上消息，逐条提交会把一次事务切碎。
            # 逐条的 `token_count` 是同一个值（原样保留）。
            _ids = self.short_term.save_messages(
                session_id, new_msgs, [token_count] * len(new_msgs)
            )
            _collect(_ids, new_msgs)
            self._saved_count[session_id] = len(messages)
            print(f"[memory] Saved {len(new_msgs)} new messages "
                  f"(total: {len(messages)}, session: {session_id[:12]}...)")
        else:
            _pair = [user_message, assistant_message]
            _collect(self.short_term.save_messages(
                session_id, _pair, [token_count, 0]
            ), _pair)

        # 这一轮算数 —— 线程名册里那个「N 轮」的来源
        #   （`/resume` 列表要靠它给每条线程一个可辨的尺子）。
        #   永不抛：名册是展示用的，坏掉不该影响一轮对话的落库。
        try:
            self.short_term.bump_thread_turn(session_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[memory] 线程名册计数失败（不影响本轮）："
                  f"{type(exc).__name__}: {exc}")

        # 轮末封印文件留档 —— 盖的是"这一轮改完之后
        #    这个文件长什么样"的哈希，它是回退那半边安全闸的输入
        #    （写回之前先拿它核对"现在这份还是我们留下的吗"）。
        #    用通道键：文件留档那张索引就是按通道键建的
        #    （`SecurityGuard` 那侧取的也是它），不是存储键。
        #    永不抛：轮末的记账，坏掉不该影响一轮对话。
        try:
            from core.file_history import seal_turn
            seal_turn(channel_key or session_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[memory] 文件留档封印失败（不影响本轮）："
                  f"{type(exc).__name__}: {exc}")

        # 2. Update token counter
        self._token_total[session_id] = (
            self._token_total.get(session_id, 0) + token_count
        )

        # 2.5. Launch background thread for session note update.
        #      This is thread-safe: it only does file I/O (.md write).
        #
        #      Compression (the next numbered step) uses the *previous* session note
        #      from the cache — at most one turn stale, negligible for
        #      a summary that covers dozens of turns.
        current_note = self._session_note_cache.get(session_id, "")

        # Pre-extract for the background closure
        #
        # 走 `content_text` —— 这一份文本同时喂三个
        # 消费者（日报文件 · 会话笔记那次 LLM 调用 · 后台记忆抽取），
        # 而带图那一轮的 `content` 是块列表，`str()`/直接插入都会得到一份
        # 带 base64 的 repr（几十万字符落进 .md、落进提示词、落进抽取器）。
        # 当前传进来的确实是 `str`（`post_process` 用的是 `ctx.user_message`）
        # —— 但那条"确实是"没有任何机械读者，而代价是三处一起坏。
        user_text = content_text(
            user_message.get("content") if isinstance(user_message, dict)
            else user_message)
        assistant_text = content_text(
            assistant_message.get("content") if isinstance(assistant_message, dict)
            else assistant_message)

        # 2.7. Daily log：框架直接写的一轮一段（零 LLM）。
        #      与会话笔记不合并（见 `__init__` 那条）：笔记随会话消失、
        #      日报按日期活过 `/clear` —— 也正因此它不在任何 purge 清单里。
        #      `log_turn` 自己保证永不抛（失败出声不阻断）。
        log_turn(self._logs_dir, session_id, user_text, assistant_text)

        # 2.8. tool 结果淘汰 ——
        #      存史只会越留越大，超上限就从最旧那头整组删 `role='tool'`
        #      （`user`/`assistant` 一条不动；判据与"为什么不会让 tool_calls 悬空"
        #      都在 `short_term.prune_old_tool_messages` 的 docstring 里）。
        #      节流：这件事不紧急（超一点没关系），而判定要扫一遍全表
        #      所以每 20 轮才看一次。
        #      计数器用线程已有的 `turn_count`，不新起一份会话态 ——
        #        新起就得去 `core/state_registry.py` 登记，
        #        而这里本来就有现成的、而且是跨重启的。
        #      永不抛：轮末的记账，坏掉不该影响一轮对话。
        try:
            _turn_no = self.short_term.thread_turn_count(session_id)
            if _turn_no > 0 and _turn_no % 20 == 0:
                self.short_term.prune_old_tool_messages()
        except Exception as exc:  # noqa: BLE001
            print(f"[memory] tool 淘汰检查失败（不影响本轮）："
                  f"{type(exc).__name__}: {exc}")

        def _update_note_bg() -> None:
            """Background: update the session note .md file via LLM."""
            try:
                updated = self.session_memory.update(
                    session_id=session_id,
                    current_note=current_note,
                    user_message=user_text,
                    assistant_message=assistant_text,
                    provider=prov,
                )
                self.session_memory.save(session_id, updated)
                self._session_note_cache[session_id] = updated
                self._note_bg_status(
                    f"[memory] Session note updated ({len(updated)} chars)"
                )
            except Exception as e:
                self._note_bg_status(
                    f"[memory] Session note update skipped: "
                    f"{type(e).__name__}: {e}"
                )

        # 攒够才写 —— 不是每轮一次 LLM 调用。
        #    判据（三个数）与"为什么不能改小"在
        #    `core/memory/session_memory.py::note_gate_met` 上方那一段。
        #    累计量先加再看：本轮的量也算数（"这一轮跑了 20 个工具"
        #    正是这条闸门要抓的那种轮次，等下一轮才认就白等一轮）。
        #    永不抛：轮末的记账，坏掉不该影响一轮对话 —— 闸门算不动
        #    就退回旧行为（写），而不是把笔记整个停掉。
        _gate = self._note_gate.setdefault(session_id, {"tokens": 0, "tools": 0})
        _gate["tokens"] += token_count
        _gate["tools"] += turn_tools
        try:
            _open = note_gate_met(
                _gate["tokens"], _gate["tools"], bool(current_note.strip()))
        except Exception as exc:  # noqa: BLE001
            _open = True
            self._note_bg_status(
                f"[memory] 笔记量闸判不动，退回每轮写：{type(exc).__name__}: {exc}")

        if not _open:
            _need = (NOTE_UPDATE_TOKENS if current_note.strip()
                     else NOTE_INIT_TOKENS)
            self._note_bg_status(
                "[memory] Session note skipped (note gate): "
                f"{_gate['tokens']}/{_need} tokens, "
                f"{_gate['tools']}/{NOTE_UPDATE_TOOL_CALLS} tool calls")
        else:
            _gate["tokens"] = 0
            _gate["tools"] = 0
            threading.Thread(target=_update_note_bg, daemon=True).start()

        # 3. Check compression threshold
        compressed_messages = None
        was_compressed = False
        # 群聊的判据与单聊不同，且只在这一处判定。
        #    · 阈值 0.8：群聊一轮有 N 条成员回复，而闸门量的是
        #      `assemble_system_prompt`（不含成员的群规则块与尾部摘要）
        #      所以读数是低估的，提前压一点；
        #    · 纪要在每 N 轮与压缩之后各兜底一次（见下面「群聊会议纪要」那段）。
        #   认群用通道键（`channel_key`），不是存储 id —— `/clear` 之后
        #      后者换新，而群还是同一个群。
        _gid = ""
        try:
            from core.session_key import group_id_of
            _gid = group_id_of(channel_key or "") or ""
        except Exception:                                       # noqa: BLE001
            _gid = ""
        if messages is not None:
            # The system prompt lives outside `messages` and the tool schemas
            # never were in it, so the gate has to be told about both —
            # otherwise the threshold silently moves by one system prompt
            # (~2,000 tokens) plus one tool table (~4,800), i.e. it stops
            # matching the quantity the threshold is denominated in.
            # The parts are handed over as the objects themselves; the
            # provider does all the counting.
            system_text, tool_schemas = self._non_history_cost(session_id)
            compressed_messages, was_compressed = self.compressor.check_and_compress(
                session_id,
                messages,
                prov,
                prov.context_window,
                session_note=current_note,
                mode="auto",  # automatic → gradual 30% compression
                system=system_text,
                tools=tool_schemas,
                # 群聊 0.8；单聊 `None`（沿用全局 0.85，一个字不变）。
                threshold=0.8 if _gid else None,
                #: 机械那两段（用户原话 / 动过的文件）按通道键读账本。
                channel_key=channel_key or "",
            )
            if was_compressed:
                self._token_total[session_id] = 0
                self._persist_compaction(session_id, compressed_messages)
                self._pending_memory_inject[session_id] = True
                print(f"[memory] Auto-compression applied: "
                      f"{len(messages)} → {len(compressed_messages)} messages")
                self._emit_compaction_progress(
                    channel_key or session_id,
                    getattr(prov, "context_window", None))
            elif getattr(self.compressor, "last_blocked", False):
                #: 已经进强制区却一点都没压成 —— 这时才劝用户重启。
                self._emit_compaction_progress(
                    channel_key or session_id,
                    getattr(prov, "context_window", None), blocked=True)

        # 3.2. 群聊会议纪要。
        #      触发：每 N 轮 + 压缩前兜底；独立一次小调用；不显示在对话里。
        #
        #      喂的是压缩前的 `messages`：压缩是把旧轮次挤出活动窗口的那一刻，
        #        纪要必须包含那些轮次。`check_and_compress` 不原地改入参
        #        （它返回新列表），所以这里拿到的仍是完整历史。
        #      只在无主理人的群里生成（含圆桌：圆桌共享历史、
        #        涨得更快，压缩更早，纪要更不能少）；
        #        有主理人的群本来就有主理人自己的上下文。
        #      后台跑：轮末的记账不该拖住用户这一轮的回复。
        #      永不抛：坏掉只说一声（同 `log_turn` / `seal_turn`）。
        if messages is not None and _gid:
            try:
                from core import groups, group_minutes
                group = groups.get(_gid)
                turn_no = self.short_term.thread_turn_count(session_id)
                if group_minutes.should_update(group, was_compressed, turn_no):
                    group_minutes.spawn_update(
                        group, session_id, messages,
                        self._provider_for(session_id), turn_no)
            except Exception as exc:                            # noqa: BLE001
                print(f"[group_minutes] 触发失败（不影响本轮）："
                      f"{type(exc).__name__}: {exc}")

        # 3.5. （滑窗不在这里。）
        #      轮末无条件剪（超 max_history 就剪）是最坏的位置：滑窗从表头
        #      开始丢，剪一次就把整段前缀改成新序列，下一轮缓存从断点起全部重算。
        #      所以改为发请求前的懒检查 + 冷闸：只有"上一次活动已过去很久"
        #      （默认 90 分钟）时才剪 —— 那时前缀缓存本来就不在了，剪不额外付代价。
        #      落点：`AgentLoopStage.process` → `maybe_trim_cold_window()`（本类）。
        #      剪法本身把截断点对齐到轮边界，避免切断工具序列
        #      （孤儿 `role: tool` 结果，上游 400）。

        # 3.7. 后台记忆抽取：轮末 fire-and-forget 一个
        #      fork 抽取器。闸序 / fork 形状 / 全部
        #      判据在 `core/memory_extract.py`；这里只喂它轮末的两个输入。
        #      `messages` 传最终的那份（压缩之后）—— 与下一轮请求
        #      同前缀（fork 吃缓存），也与"会话真正带下去的东西"一致。
        #      快照由 `maybe_extract` 自己从 ContextVar 现取（本函数跑在请求任务上）。
        #      抽取失败绝不许弄挂这一轮（`log_turn` 的同款判据）。
        #      这一块只许出现一次 —— 复制成两份会让每轮
        #         被调两次：节流计数翻倍（默认 3 → 实际约每 2 轮触发）、互斥轮在
        #         第二次调用里窗口已空而被误计。回归读者：
        #         `tests/test_memory_extract.py::test_record_turn_schedules_extract_once`。
        if messages is not None:
            try:
                from core.memory_extract import maybe_extract
                # 后台抽取拿的是通道键。
                #    它那一路全部是通道作用域的：`_state` 节流格 · 快照比对
                #    （`snap.session_id` 就是通道键）· fork 的
                #    `agent_loop(session_id=)`（工具门禁 / 白名单 / 取消）·
                #    `tiers.current_spec` · `emit(session_key=)`。
                #    必须传通道键：若传 `session_id`（存储键），
                #    快照那道闸 `snap.session_id != session_id` 会永不相等
                #    所以抽取器一次都不会跑，且一声不响。
                maybe_extract(
                    session_id=channel_key or session_id,
                    messages=(compressed_messages if compressed_messages is not None
                              else messages),
                    provider=prov,
                    status=self._note_bg_status,
                )
            except Exception as e:  # noqa: BLE001 —— 闸门本身不许成为故障源
                self._note_bg_status(
                    f"[memory] 后台抽取调度失败：{type(e).__name__}: {e}"
                )

        # 3.8. 记忆巩固：与 3.7 是兄弟不是副本 ——
        #      同一条"轮末 fire-and-forget"的路，门控完全不同（≥24h + ≥5 会话 +
        #      10 分钟扫描节流 + 一把锁文件）。闸序 / 四阶段提示词 / 锁的回滚全在
        #      `core/memory_organize.py`；这里只喂它轮末的输入与本实例的三条路径。
        #      它读得比主线宽（日报 + 会话笔记），靠 `memory_read` 的巩固模式；
        #      它占并发上限 3 里的一个名额（设计侧判据：整理是第 4 个会跑的）。
        if messages is not None:
            try:
                from core.memory_organize import maybe_organize
                # 整理同时要两个键 ——
                #    `session_id` 走通道键（`_state` / 快照比对 / fork 的
                #    `agent_loop` / 档位 / 事件），`storage_id` 走存储键
                #    （`sessions_to_review` 比的是笔记文件名，而笔记按存储 id 命名）。
                maybe_organize(
                    session_id=channel_key or session_id,
                    storage_id=session_id,
                    messages=(compressed_messages if compressed_messages is not None
                              else messages),
                    provider=prov,
                    # 锁文件住在记忆目录里（`logs/` 的父目录
                    # 就是 `storage_path`；换 storage_path 时锁跟着走）
                    memory_root=self._logs_dir.parent,
                    logs_dir=self._logs_dir,
                    sessions_dir=self._sessions_dir,
                    status=self._note_bg_status,
                )
            except Exception as e:  # noqa: BLE001 —— 闸门本身不许成为故障源
                self._note_bg_status(
                    f"[memory] 记忆巩固调度失败：{type(e).__name__}: {e}"
                )

        # 4. 长期记忆的写入有两条路：后台 fork 抽取器（写 `memory_store`，
        #    见上面「后台记忆抽取」那段）与主 agent 自己调 `memory_write`。

        return compressed_messages

    # ------------------------------------------------------------------
    # Background status flush
    # ------------------------------------------------------------------

    def _note_bg_status(self, line: str) -> None:
        """后台线程追加一行状态（带上界，满了丢最旧的）。

        上界的理由：CLI 不跑的时候没人冲刷，无界增长要跑很久才发作。
        """
        with self._bg_status_lock:
            self._bg_status.append(line)
            if len(self._bg_status) > self._BG_STATUS_MAX:
                del self._bg_status[: len(self._bg_status) - self._BG_STATUS_MAX]

    def flush_bg_status(self) -> None:
        """Print and clear any pending background-thread status lines.

        Call this BEFORE printing the `You>` prompt so that
        background output never interleaves with the input line.
        """
        with self._bg_status_lock:
            if not self._bg_status:
                return
            for line in self._bg_status:
                print(line)
            self._bg_status.clear()

    # ------------------------------------------------------------------
    # Context usage
    # ------------------------------------------------------------------

    def get_context_usage(
        self, messages: List[Dict], session_id: str = ""
    ) -> dict:
        """
        Return context-window usage statistics for display.

        The total covers all three parts of the request: `messages`,
        the system prompt (outside `messages`) and the tool schemas (never
        in it).  Each omission would be silent and would move the displayed
        percentage away from what the request actually costs — ~2,000 tokens
        for the prompt, ~4,800 for a 22-tool wire set.  The user reads that
        number as "how full is my context", so it has to be the same quantity
        the API bills.

        Returns dict with:
          - estimated_tokens: current token estimate (messages + system + tools)
          - message_tokens / system_tokens / tool_tokens: the parts
          - factor: the calibration factor applied to the sum
          - context_window: provider's max context window
          - percentage: usage percentage (0.0–100.0)
          - threshold_pct: compression trigger threshold
          - threshold_tokens: 同一个阈值，token 口径
          - armed: 已进预警区、但还没到阈值
          - window_source: 窗口那个数从哪儿来的

        后三格是给事件流用的：要能看出
        「阈值是 token 还是百分比」「窗口那个数是谁声明的」
        「离触发还有多远」—— 只靠占用一个数回答不了。

        `armed` 不许在这里再算一遍 —— 它是
        `Compressor.is_armed` 那一处判定的产物。抄第二份的代价不是
        多写一行，是改了预警比例只动一处、而两个答案看着都对。

        入参是通道键，不是存储 id（工具表那一半
        按通道作用域取）。而窗口那一半按会话取，`_provider_for` 的
        归一那一步两种形状都收（见它的 docstring）。
        这一处必须跟着会话走：否则顶栏那条读数会拿默认那条的窗口
        去除这一段的占用，显示的比例是假的（标签在说谎）。
        """
        prov = self._provider_for(session_id)
        system_text, tool_schemas = self._non_history_cost(session_id)
        bd = prov.estimate_request_tokens(
            messages, system=system_text, tools=tool_schemas
        )
        window = prov.context_window
        pct = (bd["total"] / window * 100) if window > 0 else 0.0
        # 来源随值走。只给值不给来源，读的人无从知道
        # 131,072 是"配置里写着的"还是"没声明时的保守兜底"。
        window_source = getattr(prov, "window_source", "未知")
        return {
            "estimated_tokens": bd["total"],
            "message_tokens": bd["messages"],
            "system_tokens": bd["system"],
            "tool_tokens": bd["tools"],
            "factor": bd["factor"],
            "context_window": window,
            "percentage": round(pct, 1),
            "threshold_pct": round(self.compressor._threshold * 100, 1),
            "threshold_tokens": int(window * self.compressor._threshold),
            "armed": self.compressor.is_armed(bd["total"], window),
            "window_source": window_source,
        }

    @property
    def last_compression(self) -> dict | None:
        """Stats from the most recent compression run, if any."""
        return self.compressor.last_compression

    @property
    def last_skip_note(self) -> str:
        """上一次没压成的真实原因（一句人话；没这回事，空串）。"""
        return self.compressor.last_skip_note

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close all persistent stores."""
        self.short_term.close()

    def stats(self) -> Dict:
        """Return memory system statistics for diagnostics.

        长期记忆的读数在 `memory_store.build_memory_index()` 的输出里（条目数）。
        """
        return {
            "short_term_messages": self.short_term.get_token_total(
                list(self._token_total.keys())[0]
            ) if self._token_total else 0,
            "sessions": len(self._token_total),
        }
