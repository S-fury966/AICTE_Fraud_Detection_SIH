"""Generate the demo dataset: certificates, faculty lists, websites, records, photos.

    python -m demo.generate [--out data/demo]

Deterministic from `world.SEED`, except the two signed certificates: a PDF
signature embeds a signing time and a throwaway key generated in memory, so
their bytes differ between runs (stated, as in step 7, not hidden).

Writes:
    data/demo/aicte_records.json        mock AICTE records (per department + roster)
    data/demo/websites/<key>.html       synthetic website snapshots
    data/demo/applications/<key>/...    certificate.pdf, faculty.csv, photos
    data/demo/labels.json               what was injected, from the recipes
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import photos as photo_kit
from . import world

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fmt_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d %B %Y")


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------


class NameBook:
    """Unique invented names, so no two institutions share a person by chance."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used: set[str] = set()

    def next(self) -> str:
        for _ in range(10_000):
            name = f"{self.rng.choice(world.FIRST_NAMES)} {self.rng.choice(world.LAST_NAMES)}"
            if name not in self.used:
                self.used.add(name)
                return name
        raise RuntimeError("name space exhausted")


def build_rosters(rng: random.Random, names: NameBook) -> dict[str, list[dict[str, Any]]]:
    rosters: dict[str, list[dict[str, Any]]] = {}
    for index, institution in enumerate(world.INSTITUTIONS, start=1):
        people: list[dict[str, Any]] = []
        seq = 1
        for department, count in institution["departments"].items():
            for position in range(count):
                # Cadre follows the mock 1:2:6 norm by position, and senior
                # designations hold a doctorate, so a clean roster genuinely
                # meets the configured norms instead of passing by luck.
                fraction = position / max(count, 1)
                designation = "Professor" if fraction < 1 / 9 else "Associate Professor" if fraction < 3 / 9 else "Assistant Professor"
                qualification = "PhD" if designation != "Assistant Professor" else rng.choice(world.QUALIFICATIONS)
                title = "Dr." if qualification == "PhD" else rng.choice(("Prof.", "Mr.", "Ms."))
                people.append(
                    {
                        "faculty_id": f"FAC-DEMO-{index:02d}{seq:03d}",
                        "name": f"{title} {names.next()}",
                        "department": department,
                        "designation": designation,
                        "qualification": qualification,
                    }
                )
                seq += 1
        rosters[institution["key"]] = people
    return rosters


def declared_list(
    scenario: dict[str, Any],
    rosters: dict[str, list[dict[str, Any]]],
    rng: random.Random,
    names: NameBook,
) -> list[dict[str, Any]]:
    people = [dict(p) for p in rosters[scenario["key"]]]
    spec = scenario["faculty"]
    if spec["mode"] == "adjust":
        new_seq = 1
        for department, delta in spec["adjust"].items():
            if delta > 0:
                for _ in range(delta):
                    people.append(
                        {
                            # Not on any roster: a person AICTE has no record of.
                            "faculty_id": f"FAC-DEMO-9{world.INSTITUTIONS.index(next(i for i in world.INSTITUTIONS if i['key'] == scenario['key'])):01d}{new_seq:03d}",
                            "name": f"Dr. {names.next()}",
                            "department": department,
                            "designation": "Assistant Professor",
                            "qualification": "M.Tech",
                        }
                    )
                    new_seq += 1
            else:
                in_dept = [p for p in people if p["department"] == department]
                for person in in_dept[delta:]:
                    people.remove(person)
    elif spec["mode"] == "borrow":
        donors = [p for p in rosters[spec["from"]] if p["department"] == "Computer Science and Engineering"][: spec["count"]]
        own_cse = [p for p in people if p["department"] == "Computer Science and Engineering"]
        for person in own_cse[-spec["count"] :]:
            people.remove(person)
        people.extend(dict(d) for d in donors)
    return people


# --------------------------------------------------------------------------
# Sources: AICTE records and website snapshots
# --------------------------------------------------------------------------


def _intake(institution: dict[str, Any]) -> dict[str, int]:
    """About 15 students per recorded faculty member, rounded to a batch of 30."""
    intake = {}
    for department, count in institution["departments"].items():
        if department == "Applied Sciences and Humanities":
            continue
        intake[department] = max(30, round(count * 15 / 30) * 30)
    intake.update(institution.get("intake_override", {}))
    return intake


