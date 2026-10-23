"""Generate the small synthetic sample files the extraction tests run against.

Run it directly to refresh them:

    python backend/tests/make_fixtures.py

Everything produced here is synthetic demo data: a fictional institution, a
fictional certificate number, no real accreditation record.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CERTIFICATE_LINES = [
    "NATIONAL BOARD OF TECHNICAL ACCREDITATION (DEMO)",
    "Certificate of Accreditation",
    "",
    "Institution: Shantiniketan Institute of Technology",
    "Certificate Number: NBTA/DEMO/2024/00417",
    "Programme: Bachelor of Technology, Computer Science and Engineering",
    "Issuing Body: National Board of Technical Accreditation (Demo)",
    "Valid From: 01 July 2024",
    "Valid Until: 30 June 2027",
    "",
    "This is synthetic demo data generated for testing. It accredits nothing.",
]

# A short page, deliberately under the configured character and word floors,
# so the extractor must fall back to OCR even though a text layer exists.
THIN_TEXT = "Annexure B"


def _font_size_pt() -> float:
    return 13.0


def make_text_layer_pdf(path: Path) -> None:
    """A normal digital PDF whose text layer should be accepted as-is."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    y = 72.0
    for line in CERTIFICATE_LINES:
        if line:
            page.insert_text((72, y), line, fontsize=_font_size_pt(), fontname="helv")
        y += 20
    doc.save(path)
    doc.close()


def make_certificate_image(path: Path) -> None:
    """A plain rendering of the certificate as an image, for the OCR path."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1240, 700
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    font = _load_font(30)
    y = 50
    for line in CERTIFICATE_LINES:
        if line:
            draw.text((60, y), line, fill="black", font=font)
        y += 48

    draw.rectangle([20, 20, width - 20, height - 20], outline="black", width=3)
    image.save(path, format="JPEG", quality=92)


def make_scanned_style_pdf(path: Path, image_path: Path) -> None:
    """A PDF that is just a page image, with no text layer at all."""
    import pymupdf

    doc = pymupdf.open()
    with pymupdf.open(image_path) as image_doc:
        rect = image_doc[0].rect
        pdf_bytes = image_doc.convert_to_pdf()

    with pymupdf.open("pdf", pdf_bytes) as image_pdf:
        page = doc.new_page(width=rect.width, height=rect.height)
        page.show_pdf_page(page.rect, image_pdf, 0)

    doc.save(path)
    doc.close()


def make_thin_text_layer_pdf(path: Path) -> None:
    """A PDF with a text layer too sparse to trust, forcing the OCR fallback."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), THIN_TEXT, fontsize=_font_size_pt(), fontname="helv")
    doc.save(path)
    doc.close()


def make_empty_pdf(path: Path) -> None:
    """A structurally valid PDF whose page tree is empty.

    Written by hand with a computed xref table, because PyMuPDF refuses to save
    a document with zero pages. A submission like this is rare but real (an
    export that silently produced nothing), and it must not crash extraction.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [] /Count 0 >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number
        out += body + b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_offset

    path.write_bytes(bytes(out))


def make_encrypted_pdf(path: Path) -> None:
    """A password-protected PDF."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "locked", fontsize=_font_size_pt(), fontname="helv")
    doc.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="demo-owner",
        user_pw="demo-user",
    )
    doc.close()


def make_corrupt_pdf(path: Path) -> None:
    """Bytes that claim to be a PDF but are not parseable."""
    path.write_bytes(b"%PDF-1.7\nthis is not a real pdf body\n%%EOF\n")


def _load_font(size: int):
    """A TrueType face if one is available, else Pillow's bitmap default.

    OCR on the default bitmap font is poor, so the OCR tests assert on
    structure and confidence rather than on exact recognised strings.
    """
    from PIL import ImageFont

    for candidate in (
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_all(target: Path = FIXTURES) -> dict[str, Path]:
    target.mkdir(parents=True, exist_ok=True)
    paths = {
        "text_layer_pdf": target / "certificate_text_layer.pdf",
        "certificate_image": target / "certificate_scan.jpg",
        "scanned_pdf": target / "certificate_scanned.pdf",
        "thin_text_pdf": target / "annexure_thin_text.pdf",
        "empty_pdf": target / "empty.pdf",
        "encrypted_pdf": target / "encrypted.pdf",
        "corrupt_pdf": target / "corrupt.pdf",
    }

    make_text_layer_pdf(paths["text_layer_pdf"])
    make_certificate_image(paths["certificate_image"])
    make_scanned_style_pdf(paths["scanned_pdf"], paths["certificate_image"])
    make_thin_text_layer_pdf(paths["thin_text_pdf"])
    make_empty_pdf(paths["empty_pdf"])
    make_encrypted_pdf(paths["encrypted_pdf"])
    make_corrupt_pdf(paths["corrupt_pdf"])
    return paths


if __name__ == "__main__":
    for name, path in build_all().items():
        print(f"{name}: {path} ({path.stat().st_size} bytes)")
