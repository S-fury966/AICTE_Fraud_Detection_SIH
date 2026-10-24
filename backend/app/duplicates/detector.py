"""Does this document already exist somewhere else in the corpus?

Four comparisons, cheapest and most certain first: the file hash, the validated
certificate number, the QR payload, and - least certain, and treated as such -
the perceptual hash of the rendered first page.

Three rules shape the module.

**A document is never a duplicate of itself.** Re-running the same document
must not flag it against its own earlier runs. Matches are therefore found by
*document id*, never by run.

**Order independence.** The set of relationships found must not depend on which
application happened to be processed first. Relationships are stored in
`cross_references` as an unordered pair - lower document id first - with a
unique constraint, and read from both ends. So when B arrives and matches A,
A's report shows the relationship without anything in A's finished run being
rewritten. That is the only way to have both order independence and the rule
that runs are never overwritten.

**Context decides severity, not the check.** An institution resubmitting its
own file is routine. The same file arriving from somewhere else is not. The
same observation, two very different meanings, so `strength` is looked up by
check *and* context.

No LLM anywhere in this module, and nothing here reaches the network - the
reverse image search is internal, against our own stored hashes only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get, load_config
from ..db.models import (
    Application,
    CheckRun,
    CrossReference,
    Document,
    ExtractedField,
    Institution,
    PerceptualHashRow,
    QRDetectionRow,
)
from ..similarity import hashing
from .models import (
    ComparisonSkipped,
    DuplicateAudit,
    DuplicateCheckResult,
    DuplicateStatus,
    Finding,
    FindingType,
    Match,
)


@dataclass
class DocumentContext:
    """Everything about the document under test that the checks need."""

    document_id: int
    application_id: int
    institution_id: int
    institution_name: str
    file_sha256: str
    stored_path: str
    # Only a `validated: true` value from step 2 is ever used. A rejected or
    # absent value is `None`, and `None` never matches anything.
    certificate_number: str | None = None
    # Certificate ids read out of QR payloads, and the raw payloads themselves.
    qr_certificate_ids: list[str] = None  # type: ignore[assignment]
    qr_raw_payloads: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.qr_certificate_ids is None:
            self.qr_certificate_ids = []
        if self.qr_raw_payloads is None:
            self.qr_raw_payloads = []


def detect_duplicates(
    session: Session,
    document_id: int,
    run_id: int,
    config: dict[str, Any] | None = None,
    certificate_number: str | None = None,
    qr_certificate_ids: list[str] | None = None,
    qr_raw_payloads: list[str] | None = None,
) -> DuplicateCheckResult:
    """Compare one document against every other document already stored.

    The earlier stages' values are passed in rather than re-read from the
    database, so this works identically whether it is driven by the pipeline or
    by the CLI against a document stored earlier.
    """
    cfg = config if config is not None else load_config("duplicate_checks")

    context = _load_context(
        session,
        document_id,
        certificate_number=certificate_number,
        qr_certificate_ids=qr_certificate_ids,
        qr_raw_payloads=qr_raw_payloads,
    )
    result = DuplicateCheckResult(document_id=document_id, status=DuplicateStatus.OK)

    others = _other_documents(session, document_id)
    compare_kind = str(get(cfg, "perceptual_hash.compare_kind", "dhash"))
    hash_size = int(get(cfg, "perceptual_hash.hash_size", 16))
    max_distance = int(get(cfg, "perceptual_hash.near_duplicate_max_distance", 12))

    result.audit = DuplicateAudit(
        config_version=str(get(cfg, "config_version", "unknown")),
        timestamp=datetime.now(timezone.utc).isoformat(),
        documents_compared=len(others),
        hashes_compared=0,
        compare_kind=compare_kind,
        hash_size=hash_size,
        near_duplicate_max_distance=max_distance,
    )

    matches: list[Match] = []

    if bool(get(cfg, "checks.exact_file_hash", True)):
        matches.extend(_match_exact_file(context, others))
    else:
        result.comparisons_skipped.append(
            ComparisonSkipped("exact_file_hash", "disabled in config")
        )

    if bool(get(cfg, "checks.certificate_number_reuse", True)):
        matches.extend(_match_certificate_number(context, others, session, result))
    else:
        result.comparisons_skipped.append(
            ComparisonSkipped("certificate_number_reuse", "disabled in config")
        )

    if bool(get(cfg, "checks.qr_payload_reuse", True)):
        matches.extend(_match_qr_payload(context, others, session, result))
    else:
        result.comparisons_skipped.append(
            ComparisonSkipped("qr_payload_reuse", "disabled in config")
        )

    # Hashes are computed and stored even when the image comparison is off, so
    # the corpus stays complete and a later run can compare against this
    # document without re-rendering it.
    stored = _store_hashes(session, context, run_id, cfg, result)

    if bool(get(cfg, "checks.near_duplicate_image", True)):
        if stored is None:
            result.comparisons_skipped.append(
                ComparisonSkipped(
                    "near_duplicate_image",
                    "the first page could not be rendered, so no perceptual "
                    "hash could be computed for this document",
                )
            )
            result.status = DuplicateStatus.IMAGE_UNAVAILABLE
        else:
            matches.extend(
                _match_near_duplicate(
                    context, others, session, cfg, stored, result
                )
            )
    else:
        result.comparisons_skipped.append(
            ComparisonSkipped("near_duplicate_image", "disabled in config")
        )

    cap = int(get(cfg, "output.max_matches_per_check", 25))
    result.matches = _apply_cap(matches, cap, result)
    result.findings = _build_findings(context, result.matches, cfg)

    if not others and result.status is DuplicateStatus.OK:
        # Not a failure: the first document into an empty corpus has nothing to
        # be a duplicate of. Recorded so an empty result is readable.
        result.status = DuplicateStatus.NO_CORPUS

    _record_cross_references(session, context, result.matches, run_id, cfg)
    return result


# --------------------------------------------------------------------------
# Gathering what to compare
# --------------------------------------------------------------------------


def _load_context(
    session: Session,
    document_id: int,
    certificate_number: str | None,
    qr_certificate_ids: list[str] | None,
    qr_raw_payloads: list[str] | None,
) -> DocumentContext:
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError(f"no document with id {document_id}")

    application = session.get(Application, document.application_id)
    if application is None:
        raise ValueError(f"document {document_id} has no application")
    institution = session.get(Institution, application.institution_id)

    return DocumentContext(
        document_id=document.id,
        application_id=application.id,
        institution_id=application.institution_id,
        institution_name=institution.name if institution else "",
        file_sha256=document.file_sha256,
        stored_path=document.stored_path,
        certificate_number=_clean(certificate_number),
        qr_certificate_ids=[
            value for value in (_clean(v) for v in (qr_certificate_ids or [])) if value
        ],
        qr_raw_payloads=[
            value for value in (_clean(v) for v in (qr_raw_payloads or [])) if value
        ],
    )


@dataclass
class _Other:
    document_id: int
    application_id: int
    institution_id: int
    institution_name: str
    file_sha256: str


def _other_documents(session: Session, document_id: int) -> list[_Other]:
    """Every document in the corpus except this one.

    The exclusion is by document id, which is what makes a re-run safe: a
    second run over the same document finds the same corpus and still cannot
    see itself in it.
    """
    rows = session.execute(
        select(Document, Application, Institution)
        .join(Application, Document.application_id == Application.id)
        .join(Institution, Application.institution_id == Institution.id)
        .where(Document.id != document_id)
        .order_by(Document.id)
    ).all()
    return [
        _Other(
            document_id=document.id,
            application_id=application.id,
            institution_id=institution.id,
            institution_name=institution.name,
            file_sha256=document.file_sha256,
        )
        for document, application, institution in rows
    ]


# --------------------------------------------------------------------------
# The four checks
# --------------------------------------------------------------------------


def _match_exact_file(
    context: DocumentContext, others: list[_Other]
) -> list[Match]:
    matches: list[Match] = []
    for other in others:
        if other.file_sha256 != context.file_sha256:
            continue
        matches.append(
            _match(
                FindingType.EXACT_FILE,
                context,
                other,
                evidence={
                    "compared": "file_sha256",
                    "file_sha256": context.file_sha256,
                },
            )
        )
    return matches


def _match_certificate_number(
    context: DocumentContext,
    others: list[_Other],
    session: Session,
    result: DuplicateCheckResult,
) -> list[Match]:
    if not context.certificate_number:
        result.comparisons_skipped.append(
            ComparisonSkipped(
                "certificate_number_reuse",
                "this document has no validated certificate number from field "
                "extraction, so there was no number to compare",
            )
        )
        return []

    target = _normalise_number(context.certificate_number)
    by_id = {other.document_id: other for other in others}

    rows = session.execute(
        select(ExtractedField.value, Document.id)
        .join(CheckRun, ExtractedField.run_id == CheckRun.id)
        .join(Document, Document.id == CheckRun.document_id)
        .where(
            ExtractedField.name == "certificate_number",
            ExtractedField.validated.is_(True),
            Document.id != context.document_id,
        )
    ).all()

    matches: list[Match] = []
    seen: set[int] = set()
    for value, other_document_id in rows:
        other = by_id.get(other_document_id)
        if other is None or other_document_id in seen:
            continue
        if value is None or _normalise_number(str(value)) != target:
            continue
        seen.add(other_document_id)
        matches.append(
            _match(
                FindingType.CERTIFICATE_NUMBER,
                context,
                other,
                evidence={
                    "compared": "certificate_number",
                    "certificate_number": context.certificate_number,
                    "other_certificate_number": str(value),
                },
            )
        )
    return matches


def _match_qr_payload(
    context: DocumentContext,
    others: list[_Other],
    session: Session,
    result: DuplicateCheckResult,
) -> list[Match]:
    if not context.qr_certificate_ids and not context.qr_raw_payloads:
        result.comparisons_skipped.append(
            ComparisonSkipped(
                "qr_payload_reuse",
                "no QR code was decoded from this document, so there was no "
                "payload to compare",
            )
        )
        return []

    target_ids = {_normalise_number(value) for value in context.qr_certificate_ids}
    target_payloads = {value for value in context.qr_raw_payloads}
    by_id = {other.document_id: other for other in others}

    rows = session.execute(
        select(
            QRDetectionRow.certificate_id, QRDetectionRow.raw_text, Document.id
        )
        .join(CheckRun, QRDetectionRow.run_id == CheckRun.id)
        .join(Document, Document.id == CheckRun.document_id)
        .where(Document.id != context.document_id)
    ).all()

    matches: list[Match] = []
    seen: set[int] = set()
    for certificate_id, raw_text, other_document_id in rows:
        other = by_id.get(other_document_id)
        if other is None or other_document_id in seen:
            continue

        matched_on = None
        if certificate_id and _normalise_number(certificate_id) in target_ids:
            matched_on = "certificate_id"
        elif raw_text and raw_text in target_payloads:
            matched_on = "raw_payload"
        if matched_on is None:
            continue

        seen.add(other_document_id)
        matches.append(
            _match(
                FindingType.QR_PAYLOAD,
                context,
                other,
                evidence={
                    "compared": f"qr_{matched_on}",
                    "qr_certificate_ids": context.qr_certificate_ids,
                    "matched_certificate_id": certificate_id,
                    # The payload is a string from the submitted document, not
                    # document body text, so recording it is within the audit
                    # rules - and it is the whole evidence for this finding.
                    "matched_raw_payload": raw_text if matched_on == "raw_payload" else None,
                },
            )
        )
    return matches


def _match_near_duplicate(
    context: DocumentContext,
    others: list[_Other],
    session: Session,
    cfg: dict[str, Any],
    stored: list[hashing.PerceptualHash],
    result: DuplicateCheckResult,
) -> list[Match]:
    compare_kind = str(get(cfg, "perceptual_hash.compare_kind", "dhash"))
    hash_size = int(get(cfg, "perceptual_hash.hash_size", 16))
    max_distance = int(get(cfg, "perceptual_hash.near_duplicate_max_distance", 12))
    page = int(get(cfg, "perceptual_hash.page", 1))

    mine = next((h for h in stored if h.kind == compare_kind), None)
    if mine is None:
        result.comparisons_skipped.append(
            ComparisonSkipped(
                "near_duplicate_image",
                f"config compares on {compare_kind!r} but that kind was not "
                "among the hashes computed for this document",
            )
        )
        return []

    by_id = {other.document_id: other for other in others}
    rows = session.execute(
        select(
            PerceptualHashRow.document_id,
            PerceptualHashRow.hash_value,
            PerceptualHashRow.hash_size,
        )
        .join(CheckRun, PerceptualHashRow.run_id == CheckRun.id)
        .where(
            PerceptualHashRow.hash_kind == compare_kind,
            PerceptualHashRow.page == page,
            PerceptualHashRow.document_id != context.document_id,
        )
        .order_by(PerceptualHashRow.document_id, PerceptualHashRow.id)
    ).all()

    # Our own certificate number, for the context that decides strength.
    matches: list[Match] = []
    seen: set[int] = set()
    compared = 0

    for other_document_id, hash_value, other_size in rows:
        other = by_id.get(other_document_id)
        if other is None or other_document_id in seen:
            continue
        if other_size != hash_size:
            # A hash computed under a different `hash_size` cannot be compared;
            # padding it would produce a small, meaningless distance, and a
            # small distance is exactly what this check reads as a duplicate.
            result.comparisons_skipped.append(
                ComparisonSkipped(
                    "near_duplicate_image",
                    f"document {other_document_id} has a {other_size * other_size}-bit "
                    f"{compare_kind} stored but config now compares at "
                    f"{hash_size * hash_size} bits; it was not compared",
                )
            )
            continue

        compared += 1
        distance = hashing.hamming_distance(mine.hex_value, hash_value)
        if distance > max_distance:
            continue

        seen.add(other_document_id)
        other_number = _certificate_number_for(session, other_document_id)
        matches.append(
            _match(
                FindingType.NEAR_DUPLICATE_IMAGE,
                context,
                other,
                evidence={
                    "compared": f"{compare_kind}_page_{page}",
                    "hamming_distance": distance,
                    "threshold": max_distance,
                    "hash_bits": mine.bit_count,
                    "hash_kind": compare_kind,
                    "page": page,
                    # Both numbers, always, because the distance alone cannot
                    # tell a re-saved copy from two certificates sharing a
                    # template - the numbers can.
                    "certificate_number": context.certificate_number,
                    "other_certificate_number": other_number,
                    "certificate_numbers_differ": _numbers_differ(
                        context.certificate_number, other_number
                    ),
                },
            )
        )

    if result.audit is not None:
        result.audit.hashes_compared = compared
    return matches


# --------------------------------------------------------------------------
# Hash storage
# --------------------------------------------------------------------------


def _store_hashes(
    session: Session,
    context: DocumentContext,
    run_id: int,
    cfg: dict[str, Any],
    result: DuplicateCheckResult,
) -> list[hashing.PerceptualHash] | None:
    """Render page 1 and store every configured hash of it.

    Returns None when the page could not be rendered - which is a skipped
    comparison, not a failed run.
    """
    from ..qr.images import ImageLoadError, render_page_image

    page = int(get(cfg, "perceptual_hash.page", 1))
    dpi = int(get(cfg, "perceptual_hash.render_dpi", 150))
    hash_size = int(get(cfg, "perceptual_hash.hash_size", 16))
    kinds = [str(kind) for kind in (get(cfg, "perceptual_hash.kinds", ["dhash"]) or [])]

    try:
        image = render_page_image(Path(context.stored_path), page, dpi)
    except (ImageLoadError, OSError) as exc:
        result.comparisons_skipped.append(
            ComparisonSkipped(
                "perceptual_hash",
                f"page {page} could not be rendered: {exc}",
            )
        )
        return None
    except Exception as exc:
        result.comparisons_skipped.append(
            ComparisonSkipped(
                "perceptual_hash",
                f"page {page} could not be rendered: {type(exc).__name__}: {exc}",
            )
        )
        return None

    try:
        computed = hashing.compute_all(image, kinds, hash_size=hash_size)
    except hashing.UnsupportedHashKind as exc:
        result.comparisons_skipped.append(
            ComparisonSkipped("perceptual_hash", str(exc))
        )
        return None

    for entry in computed:
        session.add(
            PerceptualHashRow(
                document_id=context.document_id,
                run_id=run_id,
                page=page,
                hash_kind=entry.kind,
                hash_value=entry.hex_value,
                hash_size=entry.hash_size,
            )
        )
    session.flush()
    result.hashes_stored = [entry.to_dict() for entry in computed]
    return computed


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


def _record_cross_references(
    session: Session,
    context: DocumentContext,
    matches: list[Match],
    run_id: int,
    cfg: dict[str, Any],
) -> None:
    """Store each relationship once, as an unordered pair.

    The unique constraint plus the normalised order is what makes the final set
    independent of processing order: whichever document arrives second
    discovers the pair, and it is written the same way either way.
    """
    normalise = bool(get(cfg, "context.normalise_pair_order", True))

    for match in matches:
        low, high = context.document_id, match.other_document_id
        if normalise and low > high:
            low, high = high, low

        existing = session.scalars(
            select(CrossReference).where(
                CrossReference.document_a_id == low,
                CrossReference.document_b_id == high,
                CrossReference.kind == match.kind.value,
            )
        ).first()
        if existing is not None:
            # Already known. Left exactly as it was, including which run
            # discovered it - rewriting that would lose when it was first seen.
            continue

        session.add(
            CrossReference(
                document_a_id=low,
                document_b_id=high,
                kind=match.kind.value,
                same_institution=match.same_institution,
                evidence=match.evidence,
                discovered_in_run_id=run_id,
            )
        )
    session.flush()


def cross_references_for_document(
    session: Session, document_id: int
) -> list[dict[str, Any]]:
    """Every relationship involving this document, from either end.

    Read from both sides on purpose. A relationship discovered while processing
    B belongs just as much to A, and A's run finished before B existed.
    """
    rows = session.scalars(
        select(CrossReference)
        .where(
            (CrossReference.document_a_id == document_id)
            | (CrossReference.document_b_id == document_id)
        )
        .order_by(CrossReference.id)
    ).all()

    entries: list[dict[str, Any]] = []
    for row in rows:
        other = (
            row.document_b_id
            if row.document_a_id == document_id
            else row.document_a_id
        )
        entries.append(
            {
                "id": row.id,
                "kind": row.kind,
                "other_document_id": other,
                "same_institution": row.same_institution,
                "evidence": row.evidence,
                "discovered_in_run_id": row.discovered_in_run_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return entries


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


def _build_findings(
    context: DocumentContext, matches: list[Match], cfg: dict[str, Any]
) -> list[Finding]:
    findings: list[Finding] = []
    for match in matches:
        context_key = _context_key(match, context)
        strength = strength_for(match.kind, context_key, cfg)
        findings.append(
            Finding(
                type=match.kind,
                strength=strength,
                reason=_reason(match, context, context_key),
                evidence={
                    **match.evidence,
                    "this_document_id": context.document_id,
                    "this_institution": context.institution_name,
                    "other_document_id": match.other_document_id,
                    "other_application_id": match.other_application_id,
                    "other_institution": match.other_institution_name,
                    "same_institution": match.same_institution,
                    "strength_context": context_key,
                },
            )
        )
    return findings


def _context_key(match: Match, context: DocumentContext) -> str:
    """Which config context decides this match's strength."""
    if match.kind is not FindingType.NEAR_DUPLICATE_IMAGE:
        return "same_institution" if match.same_institution else "other_institution"

    mine = context.certificate_number
    theirs = match.evidence.get("other_certificate_number")
    if not mine or not theirs:
        return "number_unknown"
    if _numbers_differ(mine, theirs):
        # The numbers are different, so this is most likely two certificates
        # from one issuer sharing a template.
        return "different_number"
    return (
        "same_number_same_institution"
        if match.same_institution
        else "same_number_other_institution"
    )


