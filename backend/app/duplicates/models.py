"""Result types for cross-application duplicate detection.

Language note, because it matters more here than anywhere else in the system:
a duplicate is a **possible duplicate**. Two institutions holding the same
certificate number is a discrepancy worth a human looking at it; it is not
evidence that either of them did anything. The reasons this module writes say
"appears in", "matches" and "requires manual verification", never "copied",
"stolen" or "forged".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DuplicateStatus(str, Enum):
    OK = "ok"
    # Nothing to compare against yet: the first document in an empty corpus.
    NO_CORPUS = "no_corpus"
    # The page could not be rendered, so the image comparison was skipped. The
    # hash-, number- and QR-based checks still ran.
    IMAGE_UNAVAILABLE = "image_unavailable"


class FindingType(str, Enum):
    EXACT_FILE = "duplicate_file_exact"
    NEAR_DUPLICATE_IMAGE = "near_duplicate_page_image"
    CERTIFICATE_NUMBER = "certificate_number_reuse"
    QR_PAYLOAD = "qr_payload_reuse"


@dataclass
class Match:
    """One other document that this one resembles, and in what way."""

    kind: FindingType
    other_document_id: int
    other_application_id: int
    other_institution_id: int
    other_institution_name: str
    same_institution: bool
    # What was compared and what came back. Hashes, numbers and distances -
    # never page text.
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "other_document_id": self.other_document_id,
            "other_application_id": self.other_application_id,
            "other_institution_id": self.other_institution_id,
            "other_institution_name": self.other_institution_name,
            "same_institution": self.same_institution,
            "evidence": self.evidence,
        }


@dataclass
class Finding:
    type: FindingType
    # "strong", "medium" or "weak", read from config by context. A label.
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
class ComparisonSkipped:
    """A check that could not run, recorded so its silence is not read as a pass."""

    check: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "reason": self.reason}


@dataclass
class DuplicateAudit:
    config_version: str
    timestamp: str
    # Size of the corpus each check actually compared against.
    documents_compared: int
    hashes_compared: int
    compare_kind: str
    hash_size: int
    near_duplicate_max_distance: int
    caps_reached: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "timestamp": self.timestamp,
            "documents_compared": self.documents_compared,
            "hashes_compared": self.hashes_compared,
            "compare_kind": self.compare_kind,
            "hash_size": self.hash_size,
            "near_duplicate_max_distance": self.near_duplicate_max_distance,
            "caps_reached": self.caps_reached,
        }


@dataclass
class DuplicateCheckResult:
    document_id: int
    status: DuplicateStatus
    matches: list[Match] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    comparisons_skipped: list[ComparisonSkipped] = field(default_factory=list)
    # Hashes computed for this document this run, stored for later comparisons.
    hashes_stored: list[dict[str, Any]] = field(default_factory=list)
    audit: DuplicateAudit | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "status": self.status.value,
            "matches": [m.to_dict() for m in self.matches],
            "findings": [f.to_dict() for f in self.findings],
            "comparisons_skipped": [c.to_dict() for c in self.comparisons_skipped],
            "hashes_stored": self.hashes_stored,
            "audit": self.audit.to_dict() if self.audit else None,
        }
