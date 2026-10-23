"""Result types for issuer verification.

The status here is one of the six fixed values from CLAUDE.md, chosen by the
mapping table in config. Findings carry evidence and a strength label; nothing
in this module computes a score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.verification import VerificationStatus


class FindingType(str, Enum):
    RECORD_NOT_FOUND = "issuer_record_not_found"
    FIELD_MISMATCH = "issuer_field_mismatch"
    STATUS_NOT_ACTIVE = "issuer_status_not_active"
    QR_ID_OTHER_INSTITUTION = "issuer_qr_id_other_institution"
    EXPIRED = "issuer_expired"
    NO_CERTIFICATE_ID = "issuer_no_certificate_id"
    SOURCE_UNAVAILABLE = "issuer_source_unavailable"
    MALFORMED_RESPONSE = "issuer_malformed_response"
    QR_ID_DIFFERS = "issuer_qr_id_differs_from_document"
    LOWER_TRUST_LOOKUP = "issuer_lookup_lower_trust"


class IdOrigin(str, Enum):
    DOCUMENT_NUMBER = "document_certificate_number"
    QR_CODE = "qr_certificate_id"


class FieldOutcome(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    # The comparison could not run. Never a mismatch: an absent submitted
    # value is an absence of evidence, not evidence of conflict.
    NOT_COMPARED = "not_compared"


class LookupOutcome(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"


@dataclass
class FieldComparison:
    name: str
    outcome: FieldOutcome
    submitted_value: Any = None
    issuer_value: Any = None
    # Why it was not compared, or how it matched.
    reason: str | None = None
    similarity: float | None = None
    method: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outcome": self.outcome.value,
            "submitted_value": self.submitted_value,
            "issuer_value": self.issuer_value,
            "reason": self.reason,
            "similarity": self.similarity,
            "method": self.method,
        }


@dataclass
class Finding:
    type: FindingType
    strength: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "strength": self.strength,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass
class RequestAttempt:
    """One HTTP attempt, recorded so the retry history is auditable."""

    number: int
    http_status: int | None
    latency_ms: float
    error: str | None = None
    # Seconds slept before the next attempt, if any.
    waited_before_next: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "http_status": self.http_status,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
            "waited_before_next": self.waited_before_next,
        }


@dataclass
class LookupResult:
    """One lookup: the id asked about, and what the issuer said."""

    certificate_id: str
    id_origin: IdOrigin
    # "normal" or "lower", from config. A QR id is a fallback source.
    trust: str
    outcome: LookupOutcome
    status: VerificationStatus
    # The reason keys that drove the status, in the order they were decided.
    status_reasons: list[str] = field(default_factory=list)
    record: dict[str, Any] | None = None
    comparisons: list[FieldComparison] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    http_status: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "certificate_id": self.certificate_id,
            "id_origin": self.id_origin.value,
            "trust": self.trust,
            "outcome": self.outcome.value,
            "status": self.status.value,
            "status_reasons": self.status_reasons,
            "record": self.record,
            "comparisons": [c.to_dict() for c in self.comparisons],
            "findings": [f.to_dict() for f in self.findings],
            "http_status": self.http_status,
            "error": self.error,
        }


@dataclass
class IssuerAudit:
    config_version: str
    issuer_base_url: str
    issuer_name: str | None
    registry_version: str | None
    registry_sha256: str | None
    timestamp: str
    as_of: str
    # One entry per lookup, each with its own request history.
    requests: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "issuer_base_url": self.issuer_base_url,
            "issuer_name": self.issuer_name,
            "registry_version": self.registry_version,
            "registry_sha256": self.registry_sha256,
            "timestamp": self.timestamp,
            "as_of": self.as_of,
            "requests": self.requests,
        }


@dataclass
class IssuerVerificationResult:
    source_path: str | None
    status: VerificationStatus
    # Why the document-level status is what it is.
    status_reasons: list[str] = field(default_factory=list)
    lookups: list[LookupResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    audit: IssuerAudit | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "status": self.status.value,
            "status_reasons": self.status_reasons,
            "lookups": [lookup.to_dict() for lookup in self.lookups],
            "findings": [f.to_dict() for f in self.findings],
            "audit": self.audit.to_dict() if self.audit else None,
        }
