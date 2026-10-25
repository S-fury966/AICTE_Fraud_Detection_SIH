"""Faculty count consistency: tolerance, direction, redistribution, identity, sources."""

from __future__ import annotations

import copy
import csv
from pathlib import Path

import pytest

from app.core.config import load_config
from app.faculty import allowed_deviation, check_faculty

INSTITUTION = "Alpha Institute of Technology (Test)"
OTHER = "Beta College of Engineering (Test)"


def _roster(prefix: str, counts: dict[str, int]) -> list[dict]:
    people, n = [], 1
    for department, count in counts.items():
        for _ in range(count):
            people.append(
                {"faculty_id": f"{prefix}-{n:03d}", "name": f"Dr. {prefix} Person {n}", "department": department, "qualification": "PhD"}
            )
            n += 1
    return people


ALPHA_COUNTS = {"Computer Science and Engineering": 12, "Mechanical Engineering": 10}
BETA_COUNTS = {"Computer Science and Engineering": 8}


@pytest.fixture
def records(tmp_path: Path) -> dict:
    sites = tmp_path / "websites"
    sites.mkdir()
    alpha = _roster("ALP", ALPHA_COUNTS)
    _write_site(sites / "alpha.html", alpha, "2026-10-01")
    return {
        "records_version": "test",
        "institutions": [
            {"aicte_id": "A-1", "name": INSTITUTION, "captured_on": "2026-09-15", "website_snapshot": "alpha.html", "departments": dict(ALPHA_COUNTS), "faculty": alpha, "location": {"lat": 10.0, "lon": 76.0}},
            {"aicte_id": "B-1", "name": OTHER, "captured_on": "2026-09-15", "website_snapshot": None, "departments": dict(BETA_COUNTS), "faculty": _roster("BET", BETA_COUNTS)},
        ],
    }


@pytest.fixture
def cfg(tmp_path: Path) -> dict:
    config = copy.deepcopy(load_config("faculty_checks"))
    config["sources"]["website_snapshot_dir"] = str(tmp_path / "websites")
    return config


def _write_site(path: Path, people: list[dict], captured: str) -> None:
    by_dept: dict[str, list[dict]] = {}
    for p in people:
        by_dept.setdefault(p["department"], []).append(p)
    body = "".join(
        f"<h2>{d}</h2><ul>" + "".join(f"<li>{p['name']}, Professor</li>" for p in ps) + "</ul>" for d, ps in by_dept.items()
    )
    path.write_text(f'<html><head><meta name="capture-date" content="{captured}"></head><body>{body}</body></html>')


def _declared(tmp_path: Path, people: list[dict]) -> Path:
    path = tmp_path / "declared.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["faculty_id", "name", "department", "qualification"])
        writer.writeheader()
        for p in people:
            writer.writerow({k: p.get(k) for k in writer.fieldnames})
    return path


def _types(result) -> dict[str, str]:
    return {f["type"]: f["strength"] for f in result.findings}


def test_tolerance_is_max_of_absolute_and_relative(cfg):
    assert allowed_deviation(5, cfg) == 1
    assert allowed_deviation(12, cfg) == 2
    assert allowed_deviation(40, cfg) == 4


def test_matching_sources_verify_with_no_findings(tmp_path, records, cfg):
    path = _declared(tmp_path, records["institutions"][0]["faculty"])
    result = check_faculty(path, INSTITUTION, config=cfg, records=records, as_of="2026-10-20")
    assert result.status == "verified"
    assert result.findings == []
    assert all(d["status"] == "verified" for d in result.departments)


def test_one_extra_inside_tolerance_is_not_a_count_mismatch(tmp_path, records, cfg):
    people = records["institutions"][0]["faculty"] + [{"faculty_id": "NEW-1", "name": "Dr. New Joiner", "department": "CSE", "qualification": "PhD"}]
    result = check_faculty(_declared(tmp_path, people), INSTITUTION, config=cfg, records=records, as_of="2026-10-20")
    types = _types(result)
    assert "faculty_count_exceeds_records" not in types
    # The newcomer is still visible, at weak strength only.
    assert types.get("faculty_not_in_records") == "weak"


