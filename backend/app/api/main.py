"""The reviewer-facing REST API.

    python -m app.api          # serves on 127.0.0.1:8765

Read paths return what the pipeline stored; write paths go through the same
repository functions as the CLIs, so every change lands with its audit entry.
Three things this API never does:

- **decide.** There is no approve/reject endpoint. The risk level orders the
  queue; a human acts on findings.
- **fetch a URL from a document.** Uploads are processed by the pipeline,
  which never contacts a URL found in a submission.
- **serve a document without logging it.** Every report read and file view is
  an audit entry with the caller's actor and role.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from ..core.config import config_dir, get, load_config
from ..core.verification import VerificationStatus
from ..db import audit_log, repository
from ..db.models import (
    Application,
    ApplicationCheck,
    AuditLog,
    CheckRun,
    DetectorDetail,
    Document,
    FindingRow,
    Institution,
    ReviewAction,
    StageResult,
)
from ..explain import explain
from ..pipeline.runner import ensure_application
from ..scoring import assess_application, assessment_history, latest_assessment, score_findings
from ..scoring.risk import latest_findings_for_application
from ..db.roles import UnknownRoleError, validate_role
from .service import Identity, Service

DETECTOR_LABELS = {
    "certificate_authenticity": "Certificate authenticity",
    "duplicate_detection": "Cross-application duplicates",
    "faculty_consistency": "Faculty consistency",
    "photo_authenticity": "Photo authenticity",
    "application_validation": "Application validation",
    "regulatory_compliance": "Regulatory compliance",
}


class ReviewBody(BaseModel):
    action: str
    note: str | None = None


class SignInBody(BaseModel):
    actor: str
    role: str
    passcode: str


def create_app(service: Service | None = None) -> FastAPI:
    svc = service if service is not None else Service()
    app = FastAPI(
        title="AICTE Fraud-Signal Detection API (SIH 2026 prototype)",
        description=(
            "Decision support only. Surfaces possible indicators with evidence "
            "for a human reviewer. Never approves, rejects or concludes. All "
            "data is synthetic."
        ),
        version=str(get(svc.config, "config_version", "api")),
    )
    app.state.service = svc
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(o) for o in (get(svc.config, "cors_origins", []) or [])],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def who(request: Request) -> Identity:
        return svc.identity(request)

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with svc.database.session() as session:
            chain = audit_log.verify_chain(session)
        return {
            "status": "ok",
            "schema_version": svc.database.schema_version(),
            "audit_chain_ok": chain.ok,
            "synthetic_data_only": True,
        }

    @app.get("/api/meta")
    def meta(identity: Identity = Depends(who)) -> dict[str, Any]:
        ext = load_config("certificate_extraction")
        provider = str(get(ext, "llm.provider"))
        import os

        key_env = str(get(ext, f"llm.providers.{provider}.api_key_env", ""))
        active = provider if (key_env and os.environ.get(key_env)) else get(ext, "llm.fallback_provider")
        return {
            "identity": {"actor": identity.actor, "role": identity.role},
            "roles": [r for r in (get(svc.persistence, "roles.allowed", []) or []) if r != "system"],
            "sign_in_required": sorted(svc.sessions.passcodes),
            "permissions": get(svc.config, "permissions", {}),
            "review_actions": get(svc.persistence, "review.actions", {}),
            "review_statuses": get(svc.persistence, "review.statuses", []),
            "verification_statuses": [s.value for s in VerificationStatus],
            "document_types": get(svc.config, "document_types", []),
            "detectors": DETECTOR_LABELS,
            "extraction_provider": {"configured": provider, "active": active},
            "as_of": svc.pipeline.as_of,
        }

    @app.post("/api/session")
    def sign_in(body: SignInBody) -> dict[str, Any]:
        actor = body.actor.strip()[:128]
        try:
            role = validate_role(body.role, svc.persistence)
        except UnknownRoleError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not svc.sessions.requires_sign_in(role):
            raise HTTPException(400, f"the {role} role needs no sign-in")
        if not actor:
            raise HTTPException(400, "a name is required to sign in")

        def refuse(status: int, detail: str, reason: str) -> HTTPException:
            # The attempt is recorded; the passcode never is.
            with svc.database.session() as session:
                audit_log.append(
                    session, actor=actor, role=role, action="sign_in_refused",
                    entity_type="session", entity_id=None, details={"reason": reason},
                )
                session.commit()
            return HTTPException(status, detail)

        if svc.sessions.locked_out(role):
            raise refuse(429, "too many refused attempts for this role; wait a few minutes", "too_many_attempts")
        if not svc.sessions.check_passcode(role, body.passcode):
            raise refuse(401, "passcode not accepted", "passcode_not_accepted")
        granted = svc.sessions.issue(actor, role)
        with svc.database.session() as session:
            audit_log.append(
                session, actor=actor, role=role, action="signed_in",
                entity_type="session", entity_id=None, details={"expires_at": granted.expires_at},
            )
            session.commit()
        return {"token": svc.sessions.encode(granted), "actor": actor, "role": role, "expires_at": granted.expires_at}

    @app.post("/api/session/end")
    def sign_out(identity: Identity = Depends(who)) -> dict[str, Any]:
        with svc.database.session() as session:
            audit_log.append(
                session, actor=identity.actor, role=identity.role, action="signed_out",
                entity_type="session", entity_id=None, details=None,
            )
            session.commit()
        return {"ok": True}

    @app.get("/api/stats")
    def stats(identity: Identity = Depends(who)) -> dict[str, Any]:
        with svc.database.session() as session:
            applications = session.scalars(select(Application)).all()
            levels = {"high": 0, "medium": 0, "low": 0, "unscored": 0}
            # Current findings only (each document's latest run, each
            # application's latest check): a re-run must not double the counts.
            by_detector: dict[str, dict[str, int]] = {}
            review_counts: dict[str, int] = {}
            type_counts: dict[tuple[str, str], int] = {}
            for application in applications:
                assessment = latest_assessment(session, application.id)
                levels[assessment.level if assessment else "unscored"] += 1
                for finding in latest_findings_for_application(session, application.id):
                    review_counts[finding["review_status"]] = review_counts.get(finding["review_status"], 0) + 1
                    if finding["review_status"] != "open":
                        continue
                    by_detector.setdefault(finding["detector"], {"strong": 0, "medium": 0, "weak": 0})[finding["strength"]] += 1
                    if finding["strength"] != "weak":
                        key = (finding["type"], finding["strength"])
                        type_counts[key] = type_counts.get(key, 0) + 1
            top_types = sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
            durations = session.execute(
                select(StageResult.run_id, func.sum(StageResult.duration_ms)).group_by(StageResult.run_id)
            ).all()
            chain = audit_log.verify_chain(session)
            return {
                "top_open_types": [{"type": t, "strength": st, "count": c} for (t, st), c in top_types],
                "applications": len(applications),
                "documents": session.scalar(select(func.count()).select_from(Document)),
                "check_runs": session.scalar(select(func.count()).select_from(CheckRun)),
                "risk_levels": levels,
                "open_findings_by_detector": by_detector,
                "findings_by_review_status": review_counts,
                "review_actions": session.scalar(select(func.count()).select_from(ReviewAction)),
                "avg_run_ms": round(sum(d or 0 for _, d in durations) / len(durations), 1) if durations else None,
                "audit_entries": chain.entries_checked,
                "audit_chain_ok": chain.ok,
            }

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------

    @app.get("/api/applications")
    def list_applications(identity: Identity = Depends(who)) -> list[dict[str, Any]]:
        with svc.database.session() as session:
            rows = []
            for application in session.scalars(select(Application).order_by(Application.id.desc())).all():
                rows.append(_application_summary(session, application))
        order = {"high": 0, "medium": 1, "low": 2, None: 3}
        rows.sort(key=lambda r: (order.get(r["risk"]["level"] if r["risk"] else None, 3), -(r["risk"]["score"] if r["risk"] else 0), -r["id"]))
        return rows

    @app.post("/api/applications")
    async def submit_application(
        request: Request,
        institution_name: str = Form(...),
        category: str = Form("extension_of_approval"),
        files: list[UploadFile] = File(...),
        document_types: list[str] = Form(...),
        claims: list[str] | None = Form(None),
        identity: Identity = Depends(who),
    ) -> dict[str, Any]:
        svc.require("submit_application", identity)
        if len(files) != len(document_types):
            raise HTTPException(400, "each file needs exactly one document type")
        if not institution_name.strip():
            raise HTTPException(400, "institution_name is required")
        allowed_types = {d["value"] for d in get(svc.config, "document_types", []) or []}
        for doc_type in document_types:
            if doc_type not in allowed_types:
                raise HTTPException(400, f"unknown document type {doc_type!r}")
        claims = list(claims or [])
        with tempfile.TemporaryDirectory() as scratch:
            paths = [await _save_upload(upload, Path(scratch), svc) for upload in files]
            return _process(
                svc,
                identity,
                institution_name.strip(),
                category,
                [
                    {"path": path, "document_type": doc_type, "claim": (claims[i] if i < len(claims) and claims[i] else None)}
                    for i, (path, doc_type) in enumerate(zip(paths, document_types))
                ],
            )

    @app.get("/api/applications/{application_id}/report")
    def application_report(application_id: int, all_runs: bool = False, identity: Identity = Depends(who)) -> dict[str, Any]:
        with svc.database.session() as session:
            try:
                report = repository.get_application_report(
                    session, application_id, all_runs=all_runs, actor=identity.actor, role=identity.role
                )
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            findings = latest_findings_for_application(session, application_id)
            risk = _current_risk(session, application_id)
            report["risk"] = risk
            report["risk_history"] = assessment_history(session, application_id)
            report["explanation"] = explain(risk, findings)
            report["document_summaries"] = [
                _document_summary(svc, document) for document in report["documents"]
            ]
            return report

    @app.post("/api/applications/{application_id}/rerun")
    def rerun_application(application_id: int, identity: Identity = Depends(who)) -> dict[str, Any]:
        svc.require("rerun", identity)
        with svc.database.session() as session:
            application = session.get(Application, application_id)
            if application is None:
                raise HTTPException(404, f"no application with id {application_id}")
            document_ids = [d.id for d in sorted(application.documents, key=lambda d: d.id)]
        started = time.perf_counter()
        runs = []
        with svc.lock:
            for document_id in document_ids:
                try:
                    summary = svc.pipeline.rerun_document(document_id, identity.actor, identity.role)
                    runs.append(summary.to_dict())
                except ValueError as exc:
                    runs.append({"document_id": document_id, "error": str(exc)})
            svc.pipeline.check_application(application_id, identity.actor, identity.role)
            with svc.database.session() as session:
                with session.begin():
                    risk = assess_application(session, application_id, trigger="rerun", actor=identity.actor, role=identity.role, persistence=svc.persistence)
        return {"application_id": application_id, "runs": runs, "risk": risk, "seconds": round(time.perf_counter() - started, 2)}

    @app.get("/api/applications/{application_id}/audit")
    def application_audit(application_id: int, identity: Identity = Depends(who)) -> list[dict[str, Any]]:
        svc.require("view_audit_log", identity)
        with svc.database.session() as session:
            application = session.get(Application, application_id)
            if application is None:
                raise HTTPException(404, f"no application with id {application_id}")
            document_ids = [str(d.id) for d in application.documents]
            run_ids = [r.id for d in application.documents for r in d.check_runs]
            check_ids = list(
                session.scalars(select(ApplicationCheck.id).where(ApplicationCheck.application_id == application_id)).all()
            )
            finding_ids = [
                str(i)
                for i in session.scalars(
                    select(FindingRow.id).where(
                        FindingRow.run_id.in_(run_ids) | FindingRow.application_check_id.in_(check_ids)
                    )
                ).all()
            ]
            rows = session.scalars(
                select(AuditLog)
                .where(
                    ((AuditLog.entity_type == "application") & (AuditLog.entity_id == str(application_id)))
                    | ((AuditLog.entity_type == "document") & (AuditLog.entity_id.in_(document_ids)))
                    | ((AuditLog.entity_type == "check_run") & (AuditLog.entity_id.in_([str(i) for i in run_ids])))
                    | ((AuditLog.entity_type == "application_check") & (AuditLog.entity_id.in_([str(i) for i in check_ids])))
                    | ((AuditLog.entity_type == "finding") & (AuditLog.entity_id.in_(finding_ids)))
                )
                .order_by(AuditLog.id)
            ).all()
            return [_audit_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Documents: files and evidence images
    # ------------------------------------------------------------------

    @app.get("/api/documents/{document_id}/file")
    def document_file(document_id: int, identity: Identity = Depends(who)) -> FileResponse:
        document = _document_or_404(svc, document_id)
        _log_view(svc, identity, document_id, "document_file_viewed")
        return FileResponse(document.stored_path, filename=document.original_filename)

    @app.get("/api/documents/{document_id}/preview.png")
    def document_preview(document_id: int, page: int = 1, width: int = Query(900, le=2000), identity: Identity = Depends(who)) -> Response:
        document = _document_or_404(svc, document_id)
        path = Path(document.stored_path)
        try:
            image = _render_preview(path, page, width)
        except Exception as exc:
            raise HTTPException(422, f"preview unavailable: {type(exc).__name__}") from exc
        _log_view(svc, identity, document_id, "document_preview_viewed")
        # Not cached: a view served from the browser cache would skip the audit entry.
        return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/documents/{document_id}/ela.png")
    def document_ela(document_id: int, identity: Identity = Depends(who)) -> FileResponse:
        from ..db.storage import store_root

        document = _document_or_404(svc, document_id)
        heatmap = store_root(svc.persistence) / "derived" / f"{document.file_sha256}_ela.png"
        if not heatmap.exists():
            raise HTTPException(404, "no ELA heatmap for this document (ELA runs on JPEG photos only)")
        _log_view(svc, identity, document_id, "ela_heatmap_viewed")
        return FileResponse(heatmap, media_type="image/png", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------
    # Findings and review
    # ------------------------------------------------------------------

    @app.get("/api/findings")
    def findings_queue(
        status: str | None = None,
        detector: str | None = None,
        strength: str | None = None,
        application_id: int | None = None,
        identity: Identity = Depends(who),
    ) -> list[dict[str, Any]]:
        with svc.database.session() as session:
            latest = (
                select(CheckRun.document_id, func.max(CheckRun.id).label("run_id"))
                .group_by(CheckRun.document_id)
                .subquery()
            )
            query = (
                select(FindingRow, Document, Application, Institution)
                .join(latest, FindingRow.run_id == latest.c.run_id)
                .join(Document, Document.id == latest.c.document_id)
                .join(Application, Application.id == Document.application_id)
                .join(Institution, Institution.id == Application.institution_id)
            )
            if status:
                query = query.where(FindingRow.review_status == status)
            if detector:
                query = query.where(FindingRow.detector == detector)
            if strength:
                query = query.where(FindingRow.strength == strength)
            if application_id:
                query = query.where(Application.id == application_id)
            rank = {"strong": 0, "medium": 1, "weak": 2}
            rows = []
            # Application-level findings, from each application's latest check.
            latest_check = (
                select(ApplicationCheck.application_id, func.max(ApplicationCheck.id).label("check_id"))
                .group_by(ApplicationCheck.application_id)
                .subquery()
            )
            app_query = (
                select(FindingRow, Application, Institution)
                .join(latest_check, FindingRow.application_check_id == latest_check.c.check_id)
                .join(Application, Application.id == latest_check.c.application_id)
                .join(Institution, Institution.id == Application.institution_id)
            )
            if status:
                app_query = app_query.where(FindingRow.review_status == status)
            if detector:
                app_query = app_query.where(FindingRow.detector == detector)
            if strength:
                app_query = app_query.where(FindingRow.strength == strength)
            if application_id:
                app_query = app_query.where(Application.id == application_id)
            for finding, application, institution in session.execute(app_query).all():
                rows.append(
                    {
                        **repository._finding_dict(finding),
                        "document_id": None,
                        "document_type": None,
                        "original_filename": "whole application",
                        "application_id": application.id,
                        "institution": institution.name,
                    }
                )
            for finding, document, application, institution in session.execute(query).all():
                rows.append(
                    {
                        **repository._finding_dict(finding),
                        "document_id": document.id,
                        "document_type": document.document_type,
                        "original_filename": document.original_filename,
                        "application_id": application.id,
                        "institution": institution.name,
                    }
                )
            rows.sort(key=lambda r: (rank.get(r["strength"], 3), -r["id"]))
            return rows

    @app.post("/api/findings/{finding_id}/review")
    def review_finding(finding_id: int, body: ReviewBody, identity: Identity = Depends(who)) -> dict[str, Any]:
        svc.require("review_finding", identity)
        with svc.database.session() as session:
            with session.begin():
                try:
                    repository.record_review_action(
                        session, finding_id, body.action, identity.actor, identity.role, note=body.note, config=svc.persistence
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                finding = session.get(FindingRow, finding_id)
                application_id = (
                    finding.run.document.application_id
                    if finding.run is not None
                    else finding.application_check.application_id
                )
                risk = assess_application(session, application_id, trigger="review_action", actor=identity.actor, role=identity.role, persistence=svc.persistence)
                result = {"ok": True, "finding": repository._finding_dict(finding), "risk": risk}
        return result

    # ------------------------------------------------------------------
    # Audit, config, evaluation, demo samples
    # ------------------------------------------------------------------

    @app.get("/api/audit")
    def audit(limit: int = Query(200, le=2000), offset: int = 0, action: str | None = None, identity: Identity = Depends(who)) -> dict[str, Any]:
        svc.require("view_audit_log", identity)
        with svc.database.session() as session:
            query = select(AuditLog)
            if action:
                query = query.where(AuditLog.action == action)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(query.order_by(AuditLog.id.desc()).offset(offset).limit(limit)).all()
            audit_log.append(
                session, actor=identity.actor, role=identity.role, action="audit_log_viewed",
                entity_type="audit_log", entity_id=None, details={"limit": limit, "offset": offset, "action": action},
            )
            session.commit()
            return {"total": total, "entries": [_audit_dict(r) for r in rows]}

    @app.get("/api/audit/verify")
    def audit_verify(identity: Identity = Depends(who)) -> dict[str, Any]:
        svc.require("verify_audit_chain", identity)
        with svc.database.session() as session:
            result = audit_log.verify_chain(session).to_dict()
            audit_log.append(
                session, actor=identity.actor, role=identity.role, action="audit_chain_verified",
                entity_type="audit_log", entity_id=None, details={"ok": result["ok"], "entries_checked": result["entries_checked"]},
            )
            session.commit()
            return result

    @app.get("/api/config")
    def configuration(identity: Identity = Depends(who)) -> list[dict[str, Any]]:
        svc.require("view_config", identity)
        files = []
        for path in sorted(config_dir().glob("*.yaml")):
            content = load_config(path.stem)
            files.append(
                {
                    "name": path.stem,
                    "config_version": content.get("config_version"),
                    "hash": repository.config_hash(content),
                    "raw": path.read_text(encoding="utf-8"),
                }
            )
        return files

    @app.get("/api/evaluation")
    def evaluation(identity: Identity = Depends(who)) -> dict[str, Any]:
        path = svc.path_from_config("demo.evaluation_path", "data/demo/evaluation.json")
        if not path.exists():
            raise HTTPException(404, "no evaluation report yet; run: python -m demo.evaluate")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/demo/samples")
    def demo_samples(identity: Identity = Depends(who)) -> list[dict[str, Any]]:
        labels = _demo_labels(svc)
        return [
            {
                "key": a["key"],
                "institution": a["institution"],
                "summary": a["summary"],
                "is_fraud": a["is_fraud"],
                "fraud_types": a["fraud_types"],
                "documents": [{"file": Path(d["file"]).name, "document_type": d["document_type"], "claim": d.get("claim")} for d in a["documents"]],
            }
            for a in labels.get("applications", [])
        ]

    @app.post("/api/demo/samples/{key}/submit")
    def submit_sample(key: str, identity: Identity = Depends(who)) -> dict[str, Any]:
        svc.require("submit_application", identity)
        labels = _demo_labels(svc)
        sample = next((a for a in labels.get("applications", []) if a["key"] == key), None)
        if sample is None:
            raise HTTPException(404, f"no demo sample {key!r}")
        data_dir = svc.path_from_config("demo.data_dir", "data/demo")
        return _process(
            svc,
            identity,
            sample["institution"],
            "extension_of_approval",
            [{"path": data_dir / d["file"], "document_type": d["document_type"], "claim": d.get("claim")} for d in sample["documents"]],
        )

    from .extras import register_extras

    register_extras(app, svc)
    return app


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _save_upload(upload: UploadFile, scratch: Path, svc: Service) -> Path:
    name = Path(upload.filename or "upload").name
    suffix = Path(name).suffix.lower()
    allowed = [str(e) for e in (get(svc.config, "uploads.allowed_extensions", []) or [])]
    if suffix not in allowed:
        raise HTTPException(400, f"file type {suffix or '(none)'} not accepted; allowed: {', '.join(allowed)}")
    limit = int(get(svc.config, "uploads.max_bytes", 25 * 1024 * 1024))
    # One sub-directory per upload: two files both named "photo.jpg" must not
    # overwrite each other and end up as two documents with the same bytes.
    slot = scratch / f"{len(list(scratch.iterdir())):03d}"
    slot.mkdir()
    target = slot / name
    size = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"{name} exceeds the {limit // (1024 * 1024)} MB upload limit")
            handle.write(chunk)
    return target


def _process(svc: Service, identity: Identity, institution: str, category: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    with svc.lock:
        application_id = ensure_application(
            svc.database, institution_name=institution, category=category, actor=identity.actor, role=identity.role, config=svc.persistence
        )
        runs = []
        for document in documents:
            summary = svc.pipeline.process_document(
                application_id=application_id,
                file_path=document["path"],
                document_type=document["document_type"],
                actor=identity.actor,
                role=identity.role,
                claim=document.get("claim"),
            )
            runs.append(summary.to_dict())
        svc.pipeline.check_application(application_id, identity.actor, identity.role)
        with svc.database.session() as session:
            with session.begin():
                risk = assess_application(session, application_id, trigger="submission", actor=identity.actor, role=identity.role, persistence=svc.persistence)
    return {"application_id": application_id, "runs": runs, "risk": risk, "seconds": round(time.perf_counter() - started, 2)}


def _current_risk(session: Any, application_id: int) -> dict[str, Any]:
    """The latest stored assessment, re-derived for its full breakdown."""
    stored = latest_assessment(session, application_id)
    live = score_findings(latest_findings_for_application(session, application_id))
    if stored is not None:
        live["assessment_id"] = stored.id
        live["stored_level"] = stored.level
        live["stored_score"] = stored.score
        live["assessed_at"] = stored.created_at.isoformat() if stored.created_at else None
        live["trigger"] = stored.trigger
    return live


def _application_summary(session: Any, application: Application) -> dict[str, Any]:
    stored = latest_assessment(session, application.id)
    findings = latest_findings_for_application(session, application.id)
    counts = {"strong": 0, "medium": 0, "weak": 0}
    open_count = 0
    for finding in findings:
        if finding["review_status"] == "open":
            open_count += 1
            if finding["strength"] in counts:
                counts[finding["strength"]] += 1
    documents = sorted(application.documents, key=lambda d: d.id)
    return {
        "id": application.id,
        "institution": application.institution.name,
        "category": application.category,
        "created_at": repository._iso(application.created_at),
        "document_count": len(documents),
        "document_types": sorted({d.document_type for d in documents}),
        "finding_count": len(findings),
        "open_findings": open_count,
        "reviewed_findings": len(findings) - open_count,
        "open_by_strength": counts,
        "risk": (
            {
                "level": stored.level,
                "score": stored.score,
                "subscores": {k: v.get("score") for k, v in (stored.subscores or {}).items()},
                "assessed_at": repository._iso(stored.created_at),
            }
            if stored
            else None
        ),
    }


def _document_summary(svc: Service, document: dict[str, Any]) -> dict[str, Any]:
    kind = svc.pipeline.document_kind(document["document_type"])
    run = (document.get("runs") or [{}])[-1]
    status = None
    for stage in run.get("stages", []) or []:
        if kind == "certificate" and stage["stage"] == "issuer_verification":
            status = stage.get("detail_status")
        if kind == "faculty" and stage["stage"] == "faculty_consistency":
            status = stage.get("detail_status")
    if kind == "photo":
        status = ((run.get("details") or {}).get("photo_analysis") or {}).get("verification_status")
    findings = run.get("findings", []) or []
    return {
        "document_id": document["id"],
        "kind": kind,
        "document_type": document["document_type"],
        "original_filename": document["original_filename"],
        "verification_status": status,
        "run_status": run.get("status"),
        "finding_count": len(findings),
        "strong": sum(1 for f in findings if f["strength"] == "strong"),
        "medium": sum(1 for f in findings if f["strength"] == "medium"),
        "weak": sum(1 for f in findings if f["strength"] == "weak"),
    }


def _document_or_404(svc: Service, document_id: int) -> Document:
    with svc.database.session() as session:
        document = session.get(Document, document_id)
        if document is None:
            raise HTTPException(404, f"no document with id {document_id}")
        return document


def _log_view(svc: Service, identity: Identity, document_id: int, action: str) -> None:
    with svc.database.session() as session:
        audit_log.append(
            session, actor=identity.actor, role=identity.role, action=action,
            entity_type="document", entity_id=document_id, details=None,
        )
        session.commit()


def _render_preview(path: Path, page: int, width: int) -> bytes:
    import io

    from PIL import Image, ImageOps

    if path.suffix.lower() == ".pdf":
        import fitz

        with fitz.open(path) as pdf:
            index = max(0, min(page - 1, pdf.page_count - 1))
            target = pdf[index]
            zoom = width / max(target.rect.width, 1)
            pixmap = target.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pixmap.tobytes("png")
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    image.thumbnail((width, width * 2))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _audit_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "timestamp": repository._iso(row.timestamp),
        "actor": row.actor,
        "role": row.role,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "details": row.details,
        "previous_hash": row.previous_hash,
        "entry_hash": row.entry_hash,
    }


def _demo_labels(svc: Service) -> dict[str, Any]:
    path = svc.path_from_config("demo.data_dir", "data/demo") / "labels.json"
    if not path.exists():
        raise HTTPException(404, "demo dataset not generated; run: python -m demo.generate")
    return json.loads(path.read_text(encoding="utf-8"))
