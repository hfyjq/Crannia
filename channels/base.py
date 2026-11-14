"""
Channel ABC — unified interface for all I/O platforms (CLI, Telegram, WeChat, …).

A common `Platform` ABC plus an adapter triplet.
Every channel:
1. Receives platform-specific messages → converts to `MessageEvent`
2. Pushes events to the pipeline via `commit_event()`
3. Sends responses back via `send()`

`MessageSession` provides a globally-unique session key across platforms.

v0.7.0: `Message` now carries a `MessageChain` instead of a bare
content: str.  The `content` property is preserved for backward
compatibility.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from channels.message import MessageChain


# ------------------------------------------------------------------
# MessageType
# ------------------------------------------------------------------

class MessageType(Enum):
    PRIVATE = "private"
    GROUP = "group"


# ------------------------------------------------------------------
# MessageSession — globally-unique session identifier
# ------------------------------------------------------------------

@dataclass(frozen=True)
class MessageSession:
    """Immutable session key, unique across all platforms.

    Format when serialised: `"platform:type:session_id"`
    Example: `"telegram:private:123456789"`
    """

    platform: str
    message_type: MessageType
    session_id: str

    def __str__(self) -> str:
        return f"{self.platform}:{self.message_type.value}:{self.session_id}"

    @staticmethod
    def from_str(s: str) -> "MessageSession":
        platform, msg_type, sid = s.split(":", 2)
        return MessageSession(platform, MessageType(msg_type), sid)


# ------------------------------------------------------------------
# ChannelMetadata
# ------------------------------------------------------------------

@dataclass
class ChannelMetadata:
    """Static metadata describing a channel adapter."""

    name: str
    description: str
    platform_type: str = ""          # "cli", "telegram", "wechat"
    support_multimodal: bool = False
    support_streaming: bool = False


# ------------------------------------------------------------------
# Message — internal message representation
# ------------------------------------------------------------------

@dataclass
class Message:
    """Unified message object across all platforms.

    Channels convert their native message format into this structure
    before pushing to the pipeline.  The `message_chain` carries
    rich content (text / image / file / …); `content` is a plain-text
    fallback preserved for backward compatibility.
    """

    sender_id: str
    message_chain: MessageChain = field(default_factory=MessageChain)
    sender_name: str = ""
    message_type: MessageType = MessageType.PRIVATE
    session: MessageSession | None = None   # set by channel on receipt
    raw_data: Any = None                    # platform-specific payload
    timestamp: float = field(default_factory=datetime.now().timestamp)
    message_id: str = ""                    # v0.8.0: platform-assigned ID for dedup

    @property
    def content(self) -> str:
        """Backward-compat: plain-text extraction from the message chain."""
        return self.message_chain.content

    @staticmethod
    def from_text(sender_id: str, text: str, **kwargs: Any) -> "Message":
        """Factory: create a simple text-only message."""
        return Message(
            sender_id=sender_id,
            message_chain=MessageChain.from_text(text),
            **kwargs,
        )


# ------------------------------------------------------------------
# MessageEvent — wraps a Message for pipeline processing
# ------------------------------------------------------------------

@dataclass
class MessageEvent:
    """A message travelling through the pipeline.

    Channels create this via :meth:`Channel.commit_event` and the
    pipeline scheduler passes it through each stage in order.

    *reply_callback* decouples response delivery from pipeline logic:
    the channel sets it before committing, and the AgentLoop stage
    calls it with the final response text.
    """

    message: Message
    session: MessageSession
    platform_meta: ChannelMetadata

    # Set by the channel before commit_event — pipeline calls this
    # when a response is ready.
    reply_callback: Callable[["MessageEvent"], None] | None = None

    # Extensible key-value store for stage-to-stage communication.
    extras: dict[str, Any] = field(default_factory=dict)

    # Pipeline flow control
    _stopped: bool = False

    def stop(self) -> None:
        """Signal the pipeline to stop processing this event."""
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped


# ------------------------------------------------------------------
# Channel ABC
# ------------------------------------------------------------------

class Channel(ABC):
    """Abstract base for all message-platform adapters.

    Subclasses must implement:
    - `meta()` → :class:`ChannelMetadata`
    - `run()` → start the receive loop

    They may override:
    - `send()` → platform-specific message delivery
    - `terminate()` → graceful shutdown
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self._event_handlers: list[Callable[[MessageEvent], None]] = []

    # ---- mandatory ----

    @abstractmethod
    async def run(self) -> None:
        """Start the channel's message-receive loop.  Must be a coroutine."""
        ...

    @abstractmethod
    def meta(self) -> ChannelMetadata:
        """Return static metadata for this channel."""
        ...

    # ---- optional overrides ----

    async def send(self, session: MessageSession, content: str | MessageChain) -> bool:
        """Deliver a response to *session*. Returns True on delivery.

        *content* may be a plain `str` (backward-compat) or a
        `MessageChain` with rich components.  The default
        implementation extracts plain text and prints it.

        回执契约：`True` = 真的交出去了；
        `False` / `None` = 没送达（也叫不出声地什么都没做）。
        判据是"不抛异常"不算送达 —— 第三方审计点名的正是这里：
        钉钉只打印、飞书吞错、Telegram 没 Bot 时什么都不做，
        而定时任务那边看见"没抛"就记成了 `ok`。
每个通道必须明确回一个布尔值；`Runner` 按它记账（fail-closed）。
        """
        text = content if isinstance(content, str) else content.content
        print(f"\n[{self.meta().name}] {text}")
        # 默认不许认「打印了」为送达。
        #   这个默认体没有任何对外投递；回 `True` 就是让"将来新增的通道"
        #   在还没实现投递时被判成成功（定时任务 / 后台回报那边按 `True` 记账，
        #   于是界面上写着"已送达"而用户什么都没收到（标签在说谎）。
        #`False` = 没送达（回执契约的另一半，见上文）。
        #   自己真的交出东西的通道必须覆盖这个方法 —— 五个内置通道都覆盖了；
        #     `CLIChannel` 的"打印 = 送达"是它自己的语义，它在自己那里显式回 `True`。
        return False

    async def send_note(self, session: MessageSession, text: str,
                        event=None) -> bool:
        """一句旁白（"已收到" / "已中断"）—— 回答之外的短话。

        默认就是 :meth:`send`；**会顺手把本轮待交付文件发掉的那个通道
        必须覆盖它**。`WeChatChannel.send()` 会 `_drain_delivered()`（取走
        `core/delivery_outbox` 里本轮登记的文件）—— 拿它发旁白会在**一轮还在
        跑的时候**把附件提前发出去（用户先看到文件、再看到回答）。
那条路要一个字都不捎带。

        回执契约同 :meth:`send`：`True` = 真的交出去了。

        *event* 是触发这句旁白的那条消息（可空）：钉钉要靠它的
          `session_webhook` 才发得出去 —— 那是本次会话的回话通道
          （主动推 OpenAPI 那条路还要机器人权限，不一定通）。
        """
        return await self.send(session, text)

    async def terminate(self) -> None:
        """Graceful shutdown hook.  Called on process exit."""

    async def test_connection(self) -> dict:
        """v0.8.0: Return connection health status for this channel.

        Default returns {"ok": True, "message": "channel active"}.
        Channels that maintain persistent connections (Telegram, WeChat,
        Feishu) SHOULD override this with actual connectivity checks.
        """
        return {"ok": True, "message": f"{self.meta().name}: channel active"}

    # ---- event plumbing ----

    def on_event(self, handler: Callable[[MessageEvent], None]) -> None:
        """Register a callback to be invoked for every incoming message event.

        The callback is called synchronously by `commit_event()`.
        For async handling the callback should schedule its own task.
        """
        self._event_handlers.append(handler)

    def commit_event(self, message: Message) -> MessageEvent:
        """Adapter calls this when a new user message arrives.

        Creates a :class:`MessageEvent`, fires all registered handlers,
        and returns the event object.
        """
        # Auto-create session if the message doesn't carry one
        session = message.session or MessageSession(
            platform=self.meta().name,
            message_type=message.message_type,
            session_id=message.sender_id,
        )
        message.session = session

        event = MessageEvent(
            message=message,
            session=session,
            platform_meta=self.meta(),
        )

        for handler in self._event_handlers:
            handler(event)

        return event