def _reason(match: Match, context: DocumentContext, context_key: str) -> str:
    """One plain sentence. Says what matched, never who did what."""
    other = match.other_institution_name or f"application {match.other_application_id}"

    if match.kind is FindingType.EXACT_FILE:
        if match.same_institution:
            return (
                "this file has already been submitted by the same institution "
                f"({context.institution_name}) as document "
                f"{match.other_document_id}; a resubmission is normal, shown "
                "for information"
            )
        return (
            "the identical file was also submitted by a different institution "
            f"({other}, document {match.other_document_id}); requires manual "
            "verification"
        )

    if match.kind is FindingType.CERTIFICATE_NUMBER:
        if match.same_institution:
            return (
                f"certificate number {context.certificate_number} also appears "
                f"on document {match.other_document_id} from the same "
                "institution; shown for information"
            )
        return (
            f"certificate number {context.certificate_number} also appears on "
            f"a document submitted by a different institution ({other}); "
            "mismatch detected between the two institutions holding the same "
            "number, requires manual verification"
        )

    if match.kind is FindingType.QR_PAYLOAD:
        if match.same_institution:
            return (
                "the same QR payload also appears on document "
                f"{match.other_document_id} from the same institution; shown "
                "for information"
            )
        return (
            "the same QR payload also appears on a document submitted by a "
            f"different institution ({other}); requires manual verification"
        )

    # Near-duplicate image. The wording carries the caveat, because the
    # measurement genuinely cannot distinguish the two explanations.
    distance = match.evidence.get("hamming_distance")
    bits = match.evidence.get("hash_bits")
    shared = (
        f"page 1 of this document is visually similar to page 1 of document "
        f"{match.other_document_id} ({other}): Hamming distance {distance} of "
        f"{bits} bits"
    )
    if context_key == "different_number":
        return (
            f"{shared}. The certificate numbers differ "
            f"({context.certificate_number} vs "
            f"{match.evidence.get('other_certificate_number')}), so this is "
            "most likely two certificates sharing one issuer's template; "
            "requires manual verification"
        )
    if context_key == "number_unknown":
        return (
            f"{shared}. No validated certificate number was available on both "
            "sides, so the numbers could not be compared; requires manual "
            "verification"
        )
    return (
        f"{shared}, and both carry certificate number "
        f"{context.certificate_number}; requires manual verification"
    )


