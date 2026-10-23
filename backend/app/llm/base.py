"""The LLM provider seam.

A provider is anything that turns a prompt into text. Keeping it to a single
method means the backing model can be swapped (any OpenAI-compatible
endpoint, a local model, a canned stub) without touching any extraction
logic.

Providers never interpret the response. Parsing, validation and every decision
about what the text means belong to the calling module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class LLMUnavailableError(RuntimeError):
    """The provider cannot run at all: missing SDK, missing key, bad config."""


class LLMCallError(RuntimeError):
    """The provider was available but this particular call failed."""


@dataclass
class LLMCompletion:
    """One completion, plus whatever the provider can tell us about it."""

    text: str
    model: str
    provider: str
    # Token counts where the provider reports them, for cost tracking.
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Provider-specific extras worth keeping in the audit trail, e.g. a
    # request id or a stop reason.
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Prompt in, text out."""

    name: str
    model: str

    def complete(self, prompt: str) -> LLMCompletion:
        """Return the model's response to `prompt`.

        Raises LLMUnavailableError if the provider cannot run at all, and
        LLMCallError if this call failed.
        """
        ...
