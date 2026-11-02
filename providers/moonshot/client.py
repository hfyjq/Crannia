"""
Moonshot provider — OpenAI-compatible API (legacy naming).

Alias for the Kimi platform.  Thin subclass of OpenAICompatProvider.
"""

from providers.openai_compat import OpenAICompatProvider


class MoonshotProvider(OpenAICompatProvider):
    """Moonshot LLM provider (legacy API endpoint)."""
    pass
