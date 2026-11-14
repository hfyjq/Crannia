"""
AgentLoopStage —— 核心的 LLM 交互。

用 PreProcessStage 准备好的上下文调用 `core.agent_loop.agent_loop()`
（或其流式版本）。

同步与流式两条路必须锁步：给一条加的功能，另一条也要加。
"""

from __future__ import annotations

import time

from core.agent_loop import (
    agent_loop, agent_loop_stream, answer_pending_call, deny_pending_call,
    replay_pending_call,
    # 判断"这是不是一个待审批载荷"只有一个入口
    # （`_parse_approval_payload`：绝不退回子串检查）。
    _parse_approval_payload,
    # 「用户打断」那句回执的唯一文案常量 —— 本轮是不是被打断收尾的，
    # 判据只能是它（前端拿它决定"这句不进对话/下一句到达时撤掉"）。
    CANCEL_REPLY,
)
# 消息 `content` 的纯文本视图（全仓唯一转换点）。
from core.image_input import content_text
from core.approval import ASK_COUNTED_KINDS, new_request_id, outcome_line
from core.prompt_assembler import assemble_system_prompt, model_name_of
from core.events import (
    EVT_STEP_END, EVT_STEP_START, EVT_TOOL_NEUTRALIZED, make_emitter,
)
from core.session_key import session_key_of_ctx
from core.session_date import date_change_note
from core.pipeline.stage import Stage, StageContext, register_stage


def _stream_enabled() -> bool:
    """逐字输出（流式）开不开？读设置，默认关。

    默认关不是保守：对逐字输出的评价本来就两派 —— 有人想看着活在进行，
    有人嫌闪烁分心，做成开关，不替用户决定。

    读取遵循通用的设置读取形状（每次现读、坏文件不崩）：文件不存在 /
    内容损坏 / 键缺失，返回默认值；一段对话绝不能因此失败。
    """
    import json
    from pathlib import Path
    try:
        p = Path("data/settings.json")
        if p.exists():
            return bool(json.loads(p.read_text(encoding="utf-8")).get("stream_output", False))
    except Exception:
        pass
    return False


