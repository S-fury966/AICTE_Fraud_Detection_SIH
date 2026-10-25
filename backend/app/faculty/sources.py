"""The three faculty sources, read into one shape.

- **declared**: the CSV the institution uploaded.
- **website**: a synthetic local HTML snapshot of the institution's faculty
  page. Read from the configured snapshot directory only, never fetched - the
  same rule as the issuer URL: the thing being checked never chooses the
  source it is checked against.
- **aicte**: the mock AICTE records file.

Every source carries its capture date, because a count is a fact about a moment
and two sources captured months apart can disagree honestly.

No LLM here. The synthetic pages are regular enough for a deterministic parser,
and a parser's misreading is reproducible where a model's is not.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..core.config import get


def repo_root() -> Path:
    # backend/app/faculty/sources.py -> repository root
    return Path(__file__).resolve().parents[3]


@dataclass
class Person:
    name: str
    department: str | None
    faculty_id: str | None = None
    designation: str | None = None
    qualification: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "faculty_id": self.faculty_id,
            "name": self.name,
            "department": self.department,
            "designation": self.designation,
            "qualification": self.qualification,
        }


@dataclass
class Source:
    """One source's reading: who it lists, per department, and when."""

    name: str  # declared | website | aicte
    available: bool
    captured_on: str | None = None
    # Where it came from, for the evidence link. A path or a record id, never
    # page content.
    reference: str | None = None
    people: list[Person] = field(default_factory=list)
    # Counts per canonical department. For AICTE these are the sanctioned
    # counts in the record, which may differ from the length of the roster.
    counts: dict[str, int] = field(default_factory=dict)
    unavailable_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "captured_on": self.captured_on,
            "reference": self.reference,
            "counts": dict(sorted(self.counts.items())),
            "total": sum(self.counts.values()),
            "people_count": len(self.people),
            "unavailable_reason": self.unavailable_reason,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def normalise_text(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9& ]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def canonical_department(raw: str | None, cfg: dict[str, Any]) -> str | None:
    """Map a department spelling to one canonical name via config aliases."""
    if raw is None or not str(raw).strip():
        return None
    cleaned = normalise_text(raw)
    cleaned = re.sub(r"^(department|dept) of ", "", cleaned)
    aliases = get(cfg, "departments.aliases", {}) or {}
    for canonical, names in aliases.items():
        candidates = {normalise_text(canonical)} | {
            normalise_text(name) for name in (names or [])
        }
        if cleaned in candidates:
            return str(canonical)
    # Uncovered: compared under its own name, title-cased for display.
    return str(raw).strip()


def normalise_person_name(raw: str, cfg: dict[str, Any]) -> str:
    titles = {normalise_text(t) for t in (get(cfg, "identity.strip_titles", []) or [])}
    words = [w for w in normalise_text(raw.replace(".", " ")).split() if w not in titles]
    return " ".join(words)


def name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


# --------------------------------------------------------------------------
# AICTE records (mock)
# --------------------------------------------------------------------------


