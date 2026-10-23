"""Build the synthetic issuer registry. Deterministic: same output every run.

    python -m app.mock_issuer.seed

Every institution, accrediting body and domain here is invented, and all
domains use the reserved .test TLD. Nothing in this file is derived from a
real accreditation record.

Determinism matters for two reasons: a demo that reshuffles its own data
between runs cannot be reasoned about, and the evaluation set needs the same
registry every time or detection rates are not comparable.

The deliberate cases — expired, revoked, suspended, pending, and a legitimate
name variant — are written out by hand rather than sampled, because a test
suite needs to know exactly which record carries which condition.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

SEED = 20260124
REGISTRY_VERSION = "issuer-registry-v1"

REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "demo" / "issuer_registry.json"
)

ISSUING_BODY = "National Board of Technical Accreditation (Demo)"
SECOND_BODY = "Regional Council for Technical Education (Demo)"

# The certificate that steps 1-3 already use. Held as constants so the
# fixtures and the registry cannot drift apart.
ANCHOR_CERTIFICATE_ID = "NBTA/DEMO/2024/00417"
ANCHOR_INSTITUTION = "Shantiniketan Institute of Technology"
ANCHOR_PROGRAMME = "Bachelor of Technology, Computer Science and Engineering"
ANCHOR_VALID_FROM = "2024-07-01"
ANCHOR_VALID_TO = "2027-06-30"

# Ids of the seeded special cases, exported so tests name them rather than
# rediscovering them by scanning the registry.
EXPIRED_ID = "NBTA/DEMO/2019/00031"
REVOKED_ID = "NBTA/DEMO/2023/00204"
SUSPENDED_ID = "NBTA/DEMO/2023/00205"
PENDING_ID = "NBTA/DEMO/2025/00501"
NAME_VARIANT_ID = "NBTA/DEMO/2024/00422"
# Same programme and dates as the anchor, different institution: the case
# where a QR points at a real record that is not this document's.
OTHER_INSTITUTION_ID = "NBTA/DEMO/2024/00418"

_INSTITUTIONS = [
    "Shantiniketan Institute of Technology",
    "Gomati Valley College of Engineering",
    "Harita Institute of Science and Technology",
    "Nilgiri College of Engineering and Technology",
    "Saraswati Institute of Technology and Management",
    "Konkan Coast Institute of Engineering",
    "Vindhya Institute of Technology",
    "Chenab Valley College of Technology",
    "Deccan Plateau Institute of Engineering",
    "Malabar Institute of Technology",
    "Brahmaputra College of Engineering",
    "Aravalli Institute of Technology and Research",
]

_PROGRAMMES = [
    "Bachelor of Technology, Computer Science and Engineering",
    "Bachelor of Technology, Mechanical Engineering",
    "Bachelor of Technology, Civil Engineering",
    "Bachelor of Technology, Electronics and Communication Engineering",
    "Master of Technology, Structural Engineering",
    "Bachelor of Technology, Information Technology",
]

_GRADES = ["A++", "A+", "A", "B++", "B+"]


def _record(
    certificate_id: str,
    institution_name: str,
    programmes: list[str],
    grade: str,
    valid_from: str,
    valid_to: str,
    issuer_status: str = "active",
    record_updated: str = "2026-01-10",
    issuing_body: str = ISSUING_BODY,
) -> dict[str, Any]:
    return {
        "certificate_id": certificate_id,
        "institution_name": institution_name,
        "issuing_body": issuing_body,
        "programmes": programmes,
        "grade": grade,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "issuer_status": issuer_status,
        "record_updated": record_updated,
    }


def build_records() -> list[dict[str, Any]]:
    """Every registry record, in a fixed order."""
    rng = random.Random(SEED)
    records: list[dict[str, Any]] = []

    # 1. The anchor: an exact match for the existing fixtures, so the whole
    #    chain from step 1 to step 4 verifies end to end.
    records.append(
        _record(
            ANCHOR_CERTIFICATE_ID,
            ANCHOR_INSTITUTION,
            [ANCHOR_PROGRAMME],
            "A+",
            ANCHOR_VALID_FROM,
            ANCHOR_VALID_TO,
        )
    )

    # 2. A real record for a different institution, same programme and dates.
    #    A QR pointing here from a Shantiniketan document is the "resolves to
    #    someone else's record" case: a valid lookup, wrong institution.
    records.append(
        _record(
            OTHER_INSTITUTION_ID,
            "Gomati Valley College of Engineering",
            [ANCHOR_PROGRAMME],
            "A",
            ANCHOR_VALID_FROM,
            ANCHOR_VALID_TO,
        )
    )

    # 3. Expired per the issuer's own validity window.
    records.append(
        _record(
            EXPIRED_ID,
            "Harita Institute of Science and Technology",
            ["Bachelor of Technology, Civil Engineering"],
            "B++",
            "2016-07-01",
            "2019-06-30",
            record_updated="2019-07-02",
        )
    )

    # 4. Revoked, and 5. suspended. Same institution, so a reviewer sees that
    #    issuer_status is a property of the certificate, not the college.
    records.append(
        _record(
            REVOKED_ID,
            "Nilgiri College of Engineering and Technology",
            ["Bachelor of Technology, Mechanical Engineering"],
            "B+",
            "2023-07-01",
            "2028-06-30",
            issuer_status="revoked",
            record_updated="2025-03-18",
        )
    )
    records.append(
        _record(
            SUSPENDED_ID,
            "Nilgiri College of Engineering and Technology",
            ["Bachelor of Technology, Civil Engineering"],
            "B++",
            "2023-07-01",
            "2028-06-30",
            issuer_status="suspended",
            record_updated="2026-02-02",
        )
    )

    # 6. The issuer has the application but has not finished processing it.
    #    The service answers 202 for this id.
    records.append(
        _record(
            PENDING_ID,
            "Saraswati Institute of Technology and Management",
            ["Bachelor of Technology, Information Technology"],
            "A",
            "2025-07-01",
            "2030-06-30",
            issuer_status="pending",
            record_updated="2026-09-30",
        )
    )

    # 7. A legitimate name variant: the issuer holds the abbreviated form that
    #    the certificate prints in full. Not a mismatch, and the comparison
    #    has to know that.
    records.append(
        _record(
            NAME_VARIANT_ID,
            "Konkan Coast Inst. of Engg.",
            ["Bachelor of Technology, Electronics and Communication Engineering"],
            "A",
            "2024-07-01",
            "2029-06-30",
        )
    )

    # The remaining institutions get 1-3 certificates each, sampled from the
    # seeded generator so the registry has realistic bulk without hand-writing
    # every row.
    used_ids = {record["certificate_id"] for record in records}
    counter = 600
    for institution in _INSTITUTIONS[6:]:
        for _ in range(rng.randint(1, 3)):
            counter += rng.randint(1, 9)
            certificate_id = f"NBTA/DEMO/2024/{counter:05d}"
            if certificate_id in used_ids:
                continue
            used_ids.add(certificate_id)
            start_year = rng.choice([2022, 2023, 2024])
            records.append(
                _record(
                    certificate_id,
                    institution,
                    rng.sample(_PROGRAMMES, rng.randint(1, 2)),
                    rng.choice(_GRADES),
                    f"{start_year}-07-01",
                    f"{start_year + 5}-06-30",
                    issuing_body=rng.choice([ISSUING_BODY, SECOND_BODY]),
                    record_updated="2026-01-10",
                )
            )

    return records


def build_registry() -> dict[str, Any]:
    records = build_records()
    return {
        "registry_version": REGISTRY_VERSION,
        "seed": SEED,
        "note": (
            "Synthetic demo data. Every institution, accrediting body and "
            "domain is fictional; all domains use the reserved .test TLD. "
            "This registry accredits nothing."
        ),
        "issuer_name": ISSUING_BODY,
        "issuer_domain": "verify.nbta-demo.test",
        "pending_certificate_ids": [PENDING_ID],
        "record_count": len(records),
        "records": records,
    }


def registry_hash(registry: dict[str, Any]) -> str:
    """A hash of the registry contents, for the audit record.

    Over the canonical JSON rather than the file bytes, so reformatting the
    file does not look like the data changed.
    """
    canonical = json.dumps(registry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_registry(path: Path | None = None) -> Path:
    target = Path(path) if path is not None else REGISTRY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    registry = build_registry()
    target.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target


def main() -> int:
    path = write_registry()
    registry = json.loads(path.read_text(encoding="utf-8"))
    print(f"wrote {registry['record_count']} records to {path}")
    print(f"registry_version: {registry['registry_version']}")
    print(f"registry_sha256:  {registry_hash(registry)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
