"""Reads and writes. The only module that knows the table shapes.

Every write takes an actor and a role, validates the role, and mirrors itself
into `audit_log` in the same transaction — so a change and its audit entry
land together or not at all. There is deliberately no update or delete for
runs: a re-run creates a new `check_runs` row, because a reviewer must be able
to see that a document was clean under last month's rules and is flagged under
this month's.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import get, load_config
from . import audit_log
from .models import (
    Application,
    ApplicationCheck,
    AuditLog,
    CheckRun,
    CrossReference,
    DetectorDetail,
    Document,
    FacultyMemberRow,
    ExtractedField,
    FindingRow,
    Institution,
    PageExtraction,
    PerceptualHashRow,
    QRDetectionRow,
    RawLLMResponse,
    ReviewAction,
    SignatureRow,
    StageResult,
    VerificationLookup,
)
from .roles import validate_actor, validate_role


def config_hash(config: dict[str, Any]) -> str:
    """Hash of a config's contents, for the run record."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Institutions, applications, documents
# --------------------------------------------------------------------------


def get_or_create_institution(
    session: Session,
    name: str,
    actor: str,
    role: str,
    registry_reference: str | None = None,
    config: dict[str, Any] | None = None,
) -> Institution:
    validate_actor(actor)
    validate_role(role, config)

    existing = session.scalars(
        select(Institution).where(Institution.name == name)
    ).first()
    if existing is not None:
        return existing

    institution = Institution(name=name, registry_reference=registry_reference)
    session.add(institution)
    session.flush()
    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="institution_created",
        entity_type="institution",
        entity_id=institution.id,
        details={"name": name, "registry_reference": registry_reference},
    )
    return institution


def create_application(
    session: Session,
    institution_id: int,
    category: str,
    actor: str,
    role: str,
    config: dict[str, Any] | None = None,
) -> Application:
    validate_actor(actor)
    validate_role(role, config)

    application = Application(institution_id=institution_id, category=category)
    session.add(application)
    session.flush()
    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="application_created",
        entity_type="application",
        entity_id=application.id,
        details={"institution_id": institution_id, "category": category},
    )
    return application


def create_document(
    session: Session,
    application_id: int,
    document_type: str,
    original_filename: str,
    file_sha256: str,
    stored_path: str,
    size_bytes: int,
    actor: str,
    role: str,
    newly_stored: bool = True,
    config: dict[str, Any] | None = None,
    claim: str | None = None,
) -> Document:
    validate_actor(actor)
    validate_role(role, config)

    document = Document(
        application_id=application_id,
        document_type=document_type,
        original_filename=original_filename,
        file_sha256=file_sha256,
        stored_path=stored_path,
        size_bytes=size_bytes,
        uploaded_by=actor,
        uploaded_role=role,
        claim=claim,
    )
    session.add(document)
    session.flush()
    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="document_stored",
        entity_type="document",
        entity_id=document.id,
        details={
            "application_id": application_id,
            "document_type": document_type,
            "original_filename": original_filename,
            "file_sha256": file_sha256,
            "size_bytes": size_bytes,
            # False means an identical file was already in the store.
            "newly_stored": newly_stored,
        },
    )
    return document


# --------------------------------------------------------------------------
# Check runs
# --------------------------------------------------------------------------


def start_check_run(
    session: Session,
    document_id: int,
    actor: str,
    role: str,
    config_versions: dict[str, str | None] | None = None,
    config_hashes: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> CheckRun:
    """Begin a new run. Never reuses or overwrites an existing one."""
    validate_actor(actor)
    validate_role(role, config)
    versions = config_versions or {}

    run = CheckRun(
        document_id=document_id,
        actor=actor,
        role=role,
        status="running",
        extraction_config_version=versions.get("extraction"),
        certificate_config_version=versions.get("certificate_extraction"),
        qr_config_version=versions.get("qr_checks"),
        issuer_config_version=versions.get("issuer_verification"),
        persistence_config_version=versions.get("persistence"),
        signature_config_version=versions.get("signature_checks"),
        duplicate_config_version=versions.get("duplicate_checks"),
        faculty_config_version=versions.get("faculty_checks"),
        photo_config_version=versions.get("photo_checks"),
        config_hashes=config_hashes,
    )
    session.add(run)
    session.flush()
    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="check_run_started",
        entity_type="check_run",
        entity_id=run.id,
        details={"document_id": document_id, "config_versions": versions},
    )
    return run


