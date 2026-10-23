"""Orchestration: decide which id(s) to look up, ask the issuer, compare, map.

Which id to use is a judgement with consequences, so it is explicit here:

- The **step 2 certificate_number** is primary. It is printed on the document
  and has already survived the snippet and exact-value guards, so it is the
  value we have the most reason to trust.
- A **QR certificate id** is a fallback, used when the printed number is null,
  and the lookup is marked lower trust. The QR travels with the document and
  is far easier to replace than the printed text, so a record found only by
  the QR id is weaker evidence than the same record found by the number.
- When **both exist and differ**, both are looked up and both are reported.
  Collapsing that into one answer would discard the discrepancy, which is
  itself the most interesting thing on the page.

A QR id that resolves to a real record belonging to a different institution
gets a strong finding of its own: a valid certificate, just not this
document's.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import get, load_config
from ..core.verification import VerificationStatus
from .client import (
    IssuerClient,
    IssuerUnreachableError,
    MalformedIssuerResponseError,
)
from .compare import compare_record, mismatched_fields
from .models import (
    FieldOutcome,
    Finding,
    FindingType,
    IdOrigin,
    IssuerAudit,
    IssuerVerificationResult,
    LookupOutcome,
    LookupResult,
)
from .normalise import names_match
from .status import map_status, most_severe, ordered_reasons, strength_for


def verify_certificate(
    submitted: dict[str, Any],
    qr_certificate_id: str | None = None,
    config: dict[str, Any] | None = None,
    transport: Any | None = None,
    sleep: Any = None,
    source_path: str | None = None,
    as_of: str | None = None,
    registry_version: str | None = None,
    registry_sha256: str | None = None,
) -> IssuerVerificationResult:
    """Verify one document's certificate against the issuer.

    `submitted` holds the step 2 values: certificate_number, institution_name,
    issuing_body, programmes, grade, valid_from, valid_to.
    """
    cfg = config if config is not None else load_config("issuer_verification")
    reference_date = _resolve_as_of(as_of)

    client = IssuerClient(cfg, transport=transport, sleep=sleep)
    result = IssuerVerificationResult(source_path=source_path, status=VerificationStatus.MANUAL_REVIEW_REQUIRED)
    result.audit = IssuerAudit(
        config_version=str(get(cfg, "config_version", "unknown")),
        issuer_base_url=client.base_url,
        issuer_name=get(cfg, "issuer.name", None),
        registry_version=registry_version,
        registry_sha256=registry_sha256,
        timestamp=datetime.now(timezone.utc).isoformat(),
        as_of=reference_date.isoformat(),
    )

    document_number = _clean(submitted.get("certificate_number"))
    qr_id = _clean(qr_certificate_id)

    plan = _lookup_plan(document_number, qr_id, cfg)

    if not plan:
        reasons = ["no_certificate_id"]
        result.status = map_status(reasons, cfg)
        result.status_reasons = ordered_reasons(reasons)
        result.findings.append(
            Finding(
                type=FindingType.NO_CERTIFICATE_ID,
                strength=strength_for(FindingType.NO_CERTIFICATE_ID, cfg),
                reason=(
                    "No certificate id was available from either the document "
                    "text or a QR code, so no issuer lookup could be made. "
                    "Requires manual verification."
                ),
                evidence={
                    "field_compared": "certificate_number",
                    "document_value": submitted.get("certificate_number"),
                    "qr_value": qr_certificate_id,
                },
            )
        )
        return result

    if document_number and qr_id and document_number != qr_id:
        result.findings.append(
            Finding(
                type=FindingType.QR_ID_DIFFERS,
                strength=strength_for(FindingType.QR_ID_DIFFERS, cfg),
                reason=(
                    f"Mismatch detected between the certificate number printed "
                    f"on the document ('{document_number}') and the id encoded "
                    f"in the QR code ('{qr_id}'). Both were looked up "
                    f"separately."
                ),
                evidence={
                    "field_compared": "certificate_number",
                    "document_value": document_number,
                    "qr_value": qr_id,
                },
            )
        )

    for certificate_id, origin, trust in plan:
        lookup = _run_lookup(
            client=client,
            certificate_id=certificate_id,
            origin=origin,
            trust=trust,
            submitted=submitted,
            cfg=cfg,
            reference_date=reference_date,
            result=result,
        )
        result.lookups.append(lookup)

    result.status = most_severe([lookup.status for lookup in result.lookups], cfg)
    # Union of the lookups' reasons, ordered by precedence rather than by
    # lookup, so the reason that drove the document status reads first. In
    # lookup order a clean primary lookup would put "all_fields_match" ahead
    # of the mismatch that actually decided the status.
    collected = {key for lookup in result.lookups for key in lookup.status_reasons}
    result.status_reasons = ordered_reasons(list(collected))

    for lookup in result.lookups:
        result.findings.extend(lookup.findings)

    return result


def _lookup_plan(
    document_number: str | None, qr_id: str | None, cfg: dict[str, Any]
) -> list[tuple[str, IdOrigin, str]]:
    """Which ids to look up, in order, with the trust label for each."""
    normal = str(get(cfg, "lookup.trust_document_number", "normal"))
    lower = str(get(cfg, "lookup.trust_qr_fallback", "lower"))

    if document_number and qr_id and document_number != qr_id:
        return [
            (document_number, IdOrigin.DOCUMENT_NUMBER, normal),
            (qr_id, IdOrigin.QR_CODE, lower),
        ]
    if document_number:
        return [(document_number, IdOrigin.DOCUMENT_NUMBER, normal)]
    if qr_id:
        return [(qr_id, IdOrigin.QR_CODE, lower)]
    return []


def _run_lookup(
    client: IssuerClient,
    certificate_id: str,
    origin: IdOrigin,
    trust: str,
    submitted: dict[str, Any],
    cfg: dict[str, Any],
    reference_date: date,
    result: IssuerVerificationResult,
) -> LookupResult:
    lookup = LookupResult(
        certificate_id=certificate_id,
        id_origin=origin,
        trust=trust,
        outcome=LookupOutcome.UNAVAILABLE,
        status=VerificationStatus.SOURCE_UNAVAILABLE,
    )

    if origin is IdOrigin.QR_CODE:
        lookup.findings.append(
            Finding(
                type=FindingType.LOWER_TRUST_LOOKUP,
                strength=strength_for(FindingType.LOWER_TRUST_LOOKUP, cfg),
                reason=(
                    "This lookup used the certificate id from the QR code "
                    "rather than the number printed in the document text, so "
                    "it rests on a weaker source."
                ),
                evidence={
                    "field_compared": "certificate_number",
                    "id_used": certificate_id,
                    "id_origin": origin.value,
                    "trust": trust,
                },
            )
        )

    try:
        response = client.lookup(certificate_id)
    except IssuerUnreachableError as exc:
        lookup.outcome = LookupOutcome.UNAVAILABLE
        lookup.error = str(exc)
        reasons = ["source_unavailable"]
        lookup.status = map_status(reasons, cfg)
        lookup.status_reasons = ordered_reasons(reasons)
        lookup.findings.append(
            Finding(
                type=FindingType.SOURCE_UNAVAILABLE,
                strength=strength_for(FindingType.SOURCE_UNAVAILABLE, cfg),
                reason=(
                    f"The issuer verification service could not be reached, so "
                    f"this certificate could not be checked against it: {exc}"
                ),
                evidence={
                    "field_compared": "issuer_lookup",
                    "certificate_id": certificate_id,
                    "issuer_base_url": client.base_url,
                },
            )
        )
        _record_request(result, lookup, exc, cfg, error=str(exc))
        return lookup
    except MalformedIssuerResponseError as exc:
        lookup.outcome = LookupOutcome.MALFORMED
        lookup.error = str(exc)
        reasons = ["malformed_response"]
        lookup.status = map_status(reasons, cfg)
        lookup.status_reasons = ordered_reasons(reasons)
        lookup.findings.append(
            Finding(
                type=FindingType.MALFORMED_RESPONSE,
                strength=strength_for(FindingType.MALFORMED_RESPONSE, cfg),
                reason=(
                    "The issuer service answered with a body that could not be "
                    "read, so no determination can be drawn from it. Requires "
                    "manual verification."
                ),
                evidence={
                    "field_compared": "issuer_lookup",
                    "certificate_id": certificate_id,
                    "detail": str(exc),
                },
            )
        )
        _record_request(result, lookup, exc, cfg, error=str(exc))
        return lookup

    lookup.http_status = response.http_status
    _record_request(result, lookup, response, cfg)

    body = response.body if isinstance(response.body, dict) else {}

    if response.http_status == 404:
        lookup.outcome = LookupOutcome.NOT_FOUND
        reasons = ["not_found"]
        lookup.status = map_status(reasons, cfg)
        lookup.status_reasons = ordered_reasons(reasons)
        lookup.findings.append(
            Finding(
                type=FindingType.RECORD_NOT_FOUND,
                strength=strength_for(FindingType.RECORD_NOT_FOUND, cfg),
                reason=(
                    f"The issuer has no record for certificate id "
                    f"'{certificate_id}'. This can also mean the issuer's "
                    f"records are incomplete, so it requires manual "
                    f"verification rather than any conclusion about the "
                    f"document."
                ),
                evidence={
                    "field_compared": "certificate_number",
                    "certificate_id": certificate_id,
                    "id_origin": origin.value,
                    "issuer_response": body.get("detail"),
                    "http_status": response.http_status,
                },
            )
        )
        return lookup

    if response.http_status == 202 or body.get("lookup_status") == "pending":
        lookup.outcome = LookupOutcome.PENDING
        reasons = ["issuer_pending"]
        lookup.status = map_status(reasons, cfg)
        lookup.status_reasons = ordered_reasons(reasons)
        return lookup

    record = body.get("record")
    if not isinstance(record, dict):
        lookup.outcome = LookupOutcome.MALFORMED
        reasons = ["malformed_response"]
        lookup.status = map_status(reasons, cfg)
        lookup.status_reasons = ordered_reasons(reasons)
        lookup.findings.append(
            Finding(
                type=FindingType.MALFORMED_RESPONSE,
                strength=strength_for(FindingType.MALFORMED_RESPONSE, cfg),
                reason=(
                    "The issuer answered successfully but the response carried "
                    "no certificate record. Requires manual verification."
                ),
                evidence={
                    "field_compared": "issuer_lookup",
                    "certificate_id": certificate_id,
                    "http_status": response.http_status,
                },
            )
        )
        return lookup

    lookup.outcome = LookupOutcome.FOUND
    lookup.record = record
    lookup.comparisons = compare_record(submitted, record, cfg)

    reasons: list[str] = []
    issuer_status = str(record.get("issuer_status", "")).strip().casefold()

    if issuer_status in ("revoked", "suspended"):
        reasons.append(f"issuer_status_{issuer_status}")
        lookup.findings.append(
            Finding(
                type=FindingType.STATUS_NOT_ACTIVE,
                strength=strength_for(FindingType.STATUS_NOT_ACTIVE, cfg),
                reason=(
                    f"The issuer's own record for this certificate is marked "
                    f"'{issuer_status}', not active."
                ),
                evidence={
                    "field_compared": "issuer_status",
                    "certificate_id": certificate_id,
                    "issuer_value": record.get("issuer_status"),
                    "record_updated": record.get("record_updated"),
                },
            )
        )

    mismatches = mismatched_fields(lookup.comparisons)
    if mismatches:
        reasons.append("field_mismatch")
        for comparison in mismatches:
            lookup.findings.append(
                Finding(
                    type=FindingType.FIELD_MISMATCH,
                    strength=strength_for(FindingType.FIELD_MISMATCH, cfg),
                    reason=(
                        f"Mismatch detected between the document's "
                        f"{comparison.name.replace('_', ' ')} and the issuer's "
                        f"record: {comparison.reason}."
                    ),
                    evidence={
                        "field_compared": comparison.name,
                        "document_value": comparison.submitted_value,
                        "issuer_value": comparison.issuer_value,
                        "similarity": comparison.similarity,
                        "method": comparison.method,
                        "certificate_id": certificate_id,
                    },
                )
            )

    expired = _is_expired(record.get("valid_to"), reference_date)
    if expired and not mismatches:
        reasons.append("expired_per_issuer")
        lookup.findings.append(
            Finding(
                type=FindingType.EXPIRED,
                strength=strength_for(FindingType.EXPIRED, cfg),
                reason=(
                    f"The issuer's record shows this accreditation expired on "
                    f"{record.get('valid_to')}, before the reference date "
                    f"{reference_date.isoformat()}."
                ),
                evidence={
                    "field_compared": "valid_to",
                    "issuer_value": record.get("valid_to"),
                    "as_of": reference_date.isoformat(),
                    "certificate_id": certificate_id,
                },
            )
        )

    if origin is IdOrigin.QR_CODE:
        _check_other_institution(lookup, submitted, record, certificate_id, cfg)

    if not reasons:
        reasons.append("all_fields_match")

    lookup.status = map_status(reasons, cfg)
    lookup.status_reasons = ordered_reasons(reasons)
    return lookup


def _check_other_institution(
    lookup: LookupResult,
    submitted: dict[str, Any],
    record: dict[str, Any],
    certificate_id: str,
    cfg: dict[str, Any],
) -> None:
    """A QR id resolving to someone else's valid record.

    Reported separately from an ordinary field mismatch because the shape of
    the problem is different: the record is real and internally consistent,
    it just is not this document's.
    """
    submitted_name = submitted.get("institution_name")
    issuer_name = record.get("institution_name")
    if not submitted_name or not issuer_name:
        return

    matched, ratio, _ = names_match(str(submitted_name), str(issuer_name), cfg)
    if matched:
        return

    lookup.findings.append(
        Finding(
            type=FindingType.QR_ID_OTHER_INSTITUTION,
            strength=strength_for(FindingType.QR_ID_OTHER_INSTITUTION, cfg),
            reason=(
                f"The certificate id in the QR code resolves to an issuer "
                f"record held by a different institution "
                f"('{issuer_name}') than the document claims "
                f"('{submitted_name}')."
            ),
            evidence={
                "field_compared": "institution_name",
                "certificate_id": certificate_id,
                "id_origin": IdOrigin.QR_CODE.value,
                "document_value": submitted_name,
                "issuer_value": issuer_name,
                "similarity": round(ratio, 4),
            },
        )
    )


def _record_request(
    result: IssuerVerificationResult,
    lookup: LookupResult,
    response: Any,
    cfg: dict[str, Any],
    error: str | None = None,
) -> None:
    if result.audit is None:
        return

    entry: dict[str, Any] = {
        "certificate_id": lookup.certificate_id,
        "id_origin": lookup.id_origin.value,
        "url": getattr(response, "url", None),
        "http_status": getattr(response, "http_status", None),
        "latency_ms": round(getattr(response, "latency_ms", 0.0), 2),
        "attempts": [
            attempt.to_dict() for attempt in getattr(response, "attempts", [])
        ],
        "error": error,
    }

    if bool(get(cfg, "output.include_raw_response", True)):
        limit = int(get(cfg, "output.max_raw_response_chars", 4000))
        raw = getattr(response, "raw_text", None)
        entry["raw_response"] = raw[:limit] if isinstance(raw, str) else None

    result.audit.requests.append(entry)


def _is_expired(valid_to: Any, reference: date) -> bool:
    text = str(valid_to or "").strip()
    if not text:
        return False
    try:
        return date.fromisoformat(text[:10]) < reference
    except ValueError:
        return False


def _resolve_as_of(as_of: str | None) -> date:
    """The reference date for expiry. Reuses step 2's consistency setting."""
    if as_of:
        try:
            return date.fromisoformat(str(as_of)[:10])
        except ValueError:
            pass
    try:
        certificate_cfg = load_config("certificate_extraction")
        configured = get(certificate_cfg, "consistency.as_of", None)
    except Exception:
        configured = None
    if configured:
        try:
            return date.fromisoformat(str(configured)[:10])
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def submitted_from_extraction(extraction: dict[str, Any]) -> dict[str, Any]:
    """Pull the step 2 values worth comparing out of a step 2 result.

    Only `validated: true` values are taken. An unvalidated value was already
    rejected by step 2 as untraceable to the source, so comparing it against
    the issuer would manufacture a mismatch out of our own uncertainty.
    """
    fields = extraction.get("fields") or {}

    def value_of(name: str) -> Any:
        entry = fields.get(name)
        if not isinstance(entry, dict):
            return None
        if entry.get("validated") is not True:
            return None
        return entry.get("value")

    return {
        "certificate_number": value_of("certificate_number"),
        "institution_name": value_of("institution_name"),
        "issuing_body": value_of("issuing_body"),
        "programmes": value_of("programme"),
        "grade": value_of("accreditation_status_or_grade"),
        # Prefer the ISO form step 2 parsed, falling back to the raw value.
        "valid_from": _iso_or_raw(fields, "valid_from"),
        "valid_to": _iso_or_raw(fields, "valid_to"),
    }


def _iso_or_raw(fields: dict[str, Any], name: str) -> Any:
    entry = fields.get(name)
    if not isinstance(entry, dict) or entry.get("validated") is not True:
        return None
    return entry.get("value_iso") or entry.get("value")


def qr_id_from_scan(scan: dict[str, Any]) -> str | None:
    """The certificate id from a step 3 result, if exactly one is present.

    Two distinct QR payloads is already a step 3 finding. Picking one of them
    here would hide that, so nothing is chosen.
    """
    detections = scan.get("detections") or []
    ids = []
    for detection in detections:
        value = detection.get("certificate_id") if isinstance(detection, dict) else None
        if value and value not in ids:
            ids.append(value)
    if len(ids) == 1:
        return ids[0]
    return None
