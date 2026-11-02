"""无主理人群聊的发言那一轮。

无主理人的群由成员直接发言（只限无主理人、非圆桌），
成员是可见的、各自带各自的名字与头像：

```
用户消息
  ├─ 有 @ 名字，只让被点名的成员回复（多个则并行）
  └─ 没有 @，一次小 LLM 路由（只看名册 role）选人
                 高置信，选中的成员回复
                 低置信 / 空 / 失败，全员回复（兜底）
```

每个被选中的成员直接跑一次 `agent_loop`，结果作为可见的群消息落库
（`kind="group_speaker"` + `speaker` + `avatar`），用户看到的是成员本人，
不是主持人转述。

## 四条边界

1. 有主理人的群不进来（`groups.host_of` 非空）—— 那条拓扑原样不动；
2. 圆桌不进来（`group["roundtable"]` 为真时留给后面的实现）；
3. 审批回答那轮不进来（`ctx.approval_replay` 非空）—— 让 `AgentLoopStage` 走既有的重放；
4. 命令轮不进来（`/...`）—— `pre_process` 已经处理并 `stop`，这里是双保险。

## 成本

- 路由只一次小调用：名册 + 上一轮摘要 + 当前消息，不塞全量历史；
- 成员回复是一次完整的 `agent_loop`（各自模型、各自工具）；
- 路由失败永远回退全员 —— 不许因为路由器坏了让用户没回复。
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

from core.pipeline.stage import Stage, StageContext, register_stage


def _log(msg: str) -> None:
    # 一律 ASCII 箭头 / 括号：GBK 控制台不许被一个字符弄崩。
    print("[group] " + msg)


#: 上 wire 的字段白名单。 `kind` / `speaker` / `avatar` 是我们给界面看的，
#: 不是给上游 API 的 —— 严格一点的 OpenAI 兼容端点会对未知消息字段报错。
#: `tool_calls` / `tool_call_id` / `reasoning_content` 必须留（它们是协议/思考回传）。
_WIRE_FIELDS = frozenset({
    "role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content",
})


def _wire_messages(messages: list[dict]) -> list[dict]:
    """历史副本 → 只带协议字段（不把 `speaker` / `avatar` 发给上游）。"""
    out: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        out.append({k: v for k, v in m.items() if k in _WIRE_FIELDS})
    return out


@register_stage
class GroupReplyStage(Stage):
    """无主理人群聊：点名 / 路由 / 成员直接回复。"""

    async def process(self, ctx: StageContext) -> StageContext:
        # 四道边界（顺序：便宜的判断在前）
        if ctx.metadata.get("group_reply_done") or ctx.metadata.get("stop"):
            return ctx
        if getattr(ctx, "approval_replay", None):
            return ctx
        text = str(ctx.user_message or "").strip()
        if not text or text.startswith("/") or text.startswith("／"):
            return ctx

        from core.session_key import group_id_of, session_key_of_ctx
        gid = group_id_of(ctx.session_id)
        if not gid:
            return ctx
        from core import groups
        group = groups.get(gid)
        if not group:
            return ctx
        if groups.host_of(group):
            return ctx                       # 有主理人：原拓扑，不碰

        # 工作目录（AgentLoopStage 被跳过，这里补上同一件事）
        try:
            from core import session_workspace
            session_workspace.pin(ctx.storage_id)
        except Exception as exc:            # noqa: BLE001
            _log("工作目录钉不上（回默认）：%s: %s"
                 % (type(exc).__name__, exc))

        # 冷闸滑窗（与 AgentLoopStage 同一条，别让群聊绕过它）
        try:
            if ctx.session_pool is not None:
                ctx.messages = ctx.session_pool.maybe_trim_cold_window(
                    session_key_of_ctx(ctx), ctx.messages)
        except Exception as exc:            # noqa: BLE001
            _log("冷闸滑窗检查失败（本轮不剪）：%s: %s"
                 % (type(exc).__name__, exc))

        # 上一轮摘要（互见）
        from core import group_digest, group_minutes, group_reply
        entry = group_digest.get(ctx.storage_id) or {}
        digest_text = group_reply.format_digest(entry.get("items") or [])
        # 会议纪要 —— 跨轮次的事实，不在 messages 里，
        #   所以压缩压不掉它（这就是「保留清单」）。没有就说没有，不塞空块。
        _mins = group_minutes.get(ctx.storage_id) or {}
        minutes_text = str(_mins.get("text") or "")

        # 圆桌
        # 规则：自动接力 · 最多 3 位 · 群级 brief。它走另一条发言循环
        # （共享历史、一位接一位），与下面那套"并行各说各的"不是一回事。
        if group.get("roundtable"):
            return self._roundtable(ctx, group, gid, text, digest_text,
                                    minutes_text)

        # 路由（一次小 LLM 调用）
        def _router(message: str) -> str:
            if ctx.provider is None:
                raise RuntimeError("这一轮没有 provider")
            resp = ctx.provider.chat(
                messages=[{"role": "user", "content": message}],
                system=group_reply.ROUTER_SYSTEM_PROMPT,
                temperature=0.0,
            )
            try:
                ctx.tokens_used += int(getattr(resp, "prompt_tokens", 0) or 0)
                ctx.tokens_used += int(getattr(resp, "completion_tokens", 0) or 0)
            except Exception:               # noqa: BLE001
                pass
            return str(getattr(resp, "content", "") or "")

        picked = group_reply.select_responders(group, text, digest_text, _router)
        mode = picked.get("mode") or "all"

        # 点名打错了：给名单，别静默当普通消息
        if mode == "unknown_mention":
            names = [str(m.get("name") or "")
                     for m in group.get("members") or []]
            hint = ("群里没有叫 %s 的成员。现有成员：%s —— 用 `@名字` 点名。"
                    % ("、".join(picked.get("unknown") or []),
                       "、".join(names) or "（一个都没有）"))
            self._append_user(ctx, text)
            ctx.messages.append({"role": "assistant", "content": hint,
                                 "kind": "group_hint", "speaker": "群聊"})
            ctx.metadata["group_replies"] = [
                {"name": "群聊", "avatar": None, "content": hint,
                 "kind": "group_hint", "error": True},
            ]
            ctx.metadata["group_reply_done"] = True
            ctx.response = ""
            return ctx

        members = picked.get("members") or []
        if not members:
            members = [dict(m) for m in group.get("members") or []]
            mode = "all"

        # 按账本位次排序（并行完成顺序是不可控的；落库按名册顺序）
        order = {str(m.get("name") or ""): i
                 for i, m in enumerate(group.get("members") or [])}
        members.sort(key=lambda m: order.get(str(m.get("name") or ""), 999))

        # 准备每个成员的 provider / 工具 / 提示词
        from core.group_dispatch import ensure_provider
        from bus.registry import get_active_tool_names, get_tool_schemas_filtered
        from core.tiers import current_spec
        from core.prompt_assembler import assemble_system_prompt, model_name_of
        from core.events import make_emitter

        spec = current_spec(ctx.session_id)
        active = get_active_tool_names(ctx.session_id)
        # 成员不再向下派活：ask_member / run_subagent 都摘掉。
        #   呈现（_tools）与执行（_tool_allowlist）同时摘 —— 两处必须是同一条判据。
        blocked = {"ask_member", "run_subagent"}
        allowed = sorted(active - blocked)
        schemas = get_tool_schemas_filtered(allowed)
        emit = make_emitter(ctx.events, session_key_of_ctx(ctx))

        prepared: list[dict] = []
        for m in members:
            name = str(m.get("name") or "")
            model = str(m.get("model") or "")
            try:
                prov = ensure_provider(model, fallback=ctx.provider,
                                       config=ctx.config)
                prepared.append({"member": m, "provider": prov, "error": ""})
            except Exception as exc:        # noqa: BLE001
                prepared.append({"member": m, "provider": None,
                                 "error": str(exc)})

        mentioned = (mode == "mention")
        # 只带协议字段：`speaker` / `avatar` / `kind` 是界面用的，别发上游。
        base_messages = _wire_messages(ctx.messages)

        def _run_one(item: dict) -> dict:
            m = item["member"]
            name = str(m.get("name") or "")

            def _done(ok: bool, kind: str = "group_speaker") -> None:
                _emit_member("group.member.done", name=name,
                             avatar=m.get("avatar"), ok=ok, kind=kind)

            if item["provider"] is None:
                _done(False)
                return {"name": name, "avatar": m.get("avatar"),
                        "content": "成员 %s 用不了模型：%s" % (name, item["error"]),
                        "error": True, "tokens": 0}
            system = (assemble_system_prompt(
                ctx.memory_context,
                model_name=model_name_of(item["provider"]),
                session_id="",
            ) + "\n\n" + group_reply.build_member_system_block(group, m))
            user_text = group_reply.build_member_user_text(
                text, m, mentioned, digest_text, minutes_text)
            try:
                from core.agent_loop import agent_loop, _parse_approval_payload
                response, _msgs, _chunks, tokens = agent_loop(
                    user_message=user_text,
                    images=ctx.user_images or None,
                    provider=item["provider"],
                    messages=list(base_messages),
                    session_chunks=[],
                    memory_context=ctx.memory_context,
                    tool_result_max_chars=spec.tool_result_max_chars,
                    max_turns=spec.max_turns,
                    session_id=ctx.session_id,
                    _tools=schemas,
                    _tool_allowlist=frozenset(allowed),
                    _system=system,
                    _emit=emit,
                    thinking=spec.thinking,
                )
                fresh = _msgs[len(base_messages):]
                tool_count = sum(1 for x in fresh
                                 if str(x.get("role") or "") == "tool")
                # 成员这一趟产出的图/音/视频 ——
                #   挂在它那条可见回复上（每位成员各带各的）。
                try:
                    from core import artifacts as _art
                    arts = _art.records_from_messages(
                        _msgs, len(base_messages))
                except Exception:        # noqa: BLE001 — 观测不许弄挂成员
                    arts = []
                # 成员这一轮如果真的走到「问用户」那一支（手动档 /
                #    熔断 / 安全但不知道怎么预授权），会拿到一条 `pending_*`。
                #    这一层不挂起 —— 成员的中间轨迹不落进群历史，下一轮
                #    重放找不到那条 tool 消息。当场拒绝并给路线。
                pend = None
                for x in fresh:
                    if str(x.get("role") or "") == "tool":
                        pend = _parse_approval_payload(x.get("content"))
                        if pend:
                            break
                if pend:
                    _done(False, "group_approval")
                    return {"name": name, "avatar": m.get("avatar"),
                            "content": group_reply.approval_route_text(name, pend),
                            "error": True, "kind": "group_approval",
                            "tokens": int(tokens or 0),
                            "tool_count": tool_count}
                _done(True)
                return {"name": name, "avatar": m.get("avatar"),
                        "content": str(response or "").strip() or "（没有回复）",
                        "error": False, "tokens": int(tokens or 0),
                        "tool_count": tool_count,
                        "artifacts": arts}
            except Exception as exc:        # noqa: BLE001
                _done(False)
                return {"name": name, "avatar": m.get("avatar"),
                        "content": "成员 %s 这一轮没跑成：%s: %s"
                                   % (name, type(exc).__name__, exc),
                        "error": True, "tokens": 0, "tool_count": 0}

        # 先告诉界面"谁被派活了"（群聊不展示步骤流，只展示成员状态）
        def _emit_member(type_: str, **data) -> None:
            if emit is None:
                return
            try:
                emit(type_, **data)
            except Exception:               # noqa: BLE001
                pass

        for item in prepared:
            m = item["member"]
            _emit_member("group.member.start",
                         name=str(m.get("name") or ""),
                         avatar=m.get("avatar"))

        # 并行跑（不同成员之间无依赖；同一位成员的会话内部串行）
        results: list[dict] = []
        if len(prepared) == 1:
            results = [_run_one(prepared[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(len(prepared), 8)) as pool:
                futures = [
                    pool.submit(contextvars.copy_context().run, _run_one, item)
                    for item in prepared
                ]
                results = [f.result() for f in futures]

        # 按名册顺序收（上面已 sort；futures 与 prepared 同序）
        # 成员回复会直接展示/落库，必须过与 `PostProcessStage` 同一个
        #    脱敏口（`SecurityGuard.sanitize_output`）；不然后台工具输出里的
        #    密钥会绕过那条唯一出口。
        try:
            from core.security import SecurityGuard
            _sanitize = SecurityGuard.sanitize_output
        except Exception:                   # noqa: BLE001
            _sanitize = lambda x: x          # noqa: E731
        replies = []
        for item, r in zip(prepared, results):
            m = item["member"]
            r["content"] = _sanitize(str(r.get("content") or ""))
            replies.append({
                "name": str(m.get("name") or ""),
                "avatar": m.get("avatar"),
                "content": r["content"],
                "error": bool(r["error"]),
                "kind": r.get("kind") or "group_speaker",
                "model": str(m.get("model") or ""),
                "tokens": int(r.get("tokens") or 0),
            })
            ctx.tokens_used += int(r.get("tokens") or 0)
            ctx.metadata["tool_count"] = (int(ctx.metadata.get("tool_count") or 0)
                                          + int(r.get("tool_count") or 0))

        # 落库：用户消息 + 每条成员回复（带 speaker/avatar）
        self._append_user(ctx, text)
        for r in replies:
            _msg = {
                "role": "assistant",
                "content": group_reply.member_reply_content(r["name"], r["content"]),
                "kind": "group_speaker",
                "speaker": r["name"],
                "avatar": r["avatar"],
            }
            # 成员自己的产物挂在自己的回复上（没有就不设键 ——
            #   读不到 ≠ 空）。落库走 `extra_json`，刷新后照样在。
            if r.get("artifacts"):
                _msg["artifacts"] = r["artifacts"]
            ctx.messages.append(_msg)

        # 互见摘要（内部；不显示给用户）
        items = group_reply.digest_items(group, replies)
        try:
            group_digest.set(ctx.storage_id, group_id=gid, items=items)
        except Exception as exc:            # noqa: BLE001
            _log("摘要落盘失败（下一次互见会少这一轮）：%s: %s"
                 % (type(exc).__name__, exc))

        ctx.metadata["group_replies"] = replies
        ctx.metadata["group_reply_done"] = True
        ctx.metadata["group_reply_mode"] = mode
        ctx.metadata["group_reply_names"] = [r["name"] for r in replies]
        # 不设 ctx.response：无主理人没有"主持人总结"，用户看的是成员本人。
        ctx.response = ""
        _log("mode=%s responders=%s" % (mode, ",".join(
            r["name"] for r in replies) or "(none)"))
        return ctx

    # ------------------------------------------------------------------
    # 圆桌
    # ------------------------------------------------------------------
    def _roundtable(self, ctx: StageContext, group: dict, gid: str, text: str,
                    digest_text: str, minutes_text: str) -> StageContext:
        """共享历史 · 一位接一位 · 最多 `ROUNDTABLE_MAX_SPEAKERS` 位。

        与上面那条（并行各说各的）的三个不同：
          ① 共享历史 —— 后说的人看得到前面的人说了什么（同级 assistant 消息）；
          ② 自动接力 —— 下一位由一次小 LLM 调用决定；
          ③ `@点名` = 只让被点的人说（显式控制，不接力）。
        """
        from core import group_digest, group_reply
        from core.session_key import session_key_of_ctx
        from core.group_dispatch import ensure_provider
        from bus.registry import get_active_tool_names, get_tool_schemas_filtered
        from core.tiers import current_spec
        from core.prompt_assembler import assemble_system_prompt, model_name_of
        from core.events import make_emitter

        names = [str(m.get("name") or "") for m in group.get("members") or []]
        mentioned, unknown = group_reply.parse_mentions(text, names)
        if unknown:
            hint = ("群里没有叫 %s 的成员。现有成员：%s —— 用 `@名字` 点名。"
                    % ("、".join(unknown), "、".join(names) or "（一个都没有）"))
            self._append_user(ctx, text)
            ctx.messages.append({"role": "assistant", "content": hint,
                                 "kind": "group_hint", "speaker": "群聊"})
            ctx.metadata["group_replies"] = [
                {"name": "群聊", "avatar": None, "content": hint,
                 "kind": "group_hint", "error": True},
            ]
            ctx.metadata["group_reply_done"] = True
            ctx.response = ""
            return ctx

        spec = current_spec(ctx.session_id)
        active = get_active_tool_names(ctx.session_id)
        blocked = {"ask_member", "run_subagent"}
        allowed = sorted(active - blocked)
        schemas = get_tool_schemas_filtered(allowed)
        emit = make_emitter(ctx.events, session_key_of_ctx(ctx))

        def _router(message: str) -> str:
            if ctx.provider is None:
                raise RuntimeError("这一轮没有 provider")
            resp = ctx.provider.chat(
                messages=[{"role": "user", "content": message}],
                system=group_reply.RELAY_SYSTEM_PROMPT,
                temperature=0.0,
            )
            try:
                ctx.tokens_used += int(getattr(resp, "prompt_tokens", 0) or 0)
                ctx.tokens_used += int(getattr(resp, "completion_tokens", 0) or 0)
            except Exception:               # noqa: BLE001
                pass
            return str(getattr(resp, "content", "") or "")

        # 共享历史：群历史 + 这一条用户消息（只加一次）
        from core.agent_loop import user_content
        shared = _wire_messages(ctx.messages)
        shared.append({"role": "user",
                       "content": user_content(text, getattr(ctx, "user_images", None))})

        def _emit_member(type_: str, **data) -> None:
            if emit is None:
                return
            try:
                emit(type_, **data)
            except Exception:               # noqa: BLE001
                pass

        try:
            from core.security import SecurityGuard
            _sanitize = SecurityGuard.sanitize_output
        except Exception:                   # noqa: BLE001
            _sanitize = lambda x: x          # noqa: E731

        replies: list[dict] = []
        spoken: list[dict] = []

        #: `@点名`，只让被点的人按名册顺序说（不接力）；
        #: 没有 @，自动接力。
        queue: list[dict] = []
        if mentioned:
            order = {n: i for i, n in enumerate(names)}
            queue = [dict(m) for m in group.get("members") or []
                     if str(m.get("name") or "") in set(mentioned)]
            queue.sort(key=lambda m: order.get(str(m.get("name") or ""), 999))
        relay = (not mentioned)

        while len(replies) < group_reply.ROUNDTABLE_MAX_SPEAKERS:
            if queue:
                member = queue.pop(0)
            elif relay:
                picked = group_reply.select_next_speaker(group, text, spoken, _router)
                member = picked.get("member")
                if not member:
                    _log("圆桌停止接力：%s" % (picked.get("reason") or ""))
                    break
            else:
                break

            name = str(member.get("name") or "")
            _emit_member("group.member.start", name=name,
                         avatar=member.get("avatar"))
            try:
                prov = ensure_provider(str(member.get("model") or ""),
                                       fallback=ctx.provider, config=ctx.config)
            except Exception as exc:        # noqa: BLE001
                _emit_member("group.member.done", name=name,
                             avatar=member.get("avatar"), ok=False)
                replies.append({"name": name, "avatar": member.get("avatar"),
                                "content": _sanitize(
                                    "成员 %s 用不了模型：%s" % (name, exc)),
                                "error": True, "kind": "group_speaker",
                                "model": str(member.get("model") or ""),
                                "tokens": 0})
                break

            system = (assemble_system_prompt(
                ctx.memory_context,
                model_name=model_name_of(prov),
                session_id="",
            ) + "\n\n" + group_reply.build_member_system_block(group, member))
            turn_text = group_reply.build_roundtable_turn_text(
                member, spoken, minutes_text)
            try:
                from core.agent_loop import agent_loop, _parse_approval_payload
                response, msgs, _chunks, tokens = agent_loop(
                    user_message=turn_text,
                    images=None,            # 图已经在共享历史那条 user 消息里
                    provider=prov,
                    messages=list(shared),
                    session_chunks=[],
                    memory_context=ctx.memory_context,
                    tool_result_max_chars=spec.tool_result_max_chars,
                    max_turns=spec.max_turns,
                    session_id=ctx.session_id,
                    _tools=schemas,
                    _tool_allowlist=frozenset(allowed),
                    _system=system,
                    _emit=emit,
                    thinking=spec.thinking,
                )
                fresh = msgs[len(shared):]
                #: 与并行那条同一条：成员的审批挂起没法重放（它的 tool 轨迹
                #:  不落进群历史），当场拒绝 + 给路线，不挂起。
                pend = None
                for x in fresh:
                    if str(x.get("role") or "") == "tool":
                        pend = _parse_approval_payload(x.get("content"))
                        if pend:
                            break
                if pend:
                    content = group_reply.approval_route_text(name, pend)
                    kind, error = "group_approval", True
                else:
                    content = str(response or "").strip() or "（没有回复）"
                    kind, error = "group_speaker", False
                try:
                    from core import artifacts as _art
                    arts = _art.records_from_messages(msgs, len(shared))
                except Exception:       # noqa: BLE001
                    arts = []
            except Exception as exc:        # noqa: BLE001
                content = _sanitize("成员 %s 这一轮没跑成：%s: %s"
                                    % (name, type(exc).__name__, exc))
                kind, error, arts, tokens = "group_speaker", True, [], 0
                #: 接力链断了，停（别再拿一个坏模型烧下去）。
            #: 模型会模仿共享历史里的 `【名字】`
            #:   前缀，先把它自己那一层（可能多层）剥掉，再交给框架套一次。
            content = group_reply.strip_repeated_prefix(name, content)
            content = _sanitize(str(content or ""))
            #: 说完就发：正文随 `done` 事件走，
            #:   前端立刻落一条气泡；整轮结束时的 `group_replies` 是权威那份，
            #:   会把直播气泡整批替换掉（不重复、不算两次）。只在圆桌这条
            #:   路上带 `content` —— 并行那条各说各的、顺序是名册序，不适用。
            _emit_member("group.member.done", name=name,
                         avatar=member.get("avatar"), ok=not error, kind=kind,
                         content=content)
            reply = {"name": name, "avatar": member.get("avatar"),
                     "content": content, "error": bool(error), "kind": kind,
                     "model": str(member.get("model") or ""),
                     "tokens": int(tokens or 0)}
            if arts:
                reply["artifacts"] = arts
            replies.append(reply)
            ctx.tokens_used += int(tokens or 0)
            #: 共享历史：后说的人看得到这一条（同级 assistant 消息）。
            spoken.append({"name": name, "content": content})
            shared.append({"role": "assistant",
                           "content": group_reply.member_reply_content(name, content),
                           "name": name})
            if error:
                break

        # 落库（与并行那条同形：用户消息 + 每条可见发言）
        self._append_user(ctx, text)
        for r in replies:
            msg = {"role": "assistant",
                   "content": group_reply.member_reply_content(r["name"], r["content"]),
                   "kind": "group_speaker", "speaker": r["name"],
                   "avatar": r["avatar"]}
            if r.get("artifacts"):
                msg["artifacts"] = r["artifacts"]
            ctx.messages.append(msg)

        items = group_reply.digest_items(group, replies)
        try:
            group_digest.set(ctx.storage_id, group_id=gid, items=items)
        except Exception as exc:            # noqa: BLE001
            _log("摘要落盘失败（下一次互见会少这一轮）：%s: %s"
                 % (type(exc).__name__, exc))

        ctx.metadata["group_replies"] = replies
        ctx.metadata["group_reply_done"] = True
        ctx.metadata["group_reply_mode"] = "roundtable"
        ctx.metadata["group_reply_names"] = [r["name"] for r in replies]
        ctx.response = ""
        _log("roundtable speakers=%s" % (",".join(r["name"] for r in replies)
                                         or "(none)"))
        return ctx

    @staticmethod
    def _append_user(ctx: StageContext, text: str) -> None:
        """把原始用户消息放回 `ctx.messages`（PostProcess 只落新消息）。"""
        try:
            from core.agent_loop import user_content
            content = user_content(text, getattr(ctx, "user_images", None))
        except Exception:                   # noqa: BLE001
            content = text
        ctx.messages.append({"role": "user", "content": content})
