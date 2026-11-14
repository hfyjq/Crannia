"""
WeChat Channel — iLink protocol adapter via Tencent's official API.

Architecture::

    while running:
        msgs = await _get_updates()   # long-poll POST /ilink/bot/getupdates
        for msg in msgs:
            event = commit_event(message)   # → MessageEvent
            response = await scheduler.execute(event)   # → Pipeline → LLM
            await _send_message(to, response, context_token)   # → POST sendMessage

Key differences from Telegram Channel:
  - Login via QR code scan (auth.py), not static bot token
  - Pure HTTP/JSON — no SDK dependency (just httpx)
  - context_token must be extracted from received messages and echoed back
  - from_user_id must be "" (empty string) — omitting it causes silent failure
"""

from __future__ import annotations

import asyncio
import hashlib
import base64
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from core.loop_health import LoopHealth, exc_chain
from channels.base import (
    Channel,
    ChannelMetadata,
    Message,
    MessageEvent,
    MessageSession,
    MessageType,
)
from channels.capabilities import EditStateCapable
from core import channel_turns
from channels.message import MessageChain, Plain
from channels.wechat_channel import media as wx_media
from channels.wechat_channel.auth import do_login, load_credentials
from channels.wechat_channel.converter import WeChatMessageConverter


# ------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------

class WeChatAPIError(RuntimeError):
    """HTTP 200 但 iLink 判了业务失败（`ret != 0`，或带了 `errmsg`）。

    （`scripts/probe_wechat_push.py`）：字段名写错时服务端
    照样回 200，错误全在 body 里 —— `{"ret":-2,"errmsg":"ilink_user_id required"}`。
    只按状态码判错，这句错误就会被当成"发成功了"。
    """

    def __init__(self, path: str, ret: object, errmsg: str) -> None:
        self.path = path
        self.ret = ret
        self.errmsg = errmsg
        super().__init__(f"{path}: ret={ret!r} errmsg={errmsg!r}")


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

_WECHAT_TEMP_DIR = Path("data/workdata/wechat_files")

#: 本地文件名里可读那一段的长度上限。见 `_local_name` 的两条约束。
#: 别调大：这个目录已经在仓库深处，而 Windows `MAX_PATH` 是 260。
_LOCAL_NAME_MAX = 60


def _local_name(url: str, raw_name: "str | None" = None) -> str:
    """媒体 url -> 在 Windows 上写得出来的本地文件名。

    形状：<短名>_<12 位 sha256>.<扩展名>。

    ## 为什么不是 `Path(url).name`
    ---------------------------------
    微信的媒体 url 形如
    `…/c2c/download?encrypted_query_param=…&taskid=…` ——
    `Path(...).name` 会把整个 query 当成文件名（800+ 字符），
    而 Windows 建不出那么长的名字（`MAX_PATH` 260），
    OSError: [Errno 22] Invalid argument。

    ## 为什么不是纯摘要
    ---------------------------------
    这个目录是给人看的（"打开 `报表.xlsx`"）。保留一段可读名。

    ## 为什么摘要取自 url 而不是名字
    ---------------------------------
    url 才是这个文件的身份：同一张图重发时 url 相同，文件名相同，
    `_download_files` 里那条 `local_path.exists()` 的缓存照样命中。
    （这是下载层的缓存，与 `core/image_input` 的规范化是两件事 ——
     那边不查重，同一张图发两次就是两条消息。）
    """
    base = ""
    if raw_name:
        base = Path(str(raw_name)).stem
    if not base:
        # `Path(url).name` 的安全部分：只取最后一个路径段，丢掉 query
        # （那 800 个字符全在 query 里）
        base = Path(str(url).split("?")[0].split("#")[0]).stem
    if base in ("", "/", "\\"):
        base = "media"
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in base)
    safe = safe[:_LOCAL_NAME_MAX] or "media"

    # 扩展名：优先原始名字里的（它没被截断过），否则 `.bin`
    ext = ""
    src = str(raw_name) if raw_name else str(url).split("?")[0]
    tail = Path(src).suffix
    if 1 < len(tail) <= 10 and all(ch.isalnum() or ch == "." for ch in tail):
        ext = tail

    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:12]
    return f"{safe}_{digest}{ext}"
# 退避与「日志有形状」的判定不在这里 ——
# 它在 `core/loop_health.py`，因为同一个判定有两个消费者：
# 本文件的轮询循环 与 `core/workspace.py::_BaseWatcher` 的循环。
# 判据：一个判定同时管多处时不抄第二份（抄了会漂移且没有症状）。