def load_records(cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The whole mock records file, or (None, reason)."""
    raw = str(get(cfg, "sources.aicte_records_path", "data/demo/aicte_records.json"))
    path = Path(raw) if Path(raw).is_absolute() else repo_root() / raw
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"AICTE records file not found at {raw}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"AICTE records file unreadable: {type(exc).__name__}"


def find_institution_record(
    records: dict[str, Any], institution_name: str, cfg: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The record for this institution, plus how it was matched."""
    target = normalise_text(institution_name)
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for record in records.get("institutions", []) or []:
        candidate = normalise_text(record.get("name"))
        if candidate == target:
            return record, {"method": "exact", "score": 1.0}
        score = name_similarity(candidate, target)
        if score > best[0]:
            best = (score, record)
    threshold = float(get(cfg, "institution_matching.min_name_similarity", 0.9))
    if best[1] is not None and best[0] >= threshold:
        return best[1], {
            "method": "fuzzy",
            "score": round(best[0], 4),
            "matched_name": best[1].get("name"),
        }
    return None, {"method": "none", "best_score": round(best[0], 4)}


def aicte_source(record: dict[str, Any] | None, cfg: dict[str, Any], reason: str | None = None) -> Source:
    if record is None:
        return Source(
            name="aicte",
            available=False,
            unavailable_reason=reason or "no AICTE record for this institution",
        )
    counts: dict[str, int] = {}
    for department, count in (record.get("departments") or {}).items():
        canonical = canonical_department(department, cfg)
        if canonical:
            counts[canonical] = counts.get(canonical, 0) + int(count)
    people = [
        Person(
            name=str(entry.get("name", "")),
            department=canonical_department(entry.get("department"), cfg),
            faculty_id=entry.get("faculty_id"),
            designation=entry.get("designation"),
            qualification=entry.get("qualification"),
        )
        for entry in record.get("faculty", []) or []
    ]
    return Source(
        name="aicte",
        available=True,
        captured_on=record.get("captured_on"),
        reference=str(record.get("aicte_id")),
        people=people,
        counts=counts,
    )


# --------------------------------------------------------------------------
# Website snapshot (synthetic, local)
# --------------------------------------------------------------------------


class _FacultyPageParser(HTMLParser):
    """Departments are headings; people are list items or table rows under them.

    Deliberately generic: it does not depend on class names, so a page that
    lays its list out as a table and one that uses bullets read the same.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.capture_date: str | None = None
        self.departments: dict[str, list[str]] = {}
        self._current: str | None = None
        self._in_heading = False
        self._heading_text: list[str] = []
        self._in_item = False
        self._item_text: list[str] = []
        self._in_cell = False
        self._row_cells: list[str] = []
        self._cell_text: list[str] = []
        self._header_row = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") in ("capture-date", "snapshot-date"):
            self.capture_date = attributes.get("content")
        elif tag in ("h2", "h3"):
            self._in_heading = True
            self._heading_text = []
        elif tag == "li" and self._current:
            self._in_item = True
            self._item_text = []
        elif tag == "tr" and self._current:
            self._row_cells = []
            self._header_row = False
        elif tag in ("td", "th") and self._current:
            self._in_cell = True
            self._cell_text = []
            if tag == "th":
                self._header_row = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h2", "h3") and self._in_heading:
            self._in_heading = False
            heading = " ".join("".join(self._heading_text).split())
            self._current = heading or None
            if self._current:
                self.departments.setdefault(self._current, [])
        elif tag == "li" and self._in_item:
            self._in_item = False
            text = " ".join("".join(self._item_text).split())
            if text and self._current:
                self.departments[self._current].append(text)
        elif tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._row_cells.append(" ".join("".join(self._cell_text).split()))
        elif tag == "tr" and self._current is not None:
            if self._row_cells and not self._header_row and any(self._row_cells):
                self.departments[self._current].append(" | ".join(self._row_cells))
            self._row_cells = []

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_text.append(data)
        if self._in_item:
            self._item_text.append(data)
        if self._in_cell:
            self._cell_text.append(data)


def website_source(
    record: dict[str, Any] | None, cfg: dict[str, Any]
) -> Source:
    if record is None or not record.get("website_snapshot"):
        return Source(
            name="website",
            available=False,
            unavailable_reason="no website snapshot is on file for this institution",
        )

    snapshot_dir_raw = str(get(cfg, "sources.website_snapshot_dir", "data/demo/websites"))
    snapshot_dir = (
        Path(snapshot_dir_raw)
        if Path(snapshot_dir_raw).is_absolute()
        else repo_root() / snapshot_dir_raw
    ).resolve()
    candidate = (snapshot_dir / str(record["website_snapshot"])).resolve()
    # Refuse anything outside the configured directory, however the record
    # spells it ("../../etc", an absolute path, a URL).
    if snapshot_dir not in candidate.parents:
        return Source(
            name="website",
            available=False,
            reference=str(record["website_snapshot"]),
            unavailable_reason="website snapshot path is outside the configured directory; refused",
        )
    try:
        html = candidate.read_text(encoding="utf-8")
    except OSError:
        return Source(
            name="website",
            available=False,
            reference=str(record["website_snapshot"]),
            unavailable_reason="website snapshot file not found",
        )

    parser = _FacultyPageParser()
    parser.feed(html)
    people: list[Person] = []
    counts: dict[str, int] = {}
    for heading, entries in parser.departments.items():
        canonical = canonical_department(heading, cfg)
        if not canonical or not entries:
            continue
        counts[canonical] = counts.get(canonical, 0) + len(entries)
        for entry in entries:
            name = re.split(r"[|,]", entry)[0].strip()
            people.append(Person(name=name, department=canonical))

    return Source(
        name="website",
        available=bool(counts),
        captured_on=parser.capture_date,
        reference=str(record["website_snapshot"]),
        people=people,
        counts=counts,
        unavailable_reason=None if counts else "no faculty listing found in the snapshot",
    )


# --------------------------------------------------------------------------
# Declared list (uploaded CSV)
# --------------------------------------------------------------------------


def declared_source(path: Path, cfg: dict[str, Any], captured_on: date | None) -> Source:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return Source(name="declared", available=False, unavailable_reason=f"file unreadable: {exc}")

    text = raw.decode("utf-8-sig", errors="replace")
    if path.suffix.lower() == ".json":
        try:
            rows = json.loads(text)
            if isinstance(rows, dict):
                rows = rows.get("faculty") or []
        except json.JSONDecodeError:
            return Source(name="declared", available=False, unavailable_reason="declared list is not valid JSON")
    else:
        rows = list(csv.DictReader(io.StringIO(text)))

    if not rows:
        return Source(name="declared", available=False, unavailable_reason="declared list has no rows")

    columns = get(cfg, "declared_file.columns", {}) or {}

    def pick(row: dict[str, Any], key: str) -> str | None:
        lowered = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
        for candidate in columns.get(key, [key]):
            value = lowered.get(str(candidate).lower())
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    people: list[Person] = []
    counts: dict[str, int] = {}
    skipped = 0
    for row in rows:
        name = pick(row, "name")
        department = canonical_department(pick(row, "department"), cfg)
        if not name or not department:
            skipped += 1
            continue
        people.append(
            Person(
                name=name,
                department=department,
                faculty_id=pick(row, "faculty_id"),
                designation=pick(row, "designation"),
                qualification=pick(row, "qualification"),
            )
        )
        counts[department] = counts.get(department, 0) + 1

    notes = [f"{skipped} row(s) without a name or department were not counted"] if skipped else []
    return Source(
        name="declared",
        available=bool(people),
        captured_on=captured_on.isoformat() if captured_on else None,
        reference=path.name,
        people=people,
        counts=counts,
        notes=notes,
        unavailable_reason=None if people else "no usable rows in the declared list",
    )