def test_inflation_beyond_ratio_is_strong(tmp_path, records, cfg):
    extra = [{"faculty_id": f"X-{i}", "name": f"Dr. Extra {i}", "department": "Computer Science", "qualification": "M.Tech"} for i in range(4)]
    result = check_faculty(_declared(tmp_path, records["institutions"][0]["faculty"] + extra), INSTITUTION, config=cfg, records=records, as_of="2026-10-20")
    types = _types(result)
    assert types["faculty_count_exceeds_records"] == "strong"
    assert types["faculty_count_differs_from_website"] == "medium"
    assert result.status == "mismatch_detected"
    finding = next(f for f in result.findings if f["type"] == "faculty_count_exceeds_records")
    assert finding["evidence"]["declared"] == 16 and finding["evidence"]["aicte"] == 12


def test_redistribution_is_caught_although_the_total_matches(tmp_path, records, cfg):
    people = list(records["institutions"][0]["faculty"])
    mech = [p for p in people if p["department"] == "Mechanical Engineering"][:4]
    for p in mech:
        people.remove(p)
        people.append({**p, "department": "Computer Science and Engineering"})
    result = check_faculty(_declared(tmp_path, people), INSTITUTION, config=cfg, records=records, as_of="2026-10-20")
    assert result.totals["within_tolerance"] is True
    assert _types(result)["faculty_counts_redistributed"] == "strong"


def test_shared_faculty_id_with_another_institution_is_strong(tmp_path, records, cfg):
    people = records["institutions"][0]["faculty"][:-2] + records["institutions"][1]["faculty"][:2]
    result = check_faculty(_declared(tmp_path, people), INSTITUTION, config=cfg, records=records, as_of="2026-10-20")
    shared = next(f for f in result.findings if f["type"] == "faculty_shared_across_institutions")
    assert shared["strength"] == "strong"
    assert shared["evidence"]["other_institution"] == OTHER
    assert shared["evidence"]["count"] == 2


def test_name_match_without_id_is_medium_and_needs_the_same_qualification(tmp_path, records, cfg):
    beta_person = records["institutions"][1]["faculty"][0]
    lookalike = {"faculty_id": None, "name": beta_person["name"].replace("Dr. ", "Prof. "), "department": "CSE", "qualification": "PhD"}
    result = check_faculty(_declared(tmp_path, records["institutions"][0]["faculty"] + [lookalike]), INSTITUTION, config=cfg, records=records, as_of="2026-10-20")
    assert _types(result)["faculty_possible_shared_by_name"] == "medium"

    lookalike["qualification"] = "M.Sc"
    result = check_faculty(_declared(tmp_path, records["institutions"][0]["faculty"] + [lookalike]), INSTITUTION, config=cfg, records=records, as_of="2026-10-20")
    assert "faculty_possible_shared_by_name" not in _types(result)


def test_a_website_path_outside_the_snapshot_directory_is_refused(tmp_path, records, cfg):
    records["institutions"][0]["website_snapshot"] = "../../etc/passwd"
    result = check_faculty(_declared(tmp_path, records["institutions"][0]["faculty"]), INSTITUTION, config=cfg, records=records, as_of="2026-10-20")
    assert result.sources["website"]["available"] is False
    assert "refused" in result.sources["website"]["unavailable_reason"]


def test_a_stale_source_is_flagged_weak_and_still_compared(tmp_path, records, cfg):
    path = _declared(tmp_path, records["institutions"][0]["faculty"])
    result = check_faculty(path, INSTITUTION, config=cfg, records=records, as_of="2027-09-01")
    stale = [f for f in result.findings if f["type"] == "faculty_source_stale"]
    assert stale and all(f["strength"] == "weak" for f in stale)
    assert result.departments  # comparison still ran


def test_unknown_institution_makes_no_count_comparison(tmp_path, records, cfg):
    people = _roster("NEW", {"Civil Engineering": 5})
    result = check_faculty(_declared(tmp_path, people), "Completely Different Name", config=cfg, records=records, as_of="2026-10-20")
    assert result.status == "source_unavailable"
    assert result.sources["aicte"]["available"] is False
    assert "faculty_count_exceeds_records" not in _types(result)
