"""
PostProcessStage — runs after the LLM response.

Responsibilities:
  - Sanitize credential-like patterns from output
  - Persist the turn to memory (record_turn)
  - Collect context-usage stats for display
  - Future: content safety check, metrics, logging
"""

from __future__ import annotations

from core.session_key import session_key_of_ctx
from core.pipeline.stage import Stage, StageContext, register_stage


@register_stage
class PostProcessStage(Stage):
    """Sanitize output, persist conversation turn and collect stats."""

    async def process(self, ctx: StageContext) -> StageContext:
        # Redact credential patterns before persisting or forwarding
        if ctx.response:
            from core.security import SecurityGuard
            ctx.response = SecurityGuard.sanitize_output(ctx.response)

        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)

        user_msg = {"role": "user", "content": ctx.user_message}
        # 框架代笔的那条只打标记、不丢（模型装载还要它）；
        #    显示侧按这一个 kind 不收（`is_conversation_row`）。
        _u_kind = ctx.metadata.get("user_message_kind")
        if _u_kind:
            user_msg["kind"] = str(_u_kind)
        assistant_msg = {"role": "assistant", "content": ctx.response}
        # Preserve reasoning_content for DeepSeek thinking mode
        reasoning = ctx.metadata.get("reasoning_content")
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning

        # 这一轮落库后是哪两行。
        #    前端靠它把"内存里那条消息"对到"库里那一行"—— 聊天区那个
        #    要的就是它（收藏存的是引用 `stars.message_id`），
        #    没有它，行号只活在库里（`/api/chat` 只回
        #    `{reply,tokens,files}`、`task.end` 也没有 id）。
        #    拿不到就发 `None`，不发 `{}`（读不到 ≠ 空）——
        #    前端按"有没有值"决定要不要给那条消息点亮 。
        turn_rows: dict = {}
        compressed = pool.record_turn(
            session_key=session_key,
            user_message=user_msg,
            assistant_message=assistant_msg,
            token_count=ctx.tokens_used,
            messages=ctx.messages,
            rows=turn_rows,
        )
        ctx.metadata["turn_rows"] = turn_rows or None

        # 惰性冻结
        #
        # 「一旦已有任意记录，再改默认不再对他们生效」—— 冻结的时刻就是这里。
        #
        # 为什么是这一处（判据：只要模型有任意回复就算）：
        #    本阶段跑起来 ⟺ 整条管线走到了最后，**真的有一条 assistant 消息
        #    存下去了**。另外两条路都到不了这儿：
        #      · 命令轮（`/clear` `/compact` `/effort`…）在 pre_process 就 `stop`；
        #      · 出错轮在调度器里 `break`（`metadata["stop"]=True`）。
        #    别改成"用户发了一句话就冻" —— 那句"还没有任何回复"的对话
        #      在用户心里根本还没开始，粘住它等于让他改默认之后还得再开一段。
        #    也别用 `ctx.tokens_used > 0` 当判据：上游报不报 token 是
        #      另一件事（读不到不等于 0），拿它当判据就是拿一个
        #      可能为 0 的读数去决定一件必须发生的事。
        #
        # `freeze` 不覆盖 —— 用户显式选过的那条（`/model` 或界面）不会被
        #    这里的第二次调用冲掉（两个动词分开的理由见 `core/session_model.py`）。
        # 失败只说一声：它是一条记账，不该弄挂一轮已经跑完的对话
        #    （同 `log_turn` 那条判据）。
        try:
            if ctx.model_config_id:
                from core import session_model
                if session_model.freeze(session_key, ctx.model_config_id):
                    print(f"[session_model] 这一段冻结在配置 "
                          f"'{ctx.model_config_id}' 上 —— 之后改默认不再影响它")
        except Exception as exc:                                # noqa: BLE001
            print(f"[session_model] 冻结失败（这一段会跟随默认）："
                  f"{type(exc).__name__}: {exc}")

        if compressed is not None and compressed is not ctx.messages:
            ctx.messages = compressed
            ctx.metadata["compressed"] = True
            # 压缩之后要把计划模式的 FULL 重发一遍。
            #
            # 压缩会把这一段轮次挤出上下文（落在压缩边界之前），
            # 而计划模式是会话态（活在进程里），压完之后模式照常生效
            # （写工具照拦），而模型手里一条相关指令都没有。
            # 一个还在生效的约束，没有任何地方在说它 —— 最狠的形状。
            #
            # 只在这里立标记，不发提醒：压缩发生在轮末（`PostProcess`），
            # 而提醒挂在本轮的 payload 上 —— 那时它已经发出去了。
            #消费点是下一次请求的 `build_plan_reminder`（同一个挂载点，
            # 与 SPARSE 共用），读完即清。
            #
            # 用 `ctx.session_id`（通道键）—— 与 `build_plan_reminder` /
            # `plan_mode_active` 收到的那个键一致（`pre_process` 的 `/clear`
            # 拿的也是它）。
            # 不静默：这一步坏了不会有测试红，但它不阻断对话（判据同
            # `agent_loop` 那条 `[plan_reminder] 生成失败`）。
            try:
                from core.plan import note_compressed
                note_compressed(ctx.session_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[plan_reminder] 标记压缩失败（{type(exc).__name__}: {exc}）"
                      f" —— 这一段对话不再重发计划模式提醒")
        # Keep pool cache in sync with potentially compressed messages
        if session_key in pool._loaded:
            sid = pool._loaded[session_key][0]
            pool._loaded[session_key] = (sid, ctx.messages)

        ctx.metadata["context_usage"] = pool.get_context_usage(
            session_key, ctx.messages,
        )
        ctx.metadata["last_compression"] = pool.last_compression

        # 去重账本：走到这里才算"处理成功"
        # 判据是阶段走完—— 不是"模型答完"：
        # 回复发不出去和没答，对用户是同一件事。
        # 占位是在 `pre_process` 做的；失败那条路由调度器 `release()`
        # （`ctx.metadata["dedup_done"]` 是两边唯一的分岔）。
        _settle = (ctx.metadata or {}).get("dedup")
        if _settle:
            try:
                from core.dedup import default as _dedup_default
                _dedup_default().mark_done(**_settle)
                ctx.metadata["dedup_done"] = True
            except Exception as exc:  # noqa: BLE001
                # 不阻断对话，但不许静默：落不了账的后果是"重投时再答一遍"。
                print(f"[dedup] 落账失败（{type(exc).__name__}: {exc}）"
                      f" —— 这条消息重投时可能再答一遍")

        return ctx
