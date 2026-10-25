"""Application validation and regulatory compliance: checks over a whole application.

Four questions no single document can answer:

1. **Completeness** — is every document the category requires present?
2. **Applicant identity** — does the accreditation certificate name the
   institution that is applying? A genuine certificate of *another* college
   verifies perfectly at the issuer; only this comparison notices it.
3. **Session coverage** — is the accreditation valid across the academic
   session applied for?
4. **Norms** — do the declared faculty meet the configured student:faculty
   ratio, cadre ratio and qualification norms, and would they on the AICTE
   records too?

The norms are **mock values in config**, labelled as such in every result.
Deterministic code only; no LLM. Every unmet check is an indicator with its
evidence, never a decision. Reads only what the document runs already stored.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get, load_config
from ..core.verification import VerificationStatus, most_severe
from ..db.models import Application, CheckRun, Document, ExtractedField, FacultyMemberRow
from ..faculty import sources as faculty_sources

CONFIG_NAME = "compliance_checks"
VALIDATION = "application_validation"
COMPLIANCE = "regulatory_compliance"
MOCK_NOTE = "against configured (mock) norms"


def run_application_checks(
    session: Session,
    application_id: int,
    config: dict[str, Any] | None = None,
    faculty_config: dict[str, Any] | None = None,
    as_of: str | date | None = None,
) -> dict[str, Any]:
    cfg = config if config is not None else load_config(CONFIG_NAME)
    fcfg = faculty_config if faculty_config is not None else load_config("faculty_checks")
    application = session.get(Application, application_id)
    if application is None:
        raise ValueError(f"no application with id {application_id}")
    applicant = application.institution.name
    documents = sorted(application.documents, key=lambda d: d.id)

    checks: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    _completeness(application.category, documents, cfg, checks, findings)
    certificates = _certificate_fields(session, documents)
    _applicant_identity(applicant, certificates, cfg, checks, findings)
    _session_coverage(certificates, cfg, checks, findings)

    records, _reason = faculty_sources.load_records(fcfg)
    record = None
    if records:
        record, _match = faculty_sources.find_institution_record(records, applicant, fcfg)
    declared = _declared_faculty(session, documents)
    norms = _norms(declared, record, cfg, fcfg, checks, findings)

    statuses = [VerificationStatus(c["status"]) for c in checks if c.get("status")]
    return {
        "status": (most_severe(statuses) or VerificationStatus.VERIFIED).value,
        "applicant": applicant,
        "checks": checks,
        "norms": norms,
        "findings": findings,
        "audit": {
            "config_version": cfg.get("config_version"),
            "aicte_record_id": record.get("aicte_id") if record else None,
            "records_version": (records or {}).get("records_version"),
            "documents_considered": [d.id for d in documents],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "norms_are_mock": True,
        },
    }


# --------------------------------------------------------------------------
# 1. Completeness
# --------------------------------------------------------------------------


def _completeness(category: str, documents: list[Document], cfg: dict[str, Any], checks: list, findings: list) -> None:
    if not bool(get(cfg, "completeness.enabled", True)):
        return
    required = (get(cfg, "completeness.required", {}) or {}).get(category)
    if not required:
        checks.append(_check(VALIDATION, "completeness", "Required documents", "not_evaluated", VerificationStatus.SOURCE_UNAVAILABLE, f"no document list is configured for category {category!r}"))
        return
    present = [d.document_type for d in documents]
    missing = []
    for item in required:
        count = sum(1 for t in present if t in item["any_of"])
        if count < int(item.get("min_count", 1)):
            missing.append({"label": item["label"], "accepted_types": item["any_of"], "required": item.get("min_count", 1), "found": count})
    if missing:
        findings.append(_finding(
            VALIDATION, "application_document_missing", str(get(cfg, "completeness.missing_strength", "medium")),
            f"The application is missing {len(missing)} required document(s): "
            f"{', '.join(m['label'] for m in missing)}. The checks that depend on them could not run; "
            f"requires manual verification.",
            {"missing": missing, "document_types_present": sorted(set(present)), "category": category},
            confidence="high",
        ))
    checks.append(_check(
        VALIDATION, "completeness", "Required documents",
        "met" if not missing else "not_met",
        VerificationStatus.VERIFIED if not missing else VerificationStatus.MANUAL_REVIEW_REQUIRED,
        f"{len(required) - len(missing)} of {len(required)} required document types present",
        {"required": [r["label"] for r in required], "missing": [m["label"] for m in missing]},
    ))


# --------------------------------------------------------------------------
# 2 and 3. Certificate: whose is it, and does it cover the session?
# --------------------------------------------------------------------------


def _certificate_fields(session: Session, documents: list[Document]) -> list[dict[str, Any]]:
    """Validated fields from each certificate document's latest run."""
    out = []
    for document in documents:
        run = session.scalars(select(CheckRun).where(CheckRun.document_id == document.id).order_by(CheckRun.id.desc()).limit(1)).first()
        if run is None:
            continue
        fields = {
            f.name: f
            for f in session.scalars(select(ExtractedField).where(ExtractedField.run_id == run.id)).all()
            if f.validated
        }
        if not fields:
            continue
        out.append({
            "document_id": document.id,
            "filename": document.original_filename,
            "institution_name": fields["institution_name"].value if "institution_name" in fields else None,
            "certificate_number": fields["certificate_number"].value if "certificate_number" in fields else None,
            "valid_from": fields["valid_from"].value_iso if "valid_from" in fields else None,
            "valid_to": fields["valid_to"].value_iso if "valid_to" in fields else None,
        })
    return out


