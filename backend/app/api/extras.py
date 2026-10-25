"""Reviewer conveniences on top of the core API.

- **Streamed submissions**: the same pipeline, reporting each stage as it is
  recorded (NDJSON), so a reviewer watches the checks run instead of a spinner.
- **Connections**: institutions linked by a shared certificate, QR payload,
  file, photo or faculty member — the cross-application picture that single
  findings scatter across many reports.
- **Search**: where else does this certificate number, faculty id or name
  appear?
- **Scrutiny note**: a PDF of one application's indicators and evidence for
  the file. A note of indicators, never a decision.

Every one of these reads stored results or runs the unchanged pipeline. None
adds a judgement of its own, and each read is audited.
"""

from __future__ import annotations

import hashlib
import json
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select

from ..core.config import get
from ..db import audit_log, repository
from ..db.models import (
    Application,
    CheckRun,
    CrossReference,
    Document,
    ExtractedField,
    FacultyMemberRow,
    FindingRow,
    Institution,
    QRDetectionRow,
)
from ..explain import explain
from ..pipeline.runner import ensure_application
from ..scoring import assess_application, latest_assessment
from ..scoring.risk import latest_findings_for_application, score_findings
from .service import Identity, Service

# Relationship kinds shown in the connections view, and how to name them.
EDGE_LABELS = {
    "duplicate_file_exact": "identical file",
    "certificate_number_reuse": "same certificate number",
    "qr_payload_reuse": "same QR payload",
    "near_duplicate_page_image": "near-identical certificate page",
    "photo_reuse": "reused photo",
    "shared_faculty": "shared faculty",
}
_RANK = {"strong": 3, "medium": 2, "weak": 1}


