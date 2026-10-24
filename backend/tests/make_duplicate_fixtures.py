"""Build the synthetic duplicate-detection fixtures for step 6, part B.

Two of these exist to answer a question the spec refused to let us guess: what
Hamming distance separates a resubmitted document from one that merely shares a
template? They are the `legitimate_template_pair` and the `one_digit_edited`
fixtures, and `print_distance_table()` is what measured the threshold now in
`config/duplicate_checks.yaml`.

The measurement result worth knowing before reading the rest: **no threshold
separates them.** A same-template certificate from a different institution sits
2-6 bits away, and a genuine re-save sits 1-5 bits away. That overlap is why a
near-duplicate image is never a strong finding - not caution, measurement.

All institutions and numbers are invented.
"""

from __future__ import annotations

import io
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "duplicates"

CERT_ID = "NBTA/DEMO/2024/00417"
# The same certificate with one digit changed. The distance this produces is
# the headline result: a perceptual hash cannot see a one-digit edit.
EDITED_CERT_ID = "NBTA/DEMO/2024/00418"
# A second institution's genuine certificate, on the same issuer's template.
SIBLING_CERT_ID = "NBTA/DEMO/2024/00903"

INSTITUTION_A = "Shantiniketan Institute of Technology"
INSTITUTION_B = "Konkan Coast Institute of Engineering"
INSTITUTION_C = "Vindhya Technical University"


def certificate_lines(institution: str, certificate_id: str) -> list[str]:
    """One issuer's template, filled in. Layout identical between institutions.

    That is the whole point of the legitimate-pair fixture: real certificates
    from one accreditation body *do* look alike, and a system that treated
    visual similarity as evidence would flag every honest applicant.
    """
    return [
        "NATIONAL BOARD OF TECHNICAL ACCREDITATION (DEMO)",
        "Certificate of Accreditation",
        f"Institution: {institution}",
        f"Certificate Number: {certificate_id}",
        "Programme: Bachelor of Technology, Computer Science and Engineering",
        "Issuing Body: National Board of Technical Accreditation (Demo)",
        "Valid From: 01 July 2024",
        "Valid Until: 30 June 2027",
        "This is synthetic demo data generated for testing. It accredits nothing.",
    ]


# A document of a wholly different kind, as the negative control: if the
# threshold matches this, it matches anything.
UNRELATED_LINES = [
    "ANNUAL RETURN OF FACULTY STRENGTH (DEMO)",
    f"Institution: {INSTITUTION_C}",
    "Reference: VTU/DEMO/2023/77120",
    "Department: Mechanical Engineering",
    "Sanctioned Intake: 120",
    "Faculty on roll: 41",
    "Student-teacher ratio: 1:19",
    "Prepared by the office of the Registrar.",
    "Synthetic demo data. Describes no real institution.",
]


ANNEXURE_LINES = [
    "ANNEXURE III - SUPPORTING DOCUMENTS (DEMO)",
    "",
    f"Submitted by: {INSTITUTION_B}",
    f"Certificate Number: {CERT_ID}",
    "",
    "Enclosures: accreditation certificate, faculty return, fee receipt.",
    "",
    "Synthetic demo data. Describes no real institution.",
    "",
]


def _copy(source: Path, path: Path) -> Path:
    path.write_bytes(source.read_bytes())
    return path


def _pdf(path: Path, lines: list[str]) -> Path:
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    y = 70
    for line in lines:
        page.insert_text((60, y), line, fontsize=11)
        y += 18
    document.save(path)
    document.close()
    return path


def _recompressed_copy(source: Path, path: Path, quality: int = 70) -> Path:
    """The same page, rendered and saved as a lossy JPEG.

    Stands in for the commonest kind of near-duplicate: a document that went
    through a scanner, a phone, or an email attachment pipeline on its way to
    being resubmitted. The bytes differ completely, so the exact-hash check
    cannot see it; only the perceptual hash can.
    """
    from PIL import Image

    from app.qr.images import render_page_image

    image = render_page_image(source, 1, 150)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    Image.open(buffer).convert("RGB").save(path, format="JPEG", quality=quality)
    return path


