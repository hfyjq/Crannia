# -*- coding: utf-8 -*-
"""QQ 机器人通道 —— 官方 Bot API v2。

收发都走官方接口，不需要协议端（不用另跑一个小号来转）。

    收：取网关地址，连 WebSocket，认证后靠心跳保活，事件由 op 0 派发。
    发：私聊 POST /v2/users/<openid>/messages，
        群聊换 /v2/groups/<group_openid>/messages。

被动回复有两条限制：同一条消息最多回四次；群聊窗口五分钟、私聊三十分钟。
超出就退回主动消息（不带 msg_id）。这两个数只有 `_ReplyBudget` 一处口径，
    别在调用点再写一遍：写两处就会一处改了一处没改，症状是「偶尔这条回不出去」。

凭据（appId / appSecret）读 `data/qq_bot_credentials.json`，与微信那一份同形状。
那份文件在 `DEFAULT_FORBIDDEN_PATHS` 里 —— 模型能读到自己就写的那份 json，
等于把机器人交出去了。

群消息走的是 `GROUP_AT_MESSAGE_CREATE`，即平台只推「@ 我」的那些。
    「不 @ 也能收」取决于平台侧的开关，代码这一层没有可判的输入。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
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
from channels.message import File, Image, MessageChain, Plain, Voice
from core import channel_turns

# 官方地址。沙箱换 `https://sandbox.api.sgroup.qq.com`（配置 `base_url` 可覆盖）。
DEFAULT_API_BASE = "https://api.sgroup.qq.com"
DEFAULT_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

#: 群与私聊消息事件那一位（C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE）。
#: 只订这一位：频道消息是另一套形状，本通道不处理。
INTENT_GROUP_AND_C2C = 1 << 25

#: 单条消息的字符上限。超了要切，切的时候留出 "(i/N)" 前缀的位置。
MAX_MESSAGE_LENGTH = 5000
_CHUNK_PREFIX_BUDGET = 12

#: 被动回复的次数上限与窗口（秒）。
PASSIVE_LIMIT = 4
PASSIVE_TTL = {"c2c": 1800, "group": 300}

#: 事件去重的保留时长（秒）。重连后平台会重放没确认的事件。
_DEDUP_TTL = 600.0
_DEDUP_MAX = 4000

_CRED_PATH = Path("data/qq_bot_credentials.json")
_TEMP_DIR = Path("data/workdata/qqbot_files")

#: 下载入站媒体的上限。超过就只在正文里说一句，不落盘。
_MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024


def _load_credentials() -> dict:
    """这条通道当前的凭据。

    先问配置条目账本（默认那条），读不到再看老的单文件 —— 老部署升级上来时
    账本会从那个文件播种，所以第二条只照顾"播种之前就被问"的那一瞬。
    """
    try:
        from core import channel_configs as _cc
        saved = _cc.credentials_for("qq_bot")
        if saved:
            return saved
    except Exception:                                         # noqa: BLE001
        pass
    if not _CRED_PATH.exists():
        return {}
    try:
        with open(_CRED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print("[qq_bot] 读凭据文件失败（%s: %s）" % (type(exc).__name__, exc))
        return {}


def _c2c_openid(data: dict) -> str:
    """C2C 事件里的回话对象。

    平台把 `user_openid` 放在哪儿会变：见过在 `author` 里、在 `from_user`
    里、也有直接放事件顶层的。三处都认，第一次命中就用。

    只认这三处是有意的：认不出的形状不猜。上层会把键名打出来
    （不打值 —— 那是用户的 openid），下一次现场就能把它补进这张表。
    """
    for box in (data.get("author"), data.get("from_user"), data):
        if isinstance(box, dict) and box.get("user_openid"):
            return str(box["user_openid"])
    return ""


def _split_text(text: str, max_len: int) -> list[str]:
    """按换行切段，尽量不把一行切断。

    返回的每段都含前缀预算之外的位置，调用方再加 "(i/N)"。
    """
    limit = max(1, max_len - _CHUNK_PREFIX_BUDGET)
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        candidate = line if not cur else cur + "\n" + line
        if len(candidate) > limit:
            out.append(cur)
            cur = line
        else:
            cur = candidate
    if cur or not out:
        out.append(cur)
    return out


class _Seen:
    """最近处理过的 msg_id。重连后平台会重放事件，不去重就是重复回话。"""

    def __init__(self, ttl: float = _DEDUP_TTL, max_items: int = _DEDUP_MAX) -> None:
        self._ttl = ttl
        self._max = max_items
        self._items: dict[str, float] = {}

    def first_time(self, msg_id: str) -> bool:
        """这条消息以前没见过就记下来并返回 True。空 id 一律当新消息。"""
        key = str(msg_id or "")
        if not key:
            return True
        now = time.monotonic()
        self._trim(now)
        if key in self._items:
            return False
        self._items[key] = now
        return True

    def _trim(self, now: float) -> None:
        stale = [k for k, t in self._items.items() if now - t > self._ttl]
        for k in stale:
            self._items.pop(k, None)
        if len(self._items) <= self._max:
            return
        for k in list(self._items)[: len(self._items) - self._max]:
            self._items.pop(k, None)


class _ReplyBudget:
    """被动回复的次数与窗口，外加「这个会话最近一条消息 id」的缓存。

    每次发送前向 `reserve` 要一个 msg_id：要得到就以被动回复发出去，
    要不到就退回主动消息。两种都发得出去，区别只在平台侧算不算配额。
    """

    def __init__(self) -> None:
        self._counts: dict[str, list] = {}
        self._last: dict[str, tuple[str, str, float]] = {}

    def remember(self, target_key: str, scope: str, msg_id: str) -> None:
        """记下这个会话最近一条收到的消息，供没有上下文的发送当回复目标。"""
        if msg_id:
            self._last[target_key] = (msg_id, scope, time.monotonic())

    def last_for(self, target_key: str) -> tuple[str, str]:
        rec = self._last.get(target_key)
        if rec is None:
            return "", ""
        msg_id, scope, at = rec
        if time.monotonic() - at > PASSIVE_TTL.get(scope, PASSIVE_TTL["group"]):
            self._last.pop(target_key, None)
            return "", ""
        return msg_id, scope

    def reserve(self, scope: str, msg_id: str) -> tuple[str, int]:
        """给这次发送挑一个可用的被动回复 id。

        返回 `(msg_id, msg_seq)`。挑不出来时返回 `("", 0)`，调用方按主动消息发。
        `msg_seq` 在同一条 msg_id 下逐次加一，平台靠它区分同一条消息的多次回复。
        """
        if not msg_id:
            return "", 0
        now = time.monotonic()
        ttl = PASSIVE_TTL.get(scope, PASSIVE_TTL["group"])
        rec = self._counts.get(msg_id)
        if rec is None:
            self._counts[msg_id] = [1, now, 1]
            self._trim(now)
            return msg_id, 1
        count, first, seq = rec
        if now - first > ttl or count >= PASSIVE_LIMIT:
            return "", 0
        rec[0] = count + 1
        rec[2] = seq + 1
        return msg_id, seq + 1

    def _trim(self, now: float) -> None:
        if len(self._counts) <= _DEDUP_MAX:
            return
        longest = max(PASSIVE_TTL.values())
        for k in [k for k, rec in self._counts.items() if now - rec[1] > longest]:
            self._counts.pop(k, None)


class _TokenManager:
    """appId / appSecret 换 access_token，过期前刷新。

    token 有效期约两小时。提前一分钟换，避免"刚好到期那一次请求"被拒。
    """

    def __init__(self, app_id: str, app_secret: str, token_url: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_url = token_url
        self._token = ""
        self._expires_at = 0.0

    def invalidate(self) -> None:
        self._token = ""
        self._expires_at = 0.0

    async def current(self, session) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        payload = {"appId": self._app_id, "clientSecret": self._app_secret}
        async with session.post(self._token_url, json=payload, timeout=20) as resp:
            data = await resp.json(content_type=None)
        token = str((data or {}).get("access_token") or "")
        if not token:
            raise RuntimeError("取 access_token 失败：%s"
                               % json.dumps(data, ensure_ascii=False)[:200])
        # expires_in 有时是字符串，直接拿去做算术会抛。
        try:
            life = float((data or {}).get("expires_in") or 7200)
        except (TypeError, ValueError):
            life = 7200.0
        self._token = token
        self._expires_at = time.monotonic() + max(60.0, life - 60.0)
        return token


class QQBotMessageConverter(MessageConverter):
    """QQ 事件 → MessageChain。

    只做排版，不下载任何东西：媒体由通道那边下好再把本地路径交进来，
    与 Telegram 那一份同样的分工（转换器不碰网络，测试里也就不需要网络）。
    """

    def to_internal(self, platform_message: Any,
                    media: list[tuple[dict, str]] | None = None) -> MessageChain:
        """*platform_message* 是事件里的 `d` 字典。

        *media* 是 `[(附件条目, 本地路径)]`。下不下来的附件由调用方
        放进 `undelivered`，这里替它说一句实话。
        """
        data = platform_message or {}
        chain = MessageChain()

        text = str(data.get("content") or "").strip()
        if text:
            chain.add(Plain(text))

        for item, path in (media or []):
            chain.add(self._component(item, path))

        for item in (data.get("_undelivered") or []):
            name = item.get("filename") or item.get("content_type") or "附件"
            chain.add(Plain("[附件 %s 没有下载下来，用户可能以为你看到了]" % name))

        return chain

    @staticmethod
    def _component(item: dict, path: str):
        ctype = str(item.get("content_type") or "").lower()
        name = str(item.get("filename") or "file")
        size = int(item.get("size") or 0)
        if ctype.startswith("image"):
            return Image(local_path=path, mime_type=ctype or "image/png")
        if ctype.startswith("voice") or ctype.startswith("audio"):
            voice = Voice(url="", duration_seconds=0)
            voice.local_path = path
            return voice
        return File(name=name, local_path=path, mime_type=ctype or
                    "application/octet-stream", size_bytes=size)

    def to_platform(self, message_chain: MessageChain, **kwargs: Any) -> dict[str, Any]:
        """出一条纯文本正文。QQ 的文本消息只有一个 content 字段。"""
        return {"content": message_chain.content}


class QQBotChannel(Channel):
    """QQ 机器人适配器：WebSocket 收，REST 发。"""

    def __init__(self, config: dict | None = None, scheduler=None,
                 session_pool=None) -> None:
        super().__init__(config)
        self._converter = QQBotMessageConverter()
        self._scheduler = scheduler
        self._pool = session_pool
        self._running = False
        self._session = None
        self._ws = None
        self._seq: int | None = None
        self._tokens: _TokenManager | None = None
        self._budget = _ReplyBudget()
        self._seen = _Seen()

        self.refresh_credentials()
        cfg = (config or {}).get("channel", {}).get("qq_bot", {}) or {}
        self._api_base = str(cfg.get("base_url") or DEFAULT_API_BASE).rstrip("/")
        self._token_url = str(cfg.get("token_url") or DEFAULT_TOKEN_URL)
        self._bot_openid = ""
        #: 代理每次 `run()` 现取。通道实例是进程内长活的，在 `__init__` 里定死
        #:   的话，先起通道再开代理就只能重启整个应用才生效，而症状是「界面显示
        #:   运行中、QQ 里发消息没回应」。
        self._proxy = self._resolve_proxy()

    def refresh_credentials(self) -> None:
        """重新读一次凭据（配置条目账本优先，其次老的单文件与环境变量）。

        通道实例是进程内长活的：`init_bus` 启动时只装一次。凭据在
        `__init__` 里读死的话，用户在设置页填完再点「启动」，那条实例拿的
        还是启动时那份空值 —— 卡片上写着「已配置」（那是现读账本的），
        通道却说「没有配置 AppID / AppSecret」（这是老实例说的），
        两块界面互相打架，而两边各自都对。

        与代理同一条理由（见 `_proxy` 那段注释）：先起通道、后填凭据是可
        预期的操作顺序，不该要求用户为此重启整个应用。
        """
        cfg = (self.config or {}).get("channel", {}).get("qq_bot", {}) or {}
        try:
            creds = _load_credentials()
        except Exception as exc:                                  # noqa: BLE001
            print("[qq_bot] 读凭据失败（%s: %s）" % (type(exc).__name__, exc))
            creds = {}
        self._app_id = str(cfg.get("app_id") or creds.get("app_id")
                           or os.environ.get("QQ_BOT_APP_ID") or "")
        self._app_secret = str(cfg.get("app_secret") or creds.get("app_secret")
                               or os.environ.get("QQ_BOT_APP_SECRET") or "")

    def meta(self) -> ChannelMetadata:
        return ChannelMetadata(
            name="qq_bot_channel",
            description="QQ Bot adapter — official Bot API v2",
            platform_type="qq_bot",
            support_multimodal=True,
        )

    def _resolve_proxy(self) -> str:
        """三档代理里此刻生效的那一个地址。`direct` 档一律直连。"""
        cfg = (self.config or {}).get("channel", {}).get("qq_bot", {}) or {}
        from core import net_proxy as _np
        mode, url = _np.state()
        if mode == "direct":
            base = ""
        elif mode == "manual":
            base = url
        else:
            base = os.environ.get("HTTPS_PROXY", "")
        return str(cfg.get("proxy") or base or "")

    # 启动与主循环

    async def run(self) -> None:
        """连网关，断了自己重连。整个方法不返回，直到 `terminate()`。"""
        import aiohttp

        #: 按一下「启动」时现读一次 —— 用户可能刚刚才把凭据填进设置页，
        #:   而这条实例是框架启动时就建好的（见 `refresh_credentials`）。
        self.refresh_credentials()
        if not (self._app_id and self._app_secret):
            self._fail_start("没有配置 AppID / AppSecret（设置 → 通道 → QQ）")
            return

        self._running = True
        self._proxy = self._resolve_proxy()
        if self._proxy:
            from core.net_proxy import redact as _redact
            print("[qq_bot] 使用代理：%s" % _redact(self._proxy))
        backoff = 2.0
        try:
            proxy = self._proxy or None
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None)) as session:
                self._session = session
                self._tokens = _TokenManager(self._app_id, self._app_secret,
                                             self._token_url)
                while self._running:
                    try:
                        await self._gateway_once(proxy)
                        backoff = 2.0
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if not self._running:
                            break
                        self._fail_start("%s: %s" % (type(exc).__name__, exc))
                        print("[qq_bot] 网关断开，%d 秒后重连" % int(backoff))
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60.0)
        finally:
            self._running = False
            self._session = None
            self._ws = None

    def _fail_start(self, message: str) -> None:
        """起不来时留一条记录，界面上那张通道卡读得到。

        只 print 到终端等于没有：桌面壳里没有终端，卡片只会回到「未运行」。
        """
        print("[qq_bot] %s" % message)
        try:
            from core import incidents as _inc
            _inc.note("qq_bot_channel", "channel", message,
                      component_tpl="QQ 通道启动失败：{msg}",
                      component_vars={"msg": message},
                      error_tpl="{msg}", error_vars={"msg": message})
        except Exception as exc:
            print("[qq_bot] 记事故失败（%s: %s）" % (type(exc).__name__, exc))

    async def _gateway_once(self, proxy: str | None) -> None:
        """连一次网关，直到它自己断开或要求重连。"""
        import aiohttp

        token = await self._tokens.current(self._session)
        headers = {"Authorization": "QQBot %s" % token}
        async with self._session.get(self._api_base + "/gateway",
                                     headers=headers, proxy=proxy) as resp:
            info = await resp.json(content_type=None)
        url = str((info or {}).get("url") or "")
        if not url:
            raise RuntimeError("网关地址没取到：%s"
                               % json.dumps(info, ensure_ascii=False)[:200])

        async with self._session.ws_connect(url, proxy=proxy,
                                            heartbeat=None) as ws:
            self._ws = ws
            heartbeat: asyncio.Task | None = None
            try:
                async for frame in ws:
                    if frame.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        payload = json.loads(frame.data)
                    except json.JSONDecodeError:
                        continue
                    op = payload.get("op")
                    data = payload.get("d") or {}

                    if op == 10:
                        interval = float(data.get("heartbeat_interval") or 30000) / 1000.0
                        await self._identify(ws, token)
                        heartbeat = asyncio.create_task(
                            self._heartbeat_loop(ws, interval))
                        continue

                    if op == 11:
                        continue
                    if op == 7:
                        print("[qq_bot] 平台要求重连")
                        return
                    if op == 9:
                        self._seq = None
                        await self._identify(ws, token)
                        continue
                    if op != 0:
                        continue

                    self._seq = payload.get("s")
                    await self._dispatch(str(payload.get("t") or ""), data)
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()

    @staticmethod
    def identify_payload(token: str) -> dict:
        """认证帧。`token` 前面必须带 `QQBot ` 前缀，少了平台直接拒。"""
        return {"op": 2, "d": {
            "token": "QQBot %s" % token,
            "intents": INTENT_GROUP_AND_C2C,
            "shard": [0, 1],
            "properties": {},
        }}

    @staticmethod
    def message_body(content: str, reply_id: str = "", seq: int = 0) -> dict:
        """一条文本消息的请求体。带 reply_id 就是被动回复，不带就是主动消息。"""
        body: dict[str, Any] = {"content": content, "msg_type": 0}
        if reply_id:
            body["msg_id"] = reply_id
            body["msg_seq"] = int(seq or 1)
        return body

    @staticmethod
    def file_upload_payload(data: bytes, name: str) -> dict:
        """素材上传的请求体（文件那一类）。

        `srv_send_msg: False` = 只传素材、不立刻发一条 —— 拿回 `file_info`
        之后再发媒体消息，这样那条消息能挂上被动回复的 `msg_id`。

        形状单独留一处：探针（`scripts/probe_qq_bot.py`）也用它，
        两边各写一份的话，真机上验过的就不是通道发出去的那一份了。
        """
        import base64

        return {"file_type": 4,
                "file_data": base64.b64encode(data).decode("ascii"),
                "srv_send_msg": False,
                "file_name": name}

    @staticmethod
    def media_message_body(file_info: str, reply_id: str = "",
                           seq: int = 0) -> dict:
        """一条文件消息的请求体（`msg_type=7` + `file_info`）。"""
        body: dict[str, Any] = {"content": "", "msg_type": 7,
                                "media": {"file_info": file_info}}
        if reply_id:
            body["msg_id"] = reply_id
            body["msg_seq"] = int(seq or 1)
        return body

    async def _identify(self, ws, token: str) -> None:
        await ws.send_str(json.dumps(self.identify_payload(token)))

    async def _heartbeat_loop(self, ws, interval: float) -> None:
        """每隔 interval 秒发一次心跳，带上最后一次收到的事件序号。"""
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send_str(json.dumps({"op": 1, "d": self._seq}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[qq_bot] 心跳停了（%s: %s）" % (type(exc).__name__, exc))

    async def _dispatch(self, event_type: str, data: dict) -> None:
        if event_type == "READY":
            user = data.get("user") or {}
            self._bot_openid = str(user.get("id") or "")
            print("[qq_bot] 已连接（bot id=%s）" % (self._bot_openid or "?"))
            try:
                from core import incidents as _inc
                _inc.resolve("qq_bot_channel", "channel")
            except Exception:
                pass
            return
        if event_type not in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            return
        try:
            await self._handle_message(event_type, data)
        except Exception as exc:
            print("[qq_bot] 处理消息出错（%s: %s）" % (type(exc).__name__, exc))

    # 入站

    async def _handle_message(self, event_type: str, data: dict) -> None:
        msg_id = str(data.get("id") or "")
        if not self._seen.first_time(msg_id):
            return

        if event_type == "GROUP_AT_MESSAGE_CREATE":
            scope = "group"
            target_id = str(data.get("group_openid") or "")
            author = data.get("author") or {}
            sender_id = str(author.get("member_openid") or author.get("id") or "")
            sender_name = str(author.get("member_openid") or "")
            message_type = MessageType.GROUP
        else:
            scope = "c2c"
            target_id = _c2c_openid(data)
            sender_id = target_id
            sender_name = str((data.get("author") or {}).get("username")
                              or (data.get("from_user") or {}).get("username")
                              or "")
            message_type = MessageType.PRIVATE

        if not target_id:
            #: 只打键名，不打值 —— 那些值里是用户的 openid。
            #: 打出来是为了下一次现场能把它补进 `_c2c_openid` 那张表。
            print("[qq_bot] 这条事件没有回话对象，跳过（t=%s，顶层键=%s）"
                  % (event_type, ",".join(sorted(str(k) for k in data))))
            return

        media, undelivered = await self._download_attachments(
            data.get("attachments") or [])
        enriched = dict(data)
        #: 下不下来的那几条要让模型知道 —— 静默丢掉会让它对着空气说话。
        enriched["_undelivered"] = undelivered
        chain = self._converter.to_internal(enriched, media=media)
        if chain.is_empty:
            return

        session = MessageSession(
            platform=self.meta().name,
            message_type=message_type,
            session_id=target_id,
        )
        self._budget.remember(str(session), scope, msg_id)

        text = chain.content[:80] if chain.content else "[媒体]"
        print("[qq_bot] %s 收到消息：%s" % (scope, text))

        message = Message(
            sender_id=sender_id,
            sender_name=sender_name,
            message_chain=chain,
            message_type=message_type,
            session=session,
            #: 平台消息号。管线用它去重，不传那道闸就恒不触发。
            message_id=msg_id,
        )
        event = self.commit_event(message)

        if not self._scheduler:
            return

        async def _on_reply(reply: str) -> None:
            await self._reply_and_drain(session, scope, target_id, msg_id, reply)

        await channel_turns.intake(self, event, on_reply=_on_reply)

    async def _download_attachments(self, items: list) -> tuple[list, list]:
        """把附件下到本地，返回 `([(条目, 路径)], [没下下来的条目])`。

        一个异常都不许漏出去：漏出去整条消息连管线都进不去。
        """
        media: list[tuple[dict, str]] = []
        undelivered: list = []
        for item in items:
            url = str((item or {}).get("url") or "")
            if not url:
                undelivered.append(item)
                continue
            try:
                path = await self._download_one(item, url)
            except Exception as exc:
                print("[qq_bot] 下载附件失败（%s: %s）"
                      % (type(exc).__name__, exc))
                path = ""
            if path:
                media.append((item, path))
            else:
                undelivered.append(item)
        return media, undelivered

    async def _download_one(self, item: dict, url: str) -> str:
        size = int((item or {}).get("size") or 0)
        if size > _MAX_DOWNLOAD_BYTES:
            print("[qq_bot] 附件太大（%d 字节），不下载" % size)
            return ""
        name = str((item or {}).get("filename") or "")
        suffix = Path(name).suffix or ".bin"
        # 目录在这里建、不在 `__init__` 里建：通道对象启动时就会构造，
        #   在那时候建目录，任何"只是把通道导进来"的地方都会往工作目录里落一个空夹。
        _TEMP_DIR.mkdir(parents=True, exist_ok=True)
        dest = _TEMP_DIR / ("%s%s" % (time.strftime("%Y%m%d%H%M%S"), suffix))
        async with self._session.get(url, proxy=self._proxy or None) as resp:
            if resp.status != 200:
                return ""
            data = await resp.read()
        if len(data) > _MAX_DOWNLOAD_BYTES:
            return ""
        dest.write_bytes(data)
        return str(dest)

    # 出站

    async def _reply_and_drain(self, session: MessageSession, scope: str,
                               target_id: str, msg_id: str,
                               response: str | None) -> None:
        """先回话，再把本轮登记的文件发出去。

        顺序是判据：用户先看到那句话，再收到附件。取件放在 finally 里，
        发送这一段抛了文件也照样走。
        """
        try:
            if response:
                await self._send_text(scope, target_id, response, msg_id)
        finally:
            try:
                await self._drain_delivered(session)
            except Exception as exc:
                print("[qq_bot] 发待交付文件时出错（%s: %s）"
                      % (type(exc).__name__, exc))

    async def _send_text(self, scope: str, target_id: str, text: str,
                         msg_id: str = "") -> bool:
        chunks = _split_text(text, MAX_MESSAGE_LENGTH)
        total = len(chunks)
        ok = True
        for i, chunk in enumerate(chunks, 1):
            body = chunk if total == 1 else "(%d/%d)\n%s" % (i, total, chunk)
            if not await self._send_one(scope, target_id, body, msg_id):
                ok = False
        return ok

    async def _send_one(self, scope: str, target_id: str, content: str,
                        msg_id: str = "") -> bool:
        """发一条纯文本。被动配额用完就退回主动消息，两条路都算发送。"""
        reply, seq = self._budget.reserve(scope, msg_id)
        payload = self.message_body(content, reply, seq)
        url = "%s/v2/%s/%s/messages" % (
            self._api_base,
            "users" if scope == "c2c" else "groups",
            target_id,
        )
        try:
            token = await self._tokens.current(self._session)
            headers = {"Authorization": "QQBot %s" % token,
                       "Content-Type": "application/json"}
            async with self._session.post(url, json=payload, headers=headers,
                                          proxy=self._proxy or None) as resp:
                if resp.status in (200, 201, 202):
                    return True
                status, body = resp.status, await resp.text()
            print("[qq_bot] 发送被拒（%d）：%s" % (status, body[:200]))
            return False
        except Exception as exc:
            print("[qq_bot] 发送失败（%s: %s）" % (type(exc).__name__, exc))
            return False

    async def send(self, session: MessageSession, content: str | MessageChain) -> bool:
        """没有上下文的一条回复（定时任务、后台回报）。

        拿得到这个会话最近那条消息就按被动回复发，拿不到就发主动消息。
        """
        text = content if isinstance(content, str) else content.content
        scope = "group" if session.message_type is MessageType.GROUP else "c2c"
        msg_id, _ = self._budget.last_for(str(session))
        return await self._send_text(scope, str(session.session_id), text, msg_id)

    async def send_note(self, session: MessageSession, text: str,
                        event=None) -> bool:
        """一句旁白。不能走 `send()`：那个会顺手把本轮待交付文件发掉，
        用户会先看到文件再看到回答。
        """
        scope = "group" if session.message_type is MessageType.GROUP else "c2c"
        msg_id, _ = self._budget.last_for(str(session))
        return await self._send_text(scope, str(session.session_id), text, msg_id)

    async def send_media_file(self, session: MessageSession, file_path: str,
                              filename: str = "", context_token: str = "") -> bool:
        """发一个本地文件：先传素材拿 file_info，再发一条媒体消息。

        返回 False 不算送达。上传接口对文件类型有白名单，失败时如实说。
        """
        path = Path(file_path or "")
        if not path.is_file():
            print("[qq_bot] 要发的文件不存在：%s" % file_path)
            return False
        scope = "group" if session.message_type is MessageType.GROUP else "c2c"
        target_id = str(session.session_id)
        name = filename or path.name
        url = "%s/v2/%s/%s/files" % (
            self._api_base, "users" if scope == "c2c" else "groups", target_id)
        try:
            token = await self._tokens.current(self._session)
            headers = {"Authorization": "QQBot %s" % token,
                       "Content-Type": "application/json"}
            #: file_type 4 = 文件。素材要先传上去，平台不接受本地路径。
            payload = self.file_upload_payload(path.read_bytes(), name)
            async with self._session.post(url, json=payload, headers=headers,
                                          proxy=self._proxy or None) as resp:
                if resp.status not in (200, 201, 202):
                    status, body = resp.status, await resp.text()
                    print("[qq_bot] 上传文件被拒（%d）：%s" % (status, body[:200]))
                    return False
                info = await resp.json(content_type=None)
        except Exception as exc:
            print("[qq_bot] 上传文件失败（%s: %s）" % (type(exc).__name__, exc))
            return False

        file_info = str((info or {}).get("file_info") or "")
        if not file_info:
            print("[qq_bot] 上传成功但没拿到 file_info，没法发消息")
            return False

        msg_id, _ = self._budget.last_for(str(session))
        reply, seq = self._budget.reserve(scope, msg_id)
        msg = self.media_message_body(file_info, reply, seq)
        api = "%s/v2/%s/%s/messages" % (
            self._api_base, "users" if scope == "c2c" else "groups", target_id)
        try:
            token = await self._tokens.current(self._session)
            headers = {"Authorization": "QQBot %s" % token,
                       "Content-Type": "application/json"}
            async with self._session.post(api, json=msg, headers=headers,
                                          proxy=self._proxy or None) as resp:
                if resp.status in (200, 201, 202):
                    return True
                status, body = resp.status, await resp.text()
            print("[qq_bot] 发送文件消息被拒（%d）：%s" % (status, body[:200]))
            return False
        except Exception as exc:
            print("[qq_bot] 发送文件消息失败（%s: %s）" % (type(exc).__name__, exc))
            return False

    async def _drain_delivered(self, session: MessageSession) -> int:
        """把本轮登记的文件发出去，返回真发出去的条数。

        工具是同步的、不能 await 这个循环，所以它只登记；通道在回话之后
        取走发掉。三格都要对上：会话键、通道名、用户 id。
        """
        from core import delivery_outbox as _outbox
        from core.session_key import session_key_for

        try:
            recs = _outbox.take_for(
                session_key=session_key_for(session),
                channel="qq_bot_channel",
                user_id=str(getattr(session, "session_id", "") or ""))
        except Exception as exc:
            print("[qq_bot] 取待发文件失败（%s: %s）" % (type(exc).__name__, exc))
            return 0
        sent = 0
        for rec in recs:
            if await self.send_media_file(session, rec.get("path", ""),
                                          rec.get("name", "")):
                sent += 1
                continue
            await self.send_note(session, "文件 %s 没能发出去（原因在框架日志里）。"
                                 % (rec.get("name") or "?"))
        return sent

    async def test_connection(self) -> dict:
        """问一次 /users/@me。连得上说明 token 与网络都通。"""
        #: 同 `run()`：探活多半就是"刚填完凭据、按一下试试"的那一下。
        self.refresh_credentials()
        if not (self._app_id and self._app_secret):
            return {"ok": False, "message": "没有配置 AppID / AppSecret"}
        #: 每次都自建会话。探活是从别的线程（设置页那个请求）进来的，
        #:   借通道自己那条会话会把两个事件循环缠在一起。
        session = None
        try:
            import aiohttp
            session = aiohttp.ClientSession()
            tokens = _TokenManager(self._app_id, self._app_secret, self._token_url)
            token = await tokens.current(session)
            async with session.get(
                    self._api_base + "/users/@me",
                    headers={"Authorization": "QQBot %s" % token},
                    proxy=self._proxy or None) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return {"ok": False, "message": "QQ 接口返回 %d：%s"
                            % (resp.status, body[:200])}
                data = await resp.json(content_type=None) or {}
            return {"ok": True, "message": "QQ 机器人：%s"
                    % (data.get("username") or data.get("id") or "已连接")}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            if session is not None:
                await session.close()

    async def terminate(self) -> None:
        self._running = False
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
