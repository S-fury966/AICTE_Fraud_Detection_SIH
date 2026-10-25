"""Faculty count consistency: declared vs website vs AICTE records, per department.

Per department, not totals, because a totals comparison cannot distinguish a
genuine overall count from departments padded to cover a shortfall elsewhere -
and that redistribution is the pattern most worth detecting (a settled
decision; see CLAUDE.md).

Three rules shape the module.

**Tolerance is per department and configurable.** Allowed deviation is
`max(absolute, ceil(relative x reference))`, so a faculty member joining
between two capture dates is not a discrepancy.

**Direction matters.** Declared above AICTE records is the inflation pattern;
declared below is a disagreement worth a note, not a concern of this detector.

**Identity: synthetic faculty id first, name second.** A shared faculty id
across institutions is strong. A name match without an id match is medium at
most, and requires the same qualification - two people can share a name.

No LLM. Status from code only, from the fixed taxonomy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import get, load_config
from ..core.verification import VerificationStatus, most_severe
from . import sources as src

CONFIG_NAME = "faculty_checks"
DETECTOR = "faculty_consistency"


@dataclass
class FacultyCheckResult:
    status: str
    institution_name: str
    record_match: dict[str, Any]
    sources: dict[str, dict[str, Any]]
    departments: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    declared_people: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "institution_name": self.institution_name,
            "record_match": self.record_match,
            "sources": self.sources,
            "departments": self.departments,
            "totals": self.totals,
            "identity": self.identity,
            "findings": self.findings,
            "audit": self.audit,
        }


def check_faculty(
    declared_path: str | Path,
    institution_name: str,
    institution_id: int | None = None,
    config: dict[str, Any] | None = None,
    declared_on: date | None = None,
    other_declarations: list[dict[str, Any]] | None = None,
    as_of: str | date | None = None,
    records: dict[str, Any] | None = None,
    declared_name: str | None = None,
) -> FacultyCheckResult:
    """Compare the declared list against the website snapshot and AICTE records.

    `other_declarations` are faculty rows other institutions declared in
    applications already stored: dicts with `institution_id`,
    `institution_name`, `faculty_id`, `name`, `name_normalised`,
    `qualification`. Passed in so this module needs no database.
    """
    cfg = config if config is not None else load_config(CONFIG_NAME)
    today = _as_of(as_of if as_of is not None else get(cfg, "sources.as_of"))

    records_reason = None
    if records is None:
        records, records_reason = src.load_records(cfg)
    record, match = (None, {"method": "none"})
    if records is not None:
        record, match = src.find_institution_record(records, institution_name, cfg)

    declared = src.declared_source(Path(declared_path), cfg, declared_on or today)
    if declared_name:
        # The uploaded name, not the content-addressed store path.
        declared.reference = declared_name
    aicte = src.aicte_source(record, cfg, reason=records_reason)
    website = src.website_source(record, cfg)
    sources = {"declared": declared, "website": website, "aicte": aicte}

    result = FacultyCheckResult(
        status=VerificationStatus.MANUAL_REVIEW_REQUIRED.value,
        institution_name=institution_name,
        record_match=match,
        sources={name: source.to_dict() for name, source in sources.items()},
        declared_people=[p.to_dict() for p in declared.people],
        audit={
            "config_version": cfg.get("config_version"),
            "records_version": (records or {}).get("records_version"),
            "as_of": today.isoformat(),
            "aicte_record_id": record.get("aicte_id") if record else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    if not declared.available:
        result.status = VerificationStatus.MANUAL_REVIEW_REQUIRED.value
        result.findings.append(
            _finding(
                "faculty_declared_list_unreadable",
                "weak",
                "The declared faculty list could not be read, so no comparison "
                "was possible. Requires manual verification.",
                {"reason": declared.unavailable_reason, "file": declared.reference},
            )
        )
        return result

    statuses: list[VerificationStatus] = []
    _source_health(sources, cfg, today, result, statuses)
    _compare_departments(declared, website, aicte, cfg, result, statuses)
    _identity(declared, aicte, record, records, institution_id, other_declarations or [], cfg, result, statuses)

    result.status = (most_severe(statuses) or VerificationStatus.VERIFIED).value
    return result


# --------------------------------------------------------------------------
# Source health: availability and staleness
# --------------------------------------------------------------------------


def _source_health(
    sources: dict[str, src.Source],
    cfg: dict[str, Any],
    today: date,
    result: FacultyCheckResult,
    statuses: list[VerificationStatus],
) -> None:
    max_age = int(get(cfg, "sources.max_source_age_days", 180))
    for name in ("website", "aicte"):
        source = sources[name]
        if not source.available:
            statuses.append(VerificationStatus.SOURCE_UNAVAILABLE)
            result.findings.append(
                _finding(
                    "faculty_source_unavailable",
                    str(get(cfg, "strength.source_unavailable", "weak")),
                    f"The {_label(name)} could not be consulted, so the "
                    f"comparison against it was not made. This is a gap in "
                    f"the evidence, not an indicator about the institution.",
                    {"source": name, "reason": source.unavailable_reason},
                )
            )

    for name, source in sources.items():
        if not source.available or not source.captured_on:
            continue
        captured = _parse_date(source.captured_on)
        if captured is None:
            continue
        age = (today - captured).days
        if age > max_age:
            result.findings.append(
                _finding(
                    "faculty_source_stale",
                    str(get(cfg, "strength.stale_source", "weak")),
                    f"The {_label(name)} was captured {age} days before the "
                    f"evaluation date (limit {max_age}). Counts may have "
                    f"changed honestly since.",
                    {"source": name, "captured_on": source.captured_on, "age_days": age, "max_age_days": max_age},
                )
            )


# --------------------------------------------------------------------------
# Per-department counts
# --------------------------------------------------------------------------


def allowed_deviation(reference: int, cfg: dict[str, Any]) -> int:
    absolute = int(get(cfg, "tolerance.absolute", 1))
    relative = float(get(cfg, "tolerance.relative", 0.1))
    return max(absolute, math.ceil(relative * max(reference, 0)))


def _compare_departments(
    declared: src.Source,
    website: src.Source,
    aicte: src.Source,
    cfg: dict[str, Any],
    result: FacultyCheckResult,
    statuses: list[VerificationStatus],
) -> None:
    departments = sorted(
        set(declared.counts)
        | (set(aicte.counts) if aicte.available else set())
        | (set(website.counts) if website.available else set())
    )
    strong_ratio = float(get(cfg, "strength.declared_exceeds_records_strong_ratio", 0.25))
    over_positive: list[dict[str, Any]] = []
    over_negative: list[dict[str, Any]] = []

    for department in departments:
        d = declared.counts.get(department, 0)
        a = aicte.counts.get(department) if aicte.available else None
        w = website.counts.get(department) if website.available else None
        if aicte.available and a is None:
            a_effective = 0
        else:
            a_effective = a
        if website.available and w is None:
            w_effective = 0
        else:
            w_effective = w

        reference = a_effective if a_effective is not None else (w_effective if w_effective is not None else d)
        allowed = allowed_deviation(reference or 0, cfg)
        row: dict[str, Any] = {
            "department": department,
            "declared": d,
            "website": w_effective,
            "aicte": a_effective,
            "allowed_deviation": allowed,
            "declared_minus_aicte": None if a_effective is None else d - a_effective,
            "declared_minus_website": None if w_effective is None else d - w_effective,
            "checks": [],
        }
        dept_statuses: list[VerificationStatus] = []

        # Declared vs AICTE
        if a_effective is None:
            dept_statuses.append(VerificationStatus.SOURCE_UNAVAILABLE)
            row["checks"].append({"pair": "declared_vs_aicte", "outcome": "not_compared", "reason": "AICTE records unavailable"})
        elif aicte.available and department not in aicte.counts and d > 0:
            dept_statuses.append(VerificationStatus.MANUAL_REVIEW_REQUIRED)
            row["checks"].append({"pair": "declared_vs_aicte", "outcome": "department_not_in_records"})
            result.findings.append(
                _finding(
                    "faculty_department_not_in_records",
                    str(get(cfg, "strength.department_not_in_records", "medium")),
                    f"The application declares {d} faculty in {department}, a "
                    f"department the AICTE records for this institution do not "
                    f"list. Requires manual verification.",
                    {"department": department, "declared": d, "aicte": None, "aicte_record": aicte.reference},
                )
            )
        else:
            diff = d - a_effective
            within = abs(diff) <= allowed
            row["checks"].append({"pair": "declared_vs_aicte", "outcome": "within_tolerance" if within else "outside_tolerance", "difference": diff})
            if within:
                dept_statuses.append(VerificationStatus.VERIFIED)
            else:
                dept_statuses.append(VerificationStatus.MISMATCH_DETECTED)
                (over_positive if diff > 0 else over_negative).append({"department": department, "difference": diff})
                if diff > 0:
                    ratio = diff / a_effective if a_effective else math.inf
                    strength = (
                        "strong"
                        if ratio >= strong_ratio
                        else str(get(cfg, "strength.declared_exceeds_records", "medium"))
                    )
                    result.findings.append(
                        _finding(
                            "faculty_count_exceeds_records",
                            strength,
                            f"Mismatch detected between the declared faculty "
                            f"count and AICTE records for {department}: "
                            f"{d} declared vs {a_effective} on record "
                            f"(+{diff}; tolerance ±{allowed}).",
                            {
                                "department": department,
                                "declared": d,
                                "aicte": a_effective,
                                "difference": diff,
                                "allowed_deviation": allowed,
                                "excess_ratio": None if a_effective == 0 else round(ratio, 3),
                                "fields_compared": ["declared.count", "aicte.count"],
                                "aicte_record": aicte.reference,
                                "aicte_captured_on": aicte.captured_on,
                                "declared_file": declared.reference,
                            },
                            confidence="high",
                        )
                    )
                else:
                    result.findings.append(
                        _finding(
                            "faculty_count_below_records",
                            str(get(cfg, "strength.declared_below_records", "weak")),
                            f"The declared faculty count for {department} is "
                            f"below AICTE records: {d} declared vs "
                            f"{a_effective} on record ({diff}; tolerance "
                            f"±{allowed}).",
                            {
                                "department": department,
                                "declared": d,
                                "aicte": a_effective,
                                "difference": diff,
                                "allowed_deviation": allowed,
                                "aicte_record": aicte.reference,
                            },
                        )
                    )

        # Declared vs website
        if w_effective is None:
            row["checks"].append({"pair": "declared_vs_website", "outcome": "not_compared", "reason": "website snapshot unavailable"})
        else:
            diff_w = d - w_effective
            within_w = abs(diff_w) <= allowed
            row["checks"].append({"pair": "declared_vs_website", "outcome": "within_tolerance" if within_w else "outside_tolerance", "difference": diff_w})
            if not within_w:
                dept_statuses.append(VerificationStatus.MISMATCH_DETECTED)
                result.findings.append(
                    _finding(
                        "faculty_count_differs_from_website",
                        str(get(cfg, "strength.declared_vs_website", "medium")),
                        f"Mismatch detected between the declared faculty count "
                        f"and the institution's own website for {department}: "
                        f"{d} declared vs {w_effective} listed on the website "
                        f"({diff_w:+d}; tolerance ±{allowed}).",
                        {
                            "department": department,
                            "declared": d,
                            "website": w_effective,
                            "difference": diff_w,
                            "allowed_deviation": allowed,
                            "fields_compared": ["declared.count", "website.count"],
                            "website_snapshot": website.reference,
                            "website_captured_on": website.captured_on,
                        },
                        confidence="medium",
                    )
                )

        status = most_severe(dept_statuses) or VerificationStatus.VERIFIED
        row["status"] = status.value
        statuses.append(status)
        result.departments.append(row)

    totals = {
        "declared": sum(declared.counts.values()),
        "website": sum(website.counts.values()) if website.available else None,
        "aicte": sum(aicte.counts.values()) if aicte.available else None,
    }
    if totals["aicte"] is not None:
        totals["allowed_deviation"] = allowed_deviation(totals["aicte"], cfg)
        totals["declared_minus_aicte"] = totals["declared"] - totals["aicte"]
        totals["within_tolerance"] = abs(totals["declared_minus_aicte"]) <= totals["allowed_deviation"]
    result.totals = totals

    # Redistribution: the total looks fine, the departments do not.
    if (
        totals.get("within_tolerance")
        and over_positive
        and over_negative
    ):
        result.findings.append(
            _finding(
                "faculty_counts_redistributed",
                str(get(cfg, "strength.redistribution", "strong")),
                "The total declared faculty count is within tolerance of AICTE "
                "records, but individual departments are outside it in "
                "opposite directions. A totals-only check would pass this; "
                "per department, the counts do not match. Requires manual "
                "verification.",
                {
                    "total_declared": totals["declared"],
                    "total_aicte": totals["aicte"],
                    "departments_above_records": over_positive,
                    "departments_below_records": over_negative,
                },
                confidence="high",
            )
        )


# --------------------------------------------------------------------------
# Identity: the same person at more than one institution
# --------------------------------------------------------------------------


def _identity(
    declared: src.Source,
    aicte: src.Source,
    record: dict[str, Any] | None,
    records: dict[str, Any] | None,
    institution_id: int | None,
    other_declarations: list[dict[str, Any]],
    cfg: dict[str, Any],
    result: FacultyCheckResult,
    statuses: list[VerificationStatus],
) -> None:
    max_examples = int(get(cfg, "identity.max_examples", 15))
    min_similarity = float(get(cfg, "identity.min_name_similarity", 0.93))
    same_qualification = bool(get(cfg, "identity.require_same_qualification", True))
    own_aicte_id = record.get("aicte_id") if record else None
    own_roster: set[str] = set()

    # The pool of people at OTHER institutions: AICTE rosters + declarations.
    pool: list[dict[str, Any]] = []
    if bool(get(cfg, "identity.compare_against_records", True)) and records:
        for other in records.get("institutions", []) or []:
            if own_aicte_id is not None and other.get("aicte_id") == own_aicte_id:
                continue
            for person in other.get("faculty", []) or []:
                pool.append(
                    {
                        "source": "aicte_records",
                        "institution_name": other.get("name"),
                        "institution_ref": other.get("aicte_id"),
                        "faculty_id": person.get("faculty_id"),
                        "name": person.get("name"),
                        "name_normalised": src.normalise_person_name(str(person.get("name", "")), cfg),
                        "qualification": person.get("qualification"),
                        "department": person.get("department"),
                    }
                )
    if bool(get(cfg, "identity.compare_against_declarations", True)):
        for row in other_declarations:
            if institution_id is not None and row.get("institution_id") == institution_id:
                continue
            pool.append(
                {
                    "source": "other_application",
                    "institution_name": row.get("institution_name"),
                    "institution_ref": f"institution:{row.get('institution_id')}",
                    "document_id": row.get("document_id"),
                    "faculty_id": row.get("faculty_id"),
                    "name": row.get("name"),
                    "name_normalised": row.get("name_normalised") or src.normalise_person_name(str(row.get("name", "")), cfg),
                    "qualification": row.get("qualification"),
                    "department": row.get("department"),
                }
            )

    by_id: dict[str, list[dict[str, Any]]] = {}
    for entry in pool:
        if entry.get("faculty_id"):
            by_id.setdefault(str(entry["faculty_id"]).strip().upper(), []).append(entry)

    id_matches: dict[str, list[dict[str, Any]]] = {}
    name_matches: dict[str, list[dict[str, Any]]] = {}
    seen_ids: dict[str, int] = {}
    duplicates_within: list[str] = []

    own_roster = {
        str(p.faculty_id).strip().upper() for p in aicte.people if p.faculty_id
    } if aicte.available else set()

    for person in declared.people:
        key = (person.faculty_id or "").strip().upper()
        if key:
            seen_ids[key] = seen_ids.get(key, 0) + 1
            if seen_ids[key] == 2:
                duplicates_within.append(key)

        matched_by_id = False
        if key and bool(get(cfg, "identity.match_on_id", True)):
            for other in by_id.get(key, []):
                matched_by_id = True
                group = id_matches.setdefault(str(other["institution_name"]), [])
                existing = next((m for m in group if m["faculty_id"] == person.faculty_id), None)
                if existing is not None:
                    # Same person seen in both the other institution's roster
                    # and its declaration: one match, two sources.
                    if other["source"] not in existing["other_sources"]:
                        existing["other_sources"].append(other["source"])
                    continue
                group.append(
                    {
                        "faculty_id": person.faculty_id,
                        "declared_name": person.name,
                        "other_name": other.get("name"),
                        "other_sources": [other["source"]],
                        "other_department": other.get("department"),
                        "other_document_id": other.get("document_id"),
                        # On THIS institution's own AICTE roster: likely the
                        # genuine employer, and the other side is the question.
                        "on_own_aicte_roster": key in own_roster,
                    }
                )
        if matched_by_id or not bool(get(cfg, "identity.match_on_name", True)):
            continue

        mine = src.normalise_person_name(person.name, cfg)
        if not mine:
            continue
        for other in pool:
            if key and other.get("faculty_id") and str(other["faculty_id"]).strip().upper() != key:
                # Both carry ids and the ids differ: two different people,
                # whatever their names say.
                continue
            if same_qualification and (
                src.normalise_text(person.qualification) != src.normalise_text(other.get("qualification"))
                or not person.qualification
            ):
                continue
            score = src.name_similarity(mine, str(other.get("name_normalised") or ""))
            if score >= min_similarity:
                name_matches.setdefault(str(other["institution_name"]), []).append(
                    {
                        "declared_name": person.name,
                        "declared_faculty_id": person.faculty_id,
                        "other_name": other.get("name"),
                        "other_faculty_id": other.get("faculty_id"),
                        "qualification": person.qualification,
                        "name_similarity": round(score, 3),
                        "other_source": other["source"],
                    }
                )

    for institution, matches in sorted(id_matches.items()):
        statuses.append(VerificationStatus.MISMATCH_DETECTED)
        on_own = all(m["on_own_aicte_roster"] for m in matches)
        result.findings.append(
            _finding(
                "faculty_shared_across_institutions",
                # Every shared person is on this institution's own AICTE
                # roster: it is most likely the employer, and the other
                # institution's claim is the one in question. Still shown -
                # the same person is counted twice - but not as strong here.
                "medium" if on_own else str(get(cfg, "strength.shared_faculty_id", "strong")),
                f"{len(matches)} faculty member(s) declared here carry a faculty "
                f"id that is also listed for {institution}."
                + (
                    " All of them are on this institution's own AICTE roster."
                    if on_own
                    else ""
                )
                + " The same person counted at two institutions requires "
                "manual verification.",
                {
                    "other_institution": institution,
                    "match_basis": "faculty_id",
                    "count": len(matches),
                    "all_on_own_aicte_roster": on_own,
                    "examples": matches[:max_examples],
                },
                confidence="high",
            )
        )
    for institution, matches in sorted(name_matches.items()):
        statuses.append(VerificationStatus.MANUAL_REVIEW_REQUIRED)
        result.findings.append(
            _finding(
                "faculty_possible_shared_by_name",
                str(get(cfg, "strength.shared_faculty_name_match", "medium")),
                f"{len(matches)} faculty member(s) declared here closely match "
                f"the name and qualification of someone listed for "
                f"{institution}, without a matching faculty id. Possibly the "
                f"same person; two people can share a name. Requires manual "
                f"verification.",
                {
                    "other_institution": institution,
                    "match_basis": "normalised_name+qualification",
                    "min_name_similarity": min_similarity,
                    "count": len(matches),
                    "examples": matches[:max_examples],
                },
                confidence="medium",
            )
        )

    if duplicates_within:
        result.findings.append(
            _finding(
                "faculty_duplicate_entry",
                "medium",
                f"{len(duplicates_within)} faculty id(s) appear more than once "
                f"in the declared list, so the same person may be counted twice.",
                {"faculty_ids": duplicates_within[:max_examples]},
                confidence="high",
            )
        )

    not_in_roster: list[dict[str, Any]] = []
    if aicte.available and aicte.people:
        roster_ids = {str(p.faculty_id).strip().upper() for p in aicte.people if p.faculty_id}
        for person in declared.people:
            key = (person.faculty_id or "").strip().upper()
            if not key or key not in roster_ids:
                not_in_roster.append({"faculty_id": person.faculty_id, "name": person.name, "department": person.department})
        if not_in_roster:
            threshold = int(get(cfg, "strength.not_in_roster_medium_count", 3))
            strength = "medium" if len(not_in_roster) > threshold else str(get(cfg, "strength.not_in_roster", "weak"))
            result.findings.append(
                _finding(
                    "faculty_not_in_records",
                    strength,
                    f"{len(not_in_roster)} declared faculty member(s) are not on "
                    f"the AICTE roster for this institution (missing or "
                    f"unmatched faculty id). Recent joiners are a common honest "
                    f"cause. Requires manual verification.",
                    {"count": len(not_in_roster), "examples": not_in_roster[:max_examples], "aicte_record": aicte.reference},
                    confidence="medium",
                )
            )

    result.identity = {
        "declared_people": len(declared.people),
        "people_compared_against": len(pool),
        "shared_by_id": {k: len(v) for k, v in id_matches.items()},
        "shared_by_name": {k: len(v) for k, v in name_matches.items()},
        "duplicate_ids_within_list": duplicates_within,
        "not_in_roster": len(not_in_roster),
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _finding(
    type_: str,
    strength: str,
    reason: str,
    evidence: dict[str, Any],
    confidence: str | None = None,
) -> dict[str, Any]:
    return {
        "type": type_,
        "strength": strength,
        "reason": reason,
        "evidence": evidence,
        "confidence": confidence,
    }


def _label(source: str) -> str:
    return {
        "declared": "declared faculty list",
        "website": "institution website snapshot",
        "aicte": "AICTE records (mock)",
    }.get(source, source)


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_of(value: Any) -> date:
    if isinstance(value, date):
        return value
    if value:
        parsed = _parse_date(str(value))
        if parsed:
            return parsed
    return datetime.now(timezone.utc).date()