@register_stage
class AgentLoopStage(Stage):
    """运行主 agent 循环（LLM + 工具调用）。"""

    async def process(self, ctx: StageContext) -> StageContext:
        # 无主理人群聊的发言已经由 `GroupReplyStage` 处理（它自己调每个
        #    成员的 agent 循环）。这里原地返回，但不 `stop` ——
        #    `PostProcessStage` 仍要照常落库/压缩。
        #    判据只看这一格：`GroupReplyStage` 会跳过审批重放轮，所以这里
        #    不会吞掉重放。
        if ctx.metadata.get("group_reply_done"):
            return ctx

        # 把这一段的工作目录钉进任务上下文
        #
        # 为什么在这里、不在调度器入口：`ctx.storage_id` 是在
        #    `PreProcessStage` 里赋值的（新对话和 `/clear` 都在那里铸新 id）
        #提前钉只会钉到默认工作区，症状是"新对话选的目录，第一条消息不
        #    生效"。
        # 消费者在 `skills/**`（`primary_output_dir()`），而工具处理器的签名
        #    是 `handler(tool_args)` —— 它看不到 `ctx`，值必须通过上下文**
        #    传递。
        # 只设置、不重置：一轮 = 一个任务 = 一个上下文（见
        #    `core/session_workspace.pin`）。
        # 绝不能弄挂一轮 —— 钉不上就意味着"用默认工作区"，那是正常行为。
        try:
            from core import session_workspace
            session_workspace.pin(ctx.storage_id)
        except Exception as _ws_exc:  # noqa: BLE001
            print(f"[workspace] 本轮工作目录钉不上（回默认）："
                  f"{type(_ws_exc).__name__}: {_ws_exc}")

        # 权限解禁：完全权限 + 设置里那个开关，本轮把剩下的阻力也拿掉
        #   钉在这里、与工作目录同一处：一轮 = 一个任务 = 一个上下文。
        #   工具 handler 拿不到 `session_id`，它们问的是 `unlock.bypassing()`
        #      （`deny` 档 / `FORBIDDEN` 路径 / 批量删除熔断那三处）。
        try:
            from core import unlock
            unlock.pin(ctx.session_id)
        except Exception as _ul_exc:  # noqa: BLE001
            print(f"[unlock] 解禁状态钉不上（本轮按**不**解禁处理）："
                  f"{type(_ul_exc).__name__}: {_ul_exc}")

        # ── 这一轮的所有代价旋钮都来自投入档
        #    （冻结在对话上，一轮内不变）──
        #
        # ① `tool_result_max_chars`：生产值从 `config.yaml` 读，是 6000。
        #    `2000` 是生产从不使用的兜底默认值；照它接线会让每个
        #    用户的上限静默掉到 2000。
        #    规则：读配置项时先问生产从哪里读 —— 不要接函数默认参数。
        # ② `max_turns`：`None` = 不设限（`spec.max_turns`）。固定一个数
        #    没有依据：它从未被重新评估过，而一轮要跑多久取决于任务，不取决于我们。
        from core.tiers import current_spec

        spec = current_spec(ctx.session_id)
        tool_result_max_chars = spec.tool_result_max_chars
        # `config.yaml` 里的 `tool_result_max_chars` 不生效 —— 要说一声，
        # 免得有人改了却看不到变化（静默忽略一个用户改过的键，比拒绝更坏）。
        _cfg_cap = ctx.config.get("tool_result_max_chars")
        if _cfg_cap is not None and int(_cfg_cap) != tool_result_max_chars:
            print(f"[tier] config.yaml 的 tool_result_max_chars={_cfg_cap} **不再生效** —— "
                  f"现在由档位给（档 {spec.level} 是 {tool_result_max_chars}）")

        max_turns = spec.max_turns
        # 流式开关
        #
        # 硬编码为 `False` 并注明"DeepSeek 流式在工具调用时会挂住"，这个
        # 归因是错的。真正的根因在流式累积器：缺失的 id 被归一化成 `''`，而新
        # 条目按 `len()` 创建，于是一次调用被拆成两条（`unknown tool ''`，看起来
        # 像挂住）。累积器已修并有测试覆盖，刹车也已镜像到流式路径（同一组
        # 场景在两条路上各跑一遍再比较）。
        #
        # 开关本身读设置（`data/settings.json` → `stream_output`，默认关）。
        supports_streaming = (
            _stream_enabled()
            and hasattr(ctx.provider, "chat_stream")
            and callable(ctx.provider.chat_stream)
        )

        # 滑窗冷闸（发送前的惰性检查）
        # 不在每轮轮末无条件裁剪，而是在发送前、且只在对话冷却时裁：从头丢会
        # 改写前缀、打断缓存。只有长时间空闲（默认 90 分钟）之后，前缀缓存本来
        # 就已经失效，裁剪才不额外付这份代价（见
        # `MemoryManager.maybe_trim_cold_window`）。
        # 放在分支之前 —— 同步与流式两条路都要过同一道闸（它们共用这
        #    一段，而不是各有一份）。
        # 安全网绝不能弄挂一轮：任何异常都只意味着"这次不裁"。
        try:
            ctx.messages = ctx.session_pool.maybe_trim_cold_window(
                session_key_of_ctx(ctx), ctx.messages)
        except Exception as _win_exc:  # noqa: BLE001
            print(f"[memory] 冷闸滑窗检查失败（本轮不剪）："
                  f"{type(_win_exc).__name__}: {_win_exc}")

        # 日期变化时往消息流补一条
        #
        # system prompt 里的日期行每请求重算，所以模型随时读得到今天是几号。
        # 这一段记录的是另一件事："上一轮与这一轮之间日期变了"。
        #
        # 走消息流（追加在尾部、随历史落库），而不是改写前缀，前缀
        #    只增长。
        # 不是每轮都发 —— 一个对话一天一次（第一次调用只记录，不发送）。
        # 放在分支之前 —— 同步与流式两条路都要过（它是公共段，不是
        #    每条路各一份）。
        # 安全网绝不能弄挂一轮：任何异常都只意味着"这次不补"。
        try:
            _date_note = date_change_note(ctx.session_id)
            if _date_note:
                ctx.messages.append({"role": "user", "content": _date_note,
                                     "kind": "date_change"})
                print(f"[date] 跨零点 —— 已往消息流补一条（{ctx.session_id}）")
        except Exception as _date_exc:  # noqa: BLE001
            print(f"[date] 日期检查失败（本轮不补）："
                  f"{type(_date_exc).__name__}: {_date_exc}")

        # 审批回答 = 把那次挂起调用的结果补上
        #
        # 判据：同意 = 零消息。结算器（`pre_process._settle_*`）已经发好
        # 授权，并把"哪一次调用"记在 `ctx.approval_replay`；这里把调用补完
        # —— 重放、把真实结果就地替换进那条 `role=tool` 消息，然后带着
        # `user_message=None` 进入循环（这样 `agent_loop` 不会追加用户消息）。
        # 模型看到的是：我那次调用刚刚返回了。
        #
        # 放在冷闸之后：裁剪之后，要补的那条消息可能已经不在了 —— 那种
        #    情况必须走退路回执，而不是对着一条过期消息硬补。
        # 只替换既有消息的内容，长度不变，下面的 `_msg_count_before`
        #    仍然成立（它是"本轮新增"的界线）。
        replay = getattr(ctx, "approval_replay", None)
        if replay:
            ctx.approval_replay = {}
            _replay_ok = False
            try:
                if replay.get("result") is not None:
                    # 提问的回答 —— 它不是拒绝，也不重放：
                    # 用户那句话就是结果。直接补回那次调用（与 deny 同一套
                    # 消息替换机制，但语义不同，键也不同 —— 绝不让"答案"
                    # 顶着 `deny` 的名字走）。
                    _replay_ok = answer_pending_call(
                        ctx.messages,
                        call_id=str(replay.get("call_id") or ""),
                        message=str(replay["result"]))
                elif replay.get("deny"):
                    _replay_ok = deny_pending_call(
                        ctx.messages,
                        call_id=str(replay.get("call_id") or ""),
                        message=str(replay["deny"]))
                else:
                    _replay_ok = replay_pending_call(
                        ctx.messages,
                        call_id=str(replay.get("call_id") or ""),
                        tool_name=str(replay.get("name") or ""),
                        raw_args=replay.get("arguments") or "",
                        session_id=ctx.session_id,
                        max_chars=tool_result_max_chars,
                        _emit=make_emitter(ctx.events, session_key_of_ctx(ctx)),
                    )
            except Exception as _rp_exc:  # noqa: BLE001
                print(f"[approval] 重放失败（退回旧回执）："
                      f"{type(_rp_exc).__name__}: {_rp_exc}")
                _replay_ok = False
            # 退路必须保留：补不成时（旧对话 / 消息被压缩掉了），退回
            #   原来那句"请重新提交"的回执 —— 否则就是**用户批了却什么都没
            #   发生**，而这正是这套设计要消灭的静默。
            if _replay_ok:
                # 被改动的旧消息必须整表重写才能落库（见
                # `_persist_replay`）。
                _persist_replay(ctx)
                ctx.user_message = None
            else:
                ctx.user_message = str(replay.get("fallback") or "")

        #:  超长自救的回调 —— 压缩动作在这里，事件在 `agent_loop` 里发
        #:   （一处来源；两处都发会让界面收到两份 `compact.progress`）。
        #:   判据：收到「请求超长」，强制压一次，由循环原地重发这一轮。
        #:   返回 `True` 才重发；压不动 / 失败就照旧把异常抛上去
        #:   （用户看到的是 `core/llm_errors.explain` 那段人话）。
        def _on_overflow(_msgs: list) -> bool:
            pool = getattr(ctx, "session_pool", None)
            if pool is None:
                return False
            try:
                compacted, done = pool.compact(session_key_of_ctx(ctx), _msgs)
            except Exception as _cexc:                       # noqa: BLE001
                print(f"[compact] 超长自救失败（压缩抛错）："
                      f"{type(_cexc).__name__}: {_cexc}")
                return False
            if not done:
                return False
            #: 原地替换：调用方（`agent_loop`）拿着同一个 list 继续跑。
            _msgs[:] = compacted
            ctx.metadata["compressed"] = True
            return True

        #: 压力/主动请求用的压缩回调（`core/context_pressure` 的 95% 与
        #:   `request_compaction` 都走它）。与超长自救复用同一个动作：
        #:   压一次、原地替换、本轮继续。
        #:   返回新的列表（`None` = 没压成）；循环那边用 `messages[:] = …` 收。
        def _compact_now() -> list | None:
            ok = _on_overflow(ctx.messages)
            return ctx.messages if ok else None

        _msg_count_before = len(ctx.messages)
        if supports_streaming:
            # 一行日志，说明这一轮走的是哪条路。
            # 没有它，两条路在日志里长得一模一样，"流式开着却看不到逐字输出"
            # 只能靠间接证据推断。一行能说清的事，不许留着不出声。
            print("[stream] 本轮走流式（settings.stream_output=true）")
            # 走流式路径 —— 累积所有产出的状态，以最后一个作为结果
            final_state = None
            async for state in agent_loop_stream(
                user_message=ctx.user_message,
                # 这一轮的图片块（空 = 没图）。
                # 与同步路径同名同义（锁步）。
                images=ctx.user_images,
                provider=ctx.provider,
                messages=ctx.messages,
                session_chunks=[],
                memory_context=ctx.memory_context,
                tool_result_max_chars=tool_result_max_chars,
                max_turns=max_turns,
                session_id=ctx.session_id,
                # 这一轮来自哪个渠道（`platform:type:id`）—— 周期任务建
                # 作业时需要它（`request_snapshot.current_channel()`）。
                # 它不是会话键：会话键有意忽略 `platform`。
                # 用有名字的辅助函数（不是 `str(ctx.session)`）—— 这里读的
                # 是渠道键；自己推会话键的代码会被源级读者抓到。
                channel=_channel_of_ctx(ctx),
                # 这一轮是谁起的（`cron`，本轮禁用审批）。
                _origin=_origin_of_ctx(ctx),
                # 与同步路径同一个出口：事件（含流式预览）从这里出去，所以
                # `agent_loop_stream` 仍然不知道事件总线的存在。
                _emit=make_emitter(ctx.events, session_key_of_ctx(ctx)),
                thinking=spec.thinking,      # 与同步路径相同
                #: 命令/技能声明的工具白名单（None = 不限制）。与同步路径同义。
                _tool_allowlist=ctx.tool_allowlist,
                #: 与同步路径同一条判据（锁步）：超长时压一次再重发。
                _on_overflow=_on_overflow,
                #: 压力/主动请求的压缩口（与另一条路同名同义）。
                compact_cb=_compact_now,
            ):
                final_state = state

            if final_state:
                ctx.response = final_state[0]
                ctx.messages = final_state[1]
                ctx.tokens_used = final_state[3]
            else:
                ctx.response = "(no response)"
            response, updated_messages, _, _ = (ctx.response, ctx.messages, [], ctx.tokens_used)
        else:
            # Sync path (backward compatible)
            _msg_count_before = len(ctx.messages)
            response, updated_messages, session_chunks, tokens_used = agent_loop(
                user_message=ctx.user_message,
                # 与流式路径同名同义（锁步）。
                images=ctx.user_images,
                provider=ctx.provider,
                messages=ctx.messages,
                session_chunks=[],
                memory_context=ctx.memory_context,
                tool_result_max_chars=tool_result_max_chars,
                max_turns=max_turns,
                session_id=ctx.session_id,
                # 与流式路径同义（锁步）。
                channel=_channel_of_ctx(ctx),
                _origin=_origin_of_ctx(ctx),
                # 把 (bus, session) 绑成一个回调 —— `agent_loop` 因此不需要
                # 知道事件总线的存在。
                _emit=make_emitter(ctx.events, session_key_of_ctx(ctx)),
                # 思考模式只在这条路上给 —— 它是会话冻结档的一部分
                # （会进缓存键；见 `providers/base.py::chat`）。
                # 另外两处 `agent_loop` 调用（计划分步 · 收尾摘要）
                # 故意不传：它们是辅助调用，保持默认/关闭思考；
                # 否则一个"更聪明"的档会让每次标题生成都更贵。
                thinking=spec.thinking,
                #: 命令/技能声明的工具白名单（None = 不限制）。
                _tool_allowlist=ctx.tool_allowlist,
                #: 与流式路径同一条判据（锁步）。
                _on_overflow=_on_overflow,
                #: 压力/主动请求的压缩口（与另一条路同名同义）。
                compact_cb=_compact_now,
            )

            ctx.response = response
            ctx.messages = updated_messages
            ctx.tokens_used = tokens_used

        # 这一轮是不是"用户打断"收尾的？（两条路共用这一处判据）
        #
        #   为什么要有这一格：那句话是框架的渲染，不是模型说的话
        #   （它不进 `messages`、不落库 —— 见 `CANCEL_REPLY` 上面那段）。
        #   前端要据此做两件事：给它一个回执身份 · 用户发下一句时撤掉它。
        #   判据是常量本身（全仓唯一文案），不是"回复为空"也不是
        #      "用户按过停"—— 后者与"这一轮真的被打断"之间有竞态。
        if response == CANCEL_REPLY:
            ctx.metadata["cancelled"] = True

        # 上游错误不是正文。流式路径用进程内标记
        # （`core.llm_errors.MARK`）标出错误文本；在这里剥掉并写
        # `ctx.metadata["error"]`，于是：
        #   · 用户看到的是一段说人话、带下一步的说明（`explain`）；
        #   · `task.end.ok` 是 false（否则它会跟着"正常返回"一起报成
        #     true，而正文写着失败了）。
        # 标记只存在于这次内存跳转 —— 落库和展示的已经是剥好的文本。
        apply_llm_error_mark(ctx)

        # 这一轮调了几次工具（供 `task.end` 使用）。
        #   判据是一一对应：每条 `tool_call` 必须恰好有一条 `role=tool`
        #     回填（上游硬要求 —— 少一条就 400），数回填就是数调用。
        #   只读：不改任何状态，也不碰下面的收尾路径。
        ctx.metadata["tool_count"] = _count_tool_calls(
            ctx.messages, _msg_count_before)

        # 这一轮工具产出的图 / 音 / 视频（没有就不设键）。`task.end`
        #   与 `/api/chat` 都从这里取 —— 两条实时出口共用一个来源。落库
        #   那一份在 assistant 消息的 `artifacts` 字段里，刷新后由
        #   `/api/messages` 带回。
        _arts = _turn_artifacts(ctx.messages, _msg_count_before)
        if _arts:
            ctx.metadata["artifacts"] = _arts

        # 审批检测（两条路都走）
        # 它还会收尾没人会回答的 `pending_*` —— 返回被中和掉的下标
        # （那些没人会回答的条目；见 `_neutralize_unarmed_pending`）。
        _neutralized = _check_cross_zone_pending(ctx, _msg_count_before)
        if any(_i < _msg_count_before for _i in _neutralized):
            # 只有改动界线之前的旧消息才需要整表重写：本轮新增的消息由
            #   正常追加路径写出，而旧消息写不出去。
            # 整表 DELETE+INSERT 很贵，绝不放到每轮都走的路上。
            _persist_replay(ctx)


        return ctx


