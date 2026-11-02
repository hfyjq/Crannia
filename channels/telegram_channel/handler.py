"""
Telegram Channel — multi-user messaging via Telegram Bot API.

Uses a manual `getUpdates` polling loop instead of PTB's
`Updater` / `run_polling()`.  This avoids all the threading and
event-loop conflicts that prevent message dispatch when running
inside an existing asyncio context.

Architecture::

    while running:
        updates = await bot.get_updates(offset=offset, timeout=0)
        for update in updates:
            await app.process_update(update)   # dispatches to handlers
            offset = update.update_id + 1

所有 `/` 命令都进管线（`filters.COMMAND`）——
  以前只注册了 `/start` `/clear` `/compact` 三条，其余命令
  发过去什么都不发生（`filters.TEXT & ~filters.COMMAND` 把它们挡在门外），
  而管线的 `/effort` `/skill` `/btw` 那一族一条都没被叫到过。
入站媒体每种都有去处（照片 / 语音 / 音频 / 视频 / 视频留言 / GIF /
  贴纸 / 文档）—— 分派在 `media.items_of`，下载一个异常都不许漏出去。
出站媒体（`send_media_file`）—— `deliver_file` 因此在 Telegram 会话里
  也走直发，不再落到邮件兜底；取件排在回复之后（`core/delivery_outbox`）。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from channels.base import (
    Channel,
    ChannelMetadata,
    Message,
    MessageSession,
    MessageType,
)
from channels.converter import MessageConverter
from channels.message import MessageChain, Plain
from channels.telegram_channel import media as tg_media
from core import channel_turns

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_TEMP_DIR = Path("data/workdata/telegram_files")

#: 分片前缀 `(i/N)` 的预算（最长 `(9999/9999)\n` = 12 字符）。
#: 不扣这 12 个字符的后果：第一片正好 4096 时 `前缀 + 正文` 超限，
#: Telegram 直接拒收 —— 整条回复发不出去，而日志里只有一行 400。
_CHUNK_PREFIX_BUDGET = 12

#: 轮询游标（`update_id + 1`）的落点 —— 运行时数据，进 `data/runtime/`。
_OFFSET_PATH = Path("data/runtime/telegram_offset.json")


# ------------------------------------------------------------------
# TelegramMessageConverter
# ------------------------------------------------------------------

class TelegramMessageConverter(MessageConverter):
    """Translate between Telegram `Update` and `MessageChain`.

    `to_internal` extracts text, caption, photo, and document from
    a `telegram.Update` (the raw message from PTB).

    `to_platform` converts a `MessageChain` back to a dict
    representation that the channel's send methods understand.
    """

    # -- platform → internal -----------------------------------------

    def to_internal(
        self,
        platform_message: Any,
        media: list[tuple[dict, str]] | None = None,
    ) -> MessageChain:
        """Convert a `telegram.Update` into a MessageChain.

        *media* 是已经下好的 `[(item, local_path)]`（`media.items_of` 的
        形状，路径由通道的 `_download_media` 填）—— 这里只负责排进链子。

        本方法不 import `telegram`：鸭子类型就够（字段用 `getattr` 取），
          所以没装 SDK 的测试也能跑；而"没装 SDK 就整条丢空"是旧写法的副作用。
        """
        tg_msg = getattr(platform_message, "message", None)
        if tg_msg is None:
            return MessageChain()

        components: list = []
        #: 文字 / 图注（媒体消息也常带 caption）—— 排在媒体之前，
        #: 与钉钉 / 飞书那两条通道同一个顺序。
        text = getattr(tg_msg, "text", None) or getattr(tg_msg, "caption", None) or ""
        if text:
            components.append(Plain(str(text)))
        for item, local_path in (media or []):
            comp = tg_media.component_of(item, local_path)
            if comp is not None:
                components.append(comp)
        return MessageChain(components) if components else MessageChain()

    # -- internal → platform -----------------------------------------

    def to_platform(self, message_chain: MessageChain, **kwargs: Any) -> dict[str, Any]:
        """Convert a MessageChain to a dict for Telegram send methods.

        Returns a dict with keys like `text`, `photo_path`,
        `doc_path`, `doc_name`.

        今天只有 `to_internal` 那条路在用（入站）；出站走的是
        `send_media_file`（本地文件直接交给 Bot API），不再经过这个 dict。
        留着它是 `MessageConverter` 的契约，别在别处按它写第二套。
        """
        result: dict[str, Any] = {"text": message_chain.content}

        for img in message_chain.images:
            result["photo_path"] = img.local_path
            if img.mime_type:
                result["photo_mime"] = img.mime_type
            break  # Only one photo per message (Telegram limitation)

        for f in message_chain.files:
            result["doc_path"] = f.local_path
            result["doc_name"] = f.name
            if f.mime_type:
                result["doc_mime"] = f.mime_type
            break  # Only one doc per message

        return result


# ------------------------------------------------------------------
# TelegramChannel
# ------------------------------------------------------------------

class TelegramChannel(Channel):
    """Telegram Bot adapter — manual getUpdates loop, no PTB Updater."""

    def __init__(
        self,
        config: dict | None = None,
        scheduler=None,
        session_pool=None,
    ) -> None:
        super().__init__(config)
        self._converter = TelegramMessageConverter()
        self._scheduler = scheduler
        self._pool = session_pool
        self._running = False
        self._app = None
        self._bot = None

        tg_cfg = (config or {}).get("channel", {}).get("telegram", {})
        self._bot_token = tg_cfg.get(
            "bot_token",
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        )
        #: 代理从一处口径取（`core/net_proxy` 的三档），而不是"读环境变量"。
        #:   真取值发生在 `run()` 里（`_resolve_proxy()`）—— 这里只放一个初值，
        #:   好让"构造完就能看到它是什么"这件事成立（自检 / 日志用）。
        self._proxy_url = self._resolve_proxy()
        self._polling_timeout = tg_cfg.get("polling_timeout", 30)

        #: 机器人自己的身份（`run()` 拿到 `get_me` 之后填）—— 群聊唤醒门要用。
        self._bot_username = ""
        self._bot_id = None
        #: 群聊里什么时候才回：`mention`（默认，@ / 回复机器人）· `any`（每条都回）。
        #:   默认与 Telegram 自己的隐私模式一致；`any` 是给"把机器人当群助手"的人留的。
        #:   读不到/写错，回默认（不是拒启：这是一个口味轴，不是能力轴）。
        _gt = str(tg_cfg.get("group_trigger", "mention") or "mention").strip().lower()
        self._group_trigger = _gt if _gt in ("mention", "any") else "mention"

        _TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Channel ABC ----

    def meta(self) -> ChannelMetadata:
        return ChannelMetadata(
            name="telegram_channel",
            description="Telegram Bot adapter — multi-user messaging",
            platform_type="telegram",
            support_multimodal=True,
        )

    def _resolve_proxy(self) -> str:
        """每次 `run()` 现取代理 —— 与微信一致（它在 `run()` 里建 client）。

        为什么不能只在 `__init__` 里取：
          通道实例是进程内长活的（`bus.registry` 里那一份），
          `__init__` 只在应用启动时跑一次。若在那里定死，
          「先启动通道 → 之后才开代理」时，连"停止通道再启动"都换不过来
          （拿到的还是同一个实例），只能重启整个应用 —— 而症状是
          界面绿框"运行中"、IM 里发消息没有任何回应（最贵的那种形状）。
        优先级：通道自己的 `proxy` 配置 > 全局三档；`direct` 档下**连通道级
          配置也让开**（那正是自救路）。
        """
        tg_cfg = (self.config or {}).get("channel", {}).get("telegram", {})
        from core import net_proxy as _np
        mode, url = _np.state()
        if mode == "direct":
            base = ""
        elif mode == "manual":
            base = url
        else:
            base = os.environ.get("HTTPS_PROXY", "")
        return tg_cfg.get("proxy", "") or base

    async def run(self) -> None:
        self._proxy_url = self._resolve_proxy()
        """Manual getUpdates loop — all inside our event loop."""
        if not self._bot_token:
            print("[telegram] ERROR: No bot token configured.")
            return

        # Proxy
        #: 不再往 `os.environ` 里塞（那会污染同进程里所有其他出网方，
        #:   包括模型 provider —— 一处配错全线遭殃）。改成只作用于这个 bot。
        from telegram.ext import ApplicationBuilder
        builder = ApplicationBuilder().token(self._bot_token)
        if self._proxy_url:
            from core.net_proxy import redact as _redact
            print(f"[telegram] Using proxy: {_redact(self._proxy_url)}")
            builder = builder.proxy(self._proxy_url).get_updates_proxy(self._proxy_url)
        app = builder.build()

        try:
            await app.initialize()
            await app.start()
        except Exception as e:
            print(f"[telegram] ERROR: Cannot start — {type(e).__name__}: {e}")
            print("[telegram] Check your token, proxy, and network.")
            return

        self._app = app
        self._bot = app.bot

        # Verify connection
        try:
            me = await app.bot.get_me()
            #: 机器人自己的身份 —— 群聊唤醒门要用（`media.addressed_to_bot`）。
            self._bot_username = str(getattr(me, "username", "") or "")
            self._bot_id = getattr(me, "id", None)
            print(f"[telegram] Connected as @{me.username} (id={me.id})")
        except Exception as e:
            print(f"[telegram] WARNING: get_me failed — {type(e).__name__}: {e}")

        # ---- Register handlers on the application ----
        from telegram.ext import MessageHandler, filters

        channel = self  # closure capture

        #: 回调只做转发 —— 真逻辑在 `self._handle_update` / `self._reply_update`
        #: （类方法才测得了：闭包只能在 `run()` 里活一次）。
        #: 回答不在这里发了 —— `_handle_update` 只入队就返回，
        #:   回答由 worker 线程回头调 `_reply_update`（本条的 `on_reply`）。
        #:   旧写法是 `_reply_update(update, await _handle_update(update))`，
        #:   而 `_handle_update` 里 `await execute` 整轮不回头，轮询循环
        #:   连下一条 `get_updates` 都发不出去（"IM 打不断"的真形状）。
        async def _handler(update: Any, context: Any) -> None:
            """文本 / 所有 `/` 命令 —— 都走同一条处理链。"""
            await channel._handle_update(update)

        async def _media_handler(update: Any, context: Any) -> None:
            """照片 / 语音 / 音频 / 视频 / 视频留言 / GIF / 贴纸 / 文档。"""
            if not update.message:
                return
            media = await channel._download_media(update.message)
            await channel._handle_update(update, media=media)

        # 一条命令都不许漏：`filters.COMMAND` 兜住所有 `/xxx`。
        #   旧写法只注册了 `/start` `/clear` `/compact` 三条 ——
        #   `/effort` `/skill` `/btw` `/recap` `/language` 那一族发过去
        #   什么都不发生（而它们全都在管线的 pre_process 里实现好了）。
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, _handler,
        ))
        app.add_handler(MessageHandler(filters.COMMAND, _handler))
        app.add_handler(MessageHandler(
            filters.PHOTO | filters.VOICE | filters.AUDIO | filters.VIDEO
            | filters.VIDEO_NOTE | filters.ANIMATION | filters.Sticker.ALL
            | filters.Document.ALL,
            _media_handler,
        ))

        # ---- Main polling loop ----
        print("[telegram] Bot is running (manual getUpdates loop).")
        self._running = True
        #: 游标从盘上接回来 —— 否则重启会把最近未确认的那批 update
        #:   再处理一遍（去重缓存重启后是空的，拦不住）。
        offset = _load_offset()
        if offset:
            print("[telegram] 从上次的游标接着收（update_id >= %d）" % offset)

        while self._running:
            retries = 0
            try:
                updates = await app.bot.get_updates(
                    offset=offset,
                    timeout=0,
                    allowed_updates=["message"],
                )
                retries = 0  # reset on success
                await asyncio.sleep(0.8)  # rate-limit
            except Exception as e:
                err_name = type(e).__name__
                if "TimedOut" in err_name or "Timeout" in str(e):
                    await asyncio.sleep(1)
                    continue
                retries += 1
                wait = min(2 ** retries, 30)
                print(f"[telegram] {err_name}, retry in {wait}s (attempt {retries})")
                await asyncio.sleep(wait)
                continue

            moved = False
            for update in updates:
                offset = update.update_id + 1
                moved = True
                try:
                    await app.process_update(update)
                except Exception as e:
                    print(f"[telegram] process_update error: "
                          f"{type(e).__name__}: {e}")
                    # 游标照推（这条算丢），但必须说出来
                    #   （「丢，提醒用户『消息遗失，未接受』」）。
                    #   不推游标，一条坏消息会被无限重投，整条通道卡死；
                    #   推了却不说，用户看见的是"我发了，它没理我"。
                    await self._notify_lost(update)
            #: 一批处理完才落盘（每条都写会把盘写穿）；写失败它自己会出声。
            if moved:
                _save_offset(offset)

    async def _notify_lost(self, update: Any) -> None:
        """一条 update 没能处理成，在那个会话里明说「消息遗失，未接受」。

        不抛：这条路本来就是失败路径，再抛一次会连累同一批后面的
        update（`process_update` 那层会继续下一轮）。
        """
        chat_id = None
        try:
            chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
        except Exception:                                  # noqa: BLE001
            chat_id = None
        if chat_id is None or self._bot is None:
            print("[telegram] 这条消息没处理成，且没有可用的会话回话 —— 只能记日志")
            return
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text="⚠️ 消息遗失，未接收，需要时请重新发送。")
        except Exception as e:                             # noqa: BLE001
            print(f"[telegram] 「消息遗失」提醒没发出去："
                  f"{type(e).__name__}: {e}")

    async def terminate(self) -> None:
        self._running = False
        if self._app:
            try:
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                pass
        print("[telegram] Bot stopped.")

    # ------------------------------------------------------------------
    # 入站：一条 update → 管线 → 回答
    # ------------------------------------------------------------------

    async def _handle_update(self, update: Any,
                             media: list[tuple[dict, str]] | None = None
                             ) -> str | None:
        """Core processing: convert update → pipeline → response.

        *media* 是已经下好的 `[(item, path)]`（媒体那条路先下载再进来）。
        Returns the response text (or None on empty / error).
        """
        if not getattr(update, "message", None) or not getattr(update, "effective_user", None):
            return None

        user = update.effective_user

        chain = self._converter.to_internal(update, media=media)

        # Skip completely empty chains (silent messages)
        if chain.is_empty:
            return None

        session = self._make_session(update)

        # 群聊唤醒门：群里默认只回「@ 我」与「回复我」。
        #    判据见 `media.addressed_to_bot`（实体 / 回复 / 文本兜底三种信号）。
        #    机器人身份一个都拿不到时（`get_me` 失败）放行 ——
        #      判不出来就别拦：漏回一条比误吞一条更糟。
        if (session.message_type is MessageType.GROUP
                and self._group_trigger != "any"
                and (self._bot_username or self._bot_id is not None)
                and not tg_media.addressed_to_bot(
                    getattr(update, "message", None),
                    self._bot_username, self._bot_id)):
            print("[telegram] 群里这条不是在跟我说话，跳过（chat=%s）"
                  % session.session_id)
            return None

        log_text = chain.content[:80] if chain.content else "[media]"
        print(f"[telegram] msg from {user.full_name or user.username}: {log_text}")

        message = Message(
            sender_id=str(user.id),
            sender_name=user.full_name or user.username or "",
            message_chain=chain,
            message_type=session.message_type,
            session=session,
            #: 平台消息号 —— 管线用它做去重（`pre_process` 那道闸读的
            #:   就是 `ctx.metadata["message_id"]`）。不传 = 那道闸恒不触发。
            message_id=str(getattr(update.message, "message_id", "") or ""),
        )
        event = self.commit_event(message)

        # Typing indicator
        try:
            await update.message.chat.send_action("typing")
        except Exception:                                         # noqa: BLE001
            pass

        if not self._scheduler:
            return None

        #: 只入队，立刻返回。回答由 worker 线程投回**本通道的
        #:   loop** 上发（`on_reply`）—— 客户端绑在这个 loop 上，别新造循环。
        #:   异常那句人话也由 `core/channel_turns` 统一发（同一个 worker 线程）。
        async def _on_reply(text: str) -> None:
            await self._reply_update(update, text)

        await channel_turns.intake(self, event, on_reply=_on_reply)
        return None

    async def _reply_update(self, update: Any, response: str | None) -> None:
        """发回答，然后把本轮登记的文件发出去。

        顺序是判据（`core/delivery_outbox`）：用户先看到那句话，再收到附件；
          取件只取自己这一轮的（三格全对）。
        """
        # 取件放在 `finally` 里：发送这一段抛了（429 / 网络 / 实体错），
        #    文件也照样要发 —— 否则"模型说要交付"与"用户收到"是两件事。
        try:
            if response and getattr(update, "message", None):
                await self._send_long(update.message, response)
        finally:
            try:
                await self._drain_delivered(self._make_session(update))
            except Exception as e:                                # noqa: BLE001
                print(f"[telegram] 发待交付文件时出错（{type(e).__name__}: {e}）")

    async def send(self, session: MessageSession, content: str | MessageChain) -> bool:
        """没有 Bot 就是没送达 —— 以前这一支什么都不做、也不出声。

        异常照旧往上抛（`Runner` 那条 `except` 会把它记成失败）；
        这里只把"静默什么都不做"那一支改成一个明确的 `False`。

        发完之后顺手把本轮登记的文件取走发掉 —— 定时任务 / 后台回报
          这条路上，那是文件唯一的出口（同微信 / 飞书 / 钉钉）。
        """
        if not self._bot:
            print(f"[telegram] 没有 Bot 实例，这条没有送达：{session.session_id}")
            return False
        chat_id = _chat_id_of(session)
        if chat_id is None:
            print(f"[telegram] 会话 id 不是数字，发不出去：{session.session_id!r}")
            return False
        text = content if isinstance(content, str) else content.content
        delivered = False
        if str(text or "").strip():
            await self._send_long_via_bot(chat_id, str(text))
            delivered = True
        files = await self._drain_delivered(session)
        return delivered or files > 0

    async def send_note(self, session: MessageSession, text: str,
                        event=None) -> bool:
        """一行旁白（"已收到" / "已中断"）—— 不捎带本轮待发文件。

        为什么不直接用 `send()`：它会取走 `core/delivery_outbox` 里
        本轮待交付的文件，一轮还在跑的时候附件就被提前发出去了
        （用户先看到文件、再看到回答）。判据见 `channels/base.py::send_note`。
        """
        if not self._bot:
            print(f"[telegram] 没有 Bot 实例，这句旁白没送达：{session.session_id}")
            return False
        chat_id = _chat_id_of(session)
        if chat_id is None or not str(text or "").strip():
            return False
        await self._send_long_via_bot(chat_id, str(text))
        return True

    async def test_connection(self) -> dict:
        """连通性自检（通道页 / 引导页用）。

        判据：配置在 + SDK 在就算"可接入"；
          真连一次网络是 `run()` 的事（这里不替用户打请求）。
        """
        if not self._bot_token:
            return {"ok": False, "message": "Telegram bot_token 没配置"}
        try:
            import telegram  # noqa: F401
        except ImportError:
            return {"ok": False, "message": "python-telegram-bot 没装"}
        if self._bot is not None:
            try:
                me = await self._bot.get_me()
                return {"ok": True, "message": "Connected as @%s" % me.username}
            except Exception as exc:                              # noqa: BLE001
                return {"ok": False, "message": "%s: %s"
                        % (type(exc).__name__, exc)}
        return {"ok": True, "message": "Telegram SDK available"}

    # ------------------------------------------------------------------
    # 出站媒体（`MediaSendCapable`）
    # ------------------------------------------------------------------

    async def send_media_file(self, session: MessageSession, file_path: str,
                              filename: str = "",
                              context_token: str = "") -> bool:
        """发一个本地图片/文件。真交出去才回 `True`（的回执契约）。

        *context_token* 是微信那条协议的概念，Telegram 不用它 —— 保留形参
        是为了与能力协议（`channels/capabilities.py::MediaSendCapable`）同一个
        签名，`deliver_file` 那侧按属性存在认通道。
        """
        if not self._bot:
            print("[telegram] 没有 Bot 实例，附件发不出去")
            return False
        p = Path(str(file_path or ""))
        if not p.is_file():
            print("[telegram] 要发的文件不存在：%s" % (file_path or "（空）"))
            return False
        chat_id = _chat_id_of(session)
        if chat_id is None:
            print(f"[telegram] 会话 id 不是数字，附件发不出去：{session.session_id!r}")
            return False
        name = filename or p.name
        kind = tg_media.outbound_kind(name)
        try:
            from telegram import InputFile
            with open(p, "rb") as fh:
                upload = InputFile(fh, filename=name)
                if kind == "photo":
                    await self._call_with_retry(
                        self._bot.send_photo, chat_id=chat_id, photo=upload)
                else:
                    await self._call_with_retry(
                        self._bot.send_document, chat_id=chat_id,
                        document=upload)
        except Exception as exc:                                  # noqa: BLE001
            print(f"[telegram] 附件发送失败（{type(exc).__name__}: {exc}）")
            return False
        print("[telegram] 附件已发送：%s（%s）" % (name, kind))
        return True

    async def _drain_delivered(self, session: MessageSession) -> int:
        """把本轮 `deliver_file` 登记的文件发出去，返回真发出去的条数。

        判据见 `core/delivery_outbox`：工具是同步的、不能 await 这个循环，
        所以它只登记；通道在发完回复之后取走发掉，失败时回一句话给用户。
        三格都要对上：`session_key + channel + user_id`。
        """
        from core import delivery_outbox as _outbox
        from core.session_key import session_key_for

        try:
            recs = _outbox.take_for(
                session_key=session_key_for(session),
                channel="telegram_channel",
                user_id=str(getattr(session, "session_id", "") or ""))
        except Exception as exc:                                  # noqa: BLE001
            print(f"[telegram] 取待发文件失败（{type(exc).__name__}: {exc}）")
            return 0
        sent = 0
        for rec in recs:
            if await self.send_media_file(session, rec.get("path", ""),
                                          rec.get("name", "")):
                sent += 1
                continue
            chat_id = _chat_id_of(session)
            if chat_id is None:
                continue
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text="文件 %s 没能发出去（原因在框架日志里）。"
                         % (rec.get("name") or "?"))
            except Exception:                                     # noqa: BLE001
                pass
        return sent

    # ------------------------------------------------------------------
    # 入站媒体
    # ------------------------------------------------------------------

    async def _download_media(self, tg_msg: Any) -> list[tuple[dict, str]]:
        """把这条消息的媒体下到本地，返回 `[(item, 本地路径)]`。

        一个异常都不许漏出去：漏出去整条消息连管线都进不去，
          用户看到的是"这条消息不存在"（飞书那次的真现场）。取不到就
          跳过这一件，其余照常 —— 并且说出来。
        20 MB 是 Bot API `getFile` 的硬上限，超了连下载地址都拿不到