def normalise_institution(name: str, cfg: dict[str, Any]) -> str:
    abbreviations = get(cfg, "applicant_identity.abbreviations", {}) or {}
    words = re.sub(r"[^a-z0-9 ]+", " ", str(name).lower()).split()
    expanded = [str(abbreviations.get(w, w)) for w in words]
    return " ".join(w for w in expanded if w not in ("of", "and", "the", "for"))


def _applicant_identity(applicant: str, certificates: list[dict[str, Any]], cfg: dict[str, Any], checks: list, findings: list) -> None:
    if not bool(get(cfg, "applicant_identity.enabled", True)):
        return
    named = [c for c in certificates if c["institution_name"]]
    if not named:
        checks.append(_check(VALIDATION, "applicant_identity", "Certificate names the applicant", "not_evaluated", VerificationStatus.SOURCE_UNAVAILABLE, "no validated institution name was read from any certificate"))
        return
    threshold = float(get(cfg, "applicant_identity.min_name_similarity", 0.85))
    target = normalise_institution(applicant, cfg)
    worst = None
    for cert in named:
        score = SequenceMatcher(None, normalise_institution(cert["institution_name"], cfg), target).ratio()
        cert["name_similarity"] = round(score, 3)
        if score < threshold:
            worst = cert if worst is None or score < worst["name_similarity"] else worst
            findings.append(_finding(
                VALIDATION, "certificate_names_other_institution", str(get(cfg, "applicant_identity.strength", "strong")),
                f"Mismatch detected between the applicant ('{applicant}') and the institution named on the "
                f"accreditation certificate ('{cert['institution_name']}'), similarity {score:.2f} "
                f"(threshold {threshold:g}). A certificate genuinely issued to another institution would "
                f"still verify at the issuer; requires manual verification.",
                {"applicant": applicant, "certificate_institution": cert["institution_name"], "certificate_number": cert["certificate_number"],
                 "document_id": cert["document_id"], "name_similarity": round(score, 3), "threshold": threshold,
                 "fields_compared": ["application.institution", "certificate.institution_name"]},
                confidence="high",
            ))
    checks.append(_check(
        VALIDATION, "applicant_identity", "Certificate names the applicant",
        "met" if worst is None else "not_met",
        VerificationStatus.VERIFIED if worst is None else VerificationStatus.MISMATCH_DETECTED,
        "every certificate names the applying institution" if worst is None else f"certificate names '{worst['institution_name']}'",
        {"certificates": [{k: c.get(k) for k in ("document_id", "institution_name", "name_similarity")} for c in named]},
    ))