# 审批回答不新开一轮
#
# 它把挂起调用的结果补上（见 `core/agent_loop.py::replay_pending_call`）。
# 三个函数各管一环：
#   * `_persist_replay`    — 被修改的旧消息需要整表重写才能进库
#   * `_call_info_for`     — 从 `assistant.tool_calls` 里取出"哪一次调用"
#   * `_note_pending_call` — 把它记进 `pending_*` 载荷

def _persist_replay(ctx) -> None:
    """把被重放就地修改过的旧消息写回数据库。

    为什么常规落库不管用：`MemoryManager.record_turn` 写的是
    `new_msgs = messages[saved_count:]` —— 只追加。而重放修改的是一条
    已经落库的消息（`pending_*` 的内容被替换成真实结果），追加路径永远
    写不出去。

    不写的话，当轮看起来正常（`ctx.messages` 里是真实结果），但**下一轮会把
    旧载荷加载回来**，包括那些永远不会被回答的 pending 条目 —— 正是审批重放
    要消灭的东西。

    失败要报告，但不许弄挂这一轮：退化成"下一轮读回旧载荷"，比丢掉
    整轮便宜。
    """
    mm = getattr(ctx, "memory_manager", None)
    # 这里需要的是存储键（它写消息行），不是通道键。今天两者恰好相等，
    #    但必须读 `storage_id`：否则重放结果会落到一个当前线程看不见的 id
    #    上。
    if mm is None or not getattr(ctx, "storage_id", ""):
        return
    try:
        mm.repersist_messages(ctx.storage_id, ctx.messages)
    except Exception as exc:  # noqa: BLE001
        print(f"[approval] 重放结果落库失败（下一轮会读回旧载荷）："
              f"{type(exc).__name__}: {exc}")


