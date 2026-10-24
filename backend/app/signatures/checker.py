"""Does this PDF's signature still cover what it claims to cover?

That is the whole question. The scope was fixed before this module was written:
**integrity only**. We do not validate the signing certificate against a chain
or a trust store, so we never learn who signed - only whether the bytes the
signature vouches for are still the bytes in the file. Every result says so.

Two libraries, each doing what it is actually good at:

- **pyHanko** locates the signature fields, reads `/ByteRange`, and works out
  how much of the file the range covers (its `SignatureCoverageLevel` is
  exactly the primitive this check needs, and re-deriving revision boundaries
  from scratch is where subtle bugs live).
- **hashlib plus asn1crypto**, ours, does the digest comparison: recompute over
  the byte-range gaps, and read the signed `messageDigest` out of the CMS
  SignedData.

That second split is not arbitrary. pyHanko's `external_digests` looks like the
signed digest and is not - it is a cache of *recomputed* digests, so comparing
it against our recomputation would always agree and the check would pass on a
tampered file. A test asserts that our recomputation agrees with pyHanko's
`compute_digest()`, which is the part of pyHanko that is genuinely independent.

No LLM anywhere in this module, and nothing here reaches the network.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import get, load_config
from .models import (
    AppendedBytes,
    CoverageLevel,
    Finding,
    FindingType,
    SignatureAudit,
    SignatureCheckResult,
    SignatureDetail,
    SignatureError,
    SignatureStatus,
)

# pyHanko's coverage levels, mapped into our own vocabulary. Keyed by the
# library's enum *name* rather than its value so a renumbering upstream cannot
# silently turn "covers nothing" into "covers everything".
_COVERAGE_BY_NAME = {
    "ENTIRE_FILE": CoverageLevel.ENTIRE_FILE,
    "ENTIRE_REVISION": CoverageLevel.ENTIRE_REVISION,
    "CONTIGUOUS_BLOCK_FROM_START": CoverageLevel.CONTIGUOUS_BLOCK_FROM_START,
    "UNCLEAR": CoverageLevel.UNCLEAR,
}

# pyHanko's diff analysis logs an error with a traceback whenever a signed
# revision differs from a later one - which, for the one question this module
# asks, is a normal and expected finding rather than a fault. We do not use the
# diff result (judging *whether* an append is legitimate is the reviewer's job,
# not ours), so the traceback is noise that would appear in CLI output next to
# a finding that already says the same thing in plain words.
_PYHANKO_DIFF_LOGGER = "pyhanko.sign.diff_analysis.policies"


@contextlib.contextmanager
def _quiet_diff_analysis():
    """Silence pyHanko's diff-analysis logger for the duration of a read.

    Scoped to the call and restored afterwards, so this never changes logging
    for the rest of the process.
    """
    logger = logging.getLogger(_PYHANKO_DIFF_LOGGER)
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


# Reviewer-facing wording for the appended-bytes caveat. Kept in one place
# because it is the sentence that stops a legitimate append reading as an
# accusation, and it must be identical wherever it appears.
_LEGITIMATE_APPEND_CAVEAT = (
    "some legitimate tools append data to a signed PDF - for example "
    "long-term validation data, or a second signature - so this requires "
    "manual verification rather than being a problem on its own"
)


def check_signatures(
    path: str | Path,
    config: dict[str, Any] | None = None,
    document_type: str | None = None,
    issuing_body: str | None = None,
) -> SignatureCheckResult:
    """Inspect every signature in the PDF at `path`.

    `document_type` and `issuing_body` are only ever read to decide whether an
    *unsigned* document should be flagged, which is off by default.
    """
    cfg = config if config is not None else load_config("signature_checks")
    source = Path(path)
    result = SignatureCheckResult(
        source_path=str(source), status=SignatureStatus.ABSENT
    )

    if not source.exists():
        result.error = SignatureError.FILE_NOT_FOUND
        result.error_detail = f"no file at {source}"
        result.status = SignatureStatus.UNVERIFIABLE
        return result

    try:
        data = source.read_bytes()
    except OSError as exc:
        result.error = SignatureError.CORRUPT_FILE
        result.error_detail = f"could not read file: {exc}"
        result.status = SignatureStatus.UNVERIFIABLE
        return result

    result.audit = SignatureAudit(
        config_version=str(get(cfg, "config_version", "unknown")),
        timestamp=datetime.now(timezone.utc).isoformat(),
        input_sha256=hashlib.sha256(data).hexdigest(),
        input_bytes=len(data),
        library="pyhanko",
        library_version=_pyhanko_version(),
        signature_count=0,
    )

    if source.suffix.lower() != ".pdf" or not data.startswith(b"%PDF"):
        # Only a PDF can carry a PDF signature. An image attachment is not a
        # failure of this check; it simply has nothing for it to look at.
        result.error = SignatureError.NOT_A_PDF
        result.error_detail = "not a PDF, so it carries no PDF signature"
        result.status = SignatureStatus.ABSENT
        return result

    signatures, used_fallback, fatal = _collect_signatures(data, cfg)
    result.signatures = signatures
    result.audit.signature_count = len(signatures)
    result.audit.used_raw_fallback = used_fallback

    if fatal is not None:
        error, detail = fatal
        result.error = error
        result.error_detail = detail
        result.status = SignatureStatus.UNVERIFIABLE
        return result

    result.status = _file_status(signatures)
    result.findings = _build_findings(
        signatures, cfg, document_type=document_type, issuing_body=issuing_body
    )
    return result


# --------------------------------------------------------------------------
# Locating and reading signatures
# --------------------------------------------------------------------------


def _collect_signatures(
    data: bytes, cfg: dict[str, Any]
) -> tuple[list[SignatureDetail], bool, tuple[SignatureError, str] | None]:
    """Enumerate signatures, falling back to a raw scan when that fails.

    The fallback matters more than it looks. A single malformed CMS blob makes
    pyHanko's enumeration raise, which takes out *every* signature in the file,
    not just the bad one - so without the fallback a file with one deliberately
    corrupted signature would report as unsigned, which is the opposite of what
    is true.
    """
    import io

    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        return [], False, (SignatureError.LIBRARY_UNAVAILABLE, str(exc))

    max_signatures = int(get(cfg, "detection.max_signatures", 20))

    try:
        reader = PdfFileReader(io.BytesIO(data))
    except Exception as exc:
        if _looks_encrypted(data):
            return [], False, (SignatureError.PASSWORD_PROTECTED, str(exc))
        return [], False, (SignatureError.CORRUPT_FILE, f"could not open PDF: {exc}")

    try:
        embedded = list(reader.embedded_signatures)
    except Exception as exc:
        # Enumeration itself failed. Count the signature structures in the raw
        # bytes so the file is reported as "has signatures we cannot read"
        # rather than as unsigned.
        count = _raw_signature_count(data, cfg)
        details = [
            SignatureDetail(
                field_name=None,
                present=True,
                parsed=False,
                parse_error=f"{type(exc).__name__}: {exc}",
            )
            for _ in range(max(count, 1))
        ]
        return details[:max_signatures], True, None

    if not embedded:
        # Belt and braces: a structure the reader did not surface as a
        # signature still counts as present.
        count = _raw_signature_count(data, cfg)
        if count:
            return (
                [
                    SignatureDetail(
                        field_name=None,
                        present=True,
                        parsed=False,
                        parse_error=(
                            "a /ByteRange was present in the file but the PDF "
                            "reader surfaced no signature field for it"
                        ),
                    )
                    for _ in range(min(count, max_signatures))
                ],
                True,
                None,
            )
        return [], False, None

    details = [
        _read_signature(signature, data, reader)
        for signature in embedded[:max_signatures]
    ]
    return details, False, None


def _read_signature(signature: Any, data: bytes, reader: Any) -> SignatureDetail:
    """Everything one signature can tell us, with every failure contained."""
    detail = SignatureDetail(
        field_name=_text_or_none(getattr(signature, "field_name", None)),
        present=True,
        parsed=True,
    )

    try:
        with _quiet_diff_analysis():
            signature.compute_integrity_info()
    except Exception as exc:
        detail.parsed = False
        detail.parse_error = f"{type(exc).__name__}: {exc}"
        return detail

    try:
        byte_range = [int(value) for value in signature.byte_range]
    except Exception as exc:
        detail.parsed = False
        detail.parse_error = f"unreadable /ByteRange: {type(exc).__name__}: {exc}"
        return detail

    detail.byte_range = byte_range
    detail.coverage = _COVERAGE_BY_NAME.get(
        getattr(getattr(signature, "coverage", None), "name", ""),
        CoverageLevel.UNKNOWN,
    )
    detail.covers_whole_file = detail.coverage is CoverageLevel.ENTIRE_FILE

    algorithm = _text_or_none(getattr(signature, "external_md_algorithm", None))
    detail.digest_algorithm = algorithm

    computed = _recompute_digest(data, byte_range, algorithm)
    signed = _signed_message_digest(signature)
    detail.computed_digest_hex = computed.hex() if computed else None
    detail.signed_digest_hex = signed.hex() if signed else None
    if computed is not None and signed is not None:
        detail.digest_matches = computed == signed

    if not detail.covers_whole_file:
        detail.appended = _measure_appended(data, byte_range, reader, signature)

    # Both of these are claims printed in the file. Named `_unverified` because
    # nothing in this module checks them against anything.
    detail.signer_name_unverified = _signer_subject(signature)
    detail.claimed_signing_time_unverified = _text_or_none(
        getattr(signature, "self_reported_timestamp", None)
    )
    return detail


def _recompute_digest(
    data: bytes, byte_range: list[int], algorithm: str | None
) -> bytes | None:
    """Hash the bytes the signature says it covers.

    `/ByteRange` is a flat list of offset/length pairs - normally two pairs,
    with the gap between them occupied by the signature container itself.
    """
    if not algorithm or len(byte_range) < 2 or len(byte_range) % 2:
        return None
    try:
        digest = hashlib.new(algorithm.replace("-", "").lower())
    except ValueError:
        return None

    for offset, length in zip(byte_range[0::2], byte_range[1::2]):
        if offset < 0 or length < 0 or offset + length > len(data):
            # A range reaching past the end of the file cannot be hashed as
            # stated. Reported as "no comparison possible", never as a match.
            return None
        digest.update(data[offset : offset + length])
    return digest.digest()


def _signed_message_digest(signature: Any) -> bytes | None:
    """The `messageDigest` attribute from inside the CMS SignedData.

    This is the value the signer committed to. It is the only digest in the
    file that a modification cannot update without re-signing, which is what
    makes the comparison meaningful.
    """
    try:
        signer_info = signature.signer_info
        attributes = signer_info["signed_attrs"]
    except Exception:
        return None
    if attributes is None:
        return None

    try:
        for attribute in attributes:
            if attribute["type"].native == "message_digest":
                value = attribute["values"][0].native
                return value if isinstance(value, bytes) else None
    except Exception:
        return None
    return None


def _measure_appended(
    data: bytes, byte_range: list[int], reader: Any, signature: Any
) -> AppendedBytes:
    """How much sits after the signed revision - counted, never quoted."""
    covered_end = byte_range[-2] + byte_range[-1] if len(byte_range) >= 2 else 0
    byte_length = max(len(data) - covered_end, 0)

    revisions_after = 0
    try:
        total = int(reader.xrefs.total_revisions)
        signed_revision = int(signature.signed_revision)
        # Revisions are numbered from 0, so the signed revision leaves
        # `total - signed_revision - 1` after it.
        revisions_after = max(total - signed_revision - 1, 0)
    except Exception:
        revisions_after = 0

    # Count indirect object headers in the appended region. A rough measure on
    # purpose: a reviewer wants to know whether one dictionary was added or two
    # hundred objects were, and that is what an order of magnitude answers.
    object_count = len(re.findall(rb"\n\s*\d+\s+\d+\s+obj\b", data[covered_end:]))

    return AppendedBytes(
        byte_length=byte_length,
        object_count=object_count,
        revision_count_after=revisions_after,
    )


def _signer_subject(signature: Any) -> str | None:
    """The signing certificate's subject, as printed. Not verified."""
    try:
        certificate = signature.signer_cert
        if certificate is None:
            return None
        return _text_or_none(certificate.subject.human_friendly)
    except Exception:
        return None