def finish_check_run(
    session: Session,
    run_id: int,
    status: str,
    actor: str,
    role: str,
    provider: str | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
    registry_hash: str | None = None,
    summary: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> CheckRun:
    validate_actor(actor)
    validate_role(role, config)

    run = session.get(CheckRun, run_id)
    if run is None:
        raise ValueError(f"no check_run with id {run_id}")

    run.status = status
    run.finished_at = datetime.now(timezone.utc)
    if provider is not None:
        run.provider = provider
    if model_name is not None:
        run.model_name = model_name
    if prompt_version is not None:
        run.prompt_version = prompt_version
    if registry_hash is not None:
        run.registry_hash = registry_hash
    session.flush()

    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="check_run_finished",
        entity_type="check_run",
        entity_id=run.id,
        details={"status": status, **(summary or {})},
    )
    return run


def record_stage_result(
    session: Session,
    run_id: int,
    stage: str,
    status: str,
    actor: str,
    role: str,
    detail_status: str | None = None,
    reason: str | None = None,
    started_at: datetime | None = None,
    duration_ms: float | None = None,
    details: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> StageResult:
    validate_actor(actor)
    validate_role(role, config)

    result = StageResult(
        run_id=run_id,
        stage=stage,
        status=status,
        detail_status=detail_status,
        reason=reason,
        started_at=started_at or datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        duration_ms=duration_ms,
    )
    session.add(result)
    session.flush()

    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="stage_completed",
        entity_type="check_run",
        entity_id=run_id,
        details={
            "stage": stage,
            "status": status,
            "detail_status": detail_status,
            "reason": reason,
            "duration_ms": duration_ms,
            **(details or {}),
        },
    )
    return result


# --------------------------------------------------------------------------
# Stage payloads
# --------------------------------------------------------------------------


def save_page_extractions(
    session: Session,
    run_id: int,
    document_result: dict[str, Any],
    store_text: bool = True,
) -> list[PageExtraction]:
    rows: list[PageExtraction] = []
    for page in document_result.get("pages", []) or []:
        rows.append(
            PageExtraction(
                run_id=run_id,
                page_number=int(page.get("page_number", 0)),
                extraction_method=page.get("extraction_method"),
                text=page.get("text") if store_text else None,
                avg_confidence=page.get("avg_confidence"),
                text_layer_present=page.get("text_layer_present"),
                ocr_text=page.get("ocr_text") if store_text else None,
                decision_reason=page.get("decision_reason"),
                low_confidence=page.get("low_confidence"),
                error=page.get("error"),
            )
        )
    session.add_all(rows)
    session.flush()
    return rows


def save_extracted_fields(
    session: Session, run_id: int, certificate_result: dict[str, Any]
) -> list[ExtractedField]:
    rows: list[ExtractedField] = []
    for name, field in (certificate_result.get("fields") or {}).items():
        if not isinstance(field, dict):
            continue
        rows.append(
            ExtractedField(
                run_id=run_id,
                name=name,
                value=field.get("value"),
                source_snippet=field.get("source_snippet"),
                page=field.get("page"),
                confidence=field.get("confidence"),
                validated=bool(field.get("validated")),
                value_iso=field.get("value_iso"),
                invalidation_reason=field.get("invalidation_reason"),
                model_value=field.get("model_value"),
            )
        )
    session.add_all(rows)
    session.flush()
    return rows


