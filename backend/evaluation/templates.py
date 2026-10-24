"""Certificate layouts, and the forms a finished document arrives in.

Five layouts that differ in more than colour: wording, whether the fields are
a table or free prose, font sizes, an abstract seal, a watermark, and where the
QR sits. That variety is the point. Step 6's Hamming thresholds were measured
on a single layout, and a perceptual hash is sensitive to layout, so a set that
reused one template would re-confirm the same number rather than test it.

**Nothing here draws a real logo, emblem or seal.** The seals are a circle, a
hexagon and a square with invented initials. Imitating a government emblem is
not something a demo dataset should contain, whatever the intent.

## Compressed by default, uncompressed only where it is needed

`render_pdf` compresses content streams unless asked not to, and that is a
determinism decision rather than a size one. **MuPDF's uncompressed output is
not byte-reproducible**: measured on this machine, about one render in sixty
differs on identical input when `deflate=False`, and zero in sixty differ when
`deflate=True`. Compression evidently normalises whatever varies.

Exactly one case needs uncompressed output: `signed_then_tampered` has to edit
a printed digit *inside the signed byte range*, which requires the text to be
literally present in the file's bytes - otherwise the "edit" is a byte flip at
an arbitrary offset and the fixture stops being the thing it claims to be. That
document is signed, and signed documents are already excluded from the
byte-determinism guarantee (a signature embeds a signing time and a fresh
key), so the nondeterminism costs nothing there.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# PyMuPDF writes a random file identifier into the PDF trailer on every save,
# and regenerates the second element of it even when the first is pinned. It
# serves no purpose for a generated fixture and it is the one thing that made
# otherwise identical documents differ byte for byte between runs, which broke
# the determinism the whole evaluation rests on.
#
# Replaced with zeros at equal length. The trailer sits after the xref table
# and `startxref` points at the xref, so nothing downstream moves - and equal
# length means nothing would move even if it did.
_TRAILER_ID = re.compile(rb"/ID\s*\[\s*<([0-9A-Fa-f]*)>\s*<([0-9A-Fa-f]*)>\s*\]")


def make_deterministic(pdf_bytes: bytes) -> bytes:
    """Strip the random trailer /ID so the same inputs give the same bytes."""

    def replace(match: "re.Match[bytes]") -> bytes:
        first = b"0" * len(match.group(1))
        second = b"0" * len(match.group(2))
        return b"/ID[<" + first + b"><" + second + b">]"

    return _TRAILER_ID.sub(replace, pdf_bytes)


class NonDeterministicRender(RuntimeError):
    """A render would not settle on one answer. Raised rather than guessed."""


def stable_bytes(build, attempts: int = 6) -> bytes:
    """Call `build` until two consecutive results agree, and return that.

    MuPDF is *occasionally* nondeterministic - measured on this machine, about
    one render in twenty-five to sixty differs on identical input, and the
    variability survives pinning the trailer /ID, clearing the metadata and
    compressing the streams. The cause is somewhere inside the library and not
    reachable from here.

    Rather than accept a dataset that is almost reproducible, every render is
    repeated until two consecutive attempts match. Two agreeing draws pin the
    output to the common answer, so the rare odd one is emitted with
    probability around (1/n)^2 instead of 1/n - which across eighty documents
    and repeated runs is the difference between "reproducible" and "usually
    reproducible".

    It costs one extra render per document. That is a cheap price for a
    dataset whose numbers can be compared with last week's.

    Raises if it never settles: a generator that silently produced a
    non-reproducible dataset would undermine every measurement taken from it.
    """
    previous = build()
    for _ in range(attempts - 1):
        current = build()
        if current == previous:
            return current
        previous = current
    raise NonDeterministicRender(
        f"a render did not produce the same bytes twice in {attempts} "
        "attempts; the dataset would not be reproducible"
    )

# Candidate fonts that cover Devanagari, in the order we would rather have
# them. Checked at generation time; a miss is reported, never silently
# swallowed, because it is a gap in script coverage.
_DEVANAGARI_FONT_CANDIDATES = (
    # .ttc is a font *collection*; PyMuPDF opens it and renders Devanagari
    # from it correctly, verified on this machine.
    "C:/Windows/Fonts/Nirmala.ttc",
    "C:/Windows/Fonts/Nirmala.ttf",
    "C:/Windows/Fonts/NirmalaS.ttf",
    "C:/Windows/Fonts/mangal.ttf",
    "C:/Windows/Fonts/Kokila.ttf",
    "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
)

DEVANAGARI_HEADING = "तकनीकी शिक्षा मान्यता प्रमाणपत्र (नमूना)"

# Note on the Devanagari line: PyMuPDF renders it, and the text layer reads it
# back with the Devanagari codepoints intact, but the ASCII space inside it
# comes back as U+00A0. That is harmless here because the heading is
# decorative - no field *value* is printed on it - but it is exactly the kind
# of difference that would break step 2's exact-value guard if a certificate
# number ever shared a line with non-Latin text.


@dataclass
class CertificateFields:
    """What is printed on the page.

    This is the ground truth for extraction: whatever is in here is what step 2
    should return, and the labels copy it verbatim.
    """

    institution_name: str
    certificate_number: str
    issuing_body: str
    programme: str
    accreditation_status_or_grade: str | None
    valid_from: str
    valid_to: str
    issue_date: str | None = None
    signatory_name: str | None = None

    def printed(self) -> dict[str, Any]:
        """The printed values, as the labels record them."""
        return {
            "institution_name": self.institution_name,
            "certificate_number": self.certificate_number,
            "issuing_body": self.issuing_body,
            "programme": self.programme,
            "accreditation_status_or_grade": self.accreditation_status_or_grade,
            "issue_date": self.issue_date,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "signatory_name": self.signatory_name,
        }


def devanagari_font() -> str | None:
    """The first installed font that can render Devanagari, or None."""
    for candidate in _DEVANAGARI_FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


# --------------------------------------------------------------------------
# Line composition
# --------------------------------------------------------------------------


def _free_text_lines(
    fields: CertificateFields, template: dict[str, Any]
) -> list[tuple[str, bool]]:
    """Prose layout. Returns (text, is_heading) pairs.

    The wording differs per template deliberately: a detector tuned to one
    body's phrasing is not a detector, and step 2's snippet guard has to cope
    with "Accredited Programme:" as well as "Programme:".
    """
    key = template["key"]
    if key == "plain":
        return [
            (fields.issuing_body.upper(), True),
            ("STATEMENT OF PROGRAMME ACCREDITATION", True),
            (f"Name of Institution: {fields.institution_name}", False),
            (f"Reference No.: {fields.certificate_number}", False),
            (f"Accredited Programme: {fields.programme}", False),
            (
                "Outcome: "
                + (fields.accreditation_status_or_grade or "Not graded"),
                False,
            ),
            (f"Effective From: {fields.valid_from}", False),
            (f"Effective Until: {fields.valid_to}", False),
            (f"Authorised Signatory: {fields.signatory_name or '-'}", False),
            ("Synthetic evaluation data. This accredits nothing.", False),
        ]
    if key == "bilingual":
        return [
            (fields.issuing_body.upper(), True),
            ("Certificate of Programme Accreditation", True),
            (f"Institution: {fields.institution_name}", False),
            (f"Certificate Number: {fields.certificate_number}", False),
            (f"Programme: {fields.programme}", False),
            (f"Grade Awarded: {fields.accreditation_status_or_grade or '-'}", False),
            (f"Date of Issue: {fields.issue_date or '-'}", False),
            (f"Valid From: {fields.valid_from}", False),
            (f"Valid Until: {fields.valid_to}", False),
            (f"Signed: {fields.signatory_name or '-'}", False),
            ("Synthetic evaluation data. This accredits nothing.", False),
        ]
    # classic
    return [
        (fields.issuing_body.upper(), True),
        ("Certificate of Accreditation", True),
        (f"Institution: {fields.institution_name}", False),
        (f"Certificate Number: {fields.certificate_number}", False),
        (f"Programme: {fields.programme}", False),
        (f"Issuing Body: {fields.issuing_body}", False),
        (f"Grade: {fields.accreditation_status_or_grade or '-'}", False),
        (f"Valid From: {fields.valid_from}", False),
        (f"Valid Until: {fields.valid_to}", False),
        (f"Signatory: {fields.signatory_name or '-'}", False),
        ("Synthetic evaluation data. This accredits nothing.", False),
    ]


def _table_rows(fields: CertificateFields, template: dict[str, Any]) -> list[tuple[str, str]]:
    """Two-column layout. Label and value, drawn as a ruled table."""
    if template["key"] == "ledger":
        rows = [
            ("Institution", fields.institution_name),
            ("Certificate No.", fields.certificate_number),
            ("Issuing Authority", fields.issuing_body),
            ("Programme", fields.programme),
            ("Assessment Grade", fields.accreditation_status_or_grade or "-"),
            ("Issued On", fields.issue_date or "-"),
            ("Valid From", fields.valid_from),
            ("Valid Until", fields.valid_to),
            ("Signatory", fields.signatory_name or "-"),
        ]
    else:  # tabular
        rows = [
            ("Name of Institution", fields.institution_name),
            ("Certificate Number", fields.certificate_number),
            ("Issuing Body", fields.issuing_body),
            ("Programme Accredited", fields.programme),
            ("Grade", fields.accreditation_status_or_grade or "-"),
            ("Valid From", fields.valid_from),
            ("Valid Until", fields.valid_to),
            ("Signatory", fields.signatory_name or "-"),
        ]
    return rows


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _draw_seal(page: Any, template: dict[str, Any], abbreviation: str) -> None:
    """An abstract seal: a shape and some invented initials.

    Explicitly not a reproduction of anybody's emblem.
    """
    import fitz

    shape_kind = str(template.get("seal", "none"))
    if shape_kind == "none":
        return

    accent = tuple(template.get("accent", [0.2, 0.2, 0.2]))
    position = str(template.get("seal_position", "bottom_right"))
    centre = {
        "bottom_right": (470.0, 700.0),
        "bottom_left": (120.0, 700.0),
        "top_right": (480.0, 120.0),
    }.get(position, (470.0, 700.0))
    radius = 46.0

    shape = page.new_shape()
    if shape_kind == "circle":
        shape.draw_circle(fitz.Point(*centre), radius)
        shape.draw_circle(fitz.Point(*centre), radius - 6)
    elif shape_kind == "square":
        shape.draw_rect(
            fitz.Rect(
                centre[0] - radius,
                centre[1] - radius,
                centre[0] + radius,
                centre[1] + radius,
            )
        )
        shape.draw_rect(
            fitz.Rect(
                centre[0] - radius + 6,
                centre[1] - radius + 6,
                centre[0] + radius - 6,
                centre[1] + radius - 6,
            )
        )
    elif shape_kind == "hexagon":
        points = [
            fitz.Point(
                centre[0] + radius * math.cos(math.radians(60 * i)),
                centre[1] + radius * math.sin(math.radians(60 * i)),
            )
            for i in range(6)
        ]
        for index in range(6):
            shape.draw_line(points[index], points[(index + 1) % 6])
    shape.finish(color=accent, width=1.2)
    shape.commit()

    page.insert_text(
        (centre[0] - 16, centre[1] + 4),
        abbreviation[:4],
        fontsize=12,
        color=accent,
    )


def _draw_watermark(page: Any, template: dict[str, Any]) -> None:
    """A pale diagonal word across the page. Changes the hash a lot."""
    if not bool(template.get("watermark")):
        return
    page.insert_text(
        (130, 520),
        "EVALUATION SPECIMEN",
        fontsize=30,
        color=(0.86, 0.86, 0.90),
        rotate=0,
    )


def _qr_rect(template: dict[str, Any]) -> Any:
    import fitz

    position = str(template.get("qr_position", "none"))
    size = float(template.get("qr_size", 0))
    if position == "none" or size <= 0:
        return None
    boxes = {
        "bottom_right": (595 - 60 - size, 842 - 80 - size),
        "bottom_left": (60, 842 - 80 - size),
        "top_right": (595 - 60 - size, 150),
    }
    x, y = boxes.get(position, boxes["bottom_right"])
    return fitz.Rect(x, y, x + size, y + size)


def _qr_png(payload: str, scale: int = 8) -> bytes:
    import segno

    code = segno.make(payload, error="h")
    buffer = io.BytesIO()
    code.save(buffer, kind="png", scale=scale, border=3)
    return buffer.getvalue()


def render_pdf(
    fields: CertificateFields,
    template: dict[str, Any],
    qr_payload: str | None,
    abbreviation: str,
    devanagari_font_path: str | None = None,
    uncompressed: bool = False,
) -> bytes:
    """Render one certificate as a text-layer PDF.

    `uncompressed=True` keeps the printed text literally present in the output
    bytes, which only the signed-tamper case needs - and costs byte
    reproducibility. See the module docstring.
    """
    return stable_bytes(
        lambda: _render_pdf_once(
            fields,
            template,
            qr_payload,
            abbreviation,
            devanagari_font_path,
            uncompressed,
        )
    )


def _render_pdf_once(
    fields: CertificateFields,
    template: dict[str, Any],
    qr_payload: str | None,
    abbreviation: str,
    devanagari_font_path: str | None = None,
    uncompressed: bool = False,
) -> bytes:
    """One render attempt. Called through `stable_bytes`, never directly."""
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    accent = tuple(template.get("accent", [0.2, 0.2, 0.2]))
    heading_size = float(template.get("heading_size", 15))
    body_size = float(template.get("body_size", 11))

    _draw_watermark(page, template)

    y = 70.0
    if template.get("devanagari_heading") and devanagari_font_path:
        page.insert_font(fontname="deva", fontfile=devanagari_font_path)
        page.insert_text(
            (60, y), DEVANAGARI_HEADING, fontsize=heading_size, fontname="deva",
            color=accent,
        )
        y += heading_size + 10

    if str(template.get("style")) == "table":
        for text, is_heading in [
            (fields.issuing_body.upper(), True),
            (
                "Certificate of Accreditation"
                if template["key"] == "ledger"
                else "Programme Accreditation Record",
                True,
            ),
        ]:
            page.insert_text(
                (60, y),
                text,
                fontsize=heading_size if is_heading else body_size,
                color=accent if is_heading else (0, 0, 0),
            )
            y += heading_size + 8

        y += 10
        rows = _table_rows(fields, template)
        row_height = body_size + 12
        label_x, value_x, right = 70.0, 230.0, 535.0
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(60, y - 6, right, y - 6 + row_height * len(rows)))
        for index in range(len(rows) + 1):
            line_y = y - 6 + row_height * index
            shape.draw_line(fitz.Point(60, line_y), fitz.Point(right, line_y))
        shape.draw_line(
            fitz.Point(value_x - 10, y - 6),
            fitz.Point(value_x - 10, y - 6 + row_height * len(rows)),
        )
        shape.finish(color=accent, width=0.7)
        shape.commit()

        for label, value in rows:
            page.insert_text((label_x, y + body_size), label, fontsize=body_size)
            page.insert_text((value_x, y + body_size), value, fontsize=body_size)
            y += row_height
        y += 16
        page.insert_text(
            (60, y),
            "Synthetic evaluation data. This accredits nothing.",
            fontsize=body_size - 1,
        )
    else:
        for text, is_heading in _free_text_lines(fields, template):
            page.insert_text(
                (60, y),
                text,
                fontsize=heading_size if is_heading else body_size,
                color=accent if is_heading else (0, 0, 0),
            )
            y += (heading_size if is_heading else body_size) + 7

    _draw_seal(page, template, abbreviation)

    rect = _qr_rect(template)
    if rect is not None and qr_payload:
        page.insert_image(rect, stream=_qr_png(qr_payload))

    # Clear the Info and XMP metadata as well: both can carry a timestamp,
    # and neither tells a reader of a synthetic fixture anything.
    document.set_metadata({})
    try:
        document.del_xml_metadata()
    except Exception:
        pass
    # clean=True in both cases: it normalises the file structure. deflate is
    # what decides reproducibility.
    data = document.tobytes(deflate=not uncompressed, clean=True)
    document.close()
    return make_deterministic(data)


def render_page_image(pdf_bytes: bytes, dpi: int = 200) -> Any:
    """Page 1 of a PDF as a PIL image, for the scanned forms."""
    import fitz
    from PIL import Image

    document = fitz.open("pdf", pdf_bytes)
    try:
        pixmap = document.load_page(0).get_pixmap(dpi=dpi, alpha=False)
        image = Image.frombytes(
            "RGB" if pixmap.n >= 3 else "L",
            (pixmap.width, pixmap.height),
            pixmap.samples,
        )
    finally:
        document.close()
    return image.convert("RGB")


def image_to_pdf(image: Any) -> bytes:
    """Wrap a page image in a PDF with no text layer, forcing OCR.

    A scanned-style PDF is not a cosmetic variation: it is the form in which
    most real EoA attachments arrive, and it is the only one that exercises
    step 1's OCR path end to end.
    """
    return stable_bytes(lambda: _image_to_pdf_once(image))


def _image_to_pdf_once(image: Any) -> bytes:
    import fitz

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=buffer.getvalue())
    document.set_metadata({})
    try:
        document.del_xml_metadata()
    except Exception:
        pass
    data = document.tobytes(deflate=True)
    document.close()
    return make_deterministic(data)