def write_records(out: Path, rosters: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    institutions = []
    for index, institution in enumerate(world.INSTITUTIONS, start=1):
        institutions.append(
            {
                "aicte_id": f"AICTE-DEMO-{index:04d}",
                "name": institution["name"],
                "city": institution["city"],
                "location": {**institution["location"], "city": institution["city"]},
                "captured_on": world.RECORDS_CAPTURED_ON,
                "website_snapshot": f"{institution['key']}.html",
                "departments": dict(institution["departments"]),
                # Approved intake (mock), for the student:faculty ratio norm.
                # Service departments teach across programmes and have none.
                "intake": _intake(institution),
                "faculty": rosters[institution["key"]],
            }
        )
    records = {
        "records_version": "aicte-records-demo-v1",
        "seed": world.SEED,
        "note": (
            "MOCK AICTE records. Every institution, person, faculty id and "
            "coordinate is invented. No real AICTE system was consulted."
        ),
        "institutions": institutions,
    }
    (out / "aicte_records.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return records


def write_website(out: Path, institution: dict[str, Any], roster: list[dict[str, Any]], captured_on: str, as_table: bool) -> None:
    by_department: dict[str, list[dict[str, Any]]] = {}
    for person in roster:
        by_department.setdefault(person["department"], []).append(person)
    sections = []
    for department, people in by_department.items():
        heading = world.WEBSITE_SPELLING.get(department, department)
        if as_table:
            rows = "\n".join(
                f"        <tr><td>{p['name']}</td><td>{p['designation']}</td><td>{p['qualification']}</td></tr>"
                for p in people
            )
            body = f"      <table>\n        <tr><th>Name</th><th>Designation</th><th>Qualification</th></tr>\n{rows}\n      </table>"
        else:
            items = "\n".join(
                f"        <li>{p['name']}, {p['designation']} ({p['qualification']})</li>" for p in people
            )
            body = f"      <ul>\n{items}\n      </ul>"
        sections.append(f"    <section>\n      <h2>{heading}</h2>\n{body}\n    </section>")
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="capture-date" content="{captured_on}">
    <meta name="note" content="Synthetic snapshot for the SIH 2026 demo. Not a real website.">
    <title>Faculty - {institution['name']}</title>
  </head>
  <body>
    <h1>{institution['name']} - Our Faculty</h1>
{chr(10).join(sections)}
    <footer>Synthetic demo page. Snapshot captured {captured_on}.</footer>
  </body>
</html>
"""
    (out / "websites").mkdir(parents=True, exist_ok=True)
    (out / "websites" / f"{institution['key']}.html").write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------
# Certificates
# --------------------------------------------------------------------------

_CLASSIC = {
    "key": "classic",
    "style": "free_text",
    "heading_size": 15,
    "body_size": 10,
    "accent": [0.12, 0.23, 0.44],
    "seal": "circle",
    "seal_position": "bottom_right",
    "watermark": False,
    "qr_position": "bottom_left",
    "qr_size": 120,
}


def _programme_line(programmes: list[str]) -> str:
    """One printable line: the comma-separated parts, each once.

    A two-programme record joined naively runs past the page edge and is
    clipped out of the text layer. The issuer comparison splits on commas and
    compares sets, so the deduplicated parts are the same claim, shorter.
    """
    parts: list[str] = []
    for programme in programmes:
        for part in programme.split(","):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
    return ", ".join(parts)


def build_certificate(scenario: dict[str, Any], registry: dict[str, Any], signer: Any) -> bytes:
    from evaluation import signing, templates

    spec = scenario["certificate"]
    record = next(r for r in registry["records"] if r["certificate_id"] == spec["record"])
    institution = next(i for i in world.INSTITUTIONS if i["key"] == scenario["key"])
    fields = templates.CertificateFields(
        institution_name=institution["name"] if spec.get("as_institution") else record["institution_name"],
        certificate_number=spec.get("print_number", record["certificate_id"]),
        issuing_body=record["issuing_body"],
        programme=_programme_line(record["programmes"]),
        accreditation_status_or_grade=record.get("grade"),
        valid_from=_fmt_date(record["valid_from"]),
        valid_to=_fmt_date(record["valid_to"]),
        signatory_name="Registrar (Demo)",
    )
    host = spec.get("qr_host", world.GENUINE_HOST)
    # The QR always carries the id the issuer actually holds: an edit to the
    # printed number does not regenerate the QR, which is how it is caught.
    payload = f"https://{host}/c/{quote(record['certificate_id'], safe='')}"
    sign = spec.get("sign")
    pdf = templates.render_pdf(fields, _CLASSIC, payload, "NBTA", uncompressed=(sign == "tampered"))
    if sign:
        pdf = signing.sign(pdf, signer)
        if sign == "tampered":
            before, after = spec["tamper"]
            pdf = signing.tamper_inside_signed_range(pdf, before, after)
    return pdf


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------


def build_photo(scenario: dict[str, Any], spec: dict[str, Any], out_root: Path, rng: random.Random) -> bytes:
    institution = next(i for i in world.INSTITUTIONS if i["key"] == scenario["key"])
    if spec.get("reuse_of"):
        source = out_root / "applications" / spec["reuse_of"].split("/")[0] / "photos" / spec["reuse_of"].split("/")[1]
        from PIL import Image

        original = Image.open(source).convert("RGB")
        return photo_kit.save_jpeg(photo_kit.disguise_reuse(original), None, quality=85)

    image = photo_kit.render_room(int(spec["scene"]), spec.get("kind", "lab"))
    manipulation = spec.get("manipulation")
    if manipulation == "clone":
        image = photo_kit.clone_region(image, int(spec["scene"]))
    elif manipulation == "paste":
        image = photo_kit.paste_foreign_patch(image, int(spec["scene"]))

    profile = spec.get("exif", "camera")
    loc = institution["location"]
    near = (loc["lat"] + rng.uniform(-0.004, 0.004), loc["lon"] + rng.uniform(-0.004, 0.004))
    captured = datetime(2026, 8, 1, 10, 0, 0) + timedelta(days=rng.randint(0, 40), minutes=rng.randint(0, 400))

    if profile == "generated":
        return photo_kit.save_generated_png(
            image,
            "a modern smart classroom with projector, photorealistic, wide angle; "
            "Steps: 30, Sampler: DPM++ 2M, CFG scale: 7, Model: sdxl-demo (synthetic marker)",
        )
    if profile == "none":
        return photo_kit.save_jpeg(image, None)
    if profile == "edited":
        return photo_kit.save_jpeg(image, photo_kit.make_exif(captured, near, software="Adobe Photoshop 25.1 (Windows)"))
    if profile == "elsewhere":
        # ~520 km from the institution, and dated after the application.
        return photo_kit.save_jpeg(image, photo_kit.make_exif(datetime(2027, 1, 15, 11, 30), (12.9716, 77.5946)))
    return photo_kit.save_jpeg(image, photo_kit.make_exif(captured, near))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def generate(out: Path) -> dict[str, Any]:
    from evaluation import signing

    rng = random.Random(world.SEED)
    names = NameBook(rng)
    registry = json.loads((REPO_ROOT / "data" / "demo" / "issuer_registry.json").read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)

    rosters = build_rosters(rng, names)
    write_records(out, rosters)
    for index, institution in enumerate(world.INSTITUTIONS):
        scenario = next(s for s in world.SCENARIOS if s["key"] == institution["key"])
        captured = scenario.get("website", {}).get("captured_on", world.WEBSITE_CAPTURED_ON)
        write_website(out, institution, rosters[institution["key"]], captured, as_table=index % 2 == 1)

    signer = signing.throwaway_signer()
    labels: list[dict[str, Any]] = []
    for order, scenario in enumerate(world.SCENARIOS, start=1):
        folder = out / "applications" / scenario["key"]
        (folder / "photos").mkdir(parents=True, exist_ok=True)
        institution = next(i for i in world.INSTITUTIONS if i["key"] == scenario["key"])
        documents: list[dict[str, Any]] = []

        certificate = build_certificate(scenario, registry, signer)
        (folder / "certificate.pdf").write_bytes(certificate)
        documents.append(
            {
                "file": f"applications/{scenario['key']}/certificate.pdf",
                "document_type": "accreditation_certificate",
                "sha256": None if scenario["certificate"].get("sign") else hashlib.sha256(certificate).hexdigest(),
            }
        )

        # A scenario may leave the faculty list out (an incomplete application).
        if scenario["faculty"] is not None:
            people = declared_list(scenario, rosters, rng, names)
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=["faculty_id", "name", "department", "designation", "qualification"])
            writer.writeheader()
            for person in people:
                writer.writerow({**person, "department": world.DECLARED_SPELLING.get(person["department"], person["department"])})
            (folder / "faculty.csv").write_text(buffer.getvalue(), encoding="utf-8")
            documents.append({"file": f"applications/{scenario['key']}/faculty.csv", "document_type": "faculty_declaration", "sha256": hashlib.sha256(buffer.getvalue().encode()).hexdigest()})

        for spec in scenario["photos"]:
            data = build_photo(scenario, spec, out, rng)
            (folder / "photos" / spec["name"]).write_bytes(data)
            documents.append(
                {
                    "file": f"applications/{scenario['key']}/photos/{spec['name']}",
                    "document_type": spec["type"],
                    "claim": spec.get("claim"),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )

        expected = [
            {"fraud_type": ft, **world.EXPECTED_BY_FRAUD_TYPE[ft]} for ft in scenario["fraud_types"]
        ]
        labels.append(
            {
                "key": scenario["key"],
                "processing_order": order,
                "institution": institution["name"],
                "summary": scenario["summary"],
                "is_fraud": scenario["is_fraud"],
                "fraud_types": scenario["fraud_types"],
                "expected": expected,
                "expected_honest_flags": scenario.get("expected_honest_flags", []),
                # A clean application must not carry a medium or strong finding.
                "must_not_min_strength": None if scenario["is_fraud"] else "medium",
                "documents": documents,
            }
        )

    manifest = {
        "dataset": "sih-2026-demo",
        "seed": world.SEED,
        "as_of": world.AS_OF,
        "note": "Synthetic. Every institution, person, certificate and photo is invented.",
        "non_reproducible_documents": [
            d["file"] for l in labels for d in l["documents"] if d["sha256"] is None
        ],
        "applications": labels,
    }
    (out / "labels.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo.generate", description="Generate the synthetic demo dataset.")
    parser.add_argument("--out", default=str(REPO_ROOT / "data" / "demo"), help="output directory")
    args = parser.parse_args(argv)
    manifest = generate(Path(args.out))
    print(json.dumps({"applications": len(manifest["applications"]), "out": args.out}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
