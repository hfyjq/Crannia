"""
DingTalkChannel — DingTalk IM adapter via dingtalk-stream SDK WebSocket.

使用 dingtalk-stream 的长连接模式（不需要公网 IP）。

消息流::

    钉钉 WS（SDK 线程）→ _DingTalkBotHandler.raw_process
      → process()：只解析 + 投递，立刻 ACK
      → 主循环上的 _handle()：建链子 → 取附件 → 提交事件 → 管线
      → 回复（线程）：sessionWebhook（`text` / `markdown`，带 @）
      → 交付的附件（线程）：机器人的 OpenAPI（webhook 没有 image/file）

三条与旧实现的差别（都是踩出来的形状）：

  * 解析走 `media.extract` —— 旧实现只从裸 dict 里抠 `text.content`，
    图片 / 文件 / 语音 / 视频 / 富文本全被丢掉，连"收到"都没有；
  * ACK 不再等管线 —— 旧实现 `future.result(timeout=120)` 把 SDK 回调线程
    堵住两分钟，期间其它消息只能排队；
  * 主动推送真的发 —— 旧实现打印一句就 `return False`（定时任务永远记失败）。

Requires: pip install dingtalk-stream
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from channels.base import (
    Channel, ChannelMetadata, Message, MessageEvent,
    MessageSession, MessageType,
)
from channels import textfmt
from channels.dingtalk_channel import media as dt_media
from channels.message import MessageChain
from core import channel_turns

#: 附件落盘目录（与另外两条通道同一个习惯）。
_DINGTALK_TEMP_DIR = Path("data/workdata/dingtalk_files")


def _report_future_error(fut: Any) -> None:
    """投递出去的那个协程抛了异常，当场出声（同飞书那条）。

    没人取的 `concurrent.futures.Future` 等于没报错：没有回复、也没有
      步骤流，看起来与"这条消息根本没收到"一模一样。
    """
    try:
        exc = fut.exception()
    except Exception:                                             # noqa: BLE001
        return
    if exc is not None:
        print("[dingtalk] 处理这条消息时抛了异常（这一条没能进入管线）：%s: %s"
              % (type(exc).__name__, exc))


# ------------------------------------------------------------------
# SDK Bot Handler (runs in SDK's thread)
# ------------------------------------------------------------------

class _DingTalkBotHandler:
    """Bridge between dingtalk-stream SDK and Crannia Pipeline.

    Subclasses both EventHandler and CallbackHandler patterns to catch
    IM messages whether they arrive as EVENT or CALLBACK type.
    """

    def __init__(self, app_key: str, app_secret: str, channel: "DingTalkChannel") -> None:
        import dingtalk_stream
        import logging
        logging.getLogger("dingtalk_stream").setLevel(logging.INFO)

        self._channel = channel
        self.dingtalk_client: Any = None
        credential = dingtalk_stream.Credential(app_key, app_secret)
        self._client = dingtalk_stream.DingTalkStreamClient(credential)
        self._client.register_all_event_handler(self)
        # IM messages arrive via CALLBACK on this topic
        self._client.register_callback_handler(
            "/v1.0/im/bot/messages/get", self,
        )

    # EventHandler interface
    def pre_start(self) -> None:
        pass

    # raw_process: required by both EventHandler and CallbackHandler
    async def raw_process(self, message: Any) -> Any:
        import dingtalk_stream
        code, msg = await self.process(message)
        ack = dingtalk_stream.AckMessage()
        ack.code = code
        ack.headers.message_id = getattr(
            getattr(message, "headers", None), "message_id", ""
        )
        ack.headers.content_type = "application/json"
        ack.message = msg
        ack.data = getattr(message, "data", {})
        return ack

    def start(self) -> None:
        print("[dingtalk] SDK client starting (EventHandler + Callback mode)...")
        self._client.start_forever()

    def stop(self) -> None:
        try:
            self._client.stop()
        except Exception:
            pass

    # Core: called by SDK for both EVENT and CALLBACK messages
    async def process(self, message: Any) -> Any:
        """只做两件事：解析、交给主循环。ACK 立刻回。

        这里不许等管线：SDK 的回调线程被堵住时，后面的消息只能排队，
        而钉钉那头等不到 ACK 会重投（重复处理靠 `msgId` 去重兜着）。
        """
        import dingtalk_stream
        try:
            data = getattr(message, "data", {}) or {}
            if isinstance(data, str):
                data = json.loads(data)
            if not isinstance(data, dict):
                return dingtalk_stream.AckMessage.STATUS_OK, ""

            info = dt_media.extract(data)
            if not info.get("msgtype"):
                # 非消息事件（进群、退群之类）—— 不打扰
                return dingtalk_stream.AckMessage.STATUS_OK, ""

            self._channel._dispatch(info)
            return dingtalk_stream.AckMessage.STATUS_OK, ""
        except Exception as e:                                    # noqa: BLE001
            print(f"[dingtalk] Handler error: {type(e).__name__}: {e}")
            return dingtalk_stream.AckMessage.STATUS_OK, ""


# ------------------------------------------------------------------
# Channel
# ------------------------------------------------------------------

class DingTalkChannel(Channel):
    """DingTalk channel adapter via dingtalk-stream WebSocket.

    Config keys (in config.yaml → channel.dingtalk):
        - app_key: DingTalk app key (ClientId)
        - app_secret: DingTalk app secret (ClientSecret)
    """

    def __init__(self, config: dict | None = None, **deps) -> None:
        super().__init__(config)
        self._scheduler = deps.get("scheduler")
        self._session_pool = deps.get("session_pool")
        self._handler: _DingTalkBotHandler | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        dt_cfg = (self.config or {}).get("channel", {}).get("dingtalk", {})
        self._app_key = dt_cfg.get("app_key", "")
        self._app_secret = dt_cfg.get("app_secret", "")
        self._token = ""
        self._token_exp = 0.0

    # ------------------------------------------------------------------
    # Channel ABC
    # ------------------------------------------------------------------

    def meta(self) -> ChannelMetadata:
        return ChannelMetadata(
            name="dingtalk_channel",
            description="DingTalk IM via WebSocket (dingtalk-stream v2)",
            platform_type="dingtalk",
            # 收：文字 / 富文本 / 图片 / 文件 / 语音 / 视频都取得到字节。
            support_multimodal=True,
            # 回复一次一条完整消息（没有流式契约）。
            support_streaming=False,
        )

    async def run(self) -> None:
        """Start WebSocket listener for DingTalk events."""
        app_key = self._app_key
        app_secret = self._app_secret

        if not app_key or not app_secret:
            print("[dingtalk] WARNING: app_key/app_secret not configured. "
                  "Set DINGTALK_APP_KEY and DINGTALK_APP_SECRET in .env")
            while True:
                await asyncio.sleep(3600)

        try:
            import dingtalk_stream  # noqa: F401
        except ImportError:
            print("[dingtalk] dingtalk-stream not installed. "
                  "Install: pip install dingtalk-stream")
            return

        self._main_loop = asyncio.get_running_loop()
        self._running = True
        self._handler = _DingTalkBotHandler(app_key, app_secret, self)

        print(f"[dingtalk] Starting WebSocket listener (app_key={app_key[:8]}...)")
        thread = threading.Thread(target=self._handler.start, daemon=True)
        thread.start()

        while self._running:
            await asyncio.sleep(1)

    async def terminate(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._handler:
            self._handler.stop()

    async def test_connection(self) -> dict:
        """Check if the DingTalk SDK is available."""
        if not self._app_key:
            return {"ok": False, "message": "DingTalk app_key not configured"}
        try:
            import dingtalk_stream  # noqa: F401
            return {"ok": True, "message": "dingtalk-stream SDK available"}
        except ImportError:
            return {"ok": False, "message": "dingtalk-stream not installed"}

    # ------------------------------------------------------------------
    # 入站：投递 / 处理
    # ------------------------------------------------------------------

    def _dispatch(self, info: dict) -> None:
        """把一条消息投递到主循环（SDK 线程里只做这一步）。"""
        if not self._main_loop or not self._running:
            print("[dingtalk] 通道还没就绪（main_loop/running），这条消息丢了：type=%s"
                  % (info.get("msgtype") or "?"))
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._handle(info), self._main_loop)
            # 没人取的 Future 等于没报错 —— 见 `_report_future_error`。
            fut.add_done_callback(_report_future_error)
        except Exception as exc:                                  # noqa: BLE001
            print(f"[dingtalk] 投递事件失败（{type(exc).__name__}: {exc}）")

    async def _handle(self, info: dict) -> None:
        """主循环上的处理：建链子 → 取附件 → 提交事件 → 管线。"""
        print("[dingtalk] 收到消息 type=%s conv_type=%s group=%s"
              % (info.get("msgtype") or "?",
                 info.get("conversation_type") or "?",
                 bool(info.get("is_group"))))
        chain = MessageChain(dt_media.components_of(info))
        if chain.is_empty:
            if info.get("msgtype") == "text":
                return                    # 空文字消息：不打扰模型
            print("[dingtalk] 收到一条没有可用内容的消息（type=%s）"
                  % (info.get("msgtype") or "?"))
            return

        await self._resolve_media(chain, info)

        is_group = bool(info.get("is_group"))
        #: 单聊的投递地址是发送人（主动推送要 userIds），群里是会话 id。
        session_id = (info.get("conversation_id") if is_group
                      else (info.get("sender_staff_id")
                            or info.get("sender_id") or ""))
        session = MessageSession(
            platform="dingtalk",
            message_type=MessageType.GROUP if is_group else MessageType.PRIVATE,
            session_id=session_id,
        )

        sender = info.get("sender_staff_id") or info.get("sender_id") or ""
        message = Message(
            sender_id=sender,
            sender_name=info.get("sender_nick") or sender,
            message_chain=chain,
            message_type=session.message_type,
            session=session,
            raw_data=info,
            message_id=info.get("msg_id") or "",
        )

        event = self.commit_event(message)
        event.extras["platform_meta"] = {
            "conversation_id": info.get("conversation_id"),
            "conversation_type": info.get("conversation_type"),
            "session_webhook": info.get("session_webhook"),
            "is_in_at": info.get("is_in_at"),
            "sender_staff_id": info.get("sender_staff_id"),
        }

        #: 回复在独立线程里发（webhook 是同步 HTTP）—— 这里不等它。
        def _do_reply(e: MessageEvent) -> None:
            threading.Thread(target=self._reply, args=(e, info),
                             daemon=True).start()

        event.reply_callback = _do_reply

        if self._scheduler:
            # 只入队，立刻返回（回复仍走 `reply_callback`）。
            #   异常时那句人话由 `core/channel_turns._run_turn` 统一发 ——
            #   与这里原来那句是同一份台词（`_COPY["error"]`），不再各写一份。
            await channel_turns.intake(self, event)

    def _reply(self, event: MessageEvent, info: dict) -> None:
        """线程里跑：把这一轮的回答发回 webhook。"""
        try:
            response = str(event.extras.get("response", "") or "")
        except Exception:                                         # noqa: BLE001
            response = ""
        if response.strip():
            ok = self._send_webhook(info, response)
            if not ok:
                print("[dingtalk] 这一轮的回答没能送到（webhook 失败，见上一条日志）")
        # 本轮登记要发的文件 —— 排在回复之后（判据见 `core/delivery_outbox`：
        #   工具只登记，通道在自己的循环里发；用户先看到那句话，再收到附件）。
        try:
            self._drain_delivered(event.session, info)
        except Exception as exc:                                  # noqa: BLE001
            print(f"[dingtalk] 发待交付文件时出错（{type(exc).__name__}: {exc}）")

    # ------------------------------------------------------------------
    # 出站：回复（sessionWebhook）
    # ------------------------------------------------------------------

    def _send_webhook(self, info: dict, text: str) -> bool:
        """分段发到 `sessionWebhook`，检查每一段的返回。

        旧实现把 `requests.post` 的返回值直接丢了 —— 发失败也无声。
        钉钉的约定与飞书一样：HTTP 200 里还可能带 非 0 的 `errcode`。
        """
        import requests
        from core import net_proxy as _np

        hook = str((info or {}).get("session_webhook") or "")
        if not hook:
            print("[dingtalk] 这条消息没有 sessionWebhook，回不了")
            return False
        #: 群里 @ 提问的人（单聊里这个字段无害，但没必要带）
        at_ids = []
        if info.get("is_group"):
            at_ids = [info.get("sender_staff_id") or info.get("sender_id") or ""]

        ok = True
        chunks = textfmt.split_text(text)
        for i, chunk in enumerate(chunks):
            payload = dt_media.reply_payload(chunk, at_ids if i == 0 else [])
            try:
                resp = requests.post(hook, json=payload, timeout=15,
                                     proxies=_np.requests_proxies())
            except Exception as exc:                              # noqa: BLE001
                print(f"[dingtalk] 回复出错（{type(exc).__name__}: {exc}）")
                ok = False
                continue
            if resp.status_code != 200:
                print("[dingtalk] 回复被拒（HTTP %d）：%s"
                      % (resp.status_code, (resp.text or "")[:200]))
                ok = False
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if int((body or {}).get("errcode") or 0) != 0:
                print("[dingtalk] 回复被拒（errcode=%s %s）"
                      % (body.get("errcode"), body.get("errmsg", "")))
                ok = False
        return ok

    # ------------------------------------------------------------------
    # 出站：主动推送（OpenAPI）
    # ------------------------------------------------------------------

    async def send(self, session: MessageSession,
                   content: str | MessageChain) -> bool:
        """主动发一条（定时任务 / 后台回报那条路）。

        发完之后顺手把本轮登记的文件（`core/delivery_outbox`）取走发掉 ——
        定时任务 / 后台回报这条路上，那是文件唯一的出口。
        """
        text = content if isinstance(content, str) else content.content
        ok = await asyncio.to_thread(
            self._send_proactive_sync, session, str(text or ""))
        try:
            await asyncio.to_thread(self._drain_delivered, session, None)
        except Exception as exc:                                  # noqa: BLE001
            print(f"[dingtalk] 发待交付文件时出错（{type(exc).__name__}: {exc}）")
        return ok

    async def send_note(self, session: MessageSession, text: str,
                        event=None) -> bool:
        """一行旁白（"已收到" / "已中断"）—— 不捎带本轮待发文件。

        为什么不直接用 `send()`：它会取走 `core/delivery_outbox` 里
        本轮待交付的文件，一轮还在跑的时候附件就被提前发出去了
        （用户先看到文件、再看到回答）。判据见 `channels/base.py::send_note`。
        """
        #: 优先走这条消息自己的 `sessionWebhook` —— 回答就是走它的，
        #:   旁白没理由换一条更脆的路（机器人 OpenAPI 要权限、群聊形式还不同）。
        #:   拿不到 webhook（比如定时任务起的轮）才退回主动推送。
        meta = (getattr(event, "extras", None) or {}).get("platform_meta") or {}
        if meta.get("session_webhook"):
            return await asyncio.to_thread(self._send_webhook, meta, str(text or ""))
        return await asyncio.to_thread(
            self._send_proactive_sync, session, str(text or ""))

    def _send_proactive_sync(self, session: MessageSession, text: str) -> bool:
        if not text.strip():
            return False
        # 没配凭据，不联网、直接说没送到（测试与"没配"这条路都不该打网络）
        if not self._app_key or not self._app_secret:
            print("[dingtalk] 没配 app_key / app_secret，主动推送发不出去")
            return False
        try:
            token = self._access_token()
        except Exception as exc:                                  # noqa: BLE001
            print(f"[dingtalk] 拿不到 access token（{type(exc).__name__}: {exc}）")
            return False

        is_group = session.message_type == MessageType.GROUP
        ok = True
        for chunk in textfmt.split_text(text):
            path, body = dt_media.proactive_request(
                str(session.session_id), is_group, chunk)
            body["robotCode"] = self._app_key
            ok = self._post_robot(path, body, token) and ok
        return ok

    def _post_robot(self, path: str, body: dict, token: str) -> bool:
        """调一次机器人 OpenAPI（文字 / 图片 / 文件都走这里）。真送到才 True。

        钉钉的 200 里还可能带 `code`（非空 = 被拒）—— 与 webhook 的 `errcode`
          同一类坑：只看状态码会把失败读成成功。
        """
        import requests
        from core import net_proxy as _np

        try:
            resp = requests.post(
                dt_media.API_BASE + path, json=body, timeout=30,
                headers={"x-acs-dingtalk-access-token": token,
                         "Content-Type": "application/json"},
                proxies=_np.requests_proxies())
        except Exception as exc:                                  # noqa: BLE001
            print(f"[dingtalk] 调 OpenAPI 出错（{type(exc).__name__}: {exc}）")
            return False
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code != 200 or (payload or {}).get("code"):
            print("[dingtalk] 发送被拒（HTTP %d）：%s"
                  % (resp.status_code, (resp.text or "")[:200]))
            return False
        return True

    # ------------------------------------------------------------------
    # 出站：媒体（图片 / 文件）
    # ------------------------------------------------------------------

    async def send_media_file(self, session: MessageSession, file_path: str,
                              filename: str = "",
                              context_token: str = "") -> bool:
        """发一个本地图片/文件（`MediaSendCapable`）。

        *context_token* 是微信那条协议的概念，钉钉不用它 —— 保留形参是为了
        与能力协议同一个签名（`deliver_file` 那侧按属性认通道）。

        走的是机器人 OpenAPI，不是回复用的 `sessionWebhook`：
          webhook 只认 text / markdown 那几种，没有 image/file。
        """
        return await asyncio.to_thread(
            self._send_media_sync, session, file_path, filename)

    def _send_media_sync(self, session: MessageSession, file_path: str,
                         filename: str = "") -> bool:
        """上传拿 media_id，再用机器人的 `sampleImageMsg` / `sampleFile` 发出去。"""
        p = Path(str(file_path or ""))
        if not p.is_file():
            print("[dingtalk] 要发的文件不存在：%s" % (file_path or "（空）"))
            return False
        if not self._app_key or not self._app_secret:
            print("[dingtalk] 没配 app_key / app_secret，附件发不出去")
            return False
        name = filename or p.name
        try:
            data = p.read_bytes()
        except OSError as exc:
            print(f"[dingtalk] 读不出要发的文件（{type(exc).__name__}: {exc}）")
            return False
        if len(data) > dt_media.MAX_UPLOAD_BYTES:
            print("[dingtalk] 文件太大（%d 字节 > %d），没有上传：%s"
                  % (len(data), dt_media.MAX_UPLOAD_BYTES, name))
            return False
        kind = dt_media.kind_for(name)
        try:
            token = self._access_token()
        except Exception as exc:                                  # noqa: BLE001
            print(f"[dingtalk] 拿不到 access token（{type(exc).__name__}: {exc}）")
            return False
        media_id = self._upload_media(data, kind, name, token)
        if not media_id:
            return False
        path, body = dt_media.media_request(
            str(session.session_id),
            session.message_type == MessageType.GROUP,
            kind, media_id, name)
        body["robotCode"] = self._app_key
        ok = self._post_robot(path, body, token)
        if ok:
            print("[dingtalk] 附件已发送：%s（%s）" % (name, kind))
        return ok

    def _upload_media(self, data: bytes, kind: str, name: str,
                      token: str) -> str:
        """字节 → `media_id`（旧版 oapi 的 `media/upload`）。

        v1.0 拿到的 access token 直接当 `access_token`
          查询参数用，返回 `errcode=0` + 19 位 `media_id`。
        `type` 只有 image / voice / video / file 四种，没有"音频"以外的细分；
          传错不报错，只会让对面收到一个打不开的占位。
        """
        import mimetypes
        import requests
        from core import net_proxy as _np

        #: multipart 的 Content-Type —— 服务端主要看 `type` 那个查询参数，
        #  这里给个准的（给错的类型在个别客户端上会存成打不开的文件）。
        mime = (mimetypes.guess_type(str(name or ""))[0]
                or ("image/png" if kind == "image" else "application/octet-stream"))
        try:
            resp = requests.post(
                dt_media.OAPI_BASE + "/media/upload",
                params={"access_token": token, "type": kind},
                files={"media": (name, data, mime)},
                timeout=120, proxies=_np.requests_proxies())
        except Exception as exc:                                  # noqa: BLE001
            print(f"[dingtalk] 附件上传出错（{type(exc).__name__}: {exc}）")
            return ""
        if resp.status_code != 200:
            print("[dingtalk] 附件上传被拒（HTTP %d）：%s"
                  % (resp.status_code, (resp.text or "")[:200]))
            return ""
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if int((body or {}).get("errcode") or 0) != 0:
            print("[dingtalk] 附件上传被拒（errcode=%s %s）"
                  % (body.get("errcode"), body.get("errmsg", "")))
            return ""
        media_id = str((body or {}).get("media_id") or "")
        if not media_id:
            print("[dingtalk] 附件上传没拿到 media_id：%s" % (str(body)[:200],))
        return media_id

    def _drain_delivered(self, session: MessageSession, info: dict | None) -> None:
        """把本轮 `deliver_file` 登记的文件发出去（判据见 `core/delivery_outbox`）。

        工具是同步的、不能 await 这个循环，它只登记；通道在发完回复之后
          取走发掉，失败时回一句话给用户（他问过一次"发过来"，不能石沉大海）。
        三格都要对上：`session_key + channel + user_id` —— 六个通道共用一个
          进程，私聊还共用 `workspace:private` 这一个键。
        """
        from core import delivery_outbox as _outbox
        from core.session_key import session_key_for

        try:
            recs = _outbox.take_for(
                session_key=session_key_for(session),
                channel="dingtalk_channel",
                user_id=str(getattr(session, "session_id", "") or ""))
        except Exception as exc:                                  # noqa: BLE001
            print(f"[dingtalk] 取待发文件失败（{type(exc).__name__}: {exc}）")
            return
        for rec in recs:
            if self._send_media_sync(session, rec.get("path", ""),
                                     rec.get("name", "")):
                continue
            note = "文件 %s 没能发出去（原因在框架日志里）。" % (rec.get("name") or "?")
            try:
                if info and info.get("session_webhook"):
                    self._send_webhook(info, note)
                else:
                    self._send_proactive_sync(session, note)
            except Exception:                                     # noqa: BLE001
                pass

    def _access_token(self) -> str:
        """企业应用的 access token（带缓存，提前 60 秒换）。"""
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        import requests
        from core import net_proxy as _np

        resp = requests.post(
            dt_media.API_BASE + "/v1.0/oauth2/accessToken",
            json={"appKey": self._app_key, "appSecret": self._app_secret},
            timeout=15, proxies=_np.requests_proxies())
        resp.raise_for_status()
        body = resp.json() or {}
        token = str(body.get("accessToken") or "")
        if not token:
            raise RuntimeError("钉钉没有返回 accessToken：%s"
                               % (str(body)[:200],))
        try:
            ttl = int(body.get("expireIn") or 7200)
        except (TypeError, ValueError):
            ttl = 7200
        self._token, self._token_exp = token, now + ttl
        return token

    # ------------------------------------------------------------------
    # 入站附件
    # ------------------------------------------------------------------

    def _download_file(self, code: str) -> bytes:
        """`downloadCode` → 字节（先把 code 换成下载地址，再 GET）。"""
        import requests
        from core import net_proxy as _np

        token = self._access_token()
        resp = requests.post(
            dt_media.API_BASE + "/v1.0/robot/messageFiles/download",
            json={"robotCode": self._app_key, "downloadCode": code},
            timeout=30,
            headers={"x-acs-dingtalk-access-token": token,
                     "Content-Type": "application/json"},
            proxies=_np.requests_proxies())
        if resp.status_code != 200:
            raise RuntimeError("换下载地址被拒（HTTP %d）：%s"
                               % (resp.status_code, (resp.text or "")[:200]))
        body = resp.json() or {}
        url = str(body.get("downloadUrl") or "")
        if not url:
            raise RuntimeError("换下载地址没拿到 downloadUrl：%s"
                               % (str(body)[:200],))
        got = requests.get(url, timeout=120, proxies=_np.requests_proxies())
        got.raise_for_status()
        return got.content

    def _save_media(self, data: bytes, kind: str, code: str,
                    name: str = "") -> Path:
        """落盘（按 downloadCode 命名，同一条消息重投不存第二份）。"""
        _DINGTALK_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(code).encode("utf-8")).hexdigest()[:12]
        path = _DINGTALK_TEMP_DIR / ("%s_%s%s"
                                     % (kind, digest, dt_media.ext_for(kind, name)))
        if not path.exists():
            path.write_bytes(data)
        return path

    async def _resolve_media(self, chain: MessageChain, info: dict) -> None:
        """把 `dingtalk://` 句柄换成本地路径（取不到就出声）。

        与飞书那条同一个判据：异常一个都不许漏出去 —— 漏出去整条消息
          连管线都进不去，用户看到的是"这条消息不存在"。
        """
        for comp in chain.components:
            kind, code = dt_media.parse_handle(getattr(comp, "url", "") or "")
            if not kind:
                continue
            try:
                data = await asyncio.to_thread(self._download_file, code)
            except Exception as exc:                              # noqa: BLE001
                print(f"[dingtalk] 取附件失败（{type(exc).__name__}: {exc}）")
                continue
            if not data:
                print("[dingtalk] 取回来的附件是空的（kind=%s code=%s…）"
                      % (kind, str(code)[:8]))
                continue
            if len(data) > dt_media.MAX_DOWNLOAD_BYTES:
                print("[dingtalk] 附件太大（%d 字节），没有下载：%s"
                      % (len(data), code))
                continue
            name = getattr(comp, "name", "") or ""
            try:
                path = await asyncio.to_thread(self._save_media, data, kind, code, name)
            except Exception as exc:                              # noqa: BLE001
                print(f"[dingtalk] 附件落盘失败（{type(exc).__name__}: {exc}）")
                continue
            comp.local_path = str(path)
            if hasattr(comp, "size_bytes"):
                comp.size_bytes = len(data)
            print("[dingtalk] 附件已落盘：%s（%d 字节）" % (path.name, len(data)))