def register_extras(app: FastAPI, svc: Service) -> None:
    def who(request: Request) -> Identity:
        return svc.identity(request)

    # ------------------------------------------------------------------
    # Streamed submission
    # ------------------------------------------------------------------

    @app.post("/api/demo/samples/{key}/stream")
    def stream_sample(key: str, identity: Identity = Depends(who)) -> StreamingResponse:
        from .main import _demo_labels

        svc.require("submit_application", identity)
        sample = next((a for a in _demo_labels(svc).get("applications", []) if a["key"] == key), None)
        if sample is None:
            raise HTTPException(404, f"no demo sample {key!r}")
        data_dir = svc.path_from_config("demo.data_dir", "data/demo")
        documents = [
            {"path": data_dir / d["file"], "document_type": d["document_type"], "claim": d.get("claim")}
            for d in sample["documents"]
        ]
        return stream_processing(svc, identity, sample["institution"], "extension_of_approval", documents)

    @app.post("/api/applications/stream")
    async def stream_upload(
        institution_name: str = Form(...),
        category: str = Form("extension_of_approval"),
        files: list[UploadFile] = File(...),
        document_types: list[str] = Form(...),
        claims: list[str] | None = Form(None),
        identity: Identity = Depends(who),
    ) -> StreamingResponse:
        from .main import _save_upload

        svc.require("submit_application", identity)
        if len(files) != len(document_types):
            raise HTTPException(400, "each file needs exactly one document type")
        if not institution_name.strip():
            raise HTTPException(400, "institution_name is required")
        allowed = {d["value"] for d in get(svc.config, "document_types", []) or []}
        for doc_type in document_types:
            if doc_type not in allowed:
                raise HTTPException(400, f"unknown document type {doc_type!r}")
        scratch = Path(tempfile.mkdtemp(prefix="sih-upload-"))
        try:
            paths = [await _save_upload(upload, scratch, svc) for upload in files]
        except Exception:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        claims = list(claims or [])
        documents = [
            {"path": path, "document_type": t, "claim": (claims[i] if i < len(claims) and claims[i] else None)}
            for i, (path, t) in enumerate(zip(paths, document_types))
        ]
        return stream_processing(
            svc, identity, institution_name.strip(), category, documents,
            cleanup=lambda: shutil.rmtree(scratch, ignore_errors=True),
        )

    # ------------------------------------------------------------------
    # Connections between institutions
    # ------------------------------------------------------------------

    @app.get("/api/network")
    def network(identity: Identity = Depends(who)) -> dict[str, Any]:
        with svc.database.session() as session:
            return build_network(session)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @app.get("/api/search")
    def search(q: str = Query(..., min_length=2, max_length=100), identity: Identity = Depends(who)) -> dict[str, Any]:
        with svc.database.session() as session:
            results = run_search(session, q)
            # The query can be a person's name; the log keeps its hash only.
            audit_log.append(
                session, actor=identity.actor, role=identity.role, action="search_performed",
                entity_type="search", entity_id=None,
                details={"query_sha256": hashlib.sha256(q.strip().lower().encode()).hexdigest()[:16], "result_count": sum(len(v) for v in results.values())},
            )
            session.commit()
            return {"query": q, "results": results}

    # ------------------------------------------------------------------
    # Scrutiny note (PDF)
    # ------------------------------------------------------------------

    @app.get("/api/applications/{application_id}/export.pdf")
    def export_pdf(application_id: int, identity: Identity = Depends(who)) -> Response:
        from .export import build_scrutiny_note

        with svc.database.session() as session:
            try:
                report = repository.get_application_report(session, application_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            findings = latest_findings_for_application(session, application_id)
            risk = score_findings(findings)
            chain = audit_log.verify_chain(session)
            explanation = explain(risk, findings)
            pdf = build_scrutiny_note(report, findings, risk, explanation, identity, chain.ok)
            audit_log.append(
                session, actor=identity.actor, role=identity.role, action="scrutiny_note_exported",
                entity_type="application", entity_id=application_id,
                details={"finding_count": len(findings), "level": risk["level"], "pdf_sha256": hashlib.sha256(pdf).hexdigest()},
            )
            session.commit()
        name = f"scrutiny-note-APP-{application_id:04d}.pdf"
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


def stream_processing(
    svc: Service,
    identity: Identity,
    institution: str,
    category: str,
    documents: list[dict[str, Any]],
    cleanup: Callable[[], None] | None = None,
) -> StreamingResponse:
    """Run the unchanged pipeline in a worker thread; stream its stage events.

    One line of JSON per event: application, document_started, stage,
    document_finished, done (or error). The process lock still holds: a second
    submission waits for this one, exactly as without streaming.
    """
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def worker() -> None:
        started = time.perf_counter()
        try:
            with svc.lock:
                svc.pipeline.on_stage = lambda run_id, stage: events.put({"event": "stage", "run_id": run_id, **stage})
                try:
                    application_id = ensure_application(
                        svc.database, institution_name=institution, category=category,
                        actor=identity.actor, role=identity.role, config=svc.persistence,
                    )
                    events.put({"event": "application", "application_id": application_id, "institution": institution, "documents": len(documents)})
                    for index, document in enumerate(documents):
                        kind = svc.pipeline.document_kind(document["document_type"])
                        events.put({
                            "event": "document_started", "index": index, "total": len(documents),
                            "filename": Path(document["path"]).name, "document_type": document["document_type"], "kind": kind,
                        })
                        summary = svc.pipeline.process_document(
                            application_id=application_id, file_path=document["path"],
                            document_type=document["document_type"], actor=identity.actor,
                            role=identity.role, claim=document.get("claim"),
                        )
                        events.put({
                            "event": "document_finished", "index": index, "document_id": summary.document_id,
                            "run_status": summary.status, "verification_status": summary.verification_status,
                            "finding_types": summary.finding_types,
                        })
                    check = svc.pipeline.check_application(application_id, identity.actor, identity.role)
                    events.put({
                        "event": "application_checked", "status": check["status"],
                        "checks": [{k: c[k] for k in ("detector", "check", "label", "outcome", "status", "summary")} for c in check["checks"]],
                        "finding_types": [f["type"] for f in check["findings"]],
                    })
                    with svc.database.session() as session:
                        with session.begin():
                            risk = assess_application(
                                session, application_id, trigger="submission", actor=identity.actor,
                                role=identity.role, persistence=svc.persistence,
                            )
                    events.put({
                        "event": "done", "application_id": application_id,
                        "risk": {"level": risk["level"], "score": risk["score"], "subscores": {k: v["score"] for k, v in risk["subscores"].items()}},
                        "seconds": round(time.perf_counter() - started, 2),
                    })
                finally:
                    svc.pipeline.on_stage = None
        except Exception as exc:
            events.put({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})
        finally:
            if cleanup is not None:
                cleanup()
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def lines():
        while True:
            item = events.get()
            if item is None:
                return
            yield json.dumps(item, default=str) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------


def build_network(session: Any) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    for institution in session.scalars(select(Institution).order_by(Institution.id)).all():
        applications = sorted(institution.applications, key=lambda a: a.id)
        latest = applications[-1] if applications else None
        assessment = latest_assessment(session, latest.id) if latest else None
        open_strong = 0
        if latest:
            open_strong = sum(
                1 for f in latest_findings_for_application(session, latest.id)
                if f["strength"] == "strong" and f["review_status"] == "open"
            )
        nodes[f"i{institution.id}"] = {
            "id": f"i{institution.id}",
            "label": institution.name,
            "kind": "institution",
            "application_id": latest.id if latest else None,
            "risk_level": assessment.level if assessment else None,
            "risk_score": assessment.score if assessment else None,
            "open_strong": open_strong,
        }

    edges: dict[tuple[str, str], dict[str, Any]] = {}

    def add_edge(a: str, b: str, kind: str, strength: str, detail: dict[str, Any]) -> None:
        if a == b:
            return
        key = tuple(sorted((a, b)))
        edge = edges.setdefault(key, {"source": key[0], "target": key[1], "kinds": {}, "strength": "weak", "details": []})
        edge["kinds"][kind] = edge["kinds"].get(kind, 0) + 1
        if _RANK.get(strength, 0) > _RANK.get(edge["strength"], 0):
            edge["strength"] = strength
        if len(edge["details"]) < 12:
            edge["details"].append({"kind": kind, "label": EDGE_LABELS.get(kind, kind), **detail})

    doc_inst = {
        doc_id: inst_id
        for doc_id, inst_id in session.execute(
            select(Document.id, Application.institution_id).join(Application, Document.application_id == Application.id)
        ).all()
    }
    for row in session.scalars(select(CrossReference)).all():
        if row.same_institution:
            continue
        evidence = row.evidence or {}
        if row.kind == "near_duplicate_page_image" and evidence.get("certificate_numbers_differ") is not False:
            # Template siblings: the number checks decide, and drawing every
            # sibling pair would turn the graph into a solid mesh.
            continue
        a, b = doc_inst.get(row.document_a_id), doc_inst.get(row.document_b_id)
        if a is None or b is None:
            continue
        # A same-number page match corroborates the number finding (medium,
        # as in duplicate_checks.yaml); every other cross-institution kind is strong.
        strength = "medium" if row.kind == "near_duplicate_page_image" else "strong"
        add_edge(f"i{a}", f"i{b}", row.kind, strength, {
            "documents": [row.document_a_id, row.document_b_id],
            "certificate_number": evidence.get("certificate_number") or evidence.get("matched_value"),
        })

    # Shared faculty: from each document's latest faculty run.
    names = {n["label"]: key for key, n in nodes.items()}
    latest_runs = (
        select(CheckRun.document_id, func.max(CheckRun.id).label("run_id")).group_by(CheckRun.document_id).subquery()
    )
    rows = session.execute(
        select(FindingRow, Document, Application)
        .join(latest_runs, FindingRow.run_id == latest_runs.c.run_id)
        .join(Document, Document.id == latest_runs.c.document_id)
        .join(Application, Application.id == Document.application_id)
        .where(FindingRow.type.in_(["faculty_shared_across_institutions", "faculty_possible_shared_by_name"]))
    ).all()
    for finding, document, application in rows:
        evidence = finding.evidence or {}
        other = str(evidence.get("other_institution") or "")
        target = names.get(other)
        if target is None:
            target = f"r:{other}"
            nodes.setdefault(target, {"id": target, "label": other, "kind": "records_only", "application_id": None, "risk_level": None, "risk_score": None, "open_strong": 0})
            names[other] = target
        add_edge(f"i{application.institution_id}", target, "shared_faculty", finding.strength, {
            "count": evidence.get("count"), "basis": evidence.get("match_basis"),
        })

    linked = {e["source"] for e in edges.values()} | {e["target"] for e in edges.values()}
    for edge in edges.values():
        edge["kinds"] = [{"kind": k, "label": EDGE_LABELS.get(k, k), "count": c} for k, c in sorted(edge["kinds"].items())]
    return {
        "nodes": [{**n, "connected": n["id"] in linked} for n in nodes.values()],
        "edges": list(edges.values()),
        "note": "Links show shared evidence between institutions, not wrongdoing. Template-sibling page matches with different certificate numbers are omitted.",
    }


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def run_search(session: Any, q: str) -> dict[str, list[dict[str, Any]]]:
    term = q.strip().lower()
    like = f"%{term}%"

    def app_of_doc(doc_id: int) -> tuple[int, str] | None:
        row = session.execute(
            select(Application.id, Institution.name)
            .join(Document, Document.application_id == Application.id)
            .join(Institution, Institution.id == Application.institution_id)
            .where(Document.id == doc_id)
        ).first()
        return (row[0], row[1]) if row else None

    institutions = []
    for inst in session.scalars(select(Institution).where(func.lower(Institution.name).like(like)).limit(10)).all():
        apps = sorted(inst.applications, key=lambda a: a.id)
        institutions.append({"institution": inst.name, "application_id": apps[-1].id if apps else None, "applications": len(apps)})

    certificates: dict[tuple[int, str], dict[str, Any]] = {}
    for field, run in session.execute(
        select(ExtractedField, CheckRun).join(CheckRun, ExtractedField.run_id == CheckRun.id)
        .where(ExtractedField.name == "certificate_number", ExtractedField.validated.is_(True))
    ).all():
        if field.value and term in str(field.value).lower():
            app = app_of_doc(run.document_id)
            if app:
                certificates[(run.document_id, str(field.value))] = {"value": field.value, "source": "printed on document", "document_id": run.document_id, "application_id": app[0], "institution": app[1]}
    for qr, run in session.execute(
        select(QRDetectionRow, CheckRun).join(CheckRun, QRDetectionRow.run_id == CheckRun.id)
        .where(QRDetectionRow.certificate_id.is_not(None))
    ).all():
        if term in str(qr.certificate_id).lower():
            app = app_of_doc(run.document_id)
            if app:
                certificates.setdefault((run.document_id, f"qr:{qr.certificate_id}"), {"value": qr.certificate_id, "source": f"QR code ({qr.parsed_host})", "document_id": run.document_id, "application_id": app[0], "institution": app[1]})

    faculty = []
    seen: set[tuple[str | None, int]] = set()
    for member in session.scalars(
        select(FacultyMemberRow).where(
            (func.lower(FacultyMemberRow.faculty_id).like(like)) | (FacultyMemberRow.name_normalised.like(like)) | (func.lower(FacultyMemberRow.name).like(like))
        ).limit(60)
    ).all():
        if (member.faculty_id, member.institution_id) in seen:
            continue
        seen.add((member.faculty_id, member.institution_id))
        app = app_of_doc(member.document_id)
        faculty.append({
            "faculty_id": member.faculty_id, "name": member.name, "department": member.department,
            "qualification": member.qualification, "application_id": app[0] if app else None, "institution": app[1] if app else None,
        })

    documents = []
    for document in session.scalars(select(Document).where(func.lower(Document.original_filename).like(like)).limit(15)).all():
        app = app_of_doc(document.id)
        documents.append({"document_id": document.id, "filename": document.original_filename, "document_type": document.document_type, "application_id": app[0] if app else None, "institution": app[1] if app else None})

    return {
        "institutions": institutions,
        "certificates": list(certificates.values())[:25],
        "faculty": faculty[:25],
        "documents": documents,
    }
