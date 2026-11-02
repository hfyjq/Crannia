"""
FeishuChannel — Feishu (Lark) IM adapter via lark-oapi WebSocket.

Uses the lark-oapi SDK's ws.Client (protobuf-framed WebSocket) in a
dedicated thread to avoid event-loop conflicts with `asyncio.run()`.
REST API calls (send message / upload / download) go through the main loop
(or a worker thread when the caller is already synchronous).

Message flow::

    Feishu WS (worker thread) → _FeishuEventHandler._do_without_validation
    → _dispatch_event（只投递，不阻塞 WS）
    → _handle（主循环上的协程）：转换 → 取附件 → 提交事件 → 管线
    → reply_callback（线程）→ _send_* → lark REST API

Requires: pip install lark-oapi
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import mimetypes
import threading
from pathlib import Path
from typing import Any

from channels.base import (
    Channel, ChannelMetadata, Message, MessageEvent,
    MessageSession, MessageType,
)
from channels.message import MessageChain
from channels.feishu_channel import media as feishu_media
from core import channel_turns

#: 附件落盘目录（与微信那条同一个习惯：放工作区旁边，给人看得见）。
_FEISHU_TEMP_DIR = Path("data/workdata/feishu_files")

#: 上传上限（飞书侧也有；先自己拦，报错才说得清是哪一边）。
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 30 * 1024 * 1024


# ------------------------------------------------------------------
# FeishuChannel
# ------------------------------------------------------------------

class FeishuChannel(Channel):
    """Feishu (Lark) channel adapter via WebSocket long connection.

    Config keys (in config.yaml → channel.feishu):
        - app_id: Feishu app ID
        - app_secret: Feishu app secret
    """

    def __init__(self, config: dict | None = None, **deps) -> None:
        super().__init__(config)
        self._scheduler = deps.get("scheduler")
        self._session_pool = deps.get("session_pool")
        self._ws_client = None
        self._running = False
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._client = None                       # 懒建（下载 / 上传 / 发送共用）
        feishu_cfg = (self.config or {}).get("channel", {}).get("feishu", {})
        self._app_id = feishu_cfg.get("app_id", "")
        self._app_secret = feishu_cfg.get("app_secret", "")

    # ------------------------------------------------------------------
    # Channel ABC
    # ------------------------------------------------------------------

    def meta(self) -> ChannelMetadata:
        return ChannelMetadata(
            name="feishu_channel",
            description="Feishu (Lark) IM via WebSocket",
            platform_type="feishu",
            # 收：图 / 文件 / 语音 / 视频 / 表情都取得到字节；发：图 / 文件。
            support_multimodal=True,
            # 没有流式：飞书那条路今天一次发一条完整消息（`meta` 里
            #    这一格以前写 True，而全仓零读者 —— 那是一句没人兑现的承诺）。
            support_streaming=False,
        )

    async def run(self) -> None:
        """Start WebSocket listener in a dedicated thread."""
        if not self._app_id or not self._app_secret:
            print("[feishu] WARNING: app_id/app_secret not configured. "
                  "Set FEISHU_APP_ID and FEISHU_APP_SECRET in .env")
            print("[feishu] Channel will idle — add config to start.")
            while True:
                await asyncio.sleep(3600)

        self._main_loop = asyncio.get_event_loop()
        self._running = True
        print(f"[feishu] Starting WebSocket listener (app_id={self._app_id[:8]}...)")

        # lark_oapi.ws.Client.start() is synchronous and blocks with its own
        # event loop.  We MUST run it in a dedicated thread so it doesn't
        # conflict with asyncio.run() in main.py.  The lark_oapi import is
        # also deferred to the thread so the module-level `loop` variable
        # captures the worker thread's (empty) loop.
        def _run_ws() -> None:
            import lark_oapi as lark  # first import in this thread
            handler = _FeishuEventHandler(self)
            self._ws_client = lark.ws.Client(
                app_id=self._app_id,
                app_secret=self._app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
            )
            try:
                self._ws_client.start()
            except Exception as e:
                if self._running:
                    print(f"[feishu] WebSocket error: {e}")

        thread = threading.Thread(target=_run_ws, daemon=True)
        thread.start()
        print("[feishu] WS thread started, waiting for events...")

        # Keep the coroutine alive
        while self._running:
            await asyncio.sleep(1)

    async def terminate(self) -> None:
        """Graceful shutdown."""
        self._running = False

    async def test_connection(self) -> dict:
        """Check if Feishu API is reachable."""
        if not self._app_id:
            return {"ok": False, "message": "Feishu app_id not configured"}
        try:
            import lark_oapi as lark  # noqa: F401
            return {"ok": True, "message": "Feishu SDK available"}
        except ImportError:
            return {"ok": False, "message": "lark-oapi not installed"}

    # ------------------------------------------------------------------
    # REST（发送 / 上传 / 下载）—— 全部走一个懒建的客户端
    # ------------------------------------------------------------------

    def _get_client(self):
        """懒建并复用 `lark.Client`。

        复用它不是因为"连接复用"（SDK 每次请求都 `requests.request`），
        而是因为令牌缓存挂在客户端持有的配置上 —— 每发一条新建一个客户端
        会让每次发送都重新取一次 `tenant_access_token`。
        """
        if self._client is None:
            import lark_oapi as lark
            self._client = lark.Client.builder() \
                .app_id(self._app_id) \
                .app_secret(self._app_secret) \
                .build()
        return self._client

    # ---- 发送 ----

    async def send(self, session: MessageSession,
                   content: str | MessageChain) -> bool:
        """Send a message (proactive path: cron / notifications)."""
        ok = await asyncio.to_thread(self._send_sync, session, content)
        await asyncio.to_thread(self._drain_sync, session, "")
        return ok

    async def send_note(self, session: MessageSession, text: str,
                        event=None) -> bool:
        """一行旁白（"已收到" / "已中断"）—— 不捎带本轮待发文件。

        为什么不直接用 `send()`：它会取走 `core/delivery_outbox` 里
        本轮待交付的文件，一轮还在跑的时候附件就被提前发出去了
        （用户先看到文件、再看到回答）。判据见 `channels/base.py::send_note`。
        """
        return await asyncio.to_thread(self._send_sync, session, str(text or ""))

    async def send_media_file(self, session: MessageSession, file_path: str,
                              filename: str = "",
                              context_token: str = "") -> bool:
        """发一个本地图片/文件（`MediaSendCapable`）。

        *context_token* 是微信那条协议的概念，飞书不用它 —— 保留形参是为了
        与能力协议同一个签名（`deliver_file` 那侧按属性认通道）。
        """
        return await asyncio.to_thread(
            self._send_media_sync, session, file_path, filename)

    def _send_sync(self, session: MessageSession,
                   content: str | MessageChain, reply_to: str = "",
                   in_thread: bool = False) -> bool:
        """发一条回复（同步；由线程调用）。返回 真送达 才 True。

        三件事都在这里：切段（超长）、rich / text 的选择、
        话题回复（`reply_to` = 收到那条的 `message_id`，失败退回普通消息）。
        """
        text = content if isinstance(content, str) else content.content
        text = str(text or "")
        if not text.strip():
            return False
        try:
            client = self._get_client()
        except ImportError:
            print(f"[feishu] (no SDK — would send): {text[:100]}")
            return False

        ok = True
        for i, chunk in enumerate(feishu_media.split_text(text)):
            # 只有第一段挂"回复"，其余发普通消息 —— 否则一段长回复会在
            # 对方那里变成好几条"回复"，每条都重复引用问题。
            ok = self._send_one(client, session, chunk,
                                reply_to if i == 0 else "", in_thread) and ok
        return ok

    def _send_one(self, client, session: MessageSession, text: str,
                  reply_to: str = "", in_thread: bool = False) -> bool:
        """一条（已切段）文本：像 markdown 就先试卡片，失败退回纯文本。"""
        attempts: list[tuple[str, str]] = []
        if feishu_media.looks_like_markdown(text):
            attempts.append(("interactive", feishu_media.to_card(text)))
        attempts.append(("text", json.dumps({"text": text}, ensure_ascii=False)))
        for msg_type, body in attempts:
            if self._post_message(client, session, msg_type, body, reply_to,
                                  in_thread):
                return True
        return False

    def _post_message(self, client, session: MessageSession, msg_type: str,
                      body: str, reply_to: str = "",
                      in_thread: bool = False) -> bool:
        """发一条消息：优先"回复到那条"，失败退回"新建一条"。

        `in_thread`：话题群里回复要落在同一个话题下（否则会跑到群的主时间线）。
        `receive_id_type` 按我们手里那个 id 的形状选，不按 `message_type`：
        私聊也会给 `chat_id`（`oc_...`），按类型选会拿 `chat_id` 当 `open_id` 发
