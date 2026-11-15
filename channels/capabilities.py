"""
Platform capability protocols.

Inspired by kirara-ai's `@runtime_checkable` pattern.  Protocols let
the pipeline (or other channels) test what a channel supports without
knowing its concrete type:

    if isinstance(channel, FileReceiveCapable):
        chain = await channel.receive_file(update.message)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from channels.message import MessageChain


@runtime_checkable
class EditStateCapable(Protocol):
    """The channel can send typing indicators / edit-state signals."""

    async def set_typing(self, session: Any) -> None:
        """Signal that the bot is "typing."  *session* is a `MessageSession`."""
        ...

    async def clear_typing(self, session: Any) -> None:
        """Clear the typing indicator."""
        ...


@runtime_checkable
class FileReceiveCapable(Protocol):
    """The channel can receive files / images.

    When a platform message contains a document or photo, the handler
    calls `receive_file()` to download it and produce a `MessageChain`.
    """

    async def receive_file(self, platform_message: Any) -> MessageChain:
        """Download file(s) from *platform_message* and return a MessageChain.

        The returned chain will contain one or more `File` / `Image`
        components with `local_path` set to the downloaded location.
        """
        ...


@runtime_checkable
class MediaSendCapable(Protocol):
    """The channel can send a local file / image to the user.

    判据是"这个通道自己会把字节送上去" —— 不是"它能收文字回复"。
    一个通道实现了它，就要同时负责把本轮登记在
    `core.delivery_outbox` 里的文件取走发掉（见该模块的 docstring：
    工具只登记，通道在自己的循环里发）。

    返回 `False` = 没发出去（回执契约：不抛异常不算送达）。
    """

    async def send_media_file(self, session: Any, file_path: str,
                             filename: str = "", context_token: str = "") -> bool:
        """Send one local file (image or document).  Returns True on delivery."""
        ...


@runtime_checkable
class ReactionCapable(Protocol):
    """The channel supports message reactions (👍 👎 etc.).  Reserved."""

    async def add_reaction(self, session: Any, message_id: str, emoji: str) -> None:
        """Add a reaction to a message."""
        ...

    async def remove_reaction(self, session: Any, message_id: str, emoji: str) -> None:
        """Remove a reaction."""
        ...
