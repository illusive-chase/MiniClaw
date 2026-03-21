"""Provider factory."""

from .anthropic_provider import AnthropicProvider
from .base import Provider
from .openai_provider import OpenAIProvider


def create_provider(config: dict) -> Provider:
    """Create a provider from config."""
    provider_type = config.get("type", "openai")
    api_key = config.get("api_key", "")
    model = config.get("model", "")

    max_tokens = int(config.get("max_tokens", 8192))
    delay = float(config.get("delay", 0.0))

    if provider_type == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            base_url=config.get("base_url"),
            model=model or "claude-sonnet-4-6",
            max_tokens=max_tokens,
            delay=delay,
        )
    else:
        return OpenAIProvider(
            api_key=api_key,
            base_url=config.get("base_url"),
            model=model or "gpt-4o",
            max_tokens=max_tokens,
        )
