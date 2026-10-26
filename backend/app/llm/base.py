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
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class FailureCategory(str, Enum):
    """Why a provider call did not return usable output.

    The category, not the message, is what the extractor's `fallback_on` list
    matches against: matching on a human-readable message would be brittle and
    silently stop working the first time wording changed.

    The seven transport categories are the ones `fallback_on` governs. The last
    two describe a provider that cannot serve this request at all - a missing
    key or no request allowance - and the offline reader always stands in for
    those when one is configured, because that is the original
    demo-without-a-key contract, not something the transport list decides.
    """

    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    SERVER_ERROR = "server_error"
    RATE_LIMITED = "rate_limited"
    AUTH_REJECTED = "auth_rejected"
    MODEL_OR_ENDPOINT_NOT_FOUND = "model_or_endpoint_not_found"
    OTHER_TRANSPORT = "other_transport"
    # Not governed by `fallback_on`; always falls back when a reader exists.
    NOT_CONFIGURED = "not_configured"
    ZERO_QUOTA = "zero_quota"


class LLMError(RuntimeError):
    """Base for provider errors, carrying a category and a sanitised message.

    `sanitized` is a short description safe to store and show: it never
    contains an API key, a token or a full request URL. The ordinary message
    may carry more (a response-body excerpt) for logs, but only `sanitized` is
    written into the audit trail and surfaced to a reviewer.
    """

    def __init__(
        self,
        message: str,
        category: FailureCategory = FailureCategory.OTHER_TRANSPORT,
        sanitized: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.sanitized = sanitized or category.value


class LLMUnavailableError(LLMError):
    """The provider cannot run at all: missing SDK, missing key, bad config.

    Defaults to the `not_configured` category. 401/403 and 404 are also raised
    as this type (an authenticated call to a misconfigured key or endpoint is
    not going to come good on retry), but they carry `auth_rejected` and
    `model_or_endpoint_not_found` so `fallback_on` can govern them.
    """

    def __init__(
        self,
        message: str,
        category: FailureCategory = FailureCategory.NOT_CONFIGURED,
        sanitized: str | None = None,
    ) -> None:
        super().__init__(message, category, sanitized)


class LLMCallError(LLMError):
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