def _call_info_for(msgs: list, tool_msg_index: int) -> tuple[str, str, str]:
    """`role=tool` 消息背后的调用：`(call_id, 工具名, 原始参数)`。

    找不到 → `("", "", "")`（重放据此退回原回执，而不是静默什么都不做）。
    """
    try:
        call_id = str(msgs[tool_msg_index].get("tool_call_id") or "")
        if not call_id:
            return "", "", ""
        # 一对通常相邻，但历史里可能有连续的 `assistant` 消息，往前扫描更稳。
        for j in range(tool_msg_index - 1, -1, -1):
            m = msgs[j]
            if m.get("role") != "assistant":
                continue
            for tc in (m.get("tool_calls") or []):
                if str(tc.get("id") or "") == call_id:
                    fn = tc.get("function") or {}
                    return (call_id, str(fn.get("name") or ""),
                            fn.get("arguments") or "")
        return call_id, "", ""
    except Exception:
        return "", "", ""


def apply_llm_error_mark(ctx) -> bool:
    """剥掉流式路径加的进程内错误标记。返回这一轮是不是错误。

    标记本身绝不给用户看 —— 它只存在于从 `agent_loop_stream` 进入本
      阶段的这一跳；落库和展示的都是干净的人话
      （`core/llm_errors.explain`）。
    写 `ctx.metadata["error"]` 是承重的：`task.end.ok` 读它。
    """
    try:
        from core.llm_errors import is_marked, strip_mark
        if not is_marked(getattr(ctx, "response", "")):
            return False
        ctx.response = strip_mark(ctx.response)
        ctx.metadata["error"] = ctx.response
        ctx.metadata["llm_error"] = True
        return True
    except Exception as exc:                                    # noqa: BLE001
        print(f"[llm] 错误标记处理失败（{type(exc).__name__}: {exc}）"
              f" —— 这一轮按普通回复继续")
        return False


def _note_pending_call(pend: dict, msgs: list, tool_msg_index: int) -> None:
    """把"哪一次调用"写进 `pend` —— 重放读它。"""
    call_id, tool_name, raw_args = _call_info_for(msgs, tool_msg_index)
    if call_id and tool_name:
        pend["call_id"] = call_id
        pend["tool_name"] = tool_name
        pend["arguments"] = raw_args


#: `ctx` 上装"正在挂起"的那几格。与 `pre_process._APPROVAL_KINDS` /
#: `scheduler._PENDING_STORES` 是同一个集合（后两张表分别用于结算与持久化）。
_PENDING_STORE_ATTRS = (
    "pending_cross_zone", "pending_delete", "pending_delete_bulk",
    "pending_egress", "pending_command", "pending_destructive", "pending_plan",
    # 提问面板（也由"没人会回答"的收尾逻辑覆盖）。
    "pending_ask",
    # 工作区之外写文件。
    "pending_write_outside",
    # 接管的逐应用授权（同样由那套收尾逻辑覆盖）。
    "pending_computer_app",
    # 钩子要求人工确认（同样由那套收尾逻辑覆盖）。
    "pending_hook_ask",
)

#: 替换**没人会回答的 `pending_*`** 的那句话。
#:
#: 它不是协议载荷 —— "不许包裹、不许截断"保护的是仍在流转、等待结算
#: 的载荷。这一条已经没有结算的那一天，所以它是普通文本：不包裹，也不怕
#: 被截断（它很短）。
#: 措辞必须说清三件事：这次调用没有执行 · 不会有人再来回答它 ·
#: 如果还要做，就重新发起。
#:
#: 它的语义是 `cancelled`（"被打断，不是拒绝"），不是
#: `unavailable`（"没有可问的通道"）：撤下它的原因在这里并不唯一，所以文本
#: 如实说明，而不是断言一个原因。`cancelled` / `rejected` 两种头让它与
#: "用户说了不行"保持可区分。
#: `[框架] ` 前缀是这条通道的既有约定（与 `core/session_date.py` 相同），
#: 保留 —— 它与 `outcome_line` 的头正交（一个说谁在说话，一个说为什么
#: 没成）。
NEUTRALIZED_PENDING_NOTE = "[框架] " + outcome_line(
    "cancelled",
    "**这一次调用没有执行，也不会有人再来回答它** —— 它在框架这边被撤下了。"
    "★ 撤它的原因**不止一种**（用户给了别的话 / 同一轮里另一条调用占着那条通道）"
    "⇒ **别去猜是哪种**；唯一确定的是：**它不是「用户拒绝了」**。"
    "⇒ 若这件事仍然要做，请按用户**最新**的那条指示**重新发起**。"
)


