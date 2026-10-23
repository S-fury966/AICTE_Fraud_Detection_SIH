"""Provider lookup by the name given in config.

Every hosted provider here is the same OpenAI-compatible client; what differs
is the `base_url`, model and key, all of which come from config. Adding a new
free tier is a name in this table plus a block in the YAML.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import LLMProvider, LLMUnavailableError
from .openai_compatible import OpenAICompatibleProvider
from .stub_provider import StubProvider

# Names that resolve to the OpenAI-compatible client. The name is passed
# through so error messages say which endpoint refused.
_OPENAI_COMPATIBLE = ("mistral", "groq", "openrouter", "together", "local")


def _openai_compatible(name: str) -> Callable[[dict[str, Any]], LLMProvider]:
    def factory(settings: dict[str, Any]) -> LLMProvider:
        return OpenAICompatibleProvider({"name": name, **settings})

    return factory


_PROVIDERS: dict[str, Callable[[dict[str, Any]], LLMProvider]] = {
    name: _openai_compatible(name) for name in _OPENAI_COMPATIBLE
}
_PROVIDERS["stub"] = StubProvider


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def get_provider(name: str, settings: dict[str, Any] | None = None) -> LLMProvider:
    """Build the provider registered under `name`."""
    try:
        factory = _PROVIDERS[name]
    except KeyError as exc:
        raise LLMUnavailableError(
            f"unknown LLM provider {name!r}; available: "
            f"{', '.join(available_providers())}"
        ) from exc
    return factory(settings or {})
