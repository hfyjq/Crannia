"""
PreProcessStage —— 在调用 LLM 之前运行。

职责：
  - 跳过空消息（除非这一轮带了图片）
  - 对重复消息去重
  - 从 SessionPool 加载 / 恢复会话
  - 注入记忆上下文
  - 处理 CLI 命令（/clear、/compact、/model 等）
  - 结算对挂起审批与提问的回答

这里的审批派发器是审批回答的唯一入口：一条回答挂起审批的消息
在本阶段就被结算，永远不会作为新的用户轮次送到模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.dedup import default as _dedup_default
from core.session_key import session_key_for, session_key_of_ctx
from core.pipeline.stage import Stage, StageContext, register_stage
# 所有"审批没有发生"的文案头。放在模块顶是因为七个结算器每一条拒绝路径
# 都要用它，而 `core/approval.py` 只 import `re`/`dataclasses`，不会成环。
from core.approval import outcome_line
# 投入档的唯一入口与它的"拒"。放在模块顶是因为守卫每轮都要跑
# （`/clear` 分支里的 `reset_tier_cache` 仍是就地 import —— 它只在那条路上用得到）。
from core.tiers import TierUnsupported, current_spec
from core.i18n import t as tr, tr_fmt as trf

#: 跨重启那一层指向 `data/runtime/seen_messages.json` —— 飞书会在重启后
#: 重投几十条前的消息。
#: 内存层 300 秒只管"同一批里重复"；这层默认 24 小时。
#:
#: 实例只有一份，住在 `core/dedup.py::default()` —— 这里那道闸只占位，
#:   `post_process` 走完才落账，没成功由调度器 `release()`。三个地方必须是
#:   同一个实例（见那个函数的注释）。


@register_stage
class PreProcessStage(Stage):
    """Prepare context for the agent loop."""

    async def process(self, ctx: StageContext) -> StageContext:
        # 图片必须在"空消息就停"这一关之前挂上。
        # "截个图、一个字不打、直接发"是这种输入最常见的形式，而在这一步之前
        # 它就是一条空消息，会被 `if not user_input` 丢掉 ——
        # 用户看到的是一轮什么都没发生。
        _attach_turn_images(ctx)

        user_input = ctx.user_message.strip()

        # 空 + 有图，放行（图本身就是这一轮的内容）；
        #    空 + 没图，照旧停。判据是"这一轮有没有内容"，不是"文本空不空"。
        if not user_input and not ctx.user_images:
            ctx.metadata["stop"] = True
            return ctx

        # ---- 输入校验 ----
        # 空串不会进入校验：它能通过上一条判断的唯一原因是带了图，
        # 而 `_validate_input("")` 问的是"这段文本像不像乱码"——
        # 对一段不存在的文本没有答案。
        validation = _validate_input(user_input) if user_input else None
        if validation:
            ctx.metadata["stop"] = True
            ctx.response = validation
            return ctx

        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)

        # ---- 中断提示：在下一条用户消息前说明上一轮被打断 ----
        #
        # 一轮在工具执行中被停下时，下一条用户消息前面会插入一句说明：
        # 那一步可能只做了一半。
        #
        # 插在用户消息前面，绝不替换它 —— 用户自己的话一个字节都不动
        #   （框架注入的内容必须读作背景事实，而不是用户话语）。
        # 一次性（`take_`，不是 `get_`）：说过一次就忘掉。
        # 它不跟取消标志一起清 —— 两者寿命不同
        #   （见 `core/cancellation.clear`）。
        # 位置在输入校验之后：这段是框架写的，不该被"像不像乱码"
        #   那套启发式过一遍。
        try:
            from core.cancellation import take_tool_interrupt
            if take_tool_interrupt(session_key):
                ctx.user_message = (
                    "（框架提示：上一轮在**执行某个工具的过程中**被用户主动打断了"
                    " —— 那一步可能**只做了一半**，不要假定它完成了。"
                    "下面这条是用户的新消息。）\n\n" + ctx.user_message)
                user_input = ctx.user_message.strip()
        except Exception as exc:                      # noqa: BLE001
            print("[cancel] 读『上一轮被打断』标记失败（不影响这一轮）：%s: %s"
                  % (type(exc).__name__, exc))

        # ---- 轮首清掉上一轮的 ICE 放行 ----
        #
        # 这就是"放行只活这一轮"的实现：豁免恰好活一轮。
        # 在轮首而不是轮末清，是因为轮末有多个出口（正常结束 / `stop` / 异常），
        # 而轮首只有一个点。
        #
        # 顺序：在这里清、在下面结算（`_settle_ice`）—— 这里清的是
        # 上一轮的放行，结算器处理的是这一轮的。顺序反了会把用户刚做出的
        # 放行丢掉。
        try:
            from core.ice import reveal as _ice_reveal
            _ice_reveal.clear_exempt(session_key)
        except Exception as exc:  # noqa: BLE001
            print("[ice] 放行表清理失败（%s: %s）" % (type(exc).__name__, exc))

        # 审批回答的唯一入口
        # 跨区 / 删除 / 计划三块几乎一样的代码，各自判 `active`、各自用精确
        # 字符串匹配回答，会让数字契约有三份实现。收成一个派发器之后它只有一份，
        # 而 `/clear` 被误当成选项回答这类问题也只有一个地方会犯。
        result = _dispatch_approval(user_input, ctx)
        if result is not None:
            # 这一轮的用户消息是框架代笔的（结算器把它换成了
            #    `[系统] [审批结果] …` 一类的话），打 kind，显示侧不收它。
            #    重启后聊天记录里冒出绿色气泡，内容是"审批结果 …"。
            ctx.metadata["user_message_kind"] = "framework_note"
            return result

        # ---- Dedup ----
        message_id = ctx.metadata.get("message_id", "")
        platform = ctx.session.platform if hasattr(ctx.session, "platform") else "cli"
        sender_id = ctx.metadata.get("sender_id", "local")

        if platform != "cli" and message_id:
            # 会话维度进键：Telegram 的
            #   `message_id` 只在单个 chat 内唯一（Bot API 规定），
            #   不带它的话私聊与群里各自第 1 条会互撞，第二条被当成
            #   "重复投递"静默丢掉。
            _dedup_args = {
                "platform": platform, "sender_id": sender_id,
                "content": user_input, "message_id": message_id,
                "scope": str(getattr(ctx.session, "session_id", "") or ""),
            }
            if _dedup_default().is_duplicate(**_dedup_args):
                ctx.metadata["stop"] = True
                ctx.metadata["dedup_skipped"] = True
                # 不许静默：这条消息被丢掉了，用户那边看见的是"没有回复"。
                #   一行日志是"重复投递"与"它没收到"唯一的区别。
                print("[dedup] 重复投递，跳过这一条（platform=%s scope=%s id=%s）"
                      % (platform, _dedup_args["scope"], message_id))
                return ctx
            #: 轮末要用同一组参数落账 / 放位（`post_process` 与调度器读这一格）。
            ctx.metadata["dedup"] = _dedup_args

        # FreeNote 的拦截层早已移除：主动/被动模式、`/R` `/r` 前缀、识别器单例
        # 与辅助表都没有消费者，连 `skills/freenote/` 本身也删了 ——
        # 这条能力现在整条不在仓里。

        # ---- 指令型 skill 命令 ----
        #
        # 不 `stop`、不产出 `response` —— 与 `/remember` 同一条路：
        #   正文被换成一条 user 消息，主循环照常运行；干活的是主 agent 自己。
        #
        # 排在所有内置分支之前：判据是整词，而内置命令用 `startswith`
        #   —— 一个叫 `model-x` 的 skill 否则会被 `/model` 吃掉。
        #   （撞名的 skill 在装载时已被拒，所以内置命令不会被遮住。）
        _skill = _skill_command_of(user_input)
        if _skill is not None:
            from bus.loaders.skill_md import render_skill_content

            _parts = user_input.strip().split(maxsplit=1)
            _rest = _parts[1].strip() if len(_parts) > 1 else ""
            # 外壳 + `Base directory` + 参数替换 + 关闭内联 shell，全部住在
            # `render_skill_content` 里（一处实现 —— 不要在这里再写一遍）。
            _block = render_skill_content(_skill, _rest)
            # 展开之后通知钩子；它给的上下文追加在这段提示词后面。
            try:
                from core import hooks as _hooks
                _exp = _hooks.user_prompt_expansion(
                    getattr(ctx, "session_id", "") or "", user_input, _block,
                    getattr(_skill, "name", "") or "")
                if _exp.get("context"):
                    _block = _block + "\n\n" + _exp["context"]
            except Exception as _exc:                          # noqa: BLE001
                print("[hooks] UserPromptExpansion 失败（忽略）：%s: %s"
                      % (type(_exc).__name__, _exc))
            user_input = _block
            ctx.user_message = _block
            # 这条命令/技能声明的工具白名单只作用于这一轮。
            ctx.tool_allowlist = getattr(_skill, "allowed_tools", None)
            # 这里不 return —— 落下去走正常流程，这一轮就是主循环的一轮。

        # 这一轮该在哪一段对话里跑
        #
        # 桌面端的三个动作（开一段新的 · 接着某一段 · 追加到当前这段）是
        # 这一轮自己的属性；`web/server.py::api_chat` 把它放进
        # `event.extras["segment"]` 带下来。
        #
        # 位置有讲究：它与 `/clear` 走同一条路 ——
        #   ① 在加载会话之前（`/clear` 正是为此提前到这一段）；
        #   ② 在轮首 ICE 清理之后（下面的 `_cmd_clear` 会恢复全保护，
        #      而轮首刚清过上一轮的放行）—— 顺序反了会把刚恢复的那份又清掉。
        # 它不 return：这一轮照常跑模型、给用户真答复。
        _seg = (((getattr(ctx, "event", None) is not None
                  and getattr(ctx.event, "extras", None)) or {})
                .get("segment"))
        if _seg:
            if _seg.get("new"):
                # 与 `/clear` 同一段代码，差别只有"不出声"（不停轮、不写回执）。
                # `_cmd_clear` 会把 `ctx.messages` 换成新段（空的）——
                #    这一轮的用户消息由下面的正常路径接上去，不要在这里动它。
                ctx = self._cmd_clear(ctx, "/clear", announce=False)
            else:
                _want = str(_seg.get("thread") or "")
                if not pool.thread_belongs_to(session_key, _want):
                    # 拒，而且这一轮不跑。绝不能退回"就在当前这段跑" ——
                    #    那会把用户这句话追加到另一段对话上，而界面显示的
                    #    是这一段：静默改坏，事后看不出来。
                    #    失败要出声；别把成本推回给用户。
                    ctx.metadata["stop"] = True
                    ctx.response = (
                        "这一段对话**不在**当前通道的名册里（可能刚被清掉，"
                        "或者界面手上是一个过期的编号）—— 这句话**没有发出去**，"
                        "免得它落到别的对话里。刷新一下左侧那份列表再试。"
                    )
                    return ctx
                # 与 `/resume <n>` 是同一个动作（`_handle_resume`）。
                # 不要抄它的 `ctx` 回填 —— `/resume` 自己写，是因为
                #   那条命令在这一轮就 return 了（命令轮，下面的正常路径
                #   永远到不了）；而这里这一轮照常继续，正常装载点
                #   （`pool.get_or_create()`）会按池子里那一份填好
                #   `ctx.session_id` / `storage_id` / `messages` —— `resume`
                #   刚把池子里那份换成了这一段，值一模一样。
                #   抄一份不只是重复：会话键测试按处数钉着
                #   `process()` 里这两个键的写点，多一处会立刻失败的。
                # 即使目标就是当前段也照切 —— `resume` 在
                #   `prev == storage_id` 时会跳过 `end_thread`；代价只是一次
                #   重读，换来的是没有分支可以写错。
                pool.resume(session_key, _want)

        # FreeNote 的 14 条命令派发分支随功能一起移除：`/日程` `/schedules`
        # `/清空日程` `/删除日程 N` `/完成日程 N` `/提醒` `/记录`
        # `/删除记录 N` `/清空记录` `/全量显示`（外加只存在于短表中的
        # `/删除` `/清空` `/完成` `/全量`）。带参数的那几条也一并移除。
        # skill 本体也一并删除。

        # 内置命令搬进注册表 ——
        #
        # 16 个 `if` 串成的分支，加上另一张字面量 `frozenset` 里的命令名，
        # 就是两个真相源，一条命令可以被声明却没有分支，而没人发现。
        #
        # 所以现在表就是真相源：`_COMMANDS` 一行一条 =
        #   （名字 · 说明 · 派发入口 · 匹配形状 · 出口模式），
        #   而 `_FRAMEWORK_COMMANDS` / `_COMMAND_HELP` /
        #   `_COMMAND_UNIMPLEMENTED` 全部从它推导。
        #
        # 顺序承重 —— 表序就是派发序；理由写在 `_COMMANDS` 上方。
        # 匹配形状保持现有语义（`exact` / `prefix`）—— 这不是重新设计。
        #   收紧成整词是另一件事，混在一起会让 diff 读不出对错。
        for _spec in _COMMANDS:
            if not _spec.matches(user_input):
                continue
            if _spec.mode == CMD_REWRITE:
                # 改写这一轮的消息，落回主循环（与指令型 skill 同形）。
                # 取值只认 `ctx.user_message` —— 两条路一条规则。
                getattr(self, _spec.handler)(ctx, user_input)
                user_input = ctx.user_message.strip()
                break
            return getattr(self, _spec.handler)(ctx, user_input)

        # ---- `/recap`：用户回来时说一句"我们到哪了" ----
        #
        # 它只记一个 metadata，什么都不发 —— 发送点在调度器轮末
        # （与 `context.state` 同一处、同一理由：覆盖所有出口路径）。
        #
        # 位置：在所有 `/` 命令之后。回来的第一句话如果是一条命令，
        # 说明他知道自己要干什么 —— 这时一段回顾是噪声。
        #
        # 零 LLM（从会话笔记拼一段话）· 默认关 · 闸门通过之前不碰磁盘。
        try:
            from core.events import EVT_RECAP
            from core.session_recap import recap_for
            _recap = recap_for(session_key)
        except Exception as exc:                     # noqa: BLE001
                # 回顾是锦上添花 —— 绝不能因此弄挂这一轮。
            print("[recap] 生成失败（%s: %s）—— 这一轮没有回顾"
                  % (type(exc).__name__, exc))
            _recap = None
        if _recap:
            # 在 `pre_process` 就发（不是轮末）：它必须落在
            # 这一轮的回复之前 —— 回来的用户第一眼要看的是"我们到哪了"，
            # 而不是先读一段新回答再回头找。放在轮末会让它落在回复下方，
            # 顺序正好反了。
            # 在这里发不违反"发送点要覆盖所有路径"：那条规矩说的是
            # 读数（每轮都要有）。回顾本来就不该出现在 `/clear`、
            # `/btw` 这类命令轮上 —— 那些轮在它之前就 return 了。
            ctx.metadata["recap"] = _recap
            try:
                if ctx.events is not None:
                    ctx.events.emit(EVT_RECAP, session_key=session_key,
                                    recap=_recap)
            except Exception as exc:                 # noqa: BLE001
                print("[recap] 事件发失败（%s: %s）—— 回顾这次看不到"
                      % (type(exc).__name__, exc))

        # 档位读取：设置读不出来，在这一轮开始前拒
        #
        # `current_spec` 只在两种情况下抛：设置里的档号不合法，或那一档还没实现。
        # 两者都是"我们知道用户要什么、但做不到" —— 这不许静默降级。
        # （静默吞掉和静默夹取都是错的；拒是唯一诚实的回答。）
        #
        # 位置是故意的：在所有 `/` 命令之后。提前拒的话，用户把设置改坏
        #   之后连 `/clear` 都发不出去 —— 而"去改设置"正是他的下一步。
        #   与"框架命令先跳过审批派发"同族。
        #
        # 这里只拒，从不改写设置（框架不"顺手修好"一个它没读懂的值）。
        try:
            _spec = current_spec(session_key)
        except TierUnsupported as exc:
            ctx.metadata["stop"] = True
            ctx.response = (
                "**这一轮没有开始** —— 投入档的设置有问题：\n\n"
                f"{exc}\n\n"
                "改 `data/settings.json` 的 `effort_level`（1–5 档），或在设置页里改。"
            )
            print(f"[tier] 拒了这一轮：{exc}")
            return ctx

        # 工作区热重载检测
        # 用户上一轮之后在 Web UI 里改过工作区设置（工作组、跨区开关）时，
        # 记一条简短提示，让模型知道工作区状态可能已经过期。
        #
        # 提示拼进 `ctx.memory_context`（即 `<background_context>`），
        # 不前置到 `ctx.user_message`：它是参考资料，不是用户的要求。
        _ws_note = ""
        try:
            from bus.registry import get_workspace_manager
            wm = get_workspace_manager()
            if wm:
                _ws_note = wm.check_settings_changed(session_key) or ""
        except Exception:
            pass

        # ---- 正常流程 ----
        # 独立一段（周期任务的 `deliver="new"`）：铸一条不与通道绑定
        # 的线程，这一轮写进去；通道当前那一段一个字节不动。
        # 这与上面 `{"new": True}` 那条正相反 —— 那条是 `/clear` 语义
        #   （通道跟着换过去）；定时任务照它做等于偷偷清掉用户的对话。
        # 因此这里不走 `pool.new_session`，也不写 `pool._loaded`。
        _det = isinstance(_seg, dict) and bool(_seg.get("detached"))
        if _det:
            # 同一批任务带同一个 `seed`，算出同一段（它们并行跑，
            #   谁都不能先问别人用哪个 id）。不给 seed，每次铸一条新的。
            sid = pool.open_detached_thread(
                session_key, seed=str((_seg or {}).get("seed") or ""))
            msgs = []
        else:
            sid, msgs = pool.get_or_create(session_key, ctx.provider)
        ctx.session_id = session_key
        ctx.storage_id = sid

        # 钩子：会话开始 + 用户这句话
        # 位置：所有 `/` 命令之后（命令不该触发 UserPromptSubmit），
        #   而 `sid` / `msgs` 都已就位。
        # 追加的是一条 `kind="hook_context"` 的 user 消息 —— 它读起来是
        #   背景资料，不是用户的新指令；两条路（sync/stream）都从 `msgs` 读。
        try:
            from core import hooks as _hooks
            _src = ("resume" if msgs
                    else (_hooks.take_session_source(sid) or "startup"))
            _ss = _hooks.session_start(sid, _src)
            if _ss.get("context"):
                msgs.append({"role": "user",
                             "content": "[hook] " + _ss["context"],
                             "kind": "hook_context"})
            _up = _hooks.user_prompt_submit(sid, user_input)
            if _up.get("blocked"):
                ctx.metadata["stop"] = True
                ctx.response = ("[hook] "
                                + (_up.get("reason") or "这一条被钩子拦下了"))
                return ctx
            if _up.get("context"):
                msgs.append({"role": "user",
                             "content": "[hook] " + _up["context"],
                             "kind": "hook_context"})
        except Exception as _he:  # noqa: BLE001
            print("[hooks] 本轮钩子失败（不影响对话）：%s: %s"
                  % (type(_he).__name__, _he))
        # 这一段对话的工作目录
        #
        # 位置不能挪：必须在这里 —— `sid` 刚铸出来（新对话与
        #   `/clear` 两条路都刚走到这一行），而它的消费者不能更早运行
        #   （`AgentLoopStage` 在轮首钉住它）。提前到调度器入口只会钉到
        #   默认工作区，症状是"新对话选的那个目录，第一条消息不生效"。
        #
        # 只在客户端显式给了才写（缺席 = 这一格一个字节不动）。
        #   "发送即锁"是界面的职责（选择器只存在于草稿态）；服务端照
        #   `MessageEvent.extras` 的通用形状办事（与 `segment` 相同）。
        # 校验不过，不写（这一轮沿用这一段原来的目录），只印一行 ——
        #   界面那侧已经校验过一次并给了用户实话；这里再拒一次是纵深防御，
        #   不是第二套判据（判据只在 `session_workspace.check_dir` 一处）。
        try:
            _ws_req = (((getattr(ctx, "event", None) is not None
                         and getattr(ctx.event, "extras", None)) or {})
                       .get("workspace"))
            if isinstance(_ws_req, str) and _ws_req.strip():
                from core import session_workspace as _sw
                _ok, _why = _sw.check_dir(_ws_req)
                if _ok:
                    _sw.set_for(sid, _ws_req)
                else:
                    print("[workspace] 这一轮带的工作目录用不了（按未选处理）：%s"
                          % _why)
        except Exception as _wse:  # noqa: BLE001
            print("[workspace] 写本轮工作目录失败（按未选处理）：%s: %s"
                  % (type(_wse).__name__, _wse))
        # 把解析出的段写回事件 —— 唯一的读者是 `core/cron/runner.py`，
        # 它要记下"这一次落在哪一段"。
        # 只能从这里拿：`deliver="new"` 那一路故意不重绑通道，事后用
        #   `bound_storage_id(channel)` 去推会得到用户当前那一段，
        #   而不是这一轮真正写进去的那一段。
        if getattr(ctx, "event", None) is not None and hasattr(ctx.event, "extras"):
            ctx.event.extras["storage_id"] = sid
        ctx.messages = msgs
        ctx.memory_context = pool.get_context(session_key, user_input, messages=msgs)
        # 工作区变更提示放进背景上下文。
        # `<background_context>` 被定义为参考资料，不是实时对话 —— 这正是它
        # 该在的位置：agent 看得到、用得上，但不会被当成用户的要求。
        if _ws_note:
            ctx.memory_context = f"{ctx.memory_context}\n\n{_ws_note}".strip()
        ctx.memory_manager = pool._mgr

        # 接上文件投递上下文（这些文件该发给谁）
        try:
            from skills.file_parser.handler import set_delivery_context
            sess = ctx.session
            channel = sess.platform if hasattr(sess, "platform") else "cli"
            # 取出原始用户 ID（"platform:type:" 之后的最后一段）
            raw_user = sess.session_id if hasattr(sess, "session_id") else str(sess)
            set_delivery_context(
                session_key=session_key,
                channel_name=channel + "_channel" if not channel.endswith("_channel") else channel,
                user_id=raw_user,
            )
        except Exception:
            pass

        return ctx

    def _cmd_mcp(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /mcp ----
        ctx.metadata["stop"] = True
        ctx.response = _handle_mcp_command()
        return ctx

    def _cmd_stop(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /stop（刹车 1：中断正在跑的那一轮）----
        #
        # 这条路径只在 Web 上真能打断一轮（waitress threads=12，
        # `/api/stop` 在另一个线程）。IM 通道打不断 —— 整条管线同步、
        # 没有 await，第二条消息进不了单事件循环，`/stop` 要等这一轮跑完
        # 才被处理；终端 CLI 的输入同样被阻塞。详见 `core/cancellation.py`。
        # 只存在 `/stop` —— `/中断` `/cancel` `/done` 都不是命令。
        #   一个动作四个名字，会把行为拆到四个可能各自漂移的入口上。
        session_key = session_key_of_ctx(ctx)
        from core.cancellation import request_cancel

        fresh = request_cancel(session_key)
        ctx.metadata["stop"] = True
        ctx.response = (
            tr("正在中断")
            if fresh else
            tr("已经在中断进程")
        )
        return ctx

    def _cmd_clear(self, ctx: StageContext, user_input: str,
                   *, announce: bool = True) -> StageContext:
        """`/clear` —— 开一段新的：旧那段归档（一条都不删），这一轮改在
        新段里跑。

        `announce=False` 供"开一段新对话"那条路使用 —— 桌面端的
        "新建任务"按钮，以及开屏之后发的第一句。它执行同一套切换动作，
        但它不是一条命令轮：那一轮还要跑模型、给用户真答复。

        所以与 `/clear` 命令的差别只有回执文字和 `ctx.metadata["stop"]`。
        其余全部（`pool.new_session` · ICE 恢复 · cwd · git 快照 · 档位解冻 ·
        清空审批格 · 名册登记）一个字都不许分叉。

        这个关键字的意义就在这里 —— 不要为了整洁把它拆成两个函数：
        一拆开，那些步骤就开始各自漂移。
        `ctx.metadata["cleared"]` 也跟着 `announce` 走 —— 它的意思是
        "这一轮是一条 `/clear` 命令轮"（界面据此不把回执落成气泡，并清空
        聊天区）。新段那条路是正常对话轮，置上它会把模型真写的那段答复吞掉。
        """
        # ---- /clear ----
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        try:
            from core.ice import reveal as _ice_reveal
            _was_down = _ice_reveal.downgraded(session_key)
            _ice_reveal.recover(session_key)
        except Exception:  # noqa: BLE001
            _was_down = False
        new_sid, msgs = pool.new_session(session_key)
        # 两个键在这里分开写。 `session_id` 取通道键
        #    （`/clear` 不动它 —— 档位 / 已发现工具 / 取消标志都认它）；
        #    `storage_id` 取池子新铸的 id，旧行因此留下来当档案。
        ctx.session_id = session_key
        ctx.storage_id = new_sid
        ctx.messages = msgs
        # `SessionStart` 的 `source` 要能说是 `clear`（下一次正常消息取走）。
        try:
            from core import hooks as _hooks
            _hooks.note_session_source(new_sid, "clear")
        except Exception:                              # noqa: BLE001
            pass
        # 机器可读的"清过了"标记。
        # 测试不许从回话措辞推断"`/clear` 有没有跑" —— 措辞会变，
        # 以它为判据的测试会因为错误的原因失败。
        # 所以判"走没走到"看这个键，不看回话文本。（回话本身仍有读者：
        #   它必须说明设置没被动过。）
        # `announce=False`（"开新段"那条路）不置这一格 ——
        #    这个标志的意思是"这一轮是一条 `/clear` 命令轮"，而那条路是
        #    正常对话轮。
        #    下面那段字面量有读者钉着措辞，不要改写法。
        if announce:
            ctx.metadata["cleared"] = True
        # 框架自己的承诺必须成立：在新对话里，上一段对话的"不再询问"
        # 不能仍然生效。`ctx` 是这份状态的载体；调度器轮末看见 False 就把
        # 这个 key 丢掉。
        ctx.session_skip_delete = False
        # 两个跨区标志同理 —— 它们都是按对话的，而 `/clear` 复用同一个
        # `session_key`。不显式重置的话，"本次对话内自动放行"和"已经问过 3 次"
        # 会活到下一段对话里。
        ctx.session_cross_zone_auto = False
        # 这是按 kind 分的字典。清成 `{}` 而不是"把每个键置 0"，
        # 才能让调度器轮末那句 `elif ...: del` 真的触发。
        ctx.approval_ask_counts = {}
        # 挂着的审批也一并丢掉。这是 `/clear` 被吞掉的另一半：命令会执行，
        # 但 `pending_*["active"]` 仍是 True，于是下一句话会被当成
        # 对上一段对话的选项回答。
        # 清成空 dict（不是 `active=False`）才能让调度器轮末的 `del` 分支
        # 可达 —— 旧写法只增不减，那个分支永远是死的。
        ctx.pending_cross_zone = {}
        ctx.pending_delete = {}
        ctx.pending_egress = {}
        ctx.pending_command = {}
        # 兜底闸那一格走同一条路归零。
        ctx.pending_destructive = {}
        # 工作区之外写文件的审批格。
        ctx.pending_write_outside = {}
        # 提问面板那一格。
        ctx.pending_ask = {}
        # 接管的逐应用授权那一格。
        ctx.pending_computer_app = {}
        # 钩子要求确认那一格。
        ctx.pending_hook_ask = {}
        # 计划批准那一格。
        ctx.pending_plan = {}
        # "补哪一次调用"的状态也丢掉。
        # 它是每轮瞬态（`/clear` 不可能是审批回答轮）。写这一行是把
        #    不变量写死：挂在 `ctx` 上的会话级审批状态在这里一次清完，
        #    下一个读者不必再去判断某一格要不要清。
        ctx.approval_replay = {}
        # 按目标的放行（出网域名 / 命令）也一并清零。
        # 必须：`/clear` 复用同一个 `session_key`；不显式重置的话，
        #    "这个域名不再询问"会活到下一段对话里。与上面的标志同形。
        ctx.session_target_grants = {}
        # 命令的工作目录（"框架记住 cwd"）。
        # 这一格是 workspace manager 上的投影，不是 `ctx` 字段 ——
        #    它的真值在调度器上，轮末从 `wm` 收回（写手是工具，而工具看不到
        #    `ctx`；见 `core/shell.py::run_command`）。清在写手写的那一格上，
        #    轮末那句 `del` 才会触发。
        # 不清的话，`/clear` 之后的第一条命令会在上一段对话最后待过的目录里跑。
        _reset_session_cwd()
        # `<git_status>` 是每对话算一次的快照（为了保持可缓存前缀稳定）。
        # `/clear` 是框架自己说的"新会话"，上一段对话的快照必须丢掉，
        # 否则新对话的第一轮还写着旧分支和旧提交。
        from core.prompt_assembler import reset_git_status_cache
        reset_git_status_cache()
        # 投入档冻结在对话上：它是提示缓存键的一部分，所以在一段对话内必须
        # 稳定，改档等于一次冷启动。`/clear` 是框架自己说的"新会话"，
        # 丢掉旧冻结；新对话在第一次提问时重读设置。
        # 与上面那条同一个理由，所以清在同一处。
        # 只清这一个会话，绝不是全部 —— 六个通道共用一个 scheduler，
        #    各有自己的 `session_key`。全局清会解冻另一个通道的活对话并让它
        #    重读设置，而"改档 = 一次冷启动"正是要防止这件事（何况用户什么都没做）。
        # 这里传 `session_key`（通道键）。档位是通道作用域的
        #    （在对话内冻结），两个读者（`pre_process` 传 `session_key`；
        #    `core/agent_loop` 传 `ctx.session_id`）都解析到通道键。
        from core.tiers import reset_tier_cache
        reset_tier_cache(session_key)
        # 这两条没有收进 `core.state_registry.purge_session()` ——
        #    它们在 `SESSION_SCOPED_BY_OTHER` 里（census 读者照样盯着），但清除
        #    留在阶段这一层，因为有两条读者钉着：一条用假 pool 驱动真阶段，
        #    另一条按 AST 判分支。收进 manager 层会让假 pool 够不到 ——
        #    "测试替身碰不到的机制"正是本仓库记过的坑，不为整洁推翻有判据的读者。
        # 到这里切换动作已经全部做完（会话换了、审批格清了、
        #    cwd / git 快照 / 档位都回到新对话该有的样子）。下面两样东西
        #    （停轮标记 + 回执文字）属于"用户打了一条 `/clear` 命令"；
        #    "开一段新的"那条路上两者都不成立 —— 它是一轮正常对话，
        #    用户那句话还没跑。
        #    用早退而不是把下面四十行缩进一层：大段重排正是
        #    "看着等价、改完就漂"的形状。
        if not announce:
            return ctx
        ctx.metadata["stop"] = True
        # 措辞三条规矩：
        # ① 中文 —— 框架目前只有这一套回话语言。
        # ② 不打印 "New session: <id>" —— 用户看到那个 id 没有用，而且
        #    会让人以为新建了会话，实际上旧行只是被归档了。
        # ③ 说明设置没被动过 —— 这是用户最担心的一格（"会不会改了我的
        #    档位或模型"）。它是一句承诺，而承诺要有读者：有测试钉着它。
        # `/clear` 是归档，不是删除 —— 行为变了必须说明，否则用户会
        #    以为内容没了。
        #    回来的路（`/resume`）已经存在，并且验证过不会删除任何东西，
        #    所以回话可以点名它。这句话仍然说的是"东西还在，只是暂时看不见"
        #    —— 两半都是真的。
        ctx.response = (
            tr("已清空窗口上下文，可以在这里重新开始。\n上一段对话仍保存在库中，可用 `/resume` 切回。")
        )
        # `/clear` 杀后台任务，不再"清账不杀"。
        # 判据："清空"必须真的清空 —— 留一个看不见的活在后台跑，
        # 用户会以为干净了（「标签在说谎」同族）。
        # 杀了要说出来；没杀到就什么都不说（不许编一个"已停止 0 个"）。
        try:
            from core import jobs as _jobs
            _n_running = _jobs.kill_all(session_key_of_ctx(ctx))
            _jobs.clear()
        except Exception:  # noqa: BLE001
            _n_running = 0
        if _n_running:
            ctx.response += (
                trf("已停止 {_n_running} 个后台任务（命令进程已杀；子代理已取消，未完成的结论不回传）。", _n_running)
            )
        return ctx

    def _cmd_compact(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /compact ----
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        sid, msgs = pool.get_or_create(session_key, ctx.provider)
        #: 手动 `/compact` 与超长自救共用同一对事件 —— 界面那条
        #:   黄横幅（"正在压缩，请稍后…"）对两条路是同一件事。
        #:   必须有收尾（`ok` / `fail`），否则横幅永远停在"进行中"。
        from core.events import EVT_COMPACT_PROGRESS, make_emitter
        _emit = make_emitter(getattr(ctx, "events", None), session_key)
        if _emit:
            _emit(EVT_COMPACT_PROGRESS, state="start")
        try:
            compacted, was_done = pool.compact(session_key, msgs)
        except Exception as _cexc:                               # noqa: BLE001
            if _emit:
                _emit(EVT_COMPACT_PROGRESS, state="fail",
                      reason="%s: %s" % (type(_cexc).__name__, str(_cexc)[:160]))
            raise
        if _emit:
            # "没压"不是"失败"（实际）：这里一条请求都没发，也不存在超限，
            #    所以第三态 `none`：黄横幅 + 一句话 + 知道了，点完就消失。
            #    `fail` 只留给真出错（下面那个 `except` 分支）。
            if was_done:
                from core.memory.compressor import progress_payload
                _emit(EVT_COMPACT_PROGRESS, **progress_payload(
                    pool.last_compression,
                    getattr(getattr(ctx, "provider", None), "context_window", None)))
            elif pool.last_blocked:
                #: 该压却一点都没压成（已进强制区）—— 这时才劝重启。
                from core.memory.compressor import progress_payload as _pp
                _emit(EVT_COMPACT_PROGRESS, **_pp(
                    None, getattr(getattr(ctx, "provider", None),
                                  "context_window", None),
                    payload_blocked=True))
            else:
                _emit(EVT_COMPACT_PROGRESS, state="none",
                      reason=tr("没有可压缩的内容"))
        if was_done:
            stats = pool.last_compression or {}
            ctx.messages = compacted
            #: 会话池缓存的是对象引用（`_loaded[key] = (sid, messages)`），
            #: 而压缩给的是一份新列表 —— 不写回的话，下一次 `get_or_create`
            #: 拿回的还是压缩前那份。
            #: 现场（2026-11-02 22:58）：连着两次 `/compact`，第二次的
            #: `messages_before` 与第一次逐字相同（778），库里因此多出一条
            #: 重复摘要 + 一条 `kept_from_id` 相同的边界行，而模型下一轮
            #: 看到的仍是没压过的那 778 条。
            #: 与 `post_process.py` 末尾那三行同一个口径 —— 那里每轮都会
            #: 同步一次，而 `/compact` 在 pre_process 就 stop，走不到它。
            if session_key in pool._loaded:
                pool._loaded[session_key] = (pool._loaded[session_key][0],
                                             ctx.messages)
            ctx.session_id = session_key
            ctx.storage_id = sid
            # "这一轮真的压过"。轮末的 `context.state` 事件只在这个标记
            # 为真时才带压缩读数 —— `compressor.last_compression` 是粘的
            # （一直留到下一次压缩），无条件带上会让每一轮都看起来刚压过。
            # 这是 post_process 唯一看不到的一条路：`/compact` 在
            #    pre_process 就 `stop` 了，post_process 不跑。
            ctx.metadata["compressed"] = True
            ctx.metadata["stop"] = True
            # 回话必须说明原文还在 —— 否则用户和模型都会以为被压掉的
            #   那几轮没了。措辞复用滑窗说明行（`window_note`，
            #   `manager._apply_sliding_window`）—— 一个事实一句话。
            #: 回话就一句：完成 + 当前占用。降级/熔断时换成那一句实话
            #:   （用户要知道"这次压得不完整"），不是同一条消息里叠两个结论。
            from core.memory.compressor import progress_payload
            _pl = progress_payload(
                stats, getattr(getattr(ctx, "provider", None), "context_window", None))
            ctx.response = _pl.get("message") or tr("压缩完成。")
        else:
            ctx.messages = msgs
            ctx.session_id = session_key
            ctx.storage_id = sid
            ctx.metadata["stop"] = True
            # 绝不说 "Not enough turns" —— 那是一个固定但错误的原因
            #   （真正的原因是"没过阈值"）。压缩器现在照实报告：`force=True`
            #   之后，能压的一定压了，压不动的把真实原因写在这里。
            # 回话就是压缩器给的那句原因本身 ——
            #    以前前面硬加一句「没有压缩。」，于是同一条消息里两个结论叠着
            #    （「没有压缩。当前上下文容量未达最低压缩量…」）。
            #    每一条 `last_skip_note` 本来就是完整的一句话（照实给原因），
            #    直接用它；读不到才落到那句"原因没拿到"。
            #    与黄横幅（`compact.progress` 的 `none` 态）同一句话，
            #       所以两处必须一起改 —— 改一处会变成两个说法。
            _why = str(getattr(pool, "last_skip_note", "") or "").strip()
            ctx.response = _why or tr("原因没拿到（压缩器没给说明）。")
        return ctx

    def _cmd_context(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /context ----
        #
        # 它与上下文面板走同一个取数口
        #   （`core/context_report.py::breakdown`，`GET /api/context` 背后那条路）
        #   —— 面板与这条命令绝不能各算一遍。
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        sid, msgs = pool.get_or_create(session_key, ctx.provider)
        ctx.metadata["stop"] = True
        ctx.response = _handle_context_command(pool, session_key, msgs)
        return ctx

    def _cmd_skill(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /skill ----
        parts = user_input.split(maxsplit=2)
        ctx.metadata["stop"] = True
        ctx.response = _handle_skill_command(parts)
        return ctx

    def _cmd_model(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /model ----
        #
        # 写 `pool.set_model_override(...)` 会让这条命令变成死命令：
        #    没有任何地方读那张表，于是 `/model xxx` 回一句
        #    "Model switched to: xxx"，下一轮却照旧跑旧模型。它接的是真正
        #    生效的那条链（`core/session_model`）。
        #
        # 语义：
        #   · `/model`，看：这一段用哪条配置、是冻住的还是跟默认
        #   · `/model <配置名 | id>`，切过去（下一轮生效）
        #   · `/model 默认` / `跟随`，切回"跟随默认"
        session_key = session_key_of_ctx(ctx)
        parts = user_input.split(maxsplit=1)
        ctx.metadata["stop"] = True
        if len(parts) == 2:
            ctx.response = _switch_session_model(session_key, parts[1].strip())
        else:
            ctx.response = _describe_session_model(session_key)
        return ctx

    def _cmd_change(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /change [编号] ----
        # 无参 = 列出可切换的；带参 = 切过去。
        #
        # 两种编号来源（「空 /change 没有对应的列表」）：
        #   ① 多会话活跃时问过的那张编号表（`im_routing` 存着）—— 那是渠道键
        #      级别的切换（私聊 / 各个群），切换会写 `routing.json`（持久）；
        #   ② 没有那张表，退到本渠道的对话列表（与 `/resume` 同一份）——
        #      私聊只有一个键，用户嘴里的"会话"就是这里的一段段对话。
        # 以前的空参只回一句「使用/change 编号切换会话」—— 用户看完更不知道
        #   有哪些编号；而 `/change 2` 在没问过编号时永远回「此编号不存在，现有：无」。
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        parts = user_input.split()
        n = None
        if len(parts) >= 2:
            try:
                n = int(parts[1])
            except ValueError:
                n = None

        from core import im_routing
        channel = im_routing.channel_key_of(ctx.session)
        numbering = im_routing.numbering_for(channel)
        ctx.metadata["stop"] = True
        #: 同 `/resume`：这一屏是列表，IM 那条路要按行发。
        ctx.metadata["deliver_lines"] = True

        if n is None:
            if numbering:
                ctx.response = _render_switchable_keys(numbering, pool)
            else:
                threads = _visible_threads(
                    pool.list_threads(session_key, limit=_RESUME_LIMIT))
                if not threads:
                    ctx.response = tr("这条通道还没有对话可以切换。")
                else:
                    ctx.response = (_render_thread_list(threads)
                                    + tr("\n（用 `/change <编号>` 切过去；"
                                         "`/resume <编号>` 只切这一条。）"))
            return ctx

        if numbering and n in numbering:
            def _exists(key: str) -> bool:
                try:
                    return bool(pool.session_exists(key))
                except Exception:
                    return True
            # 这里要的是渠道键（绑定绑在渠道上），不是会话键
            ctx.response = im_routing.change_default(channel, n, _exists)
            return ctx

        # 没有路由编号表（或编号不在表里），当成对话编号：与 `/resume` 同一条路
        ctx.response = _handle_resume(["", str(n)], ctx, session_key, pool)
        return ctx

    def _cmd_effort(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /effort ----
        #
        # 它必须排在下面的档位守卫之前 —— 设置里的档号不合法时，守卫会
        #   拒掉整轮，而 `/effort` 是用户唯一的出路（拒掉它等于把门锁死）。
        session_key = session_key_of_ctx(ctx)
        parts = user_input.split(maxsplit=1)
        ctx.metadata["stop"] = True
        ctx.response = _handle_effort(parts, session_key)
        return ctx

    def _cmd_language(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /language ----
        # 回答语言 —— 与 `/effort` 同四条：当场生效并说明是冷启动 ·
        # 只动本会话 · 不吞错 · 设置坏了也能用。
        session_key = session_key_of_ctx(ctx)
        parts = user_input.split(maxsplit=1)
        ctx.metadata["stop"] = True
        ctx.response = _handle_language(parts, session_key)
        return ctx

    def _cmd_mode(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /mode ----
        #
        # 手动 / 自动 / 计划。与 `/effort` 同四条设计：当场生效并说明这是
        # 冷启动 · 只动本会话 · 不吞错 · 设置坏掉时也能用。
        # 它不是 `agent_mode`（那条轴已整体移除）。
        session_key = session_key_of_ctx(ctx)
        parts = user_input.split(maxsplit=1)
        ctx.metadata["stop"] = True
        ctx.response = _handle_mode(parts, session_key)
        return ctx

    def _cmd_btw(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /btw ----
        # 问一句题外话，不打断主对话。
        #
        # 位置：与其它 `/` 命令并排（在档位守卫之前）。它们不消费任何
        #   档位旋钮 —— 一次 `provider.chat`、没有循环、没有工具调用 ——
        #   所以"读到了档位却做不到"不成立。（`/effort` 放在守卫之前是另一个
        #   理由：它是改设置的唯一入口。）
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        parts = user_input.split(maxsplit=1)
        ctx.metadata["stop"] = True
        ctx.response = _handle_btw(parts, ctx, session_key, pool)
        return ctx

    def _cmd_recap(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /recap ----
        # 短暂回顾最近的对话。
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        ctx.metadata["stop"] = True
        ctx.response = _handle_recap(ctx, session_key, pool)
        return ctx

    def _cmd_resume(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /resume ----
        #
        # 把通道切回已归档的某条线程。无参 = 列出；`/resume N` = 切换。
        # 它不复制、不删除任何东西 —— 只改 `session_bindings` 那一行。
        #
        # 判据是"整词或整词+空格"，不是裸 `startswith`：后者会接受
        #   `/resume2`，而 `_is_framework_command` 比的是第一个 token ——
        #   同一条输入会得到两个答案。
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        parts = user_input.split(maxsplit=1)
        ctx.metadata["stop"] = True
        #: IM 那条路按行发（微信客户端会把换行折成空格，一张墙）。
        #:   Web/CLI 不看这个标记，行为不变。
        ctx.metadata["deliver_lines"] = True
        ctx.response = _handle_resume(parts, ctx, session_key, pool)
        return ctx

    def _cmd_fork(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /fork ----
        #
        # 把当前整条线程复制一份，在副本上继续，原件一点不动。
        # 它不带序号：分叉点就是末尾，用户不需要先看列表。
        # 它与 `/rewind` 是同一台机器上的两个切点：
        #   `/fork` = 切点在末尾。
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        ctx.metadata["stop"] = True
        ctx.response = _handle_fork(ctx, session_key, pool)
        return ctx

    def _cmd_rewind(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /rewind ----
        #
        # 无参 = 列出这段对话的轮；带参 = 退到第 N 轮之前。
        # 按轮不按条：轮的边界永远是合法切点，而"第 N 条消息"往往不是
        #   （切在工具调用中间会让上游返回 400）—— 那会逼着向用户解释
        #   "你要的那条不行，我把你挪到别处了"。
        pool = ctx.session_pool
        session_key = session_key_of_ctx(ctx)
        parts = user_input.split(maxsplit=1)
        ctx.metadata["stop"] = True
        ctx.response = _handle_rewind(parts, ctx, session_key, pool)
        return ctx

    def _cmd_remember(self, ctx: StageContext, user_input: str) -> StageContext:
        # ---- /remember ----
        #
        # 与其余每一条 `/` 命令都不同：它既不 `stop`，也不产出
        # `response`。
        #
        # 载荷是一段提示词（`core/promote.py`）。它替换用户这句话之后，
        # 主循环照常运行 —— 干活的是主 agent：读记忆 → 提议 →
        # 逐条得到批准 → 用通用文件工具写入。
        #
        # 所以不需要任何新机制：换掉 `user_message` 就够了。
        #
        # 位置：与其它 `/` 命令并排（指令型 skill 命令之后、档位守卫
        #   之前）。
        # · skill 那条用 `startswith`，而这里判整词 —— 撞名的 skill 在装载时
        #   已被拒，抢不走它。
        # · 它不消费任何档位旋钮（一次普通主循环），没有被拒的理由。
        #
        # 它不 `return` —— 派发器认得 `mode=CMD_REWRITE`，调用完落回
        #   主循环（与指令型 skill 同形）。派发器只读 `ctx.user_message`，
        #   所以在这里写它就是全部契约。
        from core.promote import COMMAND, expand
        # 路径要按这一段的工作目录算（"项目根的 LEAF.md"
        #   是跟着会话走的）—— `resolve()` 回 `""` = 没选过，让 `expand`
        #   自己回退默认工作区。
        try:
            from core import session_workspace as _sw
            _base = _sw.resolve(getattr(ctx, "storage_id", "")) or None
        except Exception:                                   # noqa: BLE001
            _base = None
        ctx.user_message = expand(user_input[len(COMMAND):], base=_base)


# ------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------

# 输入校验阈值
_MAX_INPUT_LENGTH = 10_000      # chars — longer inputs are truncated with a warning
_MIN_PRINTABLE_RATIO = 0.15    # below this ratio → likely gibberish / binary
_MAX_EMOJI_RATIO = 0.30        # above this ratio → likely emoji spam


def _attach_turn_images(ctx) -> None:
    """把 `ctx.message_chain` 里的媒体做成这一轮的块。

    ## 它做三件事

    1. 把正文里的 `[Image: /path]` 占位换成非媒体部分的文本
       （媒体本身就在旁边，再留一行"某路径下有个文件"是噪声）；
    2. 规范化媒体（`core/image_input.prepare_turn`）：图片转码 / 限尺寸 /
       落盘 · 音频直发（白名单格式）· 视频抽帧 · 文件变成带路径的句柄；
    3. 过能力闸：provider 没有声明 `supports_vision` /
       `supports_audio` 时，那类媒体不发，并在正文里留下一句
       为什么、下一步怎么办。
       判据：声明支持才给原生部件，不支持就明说。
       视频没有统一的 wire 格式，所以按抽帧走图片链。

    ## 两条不许改的

    * 带图但没有文字的轮次不能被丢掉 —— "贴个截图、一个字不打"是常见
      用法。调用点把它放在"空消息就停"之前，正是为了这个。
    * 链子为空时不要动 `user_message` —— 有些路径（测试、将来的新通道）
      直接构造 `StageContext`，给了文本却没给链子；按链子覆盖会把那段文本
      静默抹掉。判据是"链子里到底有没有东西"，不是"结果空不空"。
    """
    from core import image_input as II
    from providers.base import accepts_audio, accepts_images

    chain = getattr(ctx, "message_chain", None)
    if chain is None:
        return
    components = list(getattr(chain, "components", None) or ())
    if not components:
        return

    provider = getattr(ctx, "provider", None)
    result = II.prepare_turn(
        components,
        sees_images=accepts_images(provider),
        # 音频走同一条路由 —— 模型声明支持就发原生块；否则给它一段
        #   看得懂的说明（时长 / 路径 / 下一步），而不是静默丢掉媒体。
        sees_audio=accepts_audio(provider),
    )
    if result.text is not None:
        ctx.user_message = result.text
    # 字段名还是 `user_images` —— 它装的是这一轮的媒体块
    #   （`image_url` / `input_audio`）。改名会动 `StageContext` 的签名与
    #   六处读点，而判据只看"有没有块"，不是"块是什么"（`agent_loop.user_content`）。
    ctx.user_images = result.blocks or []


def _validate_input(text: str) -> str | None:
    """*text* 未通过输入校验时返回错误说明，否则返回 `None`。

    检查项：
      1. 超长消息（>10K 字符）—— 截断并警告。
      2. 疑似乱码 / 二进制输入 —— 可打印字符比例过低。
      3. 表情刷屏 —— 表情占比过高且没有有意义的文本。
    """
    # --- 1. Super-long input ---
    if len(text) > _MAX_INPUT_LENGTH:
        truncated = text[:_MAX_INPUT_LENGTH]
        return (
            f"⚠️ 您的消息过长（{len(text):,} 字符），已截断至 "
            f"{_MAX_INPUT_LENGTH:,} 字符。如果内容不完整，请分段发送。\n\n"
            f"--- 截断内容 ---\n{truncated}\n---"
        )

    # --- 2. 表情刷屏检测（放在乱码检查之前 —— 按 CJK/ASCII 启发式，
    #        表情不算"可打印"）---
    emoji_count = sum(
        1 for c in text
        if '\U0001f000' <= c <= '\U0001ffff'
        or '\U0001f300' <= c <= '\U0001f5ff'
        or '\U0001f600' <= c <= '\U0001f64f'
        or '\U0001f680' <= c <= '\U0001f6ff'
        or '\U0001f900' <= c <= '\U0001f9ff'
        or '☀' <= c <= '➿'   # Misc symbols
        or '⭐' <= c <= '⭕'
    )
    if len(text) >= 20 and emoji_count / len(text) > _MAX_EMOJI_RATIO:
        return (
            tr("⚠️ 你的消息主要是表情符号。请用文字描述你的需求，我会尽力帮助你。")
        )

    # --- 3. Gibberish / binary detection ---
    printable = sum(
        1 for c in text
        if c.isalpha() or c.isdigit() or c in " ,.!?;:()-–—""''\n\r\t"
        or '一' <= c <= '鿿'   # CJK Unified
        or '　' <= c <= '〿'   # CJK punctuation
        or '＀' <= c <= '￯'   # Fullwidth forms
    )
    ratio = printable / max(len(text), 1)
    if ratio < _MIN_PRINTABLE_RATIO:
        return (
            tr("⚠️ 你的输入似乎包含大量非文本内容。如果你想解析文件，请使用对应的解析指令。")
        )

    return None


# ------------------------------------------------------------------
# /skill 命令处理器
# ------------------------------------------------------------------

def _handle_mcp_command() -> str:
    """处理 /mcp —— 显示 MCP 服务器状态。"""
    try:
        from mcpx.manager import (get_manager, _load_external_config,
                                  missing_runtime)
        from mcpx.servers import BUILTIN_SERVERS
        mgr = get_manager()
        external_cfgs = _load_external_config()
    except Exception as e:
        return trf("⚠️ MCP 状态不可用: {e}", e)

    lines = [tr("🔌 MCP 服务器状态"), "=" * 30]

    # 内置（名字从 `BUILTIN_SERVERS` 来，不写死 —— `get_status` 那条注释的判据）
    # `trf` 是「按占位符名字配对、但位置传值」（`tr_fmt(template, *values)`）
    #    所以这里只能给位置参数，写 `n=…` 会在运行期 TypeError。
    lines.append(trf("\n📦 内置服务器: {n} ({names})",
                     len(BUILTIN_SERVERS), ", ".join(BUILTIN_SERVERS)))
    lines.append(trf("   已注册工具: {mgr._tools_registered - _count_external_tools(mgr)}", mgr._tools_registered - _count_external_tools(mgr)))

    # 外部配置（来自文件）
    if external_cfgs:
        lines.append(trf("\n📋 配置文件 (data/mcp_servers.json): {len(external_cfgs)} 个服务器", len(external_cfgs)))
        for cfg in external_cfgs:
            name = cfg.get("name", "?")
            transport = cfg.get("transport", "stdio")
            cmd = cfg.get("command", cfg.get("url", "?"))
            connected = name in mgr._external_servers
            if connected:
                status = tr("✅ 已连接")
            else:
                # 未连接分两种：缺运行环境 vs 真连不上。
                #    把"你没装 Node"说成"连不上"，用户会去查服务器、而不是装东西。
                missing = missing_runtime(cfg)
                if missing:
                    status = trf("运行环境缺失：找不到 {cmd}", missing)
                else:
                    status = tr("❌ 未连接（重启后自动连接）")
            lines.append(f"   {status}  {name} ({transport}: {cmd})")
        # 缺什么、怎么补 —— 一句就说清，别让用户去猜
        lacking = sorted({missing_runtime(c) for c in external_cfgs} - {""})
        for cmd in lacking:
            lines.append(trf(
                "   ！！ 当前找不到 {cmd} —— 请先安装并重启尝试；"
                "或尝试改用 Python（python -m …）或 HTTP 型 MCP。", cmd))
    else:
        lines.append(trf("\n📋 配置文件: 无外部服务器"))

    # 已连接的外部服务器
    if mgr._external_servers:
        lines.append(trf("\n🔗 已连接外部: {len(mgr._external_servers)} 个", len(mgr._external_servers)))
        for name, server in mgr._external_servers.items():
            tools = len(server._tools) if hasattr(server, "_tools") else 0
            lines.append(trf("   {name}: {tools} 个工具", name, tools))

    lines.append(trf("\n💡 添加外部 MCP: 编辑 data/mcp_servers.json 后重启框架"))
    return "\n".join(lines)


def _count_external_tools(mgr) -> int:
    try:
        return sum(
            len(s._tools) if hasattr(s, "_tools") else 0
            for s in mgr._external_servers.values()
        )
    except Exception:
        return 0


def _describe_session_model(session_key: str) -> str:
    """`/model` 不带参数 —— 这一段现在用哪条模型配置。

    三个信息缺一不可：用的是哪条 · 它是冻住的还是跟随默认 ·
      默认换掉之后这一段会不会跟着变。
    少了第二样，用户看到"正在用 A"就分不出那是自己选的还是恰好是默认的
      —— 而这正是他问 `/model` 想知道的事。
    """
    from core import model_configs as _mc
    from core import session_model as _sm

    info = _sm.describe(session_key)
    if not info["config_id"]:
        return tr("跟随默认配置——前往端配置页配置一个模型以开始")
    if info["frozen"]:
        e = _mc.get(info["config_id"]) or {}
        return (tr("当前固定：%s，使用/model 配置名 以切换模型")
                % (e.get("name") or info["config_id"]))
    return tr("跟随默认配置中")


def _switch_session_model(session_key: str, ref: str) -> str:
    """`/model <配置名|id|默认>` —— 把这一段换成那条配置。

    三条纪律（与 `/effort` 同族）：
      ① 先试装、后落盘 —— 装不上就不写（`preflight`，与"设为默认"相同）。
         否则用户看到"已切换"，而下一轮静默退回旧模型。
      ② 不吞错：名字写错或有歧义时原样拒绝，并把候选列出来。
      ③ 当场生效，并说明这是一次冷启动：下一轮就用新模型，而提示缓存会
         从那个字节起重算。
    """
    from core import model_configs as _mc
    from core import session_model as _sm

    if ref in ("默认", "跟随", "default", "follow"):
        _sm.follow_default(session_key)
        return tr("已切回跟随默认")

    entry = _mc.find_by_ref(ref)
    if not entry:
        clash = _mc.name_clashes(ref)
        if clash:
            return tr("存在重名配置，%s，使用/model id 以指定") % ref
        rows = _mc.list_names()
        avail = ("、".join(f"「{n}」" for n, _i, _c, _m in rows) if rows
                 else tr("（空）"))
        return trf("没有找到此配置，已配置列表：{avail}", avail)
    why = _mc.preflight(entry["id"])
    if why:
        return tr("此配置异常，请前往端配置页检查配置")
    _sm.set_for(session_key, entry["id"])
    return tr("已切换，下一条消息起生效。")


def _handle_language(parts: list, session_key: str) -> str:
    """`/language` —— 看或改本会话的回答语言。

    三层取值见 `core/response_language.py`：本会话 > 设置 > 环境。
      这条命令只改第一层；持久默认在设置页（与 `/effort` 同一分工）。

    改了就是冷启动：那一行在 system prompt（前缀）里，下一条请求
      前缀变了、要重算一次。当场说出来（规矩是改档必须出声）。
    """
    from core import response_language as RL

    cur = RL.current(session_key)
    if len(parts) == 1:
        if RL.session_override(session_key):
            src = tr("本会话（/language 设的）")
        elif RL.configured_language():
            src = tr("设置页的「模型回答语言」")
        else:
            src = tr("跟随系统")
        return (tr("当前回答语言：%s（%s）\n可选：zh（简体中文）/ en（English），用 `/language zh` 这样改；只影响此对话")
                % (RL.LANG_NAMES.get(cur, cur), src))

    want = RL.normalize(parts[1])
    if want is None:
        return ("命令错误：只认 zh（简体中文）/ en（English），"
                "收到 %r。可以先用 `/language` 看当前值。" % (parts[1].strip(),))
    RL.set_session(session_key, want)
    return ("回答语言已改为 %s，只对当前对话生效，下一条消息起算。"
            "发 /clear 之后回到设置里的默认。"
            % RL.LANG_NAMES.get(want, want))


def _handle_effort(parts: list, session_key: str) -> str:
    """`/effort` —— 看或改投入档。用户改档的唯一入口。

    四条设计：

    ① 当场生效，并说明这是冷启动 —— 档位是提示缓存键的一部分，换档会改变
       这段对话下一条请求的前缀（要重算一次）。因此：清掉本会话的冻结
       并说出来。
       它不写设置 —— 见下面那段。另一条路（"写设置、下一段会话
       生效"）会逼用户等一次 `/clear`，而 `/effort high` 的意思就是"现在用"。
    ② 只动本会话 —— 六个通道共用一个进程，别顺手让另一个通道的活对话
       冷启动。
    ③ 不吞错：名字写错就原样拒绝，并列出可选值。"看起来成功了"是最坏的
       成功。
    ④ 设置文件坏了也能用 —— 把用户从非法档号里救出来是这条命令的职责，
       所以它不经过那道守卫（这里 `current_spec` 抛异常是信息，不是错误）。
    """
    # `json` / `Path` 不再 import —— 已经没有落盘那一段了。
    from core import tiers
    from core.tiers import TIERS, TierUnsupported, level_of

    def _table(current: int | None) -> str:
        rows = []
        for lvl in sorted(TIERS):
            spec = TIERS[lvl]
            rows.append(f"{lvl} {spec.name}")
        return "\n".join(rows)

    # 读现状（可能读不出来 —— 而上一条 docstring 的 ④ 就是这个意思）
    current: int | None = None
    try:
        current = current_spec(session_key).level
    except TierUnsupported:
        current = None

    if len(parts) == 1:
        _cur = (f"{current} {TIERS[current].name}" if current is not None
                else tr("无法识别"))
        return (tr("当前档位：%s\n%s\n用 /effort <名称或数字> 切换档位，/effort thinking on|off 开关思考")
                % (_cur, _table(current)))

    want = parts[1].strip()

    # `/effort thinking on|off` —— 只影响本会话。
    #
    #   它住在 `_handle_effort` 里而不是自成一条命令，有两个理由：
    #   ① 它改的是同一件事（这一段对话的思考强度），只是粒度更细；
    #   ② 不用动命令表 —— 那些表的读者要求 `process()` 的每个分支都
    #      登记；少一个分支就少一处会忘的地方。
    #
    #   它不是"切到关闭档" —— 六个档的思考都是开的；档表里
    #      没有"关"这一档。
    #   `parts` 是 `split(maxsplit=1)` 切出来的
    #      （`["/effort", "thinking off"]`），不是 `split()`，所以
    #      `thinking off` 是一个 token，会静默落进"看现状"分支 ——
    #      看起来成功，其实什么都没改。解析前必须再切一次。
    _sub = want.split()
    if _sub and _sub[0].lower() in ("thinking", "思考", "思考模式"):
        _off = tiers.is_thinking_overridden_off(session_key)
        if len(_sub) < 2:
            return ("当前思考开关为 %s，可以使用后缀命令管理。"
                    "后缀命令仅更改此次会话。" % (tr("关闭") if _off else tr("打开")))
        _val = _sub[1].strip().lower()
        if _val in ("on", "开", "打开", "true", "1"):
            tiers.set_thinking_override(session_key, True)
            _body = tr("开启")
        elif _val in ("off", "关", "关掉", "false", "0"):
            tiers.set_thinking_override(session_key, False)
            _body = tr("关闭")
        else:
            return tr("命令错误，请使用on/off后缀")
        return tr("思考已%s，下一条消息起生效（仅当前对话）。") % _body

    try:
        level = level_of(want)
    except TierUnsupported as exc:
        return tr("命令错误，请查询档位名称")

    # `/effort` 不落盘。
    #
    # 把 `effort_level` 写进 `data/settings.json`，会让一次 `/effort 1` 改掉
    # 以后每一段新对话的默认档，而命令自己的说法是"别的会话不受影响"
    # —— 标签在说谎。`/mode` 是同一个形状。
    #
    # 所以两个入口各管一件事：
    #   · 本会话（`_frozen`）—— 这里；`/clear` 重置它
    #   · 持久默认 —— 只由设置页（`POST /api/settings`）写
    #
    # 去掉的是落盘那一段；`reset_tier_cache(session_key)` 留着
    #   （它是"当场生效"的实现，不是持久化）。
    # 不要用"把设置也写一遍"来修它 —— 那正是这次要消灭的形状。
    # 钉住本会话（不是只重置）—— 见 `core/tiers.set_level`：只
    #   `reset` 的话，新档没地方记，`current_level` 会读回设置里的旧值，
    #   `/effort 4` 就变成一条什么都不做的命令。
    from core.tiers import set_level

    set_level(session_key, level)

    return tr("已切换，下一条消息起生效（仅当前对话）。")


def _handle_mode(parts: list, session_key: str) -> str:
    """`/mode` —— 看或改模式。

    四条规矩与 `/effort` 逐条相同：
    ① 当场生效，并说明这是冷启动（模式是提示缓存键的一部分）
    ② 只动本会话（六个通道共用一个 scheduler）
    ③ 不吞错（名字写错就原样拒绝，并列出可选值）
    ④ 设置坏了也能用（它就长在读设置那条路上）

    与 `/effort` 一处有意不同：未实现的模式在这里被拒 —— 列得出、
    设不了，绝不静默落进"选了但什么都没发生"。
    三种模式今天都已实现，所以那个分支走不到。它留着是因为它是判据，
    不是占位。
    """
    # `json` / `Path` / `SETTING_KEY` 都不再 import —— 落盘那一段已删（甲）。
    # `SETTING_KEY` is not imported — `/mode` no longer writes to disk.
    from core.modes import (MODE_NAMES, MODES,
                            ModeUnsupported, frozen_mode, is_implemented, mode_of,
                            set_mode)

    # 读现状：本会话已冻结的优先；还没冻结就报"设置里写着的"。
    # 不能用 `current_mode()` —— 那会把模式钉死（一个查询动作不该有副作用）。
    current = frozen_mode(session_key)
    shown = current if current is not None else MODES["manual"].key

    if len(parts) == 1:
        return (tr("当前模式：%s，使用/mode 模式名称 以更改模式")
                % MODE_NAMES[shown])

    want = parts[1].strip()
    try:
        key = mode_of(want)
    except ModeUnsupported as exc:
        return tr("命令错误，请查询模式名称")

    if not is_implemented(key):
        return tr("命令错误，该模式尚未实现")

    # `/mode` 不落盘。
    #
    # 把 `"default_mode": "plan"` 持久化进生产设置，会让以后每一段新对话
    # 都从只读模式开始，而且没有任何提示，而本函数承诺的是"只改这一段"
    # —— 标签在说谎。`/effort` 是同一个形状。
    #
    # 所以两个入口各管一件事：
    #   · 本会话（`core.modes._frozen`）—— 这里（`set_mode` 写它）
    #   · 持久默认 —— 只由设置页（`POST /api/settings` → `default_mode`）
    #
    # `/clear` 仍然不清模式：模式属于"这一段"，不属于"这次启动" ——
    #   只有重启才回默认。（`/clear` 恢复默认说的是投入档，那条走
    #   `reset_tier_cache`。）
    set_mode(session_key, key)
    return tr("已切换为%s。") % MODE_NAMES[key]


def _handle_btw(parts: list, ctx, session_key: str, pool) -> str:
    """`/btw <问题>` —— 问一句题外话，不打断主对话。

    三件事同时成立才叫"不打断"：① 答案不进历史（这条命令在
    pre_process 就 `stop`，主循环不跑，什么都不写进 messages）；
    ② 不调用工具（一次 `provider.chat`，没有循环）；③ 与主对话同前缀
    （复用它已有的缓存，而不是另起一段"干净"对话 —— 那样反而更贵）。
    """
    question = parts[1].strip() if len(parts) > 1 else ""
    if not question:
        return tr("命令错误，请在/btw 后附带问题")

    sid, msgs = pool.get_or_create(session_key, ctx.provider)
    ctx.session_id = session_key
    ctx.storage_id = sid
    from core import side_call

    return side_call.run(
        ctx.provider, msgs,
        session_id=session_key,
        directive=side_call.BTW_DIRECTIVE % question,
        header="#btw:",
        tag="btw",
    )


def _handle_recap(ctx, session_key: str, pool) -> str:
    """`/recap` —— 短暂回顾最近的对话。

    形态照框架自己的命令规范；内容是读会话笔记拼出来的，不调用模型。
    """
    sid, msgs = pool.get_or_create(session_key, ctx.provider)
    ctx.session_id = session_key
    ctx.storage_id = sid
    if not msgs:
        # 空会话没什么可回顾的 —— 省一次模型调用，而且这句话是真的。
        return "#recap-当前未开始对话"

    from core import side_call

    return side_call.run(
        ctx.provider, msgs,
        session_id=session_key,
        directive=side_call.RECAP_DIRECTIVE,
        header="#recap：",
        tag="recap",
    )


#: `/resume` 一次列几条 —— 够用，且不会把 IM 气泡撑爆。
_RESUME_LIMIT = 10


def _visible_threads(threads) -> list:
    """名册里该给人看的那几条 —— 与前端 `thread-list.js::visibleThreads` 同一条判据。

    0 轮且不是当前，不画。它们不是"对话"，是壳：
      · `/clear` 铸了新一段而你没说话；
      · 草稿态发送（`/api/chat` 的 `new` 先铸会话）那一轮失败/被中断，一行没落。
    左栏早就不画它们了，而 `/resume` / `/change` 的列表原来没过滤 ——
      于是微信里会冒出好几条「（没有可显示的摘要）（0 轮）」。
    编号必须按过滤后的表算（先滤后排），否则 `/resume 4` 会切到另一条。
    """
    return [t for t in (threads or [])
            if t.get("is_current") or int(t.get("turn_count") or 0) > 0]


def _render_thread_list(threads) -> str:
    """把线程名册画成 IM 能读的编号列表（新 → 旧，`1` = 最新）。"""
    lines = [tr("最近对话列表（新 → 旧，最多列出 %d 条）：") % _RESUME_LIMIT]
    for i, t in enumerate(threads, 1):
        mark = tr("● 当前") if t["is_current"] else "○"
        lines.append(tr("【%d】%s %s（%d 轮 · %s）") % (
            i, mark,
            t.get("hint") or tr("（没有可显示的摘要）"),
            t["turn_count"],
            t["started_at"][:16].replace("T", " "),
        ))
    lines.append(tr("输入 `/resume <编号>` 切换；切换不会删除或修改任何记录。"))
    return "\n".join(lines)


def _render_switchable_keys(numbering, pool) -> str:
    """把 `im_routing` 那张编号表渲染成人话（键 → 该会话最近一段的摘要）。"""
    lines = [tr("可以切换的对话：")]
    for n in sorted(numbering):
        key = numbering[n]
        title = ""
        try:
            th = pool.list_threads(key, limit=1)
            if th:
                title = th[0].get("hint") or ""
        except Exception:                                        # noqa: BLE001
            title = ""
        lines.append("%d. %s%s" % (n, title or key, ""))
    lines.append(tr("用 `/change <编号>` 切过去（这会成为本渠道的**默认接入**）。"))
    return "\n".join(lines)


def _handle_resume(parts, ctx, session_key: str, pool) -> str:
    """`/resume [编号]` —— 列出已归档线程 / 切回其中一条。

    这些对话本来就在库里（`/clear` 不再删行）；这条命令只是给它们一个入口。

    用户给的编号必须先经过列表，然后才把对应的 `storage_id` 交给
    `pool.resume()` —— 绝不把裸数字当 id 喂进去。`storage_id` 形如
    `session_<12 位十六进制>`，而 `resume` 不校验存在性（它只负责切），
    喂错就会静默造出一段空对话。
    """
    threads = _visible_threads(pool.list_threads(session_key, limit=_RESUME_LIMIT))
    if not threads:
        return (tr("这条通道还没有归档的对话。使用 `/clear` 之后，上一段会出现在这里。"))

    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        return _render_thread_list(threads)

    if not arg.isdigit():
        return ("用法：`/resume` 查看列表 · `/resume <编号>` 切换。\n"
                "请先查看列表再输入编号。")
    idx = int(arg)
    if not (1 <= idx <= len(threads)):
        return (tr("没有第 %d 条 —— 当前列表只有 %d 条，输入 `/resume` 查看。") % (idx, len(threads)))

    target = threads[idx - 1]
    if target["is_current"]:
        return tr("【%d】就是当前这一段，无需切换。") % idx

    sid, msgs = pool.resume(session_key, target["storage_id"])
    ctx.session_id = session_key
    ctx.storage_id = sid
    ctx.messages = msgs
    turns = sum(1 for m in msgs if m.get("role") == "user")
    return ("已切到【%d】：%s\n"
            "（%d 轮 · 开始于 %s）\n"
            "刚才那段对话没有被删除或修改，随时可以再切回。"
            % (idx, target.get("hint") or tr("（没有可显示的摘要）"), turns,
               target["started_at"][:16].replace("T", " ")))


def _turns_of(rows: list) -> list:
    """一条线程库内原始行里每一轮的起点下标。

    **入参必须是 `pool.thread_rows()`，不是 `ctx.messages`** —— 后者是
    渲染过的提示形状（历史被折成一个 `<conversation_history>` system 块），
    拿它的下标去切，切出来的东西和库里的行对不上。

    `kind == "history_summary"` 的行不算一轮 —— 它是压缩产物，而且
    它的 role 是 `user`（一律按 `kind` 辨认，绝不按 role）。不排掉它，
    编号从第一轮起就会错位一格。
    """
    return [i for i, m in enumerate(rows or [])
            if m.get("role") == "user" and m.get("kind") != "history_summary"]


def _excerpt(text, limit: int = 24) -> str:
    s = " ".join(str(text or "").split())
    return (s[:limit] + "…") if len(s) > limit else (s or "（空）")


def _post_thread_switch(ctx, session_key: str, sid: str, msgs: list) -> None:
    """换线程之后的阶段层收尾（与 `/clear` 同一判据）。

    `<git_status>` 与档位冻结都必须丢掉：两者都是"对话内冻结"
    （git 快照一变，从那个字节起可缓存前缀全部失效；档位是缓存键的一部分）。
    换线程就是换了一段对话，留着旧快照等于让新的一段顶着它跑。
    它们有意留在阶段层（不进 `purge_session`）—— 有两条测试钉着
    `/clear` 那一处，换线程走同一条路最省心。
    """
    from core.tiers import reset_tier_cache
    from core.prompt_assembler import reset_git_status_cache
    reset_git_status_cache()
    reset_tier_cache(session_key)
    ctx.session_id = session_key
    ctx.storage_id = sid
    ctx.messages = msgs


def _handle_fork(ctx, session_key: str, pool) -> str:
    """`/fork` —— 整条复制一份，你在副本上继续。

    复制全部消息、铸一个新的会话 id、切过去，原件一点不动。
    用途是"在尝试一个可能搞乱的方向之前，先存一个还原点"。
    """
    # 数的是库里的行（`thread_rows`），不是 `ctx.messages` ——
    #    后者是渲染过的提示形状，报出来的数会对不上。
    n = len(pool.thread_rows(session_key))
    sid, msgs = pool.fork_thread(session_key, keep=None)
    _post_thread_switch(ctx, session_key, sid, msgs)
    return (tr("已复制为新线程（%d 条消息），可以在这里继续。原对话保持不变，可用 `/resume` 切回。") % n)


def _handle_rewind(parts, ctx, session_key: str, pool) -> str:
    """`/rewind [轮号]` —— 退到第 N 轮之前。

    切点必须落在已完成的轮边界上 —— 否则上游返回 400（悬空的
    `tool_call` 之类）。判据复用
    `core/message_history.py::balanced_prefix_end()`。
    不平衡就拒，绝不往后夹 —— 宁可拒绝，也不静默把用户的锚点挪到
    别处。
    """
    # 走库里的原始行，不走 `ctx.messages` —— 后者是渲染过的提示形状
    #    （历史被折成一个 `<conversation_history>` system 块），拿它的下标
    #    去切会对不上。
    rows = pool.thread_rows(session_key)
    starts = _turns_of(rows)
    if not starts:
        return "#rewind-还没有可回退轮次"

    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        lines = ["#rewind-本段对话共%d：" % len(starts)]
        for n in range(len(starts), 0, -1):
            i = starts[n - 1]
            lines.append("%d. %s" % (n, _excerpt(rows[i].get("content"))))
        lines.append(tr("使用/rewind 编号 以回退至目标轮次之前"))
        return "\n".join(lines)

    if not arg.isdigit():
        return tr("命令错误，使用/rewind 编号 以使用")
    n = int(arg)
    if not (1 <= n <= len(starts)):
        return tr("命令错误，请检查编号")

    keep = starts[n - 1]
    from core.message_history import balanced_prefix_end
    prefix = rows[:keep]
    # 空列表必须单独判：`balanced_prefix_end([])` 返回 0，而 0
    #    同时也是"一条都不能剪"的信号。不特判的话，`/rewind 1`（退到最开始）
    #    会被误判成非法切点。
    ok = (not prefix) or balanced_prefix_end(prefix) == len(prefix)
    if not ok:
        return tr("回退错误-此切点会导致历史不完整，更换编号后尝试")

    sid, new_msgs = pool.fork_thread(session_key, keep=keep)
    _post_thread_switch(ctx, session_key, sid, new_msgs)
    # 文件那半的结论不进回执 —— `/rewind` 的话只有一句，不跟文件细节
    # （读者 `test_rewind_fork.py` 逐个情形钉着）。文件那一侧的原子性由
    # `apply_restore` 保证，它的逐条人话只留在函数返回值里。
    _restore_files(session_key, n)
    return tr("已回退到指定轮次前")


def _restore_files(session_key: str, before_turn: int) -> str:
    """把工作区文件也退到第 *before_turn* 轮之前。

    开关在设置页（`file_history.restore_enabled`），不在这里给参数。

    全批要么都过、要么一条都不写 —— 判据在
    `file_history.apply_restore`（任何一条过不了安全闸，整批停下）。
    "写一半再回滚"比"没写"更难向用户解释。
    永不抛 —— 文件这一半失败，不许把"对话已经退了"这件事一起吞掉。
    """
    try:
        from core import file_history as FH
        if not FH.restore_enabled():
            return (tr("ℹ️ 文件未改动 —— 文件回退功能请前往设置页更改"))
        plan = FH.plan_restore(session_key, before_turn)
        if not plan:
            return tr("ℹ️ 暂无文件变动")
        moved, lines = FH.apply_restore(session_key, plan)
        head = (tr("📄 文件：回退 %d 个") % moved) if moved \
            else tr("📄 文件未变更")
        return head + "\n" + "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return (tr("⚠️ 文件回退失败：%s: %s") % (type(exc).__name__, exc))


def _registered_tool_names():
    """当前已注册的工具名，供体检器的"重名"检查使用。

    拿不到就回 `None`，绝不回空集合 —— 两者含义不同：
      · `None`，体检器登记为"没查"
      · `set()`，体检器登记为"查过了，没有重名"
    读不到注册表却报"没有重名"，是一句假话。
    """
    try:
        from bus.registry import list_tools
        return set(list_tools())
    except Exception:  # noqa: BLE001
        return None


def _security_guard_or_none():
    """从已注册的 workspace manager 上取安全核（与 `core/shell.py` 同一套取法）。

    拿不到就回 `None` —— `load_skill_lazy` / `load_skill` 接受它
    （`security_guard` 形参本来可省）；`skill_loader` 仍会注册 schema，只是
    handler 拿不到 guard。不要因为取不到 guard 就跳过装载 —— 与
    "取不到会话号不该让工具不可用"是同一个取舍。
    """
    try:
        from bus.registry import get_workspace_manager
        return getattr(get_workspace_manager(), "security", None)
    except Exception:  # noqa: BLE001
        return None


def _handle_context_command(pool, session_key: str, messages) -> str:
    """`/context` —— 把上下文构成面板用文字说一遍。

    与面板同一个取数口：`pool.context_breakdown()` —— 正是
    `GET /api/context` 背后那条路。不许在别处再算一遍（三个消费者各自
    计算同一个量、给出三个值、而且都不报错，正是"唯一仪器"这条纪律要防止的
    失败模式）。

    它是只读的：不许碰 `_pending_memory_inject` —— 唯一的现算入口
    （`get_context()`）会吃掉那个标志，在这里碰它会让下一轮的注入静默少一次。

    `tokens is None` 渲染成 `-`，不是 `0` —— "读不到"不是零，而
    `0` 看起来像一个结论。
    """
    try:
        d = pool.context_breakdown(session_key, messages)
    except Exception as e:                                      # noqa: BLE001
        # 判据 4：兜底不许静默 —— "算不出来"与"算出来是 0"必须长得不一样。
        return f"[!] 读不出上下文读数：{type(e).__name__}: {e}"

    def _n(v):
        return "-" if v is None else format(int(v), ",")

    cells = d.get("cells") or []
    lines = ["Context 解析（括号内为与上次读数的差值）："]
    for c in cells:
        # 子集不参与总数，单独列出。混进去会重复计数。
        if c.get("subset_of"):
            continue
        delta = c.get("delta")
        mark = "" if delta in (None, 0) else ("（%+d）" % delta)
        lines.append("  %s：%s%s" % (c.get("label") or c.get("key"), _n(c.get("tokens")), mark))
    sub = [c for c in cells if c.get("subset_of")]
    if sub:
        lines.append("  不参与加总：" + "、".join(
            "%s %s" % (c.get("label") or c.get("key"), _n(c.get("tokens"))) for c in sub))
    lines.append("")
    pct = "-" if d.get("pct") is None else "%.1f%%" % float(d["pct"])
    lines.append(tr("当前占用 %s（%s / %s）· 压缩阈值 %s · 窗口来源 %s%s") % (
        pct, _n(d.get("estimated_tokens")), _n(d.get("window")),
        _n(d.get("threshold_tokens")), d.get("window_source") or "-",
        tr("，已进入预警区") if d.get("armed") else ""))
    lines.append(tr("（可在状态条查看同一份明细）"))
    return "\n".join(lines)


def _handle_skill_command(parts: list) -> str:
    """处理 /skill list|check|approve|reject|reload。

    操作 `skills/` 目录下的草稿 skill。

    `check` / `approve` 走静态体检（`ast.parse`，不执行任何东西）；
    `approve` 之后当场热加载。
    """
    import yaml
    from pathlib import Path

    from core import paths as _paths
    skills_dir = _paths.asset_dir("skills")
    if not skills_dir.is_dir():
        return tr("未找到目录")

    sub = parts[1].lower().strip() if len(parts) > 1 else "list"
    target = parts[2].strip() if len(parts) > 2 else ""

    # --- /skill list ---
    if sub == "list":
        lines = ["Skills:"]
        for d in sorted(skills_dir.iterdir()):
            if not d.is_dir():
                continue
            mf = d / "manifest.yaml"
            if not mf.exists():
                continue
            try:
                with open(mf, "r", encoding="utf-8") as f:
                    m = yaml.safe_load(f)
            except Exception:
                continue
            status = "[draft]" if m.get("draft") else "[active]"
            tools_n = len(m.get("tools", []))
            lines.append(f"  {status} {d.name} ({tools_n} tools) — {m.get('description', '')[:60]}")

        # 指令型 skill 单独列一段 —— 上面的循环只看得到带
        #   `manifest.yaml` 的目录，而用户放进来的 skill 多数只有
        #   `SKILL.md`，否则它们一个字都不显示。
        #   不要并进上面那张表：那张表每一行都有 `(N tools)`，而指令型
        #   skill 没有工具 —— 并进去会让它看起来像坏掉的工具提供者。
        from bus.loaders.skill_md import discover_instruction_skills
        from core.skill_sources import skill_roots
        instr = discover_instruction_skills(skill_roots(skills_dir))
        if instr:
            lines.append("  --- prompt 型（只有 SKILL.md，不提供工具）---")
            for s in instr:
                lines.append(f"  [prompt] {s.name} — {s.description[:60]}")
        return "\n".join(lines)

    # --- /skill reload ---
    #
    # 不需要 target —— 它重扫整个 `skills/`。
    # 这是生效那一步：体检是自动的（只读、不执行），而"新代码什么时候
    #   开始跑"必须是一次明确的动作。
    #   所以人手放进去的包走这条；模型造的包走 `/skill approve`（同一台机器）。
    if sub == "reload":
        from bus.loaders.skill_loader import reload_skills

        info = reload_skills(skills_dir, _security_guard_or_none())
        lines = [tr("已重载")]
        if info["failed"]:
            lines.append("[!] 失败：%s"
                         % "；".join("%s（%s）" % (n, e) for n, e in info["failed"]))
        # 被拦下的包必须出现在回执里。
        #   没有这一桶，用户看到的是"新装：（没有）/ 本来就在：…"，完全看不出
        #   有两个包被拒了，尽管记录里明明有。
        #   与"有变化是结果、跑过了是事实 —— 两件都得说"同一条判据。
        if info.get("quarantined"):
            lines.append("  [!] **被拦下**（体检不过 ⇒ 本次不装载）：%s"
                         % "、".join(info["quarantined"]))
        # 指令型 skill 不是失败 —— 它们属于另一层。把它们列出来，
        #   是为了不让"什么都没发生"成为用户看到的结果。
        if info.get("instructions"):
            lines.append(tr("  指令型（只有 SKILL.md，不注册工具）：%s")
                         % "、".join(info["instructions"]))
        lines.append(tr("★ 已加载的工具会保留：重复 reload 只更新定义，不会叠加，也不会把正在运行的工具换回占位实现。"))
        return "\n".join(lines)

    if not target:
        return tr("命令错误，使用/skill list|reload|check|approve|reject <skill_name>")

    mf = skills_dir / target / "manifest.yaml"
    if not mf.exists():
        return tr("未找到此目标")

    try:
        with open(mf, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
    except Exception as e:
        return tr("打开失败")

    # --- /skill check ---
    #
    # 走 `core/skill_doctor` 的静态分析。真 import 或子串检查有两个真问题：
    #   ① `importlib.import_module(f"skills.{target}.handler")` 是真的
    #      import，也就是执行第三人的代码 —— 而检查的全部意义恰恰是
    #      不执行它；
    #   ② `f"def {tn}(" not in h_content` 是子串检查 —— 注释或字符串里
    #      出现一次就能骗过去，而 `from X import *` 又会被误报成缺函数
    #      （`office_parser` 会报 54 条假阳）。
    # 所以 `ast.parse` 不执行任何东西，判的是"这个名字在模块命名空间里吗"。
    if sub == "check":
        from core import skill_doctor as _SD

        diag = _SD.examine(skills_dir / target,
                           registered_names=_registered_tool_names())
        if diag.ok:
            return tr("机检通过")

        # 文案走 `ui=False`（徽章换 ASCII）—— 命令回执会经过 CLI 通道，
        #    带符号那份在 GBK 控制台上会抛 `UnicodeEncodeError`
        #    （`scripts/check_gbk_safe.py` 钉着不增长的基线）。
        #    带符号那份是给 Web UI 弹窗的。
        return "机检不通过\n\n" + _SD.compat_message(diag, ui=False)

    # --- /skill approve ---
    if sub == "approve":
        if not manifest.get("draft"):
            return tr("此对象不是草稿")

        # 先体检 —— 不通过就不许生效。分工是：`check` 只验兼容性，
        # `reload` 才是真正的确认 —— 而确认之前总得先知道它行不行。
        from core import skill_doctor as _SD

        diag = _SD.examine(skills_dir / target,
                           registered_names=_registered_tool_names())
        if not diag.ok:
            return "体检未通过\n\n" + _SD.compat_message(diag, ui=False)

        manifest["draft"] = False
        try:
            with open(mf, "w", encoding="utf-8") as f:
                yaml.safe_dump(manifest, f, allow_unicode=True, default_flow_style=False)
        except Exception as e:
            return tr("保存失败")

        # 当场热加载 —— 否则 `approve` 之后还要重启框架，"热加载"这件事
        # 就等于没做。
        from bus.loaders.skill_loader import reload_skills

        info = reload_skills(skills_dir, _security_guard_or_none())
        failed = ""
        if info["failed"]:
            failed = "\n[!] 失败：%s" % "；".join(
                "%s（%s）" % (n, e) for n, e in info["failed"])
        return "已通过，重载成功" + failed

    # --- /skill reject ---
    if sub == "reject":
        import shutil
        if len(parts) > 2 and "--confirm" in " ".join(parts[2:]):
            shutil.rmtree(str(skills_dir / target))
            return tr("已删除")
        return tr("此对象为草稿，使用/skill reject %s --confirm 以删除") % target

    return tr("命令错误，使用list | check | approve | reject")


# 跨区审批处理器

# 统一审批派发器
#
# 跨区 / 删除 / 计划三条流程各自解析回答，而且都用精确字符串匹配：
#
#     text in ("1", "同意", "是", "可以", "行", "好", "ok", "yes")
#
# 于是提示语自己写的 `"1. 同意"` 认不出来：`active` 仍是 True，而且
# 什么都没注入 —— 模型收到一句裸文本，像回答被丢掉了。提示语列的每一行
# 都长成那个样子，所以这不是理论问题：提示语正在把用户引向一个会失效的
# 格式。
#
# 现在：只认数字（`core/approval.parse_reply`），三条流程共用。

#: 框架命令。
#:
#: 必须是显式命令集，绝不能写成"以 `/` 开头"：
#: `await_location` 阶段里 `/home/u/报告.pdf` 是合法回答 —— 用户是在告诉
#: 框架文件可能在哪儿。
# 匹配形状 —— 保持现有两个形状，不重新设计
#
# `MATCH_PREFIX` 是宽的：`/skillfoo` 会命中 `/skill`（裸
#    `startswith`）。这里不动它；收紧成整词是另一件事，混进来会让 diff
#    读不出对错。
#    注意：`_is_framework_command` 判的是整词，所以两边并不完全一致
#    （`/clearxyz` 在那边不是命令，在这边会走 `/clear` 分支）。收紧是候选
#    后续项。
MATCH_EXACT = "exact"          # `user_input == "/stop"`
MATCH_PREFIX = "prefix"        # `user_input.startswith("/skill")` (the broad shape)
MATCH_BOTH = "exact_or_prefix"  # `== "/resume"` or `startswith("/resume ")`

# 出口模式 —— 派发器调完 handler 之后做什么
CMD_TERMINAL = "terminal"      # handler 自己 `stop` + `return ctx`
CMD_REWRITE = "rewrite"        # handler 改写这一轮的消息，落回主循环


@dataclass(frozen=True)
class _CommandSpec:
    """一条框架命令 = 名字 + 说明 + 派发入口。

    这张表是唯一真相源：`_FRAMEWORK_COMMANDS`（有没有这条）·
    `_COMMAND_HELP`（说明）· `_COMMAND_UNIMPLEMENTED`（有没有实现）全部
    从 `_COMMANDS` 推导。三张各写各的字面量表，正是"命令被声明却没有分支、
    而且没人发现"的成因 —— 没有任何地方回答得了"这条有没有人实现"。

    `handler` 是方法名（字符串），不是函数对象：表在类外面，装不下
    未绑定方法。用字符串换来的是"这个名字存不存在可以按 AST 反查"，
    拼错立刻有测试失败，而不是等用户敲到那条命令才发现。
    """

    name: str
    help: str
    handler: str                      # 本模块那个类上的方法名
    match: str = MATCH_EXACT
    mode: str = CMD_TERMINAL
    implemented: bool = True

    def matches(self, text: str) -> bool:
        """保持现有形状，不重新设计（见 `MATCH_*` 常量）。"""
        t = (text or "").strip().lower()
        if self.match == MATCH_EXACT:
            return t == self.name
        if self.match == MATCH_PREFIX:
            return t.startswith(self.name)
        if self.match == MATCH_BOTH:
            return t == self.name or t.startswith(self.name + " ")
        raise ValueError("未知的匹配形状：%r（只认 MATCH_EXACT / MATCH_PREFIX / "
                         "MATCH_BOTH）" % (self.match,))


#: 表序就是派发序，而顺序承重。
#:
#: · `/model` 必须排在 `/mode` 前面 —— `"/model".startswith("/mode")` 是
#:   True，反过来 `/model` 永远到不了。
#: · `/mcp` 在最前是历史原因（它没有前缀冲突），原样保留。
#:
#: 所以加命令时要问"它会不会被前面某个 `prefix` 条目吃掉"，而不是"排在哪
#:   好看"。
#:
#: `implemented=False` 现在是空的，但这一格保留：它是"声明了却没
#:   实现"唯一的诚实出口（`_COMMAND_UNIMPLEMENTED` 从它推导）。
_COMMANDS: tuple = (
    _CommandSpec("/mcp", "管理 MCP 服务器", "_cmd_mcp", MATCH_PREFIX),
    _CommandSpec("/stop", "停止当前这一轮", "_cmd_stop"),
    _CommandSpec("/clear", "清空当前上下文，开始新对话", "_cmd_clear"),
    _CommandSpec("/compact", "压缩对话记录，释放上下文空间", "_cmd_compact"),
    _CommandSpec("/context", "查看当前上下文的构成与占用", "_cmd_context"),
    _CommandSpec("/skill", "检查与启用技能", "_cmd_skill", MATCH_PREFIX),
    # 这一条必须排在 `/mode` 前面（见本表抬头）
    _CommandSpec("/model", "查看或切换模型", "_cmd_model", MATCH_PREFIX),
    _CommandSpec("/change", "查看 / 切换接入的对话（/change [编号]）", "_cmd_change",
                 MATCH_PREFIX),
    _CommandSpec("/effort", "调整模型档位", "_cmd_effort", MATCH_PREFIX),
    _CommandSpec("/language", "切换模型的回答语言（仅本次对话）",
                 "_cmd_language", MATCH_PREFIX),
    _CommandSpec("/mode", "切换模式（手动 / 自动 / 计划）", "_cmd_mode",
                 MATCH_PREFIX),
    _CommandSpec("/btw", "顺带问一句，不进入主对话与历史", "_cmd_btw",
                 MATCH_PREFIX),
    _CommandSpec("/recap", "回顾最近的对话进度", "_cmd_recap"),
    _CommandSpec("/resume", "切换回以前的对话（/resume 查看列表）",
                 "_cmd_resume", MATCH_BOTH),
    _CommandSpec("/fork", "复制当前对话为新线程", "_cmd_fork"),
    _CommandSpec("/rewind", "退回到第 N 轮之前（对话与工作区文件一起回退）",
                 "_cmd_rewind", MATCH_BOTH),
    _CommandSpec("/remember", "把一句话展开为完整提示词后执行",
                 "_cmd_remember", MATCH_PREFIX, CMD_REWRITE),
)

#: 这三个名字一个没改 —— `_is_framework_command` ·
#: `_skill_command_of` · `framework_command_catalog` 及其测试都读它们，
#: 所以没有任何调用点需要搬。
#: 所以差别只在于它们现在是派生值，于是"表里有、代码里没有"在结构上不可能。
_FRAMEWORK_COMMANDS: frozenset = frozenset(c.name for c in _COMMANDS)
_COMMAND_HELP: dict = {c.name: c.help for c in _COMMANDS}
_COMMAND_UNIMPLEMENTED: frozenset = frozenset(
    c.name for c in _COMMANDS if not c.implemented)


def framework_command_catalog() -> list[dict]:
    """给"输入 `/` 弹候选"用的只读目录。

    用 `sorted()` 排序 —— 集合本身无序，顺序只是呈现问题。
    前端要按使用频率重排就自己排；后端不要再存一份命令名单。

    每行是 `{cmd, desc, implemented}`：
      · `desc` 必须非空（有断言钉着：新加命令没写说明会立刻失败）；
      · `implemented` 只有 `_COMMAND_UNIMPLEMENTED` 里的条目为假。
    """
    return [
        {"cmd": c,
         # 在这里翻：`_COMMAND_HELP` 是 import 时从 `_COMMANDS` 推导的
         #    模块级数据，`tr()` 写进那张表会在 import 那一刻定死语言。
         "desc": tr(_COMMAND_HELP.get(c, "")),
         "implemented": c not in _COMMAND_UNIMPLEMENTED}
        for c in sorted(_FRAMEWORK_COMMANDS)
    ] + [
        # 指令型 skill 也进 `/` 候选 —— 这是"把 skill 放进来就能用"
        #   在界面上的那一半。
        #   它们全部是 `implemented: true`：`instruction_command_map` 已经
        #     滤掉关闭的，能出现在这里的必然有派发分支（见下面的检查）。
        {"cmd": cmd,
         "desc": (s.description or "")[:120],
         "implemented": True}
        for cmd, s in sorted(_instruction_map_for_catalog().items())
    ]


def _instruction_map_for_catalog() -> dict:
    """目录用的那一份映射（`{"/名字": skill}`）。单独一个函数，是为了让
    目录路径与派发路径问同一个地方。"""
    from bus.loaders.skill_md import instruction_command_map
    return instruction_command_map(skill_roots(_SKILLS_ROOT), reserved=_FRAMEWORK_COMMANDS)[0]


def _is_framework_command(text: str) -> bool:
    """这条消息是不是一条框架命令（而不是审批回答）？

    判据是命令词本身，不是"以斜杠开头"；参数可以跟在后面
    （`/skill list`、`/mcp ...`）。
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return False
    if t.split(maxsplit=1)[0].lower() in _FRAMEWORK_COMMANDS:
        return True
    # 指令型 skill 也是框架命令。
    #   没有这一句，审批浮窗打开时，`/my-skill` 会被当成审批回答吞掉。
    return _skill_command_of(t) is not None