def save_raw_llm_responses(
    session: Session, run_id: int, responses: list[str]
) -> list[RawLLMResponse]:
    rows = [
        RawLLMResponse(run_id=run_id, attempt_number=index + 1, response_text=text)
        for index, text in enumerate(responses or [])
        if text
    ]
    session.add_all(rows)
    session.flush()
    return rows


def save_qr_detections(
    session: Session, run_id: int, qr_result: dict[str, Any]
) -> list[QRDetectionRow]:
    rows: list[QRDetectionRow] = []
    for detection in qr_result.get("detections", []) or []:
        url = detection.get("url") or {}
        rows.append(
            QRDetectionRow(
                run_id=run_id,
                raw_text=detection.get("raw_text", ""),
                page=detection.get("page"),
                bounding_box=detection.get("bounding_box"),
                decoder=detection.get("decoder"),
                source=detection.get("source"),
                preprocessing=detection.get("preprocessing"),
                payload_kind=detection.get("payload_kind"),
                parsed_host=url.get("host") if isinstance(url, dict) else None,
                certificate_id=detection.get("certificate_id"),
            )
        )
    session.add_all(rows)
    session.flush()
    return rows


def save_verification_lookups(
    session: Session, run_id: int, issuer_result: dict[str, Any]
) -> list[VerificationLookup]:
    requests = issuer_result.get("audit", {}) or {}
    by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in requests.get("requests", []) or []:
        key = (str(entry.get("certificate_id")), str(entry.get("id_origin")))
        by_id[key] = entry

    rows: list[VerificationLookup] = []
    for lookup in issuer_result.get("lookups", []) or []:
        key = (str(lookup.get("certificate_id")), str(lookup.get("id_origin")))
        request = by_id.get(key, {})
        attempts = request.get("attempts") or []
        rows.append(
            VerificationLookup(
                run_id=run_id,
                certificate_id=lookup.get("certificate_id", ""),
                id_origin=lookup.get("id_origin", ""),
                trust=lookup.get("trust"),
                outcome=lookup.get("outcome"),
                status=lookup.get("status"),
                status_reasons=lookup.get("status_reasons"),
                comparisons=lookup.get("comparisons"),
                issuer_record=lookup.get("record"),
                raw_response=request.get("raw_response"),
                http_status=lookup.get("http_status"),
                latency_ms=request.get("latency_ms"),
                attempt_count=len(attempts),
                attempts=attempts,
                error=lookup.get("error"),
            )
        )
    session.add_all(rows)
    session.flush()
    return rows


def save_findings(
    session: Session,
    run_id: int | None,
    detector: str,
    findings: list[dict[str, Any]],
    actor: str,
    role: str,
    config: dict[str, Any] | None = None,
    application_check_id: int | None = None,
) -> list[FindingRow]:
    """Write findings from any detector into the one findings table.

    Exactly one of `run_id` (a document's run) or `application_check_id` (an
    application-level pass) owns them.
    """
    if (run_id is None) == (application_check_id is None):
        raise ValueError("a finding belongs to exactly one of a run or an application check")
    validate_actor(actor)
    validate_role(role, config)

    rows: list[FindingRow] = []
    for finding in findings or []:
        rows.append(
            FindingRow(
                run_id=run_id,
                application_check_id=application_check_id,
                detector=detector,
                type=str(finding.get("type", "unknown")),
                strength=str(finding.get("strength", "weak")),
                reason=str(finding.get("reason", "")),
                evidence=finding.get("evidence"),
                confidence=finding.get("confidence"),
                review_status="open",
            )
        )
    session.add_all(rows)
    session.flush()
    cfg = config if config is not None else load_config("persistence")
    carried = (
        _carry_forward_reviews(session, rows, run_id, application_check_id, detector, actor, role)
        if bool(get(cfg, "review.carry_forward_on_rerun", False))
        else {}
    )

    if rows:
        audit_log.append(
            session,
            actor=actor,
            role=role,
            action="findings_created",
            entity_type="check_run" if run_id is not None else "application_check",
            entity_id=run_id if run_id is not None else application_check_id,
            details={
                "detector": detector,
                "count": len(rows),
                # Ids and types only: the evidence stays in the findings table.
                "finding_ids": [row.id for row in rows],
                "types": [row.type for row in rows],
                **({"reviews_carried_from": carried} if carried else {}),
            },
        )
    return rows