def _raw_signature_count(data: bytes, cfg: dict[str, Any]) -> int:
    marker = str(get(cfg, "detection.raw_byterange_marker", "/ByteRange"))
    return data.count(marker.encode("latin-1"))


def _looks_encrypted(data: bytes) -> bool:
    return b"/Encrypt" in data


def _pyhanko_version() -> str:
    try:
        from importlib.metadata import version

        return version("pyHanko")
    except Exception:
        return "unknown"


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------------
# Status and findings
# --------------------------------------------------------------------------


def _file_status(signatures: list[SignatureDetail]) -> SignatureStatus:
    """The file-level status. Code decides this; no config renames it."""
    if not signatures:
        return SignatureStatus.ABSENT

    # Worst case wins, in this order: a mismatch is the strongest statement we
    # can make, and an unreadable signature outranks an intact one because a
    # file is not "intact" when part of it could not be judged.
    if any(
        detail.parsed and detail.digest_matches is False for detail in signatures
    ):
        return SignatureStatus.MODIFIED_AFTER_SIGNING
    if any(
        detail.parsed and not detail.covers_whole_file for detail in signatures
    ):
        return SignatureStatus.MODIFIED_AFTER_SIGNING
    if any(not detail.parsed or detail.digest_matches is None for detail in signatures):
        return SignatureStatus.UNVERIFIABLE
    return SignatureStatus.PRESENT_INTACT