提前判，别把它报成"下载失败"。
        """
        out: list[tuple[dict, str]] = []
        for item in tg_media.items_of(tg_msg):
            file_id = str(item.get("file_id") or "")
            if not file_id:
                print("[telegram] 媒体没有 file_id，跳过（kind=%s）" % item.get("kind"))
                continue
            size = int(item.get("size") or 0)
            if size > tg_media.MAX_DOWNLOAD_BYTES:
                print("[telegram] 附件太大（%d 字节 > %d），没有下载：%s"
                      % (size, tg_media.MAX_DOWNLOAD_BYTES, item.get("name")))
                continue
            try:
                tg_file = await self._bot.get_file(file_id)
                path = _TEMP_DIR / ("%s_%s%s" % (
                    item.get("kind") or "file", file_id[-12:],
                    tg_media.ext_of(item)))
                await tg_file.download_to_drive(path)
                out.append((item, str(path)))
                print("[telegram] 附件已落盘：%s（%s，%d 字节）"
                      % (path.name, item.get("kind"), size))
            except Exception as exc:                              # noqa: BLE001
                print(f"[telegram] 取附件失败（{type(exc).__name__}: {exc}）")
        return out

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_session(self, update: Any) -> MessageSession:
        from telegram.constants import ChatType
        chat = update.effective_chat
        if chat.type == ChatType.PRIVATE:
            return MessageSession("telegram", MessageType.PRIVATE, str(chat.id))
        return MessageSession("telegram", MessageType.GROUP, str(chat.id))

    async def _call_with_retry(self, fn, *args, **kwargs):
        """调一次 Bot API；撞上 429 就等它说的秒数，重试一次。

        `RetryAfter` 必须处理：Telegram 的 per-chat 限速是真实存在的 ——
        一段长回复切成十几片时就会撞上。不处理的话症状是"最后几片没了"，
        而日志里只有一行 429。
        """
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:                                  # noqa: BLE001
            wait = _retry_after_of(exc)
            if wait is None:
                raise
            print("[telegram] 被限流，%.0f 秒后重试一次" % wait)
            await asyncio.sleep(wait)
            return await fn(*args, **kwargs)

    async def _send_long(self, message: Any, content: str) -> None:
        """Send, splitting long messages（前缀也算进 4096 的预算）。"""
        for prefix, chunk in _split_for_telegram(content):
            await self._call_with_retry(message.reply_text, prefix + chunk)

    async def _send_long_via_bot(self, chat_id: int, content: str) -> None:
        """Send via bot (for proactive messages)."""
        for prefix, chunk in _split_for_telegram(content):
            await self._call_with_retry(
                self._bot.send_message, chat_id=chat_id, text=prefix + chunk)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _load_offset(path: Path | None = None) -> int:
    """读回上次的 `update_id + 1`。读不到 / 坏文件，`0`（出声，不静默）。

    为什么它非有不可：`getUpdates` 的 offset 只活在进程里。重启后从 0 开始，
      Telegram 会把最近未确认的那批 update 再发一遍（最多 24 小时），
      而去重缓存（5 分钟 TTL）重启后是空的，用户会收到重复的回复。
    """
    p = Path(path) if path is not None else _OFFSET_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except Exception as exc:                                      # noqa: BLE001
        print("[telegram] 游标读不出来，从 0 开始（可能重放积压）：%s: %s"
              % (type(exc).__name__, exc))
        return 0
    try:
        return max(0, int((data or {}).get("offset") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _save_offset(value: int, path: Path | None = None) -> None:
    """把游标写回（先写临时文件再 `replace`，半个文件不会留在那儿）。"""
    p = Path(path) if path is not None else _OFFSET_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps({"offset": int(value)}), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:                                      # noqa: BLE001
        # 写不进去也要出声：否则下次重启静默重放
        print("[telegram] 游标写不进去（重启会重放积压）：%s: %s"
              % (type(exc).__name__, exc))


def _retry_after_of(exc: BaseException) -> float | None:
    """异常里的 `retry_after` 秒数；不是限流就 `None`。

    按属性认，不 import `telegram.error` —— 没装 SDK 也能测。
      PTB 各版本给的是 `int` 或 `timedelta`，两种都认。
    """
    value = getattr(exc, "retry_after", None)
    if value is None:
        return None
    try:
        secs = value.total_seconds() if hasattr(value, "total_seconds") else value
        return max(0.0, float(secs))
    except (TypeError, ValueError):
        return None


def _split_for_telegram(text: str) -> list[tuple[str, str]]:
    """切成 `(前缀, 正文)`，两段加起来 ≤ 4096。

    旧写法先按 4096 切、再加 `(i/N)` 前缀，第一片正好 4096 时超限，
      Telegram 拒收，整条回复发不出去（日志只有一行 400）。
    """
    s = str(text or "")
    if len(s) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return [("", s)]
    chunks = _split_text(s, TELEGRAM_MAX_MESSAGE_LENGTH - _CHUNK_PREFIX_BUDGET)
    n = len(chunks)
    if n <= 1:
        return [("", chunks[0] if chunks else s)]
    return [("(%d/%d)\n" % (i + 1, n), c) for i, c in enumerate(chunks)]


def _chat_id_of(session: MessageSession) -> int | None:
    """会话 id → Telegram 的 chat id。不猜：不是整数就回 `None`。

    回 `None` 而不是抛：调用方（`send` / `send_media_file`）要把它变成
      一个明确的 `False`（没送达要说得出来），而不是炸到调度器里。
    """
    try:
        return int(str(getattr(session, "session_id", "")))
    except (TypeError, ValueError):
        return None


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