def _carry_forward_reviews(
    session: Session,
    rows: list[FindingRow],
    run_id: int | None,
    application_check_id: int | None,
    detector: str,
    actor: str,
    role: str,
) -> dict[str, int]:
    """Give each new finding the review status of an identical one in the
    previous run of the same document (or previous check of the application).

    Identical means same detector, type and reason; the reason quotes the
    values compared, so a changed fact is a different finding and stays open.
    Returns {new finding id: earlier finding id}.
    """
    if run_id is not None:
        run = session.get(CheckRun, run_id)
        previous_owner = session.scalars(
            select(CheckRun.id)
            .where(CheckRun.document_id == run.document_id, CheckRun.id < run_id)
            .order_by(CheckRun.id.desc())
            .limit(1)
        ).first()
        owner_column = FindingRow.run_id
    else:
        check = session.get(ApplicationCheck, application_check_id)
        previous_owner = session.scalars(
            select(ApplicationCheck.id)
            .where(ApplicationCheck.application_id == check.application_id, ApplicationCheck.id < application_check_id)
            .order_by(ApplicationCheck.id.desc())
            .limit(1)
        ).first()
        owner_column = FindingRow.application_check_id
    if previous_owner is None:
        return {}
    pool: dict[tuple[str, str], list[FindingRow]] = {}
    for earlier in session.scalars(
        select(FindingRow)
        .where(owner_column == previous_owner, FindingRow.detector == detector, FindingRow.review_status != "open")
        .order_by(FindingRow.id)
    ).all():
        pool.setdefault((earlier.type, earlier.reason), []).append(earlier)
    carried: dict[str, int] = {}
    for row in rows:
        matches = pool.get((row.type, row.reason))
        if not matches:
            continue
        earlier = matches.pop(0)
        row.review_status = earlier.review_status
        session.add(
            ReviewAction(
                finding_id=row.id, actor=actor, role=role, action="carried_forward",
                note=f"Review status '{earlier.review_status}' carried from finding #{earlier.id}, identical in the previous run.",
            )
        )
        carried[str(row.id)] = earlier.id
    session.flush()
    return carried


# --------------------------------------------------------------------------
# Review actions
# --------------------------------------------------------------------------


def record_review_action(
    session: Session,
    finding_id: int,
    action: str,
    actor: str,
    role: str,
    note: str | None = None,
    config: dict[str, Any] | None = None,
) -> ReviewAction:
    """A human acting on a finding.

    The action-to-status map is config, so an organisation can decide what
    "dismiss" means for its own workflow. An action mapped to null (comment)
    records the note without changing the status.
    """
    validate_actor(actor)
    validate_role(role, config)
    cfg = config if config is not None else load_config("persistence")

    finding = session.get(FindingRow, finding_id)
    if finding is None:
        raise ValueError(f"no finding with id {finding_id}")

    actions = get(cfg, "review.actions", {}) or {}
    if action not in actions:
        raise ValueError(
            f"{action!r} is not a known review action; allowed: "
            f"{', '.join(sorted(actions))}"
        )

    new_status = actions.get(action)
    previous_status = finding.review_status

    entry = ReviewAction(
        finding_id=finding_id, actor=actor, role=role, action=action, note=note
    )
    session.add(entry)

    if new_status:
        statuses = [str(s) for s in (get(cfg, "review.statuses", []) or [])]
        if new_status not in statuses:
            raise ValueError(
                f"review.actions maps {action!r} to {new_status!r}, which is "
                f"not in review.statuses"
            )
        finding.review_status = str(new_status)

    session.flush()
    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="review_action",
        entity_type="finding",
        entity_id=finding_id,
        details={
            "review_action": action,
            "previous_status": previous_status,
            "new_status": finding.review_status,
            "has_note": note is not None,
        },
    )
    return entry