服务端拒收，症状是"发不出去、日志只有一行 code"。
        """
        if reply_to:
            try:
                from lark_oapi.api.im.v1 import (
                    ReplyMessageRequest, ReplyMessageRequestBody)
                req = ReplyMessageRequest.builder().message_id(reply_to) \
                    .request_body(ReplyMessageRequestBody.builder()
                                  .msg_type(msg_type).content(body)
                                  .reply_in_thread(bool(in_thread)).build()) \
                    .build()
                resp = client.im.v1.message.reply(req)
                if _response_ok(resp):
                    return True
                print("[feishu] 回复被拒（msg_type=%s code=%s %s），改发普通消息"
                      % (msg_type, getattr(resp, "code", "?"),
                         getattr(resp, "msg", "")))
            except Exception as e:                                # noqa: BLE001
                print(f"[feishu] 回复失败（{type(e).__name__}: {e}），改发普通消息")

        #: 形状即真相（飞书的 id 带前缀：`oc_` 群/会话、`ou_` 用户）
        receive_id_type = ("chat_id" if str(session.session_id).startswith("oc_")
                           else "open_id")
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest, CreateMessageRequestBody)
            req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(CreateMessageRequestBody.builder()
                              .receive_id(session.session_id)
                              .msg_type(msg_type).content(body).build()) \
                .build()
            resp = client.im.v1.message.create(req)
        except Exception as e:                                    # noqa: BLE001
            print(f"[feishu] send error（{type(e).__name__}: {e}）")
            return False
        if not _response_ok(resp):
            print("[feishu] send rejected（msg_type=%s code=%s %s）"
                  % (msg_type, getattr(resp, "code", "?"),
                     getattr(resp, "msg", "")))
            return False
        return True

    # ---- 出站媒体 ----

    def _send_media_sync(self, session: MessageSession, file_path: str,
                         filename: str = "") -> bool:
        """上传 + 发一条（图片走 `image`，其余走 `file`）。"""
        p = Path(str(file_path or ""))
        if not p.is_file():
            print("[feishu] 要发的文件不存在：%s" % (file_path or "（空）"))
            return False
        data = p.read_bytes()
        mime, _ = mimetypes.guess_type(p.name)
        is_image = (mime or "").lower().startswith("image/")
        limit = MAX_IMAGE_BYTES if is_image else MAX_FILE_BYTES
        if len(data) > limit:
            print("[feishu] 文件太大（%d 字节 > %d），没有上传：%s"
                  % (len(data), limit, p.name))
            return False
        name = filename or p.name
        try:
            client = self._get_client()
        except ImportError:
            print("[feishu] 没装 lark-oapi，发不了附件")
            return False
        try:
            if is_image:
                from lark_oapi.api.im.v1 import (
                    CreateImageRequest, CreateImageRequestBody)
                req = CreateImageRequest.builder().request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(io.BytesIO(data)).build()).build()
                resp = client.im.v1.image.create(req)
                key = getattr(getattr(resp, "data", None), "image_key", "") or ""
                if not _response_ok(resp) or not key:
                    print("[feishu] 图片上传失败（code=%s %s）"
                          % (getattr(resp, "code", "?"), getattr(resp, "msg", "")))
                    return False
                return self._post_message(
                    client, session, "image",
                    json.dumps({"image_key": key}))

            from lark_oapi.api.im.v1 import (
                CreateFileRequest, CreateFileRequestBody)
            req = CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder()
                .file_type("stream").file_name(name)
                .file(io.BytesIO(data)).build()).build()
            resp = client.im.v1.file.create(req)
            key = getattr(getattr(resp, "data", None), "file_key", "") or ""
            if not _response_ok(resp) or not key:
                print("[feishu] 文件上传失败（code=%s %s）"
                      % (getattr(resp, "code", "?"), getattr(resp, "msg", "")))
                return False
            return self._post_message(
                client, session, "file",
                json.dumps({"file_key": key, "file_name": name},
                           ensure_ascii=False))
        except Exception as e:                                    # noqa: BLE001
            print(f"[feishu] 附件发送失败（{type(e).__name__}: {e}）")
            return False

    def _drain_sync(self, session: MessageSession, reply_to: str = "") -> None:
        """把本轮 `deliver_file` 登记的文件发出去（判据见 `core/delivery_outbox`）。"""
        from core import delivery_outbox as _outbox
        from core.session_key import session_key_for
        try:
            recs = _outbox.take_for(session_key=session_key_for(session),
                                    channel="feishu_channel",
                                    user_id=session.session_id)
        except Exception as exc:                                  # noqa: BLE001
            print(f"[feishu] 取待发文件失败（{type(exc).__name__}: {exc}）")
            return
        for rec in recs:
            ok = self._send_media_sync(session, rec.get("path", ""),
                                       rec.get("name", ""))
            if ok:
                continue
            # 失败要说给用户听（他问过一次"发过来"）
            try:
                self._send_sync(session, "文件 %s 没能发出去（原因在框架日志里）。"
                                % (rec.get("name") or "?"))
            except Exception:                                     # noqa: BLE001
                pass

    # ---- 入站附件 ----

    def _download_resource(self, message_id: str, key: str,
                           kind: str) -> tuple[bytes, str]:
        """取一条消息里的资源字节（同步）。"""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        req = GetMessageResourceRequest.builder() \
            .message_id(message_id).file_key(key) \
            .type(feishu_media.download_type(kind)).build()
        resp = self._get_client().im.v1.message_resource.get(req)
        if not _response_ok(resp):
            raise RuntimeError("下载被拒（code=%s %s）"
                               % (getattr(resp, "code", "?"),
                                  getattr(resp, "msg", "")))
        stream = getattr(resp, "file", None)
        data = stream.read() if hasattr(stream, "read") else bytes(stream or b"")
        return data, str(getattr(resp, "file_name", "") or "")

    def _save_media(self, data: bytes, kind: str, key: str,
                    name: str = "") -> Path:
        """落盘（按资源 key 命名，同一条消息重投不会存第二份）。"""
        _FEISHU_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:12]
        path = _FEISHU_TEMP_DIR / ("%s_%s%s"
                                   % (kind, digest,
                                      feishu_media.ext_for(kind, name)))
        if not path.exists():
            path.write_bytes(data)
        return path

    async def _resolve_media(self, chain: MessageChain, message_id: str) -> None:
        """把链子里的 `feishu://` 句柄换成本地路径（取不到就出声）。

        这里一个异常都不许漏出去：附件取不到是常见状态（权限、过期、
          平台抖动），而漏出去以后整条消息连管线都进不去 —— 用户看到的是
          "这条消息不存在"。
        """
        for comp in chain.components:
            kind, key = feishu_media.parse_handle(getattr(comp, "url", "") or "")
            if not kind:
                continue
            if not message_id:
                print("[feishu] 消息里没有 message_id，取不到附件：%s" % key)
                continue
            try:
                data, real_name = await asyncio.to_thread(
                    self._download_resource, message_id, key, kind)
            except Exception as exc:                              # noqa: BLE001
                print(f"[feishu] 取附件失败（{type(exc).__name__}: {exc}）")
                continue
            if not data:
                # 空字节不是"一个空文件"：它多半是"响应 200 但没有正文"。
                #    以前会落一个 0 字节的文件，下游只能说"附件是空的"。
                print("[feishu] 取回来的附件是空的（kind=%s key=%s…）"
                      % (kind, str(key)[:8]))
                continue
            if len(data) > feishu_media.MAX_DOWNLOAD_BYTES:
                print("[feishu] 附件太大（%d 字节），没有下载：%s"
                      % (len(data), key))
                continue
            name = getattr(comp, "name", "") or real_name
            try:
                path = await asyncio.to_thread(
                    self._save_media, data, kind, key, name)
            except Exception as exc:                              # noqa: BLE001
                print(f"[feishu] 附件落盘失败（{type(exc).__name__}: {exc}）")
                continue
            comp.local_path = str(path)
            if hasattr(comp, "size_bytes"):
                comp.size_bytes = len(data)
            print("[feishu] 附件已落盘：%s（%d 字节）" % (path.name, len(data)))

    # ------------------------------------------------------------------
    # Event handling (called from worker thread via _FeishuEventHandler)
    # ------------------------------------------------------------------

    def _dispatch_event(self, event_data: dict) -> None:
        """把一条事件投递到主循环（不在这里做任何网络调用）。

        以前这一格在 SDK 的 WS 线程上直接跑，而它要读/发网络 ——
        附件一多就把收事件的线程占住（心跳与后续事件一起排队）。
        现在这里只 `run_coroutine_threadsafe` 一下就返回。
        """
        msg_data = event_data.get("event", {}).get("message", {})
        if not msg_data:
            return
        if not self._main_loop or not self._running:
            # 这一条不许静默：通道还没就绪时消息会凭空消失，
            #    看来就像"对方那条消息没发出来"。
            print("[feishu] 通道还没就绪（main_loop/running），这条消息丢了：type=%s"
                  % (msg_data.get("message_type") or "?"))
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._handle(msg_data, event_data), self._main_loop)
            # 没人取的 Future 等于没报错 —— `run_coroutine_threadsafe`
            #    返回的 future 若不读，处理链里抛的异常会被完全吞掉：界面上
            #    没有回复、也没有步骤流（症状与"这条消息没收到"一模一样）。
            fut.add_done_callback(_report_future_error)
        except Exception as exc:                                  # noqa: BLE001
            print(f"[feishu] 投递事件失败（{type(exc).__name__}: {exc}）")

    async def _handle(self, msg_data: dict, event_data: dict) -> None:
        """主循环上的处理：转换 → 取附件 → 提交事件 → 管线。"""
        msg_type = str(msg_data.get("message_type") or "")
        chat_id = msg_data.get("chat_id", "")
        sender = event_data.get("event", {}).get("sender", {}).get("sender_id", {})
        sender_id_str = (sender.get("open_id", str(sender))
                         if isinstance(sender, dict) else str(sender))
        msg_id = msg_data.get("message_id", "")

        #: 每条入站消息留一行（type + 会话 + content 的键名，不含值）——
        #:   "消息像没收到"这一类只能靠它区分是"没进来"还是"进来后被丢了"。
        #: 带消息号前 8 位 —— "它怎么自己又回了一遍"只能靠它区分
        #:   "同一条被重投"（号相同，会被去重闸拦）与"真的又发了一条"（号不同）。
        print("[feishu] 收到消息 type=%s chat=%s id=%s keys=%s"
              % (msg_type or "?",
                 (str(chat_id)[:10] + "…") if chat_id else "（空）",
                 (str(msg_id)[:8] + "…") if msg_id else "（空）",
                 ",".join(sorted(feishu_media.content_of(msg_data).keys())) or "（空）"))

        chain = FeishuMessageConverter().to_internal(msg_data)
        if chain.is_empty:
            # `system`（入群/退群等）本来就没有给模型的内容 —— 不打日志。
            if msg_type != "system":
                print("[feishu] 收到一条没有可用内容的消息（type=%s，content 键=%s）"
                      % (msg_type or "?",
                         ",".join(sorted(feishu_media.content_of(msg_data).keys()))
                         or "（空）"))
            return

        await self._resolve_media(chain, msg_id)

        # 群聊 / 私聊看 `chat_type`（`p2p` / `group` / `topic`）。
        #   旧判据是 `chat_id and msg_type != "private"` —— `msg_type` 是内容
        #   类型（text/image/...），永远不是 "private"，只要 `chat_id` 非空
        #   就判成群，而飞书的私聊也带 `chat_id`，私聊全被当成群聊：
        #   会话键变成 `workspace:group:<chat_id>`（跨通道私聊连续性丢失）、
        #   压缩阈值按群聊的 0.75 提前压、模式被钉成 auto。
        #   载荷里没有 `chat_type` 时维持旧判（当群）—— 那是老形状，
        #   猜成私聊会把一群人的对话并进私聊那条。
        chat_type = str(msg_data.get("chat_type") or "").lower()
        is_group = (chat_type != "p2p") if chat_type else bool(chat_id)
        session = MessageSession(
            platform="feishu",
            message_type=MessageType.GROUP if is_group else MessageType.PRIVATE,
            session_id=chat_id or sender_id_str,
        )

        message = Message(
            sender_id=sender_id_str,
            message_chain=chain,
            sender_name=sender_id_str,
            message_type=session.message_type,
            session=session,
            raw_data=event_data,
            message_id=msg_id,
        )

        event = self.commit_event(message)
        event.extras["platform_meta"] = {
            "chat_id": chat_id,
            "msg_type": msg_type,
            "chat_type": chat_type,
        }

        #: 回复在独立线程里发（lark REST 是同步的）——
        #: 这里不等待它，否则一轮的收尾会卡在网络上。
        def _do_reply(e: MessageEvent) -> None:
            threading.Thread(target=self._reply, args=(e, msg_id),
                             daemon=True).start()

        event.reply_callback = _do_reply

        if self._scheduler:
            # 只入队，立刻返回 —— 回复仍由管线轮末调
            #   `reply_callback`（这一格没变）。此前这里是 `await execute`，而管线
            #   的五个阶段一个 `await` 都没有，这个 loop 整轮不回头：
            #   下一条消息连"收"都排不进来，`/stop` 同理。
            await channel_turns.intake(self, event)

    def _reply(self, event: MessageEvent, message_id: str = "") -> None:
        """线程里跑：先回文字，再把本轮登记的文件发掉。"""
        response = ""
        try:
            response = str(event.extras.get("response", "") or "")
        except Exception:                                         # noqa: BLE001
            response = ""
        meta = event.extras.get("platform_meta") or {}
        #: 话题群的回复要落在同一个话题下（不然会跑到群的主时间线）。
        in_thread = str(meta.get("chat_type") or "").lower() == "topic"
        if response:
            ok = self._send_sync(event.session, response, reply_to=message_id,
                                 in_thread=in_thread)
            #: 每一次发出都留一行：两行 = 两遍（用户能看到"又回了一遍"这
            #:   件事的现场），零行 = 它压根没发出去。
            print("[feishu] 回复已发出（%d 字符，%s，msg=%s）"
                  % (len(response), "成功" if ok else "失败",
                     (str(message_id)[:8] + "…") if message_id else "（空）"))
        else:
            print("[feishu] 这一轮没有回复文本（不发送）")
        self._drain_sync(event.session, message_id)


def _report_future_error(fut: Any) -> None:
    """投递出去的那个协程抛了异常，当场出声。

    判据：`concurrent.futures.Future` 的异常只能被取一次，没人取就等于
      没发生 —— 拿不到回复、也看不到步骤流。这里只报类型与消息，不打印载荷
      （载荷里有用户消息与附件句柄）。
    """
    try:
        exc = fut.exception()
    except Exception:                                             # noqa: BLE001
        return
    if exc is not None:
        print("[feishu] 处理这条消息时抛了异常（这一条没能进入管线）：%s: %s"
              % (type(exc).__name__, exc))


def _response_ok(resp: Any) -> bool:
    """`lark` 的响应：不抛也可能失败 —— 错误在 `code` 上。

    读不到 `code`（假对象 / 老 SDK）时按"成功"处理 —— 判据与旧实现一致：
    宁可漏报一次失败，也不要让每次发送都报假失败。
    """
    fn = getattr(resp, "success", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:                                         # noqa: BLE001
            return False
    return not getattr(resp, "code", 0)


# ------------------------------------------------------------------
# SDK bridge — called by lark-oapi ws.Client in worker thread
# ------------------------------------------------------------------

class _FeishuEventHandler:
    """Duck-type compatible with lark-oapi's EventDispatcherHandler.

    The ws.Client calls _do_without_validation(payload: bytes) for
    each incoming event frame.  We forward the decoded JSON to the
    channel's `_dispatch_event`.
    """

    def __init__(self, channel: FeishuChannel) -> None:
        self._channel = channel

    def _do_without_validation(self, payload: bytes) -> Any:
        """Called by lark-oapi ws.Client for each event frame."""
        import lark_oapi as lark
        try:
            body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            event_data = json.loads(body) if isinstance(body, str) else body

            if isinstance(event_data, dict) and "challenge" in event_data:
                resp = lark.core.RawResponse()
                resp.status_code = 200
                resp.body = json.dumps(
                    {"challenge": event_data["challenge"]}
                ).encode("utf-8")
                return resp

            self._channel._dispatch_event(event_data)
        except Exception as e:
            print(f"[feishu] Event handler error: {e}")

        resp = lark.core.RawResponse()
        resp.status_code = 200
        return resp

    def do(self, request: Any) -> Any:
        """Fallback HTTP-style handler."""
        body = request.body if hasattr(request, 'body') else request
        return self._do_without_validation(body)

    def do_without_validation(self, payload: bytes) -> Any:
        """Public alias."""
        return self._do_without_validation(payload)


# ------------------------------------------------------------------
# Message converter
# ------------------------------------------------------------------

def _mime_of(name: str) -> str:
    """按文件名猜 mime —— `file` 分支也要给准的类型。

    手机端发视频走的是 `type=file`（不是 `media`），
      而 `file` 分支原来不设 mime，落成 `application/octet-stream`