def _build_findings(
    signatures: list[SignatureDetail],
    cfg: dict[str, Any],
    document_type: str | None,
    issuing_body: str | None,
) -> list[Finding]:
    findings: list[Finding] = []

    if not signatures:
        if _signature_expected(cfg, document_type, issuing_body):
            findings.append(
                _finding(
                    cfg,
                    FindingType.UNSIGNED_WHERE_EXPECTED,
                    reason=(
                        "no digital signature was found, and config records "
                        "this document type or issuing body as one that "
                        "normally signs; requires manual verification"
                    ),
                    evidence={
                        "document_type": document_type,
                        "issuing_body": issuing_body,
                        "signature_count": 0,
                    },
                )
            )
        return findings

    weak_algorithms = {
        str(name).lower()
        for name in (get(cfg, "integrity.weak_digest_algorithms", []) or [])
    }

    for index, detail in enumerate(signatures):
        label = detail.field_name or f"signature #{index + 1}"

        if not detail.parsed:
            findings.append(
                _finding(
                    cfg,
                    FindingType.UNPARSEABLE,
                    reason=(
                        f"the signature on {label} could not be read, so "
                        "whether the document changed after signing could not "
                        "be established; requires manual verification"
                    ),
                    evidence={
                        "field_name": detail.field_name,
                        "parse_error": detail.parse_error,
                    },
                )
            )
            continue

        if detail.digest_matches is False:
            findings.append(
                _finding(
                    cfg,
                    FindingType.DIGEST_MISMATCH,
                    reason=(
                        f"the content covered by the signature on {label} no "
                        "longer matches what was signed"
                    ),
                    evidence=_digest_evidence(cfg, detail),
                )
            )
        elif detail.digest_matches is None:
            findings.append(
                _finding(
                    cfg,
                    FindingType.UNPARSEABLE,
                    reason=(
                        f"the signature on {label} was located but its signed "
                        "digest could not be compared with the file's "
                        "contents; requires manual verification"
                    ),
                    evidence={
                        "field_name": detail.field_name,
                        "byte_range": detail.byte_range,
                        "digest_algorithm": detail.digest_algorithm,
                        "computed_digest_hex": detail.computed_digest_hex,
                        "signed_digest_hex": detail.signed_digest_hex,
                    },
                )
            )

        if detail.coverage in (
            CoverageLevel.CONTIGUOUS_BLOCK_FROM_START,
            CoverageLevel.UNCLEAR,
            CoverageLevel.UNKNOWN,
        ):
            findings.append(
                _finding(
                    cfg,
                    FindingType.DOES_NOT_COVER_DOCUMENT,
                    reason=(
                        f"the signature on {label} does not cover the whole "
                        "document: the byte range it names stops short of a "
                        f"complete revision ({_LEGITIMATE_APPEND_CAVEAT})"
                    ),
                    evidence=_coverage_evidence(detail),
                )
            )
        elif detail.coverage is CoverageLevel.ENTIRE_REVISION:
            findings.append(
                _finding(
                    cfg,
                    FindingType.BYTES_AFTER_SIGNED_REVISION,
                    reason=(
                        f"bytes were added after the revision that the "
                        f"signature on {label} covers "
                        f"({_LEGITIMATE_APPEND_CAVEAT})"
                    ),
                    evidence=_coverage_evidence(detail),
                )
            )

        if (detail.digest_algorithm or "").lower() in weak_algorithms:
            findings.append(
                _finding(
                    cfg,
                    FindingType.WEAK_DIGEST_ALGORITHM,
                    reason=(
                        f"the signature on {label} uses "
                        f"{detail.digest_algorithm}, a digest algorithm no "
                        "longer considered sound; requires manual verification"
                    ),
                    evidence={
                        "field_name": detail.field_name,
                        "digest_algorithm": detail.digest_algorithm,
                        "weak_digest_algorithms": sorted(weak_algorithms),
                    },
                )
            )

    return findings