# --------------------------------------------------------------------------
# Reading it back
# --------------------------------------------------------------------------


def _document_dict(document: Document) -> dict[str, Any]:
    return {
        "id": document.id,
        "document_type": document.document_type,
        "original_filename": document.original_filename,
        "file_sha256": document.file_sha256,
        "stored_path": document.stored_path,
        "size_bytes": document.size_bytes,
        "uploaded_by": document.uploaded_by,
        "uploaded_role": document.uploaded_role,
        "claim": document.claim,
        "created_at": _iso(document.created_at),
    }


def _run_dict(session: Session, run: CheckRun) -> dict[str, Any]:
    findings = session.scalars(
        select(FindingRow).where(FindingRow.run_id == run.id).order_by(FindingRow.id)
    ).all()

    return {
        "id": run.id,
        "status": run.status,
        "actor": run.actor,
        "role": run.role,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "config_versions": {
            "extraction": run.extraction_config_version,
            "certificate_extraction": run.certificate_config_version,
            "qr_checks": run.qr_config_version,
            "issuer_verification": run.issuer_config_version,
            "persistence": run.persistence_config_version,
            "signature_checks": run.signature_config_version,
            "duplicate_checks": run.duplicate_config_version,
            "faculty_checks": run.faculty_config_version,
            "photo_checks": run.photo_config_version,
        },
        "config_hashes": run.config_hashes,
        "provider": run.provider,
        "model_name": run.model_name,
        "prompt_version": run.prompt_version,
        "registry_hash": run.registry_hash,
        "stages": [
            {
                "stage": stage.stage,
                "status": stage.status,
                "detail_status": stage.detail_status,
                "reason": stage.reason,
                "duration_ms": stage.duration_ms,
            }
            for stage in sorted(run.stage_results, key=lambda s: s.id)
        ],
        "pages": [
            {
                "page_number": page.page_number,
                "extraction_method": page.extraction_method,
                "avg_confidence": page.avg_confidence,
                "text_layer_present": page.text_layer_present,
                "decision_reason": page.decision_reason,
                "low_confidence": page.low_confidence,
                "error": page.error,
                "text": page.text,
            }
            for page in sorted(run.page_extractions, key=lambda p: p.page_number)
        ],
        "extracted_fields": [
            {
                "name": field.name,
                "value": field.value,
                "source_snippet": field.source_snippet,
                "page": field.page,
                "confidence": field.confidence,
                "validated": field.validated,
                "value_iso": field.value_iso,
                "invalidation_reason": field.invalidation_reason,
                "model_value": field.model_value,
            }
            for field in sorted(run.extracted_fields, key=lambda f: f.name)
        ],
        "qr_detections": [
            {
                "raw_text": detection.raw_text,
                "page": detection.page,
                "bounding_box": detection.bounding_box,
                "decoder": detection.decoder,
                "source": detection.source,
                "preprocessing": detection.preprocessing,
                "payload_kind": detection.payload_kind,
                "parsed_host": detection.parsed_host,
                "certificate_id": detection.certificate_id,
            }
            for detection in sorted(run.qr_detections, key=lambda d: d.id)
        ],
        "verification_lookups": [
            {
                "certificate_id": lookup.certificate_id,
                "id_origin": lookup.id_origin,
                "trust": lookup.trust,
                "outcome": lookup.outcome,
                "status": lookup.status,
                "status_reasons": lookup.status_reasons,
                "comparisons": lookup.comparisons,
                "issuer_record": lookup.issuer_record,
                "http_status": lookup.http_status,
                "latency_ms": lookup.latency_ms,
                "attempt_count": lookup.attempt_count,
                "error": lookup.error,
            }
            for lookup in sorted(run.verification_lookups, key=lambda l: l.id)
        ],
        "signatures": [
            {
                "signature_index": row.signature_index,
                "field_name": row.field_name,
                "present": row.present,
                "parsed": row.parsed,
                "byte_range": row.byte_range,
                "coverage": row.coverage,
                "covers_whole_file": row.covers_whole_file,
                "digest_matches": row.digest_matches,
                "digest_algorithm": row.digest_algorithm,
                "computed_digest_hex": row.computed_digest_hex,
                "signed_digest_hex": row.signed_digest_hex,
                "appended_byte_length": row.appended_byte_length,
                "appended_object_count": row.appended_object_count,
                "signer_name_unverified": row.signer_name_unverified,
                "claimed_signing_time_unverified": (
                    row.claimed_signing_time_unverified
                ),
                # Restated per row, so a consumer reading one signature out of
                # the report cannot miss it.
                "signer_identity_verified": False,
                "parse_error": row.parse_error,
            }
            for row in sorted(run.signature_details, key=lambda s: s.signature_index)
        ],
        "perceptual_hashes": [
            {
                "page": row.page,
                "hash_kind": row.hash_kind,
                "hash_value": row.hash_value,
                "hash_size": row.hash_size,
                "bit_count": row.hash_size * row.hash_size,
            }
            for row in sorted(run.perceptual_hashes, key=lambda h: h.id)
        ],
        "details": {
            row.kind: row.payload
            for row in sorted(run.detector_details, key=lambda d: d.id)
        },
        "raw_llm_response_count": len(run.raw_llm_responses),
        "findings": [_finding_dict(finding) for finding in findings],
    }


