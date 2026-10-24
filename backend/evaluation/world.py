"""The synthetic world: fictional institutions and issuing bodies.

Written to `data/eval/world.json` so the faculty and photo steps can extend
the *same* world rather than inventing a second one. A certificate detector
measured against twelve institutions and a faculty detector measured against a
different twelve cannot be combined into an application-level number.

Every name, city and domain is invented. Every domain sits under the reserved
`.test` TLD, which cannot resolve. No real accreditation body appears here, and
nothing in the rendering draws a real logo, emblem or seal - the seals are
abstract shapes, by deliberate choice, because imitating a government emblem is
not something a demo dataset should contain at all.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Deliberately invented. Each is a plausible-sounding Indian technical
# institute name built from geography that does not name a real college.
_INSTITUTION_STEMS = [
    ("Shantiniketan", "Bolpur"),
    ("Konkan Coast", "Ratnagiri"),
    ("Vindhya", "Rewa"),
    ("Deccan Plateau", "Solapur"),
    ("Brahmaputra Valley", "Tezpur"),
    ("Aravalli", "Alwar"),
    ("Chambal", "Morena"),
    ("Nilgiri Slopes", "Coonoor"),
    ("Sundarban", "Canning"),
    ("Thar Frontier", "Barmer"),
    ("Kaveri Delta", "Kumbakonam"),
    ("Satpura", "Betul"),
    ("Gandak", "Motihari"),
    ("Malwa Highlands", "Mandsaur"),
]

_INSTITUTION_KINDS = [
    "Institute of Technology",
    "College of Engineering",
    "Technical University",
    "Institute of Engineering and Technology",
    "College of Technology",
]

_DEPARTMENTS = [
    "Computer Science and Engineering",
    "Electronics and Communication Engineering",
    "Mechanical Engineering",
    "Civil Engineering",
    "Electrical Engineering",
    "Information Technology",
    "Chemical Engineering",
]

_PROGRAMMES = [
    "Bachelor of Technology, Computer Science and Engineering",
    "Bachelor of Technology, Mechanical Engineering",
    "Bachelor of Technology, Civil Engineering",
    "Bachelor of Technology, Electronics and Communication Engineering",
    "Master of Technology, Structural Engineering",
    "Master of Technology, Computer Science and Engineering",
]

_GRADES = ["A", "A+", "A++", "B", "B+"]


@dataclass
class IssuingBody:
    key: str
    name: str
    abbreviation: str
    domain: str
    id_prefix: str
    uses_qr: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Institution:
    id: str
    name: str
    # A shortened form a real applicant might write instead. Used by the
    # abbreviation hard negative: "Inst. of Tech." must not be a mismatch.
    abbreviated_name: str
    city: str
    departments: list[str] = field(default_factory=list)
    # The .test website the faculty detector will scrape in a later step.
    website: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class World:
    config_version: str
    seed: int
    institutions: list[Institution]
    issuing_bodies: list[IssuingBody]
    programmes: list[str]
    grades: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "seed": self.seed,
            "note": (
                "Entirely fictional. Every institution, issuing body and "
                "domain is invented; all domains use the reserved .test TLD "
                "and cannot resolve. No real accreditation body, logo, emblem "
                "or seal is represented."
            ),
            "institutions": [i.to_dict() for i in self.institutions],
            "issuing_bodies": [b.to_dict() for b in self.issuing_bodies],
            "programmes": list(self.programmes),
            "grades": list(self.grades),
        }

    def institution(self, institution_id: str) -> Institution:
        for entry in self.institutions:
            if entry.id == institution_id:
                return entry
        raise KeyError(institution_id)

    def body(self, key: str) -> IssuingBody:
        for entry in self.issuing_bodies:
            if entry.key == key:
                return entry
        raise KeyError(key)


def _abbreviate(name: str) -> str:
    """A shortened form a harried applicant might actually type.

    The point of the hard negative is that this is the *same* institution, so
    the abbreviation has to be one a human would plausibly write, not a random
    truncation.
    """
    replacements = [
        ("Institute of Engineering and Technology", "Inst. of Engg. & Tech."),
        ("Institute of Technology", "Inst. of Tech."),
        ("College of Engineering", "Coll. of Engg."),
        ("College of Technology", "Coll. of Tech."),
        ("Technical University", "Tech. Univ."),
    ]
    for long, short in replacements:
        if name.endswith(long):
            return name[: -len(long)] + short
    return name


def build_world(config: dict[str, Any]) -> World:
    """Build the world deterministically from the configured seed."""
    seed = int(config.get("seed", 0))
    count = int((config.get("world") or {}).get("institution_count", 12))
    rng = random.Random(f"world-{seed}")

    institutions: list[Institution] = []
    for index in range(count):
        stem, city = _INSTITUTION_STEMS[index % len(_INSTITUTION_STEMS)]
        kind = _INSTITUTION_KINDS[index % len(_INSTITUTION_KINDS)]
        name = f"{stem} {kind}"
        slug = stem.lower().replace(" ", "-")
        departments = rng.sample(_DEPARTMENTS, k=rng.randint(3, 5))
        institutions.append(
            Institution(
                id=f"INST-{index + 1:03d}",
                name=name,
                abbreviated_name=_abbreviate(name),
                city=city,
                # Sorted so the same seed gives the same file byte for byte;
                # rng.sample order is stable but sorting removes any doubt.
                departments=sorted(departments),
                website=f"https://www.{slug}-demo.test/faculty",
            )
        )

    bodies = [
        IssuingBody(
            key=str(entry["key"]),
            name=str(entry["name"]),
            abbreviation=str(entry["abbreviation"]),
            domain=str(entry["domain"]),
            id_prefix=str(entry["id_prefix"]),
            uses_qr=bool(entry.get("uses_qr", True)),
        )
        for entry in (config.get("world") or {}).get("issuing_bodies", [])
    ]

    return World(
        config_version=str(config.get("config_version", "unknown")),
        seed=seed,
        institutions=institutions,
        issuing_bodies=bodies,
        programmes=list(_PROGRAMMES),
        grades=list(_GRADES),
    )


def write_world(world: World, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys and a fixed indent, so byte-identical output is testable.
    path.write_text(
        json.dumps(world.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return path