# 指令型 skill 命令
#
# 它们不进 `_FRAMEWORK_COMMANDS` —— 那是个字面量 `frozenset`，而 skill
#   是运行期发现的。合并发生在运行期，并且只住在一个函数里，调用方不必
#   各存一份。
# 扫描结果缓存在 `bus/loaders/skill_md.py`（每条消息扫一遍 `skills/` 是
#   不可接受的 —— 真实的 `SKILL.md` 可能有 36 KB），`/skill reload` 是
#   唯一的失效点。


#: 指令型 skill 的根。是常量，不是散落各处的字面量 —— 端到端测试必须
#: 能把它指向临时目录（否则只能污染生产的 `skills/`）。
#: 只读（常量），所以 state census 不管它。
from core.skill_sources import skill_roots  # noqa: E402
_SKILLS_ROOT = "skills"


def _skill_command_of(text: str):
    """`text` 是不是一条指令型 skill 命令？是就返回那个 skill，否则 `None`。

    判据是整词（第一个 token）—— 与 `_is_framework_command` 同一条。
    这也顺手挡住前缀撞车：`/model-x` 不会被 `/model` 那条
    `startswith` 吃掉（派发时先问这一句）。
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    tok = t.split(maxsplit=1)[0].lower()
    if tok in _FRAMEWORK_COMMANDS:
        return None                      # built-ins win (clashing skills are rejected at load)
    from bus.loaders.skill_md import instruction_command_map
    return instruction_command_map(skill_roots(_SKILLS_ROOT), reserved=_FRAMEWORK_COMMANDS)[0].get(tok)


def _skill_command_rejections() -> list:
    """被拒的指令型 skill：`[(名字, 原因)]`。供出声用 —— skill 绝不能
    被静默顶掉。"""
    from bus.loaders.skill_md import instruction_command_map
    return instruction_command_map(skill_roots(_SKILLS_ROOT), reserved=_FRAMEWORK_COMMANDS)[1]


def _is_web_session(ctx) -> bool:
    """这条消息是不是从 Web UI 来的？

    它只有一个用途：决定非数字回答怎么处理。数字契约本身不与通道
    绑定 —— 两边都只认数字。
    """
    try:
        return str(getattr(ctx.session, "platform", "") or "").lower() == "web"
    except Exception:
        return False


def _drop_pending(pend: dict) -> None:
    """结算完成，清空载体。

    `active=False; stage="done"` 会让 dict 仍为真，于是调度器轮末那句
    `if ctx.pending_cross_zone:` 永远成立，配套的 `elif ...: del` 是一个
    死分支（只增不减）。清空才能让它触发。
    """
    pend.clear()


#: 审批 kind -> `ctx` 上的载体字段名。顺序即优先级
#: （同一时刻最多只应有一个 `active`）。
_APPROVAL_KINDS = (
    ("cross_zone", "pending_cross_zone"),
    ("delete", "pending_delete"),
    # 批量删除的硬闸 —— 它自己占一格。
    # 绝不与上面的 `delete` 共用：共用是受守卫命令栽过三次的形状。
    #    规则：一格只许一个 kind 用。
    ("delete_bulk", "pending_delete_bulk"),
    # 出网与命令执行共用同一套派发/结算代码 —— 这正是重点：走现成的路，
    # 而不是各长一套流程。
    ("egress", "pending_egress"),
    ("command", "pending_command"),
    # 兜底闸：不可逆、而自身没有审批发射点的工具。
    ("destructive", "pending_destructive"),
    # 工作区之外写文件 —— 自己一格。
    # 绝不与 `command`/`egress` 共用：那两格批准的是一条命令/域名，而这一格
    #    批准的是目录（同一条规则：一格只许一个 kind 用）。
    ("write_outside", "pending_write_outside"),
    # 提问面板 —— 自己一格（同一条规则）。它排在计划之前：既不翻
    # 模式也不发授权（答案是结果），冲突面最小。
    ("ask_user", "pending_ask"),
    # 接管的逐应用授权。自己一格（同一条规则）。
    ("computer_app", "pending_computer_app"),
    # 外部命令钩子要求人工确认 —— 自己一格（同一条规则）。
    ("hook_ask", "pending_hook_ask"),
    # 计划批准。顺序即优先级，排最后。
    ("plan", "pending_plan"),
    # ICE 放行句柄背后的真值。排最后（顺序即优先级）。
    ("ice_reveal", "pending_ice"),
)


def _dispatch_approval(user_input: str, ctx):
    """审批回答的唯一入口。返回 `ctx`（框架接管）或 `None`（交给
    AgentLoop）。

    判定顺序是刻意的，每一步都有负对照：

    1. 框架命令先跳过 —— 否则 `/clear` 会被当成选项回答。（复现过：
       `stage=await_location` 时发 `/clear`，框架把"用户指定了搜索范围：
       /clear"注入给了模型。）
    2. 子状态接受自由文本 —— 框架此时在等一句话（位置 / 补充 / 反馈），
       数字规则不适用。判据按 stage 挂，不按通道挂。
    3. `stage="ask"` 走数字契约 —— 认出来就结算；认不出则分通道处理。

    超时触发的结算排在最前，先于其他一切：它不是用户回答，而是
    框架的决定（"没有可问的通道"），所以必须在"这像不像一条框架命令"
    之前判断。
    """
    # 0. 超时（`core/approval_timeout.py`）
    if _is_approval_timeout(ctx):
        for _store_kind, _attr in _APPROVAL_KINDS:
            _pend = getattr(ctx, _attr, None)
            if _pend and _pend.get("active"):
                return _settle_timeout(_pend, ctx)
        return None                      # 已经被答过了，什么都不做

    # 0b. 框架代笔的一轮不是"用户在回答"
    #
    #     后台任务回报 / 定时任务会以 `role=user` 落一条消息（调度器据此打
    #     `user_message_kind=framework_note`）。它不是用户打的字，所以它
    #     不构成任何回答。
    #
    #     不加这条时：模型弹出提问面板，同一时刻一条超时降级的后台命令跑完、
    #     回报进来 —— 那一轮把回报当成"用户补充的话"结算掉了提问
    #     （回执是「[用户没有选选项，补充了这些话] 后台任务回报 …」）。
    #     用户还没看选项，面板就没了，模型顺着一份它没答的问题继续往下写。
    #
    #     数字契约那几类同罪、而且更坏：一份回报落到 web 上会走
    #     "非数字回答 = 打断 + 补充"那一支，把挂着的审批静默撤掉。
    #
    #     位置在超时那一支之后：超时是框架自己的决定，与这一轮谁起的无关。
    if str(ctx.metadata.get("user_message_kind") or "") == "framework_note":
        return None

    if _is_framework_command(user_input):
        return None

    for store_kind, attr in _APPROVAL_KINDS:
        pend = getattr(ctx, attr, None)
        if not pend or not pend.get("active"):
            continue
        # 真正的 kind 取自载荷，不是取自格的键。
        #
        # 两种命令 kind 共用
        # `_pending_command` 一格，而选项表不同 —— 普通档的 `2` 是"同意，
        # 本对话内不再问此命令"，守卫档的 `2` 是不同意。
        #
        # 从格的键推导会同时坏两件事，而且都不出声：
        #   ① 结算器用错的选项表解析用户的 `2`，把拒绝读成同意
        #      （方向正好相反）；
        #   ② 授权发到错的 kind 上，重新提交时 `has_target_grant` 查的是
        #      另一个 kind，永远查不到，浮窗再出现，无限审批循环。
        #
        # "这一格可能装着哪个 kind"只有载荷自己知道 —— 调度器的
        # `pending_approval` 与 `emit` 都犯过同一个错误。
        kind = str(pend.get("kind") or store_kind)
        handler = _SUBSTATE_HANDLERS.get((kind, str(pend.get("stage") or "")))
        if handler is not None:
            return handler(user_input, pend, ctx)
        return _answer_ask(kind, pend, user_input, ctx)
    return None


def _audit_approval(kind: str, subject: str, key: str, ask_index: int,
                    outcome: str = "") -> None:
    """把一次审批结算写进审计日志。永不抛。

    ## 判据从哪来

    · outcome 取自选项表（唯一真相）—— `option.grant == GRANT_NONE`
      即 `rejected`；带 `stage` 的（自由文本项），`await_input`；
      其余（发了授权的），`approved`。
      绝不按数字推断：`2` 在 `cross_zone` 里是"缩小范围"，在
      `egress` 里是"本对话内不再问"，在 `delete_bulk` 里是拒绝 ——
      按数字推断会把用户的"不"记成"是"。
    · matchedRule 问 `wm.matched_target_grant` —— 在结算之后问，
      因为它记录的是"从现在起按哪条规则走"。

    审计失败不许影响结算（结算已经发生）—— 只印一行。
    """
    try:
        from core.approval import GRANT_NONE, find_option
        #: `outcome` 可以由调用方直接给（超时那条路传 `unavailable`）——
        #:   那条路没有数字键，按选项表推导会得到 `unknown`，而"未知"与
        #:   "没有可问的通道"是两件事。
        if not outcome:
            opt = find_option(kind, str(key), ask_index)
            if opt is None:
                outcome = "unknown"
            elif opt.grant == GRANT_NONE:
                outcome = "rejected"
            elif opt.stage:
                outcome = "await_input"
            else:
                outcome = "approved"
        rule = ""
        if outcome == "approved" and subject:
            try:
                from bus.registry import get_workspace_manager
                wm = get_workspace_manager()
                rule = (wm.matched_target_grant(kind, subject)
                        if wm is not None else "")
            except Exception:                                # noqa: BLE001
                rule = ""
        guard = _security_guard_or_none()
        if guard is not None:
            guard.record_approval(kind, subject, str(key), outcome, rule)
        print("[approval] %s %s（subject=%s key=%s rule=%s）"
              % (kind, outcome, subject[:60] or "-", key, rule or "none"))
    except Exception as exc:                                 # noqa: BLE001
        print("[approval] 审批审计写入失败（%s: %s）—— 结算本身已发生"
              % (type(exc).__name__, exc))


def _match_ask_options(text: str, options: list) -> tuple[list[dict], list[str]]:
    """把用户那句话对到选项上。纯函数。返回 `(对上的, 没对上的)`。

    两者必须分开 —— 这是"我想先聊聊"与"我打错了一个字"的区别：
      · 一个都没对上，自由文本
      · 对上了一部分，不结算，重新问（半句话不能算"选了那一个"）
    允许的写法：`1` / `1,3` / `1 3` / 完整标签 / 标签们。
    """
    tokens = [t for t in re.split(r"[,，、\s]+", str(text or "").strip()) if t]
    by_key = {str(o.get("key")): o for o in options}
    by_label = {str(o.get("label") or ""): o for o in options}
    matched: list[dict] = []
    unknown: list[str] = []
    for tok in tokens:
        hit = by_key.get(tok) or by_label.get(tok)
        if hit is None:
            unknown.append(tok)
        elif hit not in matched:
            matched.append(hit)
    return matched, unknown


def _answer_ask_user(pend: dict, user_input: str, ctx):
    """提问面板的回答。

    三种结果必须分得开：
      · 选了选项，`[用户的选择] …`
      · 没选、说了别的，`[用户没有选选项，补充了这些话] …`（不是拒绝）
      · 没人可问（超时 / cron / 子 agent），派发前就被挡掉，到不了这里
    """
    from core.ask_user import answer_text

    text = (user_input or "").strip()
    options = list(pend.get("options") or [])
    multi = bool(pend.get("multiSelect"))

    chosen, unknown = _match_ask_options(text, options)
    if chosen:
        if unknown:
            # 对上了一部分、另一些不在表里 —— 不猜，也不结算。
            return _ask_keep_pending(
                pend, ctx,
                tr("「%s」不在这次的选项里。回数字，或者直接说你自己的想法。")
                % "、".join(unknown))
        if not multi and len(chosen) > 1:
            return _ask_keep_pending(
                pend, ctx, tr("只能选择一个选项。"))
        keys = ",".join(str(o.get("key")) for o in chosen)
        return _SETTLERS["ask_user"](keys, pend, ctx)

    # 一个都没对上。纯数字 = 打错了键位（提示重来）；
    # 其余 = 用户想先聊（这正是那段回传文案定义的场景）。
    if unknown and all(t.isdigit() for t in unknown):
        return _ask_keep_pending(
            pend, ctx, tr("「%s」不在这次的选项里。回数字，或者直接说你自己的想法。")
            % "、".join(unknown))
    pend["_free_text"] = text or tr("（用户没有作答）")
    return _SETTLERS["ask_user"]("", pend, ctx)


def _ask_keep_pending(pend: dict, ctx, lead: str):
    """答非所问，挂起状态保持不变（替用户清掉等于替他放弃）。"""
    ctx.metadata["stop"] = True
    body = str(pend.get("body") or "")
    ctx.response = lead + ("\n" + body if body else "")
    return ctx


def _settle_ask_user(key: str, pend: dict, ctx):
    """结算一次提问：答案就是那次工具调用的结果（不重放工具）。

    *key* 是逗号连起来的选项键（`"1"` / `"1,3"`）；空串表示用户没选、
    说了别的 —— 那句原文在 `pend["_free_text"]` 里（由 `_answer_ask_user` 放）。

    与审批最关键的差别：审批的"同意"必须重放工具才真的做事；提问
    不用 —— 用户那句话本身就是产物。
    所以 `ctx.approval_replay["result"]`（不是 `deny`，也不是 `name/arguments`）。
    它不走 `_audit_approval` —— 那不是审批（没有授权可记）。把四种
    审计语义（approved/rejected/…）套在"用户选了一个选项"上只会说谎。
    """
    from core.ask_user import answer_text

    options = list(pend.get("options") or [])
    picked = {k for k in str(key or "").split(",") if k}
    selected = [o for o in options if str(o.get("key")) in picked]
    free_text = "" if picked else str(pend.get("_free_text") or "")
    question = {"question": str(pend.get("question") or ""), "options": options}

    #: 必须在 `_drop_pending`（即 `pend.clear()`）之前抓取。
    _replay = _replay_of(pend)
    text = answer_text(question=question, selected=selected, free_text=free_text)
    _drop_pending(pend)
    ctx.user_message = None
    #: 只保留 `call_id` + `result` —— 不要带 `name`/`arguments`：
    #:   那个三元组是"重放工具"的契约（`replay_pending_call` 读它），
    #:   而提问不重放。多带一对键会让下一个读者以为这里会跑一次工具。
    #: `fallback` 与 `result` 是同一句：找不到那条消息时，模型拿到的仍是答案。
    ctx.approval_replay = {"call_id": _replay.get("call_id", ""),
                           "result": text, "fallback": text}
    return None


def _settle_hook_ask(key: str, pend: dict, ctx):
    """结算一次「钩子要求人工确认」：`1` 同意 / `2` 不同意。

    与出网/命令最大的差别：不发任何授权。重放由
      `replay_pending_call` 直调工具 handler，而 `PreToolUse` 只挂在
      `_dispatch_generic_tool` 上，重放不走钩子、不会再挂起一次。
      所以这里没有"发一张一次性授权让它本轮能过"要做的事；多发一张只会是
      一张没人读的空头支票。
    """
    _replay = _replay_of(pend)
    _drop_pending(pend)
    if key == "1":
        msg = tr("[系统] 用户已同意执行这一步。请继续刚才的操作。")
        ctx.user_message = msg
        ctx.approval_replay = dict(_replay, fallback=msg)
        return None
    msg = "[系统] " + outcome_line(
        "rejected",
        tr("用户不同意执行这一步。请告知用户它**没有被执行**，"
           "不要重试、也不要换一个工具去绕过同一个限制。"))
    ctx.user_message = msg
    ctx.approval_replay = _deny_of(_replay, msg)
    return None


def _settle_computer_app(key: str, pend: dict, ctx):
    """结算一次接管的逐应用授权。

    * `1` = 本次对话允许，把这个 exe 记进接管会话（`session.authorize`），
      并重放那次工具调用（观察还没被消费，所以会照原参数再跑一遍）。
    * `2` = 同时永久批准，再写进 `data/allowlist.json` 的
      `computer_app` 段（设置页可以增删条目）。
      写入失败不影响本次对话那半 —— 先做会话授权，再报告"永久那条没写成"。
    * `3` = 不允许，那次调用收到拒绝，不重试。

    与其他审批最大的不同：授权真值不在 `ctx.target_grants` —— 它在接管
      会话里（`core/computer/session.py`）。⛔ 不要为了"统一"在这里再调
      `_grant_target`：那会造出第二份"这个应用批没批"的真相。
    """
    from core.computer import session as _cu

    exe = str(pend.get("subject") or "")
    app = str(pend.get("app") or exe)
    #: 必须在 `_drop_pending`（即 `pend.clear()`）之前抓取。
    _replay = _replay_of(pend)
    _drop_pending(pend)
    if key in ("1", "2"):
        try:
            _cu.authorize(exe)
        except Exception as exc:                     # noqa: BLE001
            # 接管已经关闭 / 状态拿不到，说清楚；绝不假装允许过。
            msg = "[系统] 没能允许控制「%s」：%s。请让用户重新启用接管。" % (app, exc)
            ctx.user_message = msg
            ctx.approval_replay = _deny_of(_replay, msg)
            return None
        extra = ""
        if key == "2":
            try:
                from core import allowlist as _al
                if _al.add("computer_app", exe):
                    extra = tr("，并已加入永久白名单")
                else:
                    extra = tr("（已在永久白名单中）")
            except Exception as exc:                 # noqa: BLE001
                extra = tr("；永久白名单写入失败（%s: %s），本次对话仍然允许") % (
                    type(exc).__name__, exc)
        msg = "[系统] 已允许 agent 控制「%s」（本次对话%s）。请继续刚才的操作。" % (
            app, extra)
        ctx.user_message = msg
        ctx.approval_replay = dict(_replay, fallback=msg)
        return None
    # 拒绝头只有一种拼法（同 `_settle_target`）。
    msg = "[系统] " + outcome_line("rejected", tr("用户不允许控制「%s」。") % app)
    ctx.user_message = msg
    ctx.approval_replay = _deny_of(_replay, msg)
    return None


def _answer_ask(kind: str, pend: dict, user_input: str, ctx):
    """`stage="ask"`：数字契约。"""
    from core.approval import (
        REPLY_BAD_CHOICE, REPLY_OK, parse_reply, render_prompt,
    )

    #: 提问面板不走数字契约 —— 它问的是意见，不是授权。任何回答都算数：
    #:   命中选项就是选择，没命中就是"先讨论一下"（措辞在
    #:   `core/ask_user.py::FREE_TEXT_NOTE`）。
    if kind == "ask_user":
        return _answer_ask_user(pend, user_input, ctx)

    text = (user_input or "").strip()
    #: 这是第几次问 —— 提示语按这个下标渲染（`options_for`），升级项从
    #: `ESCALATE_AT` 起才出现。计数由 `agent_loop` 在工具返回
    #: `pending_approval` 时推进（谁发问谁计数）。
    #:
    #: 按 kind 分：出网与命令都有 `escalate_at=2` 的升级项，共用一个
    #: 数字会让它们互相提前升级。
    ask_index = max(1, int(
        getattr(ctx, "approval_ask_counts", {}).get(kind, 0) or 1))
    verdict, key = parse_reply(text, kind, ask_index)

    if verdict == REPLY_OK:
        # 在结算之前抓取被审批物 —— 结算器会调 `pend.clear()`
        #    （`_drop_pending`），读晚了就是空串。
        _subj = str(pend.get("subject") or pend.get("file_list") or "")
        _res = _SETTLERS[kind](key, pend, ctx)
        _audit_approval(kind, _subj, key, ask_index)
        return _res

    if _is_web_session(ctx):
        # Web：非数字回答 = 打断 + 补充
        # 它是一条消息，不是一次选择。因此：不发授权（发了等于把"我还有话
        # 说"变成许可），清掉挂起状态，让模型带着补充重新判断。
        #
        # 正常界面走不到这条路 —— 审批挂起期间 `chat.html` 会禁用
        #    输入框，所以唯一的回答方式是浮窗（浮窗有自己的输入，走
        #    `expect === 'text'` 子状态）。
        #
        # 为什么堵在 UI 而不是这里：这条路没有错 —— 它表达的是"用户说了
        #   别的话"。错的是用户会走进来：他想回答"不同意"，而这一支只认数字，
        #   于是落进这里，模型被告知"根据补充重新判断"，又调了一次工具，
        #   看起来像死循环。用户看不出自己该去点浮窗，所以堵在入口。
        #
        # 这一段保留：它仍是 API 层唯一说得通的处理 —— 旧标签页、
        #   刷新到一半的页面、将来的其他客户端都可能打到它。UI 上的可达性
        #   不是删代码的理由。
        # 这是第二种语义（`cancelled`）。不做区分时它看起来与
        #   "rejected"一模一样（都只是框架说的一句中文），而模型该做的事
        #   正好相反：拒绝，停；打断，带着补充重做。
        _drop_pending(pend)
        ctx.user_message = "[系统] " + outcome_line(
            "cancelled",
            f"用户**打断**了本次审批，并补充：{text}。"
            f"请根据这条补充重新判断。**不要**当成已获得授权。")
        return None

    # 非 Web：回一句纠正，挂起状态保持不变
    # 保持是有意的：用户还没有回答，只是答非所问。替他清掉等于替他放弃；
    # 把裸文本转给模型，只会让它自由发挥。
    ctx.metadata["stop"] = True
    lead = (trf("「{key}」不在这次的选项里。", key) if verdict == REPLY_BAD_CHOICE
            else tr("请回复数字选项。"))
    ctx.response = lead + "\n" + render_prompt(kind, ask_index, footer=False)
    return ctx


# 结算（认出数字回答之后）
#
# 这里是唯一发授权的地方。其他地方（工具、内层）只消费能力。

# 审批回答不新开一轮
#
# 判据：同意 = 零消息 —— 模型只拿到工具结果，不知道中间发生过审批。
# 相反的形状是：工具返回 `pending_*`，回答变成下一轮的用户消息，框架还得写
# "[系统] 用户已同意……请重新提交" —— 那是我们在主动告诉模型。
#
# 所以结算器现在两样都给：
#   · `ctx.user_message = …`   — 退路（`AgentLoopStage` 补不成时使用）
#   · `ctx.approval_replay = …` — 正路（框架把那次调用的结果补上）
# 两样都写是有意的：结算器不知道那条工具消息还在不在（落库 / 装载 /
#    压缩都可能动过它），而"用户批了却什么都没发生"正是这套设计要消灭的
#    静默。补不成时，那句老回执必须仍然可用。
#
# 授权（`_grant_*`）仍先于重放发出 —— 重放依赖它们；顺序反了，重放
#    会再次撞上审批闸并二次挂起，死循环。

def _replay_of(pend: dict) -> dict:
    """同意：把"补哪一次调用"打包给 `AgentLoopStage`。

    `call_id` / `name` / `arguments` 在挂起那一刻从历史里取出
    （`_note_pending_call`）。三者缺一，重放补不成，使用 `fallback`
    （回执文本）。
    """
    return {
        "call_id": str(pend.get("call_id") or ""),
        "name": str(pend.get("tool_name") or ""),
        "arguments": pend.get("arguments") or "",
    }


def _deny_of(replay: dict, deny_msg: str) -> dict:
    """没批准：把实话回给那次调用（同样不是一条新的用户消息）。

    判据同上：拒绝是回给那次调用的答复，所以模型学到的是"这条路被挡了"，
    而不是"有人审过我"。

    键名 `deny` 是与 `AgentLoopStage` 的契约（改它要动三处），但它装载
    的不止"用户拒绝" —— ICE 的退赛（用户同意了，是框架没办成）也走这里。
    语义以 `deny_msg` 的那句头为准（头永远是四种固定结果之一）。
    """
    return {
        "call_id": str(replay.get("call_id") or ""),
        "deny": deny_msg,
        "fallback": deny_msg,
    }


#: 超时给模型的路线（`outcome_line` 的第二个参数）。全仓只有这一句
#: —— 它不按 kind 分：超时的语义与它来自哪一格无关，逐格各写一句就是八份
#: 需要维护的文案。
_TIMEOUT_ROUTE = (
    "这是一次**超时**：**没有人可问**（**不是**用户拒绝了这件事）。"
    "=> 换一条**不需要批准**的做法把这件事做完；"
    "确实没有别的做法时，把你要做什么、为什么需要它**写进回复告诉用户**，由他决定。"
)


def _is_approval_timeout(ctx) -> bool:
    """这一轮是不是由审批超时自己触发的？

    标记在 `event.extras` 里，不在消息正文 —— 用户可以在正文里打任何
    东西（从正文识别命令，正是"框架命令被当成选项回答"那个坑）。
    """
    evt = getattr(ctx, "event", None)
    extras = getattr(evt, "extras", None) or {}
    return bool(extras.get("approval_timeout"))


def _settle_timeout(pend: dict, ctx):
    """超时结算 —— 没有可问的通道，不是用户拒绝。

    语义走 `core/approval.py::outcome_line("unavailable", …)`（唯一一处）：
    "没有可问的通道；这不是用户的决定"。
    所以模型应当换一种做法，而不是停下。

    与 `rejected` 分开是承重的 —— 两条指令给模型的方向相反：
    一条说不要重试，另一条说换条路。合并会给模型一个确定的错误语义。
    """
    from core.approval import outcome_line
    _replay = _replay_of(pend)
    # 超时也要留痕 —— 这是框架做的决定，事后必须能回答"这条为什么
    # 没执行"。
    # 它不走 `_answer_ask`（那条路只处理数字回答），所以审计在这里自己写。
    _audit_approval(str(pend.get("kind") or ""),
                    #: 在 `_drop_pending`（`pend.clear()`）之前抓取。
                    str(pend.get("subject") or pend.get("file_list") or ""),
                    "-", 1, outcome="unavailable")
    _drop_pending(pend)
    ctx.user_message = "[系统] " + outcome_line("unavailable", _TIMEOUT_ROUTE)
    ctx.approval_replay = _deny_of(_replay, ctx.user_message)
    return None


def _settle_cross_zone(key: str, pend: dict, ctx):
    """跨区：1 同意 / 2 缩小范围 / 3 不同意 / 4 我有话说 /
    5 本对话内自动放行。"""
    hint = pend.get("hint", "")
    # 必须在这里抓（不能用到时再读）—— 下面每个分支都以
    # `_drop_pending(pend)` 开头，而它是 `pend.clear()`：清完之后"哪一次调用"
    # 就再也读不到了。
    _replay = _replay_of(pend)

    if key == "1":
        _drop_pending(pend)
        _grant_approval(ctx)
        ctx.user_message = (
            f"[系统] 用户已同意跨区搜索。请调用 request_cross_zone_search "
            f"工具（file_hint=\"{hint}\", scope=\"global\"）来执行全局搜索。"
        )
        # 上面这句话只是退路 —— 正常路径什么都不告诉模型，
        # 由 `AgentLoopStage` 把那次调用的结果补上。
        ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
        return None

    if key == "2":
        pend["stage"] = "await_location"
        ctx.metadata["stop"] = True
        ctx.response = tr("请告诉 Crannia 文件可能在什么位置？（例如：D盘、桌面、下载文件夹）")
        return ctx

    if key == "3":
        # 头是四种固定结果之一（`rejected`）—— 绝不在调用点临时编写。
        _drop_pending(pend)
        ctx.user_message = "[系统] " + outcome_line(
            "rejected",
            "用户拒绝了跨区搜索请求。请告知用户你无法在工作区外查找此文件。")
        # 拒绝也不变成新的用户消息 —— 它是回给那次调用的答复
        # （"这条调用被挡了"，不是"有人审过我"）。
        ctx.approval_replay = _deny_of(_replay, ctx.user_message)
        return None

    if key == "4":
        pend["stage"] = "await_other"
        ctx.metadata["stop"] = True
        ctx.response = tr("有什么需要告诉 Crannia 的？")
        return ctx

    if key == "5":
        # "本对话内自动放行" —— 升级项，从 `ESCALATE_AT` 起才出现。
        _drop_pending(pend)
        ctx.session_cross_zone_auto = True
        # 还必须同时发一次性授权：会话级标志要到下一轮开局才投影到
        # `wm`，而模型这一轮就会重新调工具 —— 只靠 `session_auto`，
        # 它现在仍会撞审批闸。
        _grant_approval(ctx)
        ctx.user_message = (
            "[系统] 用户已同意跨区搜索，并且要求**本次对话内不再询问**。"
            "请调用 request_cross_zone_search 工具执行全局搜索。"
        )
        # 同上 —— 这句只是退路。
        ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
        return None

    return None  # 到不了：`parse_reply` 只放行 `find_option` 认得的键


def _settle_delete(key: str, pend: dict, ctx):
    """删除：1 同意 / 2 同意且本对话内不再询问 / 3 不同意。"""
    # 在 `_drop_pending`（`pend.clear()`）之前抓取。
    _replay = _replay_of(pend)
    # 被审批物同样要在清空之前抓取 —— 工具在挂起那一刻算好并放进载荷
    #     （`delete_subject`，算法只有一处）。
    #     用户的"同意"批准的是这次删除，不是"那个工具"。
    _subj = str(pend.get("subject") or "")
    if key == "1":
        _drop_pending(pend)
        _grant_delete_approval(ctx, _subj)
        ctx.user_message = (
            "[系统] 用户已同意删除文件。请调用 delete_files 工具重新提交同样的文件路径，"
            "框架将执行删除操作。"
        )
        ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
        return None

    if key == "2":
        _drop_pending(pend)
        # "本对话内不再询问" —— 记在对话上，不是进程上。
        #
        # 如果在这里写 `wm.skip_delete_approval = True`（进程级全局），
        #    而没有任何地方把它改回 False，那么一次"2"之后，同一进程里
        #    以后每段对话都跳过询问，直到重启 —— 而面板承诺的是"本对话内"。
        # 跨轮状态由调度器在每轮开局投影（`_project_session_flags`）；本轮
        # 的重新提交由下面的一次性授权覆盖（轮末重置，只活一轮）。
        ctx.session_skip_delete = True
        _grant_delete_approval(ctx, _subj)
        ctx.user_message = (
            "[系统] 用户已同意删除文件，并且要求本次会话内不再询问。"
            "请调用 delete_files 工具重新提交文件路径，框架将直接执行删除。"
        )
        ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
        return None

    if key == "3":
        _drop_pending(pend)
        ctx.user_message = "[系统] " + outcome_line(
            "rejected", "用户拒绝了删除请求。请告知用户文件未被删除。")
        ctx.approval_replay = _deny_of(_replay, ctx.user_message)
        return None

    return None


def _grant_bulk_delete_approval(ctx) -> None:
    """发批量闸的一次性授权 —— 与 `_grant_delete_approval` 同形
    （带属主，轮末由 `reset_delete_approval` 清掉）。

    不要用 `_grant_delete_approval` 代替它 —— 那一格自动模式分类器
    也能写（`_pre_grant_for_auto`），写它等于给分类器开了一条绕过批量闸的路，
    而"分类器不能绕过批量闸"正是这个闸存在的理由。
    """
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            wm.grant_one_time_bulk_delete_approval(
                session_key_of_ctx(ctx))
    except Exception:
        pass


def _settle_delete_bulk(key: str, pend: dict, ctx):
    """批量删除硬闸：1 同意 / 2 不同意。

    为什么不复用 `_settle_delete` —— 两张表的键语义相反：
    `delete` 的 `"2"` 是"同意，本对话内不再询问"，而这里的 `"2"` 是
    "不同意"。复用会把拒绝读成同意，并种下
    `_session_skip_delete_approval`，闸从此失效。
    """
    # 在 `_drop_pending`（`pend.clear()`）之前抓取。
    _replay = _replay_of(pend)
    # 被审批物同样要在清空之前抓取（与 `_settle_delete` 同一条判据）。
    _subj = str(pend.get("subject") or "")

    if key == "1":
        _drop_pending(pend)
        # 两张通行证一起发（少一张 = 用户被问第二次）。
        #
        # · `_grant_bulk_delete_approval` —— 过批量闸；
        # · `_grant_delete_approval`     —— 过普通免问路径。
        #
        # 只发第一张的话，重放会走到"免问"分支，而
        #    `has_delete_approval()` 是假，又弹一次普通删除浮窗
        #    （`approval_kind` 从 `delete_bulk` 变回 `delete`）。用户的"同意"
        #    就是对这次删除的批准 —— 批量确认比普通确认更强（他还多看了
        #    一个数量），所以补发第二张不是放宽。
        # 两张都只活一轮（轮末由 `reset_delete_approval` 一起清）。
        _grant_bulk_delete_approval(ctx)
        # 第二张按被审批物发（与 `_settle_delete` 相同）。
        _grant_delete_approval(ctx, _subj)
        ctx.user_message = (
            "[系统] 用户已同意这次批量删除。请调用 delete_files 工具重新提交同样的"
            "文件路径，框架将执行删除操作。"
        )
        ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
        return None

    if key == "2":
        _drop_pending(pend)
        # 这里不是"不再询问" —— 在这张表里 `2` 表示"拒绝"。两张表
        #    在这个键上含义相反，所以结算器必须是两个函数。
        ctx.user_message = "[系统] " + outcome_line(
            "rejected", "用户拒绝了这次批量删除。请告知用户文件未被删除。")
        ctx.approval_replay = _deny_of(_replay, ctx.user_message)
        return None

    return None


# 出网 / 命令执行
#
# 两个域共用一个结算器。它们只在"批准之后告诉模型什么"上不同 ——
# 那是调用方传进来的两句话，不是两套实现。
#
# 被审批物在 `pend["subject"]` 里（域名 / 收件人 / 命令文本）。两半授权都是
# 按目标的；见 `WorkspaceManager.grant_target_once`。

def _settle_target(kind: str, key: str, pend: dict, ctx, *,
                   approve_msg: str, deny_msg: str, all_msg: str = ""):
    subject = str(pend.get("subject") or "")
    # 规则也必须在 `_drop_pending` 之前抓取 —— 那是 `pend.clear()`，
    #    读晚了就是空串（与下面的 `_replay` 同一个坑）。
    _rule = str(pend.get("rule") or "")
    # 在 `_drop_pending`（`pend.clear()`）之前抓取。
    _replay = _replay_of(pend)

    if key == "1":
        _drop_pending(pend)
        _grant_target(ctx, kind, subject)
        ctx.user_message = approve_msg
        # 这句话只是退路（那次调用补不成时使用）。正常路径由
        # `AgentLoopStage` 把结果补上 —— 模型只知道"它返回了"。
        ctx.approval_replay = dict(_replay, fallback=approve_msg)
        return None

    if key == "2":
        # "本对话内不再询问这个目标" —— 只加这一个（不是 `"*"`）。
        #
        # 对命令域来说，"这一个"是一条规则。两半有意不同，
        #    不要合并：
        #     · 一次性（本轮重新提交要能过），仍是命令原文 ——
        #       工具是在同一条串上问的，换成规则就对不上；
        #     · 会话载体（下一轮起生效），规则（`npm run *`）——
        #       用户要的正是"批准一个前缀，同类命令以后不再问"。
        #   规则为空串时退回原文（拿不准，整条精确匹配，与以前一样）。
        _drop_pending(pend)
        _grant_target(ctx, kind, subject)
        #   （规则由工具算好，随载荷带下来。）
        _grant_target_session(ctx, kind, _rule or subject)
        ctx.user_message = approve_msg
        ctx.approval_replay = dict(_replay, fallback=approve_msg)
        return None

    if key == "3":
        # 这是唯一接上"用户拒绝"头的地方 —— 出网 / 命令 / 兜底闸共用
        # 这个分支；它们的 `deny_msg` 动词不同，头是同一句。
        _drop_pending(pend)
        ctx.user_message = "[系统] " + outcome_line("rejected", deny_msg)
        ctx.approval_replay = _deny_of(_replay, ctx.user_message)
        return None

    if key == "4":
        pend["stage"] = "await_other"
        ctx.metadata["stop"] = True
        ctx.response = tr("有什么需要告诉 Crannia 的？")
        return ctx

    if key == "5":
        # 升级项："本对话内所有目标自动放行" —— 存成通配符 `"*"`。
        #
        # 会话级，绝不落盘。
        #
        # 写进 `data/allowlist.json` 而标签写着"本对话内"，会把一次点击
        # 变成永久放行，而用户以为它只活这一段对话。
        #
        # 所以走会话载体（`_grant_target_session`），`/clear` 一清就没。
        # 持久放行仍然存在，但只能由设置页创建（`data/allowlist.json`
        #    仍是唯一真相源；对话里的选项 5 只是不再往里写）—— 这样"永久"
        #    只能是用户在设置页明确做出的动作，不是弹窗里的一次点击。
        #
        # 两半都发，与选项 2 同一条规矩：
        #   · `_grant_target`         — 一次性、带属主，让本轮重新提交能过
        #   · `_grant_target_session` — 载体，下一轮起同类任何目标都能过
        _drop_pending(pend)
        _grant_target(ctx, kind, subject)
        # 一次性那一半也必须拿到通配符 —— 否则"本对话内全部放行"
        #    要到下一轮开局才生效，本轮里换一条命令照样弹窗。两半接力：
        #    本轮靠一次性授权（轮末被 `reset_target_grants` 消费掉），
        #    下一轮起靠载体投影。
        _grant_target(ctx, kind, "*")
        _grant_target_session(ctx, kind, "*")
        # 必须告诉模型实际发生了什么：这一档是"整类放行"，与选项 1/2
        #   的"这一条"不是一回事。
        # 三档共用一句话会完全漏掉"现在已经全部放行" —— 用户只能自己发现。
        ctx.user_message = all_msg or approve_msg
        ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
        return None

    if key == "6":
        # "信任此站点，不再询问" —— 全仓唯一一处由弹窗写入
        #    `data/allowlist.json` 的地方。
        #
        # 这是对一条旧规则的例外（"对话里点出来的东西不写那个文件"）。
        #    理由必须有正式记录，不能只留在代码注释中。
        #
        # fail-closed：只许 `egress`。其他 kind 走到这一支，说明
        #    选项表配错了（今天只有 `egress` 有键 6），拒掉这一次，
        #    什么都不记。宁可退化成"再问一次"，也不要持久放行一个从未被
        #    设计为可持久的东西（对 `command` 来说，持久化等于一张空头
        #    支票：它按整条命令精确匹配）。
        if kind != "egress":
            print(f"[approval] kind={kind!r} 收到选项 6（信任此站点）—— "
                  f"**只有 egress 有这一档**，这次不记，退回重问")
            return None
        _drop_pending(pend)
        # 两半都发（与选项 2 同一条规矩）：本轮重新提交能过 + 下一轮起生效。
        _grant_target(ctx, kind, subject)
        _grant_target_session(ctx, kind, subject)
        # 第三半：落盘 —— 这是与选项 2 唯一的区别。
        try:
            from core.allowlist import add as _wl_add
            _ok = _wl_add("egress", subject)
            # 没写进去也要出声：`add` 是幂等的（已存在返回 False），
            #   而"点了信任、下次还问"必须能一眼看出是哪种情况。
            print(f"[approval] 信任此站点：{subject} —— "
                  + ("已写进 data/allowlist.json" if _ok else "本来就在白名单里"))
        except Exception as exc:                                            # noqa: BLE001
            # 写盘失败不回滚：两半已经发出，用户这一下是有效的；
            #   回滚只会让他再点一次，而那次同样会失败。如实报告就够了。
            print(f"[approval] 信任此站点写盘失败（本次仍放行，但下次还会问）："
                  f"{type(exc).__name__}: {exc}")
        ctx.user_message = approve_msg
        ctx.approval_replay = dict(_replay, fallback=approve_msg)
        return None

    return None


def _settle_egress(key: str, pend: dict, ctx):
    """出网：1 同意 / 2 本对话内不再问此目标 / 3 不同意 / 4 我有话说 /
    5 全部放行 / 6 信任此站点（落盘）。

    `cross_zone` 的键含义不同（`2` = 缩小范围，`3` = 不同意）——
    不要跨表照抄 `1/2/3`；各表的定义在 `core/approval.py`。
    """
    return _settle_target(
        "egress", key, pend, ctx,
        # 模型必须重新发起请求：返回 `pending_*` 意味着实际什么都没发，
        #   所以"同意"之后还要再调一次（一次性授权正是为这一轮准备的）。
        approve_msg=("[系统] 用户已同意本次联网访问。"
                     "请**重新发起**刚才那个请求（同样的目标），框架会放行。"),
        # `deny_msg` 不带 `[系统] ` —— `_settle_target` 通过
        #    `outcome_line` 统一加那个前缀（头只在一处写）。
        deny_msg=("用户拒绝了本次联网访问。"
                  "请告知用户该请求**没有被发出**，不要重试、也不要换个目标重试。"),
        all_msg=("[系统] 用户已同意**本次对话内所有联网目标自动放行**。"
                 "请**重新发起**刚才那个请求。"
                 "⚠️ 这是**会话级**的 —— 换一段对话（`/clear`）就失效，"
                 "**没有**写进设置页的白名单。"),
    )


def _settle_destructive(key: str, pend: dict, ctx):
    """兜底闸：不可逆、而工具自身没有审批发射点的那些操作。

    键与出网/命令相同（走同一个 `_settle_target`，选项表照
    `COMMAND_OPTIONS` 写），但被审批物不同：那边是目标（来自工具参数的
    域名 / 命令），这里是工具名。不要把它读成"又一遍出网审批"。
    """
    return _settle_target(
        "destructive", key, pend, ctx,
        # 返回 `pending_*` 意味着工具没有执行，"同意"之后必须再调一次
        # （一次性授权正是为此准备的）。
        approve_msg=("[系统] 用户已同意执行这次不可逆的操作。"
                     "请**重新调用**刚才那个工具，框架会放行。"),
        # 同样不带 `[系统] `（头的写法在 `_settle_target`）。
        deny_msg=("用户拒绝了这个不可逆的操作。"
                  "请告知用户它**没有被执行**，不要重试、"
                  "也不要**换个工具**去达成同一件事。"),
        all_msg=("[系统] 用户已同意**本次对话内所有这类不可逆操作自动放行**。"
                 "请**重新调用**刚才那个工具。"
                 "⚠️ 这是**会话级**的 —— 换一段对话（`/clear`）就失效，"
                 "**没有**写进设置页的白名单。"),
    )


def _settle_command(key: str, pend: dict, ctx):
    """命令执行：与出网同形。"""
    return _settle_target(
        "command", key, pend, ctx,
        approve_msg=("[系统] 用户已同意执行这条命令。"
                     "请**重新提交**刚才那条命令，框架会执行它。"),
        # 同样不带 `[系统] `（头的写法见 `_settle_target`）。
        deny_msg=("用户拒绝了执行这条命令。"
                  "请告知用户命令**没有执行**，不要重试、也不要改写后再试。"),
        all_msg=("[系统] 用户已同意**本次对话内所有命令自动放行**。"
                 "请**重新提交**刚才那条命令。"
                 "⚠️ 这是**会话级**的 —— 换一段对话（`/clear`）就失效，"
                 "**没有**写进设置页的白名单。"),
        # 选项 2 放进会话载体的是规则（可能带前缀），不是命令原文。
        #    受守卫的命令拿不到前缀（对 `git …`、带路径、带重定向的，
        #    提取器一律返回空串），退回整条精确匹配。
    )


def _settle_write_outside(key: str, pend: dict, ctx):
    """工作区之外写文件：1 同意 / 2 本对话内不再问此目录 / 3 不同意 /
    4 我有话说 / 5 全部放行。

    与出网/命令共用 `_settle_target` —— 被审批物是一个目标
      （这里是目录），选项键完全相同（1/2/3/4/5）。

    关键：工具抛 `WriteNeedsApproval` 时一个字节都还没写
      （`validate_path` 在每条写路的最前面），"先挂起、后干活"在这里是
      结构上成立的，不是靠约定。

    因此"同意"之后框架重放那次调用 —— 用户看到的是事情直接做成，
      而不是"请再试一次"。
    """
    return _settle_target(
        "write_outside", key, pend, ctx,
        # 与出网的 `approve_msg` 同形：告诉模型重新发起那次调用。
        #    唯一重要的差别：这一条的授权是按目录发的，而重放走框架那条路
        #    —— 所以正常情况下模型不需要再发一次；这句话是退路。
        approve_msg=("[系统] 用户已同意把文件写到工作区之外的 `%s`。"
                     "请**继续刚才那一步**。"
                     % (str(pend.get("subject") or "") or tr("那个目录"))),
        deny_msg=(tr("本次没有写入任何文件。要写入该目录，可以：① 把产物放进工作区；② 在设置页把该目录加入工作区列表（长期有效）；③ 在对话中同意这一次。")),
        all_msg=("[系统] 用户已同意：**本次对话内**往工作区之外写文件不再询问。"
                 "请**继续刚才那一步**。"),
    )


def _settle_plan(key: str, pend: dict, ctx):
    """计划批准：1 同意执行 / 2 不同意并继续规划 / 3 自由文本项。

    它不复用 `_settle_target` —— 那一条的被审批物是"目标"
    （域名 / 命令 / 工具名），键的含义是"按目标授权"；而这里的"批准"动作是
    翻模式（退出计划模式）。复用会把键读反。

    退出时恢复进入前的模式，不是"直接继续" —— 用户可能是从另一个
      模式进来的。
    """
    # 在 `_drop_pending`（`pend.clear()`）之前抓取。
    _replay = _replay_of(pend)

    if key == "1":
        _drop_pending(pend)
        # ① 一次性授权 —— 让本轮的重放能过（退出工具会读它）。
        #    必须在这一步发：重放依赖它；顺序反了 = 重放撞审批闸 = 死循环。
        _grant_plan(ctx)
        # ② 翻模式：退出计划模式，回到进入前的模式。
        _leave_plan_mode(ctx)
        ctx.user_message = (
            "[系统] 用户已批准这份计划。请**按计划开始执行** —— "
            "计划模式的只读限制已经解除。"
        )
        # 这句话只是退路；正常路径由 `AgentLoopStage` 把那次调用的
        # 结果补上（模型只知道"它返回了"）。
        ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
        return None

    if key == "2":
        # 「不同意，继续规划」—— 留在计划模式里，把话带回去。
        # 清 pending 但不动模式：这正是与选项 1 的分界。
        _drop_pending(pend)
        ctx.user_message = "[系统] " + outcome_line(
            "rejected",
            "用户**不同意**这份计划。请继续规划 —— "
            "可以改计划文件，也可以问用户想怎么改。")
        ctx.approval_replay = _deny_of(_replay, ctx.user_message)
        return None

    if key == "3":
        pend["stage"] = "await_other"
        ctx.metadata["stop"] = True
        ctx.response = tr("有什么需要告诉 Crannia 的？")
        return ctx

    return None


def _grant_plan(ctx) -> None:
    """发计划批准的一次性授权，让本轮重放能过。

    它传的是会话键，而 `_settle_plan` 在 `ctx.session_id = sid`
    之前运行（派发器在 `process()` 最前面）。按不变量两者是同一个串，
    而重放会把 `ctx.session_id` 盖进快照，两边落在同一个 slug 上。
    """
    try:
        from bus.registry import get_workspace_manager
        from core.plan import grant_plan_approval
        grant_plan_approval(get_workspace_manager(), session_key_of_ctx(ctx))
    except Exception:
        pass


def _leave_plan_mode(ctx) -> None:
    """退出计划模式，回到进入前的模式。

    用 `core.plan.previous_mode`（记在会话上），不是硬编码 `manual`：
    用户可能是从另一个模式进来的。取不到时，那个函数会回落到默认模式。
    拿不到会话键，什么都不做（留在计划模式）—— 与"悄悄放行"相比，
    留在只读是安全的那一侧。真值记不下来时不要赌。
    """
    try:
        sid = session_key_of_ctx(ctx)
        if not sid:
            return
        from core.modes import set_mode
        from core.plan import previous_mode, reset_turn_index
        set_mode(sid, previous_mode(sid))
        # 轮次计数也要归零 —— 否则下次进入计划模式会接着上一段的数继续
        #    数（让稀疏提醒过早或过晚触发）。
        reset_turn_index(sid)
    except Exception as exc:
        print("[plan] 退出计划模式失败：%s" % exc)


def _grant_target(ctx, kind: str, subject: str) -> None:
    """发一次性授权（本轮有效、带属主），让本轮的重新提交能过。"""
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            wm.grant_target_once(kind, subject,
                                 session_key_of_ctx(ctx))
    except Exception:
        pass


def _grant_target_session(ctx, kind: str, subject: str) -> None:
    """把目标记进会话载体（调度器轮末持久化，轮首再投影回 `wm`）。

    不要直接写 `wm` 的会话集合：那是投影，而且本轮开局就已经投影完了。
    直接往里加会让"本轮的真值"与"载体的真值"分叉 —— 而 `/clear` 清的是载体，
    分叉之后清了也不生效。
    """
    carrier = getattr(ctx, "session_target_grants", None)
    if carrier is None:
        return
    carrier.setdefault(str(kind), set()).add(str(subject))


def _reset_session_cwd() -> None:
    """`/clear` 专用：把命令工作目录的投影清回空（= 回到工作区）。

    它清的是 `wm.session_cwd`，不是 `ctx` 字段 —— 这一格的写手是工具
    （`core/shell.py::run_command`），真值由调度器轮末从 `wm` 收回。清在写手
    写的那一格上，轮末那句 `elif session_key in …: del` 才会触发。
    """
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm is not None:
            wm.session_cwd = ""
    except Exception:
        pass


#: `(kind, stage) -> handler`。子状态 = 框架正在等一句自由文本。
_SUBSTATE_HANDLERS: dict[tuple[str, str], object] = {}


def _substate(kind: str, stage: str):
    def deco(fn):
        _SUBSTATE_HANDLERS[(kind, stage)] = fn
        return fn
    return deco


@_substate("ice_reveal", "await_other")
def _ice_await_other(text: str, pend: dict, ctx):
    """用户在 ICE 放行的自由文本项之后补充的那句话。

    这是一次带条件的放行，与 `cross_zone` 那个处理器不同：跨区补充
    不发授权（提供信息不等于允许搜索），而这里的用户通常是在说"可以，
    但只用在我说的那件事上" —— 所以要发授权（把真值记进本轮的放行表）。
    否则用户说"可以"也不起作用。

    与 `cross_zone` 同一条规矩：必须先调 `_drop_pending`（挂起即停车）；
    重放依赖 `ctx.approval_replay`，没有它结果就退化成一句回执。
    """
    from core.ice import reveal as _ice_reveal
    from core.session_key import session_key_of_ctx

    _replay = _replay_of(pend)
    handle = str(pend.get("subject") or "")
    sid = session_key_of_ctx(ctx)
    note = (text or "").strip()
    _drop_pending(pend)
    value = _ice_reveal.note_exempt_by_handle(sid, handle)
    if value is None:
        ctx.user_message = (
            "[系统] 用户补了一句话，但那个句柄**已经不在**框架这边了 —— "
            "没有值可以给你。请如实告诉用户，别猜。"
        )
    else:
        ctx.user_message = (
            "[系统] 用户补充了这句话：%s\n\n"
            "他同意把这个值的真实内容给你看 —— **这一轮**你会直接看到它。"
            "[!] 只在这一轮。请**结合他这句话**把事做完，别把它抄进回复。" % note
        )
    ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
    return None


@_substate("cross_zone", "await_location")
def _cz_await_location(text: str, pend: dict, ctx):
    """用户说了文件可能在哪儿 —— 这就是一次同意（他选了"缩小范围"）。

    这个子状态接受自由文本，所以 `/home/u/报告.pdf` 这样的路径是合法
    输入。框架命令在 `_dispatch_approval` 第 1 步被滤掉，到不了这里。
    """
    location = (text or "").strip()
    hint = pend.get("hint", "")
    _drop_pending(pend)
    _grant_approval(ctx)
    ctx.user_message = (
        f"[系统] 用户指定了搜索范围：{location}。请调用 request_cross_zone_search "
        f"工具（file_hint=\"{hint}\", scope=\"{location}\"）来执行搜索。"
    )
    return None


@_substate("cross_zone", "await_other")
def _cz_await_other(text: str, pend: dict, ctx):
    """进入 `await_other` 子状态之后用户补充的那句话。

    选项的标签文字有意不抄到这里 —— 有守卫要求标签字面量只出现在
    `core/approval.py`。这比"看一眼"严格，但正是它挡住了多份提示语各自漂移
    却没有任何症状。

    不发授权。提供信息不等于允许跨区搜索 —— 要不要搜，由模型拿到
    补充后自己判断。在这里发一次性授权，等于"随便说点什么都能拿到跨区权限"。
    """
    note = (text or "").strip()
    _drop_pending(pend)
    ctx.user_message = (
        f"[系统] 用户补充：{note}。请根据此信息重新决定是否需要跨区搜索。"
    )
    return None


def _other_await_other(text: str, pend: dict, ctx):
    """出网 / 命令 / 兜底闸 / 计划共用的 `await_other` 子状态。

    这是补上了一个真实的洞，不是顺手重构。四张选项表都带一项
    `stage="await_other"`（自由文本项），`core/approval.py` 也为它们各写了
    一句问题 —— 但 `_SUBSTATE_HANDLERS` 只注册了 `cross_zone` 那一个。

    症状：用户选了自由文本项，框架问"有什么需要告诉 Crannia 的？"，用户写完
    一句话，框架回"请回复数字选项。"

    用户被递了一个"随便说"的入口，走进去却被告知只认数字。退回 `_answer_ask`
    兜底时，Web 那边退化成"打断 + 补充"（勉强可用），IM 那边是死胡同。

    本 docstring 有意不写出那一项的标签原文 —— 它只许出现在
    `core/approval.py`（有断言会把这里的副本算作第四份重复）。

    与 `cross_zone` 的唯一差别是结尾那句问候语（"重新判断是否需要跨区
    搜索"），所以 `_cz_await_other` 仍是一个单独的处理器。

    不发授权（同 `_cz_await_other`）：提供信息不是批准。要不要做，
    由模型拿到补充后自己判断。
    """
    note = (text or "").strip()
    _drop_pending(pend)
    ctx.user_message = (
        f"[系统] 用户补充：{note}。请根据这条补充重新判断 —— "
        f"**不要**当成已获得授权。"
    )
    return None


_SUBSTATE_HANDLERS[("egress", "await_other")] = _other_await_other
_SUBSTATE_HANDLERS[("command", "await_other")] = _other_await_other
_SUBSTATE_HANDLERS[("destructive", "await_other")] = _other_await_other
_SUBSTATE_HANDLERS[("plan", "await_other")] = _other_await_other
# 工作区之外写文件也有 `stage="await_other"` 项（自由文本项），必须
# 登记，否则用户那句话没有处理器（有读者按 `_TABLES` 反查，漏登记会失败）。
# 不要把那句标签抄到这里：标签只住在 `core/approval.py`（有读者检查
# 字面量，重复会失败）。
_SUBSTATE_HANDLERS[("write_outside", "await_other")] = _other_await_other

#: `kind -> 结算函数`
#:
#: 每个 `kind` 必须映射到自己的函数 —— 不同表给同一个键不同的含义
#: （`cross_zone` 的 `2` 是"缩小范围"，`delete` 的 `2` 是"别再问删除"），
#: 共用一个函数会把它们读反。
def _settle_ice(key: str, pend: dict, ctx):
    """ICE 放行：1 这次给它 / 2 同意并降级本会话 / 3 不同意 / 4 自由文本。

    它不复用 `_settle_target` —— 那一条的键含义是"按目标授权"
    （`has_target_grant`），而这一条的后果是把真值记进本轮的放行表
    （`core/ice/reveal.py::_EXEMPT`），选项 2 还额外做了一件
    `_settle_target` 从不做的事（关掉本会话的信息层）。复用会把两件事搅在
    一起 —— 与 `_settle_plan` 分出去的理由相同。

    顺序与 `exit_plan_mode` 一致：一次性授权必须在重放之前发好，
    否则重放会再次挂起，逼用户点第二次。
    """
    from core.ice import reveal as _ice_reveal
    from core.session_key import session_key_of_ctx

    _replay = _replay_of(pend)
    handle = str(pend.get("subject") or "")
    sid = session_key_of_ctx(ctx)

    if key in ("1", "2"):
        _drop_pending(pend)
        value = _ice_reveal.note_exempt_by_handle(sid, handle)
        if value is None:
            # 句柄没了（映射被清 / 换了对话），如实说，绝不假装成功
            #   （"读不到"不是零）。与计划那条相同：补不成时退回老回执。
            #
            # 这是第四种语义（`recused`）唯一的落点 —— 用户点了
            #   同意，是框架没办成。走 `_deny_of` 会让模型看到
            #   "用户拒绝了" = 一个确定的错误语义。
            ctx.user_message = "[系统] " + outcome_line(
                "recused",
                "用户**同意**了，但那个句柄**已经不在**框架这边了"
                "（映射被清掉或属于别的对话）—— 所以没有值可以给你。"
                "请如实告诉用户，别猜。")
            ctx.approval_replay = _deny_of(_replay, ctx.user_message)
            return None
        if key == "2":
            # 关掉本会话的信息层（选项 2）。性质是授权，不是数据 ——
            #    `/clear` 会回收这个权限（`reveal.recover`）。
            _ice_reveal.downgrade(sid)
            _emit_ice_downgraded(sid)
        ctx.user_message = (
            "[系统] 用户同意把这个值的真实内容给你看 —— **这一轮**你会直接看到它。"
            "⚠️ 只在这一轮：下一轮起你又会看到句柄。"
            "用它把事做完，**别把它抄进回复**（回复里的句柄框架会替你换回真值）。"
        )
        ctx.approval_replay = dict(_replay, fallback=ctx.user_message)
        return None

    if key == "3":
        _drop_pending(pend)
        ctx.user_message = "[系统] " + outcome_line(
            "rejected",
            "用户**不同意**把它给你 —— 这一步不要做。"
            "如果这件事非它不可，如实说明并停下，**不要绕**。")
        ctx.approval_replay = _deny_of(_replay, ctx.user_message)
        return None

    if key == "4":
        pend["stage"] = "await_other"
        ctx.metadata["stop"] = True
        ctx.response = tr("有什么需要告诉 Crannia 的？")
        return ctx

    return None


def _emit_ice_downgraded(session_id: str) -> None:
    """说出"保护已降级"（标签不许说谎，界面必须一直看得见）。"""
    print("[ice] 信息层在**本会话**内已降级（用户 §6.9 选项 2）—— /clear 回收")
    try:
        from core.events import EVT_ICE_DOWNGRADED, get_event_bus
        get_event_bus().emit(EVT_ICE_DOWNGRADED, session_key=session_id)
    except Exception as exc:  # noqa: BLE001
        print("[ice] 降级事件发失败（%s: %s）" % (type(exc).__name__, exc))


_SETTLERS = {
    "cross_zone": _settle_cross_zone,
    "delete": _settle_delete,
    # 不能复用 `_settle_delete` —— 两张表的键语义相反
    # （见 `_settle_delete_bulk`）。
    "delete_bulk": _settle_delete_bulk,
    "egress": _settle_egress,
    "command": _settle_command,
    "destructive": _settle_destructive,
    # 工作区之外写文件 —— 与出网/命令同一种结算器形状
    # （被审批物是一个目标 = 目录）。
    "write_outside": _settle_write_outside,
    # 计划批准：键含义与上面四条都不同（这一条翻模式，不是发目标
    # 授权），独立函数，不并入 `_settle_target`。
    "plan": _settle_plan,
    # ICE 放行：键含义也不同（它记录"放行一个值"，而且选项 2 还会
    # 关掉本会话的信息层），独立函数。
    "ice_reveal": _settle_ice,
    # 提问面板。它不是审批 —— 没有授权可发；答案就是那次调用的结果
    # （见 `_settle_ask_user`）。
    "ask_user": _settle_ask_user,
    # 接管的逐应用授权 —— 键含义同样不同（授权真值在接管会话里，不在
    # `ctx.target_grants`），独立函数。
    "computer_app": _settle_computer_app,
    # 钩子要求确认 —— 不发授权（重放不走钩子），独立函数。
    "hook_ask": _settle_hook_ask,
}


def _grant_approval(ctx) -> None:
    """通过 WorkspaceManager 发一次性跨区授权。

    授权带属主（会话键）。授权存在 `wm` 上，而 `wm` 是进程单例 ——
    不记属主的话，会话 A 发的授权在会话 B 里也能用。`wm` 会拿它和本轮的
    `approval_scope` 比对；对不上就当没有（失败方向是安全的）。
    """
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            wm.grant_one_time_approval(session_key_of_ctx(ctx))
    except Exception:
        pass


def _grant_delete_approval(ctx, subject: str) -> None:
    """发一次性删除授权 —— 带属主。

    与 `_grant_approval`（跨区）同形。一个无身份保证的布尔只能靠锁 + 轮末
    清除：锁只防并发，而清除若不在 `finally` 里就不成立。现在两半都覆盖了
    （见 `WorkspaceManager.has_delete_approval()`）。

    *subject* = 这次调用的参数摘要，必须传。布尔通行证意味着"一次批准
    = 本轮内全部免问"，所以通行证必须绑定到具体目标。
    subject 为空 = 取不到目标，那张通行证谁都不放行
    （fail-closed 的一侧）。
    """
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            wm.grant_one_time_delete_approval(
                session_key_of_ctx(ctx), str(subject or ""))
    except Exception:
        pass