def _finding_dict(finding: FindingRow) -> dict[str, Any]:
    return {
        "id": finding.id,
        "detector": finding.detector,
        "type": finding.type,
        "strength": finding.strength,
        "reason": finding.reason,
        "evidence": finding.evidence,
        "confidence": finding.confidence,
        "review_status": finding.review_status,
        "created_at": _iso(finding.created_at),
        "review_actions": [
            {
                "actor": action.actor,
                "role": action.role,
                "action": action.action,
                "note": action.note,
                "created_at": _iso(action.created_at),
            }
            for action in sorted(finding.review_actions, key=lambda a: a.id)
        ],
    }


def get_application_report(
    session: Session,
    application_id: int,
    all_runs: bool = False,
    actor: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Everything known about one application, as one JSON-serialisable tree.

    By default each document carries only its latest run, because that is the
    reviewer's question; `all_runs` returns the full history. An actor is
    optional for a read, and recorded in the audit log when given.
    """
    application = session.get(Application, application_id)
    if application is None:
        raise ValueError(f"no application with id {application_id}")

    institution = application.institution
    documents: list[dict[str, Any]] = []

    for document in sorted(application.documents, key=lambda d: d.id):
        runs = sorted(document.check_runs, key=lambda r: r.id)
        chosen = runs if all_runs else runs[-1:]
        entry = _document_dict(document)
        entry["run_count"] = len(runs)
        entry["runs"] = [_run_dict(session, run) for run in chosen]
        # Read from both ends. A relationship discovered while processing
        # document B belongs just as much to A, whose run finished before B
        # existed - and runs are never rewritten, so this is where it shows up.
        entry["cross_references"] = cross_references_for_document(
            session, document.id
        )
        documents.append(entry)

    if actor:
        audit_log.append(
            session,
            actor=actor,
            role=role or "officer",
            action="report_read",
            entity_type="application",
            entity_id=application_id,
            details={"all_runs": all_runs},
        )
        session.commit()

    check = session.scalars(
        select(ApplicationCheck)
        .where(ApplicationCheck.application_id == application_id)
        .order_by(ApplicationCheck.id.desc())
        .limit(1)
    ).first()
    application_check = None
    if check is not None:
        application_check = {
            "id": check.id,
            "status": check.status,
            "config_version": check.config_version,
            "created_at": _iso(check.created_at),
            "actor": check.actor,
            **(check.payload or {}),
            "findings": [_finding_dict(f) for f in sorted(check.findings, key=lambda f: f.id)],
        }

    return {
        "application": {
            "id": application.id,
            "category": application.category,
            "created_at": _iso(application.created_at),
        },
        "application_check": application_check,
        "institution": {
            "id": institution.id,
            "name": institution.name,
            "registry_reference": institution.registry_reference,
        },
        "documents": documents,
    }


def findings_for_run(session: Session, run_id: int) -> list[FindingRow]:
    return list(
        session.scalars(
            select(FindingRow)
            .where(FindingRow.run_id == run_id)
            .order_by(FindingRow.id)
        ).all()
    )


def runs_for_document(session: Session, document_id: int) -> list[CheckRun]:
    return list(
        session.scalars(
            select(CheckRun)
            .where(CheckRun.document_id == document_id)
            .order_by(CheckRun.id)
        ).all()
    )


def documents_by_hash(session: Session, file_sha256: str) -> list[Document]:
    return list(
        session.scalars(
            select(Document)
            .where(Document.file_sha256 == file_sha256)
            .order_by(Document.id)
        ).all()
    )


def audit_entries(session: Session) -> list[AuditLog]:
    return audit_log.all_entries(session)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Step 6: signature details, perceptual hashes, cross references
# --------------------------------------------------------------------------


def save_signature_details(
    session: Session, run_id: int, signature_result: dict[str, Any]
) -> list[SignatureRow]:
    """Store each signature's integrity detail, findings or not.

    A clean signed file produces no findings and still gets rows here: "this
    document carries one intact signature" is a fact a reviewer wants, and it
    is only visible if the detail is recorded when nothing is wrong.
    """
    rows: list[SignatureRow] = []
    for index, detail in enumerate(signature_result.get("signatures", []) or []):
        appended = detail.get("appended") or {}
        rows.append(
            SignatureRow(
                run_id=run_id,
                signature_index=index,
                field_name=detail.get("field_name"),
                present=bool(detail.get("present")),
                parsed=bool(detail.get("parsed")),
                byte_range=detail.get("byte_range"),
                coverage=detail.get("coverage"),
                covers_whole_file=bool(detail.get("covers_whole_file")),
                digest_matches=detail.get("digest_matches"),
                digest_algorithm=detail.get("digest_algorithm"),
                computed_digest_hex=detail.get("computed_digest_hex"),
                signed_digest_hex=detail.get("signed_digest_hex"),
                appended_byte_length=appended.get("byte_length"),
                appended_object_count=appended.get("object_count"),
                signer_name_unverified=detail.get("signer_name_unverified"),
                claimed_signing_time_unverified=detail.get(
                    "claimed_signing_time_unverified"
                ),
                parse_error=detail.get("parse_error"),
            )
        )
    session.add_all(rows)
    session.flush()
    return rows


def latest_certificate_number(session: Session, document_id: int) -> str | None:
    """The newest validated certificate number for a document, if any.

    Only `validated: true` values are read. Step 2 returns null both for an
    absent field and for one that failed the snippet guard, and neither is a
    number this system may compare against anything.
    """
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


def latest_qr_certificate_ids(session: Session, document_id: int) -> list[str]:
    """Certificate ids decoded from QR codes on a document's newest run."""
    run_id = _latest_run_id_with_qr(session, document_id)
    if run_id is None:
        return []
    values = session.scalars(
        select(QRDetectionRow.certificate_id)
        .where(
            QRDetectionRow.run_id == run_id,
            QRDetectionRow.certificate_id.is_not(None),
        )
        .order_by(QRDetectionRow.id)
    ).all()
    return list(dict.fromkeys(str(value) for value in values if value))


def latest_qr_payloads(session: Session, document_id: int) -> list[str]:
    run_id = _latest_run_id_with_qr(session, document_id)
    if run_id is None:
        return []
    values = session.scalars(
        select(QRDetectionRow.raw_text)
        .where(QRDetectionRow.run_id == run_id)
        .order_by(QRDetectionRow.id)
    ).all()
    return list(dict.fromkeys(str(value) for value in values if value))


def _latest_run_id_with_qr(session: Session, document_id: int) -> int | None:
    return session.scalars(
        select(CheckRun.id)
        .join(QRDetectionRow, QRDetectionRow.run_id == CheckRun.id)
        .where(CheckRun.document_id == document_id)
        .order_by(CheckRun.id.desc())
        .limit(1)
    ).first()


def perceptual_hashes_for_document(
    session: Session, document_id: int
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(PerceptualHashRow)
        .where(PerceptualHashRow.document_id == document_id)
        .order_by(PerceptualHashRow.id)
    ).all()
    return [
        {
            "page": row.page,
            "hash_kind": row.hash_kind,
            "hash_value": row.hash_value,
            "hash_size": row.hash_size,
            "bit_count": row.hash_size * row.hash_size,
            "run_id": row.run_id,
        }
        for row in rows
    ]


def record_cross_reference_audit(
    session: Session,
    run_id: int,
    document_id: int,
    relationships: list[dict[str, Any]],
    actor: str,
    role: str,
    config: dict[str, Any] | None = None,
) -> None:
    """Audit the relationships a run discovered: ids, kinds and counts only."""
    validate_actor(actor)
    validate_role(role, config)
    if not relationships:
        return
    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="cross_references_recorded",
        entity_type="document",
        entity_id=document_id,
        details={
            "run_id": run_id,
            "count": len(relationships),
            "kinds": sorted({str(entry.get("kind")) for entry in relationships}),
            "other_document_ids": sorted(
                {int(entry["other_document_id"]) for entry in relationships}
            ),
        },
    )