#: 循环名（进日志行）
_LOOP_NAME = "wechat"
# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _random_wechat_uin() -> str:
    """Generate a random X-WECHAT-UIN header value (base64-encoded uint32)."""
    raw = secrets.randbits(32).to_bytes(4, "big")
    return base64.b64encode(raw).decode("ascii")


def _split_long_text(text: str, max_chars: int = 2048) -> list[str]:
    """Split long text at paragraph boundaries for WeChat delivery.

    WeChat iLink has a byte-level message limit.  Chinese characters
    are 3 bytes each in UTF-8, so we use a conservative character limit
    and also enforce a hard byte cap of ~4096 bytes per chunk.
    """
    MAX_BYTES = 4096  # hard byte cap per chunk

    # Fast path: fits in one chunk
    if len(text) <= max_chars and len(text.encode("utf-8")) <= MAX_BYTES:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        # Choose a split point that satisfies both char and byte limits
        limit = max_chars
        while limit > 0:
            candidate = remaining[:limit]
            if len(candidate.encode("utf-8")) <= MAX_BYTES:
                break
            limit = max(limit - limit // 10, 100)  # step back 10%

        # Try to break at a paragraph boundary
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            # Try sentence boundary (。！？)
            for punct in ("。", "！", "？", "\n", ".", "!", "?"):
                pos = remaining.rfind(punct, 0, limit)
                if pos > limit // 2:
                    split_at = pos + 1
                    break
            else:
                split_at = limit

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n ")

    return chunks


# ------------------------------------------------------------------
# WeChatChannel
# ------------------------------------------------------------------

class WeChatChannel(Channel):
    """WeChat bot adapter via Tencent iLink protocol."""

    def __init__(
        self,
        config: dict | None = None,
        scheduler=None,
        session_pool=None,
    ) -> None:
        super().__init__(config)
        self._converter = WeChatMessageConverter()
        self._scheduler = scheduler
        self._pool = session_pool
        self._running = False

        wx_cfg = (config or {}).get("channel", {}).get("wechat", {})
        self._base_url = wx_cfg.get("base_url", "https://ilinkai.weixin.qq.com")
        #: 媒体 CDN 与 API 是两个入口（登录返回的 `base_url` 只是 API 那个）。
        self._cdn_base = wx_cfg.get("cdn_base_url", wx_media.DEFAULT_CDN_BASE)
        self._credential_path = wx_cfg.get(
            "credentials_path", "data/wechat_credentials.json",
        )
        self._polling_timeout = wx_cfg.get("polling_timeout", 35)
        self._aggregation_delay: float = float(
            wx_cfg.get("aggregation_delay", 1.5)
        )

        # State set after login
        self._bot_token: str = ""
        self._ilink_bot_id: str = ""
        self._ilink_user_id: str = ""
        self._get_updates_buf: str = ""  # cursor for incremental sync

        self._http: httpx.AsyncClient | None = None

        # Message aggregation: buffer rapid-fire messages per user,
        # merge them into one MessageChain before pipeline processing.
        # This prevents file+text arriving as two separate turns.
        self._agg_buffers: dict[str, dict[str, Any]] = {}
        self._agg_lock = asyncio.Lock()

    # ---- Channel ABC ----

    def meta(self) -> ChannelMetadata:
        return ChannelMetadata(
            name="wechat_channel",
            description="WeChat Bot via Tencent iLink protocol",
            platform_type="wechat",
            support_multimodal=True,
        )

    async def run(self) -> None:
        """Main loop: login → poll → process → reply."""
        # 1. Ensure logged in
        try:
            await self._ensure_login()
        except Exception as e:
            print(f"[wechat] Login failed: {e}")
            return

        # 代理走一处口径（`core/net_proxy`）。
        from core import net_proxy as _np
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self._polling_timeout + 10), **_np.httpx_kwargs())
        self._running = True

        print(f"[wechat] Bot is running (iLink long-poll loop). "
              f"bot_id={self._ilink_bot_id[:12]}...")

        # 2. Main polling loop
        # 退避 + 有形状的日志，判定在 `core/loop_health`（照 `telegram_channel` 那一套）。
        # 判据见 `_LOG_MILESTONES_S` 上面的注释 —— 一句话：
        # 「闪了一下」与「死了九小时」在盘上留下的痕迹长得一模一样。
        health = LoopHealth(_LOOP_NAME)

        while self._running:
            try:
                updates = await self._get_updates()
            except httpx.TimeoutException:
                # 长轮询超时是正常的（服务端在等新消息）—— 不算失败、不退避。
                # 但它结束一次失败连续，所以要报一句「好了」。
                if (line := health.ok(time.monotonic())) is not None:
                    print(line)
                continue
            except httpx.HTTPError as e:
                wait, line = health.fail(e, time.monotonic())
                if line is not None:
                    print(line)
                await asyncio.sleep(wait)
                continue
            except WeChatAPIError as e:
                # 业务失败（不是网络）：同样退避，而且不许让它逃出去 ——
                # 这个循环只 catch 了 httpx 那一族，逃出去 = 整个 bot 静默死掉，
                # 而症状和"没有新消息"长得一样。
                wait, line = health.fail(e, time.monotonic())
                if line is not None:
                    print(line)
                await asyncio.sleep(wait)
                continue

            # 拿到了数据 —— 若刚从失败里出来，先报一句恢复
            if (line := health.ok(time.monotonic())) is not None:
                print(line)

            msgs = updates.get("msgs", []) or []
            for raw_msg in msgs:
                try:
                    await self._buffer_message(raw_msg)
                except Exception as e:
                    print(f"[wechat] Buffer error: {exc_chain(e)}")

        # Cleanup
        if self._http:
            await self._http.aclose()
            self._http = None

    async def terminate(self) -> None:
        self._running = False
        if self._http:
            await self._http.aclose()
            self._http = None
        print("[wechat] Bot stopped.")

    async def send(self, session: MessageSession, content: str | MessageChain) -> bool:
        """Send a message to a WeChat user (used for proactive messages).

        这里本来就会抛（`_api_post` 失败照常上抛），回 `True` 只在
        真走完之后。别忘了它是回执契约的一部分（见 `channels/base.py::send`）。

        发完之后顺手把本轮登记的文件（`core/delivery_outbox`）取走发掉 ——
        定时任务 / 后台回报这条路上，那是文件唯一的出口。
        """
        to_user = session.session_id  # ilink_user_id
        ctx_token = ""  # proactive messages have no context_token

        if isinstance(content, str):
            await self._send_text(to_user, content, ctx_token)
        else:
            await self._send_chain(to_user, content, ctx_token)
        await self._drain_delivered(session, to_user, ctx_token)
        return True

    async def send_file(
        self, session: MessageSession, file_path: str, filename: str = "",
    ) -> bool:
        """Send a file to a WeChat user. Returns True on delivery."""
        return await self.send_media_file(session, file_path, filename=filename)

    async def send_media_file(self, session: MessageSession, file_path: str,
                              filename: str = "",
                              context_token: str = "") -> bool:
        """发一个本地文件/图片。真发出去才回 `True`（回执契约）。

        能力声明在 `channels/capabilities.py::MediaSendCapable` ——
        `deliver_file` 靠这个属性判断"当前通道能不能直发"。
        """
        try:
            await self._send_media(session.session_id, file_path, filename,
                                   context_token)
            return True
        except Exception as exc:                                  # noqa: BLE001
            print("[wechat] 媒体发送失败（%s）：%s" % (type(exc).__name__, exc))
            return False

    # ---- 出站媒体 ---------------------------------------------------------

    async def _send_media(self, to_user: str, file_path: str,
                          filename: str = "", context_token: str = "",
                          kind_hint: str = "") -> None:
        """三步走：换上传地址 -> 加密上传 -> 发条目。失败一律抛（带人话）。

        形状与常量在 `channels/wechat_channel/media.py`（纯函数、有读者），
        这里只管网络：拿地址、传字节、发消息。

        *kind_hint* = `"image"` / `"file"`。调用方知道组件类型时必须给 ——
        微信下载回来的图片可能没有扩展名（文件名是"短名 + 摘要"），
        只看扩展名会把它当成文件附件发出去（对方收到的是一个附件，不是图）。
        """
        p = Path(str(file_path or ""))
        if not p.is_file():
            raise wx_media.MediaError("要发的文件不存在：%s" % (file_path or "（空）"))
        if self._http is None:
            raise wx_media.MediaError("微信通道没在运行，发不了文件")

        # 整份读进内存再加密（协议要 md5 与"密文大小"，流式得自己算一遍、
        #    而密文大小又依赖填充）—— 几十 MB 的文档没问题，超大文件要另做分块。
        raw = p.read_bytes()
        kind, media_type, _item_type = wx_media.kind_for(p.name)
        if kind_hint in ("image", "file"):
            kind = kind_hint
            media_type, _item_type = wx_media.kind_consts(kind)
        key = secrets.token_bytes(16)
        cipher = wx_media.encrypt(raw, key)
        filekey = secrets.token_hex(16)

        payload = wx_media.upload_payload(
            filekey=filekey, media_type=media_type, to_user=to_user,
            raw_size=len(raw),
            raw_md5=hashlib.md5(raw, usedforsecurity=False).hexdigest(),
            aes_key_hex=key.hex())
        resp = await self._api_post("ilink/bot/getuploadurl", payload)

        full = str(resp.get("upload_full_url") or "").strip()
        param = str(resp.get("upload_param") or "").strip()
        if full:
            upload_url = full
        elif param:
            upload_url = wx_media.cdn_upload_url(self._cdn_base, param, filekey)
        else:
            raise wx_media.MediaError(
                "服务端没给上传地址（upload_param / upload_full_url 都是空的）：%s"
                % (sorted(resp.keys())[:8],))

        enc_param = await self._cdn_upload(upload_url, cipher)
        item = wx_media.build_item(
            kind, wx_media.media_ref(enc_param, key.hex()),
            cipher_size=len(cipher), plain_size=len(raw),
            file_name=(filename or p.name))
        print("[wechat] 上传完成：%s（%d 字节，%s）"
              % (filename or p.name, len(raw), kind))
        await self._send_items(to_user, [item], context_token)

    async def _cdn_upload(self, upload_url: str, cipher: bytes) -> str:
        """把密文 POST 到 CDN，返回 `x-encrypted-param`（下一步的引用）。"""
        import httpx
        if self._http is None:
            raise wx_media.MediaError("微信通道没在运行，发不了文件")
        try:
            resp = await self._http.post(
                upload_url, content=cipher,
                headers={"Content-Type": "application/octet-stream"},
                timeout=120)
        except httpx.HTTPError as exc:
            raise wx_media.MediaError(
                "上传到微信 CDN 时网络出错（%s）—— 若开了代理，检查设置页那一档"
                % type(exc).__name__) from exc
        if resp.status_code != 200:
            raise wx_media.MediaError(
                "上传到微信 CDN 失败（HTTP %d）：%s"
                % (resp.status_code, (resp.text or "")[:200]))
        param = resp.headers.get("x-encrypted-param") or ""
        if not param:
            raise wx_media.MediaError("上传到微信 CDN 后没拿到 x-encrypted-param")
        return param

    async def _drain_delivered(self, session: MessageSession, to_user: str,
                               context_token: str) -> None:
        """把本轮 `deliver_file` 登记的那些文件发出去。

        判据见 `core/delivery_outbox`：工具是同步的、不能 await 这个循环，
        所以它只登记；通道在发完回复之后取走发掉，失败时回一句话给用户。
        """
        from core import delivery_outbox as _outbox
        from core.session_key import session_key_for
        try:
            recs = _outbox.take_for(session_key=session_key_for(session),
                                    channel="wechat_channel", user_id=to_user)
        except Exception as exc:                                  # noqa: BLE001
            print("[wechat] 取待发文件失败（%s）：%s" % (type(exc).__name__, exc))
            return
        for rec in recs:
            ok = await self.send_media_file(
                session, rec.get("path", ""), filename=rec.get("name", ""),
                context_token=context_token)
            if ok:
                continue
            # 失败要说给用户听 —— 他问过一次"发过来"，不能石沉大海
            try:
                await self._send_text(
                    to_user,
                    "文件 %s 没能发出去（具体原因在框架日志里）。"
                    % (rec.get("name") or "?"),
                    context_token)
            except Exception:                                     # noqa: BLE001
                pass

    # ---- Web login callback -----------------------------------------------

    def set_web_login_callback(self, cb) -> None:
        """Set a callback for web-mode QR login.

        When set, `_ensure_login()` will pass this callback to
        `do_login()` so that QR state is pushed to the Web UI
        instead of printed to the terminal.
        """
        self._web_login_cb = cb

    # ---- Login ------------------------------------------------------------

    async def _ensure_login(self) -> None:
        """Load cached credentials or run QR code login flow."""
        creds = load_credentials(self._credential_path)
        if creds:
            print(f"[wechat] Loaded cached credentials "
                  f"(bot_id={creds.get('ilink_bot_id', '')[:12]}...)")
            #: 有缓存凭据也要把界面推到"已连接"：网页那一侧的状态机
            #:   只有扫码那一条路会写 `confirmed`，用缓存直接跑起来时它永远停在
            #:   `starting`，界面就一直是"正在唤起…"（
            #:   「扫码连上了，发消息没反应」—— 而通道其实在跑）。
            web_cb = getattr(self, "_web_login_cb", None)
            if web_cb:
                try:
                    web_cb("confirmed", {
                        "bot_id": (creds.get("ilink_bot_id", "") or "")[:16] + "...",
                    })
                except Exception:                              # noqa: BLE001
                    pass
        else:
            print("[wechat] No cached credentials — starting QR login...")
            web_cb = getattr(self, "_web_login_cb", None)
            creds = do_login(
                base_url=self._base_url,
                credential_path=self._credential_path,
                web_callback=web_cb,
            )

        self._bot_token = creds["bot_token"]
        self._ilink_bot_id = creds.get("ilink_bot_id", "")
        self._ilink_user_id = creds.get("ilink_user_id", "")
        if creds.get("base_url"):
            self._base_url = creds["base_url"]

    # ---- Message aggregation + handling -----------------------------------

    async def _buffer_message(self, raw_msg: dict[str, Any]) -> None:
        """Buffer a message for aggregation, or flush immediately if disabled.

        When a user sends rapid-fire messages (e.g. file + instruction),
        WeChat delivers them as separate messages.  Aggregation merges
        them into a single MessageChain so the LLM sees the file AND
        the instruction in one turn.
        """
        msg = raw_msg.get("msg", raw_msg)
        from_user = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")

        if not from_user:
            return

        chain = self._converter.to_internal({"msg": msg})
        if chain.is_empty:
            return

        delay = self._aggregation_delay
        if delay <= 0:
            # Aggregation disabled — process immediately
            await self._process_aggregated(from_user, chain, context_token)
            return

        async with self._agg_lock:
            if from_user in self._agg_buffers:
                buf = self._agg_buffers[from_user]
                # Cancel existing timer
                if buf["timer"] is not None:
                    buf["timer"].cancel()
                # Merge: text + file → clearly separated so LLM
                # doesn't confuse the instruction with file content.
                has_file = any(
                    c.type.value in ("image", "file") for c in chain.components
                )
                if has_file:
                    buf["chain"].components.append(Plain("\n📎 附件: "))
                else:
                    buf["chain"].components.append(Plain("\n"))
                buf["chain"].components.extend(chain.components)
                buf["context_token"] = context_token  # latest wins
            else:
                buf = {
                    "chain": chain,
                    "context_token": context_token,
                    "timer": None,
                }
                self._agg_buffers[from_user] = buf

            # Start new timer
            buf["timer"] = asyncio.create_task(
                self._delayed_flush(from_user, delay)
            )

    async def _delayed_flush(self, user_id: str, delay: float) -> None:
        """Wait *delay* seconds, then flush the buffer for *user_id*."""
        try:
            await asyncio.sleep(delay)
            async with self._agg_lock:
                buf = self._agg_buffers.pop(user_id, None)
            if buf is not None:
                await self._process_aggregated(
                    user_id, buf["chain"], buf["context_token"],
                )
        except asyncio.CancelledError:
            pass  # timer reset by a new message
        except Exception as e:
            print(f"[wechat] Flush error for {user_id}: {exc_chain(e)}")

    async def _process_aggregated(
        self,
        from_user: str,
        chain: MessageChain,
        context_token: str,
    ) -> None:
        """Process a (possibly aggregated) MessageChain through the pipeline."""
        # Download + decrypt files/images before pipeline processing
        await self._download_files(chain)

        # Append local paths to the message text so the LLM knows
        # the files have been materialised.
        for c in chain.components:
            from channels.message import File as F, Image as Img
            if isinstance(c, (F, Img)) and c.local_path:
                chain.components.append(
                    Plain(f"\n[{('Image' if isinstance(c, Img) else 'File')}] "
                          f"已下载到本地：{c.local_path}")
                )

        log_text = chain.content[:60] if chain.content else "[media]"
        print(f"[wechat] msg from {from_user}: {log_text}")

        session = self._make_session(from_user)
        message = Message(
            sender_id=from_user,
            sender_name=from_user,
            message_chain=chain,
            message_type=session.message_type,
            session=session,
            raw_data={"context_token": context_token},
        )
        event = self.commit_event(message)

        # Typing indicator
        try:
            await self._send_typing(from_user, context_token)
        except Exception:
            pass

        # 只入队，立刻返回。回答由 worker 线程投回**本通道的
        #   loop** 上发（`on_reply` 里就是下面那一段，一字未改地搬进 `_send_reply`）。
        if self._scheduler:
            async def _on_reply(text: str) -> None:
                await self._send_reply(from_user, context_token, session, text, event)

            await channel_turns.intake(self, event, on_reply=_on_reply)

    async def _send_reply(self, from_user: str, context_token: str, session,
                          response: str, event) -> None:
        """把这一轮的回答发回去，然后发本轮登记的文件（顺序是判据）。"""
        response = str(response or "")
        if response:
            #: 列表类回复按行发（`/resume` / `/change`）：微信客户端会把
            #:   换行折成空格，十行列表糊成一张墙。
            #:   只在框架自己打的标记下走这条路（模型回复不拆），
            #:   而且行数封顶 —— 一条几十行的输出拆成几十条消息是另一种灾难。
            if (event.extras or {}).get("deliver_lines"):
                lines = [ln.strip() for ln in response.splitlines() if ln.strip()]
                if 1 < len(lines) <= 14:
                    for ln in lines:
                        await self._send_text(from_user, ln, context_token)
                else:
                    await self._send_text(from_user, response, context_token)
            else:
                await self._send_text(from_user, response, context_token)

        # 本轮登记要发的文件 —— 排在回复之后（判据见 `core/delivery_outbox`：
        #   工具只登记，通道在自己的循环里发；用户先看到那句话，再收到附件）。
        await self._drain_delivered(session, from_user, context_token)

    async def send_note(self, session: MessageSession, text: str,
                        event=None) -> bool:
        """一行旁白（"已收到" / "已中断"）—— 一个字都不捎带。

        为什么不直接沿用 `send()`：它会 `_drain_delivered()`，而那是
          本轮待交付的文件。拿它发旁白，一轮还在跑的时候附件就被提前发出去了
          （用户先看到文件、再看到回答）。判据见 `channels/base.py::send_note`。
        """
        to_user = session.session_id
        try:
            await self._send_text(to_user, str(text or ""), "")
            return True
        except Exception as exc:                                  # noqa: BLE001
            print(f"[wechat] 旁白没发出去（{type(exc).__name__}: {exc}）")
            return False

    async def _download_files(self, chain: MessageChain) -> None:
        """Download + AES-128-ECB decrypt files/images from iLink CDN."""
        _WECHAT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        for c in chain.components:
            from channels.message import File as F, Image as Img
            if not isinstance(c, (F, Img)):
                continue

            url = getattr(c, "url", None)
            aes_key = getattr(c, "_aes_key", None)
            if not url:
                # 不许用一句裸 `continue` 静默跳过：
                # 否则"没下载地址"与"压根没有这个组件"在盘上留下的痕迹完全一样
                # （判据：没有输出 ≠ 什么都没发生）。
                # 上游（转换器）也会为同一件事打一行；两行一起看就知道卡在哪一段。
                print(f"[wechat] 这个 {type(c).__name__} 组件**没有下载地址**"
                      f"（url 为空）—— 跳过了，不会去取字节"
                      f"（aes={'有' if aes_key else '无'}）")
                continue

            # 文件名必须是"短名 + 摘要"。
            #
            # ## 为什么
            #
            # 若退回 `getattr(c, "name", None) or Path(str(url)).name`：
            # `Image` 没有 `name`，落到 `Path(url).name`，而微信那条 URL 是
            # `https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=…&taskid=…`
            #`Path(...).name` 把整个 query 都当成文件名（800+ 字符）
            #Windows `MAX_PATH` = 260，
            # `OSError: [Errno 22] Invalid argument`。
            #
            # ## 两条约束，缺一不可
            #
            # * 长度：上面那条。这个目录已经在仓库深处了
            #   （`…/data/workdata/wechat_files/` ≈ 38 字符），
            #   60 + 13 之后还有很大余量。
            # * 可读：这个目录是给人看的（"打开 `报表.xlsx`"）
            #一律换成纯摘要是不行的。
            #
            #截断到 `_LOCAL_NAME_MAX` + 12 位摘要：
            #   摘要取自 url（不是名字）—— url 才是这个文件的身份
            #**同一张图重发时文件名一样，`local_path.exists()` 那条缓存照样命中**。
            #   （这一段是下载，不是 `core/image_input` 的规范化 ——
            #    它不查重、也不该查：同一张图发两次就是两条消息。）
            safe_name = _local_name(str(url), getattr(c, "name", None))
            local_path = _WECHAT_TEMP_DIR / safe_name
            if local_path.exists():
                c.local_path = str(local_path)
                continue

            print(f"[wechat] Downloading: {safe_name} "
                  f"(aes={'yes' if aes_key else 'no'})...")
            try:
                # 1. Download encrypted bytes from CDN
                from core import net_proxy as _np
                async with httpx.AsyncClient(**_np.httpx_kwargs()) as client:
                    resp = await client.get(url, timeout=60)
                    resp.raise_for_status()
                    cipher_bytes = resp.content

                if not cipher_bytes:
                    print(f"[wechat] Download: empty response from CDN")
                    continue

                # 2. AES-128-ECB decrypt if key is present
                if aes_key:
                    try:
                        from cryptography.hazmat.primitives.ciphers import (
                            Cipher, algorithms, modes,
                        )
                    except ImportError:
                        print("[wechat] cryptography not installed — "
                              "cannot decrypt files.  pip install cryptography")
                        plain_bytes = cipher_bytes  # store encrypted as-is
                    else:
                        key_bytes = _derive_aes_key(aes_key)
                        if key_bytes and len(key_bytes) == 16:
                            decipher = Cipher(
                                algorithms.AES(key_bytes),
                                modes.ECB(),
                            ).decryptor()
                            plain_bytes = decipher.update(cipher_bytes) + decipher.finalize()
                            # Strip PKCS7 padding
                            pad = plain_bytes[-1]
                            if 1 <= pad <= 16:
                                if all(plain_bytes[-i] == pad for i in range(1, pad + 1)):
                                    plain_bytes = plain_bytes[:-pad]
                        else:
                            print(f"[wechat] Download: cannot derive AES key, "
                                  f"using raw bytes")
                            plain_bytes = cipher_bytes
                else:
                    # No encryption — store raw bytes
                    plain_bytes = cipher_bytes

                local_path.write_bytes(plain_bytes)
                c.local_path = str(local_path)
                if isinstance(c, F):
                    c.size_bytes = local_path.stat().st_size
                print(f"[wechat] Downloaded: {safe_name} "
                      f"({local_path.stat().st_size} bytes)")
            except Exception as e:
                print(f"[wechat] Download failed: {type(e).__name__}: {e}")


    # ---- MessageSession ----------------------------------------------------

    def _make_session(self, user_id: str) -> MessageSession:
        """Create a session key for a WeChat user.  Always private chat."""
        return MessageSession("wechat", MessageType.PRIVATE, user_id)

    # ---- iLink API calls --------------------------------------------------

    async def _api_post(
        self,
        path: str,
        body: dict[str, Any],
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """POST to iLink API with correct headers."""
        assert self._http is not None
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self._bot_token}",
            "X-WECHAT-UIN": _random_wechat_uin(),
        }
        resp = await self._http.post(
            f"{self._base_url}/{path}",
            json=body,
            headers=headers,
            timeout=timeout or self._polling_timeout + 10,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        # HTTP 200 不等于成功 —— iLink 的业务错误走 `ret` / `errmsg`。
        #   判据照抄探针（它试过三种字段名）：`ret` 缺失或为 0，且没有 `errmsg`。
        #   抛出去是有意的：`send()` 的回执契约靠它（见本文件 `send` 的 docstring）。
        ret = data.get("ret")
        errmsg = data.get("errmsg") or ""
        if ret not in (None, 0) or errmsg:
            raise WeChatAPIError(path, ret, errmsg)
        return data

    async def _get_updates(self) -> dict[str, Any]:
        """Long-poll for new messages."""
        body: dict[str, Any] = {
            "get_updates_buf": self._get_updates_buf,
            "base_info": {"channel_version": "1.0.3"},
        }
        resp = await self._api_post(
            "ilink/bot/getupdates",
            body,
            timeout=self._polling_timeout,
        )
        # Update cursor for incremental sync
        new_buf = resp.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf
        return resp

    async def _send_chain(
        self, to_user: str, chain: MessageChain, context_token: str,
    ) -> None:
        """发一条 `MessageChain`：先文字、后媒体，每件媒体一条消息。

        媒体不能塞进同一条 `item_list` 里：它要求先上传、且服务端对
        "一条消息里混排多媒体"的态度没有依据 —— 一件一条是最稳的形状。
        """
        from channels.message import File as F, Image as Img, At

        texts: list[str] = []
        pending: list = []
        for c in chain.components:
            if isinstance(c, (Plain, At)):
                texts.append(c.to_plain_text())
            elif isinstance(c, (F, Img)):
                pending.append(c)
            # 其余组件（Voice 等）没有出站实现 —— 见下面那一行日志

        text = "".join(texts).strip()
        if text:
            await self._send_text(to_user, text, context_token)

        for c in pending:
            local = getattr(c, "local_path", None)
            if not local:
                # 没有本地字节 = 发不出去。出声，别让它看起来像发过了。
                print("[wechat] 要发的 %s 没有本地文件（只给了地址），跳过了"
                      % type(c).__name__)
                continue
            await self._send_media(to_user, str(local),
                                   getattr(c, "name", "") or "", context_token,
                                   kind_hint=("image" if isinstance(c, Img)
                                              else "file"))

    async def _send_items(self, to_user: str, item_list: list[dict],
                          context_token: str) -> None:
        """发一条只含给定 `item_list` 的消息。"""
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user,
                "client_id": uuid.uuid4().hex[:16],
                "message_type": 2,
                "message_state": 2,
                "item_list": item_list,
                "context_token": context_token,
            },
            "base_info": {"channel_version": "1.0.3"},
        }
        await self._api_post("ilink/bot/sendmessage", body)

    async def _send_text(
        self, to_user: str, text: str, context_token: str,
    ) -> None:
        """Send a text message (possibly split into multiple chunks)."""
        chunks = _split_long_text(text)
        for i, chunk in enumerate(chunks):
            prefix = f"({i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
            body = {
                "msg": {
                    "from_user_id": "",           # MUST be empty string
                    "to_user_id": to_user,
                    "client_id": uuid.uuid4().hex[:16],
                    "message_type": 2,            # BOT message
                    "message_state": 2,           # FINISH
                    "item_list": [
                        {"type": 1, "text_item": {"text": prefix + chunk}},
                    ],
                    "context_token": context_token,
                },
                "base_info": {"channel_version": "1.0.3"},
            }
            await self._api_post("ilink/bot/sendmessage", body)

    async def _send_typing(self, to_user: str, context_token: str,
                           typing_ticket: str = "") -> None:
        """Send 'typing' indicator.

        字段名与凭据取自探针（`scripts/probe_wechat_push.py`）：
          用 `to_user_id` 回 `ret:-2 ilink_user_id required`；
          改成 `ilink_user_id` 之后回 `ret:-3 typing_ticket required`。
        所以这里要两件：`ilink_user_id` 和 `typing_ticket`。

        拿不到 ticket 就不发 —— 发一个注定 -3 的请求，只是把错误
        藏进 `except: pass` 里，而外面看起来"指示器在工作"。
        当前收到的消息里没有这个字段，所以这次调用实际上不会发出请求；
        等哪天能从握手/消息里拿到它，把它传进来即可。
        """
        if not typing_ticket:
            return
        body = {
            "ilink_user_id": to_user,
            "typing_ticket": typing_ticket,
            "context_token": context_token,
        }
        try:
            await self._api_post("ilink/bot/sendtyping", body, timeout=5)
        except Exception:
            pass  # typing indicator is best-effort


# ------------------------------------------------------------------
# AES key derivation (module-level — used by _download_files)
# ------------------------------------------------------------------

#: 32 位十六进制 —— 密钥的两种形状里都可能是它。
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")


def _key_candidates(raw: str):
    """按可信度从高到低吐出候选密钥，最多一个。

    三种形状：直接是 32 位十六进制 /
    base64 里裹着那串十六进制 / base64 里就是裸的 16 字节。先认最无歧义的，
    再解 base64 按长度分派 —— 顺序换了行为会变，别调。
    """
    if _HEX32.fullmatch(raw):
        yield bytes.fromhex(raw)
        return
    try:
        blob = base64.b64decode(raw)
    except Exception:
        return
    if len(blob) == 32:
        text = blob.decode("ascii", errors="ignore")
        if _HEX32.fullmatch(text):
            yield bytes.fromhex(text)
    elif len(blob) == 16:
        yield blob


def _derive_aes_key(aes_key: str) -> bytes | None:
    """16 字节 AES-128 密钥；三种写法里认不出来就回 `None`。"""
    raw = (aes_key or "").strip()
    if not raw:
        return None
    for candidate in _key_candidates(raw):
        return candidate
    return None