def _live_pending_call_ids(ctx) -> set:
    """框架此刻真正握在手上的那些 `call_id` —— 它们才是真的在等回答。

    判据：`ctx.pending_*` 是"已武装"状态的唯一载体（本文件写它，
    `pre_process._dispatch_approval` 读它）。历史里任何不在这里的
    `pending_*` 都已经死了 —— 见 `_neutralize_unarmed_pending`。
    """
    ids = set()
    for attr in _PENDING_STORE_ATTRS:
        pend = getattr(ctx, attr, None)
        if isinstance(pend, dict) and pend.get("active") and pend.get("call_id"):
            ids.add(str(pend["call_id"]))
    return ids


def _neutralize_unarmed_pending(msgs: list, *, keep_index: int = -1,
                                live_call_ids=None) -> list:
    """把**没人会回答的 `pending_*`** 换成一句实话。

    返回被替换的下标（调用方据此决定要不要整表重写）。

    ## 它修什么

    一条 `pending_*` 只有一个含义："框架正拿着它，等用户回答"。而模型可以在
    一条 assistant 消息里发出多个 `tool_call`，其中两个都撞审批
    （`[run_command A, run_command B]`）：

      * 两条 `role=tool` 消息都进入历史；
      * 扫描是倒着走的、碰到第一条就停，只有最后一条（`B`）被武装；
      * 两个 `run_command` 共用 `ctx.pending_command` 一格，A 的身份被
        B 覆盖。

A 的 `pending_*` 留在历史里，永远没人回答：扫描只看"本轮新增"，
    而 A 的下标在界线之前，没有任何东西会再回来处理它。下一轮模型看到 A
    还在"等"，很可能再发一次，多弹一次浮窗。（模型无法绕过审批，所以
    "同一批两条挂起"是产生这种孤儿 pending 的唯一途径。）

    ## 判据：没被武装 = 已经死了

    界线画在"**它在不在 `ctx.pending_*` 里**"，而不是"它在不在本轮"：两者
    说的是同一件事，而前者还覆盖了陈旧那条 —— 用户在浮窗里打了别的话
    （"非数字 = 打断"那条路），那条 `pending_*` 同样被 `_drop_pending` 掉了，
    同样没人会回答它。

    `keep_index` 是安全网：当 `_note_pending_call` 取不到 `call_id`
    （assistant 消息被压缩掉 / 历史是拼出来的）时，正在被武装的那一条
    也会看起来"不在 live 集合里" —— 而它恰恰是唯一正确的一条，不能被
    中和掉。
    """
    if live_call_ids is None:
        live_call_ids = set()
    neutralized: list = []
    for i, m in enumerate(msgs):
        if i == keep_index or not isinstance(m, dict):
            continue
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        # 判据只有一个入口（`_parse_approval_payload`：真正解析出来的
        #   JSON 对象、且 `status` 以 `pending_` 开头）—— 子串检查会被
        #   `grep -rn pending_approval .` 这类命令输出打中。
        if _parse_approval_payload(content) is None:
            continue
        _cid = str(m.get("tool_call_id") or "")
        if _cid and _cid in live_call_ids:
            continue
        m["content"] = NEUTRALIZED_PENDING_NOTE
        neutralized.append(i)
    return neutralized


def _emit_neutralized(ctx, msgs: list, idxs: list) -> None:
    """把"哪几次调用被中和了"发到事件流。

    必须出声：否则只有模型看得到 —— 界面上只有一张浮窗，而"另一件事
    没执行"只能存在于模型选择要说的话里（它完全可能什么都不说）。

    能带工具名就带（`_call_info_for` 取得到）：只说"有一次调用"，
    用户不知道是哪一件事没做。

    时间点：它发生在轮内（`AgentLoopStage`，`task.end` 之前），
    前端把它渲染成当前活动块里的一行，并且不关闭它（收口者 `task.end`
    还没到）。这与在 `task.end` 之后到达的事件不同 —— 那些必须自己收口。

    永不抛：这是观测，不是功能。
    """
    try:
        bus = getattr(ctx, "events", None)
        if bus is None:
            return
        key = session_key_of_ctx(ctx)
        for i in idxs:
            _call_id, _name, _args = _call_info_for(msgs, i)
            bus.emit(EVT_TOOL_NEUTRALIZED, session_key=key,
                     tool=str(_name or ""))
    except Exception as exc:  # noqa: BLE001 — 观测不许弄挂这一轮
        print(f"[approval] 中和事件发不出去（{type(exc).__name__}: {exc}）")