def _digest_evidence(cfg: dict[str, Any], detail: SignatureDetail) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "field_name": detail.field_name,
        "byte_range": detail.byte_range,
        "digest_algorithm": detail.digest_algorithm,
        "coverage": detail.coverage.value,
    }
    if bool(get(cfg, "output.include_digests", True)):
        length = int(get(cfg, "output.digest_hex_length", 64))
        evidence["computed_digest_hex"] = (detail.computed_digest_hex or "")[:length]
        evidence["signed_digest_hex"] = (detail.signed_digest_hex or "")[:length]
    _add_unverified_signer(cfg, detail, evidence)
    return evidence


def _coverage_evidence(detail: SignatureDetail) -> dict[str, Any]:
    return {
        "field_name": detail.field_name,
        "byte_range": detail.byte_range,
        "coverage": detail.coverage.value,
        # Object count and byte length, never the appended content itself.
        "appended": detail.appended.to_dict() if detail.appended else None,
        # The signature over the earlier revision can be perfectly intact while
        # bytes were added after it, so a reviewer needs both facts together.
        "digest_over_covered_bytes_matches": detail.digest_matches,
    }


def _add_unverified_signer(
    cfg: dict[str, Any], detail: SignatureDetail, evidence: dict[str, Any]
) -> None:
    if bool(get(cfg, "output.include_signer_name", True)):
        evidence["signer_name_unverified"] = detail.signer_name_unverified
    if bool(get(cfg, "output.include_claimed_signing_time", True)):
        evidence["claimed_signing_time_unverified"] = (
            detail.claimed_signing_time_unverified
        )
    evidence["signer_identity_verified"] = False