`core/image_input._is_video_comp` 认不出，**内建的"视频抽帧"那条路
      根本没接上**，模型只能自己写脚本用 cv2 抽帧（多两次 `run_command` 审批、
      多造两个工作区目录）。
判据：**同一个平台的两种到达方式（`media` / `file`）不许给同一个文件
      两种身份。** `media` 那支写死 `video/mp4`，这一支按扩展名认。

    猜不出来给 `application/octet-stream` —— **不猜成 `video/*`**：
      把单个 `.bin` 当视频只会让抽帧白跑一次，而"猜成视频"没有任何兜底价值。
    """
    return (mimetypes.guess_type(str(name or ""))[0]
            or "application/octet-stream")


class FeishuMessageConverter:
    """Convert Feishu message JSON ↔ Crannia MessageChain.

    `message.content` 永远是 JSON 字符串 —— 统一走
    `channels/feishu_channel/media.content_of`，别再在这里逐个
    `content.get(...)`（那正是"图片/文件消息抛 AttributeError、整条被吞"的根因）。
    """

    def to_internal(self, msg_data: dict) -> MessageChain:
        """飞书消息 → MessageChain（每一种类型都有去处）。

        覆盖：text / post / image / file / audio / media / sticker /
        interactive / share_chat / share_user / merge_forward / system。
        未知类型不再静默丢 —— 给模型一句"收到一条暂不支持的某某"，
        否则用户看到的是"机器人不理我"，而日志里什么都没有。
        """
        from channels.message import File as MsgFile, Image, Plain, Voice

        msg = msg_data or {}
        content = feishu_media.content_of(msg)
        mentions = msg.get("mentions") or []
        msg_type = str(msg.get("message_type") or "")
        comps: list = []

        if msg_type == "text":
            text = feishu_media.replace_mentions(
                str(content.get("text") or ""), mentions)
            if text:
                comps.append(Plain(text=text))

        elif msg_type == "post":
            comps.extend(self._post_components(content, mentions))

        elif msg_type == "image":
            key = str(content.get("image_key") or "")
            if key:
                comps.append(Image(url=feishu_media.SCHEME + "image/" + key,
                                   mime_type="image/jpeg"))

        elif msg_type == "file":
            key = str(content.get("file_key") or "")
            if key:
                name = str(content.get("file_name") or "file")
                comps.append(MsgFile(
                    name=name,
                    url=feishu_media.SCHEME + "file/" + key,
                    mime_type=_mime_of(name)))

        elif msg_type == "audio":
            key = str(content.get("file_key") or "")
            if key:
                # 时长是毫秒；`Voice` 用秒。
                try:
                    secs = int(content.get("duration") or 0) // 1000
                except (TypeError, ValueError):
                    secs = 0
                comps.append(Voice(url=feishu_media.SCHEME + "audio/" + key,
                                   duration_seconds=secs))

        elif msg_type == "media":
            key = str(content.get("file_key") or "")
            cover = str(content.get("image_key") or "")
            if key:
                comps.append(MsgFile(
                    name=str(content.get("file_name") or "video.mp4"),
                    url=feishu_media.SCHEME + "video/" + key,
                    mime_type="video/mp4"))
            if cover:
                # 封面顺带给它：抽帧要 `cv2`，抽不到时封面至少能看
                comps.append(Image(url=feishu_media.SCHEME + "image/" + cover,
                                   mime_type="image/jpeg"))

        elif msg_type == "sticker":
            key = str(content.get("file_key") or "")
            if key:
                comps.append(Image(url=feishu_media.SCHEME + "sticker/" + key,
                                   mime_type="image/gif"))

        elif msg_type == "interactive":
            text = feishu_media.card_plain_text(content)
            comps.append(Plain(text="[卡片] " + (text or "（内容未解析）")))

        elif msg_type == "share_chat":
            comps.append(Plain(text="[分享了一个群聊] "
                               + str(content.get("chat_id") or "")))

        elif msg_type == "share_user":
            comps.append(Plain(text="[分享了一个联系人] "
                               + str(content.get("user_id") or "")))

        elif msg_type == "merge_forward":
            # 合并转发要另调一次"按容器取消息"，而那条 API 的
            #    `container_id_type` 取值在本机 SDK 里没有依据，不猜，
            #    如实说明 + 打日志（别让它变成"机器人不理我"）。
            comps.append(Plain(text="[合并转发的聊天记录]（暂不支持展开）"))
            print("[feishu] 合并转发消息：暂不支持展开")

        elif msg_type == "system":
            # 入群 / 退群 / 改名 —— 不是给模型的输入。
            return MessageChain()

        else:
            comps.append(Plain(text="[收到一条暂不支持的消息类型：%s]"
                               % (msg_type or "未知")))

        return MessageChain(comps) if comps else MessageChain()

    # ---- 内部 ----

    @staticmethod
    def _post_body(content: dict) -> dict:
        """从 post 的三种形状里取出 `{title, content}` —— 取不到给空 dict。

        ① 出站 / 部分事件的形状：`{"post": {"zh_cn": {…}}}`（多语言，取第一个有内容的）
        ② 入站的真形状：`{"title": …, "content": [[…]]}`（扁平，没有语言键）
        ③ 手机端富文本：正文塞在 `content_v2`
           （一个 JSON 字符串，解开才是 `{title, content}`），而**附件在顶层
           `files`** —— 只读 `content` 的话，文字看得见、PDF 丢了。
           现场日志：`keys=content,content_v2,files,title`。
        """
        node = content
        if isinstance(content, dict) and isinstance(content.get("post"), dict):
            node = content["post"]
        if not isinstance(node, dict):
            return {}
        for lang in ("zh_cn", "en_us", "ja_jp"):
            cand = node.get(lang)
            if isinstance(cand, dict) and cand.get("content"):
                return cand
        if isinstance(node.get("content"), list):
            return node
        #: ③ `content_v2`：JSON 字符串（解析失败就照旧回空，别抛）
        v2 = node.get("content_v2")
        if isinstance(v2, str) and v2.strip():
            try:
                parsed = json.loads(v2)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("content"), list):
                return parsed
        return {}

    @staticmethod
    def _post_files(content: dict) -> list[dict]:
        """顶层 `files` → `[{file_key, file_name}]`（手机端富文本的附件就在这里）。

        形状两种都收（列表 / 以 key 为键的字典），元素也可能是裸字符串
        （只有 file_key、没有名字）。取不到就给空列表 —— 别抛。
        """
        raw = (content or {}).get("files")
        if isinstance(raw, dict):
            vals = list(raw.values())
        elif isinstance(raw, list):
            vals = raw
        else:
            return []
        out: list[dict] = []
        for it in vals:
            if isinstance(it, dict):
                key = str(it.get("file_key") or it.get("file_token") or "")
                name = str(it.get("file_name") or "file")
            else:
                key = str(it or "")
                name = "file"
            if key:
                out.append({"file_key": key, "file_name": name})
        return out

    def _post_components(self, content: dict, mentions: list | None) -> list:
        """富文本：正文 + 链接 + @ + 内嵌图片 / 附件。

        入站的 content 形状与出站不同：出站是
        `{"post": {"zh_cn": {"title":…, "content":[[…]]}}}`，而收到的
        post 消息是扁平的 `{"title":…, "content":[[…]]}`（没有 `post`、
        也没有语言键）。只认前者，每一条富文本都被丢掉，日志只有一行
        「没有可用内容的消息（type=post）」，用户看到的是"消息没发出去"。
        两种形状都收（多语言那种仍按"第一个有内容的语言"取，不重复两遍）。
        """
        from channels.message import File as MsgFile, Image, Plain

        lang_data = self._post_body(content)
        if not lang_data:
            return []

        texts: list[str] = []
        urls: list[str] = []
        images: list = []
        #: 没见过的元素标签要出声 —— 手机端与桌面端发出来的富文本
        #:   形状并不总一样，而"某种元素被静默丢掉"的症状是「模型看不见」，
        #:   日志上却一切正常 —— 所以认不出的标签要自己说出来。
        unknown: list[str] = []
        title = str(lang_data.get("title") or "").strip()
        if title:
            texts.append(title)
        for paragraph in lang_data.get("content") or []:
            if not isinstance(paragraph, list):
                continue
            parts: list[str] = []
            for element in paragraph:
                if not isinstance(element, dict):
                    continue
                tag = element.get("tag", "")
                if tag == "text":
                    parts.append(str(element.get("text") or ""))
                elif tag == "a":
                    parts.append(str(element.get("text") or ""))
                    href = str(element.get("href") or "")
                    if href:
                        urls.append(href)
                elif tag == "at":
                    parts.append("@" + str(element.get("user_name") or ""))
                elif tag == "img":
                    #: 键名兜底：不同客户端给的是 `image_key` 或 `image_token`
                    key = str(element.get("image_key")
                              or element.get("image_token") or "")
                    if key:
                        images.append(Image(
                            url=feishu_media.SCHEME + "image/" + key,
                            mime_type="image/jpeg"))
                elif tag in ("media", "file"):
                    # 富文本里挂的附件 / 视频：这是"消息携带文件"的那条路
                    # （以前只认 `img`，附件被静默丢掉）。
                    #: 键名兜底：`file_key` / `file_token` 两种都见过
                    key = str(element.get("file_key")
                              or element.get("file_token") or "")
                    if key:
                        name = str(element.get("file_name") or "file")
                        images.append(MsgFile(
                            name=name,
                            url=feishu_media.SCHEME + "file/" + key,
                            mime_type=_mime_of(name)))
                    else:
                        unknown.append(tag + "(没有 file_key/file_token)")
                elif tag:
                    unknown.append(str(tag))
            line = "".join(parts)
            if line.strip():
                texts.append(line)

        #: 顶层 `files` —— 手机端富文本把附件放在这里，不在正文元素里；
        #:   只读正文就会整份丢掉。
        #:   与正文里 `media`/`file` 元素按 key 去重（同一份别挂两次）。
        _seen = set()
        for _c in images:
            _u = str(getattr(_c, "url", "") or "")
            if _u.startswith(feishu_media.SCHEME):
                _seen.add(_u.rsplit("/", 1)[-1])
        for _f in self._post_files(content):
            if _f["file_key"] in _seen:
                continue
            _seen.add(_f["file_key"])
            images.append(MsgFile(
                name=_f["file_name"],
                url=feishu_media.SCHEME + "file/" + _f["file_key"],
                mime_type=_mime_of(_f["file_name"])))
            print("[feishu] 富文本顶层 files 里的附件：%s" % _f["file_name"])

        if unknown:
            #: 出声：这条消息照常处理，但那种元素我们没接住 ——
            #:   下一次"模型看不见"的现场，日志里要有这句话当线索。
            print("[feishu] post 里有没见过的元素标签：%s（其余部分照常处理）"
                  % ",".join(sorted(set(unknown))))

        text = feishu_media.replace_mentions("\n".join(texts), mentions)
        if urls:
            text += "\n\n[链接]\n" + "\n".join(urls)
        out: list = []
        if text.strip():
            out.append(Plain(text=text))
        out.extend(images)
        return out

    def to_platform(self, chain: MessageChain) -> dict:
        """MessageChain → 飞书 text 载荷（只做文字；媒体走上传那条路）。"""
        return {"text": chain.content}
