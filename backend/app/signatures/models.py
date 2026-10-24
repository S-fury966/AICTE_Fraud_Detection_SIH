"""Result types for the PDF signature integrity check.

Nothing here decides authenticity. The scope is one narrow question - *was
this file modified after it was signed?* - and the output says so in every
field it exposes: `signer_identity_verified` is hard-wired to `False`, and the
signer name and signing time carry `_unverified` in their names so no caller
can read them as established fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SignatureStatus(str, Enum):
    """The file-level outcome of the check."""

    # No signature structures found at all. A normal, successful result: most
    # EoA attachments are scans and were never signed.
    ABSENT = "signature_absent"
    # At least one signature, every one of them intact and covering the file.
    PRESENT_INTACT = "present_and_byte_range_intact"
    # At least one signature whose covered bytes no longer hash to the signed
    # digest, or whose coverage does not extend to the end of the file.
    MODIFIED_AFTER_SIGNING = "modified_after_signing"
    # A signature exists but could not be read well enough to judge.
    UNVERIFIABLE = "unverifiable"


class CoverageLevel(str, Enum):
    """How much of the file the signature's /ByteRange actually covers.

    Mirrors pyHanko's `SignatureCoverageLevel`, renamed into our own vocabulary
    so the rest of the codebase does not depend on a third-party enum.
    """

    ENTIRE_FILE = "entire_file"
    # The signature covers a complete earlier revision; more was appended
    # afterwards. Expected when a file carries two signatures, or when a tool
    # added long-term validation data.
    ENTIRE_REVISION = "entire_revision"
    # The covered bytes run from the start of the file but stop somewhere that
    # is not a revision boundary.
    CONTIGUOUS_BLOCK_FROM_START = "contiguous_block_from_start"
    UNCLEAR = "unclear"
    UNKNOWN = "unknown"


class FindingType(str, Enum):
    DIGEST_MISMATCH = "signature_digest_mismatch"
    DOES_NOT_COVER_DOCUMENT = "signature_does_not_cover_document"
    BYTES_AFTER_SIGNED_REVISION = "bytes_after_signed_revision"
    UNPARSEABLE = "signature_unparseable"
    UNSIGNED_WHERE_EXPECTED = "unsigned_where_signature_expected"
    WEAK_DIGEST_ALGORITHM = "signature_weak_digest_algorithm"


class SignatureError(str, Enum):
    FILE_NOT_FOUND = "file_not_found"
    NOT_A_PDF = "not_a_pdf"
    CORRUPT_FILE = "corrupt_file"
    PASSWORD_PROTECTED = "password_protected"
    LIBRARY_UNAVAILABLE = "library_unavailable"


@dataclass
class AppendedBytes:
    """What exists after the signed revision, measured rather than quoted.

    Object count and byte length only. A content dump would put document
    material into the findings table, which is exactly what the audit rules
    forbid, and a reviewer deciding whether an append is legitimate needs the
    size and shape of it, not its text.
    """

    byte_length: int
    object_count: int
    revision_count_after: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "byte_length": self.byte_length,
            "object_count": self.object_count,
            "revision_count_after": self.revision_count_after,
        }


@dataclass
class SignatureDetail:
    """One signature, and everything the integrity check could establish."""

    field_name: str | None
    # True whenever a signature structure was located, whether or not it read.
    present: bool
    parsed: bool
    # Null when the signature would not parse.
    byte_range: list[int] | None = None
    coverage: CoverageLevel = CoverageLevel.UNKNOWN
    covers_whole_file: bool = False
    # None when no comparison could be made.
    digest_matches: bool | None = None
    digest_algorithm: str | None = None
    # Recomputed by us over the /ByteRange gaps.
    computed_digest_hex: str | None = None
    # The `messageDigest` attribute from inside the CMS SignedData.
    signed_digest_hex: str | None = None
    appended: AppendedBytes | None = None

    # --- unverified, and named so that nobody can forget it ---
    # The subject of the signing certificate as printed in the file. We do not
    # check that certificate against any trust store, so this is a claim.
    signer_name_unverified: str | None = None
    # The /M entry or the CMS signingTime attribute. Supplied by whoever
    # signed; no timestamp authority is consulted.
    claimed_signing_time_unverified: str | None = None
    # Hard-wired. There is no code path in this module that could set it True.
    signer_identity_verified: bool = False

    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "present": self.present,
            "parsed": self.parsed,
            "byte_range": self.byte_range,
            "coverage": self.coverage.value,
            "covers_whole_file": self.covers_whole_file,
            "digest_matches": self.digest_matches,
            "digest_algorithm": self.digest_algorithm,
            "computed_digest_hex": self.computed_digest_hex,
            "signed_digest_hex": self.signed_digest_hex,
            "appended": self.appended.to_dict() if self.appended else None,
            "signer_name_unverified": self.signer_name_unverified,
            "claimed_signing_time_unverified": self.claimed_signing_time_unverified,
            "signer_identity_verified": self.signer_identity_verified,
            "parse_error": self.parse_error,
        }


@dataclass
class Finding:
    """One observation for a reviewer, with what it rests on."""

    type: FindingType
    # "strong", "medium" or "weak", read from config. A label, not a score.
    strength: str
    # One plain sentence. Describes what was measured, never a motive.
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
class SignatureAudit:
    config_version: str
    timestamp: str
    input_sha256: str | None
    input_bytes: int | None
    library: str
    library_version: str
    signature_count: int
    # True when the PDF reader could not enumerate signatures and the raw
    # /ByteRange fallback was used instead.
    used_raw_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "timestamp": self.timestamp,
            "input_sha256": self.input_sha256,
            "input_bytes": self.input_bytes,
            "library": self.library,
            "library_version": self.library_version,
            "signature_count": self.signature_count,
            "used_raw_fallback": self.used_raw_fallback,
        }


@dataclass
class SignatureCheckResult:
    source_path: str | None
    status: SignatureStatus
    signatures: list[SignatureDetail] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # Stated on every result, so no consumer has to remember it.
    scope_note: str = (
        "Integrity only: this check establishes whether the file changed after "
        "signing. Signer identity is NOT verified - no certificate chain or "
        "trust store was consulted."
    )
    audit: SignatureAudit | None = None
    error: SignatureError | None = None
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "status": self.status.value,
            "signatures": [s.to_dict() for s in self.signatures],
            "findings": [f.to_dict() for f in self.findings],
            "scope_note": self.scope_note,
            "audit": self.audit.to_dict() if self.audit else None,
            "error": self.error.value if self.error else None,
            "error_detail": self.error_detail,
        }