def _check_cross_zone_pending(ctx: StageContext,
                              start_idx: int = 0) -> None:
    """认出真的停在待审批上的消息，并武装对应的 `ctx.pending_*`。

    只扫描 *start_idx* 之后新增的消息，避免对上一轮的历史 pending 重复触发。

    ## "只认最后一条工具结果"这条判据试过，已撤回

    它曾被改成"只有本轮最后一条 `role=tool` 消息才能武装"，理由是"这一轮
    有过 pending"不等于"这一轮停在等回答"。**那条判据造成过一次死锁，
    已撤回。**

    ### 实际发生了什么

    ```
    run_command -> ok                 ← 返回 pending
    say: "I need your approval to run a quick verification command."
    say: "The command is waiting for your approval. Meanwhile, …"
    create_document -> ok             ← 模型在 pending 之后又调了别的工具
    轮末：没有 `approval.request` 事件，浮窗永远不出现
    ```

模型在等，用户什么都看不到。

    ### 为什么那条判据做不到 —— 两个场景的机械形状完全相同

    | 场景 | pending 之后模型做了什么 | 模型说了什么 | 该不该弹窗 |
    |---|---|---|---|
    | A | 继续调别的工具，把任务做完 | "我改用内置工具，不需要审批" | 不该 |
    | B | 继续调别的工具，把报告写完 | "我需要你的批准" | 该 |

    机械形状完全一样（pending 之后还有工具调用，工作都完成了）；唯一
    的差别是模型说了什么 —— 那是语义，不是机械上可判断的。

    ### 因此：扫描新增的全部范围，碰到 pending 就武装

    判据（fail-open 的方向是武装）：
    * 不武装的代价是死锁（模型在等，用户看不见）—— 上面那个场景；
    * 武装的代价是多弹一次浮窗（命令已经作废；用户看到 `subject` 就可以
      不理它）—— 而且"武装会顶掉模型回复"那一半在 Web 上已经去掉了。

两害相权：宁可多弹一次浮窗，也绝不让模型在没人看得见的地方等。
    （判断不了的判据，不能拿来做闸。）

    已知残余：如果模型真的绕过了审批，浮窗仍会出现一次 —— 就是上表
    那两种情况。

    ## 返回值

    返回**被中和的 `pending_*` 下标**（没人会回答的那些；见
    `_neutralize_unarmed_pending`）。调用方据此决定要不要整表重写 —— 落在
    本轮界线之前的条目无法由正常的追加路径写出。
    """
    import json as _json
    msgs = ctx.messages
    if not msgs:
        return []
    #: 本轮被武装的那一条的下标（`-1` = 没有挂起任何东西）。
    _armed_idx = -1
    # 改为按下标倒着走（而不是遍历 `reversed(turn)`），这样才能顺手
    # 记下"这次挂起对应哪一次调用"（`_note_pending_call` 必须从这条 `tool`
    # 消息往上找它的 `assistant.tool_calls`）。
    # 顺序与 `break`/`continue` 语义都没有变 —— 只是现在手上多了下标。
    for _i in range(len(msgs) - 1, start_idx - 1, -1):
        msg = msgs[_i]
        if msg.get("role") == "tool":
            # 走纯文本视图 —— 块列表没有 `.startswith`（那会抛
            #   `AttributeError`，而下面的 `except: continue` 会把它静默
            #   变成"这条不是审批载荷"）。今天带 `pending_` 的一定是 `str`，
            #   但"形状恰好对"不是依赖它的理由。
            _txt = content_text(msg.get("content"))
            try:
                data = _json.loads(_txt) if _txt.startswith("{") else {}
            except Exception:
                continue
            if data.get("status") == "pending_approval":
                ctx.pending_cross_zone["active"] = True
                ctx.pending_cross_zone["stage"] = "ask"
                ctx.pending_cross_zone["hint"] = data.get("file_hint", "")
                # 身份：浮窗靠它挡双击/双发/刷新后重放。每次发问都不同。
                ctx.pending_cross_zone["request_id"] = new_request_id("cross_zone")
                _note_pending_call(ctx.pending_cross_zone, msgs, _i)
                # 信号不在这里发：`self._pending_*` 还没持久化，前端
                #   立刻 GET 会读到空，浮窗永不出现。它在调度器轮末发。
                prompt = data.get("prompt", "")
                # Web 上什么都不写（旧代码写一行"……见浮窗。"）。
                #
                # 这一格装的是模型自己的回复文本 —— 在这里写会**整条替换
                # 掉回复**。观察到过：模型写了完整报告，而同一轮里还躺着一条
                # 它早已绕过去的陈旧 pending，轮末扫描把报告换成一行
                # 指路语，用户什么都没看到。
                #
                # 浮窗不依赖这句话：它从 `GET /api/approval` 背后的 state
                # 渲染，由 `approval.request` 事件触发。
                #Web 上什么都不写，把模型的文本留在原地。
                # 非 Web 必须保留 —— 那边没有浮窗，菜单只能印出来。
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                # 计数：这一轮问了一次
                # "问了几次"决定选项 5（"本对话内自动放行"）何时出现
                # （`core/approval.ESCALATE_AT`）。
                #
                # 数在这里，不在工具里，也不在 `core/agent_loop.py`：
                #   * 工具没有会话身份（只有 `wm` 投影），写不了真值载体
                #     `ctx`；
                #   * `core/agent_loop.py` 那里只有字符串，而这里 `data`
                #     已经是解析好的 dict，`kind` 可以直接取。
                # 不要改成"轮末从 `wm` 读回来"：那会把真值方向反过来
                # （投影 → 真值），而且 `/clear` 把 ctx 清成 0 之后，wm 的
                # 陈旧值会把它覆盖回去。
                if data.get("kind") in ASK_COUNTED_KINDS:
                    _k = str(data.get("kind"))
                    ctx.approval_ask_counts[_k] = int(
                        ctx.approval_ask_counts.get(_k, 0)) + 1
                _armed_idx = _i
                break
            # 出网审批与命令审批
            #
            # 同一个形状，两个域走一段代码 —— 这就是不另开一套流程的收益。
            # 被审批物在 `data["subject"]`（域名 / 收件人 / 命令），载体在
            # `ctx` 上。
            # 命令审批现在只有一档；按 `kind` 分派保留（如果一个格将来
            #   装两个 kind，它会重新派上用场）。
            if data.get("status") in ("pending_egress_approval",
                                      "pending_command_approval"):
                kind = str(data.get("kind") or "")
                if kind not in ("egress", "command"):
                    continue
                pend = (ctx.pending_egress if kind == "egress"
                        else ctx.pending_command)
                pend["active"] = True
                pend["stage"] = "ask"
                pend["kind"] = kind
                pend["subject"] = data.get("subject", "")
                pend["detail"] = data.get("detail", "")
                # 浮窗的正文：原样传递工具已经拼好的那份，**绝不在
                #   浮窗侧重新拼**。
                pend["body"] = data.get("body", "")
                # 选项 2 会记成哪条规则 —— 从载荷原样带下来；浮窗
                #    （`build_payload`）用它把选项 2 渲染成那条规则。
                #    只有命令域非空；出网没有这个概念（空串）。
                pend["rule"] = data.get("rule", "")
                pend["request_id"] = new_request_id(kind)
                _note_pending_call(pend, msgs, _i)
                prompt = data.get("prompt", "")
                # 同上：web 不占模型的格子（浮窗自己会弹）。
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                if kind in ASK_COUNTED_KINDS:
                    ctx.approval_ask_counts[kind] = int(
                        ctx.approval_ask_counts.get(kind, 0)) + 1
                _armed_idx = _i
                break
            # Delete approval
            if data.get("status") == "pending_delete_approval":
                # 两个 kind 各有自己的格（`delete` / `delete_bulk`）。
                #
                # 有意禁止共用一格：共用一个格但选项表不同，会同时造成
                #    三个症状 —— 浮窗永不出现、失效信号带错 kind、**"拒绝"被
                #    读成"同意"**。只要两个 kind 共用一个格，这个坑就在。
                #
                # 公开的 kind 永远取自载荷（`pend["kind"]`）—— 格的键
                #    与公开 kind 是两件事，渲染时绝不能用格的键。
                _dk = str(data.get("approval_kind") or "delete")
                _slot = (ctx.pending_delete_bulk if _dk == "delete_bulk"
                         else ctx.pending_delete)
                _slot["active"] = True
                _slot["stage"] = "ask"
                _slot["file_paths"] = data.get("file_paths", [])
                # 被审批物（这次删除覆盖哪些文件）—— 结算器用它发
                #    "这一次"的通行证，工具的闸也问同一个问题。
                _slot["subject"] = data.get("subject", "")
                # 携带工具已经排好版、折叠好的列表。浮窗正文就该是它
                # —— 让浮窗从 `file_paths` 重新拼会开第二个渲染源（两条通道
                # 会看起来不一样）。
                _slot["file_list"] = data.get("file_list", "")
                _slot["kind"] = _dk
                _slot["request_id"] = new_request_id(_dk)
                _note_pending_call(_slot, msgs, _i)
                prompt = data.get("prompt", "")
                # 同上：web 不占模型的格子（浮窗自己会弹）。
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                # NOTE：这里不要再把 `ctx.session_skip_delete` 镜像到
                # `wm.skip_delete_approval`。那个全局现在是每轮投影，且只有
                # 一个写手（`PipelineScheduler._project_session_flags`）；
                # 用户答"2"的那一轮里，重新提交由一次性授权覆盖。两个写手正是
                # 这个标志变成进程级、而且没人能重置的原因。
                _armed_idx = _i
                break
            # 兜底闸
            #
            # 与出网/命令同形（被审批物是 `data["subject"]` 里的工具名），
            # 走的也是同一套派发/结算代码。
            # 判据的来源不同：那两条问"这个目标批过没有"，这一条问
            #   "这个工具有没有自己的审批发射点"。
            if data.get("status") == "pending_destructive_approval":
                pend = ctx.pending_destructive
                pend["active"] = True
                pend["stage"] = "ask"
                pend["kind"] = "destructive"
                pend["subject"] = data.get("subject", "")
                pend["detail"] = data.get("detail", "")
                pend["body"] = data.get("body", "")
                pend["request_id"] = new_request_id("destructive")
                _note_pending_call(pend, msgs, _i)
                prompt = data.get("prompt", "")
                # 同上：web 不占模型的格子（浮窗自己会弹）。
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                if "destructive" in ASK_COUNTED_KINDS:
                    ctx.approval_ask_counts["destructive"] = int(
                        ctx.approval_ask_counts.get("destructive", 0)) + 1
                _armed_idx = _i
                break
            # 工作区之外写文件
            #
            # 与出网/命令同形（被审批物是 `data["subject"]` 里的目录），
            # 走同一套派发/结算代码（`_settle_target`）。
            # `status` 是新的字符串 —— 上面的跨区分支是精确比较
            #    （`== "pending_approval"`），写重了会被它先接住。
            # 这不是"选定目录，免问"那条路：能走到这里的一定既不是
            #    这一段选定的目录，也不在工作区列表里（那一半是
            #    `core/security.py::_is_session_workspace` 的免问旁路）。
            if data.get("status") == "pending_write_outside_approval":
                pend = ctx.pending_write_outside
                pend["active"] = True
                pend["stage"] = "ask"
                pend["kind"] = "write_outside"
                pend["subject"] = data.get("subject", "")
                pend["detail"] = data.get("detail", "")
                # 与出网/命令相同：携带产者已拼好的正文，绝不在浮窗侧
                #   重新拼。这里的产者是框架那侧。
                pend["body"] = data.get("body", "")
                #: 这个 kind 没有前缀规则（会话授权只记"这个目录"）——
                #:   显式写空串，让读者知道这里没有规则。
                pend["rule"] = data.get("rule", "")
                pend["request_id"] = new_request_id("write_outside")
                _note_pending_call(pend, msgs, _i)
                prompt = data.get("prompt", "")
                # 同其余六条：web 不占模型的格子（浮窗自己会弹）。
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                if "write_outside" in ASK_COUNTED_KINDS:
                    ctx.approval_ask_counts["write_outside"] = int(
                        ctx.approval_ask_counts.get("write_outside", 0)) + 1
                _armed_idx = _i
                break
            # 接管的逐应用授权
            #
            # 与审批同形（"同意"，重放工具，让它真的执行），但授权真值不在
            # `ctx.target_grants` —— 它在接管会话里（见
            # `_settle_computer_app`）。
            # `status` 是新的字符串；不要复用 `pending_approval`（那个分支
            #   是精确比较，会先接住它）。
            if data.get("status") == "pending_computer_app_approval":
                pend = ctx.pending_computer_app
                pend["active"] = True
                pend["stage"] = "ask"
                pend["kind"] = "computer_app"
                pend["subject"] = data.get("subject", "")
                pend["app"] = data.get("app", "")
                pend["exe"] = data.get("exe", "")
                pend["body"] = data.get("body", "")
                pend["rule"] = ""
                pend["request_id"] = new_request_id("computer_app")
                _note_pending_call(pend, msgs, _i)
                prompt = data.get("prompt", "")
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                _armed_idx = _i
                break
            # 提问面板
            #
            # 与计划批准同形（都是问人），但语义相反：审批 = 求授权
            # （同意之后重放工具让它真的执行）；提问 = 问意见（答案就是
            # 那次调用的结果，不重放）。
            # `status` 是新的字符串 —— 上面的跨区分支是精确比较
            #    （`== "pending_approval"`），写重了会被它先接住。
            if data.get("status") == "pending_user_question":
                pend = ctx.pending_ask
                pend["active"] = True
                pend["stage"] = "ask"
                pend["kind"] = "ask_user"
                # 选项表随载荷走（它是动态的；这个 kind 在 `_TABLES`
                #   里的条目是空的）。
                pend["options"] = list(data.get("options") or [])
                pend["multiSelect"] = bool(data.get("multiSelect"))
                pend["question"] = str(data.get("question") or "")
                pend["header"] = str(data.get("header") or "")
                # 正文/提示语由 `core/ask_user.py` 拼好带下来 —— 绝不在浮窗
                #   侧重拼（同一个问题会在两条通道上渲染出不同样子）。
                pend["body"] = data.get("body", "")
                pend["request_id"] = new_request_id("ask_user")
                _note_pending_call(pend, msgs, _i)
                prompt = data.get("prompt", "")
                # 同其余几条：web 不占模型的格子（浮窗自己会弹）。
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                _armed_idx = _i
                break
            # 钩子要求人工确认
            #
            # 与计划批准同形（都是问人），但结算最省：不发任何授权
            # —— 重放直调工具 handler，`PreToolUse` 不会再跑（见
            # `pre_process._settle_hook_ask`）。
            # `status` 是新的字符串 —— 上面的跨区分支是精确比较
            #    （`== "pending_approval"`），写重了会被它先接住。
            if data.get("status") == "pending_hook_ask_approval":
                pend = ctx.pending_hook_ask
                pend["active"] = True
                pend["stage"] = "ask"
                pend["kind"] = "hook_ask"
                pend["subject"] = data.get("subject", "")
                pend["tool_name_hint"] = data.get("tool", "")
                pend["body"] = data.get("body", "")
                pend["request_id"] = new_request_id("hook_ask")
                _note_pending_call(pend, msgs, _i)
                # 钩子同时给了 `updatedInput` 时，重放要用改过的参数
                #   （载荷里带的是 JSON 串；`call_info_for` 拿到的是模型原来
                #   那份，不覆盖就会"用户看着 A 批准、实际跑的是 B"）。
                _uargs = data.get("arguments")
                if isinstance(_uargs, str) and _uargs.strip():
                    pend["arguments"] = _uargs
                prompt = data.get("prompt", "")
                # 同其余几条：web 不占模型的格子（浮窗自己会弹）。
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                _armed_idx = _i
                break
            # 计划批准
            #
            # 被审批物是计划本身（`data["subject"]` = slug）。与上面五条
            # 最大的不同：结算器翻的是模式（退出计划模式），不是按目标的
            # 授权。
            # `status` 是新的字符串；不要复用 `pending_approval` ——
            #    那个分支是精确比较（`==`），会先接住它。
            if data.get("status") == "pending_plan_approval":
                pend = ctx.pending_plan
                pend["active"] = True
                pend["stage"] = "ask"
                pend["kind"] = "plan"
                pend["subject"] = data.get("subject", "")
                pend["body"] = data.get("body", "")
                pend["request_id"] = new_request_id("plan")
                _note_pending_call(pend, msgs, _i)
                prompt = data.get("prompt", "")
                # 同其余五条：web 不占模型的格子（浮窗自己会弹）。
                if prompt and not _is_web(ctx):
                    ctx.response = prompt
                _armed_idx = _i
                break

    # **没人会回答的 `pending_*` 必须收尾**
    #
    # 上面的扫描是倒着走的、碰到第一条就停，只有同一批里最后那条被
    # 武装；更早的那些（以及被"非数字 = 打断"丢掉的陈旧那条）**没有任何东西
    # 会回来处理**，却仍在历史里假装"正在等回答"。就地换成实话（见
    # `_neutralize_unarmed_pending`）。
    #
    # 收尾之后才返回下标 —— 调用方据此决定要不要整表重写：只有改动
    #    界线之前的旧消息才需要。
    neutralized = _neutralize_unarmed_pending(
        msgs, keep_index=_armed_idx, live_call_ids=_live_pending_call_ids(ctx))
    # 每一次被中和的调用都应在界面上留一行 —— 否则只有模型看到的工具
    #   消息变了，用户侧没有任何信号。这条由框架发，不是模型的转述。
    # 在收尾之后发：被中和的下标要等 `_neutralize_unarmed_pending`
    #   跑完才存在。
    if neutralized:
        _emit_neutralized(ctx, msgs, neutralized)
    return neutralized


