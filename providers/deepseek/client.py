"""
DeepSeek provider — OpenAI-compatible API.

Thin subclass of OpenAICompatProvider.  The shared logic lives in
providers/openai_compat.py; this module only declares DeepSeek-specific
defaults.
"""

from providers.openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    """
    DeepSeek LLM provider.

    Uses the OpenAI-compatible chat-completions endpoint.
    Token counting via tiktoken `cl100k_base` (good approximation;
    actual token counts are retrieved from the API `usage` field).
    """
    pass