def _signature_expected(
    cfg: dict[str, Any], document_type: str | None, issuing_body: str | None
) -> bool:
    """Is a signature expected on this document?

    Off unless config says otherwise. Most EoA attachments are scans that were
    never signed, and flagging every one of them would bury the findings that
    matter under a uniform layer of noise.
    """
    if bool(get(cfg, "expectations.signature_expected_by_default", False)):
        return True

    types = {
        _normalise(name)
        for name in (get(cfg, "expectations.signature_expected_document_types", []) or [])
    }
    if document_type and _normalise(document_type) in types:
        return True

    issuers = {
        _normalise(name)
        for name in (get(cfg, "expectations.signature_expected_issuers", []) or [])
    }
    return bool(issuing_body) and _normalise(issuing_body) in issuers


def _normalise(value: str) -> str:
    return " ".join(str(value).lower().split())


def _finding(
    cfg: dict[str, Any],
    finding_type: FindingType,
    reason: str,
    evidence: dict[str, Any],
) -> Finding:
    return Finding(
        type=finding_type,
        strength=strength_for(finding_type, cfg),
        reason=reason,
        evidence=evidence,
    )


def strength_for(finding_type: FindingType, cfg: dict[str, Any]) -> str:
    """The configured label for a finding type.

    Defaults to `weak` rather than `strong` for an unlisted type: a missing
    config entry is our mistake, and it should not be allowed to manufacture a
    strong finding against an institution.
    """
    value = get(cfg, f"strength.{finding_type.value}", None)
    return str(value) if value else "weak"
