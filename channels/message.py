"""
Unified message model — the "universal currency" for all platforms.

Every Channel converts its platform-native messages into a `MessageChain`
and converts `MessageChain` back to platform-native format for sending.

Key invariants:
- `MessageChain.content` always returns a plain-text string (backward compat).
- Components are plain `@dataclass` — no MediaManager, no DI, no async init.
- `Voice` and `At` are defined but not yet wired; they exist so future
  work has a clear interface to implement against.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ------------------------------------------------------------------
# ComponentType
# ------------------------------------------------------------------

class ComponentType(Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    VOICE = "voice"          # reserved — not wired yet
    AT = "at"                # reserved — not wired yet


# ------------------------------------------------------------------
# MessageComponent subclasses
# ------------------------------------------------------------------

@dataclass
class MessageComponent(ABC):
    """Abstract base for every element in a MessageChain."""

    type: ComponentType

    @abstractmethod
    def to_plain_text(self) -> str:
        """Extract a human-readable plain-text representation.

        This is used by `MessageChain.content` to provide backward
        compatibility with code that expects a single string.
        """
        ...

    def to_dict(self) -> dict[str, Any]:
        """Serialise component metadata (for debugging / logging)."""
        return {"type": self.type.value}


# -- concrete components -------------------------------------------

@dataclass
class Plain(MessageComponent):
    """Plain text."""

    text: str

    def __init__(self, text: str) -> None:
        # Bypass dataclass __init__ to set `type` automatically.
        self.type = ComponentType.TEXT
        self.text = text

    def to_plain_text(self) -> str:
        return self.text

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass
class Image(MessageComponent):
    """Image — can come from a URL, base64 data, or local file path."""

    url: str | None = None
    base64: str | None = None
    local_path: str | None = None
    mime_type: str = "image/png"

    def __init__(
        self,
        url: str | None = None,
        base64: str | None = None,
        local_path: str | None = None,
        mime_type: str = "image/png",
    ) -> None:
        self.type = ComponentType.IMAGE
        self.url = url
        self.base64 = base64
        self.local_path = local_path
        self.mime_type = mime_type

    def to_plain_text(self) -> str:
        if self.local_path:
            return f"[Image: {self.local_path}]"
        if self.url:
            return f"[Image: {self.url}]"
        return "[Image]"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "image", "mime_type": self.mime_type}
        if self.url:
            d["url"] = self.url
        if self.local_path:
            d["local_path"] = self.local_path
        return d


@dataclass
class File(MessageComponent):
    """A document or other binary file."""

    name: str
    url: str | None = None
    local_path: str | None = None
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0

    def __init__(
        self,
        name: str,
        url: str | None = None,
        local_path: str | None = None,
        mime_type: str = "application/octet-stream",
        size_bytes: int = 0,
    ) -> None:
        self.type = ComponentType.FILE
        self.name = name
        self.url = url
        self.local_path = local_path
        self.mime_type = mime_type
        self.size_bytes = size_bytes

    def to_plain_text(self) -> str:
        return f"[File: {self.name}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "file",
            "name": self.name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "local_path": self.local_path,
            "url": self.url,
        }


@dataclass
class Voice(MessageComponent):
    """Voice / audio clip.  Reserved — not wired to any channel yet."""

    url: str | None = None
    duration_seconds: int = 0

    def __init__(
        self,
        url: str | None = None,
        duration_seconds: int = 0,
    ) -> None:
        self.type = ComponentType.VOICE
        self.url = url
        self.duration_seconds = duration_seconds

    def to_plain_text(self) -> str:
        return "[Voice]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "voice",
            "url": self.url,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class At(MessageComponent):
    """@-mention.  Reserved — not wired to any channel yet."""

    target_id: str
    display_name: str = ""

    def __init__(self, target_id: str, display_name: str = "") -> None:
        self.type = ComponentType.AT
        self.target_id = target_id
        self.display_name = display_name

    def to_plain_text(self) -> str:
        name = self.display_name or self.target_id
        return f"@{name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "at",
            "target_id": self.target_id,
            "display_name": self.display_name,
        }


# ------------------------------------------------------------------
# MessageChain
# ------------------------------------------------------------------

@dataclass
class MessageChain:
    """Ordered list of components forming one message.

    This is THE internal message representation.  Every Channel's
    `MessageConverter` produces a `MessageChain` on receipt and
    consumes one on send.

    `content` returns the concatenated plain text — this keeps
    backward compatibility with every piece of code that currently
    expects Message.content: str.
    """

    components: list[MessageComponent] = field(default_factory=list)

    # -- properties --------------------------------------------------

    @property
    def content(self) -> str:
        """Plain-text concatenation of all components.

        Backward-compat: existing code that reads `message.content`
        or `ctx.user_message` continues to work unchanged.
        """
        return "".join(c.to_plain_text() for c in self.components)

    @property
    def images(self) -> list[Image]:
        """All image components in order."""
        return [c for c in self.components if isinstance(c, Image)]

    @property
    def files(self) -> list[File]:
        """All file components in order."""
        return [c for c in self.components if isinstance(c, File)]

    @property
    def is_empty(self) -> bool:
        return len(self.components) == 0

    # -- factories ---------------------------------------------------

    @staticmethod
    def from_text(text: str) -> "MessageChain":
        """Create a single-component text chain."""
        return MessageChain(components=[Plain(text)])

    @staticmethod
    def from_image(
        url: str | None = None,
        base64: str | None = None,
        local_path: str | None = None,
        mime_type: str = "image/png",
    ) -> "MessageChain":
        """Create a single-component image chain."""
        return MessageChain(
            components=[Image(url=url, base64=base64, local_path=local_path, mime_type=mime_type)]
        )

    @staticmethod
    def from_file(
        name: str,
        url: str | None = None,
        local_path: str | None = None,
        mime_type: str = "application/octet-stream",
        size_bytes: int = 0,
    ) -> "MessageChain":
        """Create a single-component file chain."""
        return MessageChain(
            components=[File(name=name, url=url, local_path=local_path, mime_type=mime_type, size_bytes=size_bytes)]
        )

    # -- helpers -----------------------------------------------------

    def add(self, component: MessageComponent) -> "MessageChain":
        """Add a component and return self (fluent)."""
        self.components.append(component)
        return self

    def __len__(self) -> int:
        return len(self.components)

    def __bool__(self) -> bool:
        return not self.is_empty

    def __repr__(self) -> str:
        summary = ", ".join(
            c.type.value if not isinstance(c, Plain) else f"\"{c.text[:30]}\""
            for c in self.components
        )
        return f"MessageChain([{summary}])" if summary else "MessageChain([])"
