"""
ProviderMetadata — centralised registry of provider capabilities.

Each provider's `manifest.yaml` is parsed into a `ProviderMetadata`
dataclass at load time.  The metadata is used by:

- The Provider Factory (select the right implementation class)
- Runtime validation (is the provider correctly configured?)
- Prompt assembly (can this model handle tool calls / streaming?)
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderMetadata:
    """Describes one LLM provider's static capabilities."""

    name: str
    """Provider key (e.g. 'deepseek', 'anthropic')."""

    api_mode: str = "openai_compat"
    """
    Which API protocol the provider speaks:
    - ``"openai_compat"`` — OpenAI-style ``/v1/chat/completions``
    - ``"anthropic_native"`` — Anthropic Messages API
    - ``"custom"`` — requires a manual provider class
    """

    description: str = ""
    version: str = "0.1.0"

    # --- endpoint config ---
    default_model: str = ""
    base_url: str = ""
    requires_api_key: bool = True
    requires_api_base: bool = True

    # --- capabilities ---
    context_window: int = 65536
    max_output_tokens: int = 4096
    supports_function_calling: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    #: 收不收音频（默认 False，理由见
    #: `providers/base.py::accepts_audio`）。
    supports_audio: bool = False
    supports_thinking: bool = False

    # --- pricing (per 1k tokens, USD) ---
    pricing_per_1k_input: float = 0.0
    pricing_per_1k_output: float = 0.0


# ------------------------------------------------------------------
# In-memory metadata store (lives alongside bus/registry.py)
# ------------------------------------------------------------------

_provider_metadata: dict[str, ProviderMetadata] = {}


def register_provider_metadata(meta: ProviderMetadata) -> None:
    """Store metadata for a loaded provider."""
    _provider_metadata[meta.name] = meta


def get_provider_metadata(name: str) -> Optional[ProviderMetadata]:
    """Return metadata for *name*, or None."""
    return _provider_metadata.get(name)


def list_provider_metadata() -> dict[str, ProviderMetadata]:
    """Return a shallow copy of all provider metadata."""
    return dict(_provider_metadata)


def clear_provider_metadata() -> None:
    """Reset metadata store (testing only)."""
    _provider_metadata.clear()
