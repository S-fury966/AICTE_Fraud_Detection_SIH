"""The project's fixed verification status taxonomy.

Defined once, here, because more than one detector will set it. The set is
closed: CLAUDE.md fixes these six values, and a detector that needs a seventh
needs a conversation, not a new enum member.

Set by code logic only. No LLM ever chooses one of these.
"""

from __future__ import annotations

from enum import Enum


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    MISMATCH_DETECTED = "mismatch_detected"
    EXPIRED = "expired"
    VERIFICATION_PENDING = "verification_pending"
    SOURCE_UNAVAILABLE = "source_unavailable"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


def parse_status(value: str) -> VerificationStatus:
    """Turn a configured string into a status, rejecting anything outside the set.

    Config files name statuses, so a typo there would otherwise invent a status
    that no reviewer or later step knows how to read.
    """
    try:
        return VerificationStatus(str(value))
    except ValueError as exc:
        allowed = ", ".join(status.value for status in VerificationStatus)
        raise ValueError(
            f"{value!r} is not a verification status; allowed: {allowed}"
        ) from exc


# Least to most severe. The issuer detector reads its own ordering from config
# (`status_severity`); detectors without one use this, which is the same list.
DEFAULT_SEVERITY = (
    VerificationStatus.VERIFIED,
    VerificationStatus.VERIFICATION_PENDING,
    VerificationStatus.EXPIRED,
    VerificationStatus.SOURCE_UNAVAILABLE,
    VerificationStatus.MANUAL_REVIEW_REQUIRED,
    VerificationStatus.MISMATCH_DETECTED,
)


def most_severe(statuses: list[VerificationStatus | str]) -> VerificationStatus | None:
    """The most severe of several statuses, or None for an empty list."""
    parsed = [parse_status(str(getattr(s, "value", s))) for s in statuses]
    if not parsed:
        return None
    return max(parsed, key=DEFAULT_SEVERITY.index)
