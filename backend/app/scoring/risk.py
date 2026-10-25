"""Combine findings into per-detector sub-scores and one review-priority level.

**The level prioritises review order only.** It approves nothing, rejects
nothing and says nothing about an institution. It decides which application a
reviewer opens first.

Deterministic and additive-only. Points come from findings, and only from
findings: a verified lookup, an intact signature or the absence of findings
contributes zero - nothing in this module can *lower* a score (see CLAUDE.md,
"A clean signature result is NEUTRAL"). The one thing that removes points is a
human dismissing a finding, which is a recorded review action, not a signal.

Every contribution is returned, so a number can always be taken apart into the
findings that produced it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get, load_config
from ..db import audit_log
from ..db.models import Application, ApplicationCheck, CheckRun, Document, FindingRow, RiskAssessment
from ..db.roles import validate_actor, validate_role

CONFIG_NAME = "risk_scoring"
LEVELS = ("low", "medium", "high")


def config_hash(cfg: dict[str, Any]) -> str:
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def score_findings(findings: list[dict[str, Any]], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure function: findings in, score breakdown out."""
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    strength_points = get(cfg, "points.strength", {}) or {}
    overrides = get(cfg, "points.type_overrides", {}) or {}
    confidence_mult = get(cfg, "points.confidence_multiplier", {}) or {}
    decay = float(get(cfg, "points.repeat_decay", 0.5))
    excluded = {str(s) for s in (get(cfg, "review.exclude_statuses", ["dismissed"]) or [])}
    confirmed_mult = float(get(cfg, "review.confirmed_multiplier", 1.0))
    detectors_cfg = {
        k: v for k, v in (get(cfg, "detectors", {}) or {}).items() if isinstance(v, dict)
    }
    cap = float(get(cfg, "detectors.cap", 100))

    contributions: list[dict[str, Any]] = []
    excluded_findings: list[dict[str, Any]] = []
    for finding in findings:
        status = str(finding.get("review_status") or "open")
        if status in excluded:
            excluded_findings.append({"id": finding.get("id"), "type": finding.get("type"), "review_status": status})
            continue
        ftype = str(finding.get("type"))
        strength = str(finding.get("strength") or "weak")
        base = float(overrides.get(ftype, strength_points.get(strength, 0)))
        multiplier = float(confidence_mult.get(str(finding.get("confidence")), 1.0)) if finding.get("confidence") else 1.0
        if status == "confirmed":
            multiplier *= confirmed_mult
        contributions.append(
            {
                "finding_id": finding.get("id"),
                "detector": finding.get("detector"),
                "type": ftype,
                "strength": strength,
                "confidence": finding.get("confidence"),
                "review_status": status,
                "base_points": base,
                "multiplier": round(multiplier, 3),
                "points": base * multiplier,
            }
        )

    # Repeats of one type decay: ten missing-EXIF photos are one observation
    # made ten times, not ten independent signals.
    by_type: dict[str, list[dict[str, Any]]] = {}
    for entry in contributions:
        by_type.setdefault(entry["type"], []).append(entry)
    for entries in by_type.values():
        entries.sort(key=lambda e: (-e["points"], e["finding_id"] or 0))
        for index, entry in enumerate(entries):
            entry["repeat_index"] = index
            entry["points"] = round(entry["points"] * (decay ** index), 2)

    subscores: dict[str, Any] = {}
    for name, settings in detectors_cfg.items():
        subscores[name] = {
            "label": settings.get("label", name),
            "weight": float(settings.get("weight", 1.0)),
            "raw_points": 0.0,
            "score": 0.0,
            "finding_count": 0,
            "strong": 0,
            "medium": 0,
            "weak": 0,
        }
    for entry in contributions:
        bucket = subscores.setdefault(
            str(entry["detector"]),
            {"label": entry["detector"], "weight": 1.0, "raw_points": 0.0, "score": 0.0, "finding_count": 0, "strong": 0, "medium": 0, "weak": 0},
        )
        bucket["raw_points"] += entry["points"]
        bucket["finding_count"] += 1
        if entry["strength"] in ("strong", "medium", "weak"):
            bucket[entry["strength"]] += 1
    for bucket in subscores.values():
        bucket["raw_points"] = round(bucket["raw_points"], 2)
        bucket["score"] = round(min(cap, bucket["raw_points"]), 2)

    combined = round(min(100.0, sum(b["weight"] * b["score"] for b in subscores.values())), 2)

    thresholds = get(cfg, "levels", {}) or {}
    level = "low"
    if combined >= float(thresholds.get("high", 40)):
        level = "high"
    elif combined >= float(thresholds.get("medium", 15)):
        level = "medium"

    floors: list[dict[str, Any]] = []
    strong_detectors = {e["detector"] for e in contributions if e["strength"] == "strong"}
    if strong_detectors:
        floor = str(thresholds.get("floor_for_strong", "medium"))
        if LEVELS.index(floor) > LEVELS.index(level):
            floors.append({"rule": "floor_for_strong", "from": level, "to": floor})
            level = floor
    if len(strong_detectors) >= 2:
        floor = str(thresholds.get("floor_for_corroborated_strong", "high"))
        if LEVELS.index(floor) > LEVELS.index(level):
            floors.append({"rule": "floor_for_corroborated_strong", "from": level, "to": floor, "detectors": sorted(strong_detectors)})
            level = floor

    contributions.sort(key=lambda e: (-e["points"], e["finding_id"] or 0))
    return {
        "score": combined,
        "level": level,
        "subscores": subscores,
        "contributions": contributions,
        "excluded_findings": excluded_findings,
        "floors_applied": floors,
        "config_version": cfg.get("config_version"),
        "config_hash": config_hash(cfg),
        "purpose": "review prioritisation only; not a decision",
    }