def _session_coverage(certificates: list[dict[str, Any]], cfg: dict[str, Any], checks: list, findings: list) -> None:
    if not bool(get(cfg, "session.enabled", True)):
        return
    start = date.fromisoformat(str(get(cfg, "session.start")))
    end = date.fromisoformat(str(get(cfg, "session.end")))
    label = str(get(cfg, "session.label", f"{start}–{end}"))
    dated = [c for c in certificates if c["valid_to"]]
    if not dated:
        checks.append(_check(COMPLIANCE, "session_coverage", f"Accreditation covers session {label}", "not_evaluated", VerificationStatus.SOURCE_UNAVAILABLE, "no validated validity dates were read from any certificate"))
        return
    gaps = []
    for cert in dated:
        valid_to = date.fromisoformat(cert["valid_to"])
        valid_from = date.fromisoformat(cert["valid_from"]) if cert["valid_from"] else None
        if valid_to < end or (valid_from and valid_from > start):
            gaps.append({**cert, "session_start": start.isoformat(), "session_end": end.isoformat()})
    for gap in gaps:
        findings.append(_finding(
            COMPLIANCE, "accreditation_does_not_cover_session", str(get(cfg, "session.strength", "medium")),
            f"The accreditation printed on '{gap['filename']}' is valid {gap['valid_from'] or '?'} to {gap['valid_to']}, "
            f"which does not cover the full {label} session ({start.isoformat()} to {end.isoformat()}). "
            f"Requires manual verification.",
            {k: gap[k] for k in ("document_id", "certificate_number", "valid_from", "valid_to", "session_start", "session_end")} | {"session": label},
            confidence="high",
        ))
    checks.append(_check(
        COMPLIANCE, "session_coverage", f"Accreditation covers session {label}",
        "met" if not gaps else "not_met",
        VerificationStatus.VERIFIED if not gaps else VerificationStatus.EXPIRED,
        "validity covers the whole session" if not gaps else f"{len(gaps)} certificate(s) lapse before or start after the session",
        {"session": {"start": start.isoformat(), "end": end.isoformat()}, "certificates": [{k: c[k] for k in ("document_id", "valid_from", "valid_to")} for c in dated]},
    ))


# --------------------------------------------------------------------------
# 4. Norms
# --------------------------------------------------------------------------


def _declared_faculty(session: Session, documents: list[Document]) -> list[FacultyMemberRow]:
    people: list[FacultyMemberRow] = []
    for document in documents:
        if document.document_type != "faculty_declaration":
            continue
        run = session.scalars(select(CheckRun).where(CheckRun.document_id == document.id).order_by(CheckRun.id.desc()).limit(1)).first()
        if run is not None:
            people.extend(session.scalars(select(FacultyMemberRow).where(FacultyMemberRow.run_id == run.id)).all())
    return people


def _designation(raw: str | None, cfg: dict[str, Any]) -> str | None:
    text = re.sub(r"\s+", " ", str(raw or "").lower().replace(".", ". ")).replace(". ", " ").strip()
    aliases = get(cfg, "norms.designation_aliases", {}) or {}
    # Longest alias first, so "associate professor" is not read as "professor".
    for canonical, names in sorted(aliases.items(), key=lambda kv: -max(len(n) for n in kv[1])):
        for name in sorted(names, key=len, reverse=True):
            if text == name.replace(".", "").strip() or text.startswith(name.replace(".", "").strip() + " "):
                return str(canonical)
    return None