def _is_web(ctx) -> bool:
    """这条消息是不是从 Web UI 来的？只用来决定怎么渲染。"""
    try:
        return str(getattr(ctx.session, "platform", "") or "").lower() == "web"
    except Exception:
        return False


def _origin_of_ctx(ctx) -> str:
    """这一轮是谁起的（`""` / `"cron"`），从 `event.extras["origin"]` 读。

    它只有一个消费者：`agent_loop` 用它决定这一轮许不许开审批
    （`"cron"`，拒绝并给路线）。定时任务触发时没有人能回答审批，而挂起
    的代价是被冻住的桌面加一轮没有回复。
    """
    try:
        extras = getattr(getattr(ctx, "event", None), "extras", None) or {}
        return str(extras.get("origin") or "")
    except Exception:                                # noqa: BLE001
        return ""


def _channel_of_ctx(ctx) -> str:
    """这一轮来自哪个渠道（`platform:type:session_id`）。

    转发到 `core/im_routing.py::channel_key_of` —— 渠道键的唯一定义。
    这里不要写 `str(ctx.session)` / `str(getattr(...))`：源级读者会把
      两者判成"又自己推会话键了"，而且它抓得对 —— 两个键只差一个 `platform`，
      很容易写串。
    """
    try:
        from core.im_routing import channel_key_of
        return channel_key_of(getattr(ctx, "session", None))
    except Exception:                                # noqa: BLE001
        return ""


def _turn_artifacts(msgs: list, start: int) -> list:
    """这一轮挂在 assistant 消息上的产物 —— 只读。

    判据只住在一处（`core/artifacts.records_from_messages`）；群聊那条路
      也从同一个函数取。
    """
    from core import artifacts as _art
    return _art.records_from_messages(msgs, start)


def _count_tool_calls(msgs: list, start: int) -> int:
    """这一轮新增的 `role=tool` 消息数 = 工具调用数。

    为什么数回填就是数调用：每条 `tool_call` 必须有且只有一条
    `role=tool` 回填（上游硬要求 —— 少一条就 400），所以两者一一对应。

    只读；`msgs` 不是列表时返回 0（而不是抛异常）—— 这是观测，
    不许弄挂它观测的那一轮。
    """
    try:
        return sum(1 for m in msgs[start:]
                   if isinstance(m, dict) and m.get("role") == "tool")
    except Exception:
        return 0