def latest_findings_for_application(session: Session, application_id: int) -> list[dict[str, Any]]:
    """Findings from each document's newest run and the newest application check."""
    documents = session.scalars(
        select(Document).where(Document.application_id == application_id).order_by(Document.id)
    ).all()
    findings: list[dict[str, Any]] = []
    for document in documents:
        run_id = session.scalars(
            select(CheckRun.id).where(CheckRun.document_id == document.id).order_by(CheckRun.id.desc()).limit(1)
        ).first()
        if run_id is None:
            continue
        for row in session.scalars(select(FindingRow).where(FindingRow.run_id == run_id).order_by(FindingRow.id)).all():
            findings.append(
                {
                    "id": row.id,
                    "document_id": document.id,
                    "document_type": document.document_type,
                    "detector": row.detector,
                    "type": row.type,
                    "strength": row.strength,
                    "confidence": row.confidence,
                    "review_status": row.review_status,
                    "reason": row.reason,
                }
            )
    check_id = session.scalars(
        select(ApplicationCheck.id)
        .where(ApplicationCheck.application_id == application_id)
        .order_by(ApplicationCheck.id.desc())
        .limit(1)
    ).first()
    if check_id is not None:
        for row in session.scalars(select(FindingRow).where(FindingRow.application_check_id == check_id).order_by(FindingRow.id)).all():
            findings.append(
                {
                    "id": row.id,
                    "document_id": None,
                    "document_type": None,
                    "detector": row.detector,
                    "type": row.type,
                    "strength": row.strength,
                    "confidence": row.confidence,
                    "review_status": row.review_status,
                    "reason": row.reason,
                }
            )
    return findings


def assess_application(
    session: Session,
    application_id: int,
    trigger: str,
    actor: str,
    role: str,
    cfg: dict[str, Any] | None = None,
    persistence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score an application and store the assessment as a new history row."""
    validate_actor(actor)
    validate_role(role, persistence)
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    if session.get(Application, application_id) is None:
        raise ValueError(f"no application with id {application_id}")

    result = score_findings(latest_findings_for_application(session, application_id), cfg)
    row = RiskAssessment(
        application_id=application_id,
        config_version=str(result["config_version"]),
        config_hash=result["config_hash"],
        score=result["score"],
        level=result["level"],
        subscores=result["subscores"],
        contributions=result["contributions"],
        floors_applied=result["floors_applied"],
        trigger=trigger,
        actor=actor,
        role=role,
    )
    session.add(row)
    session.flush()
    audit_log.append(
        session,
        actor=actor,
        role=role,
        action="risk_assessed",
        entity_type="application",
        entity_id=application_id,
        details={
            "assessment_id": row.id,
            "level": result["level"],
            "score": result["score"],
            "trigger": trigger,
            "config_version": result["config_version"],
            "finding_count": len(result["contributions"]),
        },
    )
    result["assessment_id"] = row.id
    result["created_at"] = row.created_at.isoformat() if row.created_at else None
    result["trigger"] = trigger
    return result


def latest_assessment(session: Session, application_id: int) -> RiskAssessment | None:
    return session.scalars(
        select(RiskAssessment)
        .where(RiskAssessment.application_id == application_id)
        .order_by(RiskAssessment.id.desc())
        .limit(1)
    ).first()


def assessment_history(session: Session, application_id: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(RiskAssessment).where(RiskAssessment.application_id == application_id).order_by(RiskAssessment.id)
    ).all()
    return [
        {
            "id": r.id,
            "score": r.score,
            "level": r.level,
            "trigger": r.trigger,
            "actor": r.actor,
            "role": r.role,
            "config_version": r.config_version,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
