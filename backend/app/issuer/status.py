"""Mapping an outcome to one of the six fixed verification statuses.

Code decides which *reason key* applies; the config table decides what that
key is called. Both halves matter: the precedence below is logic and belongs
in code, while an organisation that wants a suspended certificate routed to
manual review rather than reported as a mismatch should be able to say so
without a code change.

No LLM participates in any of this.
"""

from __future__ import annotations

from typing import Any

from ..core.config import get
from ..core.verification import VerificationStatus, parse_status

# Reason keys, in the order they are tested. The first that applies wins.
#
# The order encodes judgements worth stating:
#   - A missing or unreadable answer is decided before anything about the
#     record, because without an answer there is no record to reason about.
#   - revoked/suspended outranks a field mismatch: the issuer actively
#     withdrew the certificate, which is a stronger statement than a
#     disagreeing field.
#   - expired is tested last among found-record cases, so a certificate that
#     is both expired and mismatched is reported as a mismatch. Expiry is a
#     fact about dates; a mismatch is a question about the document.
PRECEDENCE = (
    "no_certificate_id",
    "source_unavailable",
    "malformed_response",
    "not_found",
    "issuer_pending",
    "issuer_status_revoked",
    "issuer_status_suspended",
    "field_mismatch",
    "expired_per_issuer",
    "all_fields_match",
)


def map_status(reason_keys: list[str], cfg: dict[str, Any]) -> VerificationStatus:
    """Resolve the applicable reason keys to a single status."""
    table = get(cfg, "status_mapping", {}) or {}
    applicable = [key for key in PRECEDENCE if key in reason_keys]
    if not applicable:
        # No key matched: route to a human rather than invent a verdict.
        return VerificationStatus.MANUAL_REVIEW_REQUIRED

    chosen = applicable[0]
    if chosen not in table:
        raise ValueError(
            f"status_mapping in config has no entry for {chosen!r}; "
            f"present: {', '.join(sorted(table))}"
        )
    return parse_status(table[chosen])


def ordered_reasons(reason_keys: list[str]) -> list[str]:
    """The applicable keys in precedence order, for the output."""
    return [key for key in PRECEDENCE if key in reason_keys]


def most_severe(
    statuses: list[VerificationStatus], cfg: dict[str, Any]
) -> VerificationStatus:
    """The document-level status when there is more than one lookup.

    Severity order comes from config. A status not listed there sorts last
    rather than raising: a missing entry should not stop a document being
    reviewed.
    """
    if not statuses:
        return VerificationStatus.MANUAL_REVIEW_REQUIRED

    order = [str(value) for value in (get(cfg, "status_severity", []) or [])]

    def rank(status: VerificationStatus) -> int:
        try:
            return order.index(status.value)
        except ValueError:
            return -1

    return max(statuses, key=rank)


def strength_for(finding_type: Any, cfg: dict[str, Any]) -> str:
    """The configured label for a finding type. Read, never computed."""
    key = getattr(finding_type, "value", finding_type)
    return str(get(cfg, f"strength.{key}", "weak"))
