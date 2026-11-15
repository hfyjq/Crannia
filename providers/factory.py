"""
Provider Factory — auto-select the right implementation class.

Reads `provider_metadata.api_mode` and instantiates the matching
provider class.  Supports:

- `"openai_compat"` → OpenAICompatProvider (DeepSeek, GLM, Qwen, ...)
- `"anthropic_native"` → AnthropicProvider (Claude models)
- `"custom"` → deferred to plugin (future)

A single factory maps a provider kind to its implementation class.

v0.8.5: Initial implementation.
"""

from typing import Any, Optional


def create_provider(
    name: str,
    metadata: dict,
    config: dict,
) -> Any:
    """
    Factory function: instantiate the right provider class based on
    `api_mode` in *metadata*.

    Args:
        name: Provider key (e.g. "deepseek", "anthropic").
        metadata: Provider metadata dict from bus/registry.
        config: Full application config dict (from config.yaml).

    Returns:
        A BaseProvider instance.

    Raises:
        ValueError: If api_mode is unknown or required config is missing.
    """
    api_mode = metadata.get("api_mode", "openai_compat")
    model_cfg = config.get("model", {})
    rate_limit_cfg = model_cfg.get("rate_limit", {})

    if api_mode == "openai_compat":
        return _create_openai_compat(name, metadata, model_cfg, rate_limit_cfg)
    elif api_mode == "anthropic_native":
        return _create_anthropic(name, metadata, model_cfg, rate_limit_cfg, config)
    elif api_mode == "custom":
        raise NotImplementedError(
            f"Provider '{name}' uses api_mode='custom'. "
            f"Custom providers must be loaded via plugin system."
        )
    else:
        raise ValueError(
            f"Unknown api_mode '{api_mode}' for provider '{name}'. "
            f"Valid modes: openai_compat, anthropic_native, custom."
        )


# ------------------------------------------------------------------
# Internal factory methods
# ------------------------------------------------------------------

def _create_openai_compat(
    name: str,
    metadata: dict,
    model_cfg: dict,
    rate_limit_cfg: dict,
) -> Any:
    """Create an OpenAICompatProvider (or subclass)."""
    from core import window_estimate
    from providers.openai_compat import OpenAICompatProvider

    _model_name = model_cfg.get("model_name", metadata.get("default_model", ""))
    init_kwargs: dict = {
        "api_key": model_cfg.get("api_key", ""),
        "base_url": model_cfg.get("base_url", metadata.get("base_url", "")),
        "model": _model_name,
        "temperature": model_cfg.get("temperature", 0.3),
        # 同 `provider_loader`：按模型能力定，不按常数。
        "max_tokens": window_estimate.resolve_max_tokens(name, _model_name),
        "context_window": model_cfg.get(
            "context_window",
            metadata.get("context_window", 65536),
        ),
        # 价格表按 `(provider, model)` 查。
        "provider_name": name,
    }

    # Optional multi-key pool
    api_keys = model_cfg.get("api_keys")
    if api_keys and isinstance(api_keys, list):
        init_kwargs["api_keys"] = api_keys

    # Optional rate limiting
    if rate_limit_cfg:
        # 缺省 0 = 不启用客户端限流（详见 config.yaml 的注释）
        init_kwargs["rpm"] = rate_limit_cfg.get("rpm", 0)
        init_kwargs["tpm"] = rate_limit_cfg.get("tpm", 0)

    return OpenAICompatProvider(**init_kwargs)


def _create_anthropic(
    name: str,
    metadata: dict,
    model_cfg: dict,
    rate_limit_cfg: dict,
    config: dict | None = None,
) -> Any:
    """Create an AnthropicProvider."""
    from providers.anthropic.client import AnthropicProvider

    # For Anthropic, the API key should come from ANTHROPIC_API_KEY env var
    # or be explicitly set in config under a dedicated section
    config = config or {}
    anthropic_cfg = config.get("anthropic", {})
    api_key = anthropic_cfg.get("api_key") or model_cfg.get("api_key", "")

    kwargs: dict = {
        "api_key": api_key,
        "model": model_cfg.get("model_name", metadata.get("default_model", "claude-sonnet-4-20250514")),
        "temperature": model_cfg.get("temperature", 0.3),
        "max_tokens": model_cfg.get("max_tokens", 8192),
        "context_window": model_cfg.get("context_window", metadata.get("context_window", 200000)),
        "provider_name": name,          # 价格表按 `(provider, model)` 查
    }

    base_url = anthropic_cfg.get("base_url") or model_cfg.get("base_url", "")
    if base_url and "anthropic.com" not in base_url:
        kwargs["base_url"] = base_url

    thinking_budget = anthropic_cfg.get("thinking_budget", 0)
    if thinking_budget > 0:
        kwargs["thinking_budget"] = thinking_budget

    return AnthropicProvider(**kwargs)
