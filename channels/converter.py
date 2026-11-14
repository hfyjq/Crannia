"""
Message converter ABC — platform ↔ MessageChain translation.

Each Channel owns a concrete converter that translates between its
platform-native message format and Crannia's `MessageChain`.

Crannia does not need a separate `EventConverter` because we
only have two event types (PRIVATE / GROUP), covered by `MessageType`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from channels.message import MessageChain


class MessageConverter(ABC):
    """Translate between `MessageChain` and platform-native formats.

    Subclasses implement two methods:

    - `to_internal` — called when a message arrives from the platform
    - `to_platform` — called when the pipeline wants to send a response
    """

    @abstractmethod
    def to_internal(self, platform_message: Any) -> MessageChain:
        """Convert a platform-native message into a MessageChain.

        Args:
            platform_message: The raw message object from the platform
                (`str` for CLI, `telegram.Update` for Telegram, etc.).

        Returns:
            A MessageChain representing the message content.
        """
        ...

    @abstractmethod
    def to_platform(self, message_chain: MessageChain, **kwargs: Any) -> Any:
        """Convert a MessageChain into a platform-sendable format.

        Args:
            message_chain: The content to send.
            **kwargs: Platform-specific extra parameters
                (e.g. `reply_to`, `parse_mode` for Telegram).

        Returns:
            A platform-specific object ready to be passed to the
            platform SDK's send method.
        """
        ...