def strength_for(
    finding_type: FindingType, context_key: str, cfg: dict[str, Any]
) -> str:
    """The configured label for a finding type in a given context.

    Defaults to `weak` for an unlisted combination: a missing config entry is
    our mistake, and it must not be allowed to manufacture a strong finding
    against an institution.
    """
    value = get(cfg, f"strength.{finding_type.value}.{context_key}", None)
    return str(value) if value else "weak"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _match(
    kind: FindingType,
    context: DocumentContext,
    other: _Other,
    evidence: dict[str, Any],
) -> Match:
    return Match(
        kind=kind,
        other_document_id=other.document_id,
        other_application_id=other.application_id,
        other_institution_id=other.institution_id,
        other_institution_name=other.institution_name,
        same_institution=other.institution_id == context.institution_id,
        evidence=evidence,
    )


def _apply_cap(
    matches: list[Match], cap: int, result: DuplicateCheckResult
) -> list[Match]:
    """Keep at most `cap` matches per check, and record when that bites."""
    kept: list[Match] = []
    counts: dict[FindingType, int] = {}
    for match in matches:
        count = counts.get(match.kind, 0)
        if count >= cap:
            note = (
                f"max_matches_per_check ({cap}) reached for {match.kind.value}; "
                "further matches were not reported"
            )
            if result.audit is not None and note not in result.audit.caps_reached:
                result.audit.caps_reached.append(note)
            continue
        counts[match.kind] = count + 1
        kept.append(match)
    return kept


def _certificate_number_for(session: Session, document_id: int) -> str | None:
    """The most recent validated certificate number for a document."""
    row = session.execute(
        select(ExtractedField.value)
        .join(CheckRun, ExtractedField.run_id == CheckRun.id)
        .where(
            CheckRun.document_id == document_id,
            ExtractedField.name == "certificate_number",
            ExtractedField.validated.is_(True),
        )
        .order_by(CheckRun.id.desc())
        .limit(1)
    ).first()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _normalise_number(value: str) -> str:
    """Compare identifiers the way steps 2 and 3 already compare them.

    Reuses step 3's accessor for step 2's own `normalise`, rather than
    reimplementing it, so "the same certificate number" means one thing across
    the whole system. Note what this does *not* do: it is not fuzzy matching. A
    number differing by one digit is a different number, which is the entire
    point of the check.
    """
    return _normaliser()(value)


@lru_cache(maxsize=1)
def _normaliser():
    # Built once: this runs inside the comparison loops, and rebuilding the
    # closure (and re-reading config) per identifier would be wasteful.
    from ..qr.scanner import _certificate_number_normaliser

    return _certificate_number_normaliser()


def _numbers_differ(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return _normalise_number(left) != _normalise_number(right)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