def cross_references_for_document(
    session: Session, document_id: int
) -> list[dict[str, Any]]:
    """Every relationship involving this document, read from both ends.

    The same function the duplicate detector exposes, re-exported here so the
    report does not import a detector. Both read the one normalised row, which
    is what makes a relationship visible on both sides without either
    document's finished run being touched.
    """
    from ..duplicates.detector import cross_references_for_document as reader

    return reader(session, document_id)


# --------------------------------------------------------------------------
# Faculty and photo detectors
# --------------------------------------------------------------------------


def save_detector_detail(
    session: Session, run_id: int, detector: str, kind: str, payload: dict[str, Any]
) -> DetectorDetail:
    row = DetectorDetail(run_id=run_id, detector=detector, kind=kind, payload=payload)
    session.add(row)
    session.flush()
    return row


def save_faculty_members(
    session: Session,
    document_id: int,
    run_id: int,
    institution_id: int,
    people: list[dict[str, Any]],
    normalise: Any,
) -> list[FacultyMemberRow]:
    rows = [
        FacultyMemberRow(
            document_id=document_id,
            run_id=run_id,
            institution_id=institution_id,
            faculty_id=person.get("faculty_id"),
            name=str(person.get("name", "")),
            name_normalised=normalise(str(person.get("name", ""))),
            department=person.get("department"),
            designation=person.get("designation"),
            qualification=person.get("qualification"),
        )
        for person in people
    ]
    session.add_all(rows)
    session.flush()
    return rows


def faculty_declared_elsewhere(
    session: Session, exclude_institution_id: int
) -> list[dict[str, Any]]:
    """Faculty rows declared by other institutions, from each document's newest run."""
    latest = (
        select(CheckRun.document_id, func.max(CheckRun.id).label("run_id"))
        .group_by(CheckRun.document_id)
        .subquery()
    )
    rows = session.execute(
        select(FacultyMemberRow, Institution)
        .join(latest, FacultyMemberRow.run_id == latest.c.run_id)
        .join(Institution, FacultyMemberRow.institution_id == Institution.id)
        .where(FacultyMemberRow.institution_id != exclude_institution_id)
    ).all()
    return [
        {
            "institution_id": member.institution_id,
            "institution_name": institution.name,
            "document_id": member.document_id,
            "faculty_id": member.faculty_id,
            "name": member.name,
            "name_normalised": member.name_normalised,
            "department": member.department,
            "qualification": member.qualification,
        }
        for member, institution in rows
    ]
