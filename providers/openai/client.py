"""
OpenAI provider — native OpenAI API.

Thin subclass of OpenAICompatProvider.  Uses the standard
OpenAI chat-completions endpoint.
"""

from providers.openai_compat import OpenAICompatProvider


class OpenAIProvider(OpenAICompatProvider):
    """OpenAI GPT provider — standard chat-completions API."""
    pass
