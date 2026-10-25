"""The single entry point: chain steps 1-4 over one document and store it all.

Two rules shape this module.

**Degradation is recorded, not fatal.** An expected failure — a corrupt page,
an unreadable text layer, an unreachable issuer — is a *result*, and the run
continues into whatever stage can still do something useful. A document whose
text extraction failed can still have its QR decoded, and that QR's id can
still be looked up, at lower trust. Crashing the run would throw away the
stages that did work, which is exactly the evidence a reviewer needs in order
to understand why the rest is missing.

**Each stage commits in its own transaction.** A failure in stage 3 must not
roll back stages 1 and 2. That is why every stage takes a fresh session rather
than sharing one across the run.

Nothing here computes a risk score or an overall verification status. Step 4's
per-document status and the findings are the output; combining them is step 7.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import get, load_config
from ..db import repository
from ..db.engine import Database
from ..db.roles import SYSTEM_ACTOR, SYSTEM_ROLE, validate_actor, validate_role
from ..db.storage import store_file

# Stage status vocabulary. Deliberately separate from the verification status
# taxonomy: these describe whether a *check ran*, not what it concluded.
STAGE_OK = "ok"
STAGE_DEGRADED = "degraded"
STAGE_SKIPPED = "skipped"
STAGE_FAILED = "failed"

CONFIG_NAMES = (
    "extraction",
    "certificate_extraction",
    "qr_checks",
    "issuer_verification",
    "signature_checks",
    "duplicate_checks",
    "faculty_checks",
    "photo_checks",
    "persistence",
)


@dataclass
class StageOutcome:
    stage: str
    status: str
    detail_status: str | None = None
    reason: str | None = None
    duration_ms: float = 0.0
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "detail_status": self.detail_status,
            "reason": self.reason,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class RunSummary:
    application_id: int
    document_id: int
    run_id: int
    status: str
    stages: list[StageOutcome] = field(default_factory=list)
    finding_count: int = 0
    finding_types: list[str] = field(default_factory=list)
    # Step 4's per-document verification status, where the stage ran.
    verification_status: str | None = None
    file_sha256: str | None = None
    duplicate_of_document_ids: list[int] = field(default_factory=list)
    # Step 6. The signature status is the file-level integrity outcome; the
    # relationships are every cross-reference this document now has, from
    # either end.
    signature_status: str | None = None
    cross_reference_count: int = 0
    cross_reference_kinds: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "document_id": self.document_id,
            "run_id": self.run_id,
            "status": self.status,
            "stages": [stage.to_dict() for stage in self.stages],
            "finding_count": self.finding_count,
            "finding_types": self.finding_types,
            "verification_status": self.verification_status,
            "signature_status": self.signature_status,
            "file_sha256": self.file_sha256,
            "duplicate_of_document_ids": self.duplicate_of_document_ids,
            "cross_reference_count": self.cross_reference_count,
            "cross_reference_kinds": self.cross_reference_kinds,
        }


class Pipeline:
    """Holds the injectable collaborators so tests need no network.

    `llm_provider` takes the stub provider; `issuer_transport` takes the mock
    issuer's in-process TestClient; `configs` overrides the loaded config.
    """

    def __init__(
        self,
        database: Database | None = None,
        llm_provider: Any | None = None,
        issuer_transport: Any | None = None,
        issuer_sleep: Any | None = None,
        configs: dict[str, dict[str, Any]] | None = None,
        registry_hash: str | None = None,
        as_of: str | None = None,
    ) -> None:
        self.configs = {
            name: (configs or {}).get(name) or load_config(name)
            for name in CONFIG_NAMES
        }
        self.persistence = self.configs["persistence"]
        # Application-level checks run after the documents, so their config is
        # stamped on the application check rather than on each document run.
        self.compliance_config = (configs or {}).get("compliance_checks") or load_config("compliance_checks")
        self.database = database if database is not None else Database(self.persistence)
        self.llm_provider = llm_provider
        self.issuer_transport = issuer_transport
        self.issuer_sleep = issuer_sleep
        self.registry_hash = registry_hash
        self.as_of = as_of
        # Optional progress listener: called with (run_id, stage dict) after
        # each stage is recorded. Observes only; it cannot alter a result.
        self.on_stage: Any | None = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def process_document(
        self,
        application_id: int,
        file_path: str | Path,
        document_type: str,
        actor: str,
        role: str,
        claim: str | None = None,
    ) -> RunSummary:
        validate_actor(actor)
        validate_role(role, self.persistence)

        source = Path(file_path)

        # --- store the file and create the document row (own transaction) ---
        stored = store_file(source, self.persistence)
        with self.database.session() as session:
            with session.begin():
                duplicates = [
                    document.id
                    for document in repository.documents_by_hash(
                        session, stored.sha256
                    )
                ]
                document = repository.create_document(
                    session,
                    application_id=application_id,
                    document_type=document_type,
                    original_filename=source.name,
                    file_sha256=stored.sha256,
                    stored_path=str(stored.path),
                    size_bytes=stored.size_bytes,
                    actor=actor,
                    role=role,
                    newly_stored=stored.newly_stored,
                    config=self.persistence,
                    claim=claim,
                )
            document_id = document.id

        return self._run(
            application_id=application_id,
            document_id=document_id,
            stored_path=stored.path,
            file_sha256=stored.sha256,
            document_type=document_type,
            duplicates=duplicates,
            actor=actor,
            role=role,
        )

    def rerun_document(self, document_id: int, actor: str, role: str) -> RunSummary:
        """A new run over a document already stored, under today's config.

        Never touches the earlier runs: a reviewer must be able to compare
        what the document looked like under last month's rules with this one.
        """
        from ..db.models import Document
        from ..db.storage import verify_stored_file

        validate_actor(actor)
        validate_role(role, self.persistence)
        with self.database.session() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError(f"no document with id {document_id}")
            if not verify_stored_file(document.stored_path, document.file_sha256):
                raise ValueError(
                    f"stored file for document {document_id} is missing or no "
                    f"longer matches its recorded hash"
                )
            duplicates = [
                other.id
                for other in repository.documents_by_hash(session, document.file_sha256)
                if other.id != document_id
            ]
            application_id = document.application_id
            stored_path = Path(document.stored_path)
            sha256 = document.file_sha256
            document_type = document.document_type

        return self._run(
            application_id=application_id,
            document_id=document_id,
            stored_path=stored_path,
            file_sha256=sha256,
            document_type=document_type,
            duplicates=duplicates,
            actor=actor,
            role=role,
        )

    def _run(
        self,
        application_id: int,
        document_id: int,
        stored_path: Path,
        file_sha256: str,
        document_type: str,
        duplicates: list[int],
        actor: str,
        role: str,
    ) -> RunSummary:
        # --- create the run (own transaction) ---
        versions = {
            name: str(get(self.configs[name], "config_version", "unknown"))
            for name in CONFIG_NAMES
        }
        hashes = {
            name: repository.config_hash(self.configs[name]) for name in CONFIG_NAMES
        }
        with self.database.session() as session:
            with session.begin():
                run = repository.start_check_run(
                    session,
                    document_id=document_id,
                    actor=actor,
                    role=role,
                    config_versions=versions,
                    config_hashes=hashes,
                    config=self.persistence,
                )
            run_id = run.id

        summary = RunSummary(
            application_id=application_id,
            document_id=document_id,
            run_id=run_id,
            status="running",
            file_sha256=file_sha256,
            duplicate_of_document_ids=duplicates,
        )

        # --- stages, each in its own transaction ---
        # Routed by document type: a faculty list and a photograph each have
        # their own detector; everything else is read as a certificate.
        kind = self.document_kind(document_type)
        fields = StageOutcome(stage="certificate_fields", status=STAGE_SKIPPED)
        if kind == "faculty":
            summary.stages.append(
                self._stage_faculty(run_id, document_id, application_id, stored_path, actor, role, summary)
            )
        elif kind == "photo":
            summary.stages.extend(
                self._stages_photo(run_id, document_id, application_id, stored_path, actor, role, summary)
            )
        else:
            fields = self._certificate_stages(
                run_id, document_id, stored_path, document_type, actor, role, summary
            )

        summary.status = self._roll_up(summary.stages)

        with self.database.session() as session:
            with session.begin():
                repository.finish_check_run(
                    session,
                    run_id=run_id,
                    status=summary.status,
                    actor=actor,
                    role=role,
                    provider=self._provider_name(fields),
                    model_name=self._model_name(fields),
                    prompt_version=self._prompt_version(fields),
                    registry_hash=self.registry_hash,
                    summary={
                        "finding_count": summary.finding_count,
                        "verification_status": summary.verification_status,
                        "stages": [stage.to_dict() for stage in summary.stages],
                    },
                    config=self.persistence,
                )
                findings = repository.findings_for_run(session, run_id)
                summary.finding_count = len(findings)
                summary.finding_types = [finding.type for finding in findings]

        return summary

    def check_application(self, application_id: int, actor: str, role: str) -> dict[str, Any]:
        """Run the application-level checks over every document's latest run.

        Call after the documents of an application have been processed. A new
        pass is a new `application_checks` row; earlier passes are kept.
        """
        from ..compliance import run_application_checks
        from ..db import audit_log
        from ..db.models import ApplicationCheck

        validate_actor(actor)
        validate_role(role, self.persistence)
        with self.database.session() as session:
            with session.begin():
                payload = run_application_checks(
                    session,
                    application_id,
                    config=self.compliance_config,
                    faculty_config=self.configs["faculty_checks"],
                    as_of=self.as_of,
                )
                row = ApplicationCheck(
                    application_id=application_id,
                    config_version=str(get(self.compliance_config, "config_version", "unknown")),
                    config_hash=repository.config_hash(self.compliance_config),
                    status=payload["status"],
                    payload={k: v for k, v in payload.items() if k != "findings"},
                    actor=actor,
                    role=role,
                )
                session.add(row)
                session.flush()
                by_detector: dict[str, list[dict[str, Any]]] = {}
                for finding in payload["findings"]:
                    by_detector.setdefault(finding["detector"], []).append(finding)
                for detector, findings in by_detector.items():
                    repository.save_findings(
                        session, None, detector=detector, findings=findings, actor=actor,
                        role=role, config=self.persistence, application_check_id=row.id,
                    )
                audit_log.append(
                    session, actor=actor, role=role, action="application_checked",
                    entity_type="application", entity_id=application_id,
                    details={
                        "application_check_id": row.id, "status": payload["status"],
                        "finding_count": len(payload["findings"]),
                        "config_version": row.config_version,
                    },
                )
                payload["application_check_id"] = row.id
        return payload

    def document_kind(self, document_type: str) -> str:
        """certificate | faculty | photo, from `pipeline.document_kinds`."""
        kinds = get(self.persistence, "pipeline.document_kinds", {}) or {}
        for kind, types in kinds.items():
            if document_type in (types or []):
                return str(kind)
        return "certificate"

    def _certificate_stages(
        self,
        run_id: int,
        document_id: int,
        path: Path,
        document_type: str,
        actor: str,
        role: str,
        summary: RunSummary,
    ) -> StageOutcome:
        """Stages 1-6 over a certificate. Returns the field stage's outcome."""
        extraction = self._stage_extraction(run_id, path, actor, role)
        summary.stages.append(extraction)

        fields = self._stage_certificate_fields(
            run_id, extraction, actor, role, summary
        )
        summary.stages.append(fields)

        qr = self._stage_qr(run_id, path, fields, actor, role)
        summary.stages.append(qr)

        issuer = self._stage_issuer(run_id, fields, qr, actor, role, summary)
        summary.stages.append(issuer)

        signature = self._stage_signature(
            run_id, path, document_type, fields, actor, role, summary
        )
        summary.stages.append(signature)

        duplicate = self._stage_duplicate(
            run_id, document_id, fields, qr, actor, role, summary
        )
        summary.stages.append(duplicate)
        return fields

    # ------------------------------------------------------------------
    # Stage 1: text extraction
    # ------------------------------------------------------------------

    def _stage_extraction(
        self, run_id: int, path: Path, actor: str, role: str
    ) -> StageOutcome:
        from ..extraction import extract_document

        started = time.perf_counter()
        try:
            document_result = extract_document(path, config=self.configs["extraction"])
            payload = document_result.to_dict()
            detail = payload.get("status")
        except Exception as exc:
            # The module returns errors as data, so reaching here means
            # something genuinely unexpected. Recorded, never raised onward.
            return self._record_stage(
                run_id,
                "extraction",
                STAGE_FAILED,
                actor,
                role,
                reason=f"unexpected error: {type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        status = {
            "ok": STAGE_OK,
            "partial": STAGE_DEGRADED,
            "failed": STAGE_FAILED,
        }.get(str(detail), STAGE_DEGRADED)

        reason = payload.get("error_detail") or payload.get("error")

        with self.database.session() as session:
            with session.begin():
                repository.save_page_extractions(
                    session,
                    run_id,
                    payload,
                    store_text=bool(
                        get(self.persistence, "runs.store_page_text", True)
                    ),
                )

        outcome = self._record_stage(
            run_id,
            "extraction",
            status,
            actor,
            role,
            detail_status=str(detail) if detail else None,
            reason=reason,
            duration_ms=(time.perf_counter() - started) * 1000,
            details={"page_count": len(payload.get("pages", []) or [])},
        )
        outcome.payload = payload
        return outcome

    # ------------------------------------------------------------------
    # Stage 2: certificate fields
    # ------------------------------------------------------------------

    def _stage_certificate_fields(
        self,
        run_id: int,
        extraction: StageOutcome,
        actor: str,
        role: str,
        summary: RunSummary,
    ) -> StageOutcome:
        from ..certificates.extractor import extract_certificate_fields

        started = time.perf_counter()

        pages = self._usable_pages(extraction)
        if not pages:
            return self._record_stage(
                run_id,
                "certificate_fields",
                STAGE_SKIPPED,
                actor,
                role,
                reason=(
                    "no usable page text from the extraction stage, so there "
                    "was nothing for field extraction to read"
                ),
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            result = extract_certificate_fields(
                pages,
                config=self.configs["certificate_extraction"],
                provider=self.llm_provider,
            )
            payload = result.to_dict()
        except Exception as exc:
            return self._record_stage(
                run_id,
                "certificate_fields",
                STAGE_FAILED,
                actor,
                role,
                reason=f"unexpected error: {type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        detail = payload.get("status")
        status = {
            "ok": STAGE_OK,
            "extraction_failed": STAGE_FAILED,
            "provider_unavailable": STAGE_DEGRADED,
        }.get(str(detail), STAGE_DEGRADED)

        store_raw = bool(get(self.persistence, "runs.store_raw_llm_responses", True))
        with self.database.session() as session:
            with session.begin():
                repository.save_extracted_fields(session, run_id, payload)
                if store_raw:
                    repository.save_raw_llm_responses(
                        session,
                        run_id,
                        (payload.get("audit") or {}).get("raw_responses") or [],
                    )

        outcome = self._record_stage(
            run_id,
            "certificate_fields",
            status,
            actor,
            role,
            detail_status=str(detail) if detail else None,
            reason=payload.get("error"),
            duration_ms=(time.perf_counter() - started) * 1000,
            details={
                "field_count": len(payload.get("fields") or {}),
                "missing_fields": payload.get("missing_fields"),
            },
        )
        outcome.payload = payload
        return outcome

    # ------------------------------------------------------------------
    # Stage 3: QR checks
    # ------------------------------------------------------------------

    def _stage_qr(
        self,
        run_id: int,
        path: Path,
        fields: StageOutcome,
        actor: str,
        role: str,
    ) -> StageOutcome:
        from ..qr import scan_document

        started = time.perf_counter()

        # Runs on the file itself, so it does not depend on stage 1 or 2. When
        # stage 2 did produce a number, it is passed in for the cross-check.
        certificate_number = self._validated_value(fields, "certificate_number")
        issuing_body = self._validated_value(fields, "issuing_body")

        try:
            result = scan_document(
                path,
                config=self.configs["qr_checks"],
                certificate_number=certificate_number,
                issuing_body=issuing_body,
                compare_certificate_number=fields.payload is not None,
            )
            payload = result.to_dict()
        except Exception as exc:
            return self._record_stage(
                run_id,
                "qr_checks",
                STAGE_FAILED,
                actor,
                role,
                reason=f"unexpected error: {type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        detail = payload.get("status")
        status = {
            "ok": STAGE_OK,
            # No QR is a normal outcome, not a degraded run.
            "no_qr_found": STAGE_OK,
            "decode_error": STAGE_FAILED,
        }.get(str(detail), STAGE_DEGRADED)

        with self.database.session() as session:
            with session.begin():
                repository.save_qr_detections(session, run_id, payload)
                repository.save_findings(
                    session,
                    run_id,
                    detector=str(
                        get(self.persistence, "pipeline.detector_name", "certificate_authenticity")
                    ),
                    findings=payload.get("findings") or [],
                    actor=actor,
                    role=role,
                    config=self.persistence,
                )

        outcome = self._record_stage(
            run_id,
            "qr_checks",
            status,
            actor,
            role,
            detail_status=str(detail) if detail else None,
            reason=payload.get("error_detail"),
            duration_ms=(time.perf_counter() - started) * 1000,
            details={
                "detection_count": len(payload.get("detections") or []),
                "finding_count": len(payload.get("findings") or []),
            },
        )
        outcome.payload = payload
        return outcome

    # ------------------------------------------------------------------
    # Stage 4: issuer verification
    # ------------------------------------------------------------------

    def _stage_issuer(
        self,
        run_id: int,
        fields: StageOutcome,
        qr: StageOutcome,
        actor: str,
        role: str,
        summary: RunSummary,
    ) -> StageOutcome:
        from ..issuer import qr_id_from_scan, submitted_from_extraction
        from ..issuer.verifier import verify_certificate

        started = time.perf_counter()

        submitted = (
            submitted_from_extraction(fields.payload)
            if fields.payload
            else {
                "certificate_number": None,
                "institution_name": None,
                "issuing_body": None,
                "programmes": None,
                "grade": None,
                "valid_from": None,
                "valid_to": None,
            }
        )
        qr_certificate_id = qr_id_from_scan(qr.payload) if qr.payload else None

        if submitted.get("certificate_number") is None and qr_certificate_id is None:
            return self._record_stage(
                run_id,
                "issuer_verification",
                STAGE_SKIPPED,
                actor,
                role,
                reason=(
                    "no certificate id was available from the document text or "
                    "from a QR code, so there was nothing to look up"
                ),
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            result = verify_certificate(
                submitted,
                qr_certificate_id=qr_certificate_id,
                config=self.configs["issuer_verification"],
                transport=self.issuer_transport,
                sleep=self.issuer_sleep,
                as_of=self.as_of,
                registry_version=None,
                registry_sha256=self.registry_hash,
            )
            payload = result.to_dict()
        except Exception as exc:
            return self._record_stage(
                run_id,
                "issuer_verification",
                STAGE_FAILED,
                actor,
                role,
                reason=f"unexpected error: {type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        detail = str(payload.get("status"))
        summary.verification_status = detail
        # source_unavailable means the check could not run, which is a degraded
        # stage. Every other status is a conclusion the stage did reach.
        status = STAGE_DEGRADED if detail == "source_unavailable" else STAGE_OK

        with self.database.session() as session:
            with session.begin():
                repository.save_verification_lookups(session, run_id, payload)
                repository.save_findings(
                    session,
                    run_id,
                    detector=str(
                        get(self.persistence, "pipeline.detector_name", "certificate_authenticity")
                    ),
                    findings=payload.get("findings") or [],
                    actor=actor,
                    role=role,
                    config=self.persistence,
                )

        outcome = self._record_stage(
            run_id,
            "issuer_verification",
            status,
            actor,
            role,
            detail_status=detail,
            reason=None if status == STAGE_OK else "issuer source unavailable",
            duration_ms=(time.perf_counter() - started) * 1000,
            details={
                "lookup_count": len(payload.get("lookups") or []),
                "status_reasons": payload.get("status_reasons"),
                "finding_count": len(payload.get("findings") or []),
            },
        )
        outcome.payload = payload
        return outcome

    # ------------------------------------------------------------------
    # Stage 5: PDF signature integrity
    # ------------------------------------------------------------------

    def _stage_signature(
        self,
        run_id: int,
        path: Path,
        document_type: str,
        fields: StageOutcome,
        actor: str,
        role: str,
        summary: RunSummary,
    ) -> StageOutcome:
        """Was the file modified after signing?

        Runs on the file itself, so a failed extraction does not stop it. The
        step 2 issuing body is passed in only to decide whether an *unsigned*
        document should be flagged, which is off unless config says otherwise.
        """
        from ..signatures import check_signatures

        started = time.perf_counter()

        try:
            result = check_signatures(
                path,
                config=self.configs["signature_checks"],
                document_type=document_type,
                issuing_body=self._validated_value(fields, "issuing_body"),
            )
            payload = result.to_dict()
        except Exception as exc:
            return self._record_stage(
                run_id,
                "signature_check",
                STAGE_FAILED,
                actor,
                role,
                reason=f"unexpected error: {type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        detail = str(payload.get("status"))
        summary.signature_status = detail

        # An unsigned document is a normal, successful result: most EoA
        # attachments are scans and were never signed. "Unverifiable" is the
        # degraded case - a signature exists that we could not judge.
        status = {
            "signature_absent": STAGE_OK,
            "present_and_byte_range_intact": STAGE_OK,
            "modified_after_signing": STAGE_OK,
            "unverifiable": STAGE_DEGRADED,
        }.get(detail, STAGE_DEGRADED)
        if payload.get("error") == "not_a_pdf":
            # An image attachment cannot carry a PDF signature. Skipped rather
            # than failed: nothing went wrong, there was nothing to look at.
            status = STAGE_SKIPPED

        with self.database.session() as session:
            with session.begin():
                repository.save_signature_details(session, run_id, payload)
                repository.save_findings(
                    session,
                    run_id,
                    detector=str(
                        get(
                            self.persistence,
                            "pipeline.detector_name",
                            "certificate_authenticity",
                        )
                    ),
                    findings=payload.get("findings") or [],
                    actor=actor,
                    role=role,
                    config=self.persistence,
                )

        outcome = self._record_stage(
            run_id,
            "signature_check",
            status,
            actor,
            role,
            detail_status=detail,
            reason=payload.get("error_detail"),
            duration_ms=(time.perf_counter() - started) * 1000,
            details={
                "signature_count": len(payload.get("signatures") or []),
                "finding_count": len(payload.get("findings") or []),
                # Recorded in the audit trail too, because "we did not check
                # who signed it" is the single most misreadable thing about
                # this stage.
                "signer_identity_verified": False,
            },
        )
        outcome.payload = payload
        return outcome

    # ------------------------------------------------------------------
    # Stage 6: cross-application duplicate detection
    # ------------------------------------------------------------------

    def _stage_duplicate(
        self,
        run_id: int,
        document_id: int,
        fields: StageOutcome,
        qr: StageOutcome,
        actor: str,
        role: str,
        summary: RunSummary,
    ) -> StageOutcome:
        """Does this document already exist elsewhere in the corpus?

        Runs last because it wants the validated certificate number from stage
        2 and the QR payloads from stage 3 - but it does not *require* them: the
        file hash and the perceptual hash work regardless, and a comparison it
        cannot make is recorded in `comparisons_skipped` rather than omitted.
        """
        from ..duplicates import cross_references_for_document, detect_duplicates

        started = time.perf_counter()

        certificate_number = self._validated_value(fields, "certificate_number")
        qr_ids: list[str] = []
        qr_payloads: list[str] = []
        for detection in (qr.payload or {}).get("detections", []) or []:
            if detection.get("certificate_id"):
                qr_ids.append(str(detection["certificate_id"]))
            if detection.get("raw_text"):
                qr_payloads.append(str(detection["raw_text"]))

        detector_name = str(
            get(
                self.persistence,
                "pipeline.duplicate_detector_name",
                "duplicate_detection",
            )
        )

        try:
            with self.database.session() as session:
                with session.begin():
                    result = detect_duplicates(
                        session,
                        document_id=document_id,
                        run_id=run_id,
                        config=self.configs["duplicate_checks"],
                        certificate_number=certificate_number,
                        qr_certificate_ids=qr_ids,
                        qr_raw_payloads=qr_payloads,
                    )
                    payload = result.to_dict()
                    repository.save_findings(
                        session,
                        run_id,
                        detector=detector_name,
                        findings=payload.get("findings") or [],
                        actor=actor,
                        role=role,
                        config=self.persistence,
                    )
                    relationships = cross_references_for_document(
                        session, document_id
                    )
                    repository.record_cross_reference_audit(
                        session,
                        run_id=run_id,
                        document_id=document_id,
                        relationships=relationships,
                        actor=actor,
                        role=role,
                        config=self.persistence,
                    )
        except Exception as exc:
            return self._record_stage(
                run_id,
                "duplicate_check",
                STAGE_FAILED,
                actor,
                role,
                reason=f"unexpected error: {type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        summary.cross_reference_count = len(relationships)
        summary.cross_reference_kinds = sorted(
            {str(entry.get("kind")) for entry in relationships}
        )
        summary.duplicate_of_document_ids = sorted(
            {
                int(entry["other_document_id"])
                for entry in relationships
                if entry.get("kind") == "duplicate_file_exact"
            }
        )

        detail = str(payload.get("status"))
        status = {
            "ok": STAGE_OK,
            # The first document into an empty corpus has nothing to be a
            # duplicate of. A successful result, not a degraded one.
            "no_corpus": STAGE_OK,
            # The hash-, number- and QR-based checks still ran; only the image
            # comparison could not.
            "image_unavailable": STAGE_DEGRADED,
        }.get(detail, STAGE_DEGRADED)

        outcome = self._record_stage(
            run_id,
            "duplicate_check",
            status,
            actor,
            role,
            detail_status=detail,
            reason=(
                None
                if status == STAGE_OK
                else "; ".join(
                    str(entry.get("reason"))
                    for entry in payload.get("comparisons_skipped") or []
                )
                or None
            ),
            duration_ms=(time.perf_counter() - started) * 1000,
            details={
                "documents_compared": (payload.get("audit") or {}).get(
                    "documents_compared"
                ),
                "match_count": len(payload.get("matches") or []),
                "finding_count": len(payload.get("findings") or []),
                "cross_reference_count": len(relationships),
                "skipped_count": len(payload.get("comparisons_skipped") or []),
            },
        )
        outcome.payload = payload
        return outcome

    # ------------------------------------------------------------------
    # Faculty: declared vs website vs AICTE records
    # ------------------------------------------------------------------

    def _application_context(self, application_id: int) -> tuple[int, str]:
        from ..db.models import Application

        with self.database.session() as session:
            application = session.get(Application, application_id)
            if application is None:
                raise ValueError(f"no application with id {application_id}")
            return application.institution_id, application.institution.name

    def _stage_faculty(
        self,
        run_id: int,
        document_id: int,
        application_id: int,
        path: Path,
        actor: str,
        role: str,
        summary: RunSummary,
    ) -> StageOutcome:
        from ..faculty import check_faculty, normalise_person_name

        started = time.perf_counter()
        cfg = self.configs["faculty_checks"]
        detector = str(get(self.persistence, "pipeline.faculty_detector_name", "faculty_consistency"))
        try:
            institution_id, institution_name = self._application_context(application_id)
            with self.database.session() as session:
                others = repository.faculty_declared_elsewhere(session, institution_id)
                from ..db.models import Document

                document = session.get(Document, document_id)
                declared_name = document.original_filename if document else None
                declared_on = document.created_at.date() if document and document.created_at else None
            result = check_faculty(
                path,
                institution_name=institution_name,
                institution_id=institution_id,
                config=cfg,
                other_declarations=others,
                as_of=self.as_of or get(cfg, "sources.as_of"),
                declared_on=declared_on,
                declared_name=declared_name,
            )
            payload = result.to_dict()
        except Exception as exc:
            return self._record_stage(
                run_id,
                "faculty_consistency",
                STAGE_FAILED,
                actor,
                role,
                reason=f"unexpected error: {type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        with self.database.session() as session:
            with session.begin():
                repository.save_faculty_members(
                    session,
                    document_id=document_id,
                    run_id=run_id,
                    institution_id=institution_id,
                    people=result.declared_people,
                    normalise=lambda name: normalise_person_name(name, cfg),
                )
                repository.save_detector_detail(session, run_id, detector, "faculty_comparison", payload)
                repository.save_findings(
                    session,
                    run_id,
                    detector=detector,
                    findings=payload.get("findings") or [],
                    actor=actor,
                    role=role,
                    config=self.persistence,
                )

        summary.verification_status = payload.get("status")
        unavailable = [
            name for name, source in (payload.get("sources") or {}).items() if not source.get("available")
        ]
        if not (payload.get("sources") or {}).get("declared", {}).get("available"):
            status = STAGE_FAILED
        elif unavailable:
            status = STAGE_DEGRADED
        else:
            status = STAGE_OK
        outcome = self._record_stage(
            run_id,
            "faculty_consistency",
            status,
            actor,
            role,
            detail_status=str(payload.get("status")),
            reason=("sources unavailable: " + ", ".join(unavailable)) if unavailable else None,
            duration_ms=(time.perf_counter() - started) * 1000,
            details={
                "department_count": len(payload.get("departments") or []),
                "finding_count": len(payload.get("findings") or []),
                "aicte_record_id": (payload.get("audit") or {}).get("aicte_record_id"),
                "records_version": (payload.get("audit") or {}).get("records_version"),
            },
        )
        outcome.payload = payload
        return outcome

    # ------------------------------------------------------------------
    # Photos: reuse, EXIF, manipulation, synthetic hint, claim
    # ------------------------------------------------------------------

    def _stages_photo(
        self,
        run_id: int,
        document_id: int,
        application_id: int,
        path: Path,
        actor: str,
        role: str,
        summary: RunSummary,
    ) -> list[StageOutcome]:
        from ..core.verification import most_severe
        from ..db.models import Document
        from ..db.storage import store_root
        from ..faculty import sources as faculty_sources
        from ..photos import (
            PhotoLoadError,
            check_claim,
            check_manipulation,
            check_metadata,
            check_reuse,
            check_synthetic,
            load_photo,
        )

        cfg = self.configs["photo_checks"]
        detector = str(get(self.persistence, "pipeline.photo_detector_name", "photo_authenticity"))
        outcomes: list[StageOutcome] = []
        analysis: dict[str, Any] = {"config_version": cfg.get("config_version")}

        started = time.perf_counter()
        try:
            photo = load_photo(path)
        except PhotoLoadError as exc:
            outcomes.append(
                self._record_stage(
                    run_id,
                    "photo_metadata",
                    STAGE_FAILED,
                    actor,
                    role,
                    reason=str(exc),
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            )
            for stage in ("photo_reuse", "photo_manipulation", "photo_synthetic_hint", "photo_claim_consistency"):
                outcomes.append(
                    self._record_stage(run_id, stage, STAGE_SKIPPED, actor, role, reason="the image could not be opened")
                )
            summary.verification_status = "manual_review_required"
            return outcomes

        with self.database.session() as session:
            document = session.get(Document, document_id)
            uploaded_at = document.created_at if document else None
            claim = document.claim if document else None
        if uploaded_at is not None and uploaded_at.tzinfo is None:
            from datetime import timezone

            uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)

        institution_id, institution_name = self._application_context(application_id)
        location = None
        records, _ = faculty_sources.load_records(self.configs["faculty_checks"])
        if records:
            record, _match = faculty_sources.find_institution_record(
                records, institution_name, self.configs["faculty_checks"]
            )
            location = (record or {}).get("location")

        all_findings: list[dict[str, Any]] = []

        def run_stage(name: str, fn: Any) -> None:
            stage_started = time.perf_counter()
            try:
                detail, findings, detail_status = fn()
            except Exception as exc:
                outcomes.append(
                    self._record_stage(
                        run_id,
                        name,
                        STAGE_FAILED,
                        actor,
                        role,
                        reason=f"unexpected error: {type(exc).__name__}: {exc}",
                        duration_ms=(time.perf_counter() - stage_started) * 1000,
                    )
                )
                return
            analysis[name] = detail
            all_findings.extend(findings)
            with self.database.session() as session:
                with session.begin():
                    repository.save_findings(
                        session,
                        run_id,
                        detector=detector,
                        findings=findings,
                        actor=actor,
                        role=role,
                        config=self.persistence,
                    )
            stage_status = STAGE_OK
            if detail_status == "source_unavailable":
                stage_status = STAGE_SKIPPED
            outcomes.append(
                self._record_stage(
                    run_id,
                    name,
                    stage_status,
                    actor,
                    role,
                    detail_status=detail_status,
                    reason=(detail or {}).get("reason") if stage_status != STAGE_OK else None,
                    duration_ms=(time.perf_counter() - stage_started) * 1000,
                    details={"finding_count": len(findings)},
                )
            )

        meta_holder: dict[str, Any] = {}

        def metadata_stage():
            detail, findings = check_metadata(photo, cfg, uploaded_at=uploaded_at, institution_location=location)
            detail["institution_location"] = location
            meta_holder["meta"] = detail.get("metadata")
            return detail, findings, None

        def reuse_stage():
            with self.database.session() as session:
                with session.begin():
                    detail, findings = check_reuse(session, document_id, run_id, photo, cfg)
            return detail, findings, None

        def manipulation_stage():
            heatmap = store_root(self.persistence) / "derived" / f"{summary.file_sha256}_ela.png"
            detail, findings = check_manipulation(photo, cfg, heatmap_path=heatmap)
            return detail, findings, None

        def synthetic_stage():
            detail, findings = check_synthetic(photo, meta_holder.get("meta"), cfg)
            return detail, findings, None

        def claim_stage():
            detail, findings, status = check_claim(photo, claim, cfg)
            return detail, findings, status

        run_stage("photo_metadata", metadata_stage)
        run_stage("photo_reuse", reuse_stage)
        run_stage("photo_manipulation", manipulation_stage)
        run_stage("photo_synthetic_hint", synthetic_stage)
        run_stage("photo_claim_consistency", claim_stage)

        # A status only where a check reached a conclusion about a mismatch.
        # No findings is NOT "verified": nothing was confirmed against a source.
        statuses = []
        for finding in all_findings:
            if finding["type"] in ("photo_reused_across_submissions", "photo_gps_far_from_institution", "photo_capture_date_after_upload"):
                statuses.append("mismatch_detected")
            else:
                statuses.append("manual_review_required")
        severe = most_severe(statuses)
        summary.verification_status = severe.value if severe else None
        analysis["verification_status"] = summary.verification_status
        analysis["claim"] = claim

        with self.database.session() as session:
            with session.begin():
                repository.save_detector_detail(session, run_id, detector, "photo_analysis", analysis)
        return outcomes

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _record_stage(
        self,
        run_id: int,
        stage: str,
        status: str,
        actor: str,
        role: str,
        detail_status: str | None = None,
        reason: str | None = None,
        duration_ms: float = 0.0,
        details: dict[str, Any] | None = None,
    ) -> StageOutcome:
        with self.database.session() as session:
            with session.begin():
                repository.record_stage_result(
                    session,
                    run_id=run_id,
                    stage=stage,
                    status=status,
                    actor=actor,
                    role=role,
                    detail_status=detail_status,
                    reason=reason,
                    duration_ms=duration_ms,
                    details=details,
                    config=self.persistence,
                )
        outcome = StageOutcome(
            stage=stage,
            status=status,
            detail_status=detail_status,
            reason=reason,
            duration_ms=duration_ms,
        )
        if self.on_stage is not None:
            try:
                self.on_stage(run_id, {**outcome.to_dict(), **(details or {})})
            except Exception:
                # A listener's failure must never fail a check.
                pass
        return outcome

    def _usable_pages(self, extraction: StageOutcome) -> dict[int, str]:
        if not extraction.payload:
            return {}
        pages: dict[int, str] = {}
        for page in extraction.payload.get("pages", []) or []:
            text = page.get("text")
            if text and str(text).strip():
                pages[int(page.get("page_number", 0))] = str(text)
        return pages

    def _validated_value(self, fields: StageOutcome, name: str) -> Any:
        if not fields.payload:
            return None
        entry = (fields.payload.get("fields") or {}).get(name)
        if not isinstance(entry, dict) or entry.get("validated") is not True:
            return None
        return entry.get("value")

    def _provider_name(self, fields: StageOutcome) -> str | None:
        return ((fields.payload or {}).get("audit") or {}).get("provider")

    def _model_name(self, fields: StageOutcome) -> str | None:
        return ((fields.payload or {}).get("audit") or {}).get("model")

    def _prompt_version(self, fields: StageOutcome) -> str | None:
        return ((fields.payload or {}).get("audit") or {}).get("prompt_version")

    def _roll_up(self, stages: list[StageOutcome]) -> str:
        """The run's own status: did the checks run, not what they concluded.

        Stages listed in `pipeline.advisory_stages` (claim-vs-image, which
        needs a VLM that may not be configured) do not count against it.
        """
        advisory = set(get(self.persistence, "pipeline.advisory_stages", []) or [])
        statuses = {stage.status for stage in stages if stage.stage not in advisory}
        if STAGE_FAILED in statuses or STAGE_DEGRADED in statuses or STAGE_SKIPPED in statuses:
            return "completed_with_failures"
        return "completed"


def ensure_application(
    database: Database,
    institution_name: str,
    category: str,
    actor: str,
    role: str,
    config: dict[str, Any] | None = None,
) -> int:
    """Create the institution (if new) and an application for it."""
    cfg = config if config is not None else load_config("persistence")
    with database.session() as session:
        with session.begin():
            institution = repository.get_or_create_institution(
                session, institution_name, actor=actor, role=role, config=cfg
            )
            application = repository.create_application(
                session,
                institution_id=institution.id,
                category=category,
                actor=actor,
                role=role,
                config=cfg,
            )
        return application.id
