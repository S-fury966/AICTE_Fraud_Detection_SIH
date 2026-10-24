"""Result types for certificate field extraction.

As in the extraction module, these statuses describe whether *extraction*
worked. They say nothing about whether the certificate is genuine. The
project's verification taxonomy (verified, mismatch_detected, expired,
verification_pending, source_unavailable, manual_review_required) is applied
later, by the detectors, from evidence this module and its siblings supply.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class FieldExtractionStatus(str, Enum):
    OK = "ok"
    # The model never returned a payload matching the schema.
    EXTRACTION_FAILED = "extraction_failed"
    # The provider could not be reached or is not configured.
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class InvalidationReason(str, Enum):
    """Why code rejected or flagged a value the model returned."""

    # The guard against hallucination: the quoted snippet is not in the text.
    SNIPPET_NOT_IN_SOURCE = "snippet_not_in_source"
    # The snippet matched, but an identifier-like value is not in the text
    # character for character. Catches a single altered digit.
    VALUE_NOT_IN_SOURCE = "value_not_in_source"
    SNIPPET_MISSING = "snippet_missing"
    SNIPPET_TOO_SHORT = "snippet_too_short"
    # Value kept, but it could not be turned into an ISO date.
    DATE_UNPARSED = "date_unparsed"
    EMPTY_VALUE = "empty_value"


@dataclass
class FieldResult:
    """One extracted field, after code has checked it."""

    name: str
    # str for most fields, list[str] for programme, None when absent or
    # rejected.
    value: Any
    source_snippet: str | None
    page: int | None
    # The model's own reading confidence: low, medium or high. Never used as a
    # measure of truth, only shown to the reviewer.
    confidence: str | None
    # Set by our code, not the model: did every applicable check pass.
    validated: bool

    # Why the value was nulled or flagged.
    invalidation_reason: InvalidationReason | None = None
    # How the snippet was located: exact, fuzzy, absent, no_snippet, skipped.
    snippet_match: str | None = None
    snippet_similarity: float | None = None
    # For fields under the exact-value rule: whether the value itself was
    # found verbatim. None when the rule does not apply to this field.
    value_match: str | None = None
    # True when the page number the model gave was wrong but the snippet was
    # found on another page.
    page_corrected: bool = False

    # Date fields only.
    value_iso: str | None = None
    date_note: str | None = None
    date_ambiguous: bool = False

    # What the model originally returned, kept when code overrode it.
    model_value: Any = None


@dataclass
class ConsistencyReport:
    """Logical checks across the date fields. Reported, never scored."""

    as_of: str
    valid_from_before_valid_to: bool | None = None
    expired: bool | None = None
    days_until_expiry: int | None = None
    issue_date_after_valid_from: bool | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class AuditRecord:
    """Everything needed to reproduce and review one extraction."""

    provider: str
    model: str
    prompt_version: str
    config_version: str | None
    input_text_sha256: str
    input_text_chars: int
    timestamp: str
    retries_used: int
    attempts: int
    schema_errors: list[list[str]] = field(default_factory=list)
    raw_responses: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    # True when the response came from the on-disk cache rather than the
    # provider. Recorded because a stored run must be able to say whether the
    # model was actually asked - token counts of zero and an unchanged answer
    # are otherwise indistinguishable from a very consistent model.
    cache_hit: bool = False


@dataclass
class CertificateExtractionResult:
    source_path: str | None
    status: FieldExtractionStatus
    fields: dict[str, FieldResult] = field(default_factory=dict)
    # Fields with no value, for whatever reason. Later steps use this to know
    # where they must fall back to other evidence, such as the QR payload.
    missing_fields: list[str] = field(default_factory=list)
    # Fields the model supplied but code rejected, a subset of missing_fields.
    rejected_fields: list[str] = field(default_factory=list)
    consistency: ConsistencyReport | None = None
    audit: AuditRecord | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict with enums reduced to their string values."""

        def unwrap(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {k: unwrap(v) for k, v in value.items()}
            if isinstance(value, list):
                return [unwrap(v) for v in value]
            return value

        return unwrap(asdict(self))