def _norms(declared: list[FacultyMemberRow], record: dict[str, Any] | None, cfg: dict[str, Any], fcfg: dict[str, Any], checks: list, findings: list) -> dict[str, Any]:
    out: dict[str, Any] = {"departments": [], "cadre": None, "qualifications": None, "note": f"Evaluated {MOCK_NOTE}."}
    if not bool(get(cfg, "norms.enabled", True)):
        return out
    if not declared:
        checks.append(_check(COMPLIANCE, "faculty_norms", "Faculty norms", "not_evaluated", VerificationStatus.SOURCE_UNAVAILABLE, "no declared faculty list was processed for this application"))
        return out

    # Student : faculty ratio, per department with an intake on record.
    max_ratio = float(get(cfg, "norms.max_student_faculty_ratio", 20))
    declared_counts: dict[str, int] = {}
    for person in declared:
        if person.department:
            declared_counts[person.department] = declared_counts.get(person.department, 0) + 1
    intake = {}
    recorded_counts = {}
    if record:
        for dept, value in (record.get("intake") or {}).items():
            canonical = faculty_sources.canonical_department(dept, fcfg)
            if canonical and value:
                intake[canonical] = int(value)
        for dept, value in (record.get("departments") or {}).items():
            canonical = faculty_sources.canonical_department(dept, fcfg)
            if canonical:
                recorded_counts[canonical] = int(value)
    ratio_rows = []
    for dept, students in sorted(intake.items()):
        d = declared_counts.get(dept, 0)
        r = recorded_counts.get(dept)
        declared_ratio = students / d if d else math.inf
        recorded_ratio = (students / r if r else math.inf) if r is not None else None
        row = {
            "department": dept, "intake": students, "declared_faculty": d, "recorded_faculty": r,
            "declared_ratio": None if math.isinf(declared_ratio) else round(declared_ratio, 1),
            "recorded_ratio": None if recorded_ratio is None or math.isinf(recorded_ratio) else round(recorded_ratio, 1),
            "max_ratio": max_ratio,
        }
        row["declared_meets"] = declared_ratio <= max_ratio
        row["recorded_meets"] = None if recorded_ratio is None else recorded_ratio <= max_ratio
        ratio_rows.append(row)
        if not row["declared_meets"]:
            findings.append(_finding(
                COMPLIANCE, "compliance_student_faculty_ratio", str(get(cfg, "norms.ratio_strength", "medium")),
                f"{dept}: intake {students} with {d} declared faculty gives {row['declared_ratio'] or '∞'} students per "
                f"faculty member, above the configured maximum of {max_ratio:g} ({MOCK_NOTE}). Requires manual verification.",
                row, confidence="high",
            ))
        elif row["recorded_meets"] is False:
            findings.append(_finding(
                COMPLIANCE, "compliance_met_only_as_declared", str(get(cfg, "norms.met_only_as_declared_strength", "medium")),
                f"{dept}: the declared faculty count meets the configured ratio ({row['declared_ratio']}:1), but the AICTE-recorded "
                f"count does not ({row['recorded_ratio']}:1, maximum {max_ratio:g}:1, {MOCK_NOTE}). The difference between the "
                f"two counts is what brings the department into line; requires manual verification.",
                row, confidence="high",
            ))
    out["departments"] = ratio_rows
    unmet_ratio = [r for r in ratio_rows if not r["declared_meets"] or r["recorded_meets"] is False]
    checks.append(_check(
        COMPLIANCE, "student_faculty_ratio", f"Student:faculty ratio ≤ {max_ratio:g}:1",
        "not_evaluated" if not ratio_rows else ("met" if not unmet_ratio else "not_met"),
        VerificationStatus.SOURCE_UNAVAILABLE if not ratio_rows else (VerificationStatus.VERIFIED if not unmet_ratio else VerificationStatus.MANUAL_REVIEW_REQUIRED),
        "no department intake on record" if not ratio_rows else f"{len(ratio_rows) - len(unmet_ratio)} of {len(ratio_rows)} departments within the norm",
    ))

    # Cadre ratio, institution-wide.
    norm = get(cfg, "norms.cadre_ratio", {}) or {}
    total_norm = sum(norm.values()) or 1
    min_fraction = float(get(cfg, "norms.min_fraction_of_norm", 0.5))
    counts = {k: 0 for k in norm}
    unknown = 0
    for person in declared:
        cadre = _designation(person.designation, cfg)
        if cadre in counts:
            counts[cadre] += 1
        else:
            unknown += 1
    known = sum(counts.values())
    cadre_rows = []
    short = []
    for cadre, weight in norm.items():
        target = weight / total_norm
        actual = counts[cadre] / known if known else 0
        row = {"cadre": cadre, "count": counts[cadre], "share": round(actual, 3), "target_share": round(target, 3), "minimum_share": round(target * min_fraction, 3)}
        cadre_rows.append(row)
        # Only senior cadres can fall short in a way that matters; a surplus of
        # assistant professors is the other side of the same shortfall.
        if cadre != "assistant_professor" and actual < target * min_fraction:
            short.append(row)
    out["cadre"] = {"rows": cadre_rows, "unknown_designations": unknown, "norm": norm}
    if short and known:
        findings.append(_finding(
            COMPLIANCE, "compliance_cadre_ratio", str(get(cfg, "norms.cadre_strength", "weak")),
            "Declared cadre shares fall below half the configured target for "
            + ", ".join(f"{r['cadre'].replace('_', ' ')} ({r['count']} of {known}, {r['share']:.0%} vs target {r['target_share']:.0%})" for r in short)
            + f" ({MOCK_NOTE}).",
            {"rows": cadre_rows, "norm": norm, "declared_people": known},
            confidence="medium",
        ))
    checks.append(_check(
        COMPLIANCE, "cadre_ratio", "Cadre ratio " + ":".join(str(v) for v in norm.values()),
        "met" if not short else "not_met",
        VerificationStatus.VERIFIED if not short else VerificationStatus.MANUAL_REVIEW_REQUIRED,
        ", ".join(f"{r['cadre'].replace('_', ' ')} {r['count']}" for r in cadre_rows),
    ))

    # Qualifications.
    needs_phd = set(get(cfg, "norms.phd_required_for", []) or [])
    markers = [m.lower() for m in (get(cfg, "norms.phd_markers", []) or [])]
    lacking = []
    checked = 0
    for person in declared:
        cadre = _designation(person.designation, cfg)
        if cadre not in needs_phd:
            continue
        checked += 1
        qualification = (person.qualification or "").lower()
        if not any(m in qualification for m in markers):
            lacking.append({"faculty_id": person.faculty_id, "name": person.name, "designation": person.designation, "qualification": person.qualification, "department": person.department})
    out["qualifications"] = {"checked": checked, "lacking": len(lacking)}
    if lacking:
        findings.append(_finding(
            COMPLIANCE, "compliance_qualification_norm", str(get(cfg, "norms.qualification_strength", "medium")),
            f"{len(lacking)} of {checked} declared faculty in designations that require a doctorate list no doctorate "
            f"({MOCK_NOTE}). Requires manual verification against their records.",
            {"count": len(lacking), "checked": checked, "examples": lacking[:15], "designations": sorted(needs_phd)},
            confidence="medium",
        ))
    checks.append(_check(
        COMPLIANCE, "qualifications", "Doctorate for senior designations",
        "met" if not lacking else "not_met",
        VerificationStatus.VERIFIED if not lacking else VerificationStatus.MANUAL_REVIEW_REQUIRED,
        f"{checked - len(lacking)} of {checked} senior faculty list a doctorate",
    ))
    return out


# --------------------------------------------------------------------------


def _check(detector: str, key: str, label: str, outcome: str, status: VerificationStatus, summary: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"detector": detector, "check": key, "label": label, "outcome": outcome, "status": status.value, "summary": summary, "detail": detail or {}}


def _finding(detector: str, type_: str, strength: str, reason: str, evidence: dict[str, Any], confidence: str | None = None) -> dict[str, Any]:
    return {"detector": detector, "type": type_, "strength": strength, "reason": reason, "evidence": evidence, "confidence": confidence}