def _rotated_copy(source: Path, path: Path, degrees: float = 2.0) -> Path:
    """The same page, rotated slightly - a photographed or badly fed scan."""
    from app.qr.images import render_page_image

    image = render_page_image(source, 1, 150)
    rotated = image.rotate(
        degrees, resample=3, fillcolor=(255, 255, 255)
    )
    rotated.save(path, format="JPEG", quality=92)
    return path


def build_all() -> dict[str, Path]:
    """Write every duplicate fixture and return a name -> path map."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    built: dict[str, Path] = {}

    built["original"] = _pdf(
        FIXTURE_DIR / "cert_a_original.pdf",
        certificate_lines(INSTITUTION_A, CERT_ID),
    )
    # The same file under a second name. Copied byte for byte rather than
    # regenerated: PyMuPDF stamps a creation timestamp into everything it
    # writes, so generating identical *content* twice gives two different
    # files - and the exact-hash check would then have nothing to find.
    built["exact_copy"] = _copy(
        built["original"], FIXTURE_DIR / "cert_a_exact_copy.pdf"
    )
    # Same document, one digit of the number changed. A perceptual hash cannot
    # see this; the certificate-number comparison can.
    built["one_digit_edited"] = _pdf(
        FIXTURE_DIR / "cert_a_one_digit_edited.pdf",
        certificate_lines(INSTITUTION_A, EDITED_CERT_ID),
    )
    # A different institution's genuine certificate on the same template. Must
    # not be flagged strongly at any threshold.
    built["legitimate_template_pair"] = _pdf(
        FIXTURE_DIR / "cert_b_same_template.pdf",
        certificate_lines(INSTITUTION_B, SIBLING_CERT_ID),
    )
    built["unrelated"] = _pdf(FIXTURE_DIR / "faculty_return.pdf", UNRELATED_LINES)
    # A different kind of document quoting the same certificate number. This is
    # what isolates the number check: the file hash differs and the pages look
    # nothing alike, so a match here can only have come from the number.
    built["different_layout_same_number"] = _pdf(
        FIXTURE_DIR / "annexure_same_number.pdf", ANNEXURE_LINES
    )

    built["recompressed"] = _recompressed_copy(
        built["original"], FIXTURE_DIR / "cert_a_recompressed.jpg"
    )
    built["rotated"] = _rotated_copy(
        built["original"], FIXTURE_DIR / "cert_a_rotated.jpg"
    )
    return built


def print_distance_table() -> list[tuple[str, int]]:
    """Measure and print the distances that set the configured threshold.

    This is the evidence behind `near_duplicate_max_distance`. Run it again
    after changing `compare_kind` or `hash_size` - a threshold carried over
    from a different hash is a threshold that means nothing.
    """
    from app.core.config import get, load_config
    from app.qr.images import render_page_image
    from app.similarity import hashing

    cfg = load_config("duplicate_checks")
    kind = str(get(cfg, "perceptual_hash.compare_kind", "dhash"))
    size = int(get(cfg, "perceptual_hash.hash_size", 16))
    dpi = int(get(cfg, "perceptual_hash.render_dpi", 150))
    threshold = int(get(cfg, "perceptual_hash.near_duplicate_max_distance", 12))

    paths = build_all()
    hashes = {
        name: hashing.compute(
            render_page_image(path, 1, dpi), kind, hash_size=size
        )
        for name, path in paths.items()
    }
    base = hashes["original"]

    labels = [
        ("exact_copy", "identical content, second file"),
        ("recompressed", "same document, re-rendered as JPEG q70"),
        ("rotated", "same document, rotated 2 degrees"),
        ("one_digit_edited", "same document, one digit of the number edited"),
        ("legitimate_template_pair", "SAME TEMPLATE, different institution"),
        ("unrelated", "unrelated document (negative control)"),
    ]

    print(f"{kind}, hash_size={size} ({size * size} bits), threshold {threshold}")
    rows: list[tuple[str, int]] = []
    for name, label in labels:
        distance = hashing.hamming_distance(base, hashes[name])
        matched = "MATCH" if distance <= threshold else "no match"
        rows.append((name, distance))
        print(f"  {distance:4d}  {matched:9} {label}")
    return rows


if __name__ == "__main__":
    print_distance_table()
