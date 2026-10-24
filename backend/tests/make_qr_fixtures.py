"""Build the synthetic QR fixtures for step 3.

Everything here is generated, never a real certificate, and every demo domain
sits under the reserved .test TLD so nothing can resolve to a live site.

The fixtures deliberately cover the awkward cases: a QR drawn as vector paths
rather than placed as an image, a QR at a resolution a rendered page would
blur away, a rotated photograph, and a scan too small to read without help
from the preprocessing ladder.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "qr"

GOOD_HOST = "verify.nbta-demo.test"
CERT_ID = "NBTA/DEMO/2024/00417"
ALTERED_CERT_ID = "NBTA/DEMO/2024/00418"


def verify_url(cert_id: str = CERT_ID, host: str = GOOD_HOST, scheme: str = "https") -> str:
    """The demo QR format: the id percent-encoded into one path segment."""
    from urllib.parse import quote

    return f"{scheme}://{host}/c/{quote(cert_id, safe='')}"


def _qr_png(payload: str, scale: int = 8, border: int = 4) -> bytes:
    import segno

    code = segno.make(payload, error="h")
    buffer = io.BytesIO()
    code.save(buffer, kind="png", scale=scale, border=border)
    return buffer.getvalue()


def _qr_modules(payload: str) -> list[list[bool]]:
    """The QR as a grid of booleans, for drawing it as vector rectangles."""
    import segno

    code = segno.make(payload, error="h")
    return [list(row) for row in code.matrix]


def _certificate_text(page: Any) -> None:
    lines = [
        "NATIONAL BOARD OF TECHNICAL ACCREDITATION (DEMO)",
        "Certificate of Accreditation",
        "Institution: Shantiniketan Institute of Technology",
        f"Certificate Number: {CERT_ID}",
        "Programme: Bachelor of Technology, Computer Science and Engineering",
        "Issuing Body: National Board of Technical Accreditation (Demo)",
        "Valid From: 01 July 2024",
        "Valid Until: 30 June 2027",
        "This is synthetic demo data generated for testing. It accredits nothing.",
    ]
    y = 70
    for line in lines:
        page.insert_text((60, y), line, fontsize=11)
        y += 18


def _pdf_with_embedded_qr(
    path: Path,
    payload: str,
    link_uri: str | None = None,
    scale: int = 8,
) -> Path:
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    _certificate_text(page)
    rect = fitz.Rect(400, 600, 540, 740)
    page.insert_image(rect, stream=_qr_png(payload, scale=scale))
    if link_uri:
        link_rect = fitz.Rect(60, 760, 300, 780)
        page.insert_text((60, 775), "Verify this certificate online", fontsize=10)
        page.insert_link({"kind": fitz.LINK_URI, "from": link_rect, "uri": link_uri})
    document.save(path)
    document.close()
    return path


def _pdf_with_vector_qr(path: Path, payload: str) -> Path:
    """A QR drawn as filled rectangles: not an image, so only rendering finds it."""
    import fitz

    modules = _qr_modules(payload)
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    _certificate_text(page)

    size = len(modules)
    box = 160.0
    module_size = box / size
    origin_x, origin_y = 400.0, 600.0
    shape = page.new_shape()
    for row_index, row in enumerate(modules):
        for col_index, filled in enumerate(row):
            if not filled:
                continue
            x = origin_x + col_index * module_size
            y = origin_y + row_index * module_size
            shape.draw_rect(fitz.Rect(x, y, x + module_size, y + module_size))
    shape.finish(fill=(0, 0, 0), color=None, width=0)
    shape.commit()
    document.save(path)
    document.close()
    return path


def _pdf_with_two_qrs(path: Path, first: str, second: str) -> Path:
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    _certificate_text(page)
    page.insert_image(fitz.Rect(60, 600, 200, 740), stream=_qr_png(first))
    page.insert_image(fitz.Rect(400, 600, 540, 740), stream=_qr_png(second))
    document.save(path)
    document.close()
    return path


def _pdf_without_qr(path: Path) -> Path:
    import fitz

    document = fitz.open()
    page = document.new_page(width=595, height=842)
    _certificate_text(page)
    document.save(path)
    document.close()
    return path


def _jpg_rotated(path: Path, payload: str, degrees: float = 7.0) -> Path:
    """A photographed certificate: rotated a few degrees, on an off-white page."""
    from PIL import Image

    qr = Image.open(io.BytesIO(_qr_png(payload, scale=10))).convert("RGB")
    canvas = Image.new("RGB", (900, 900), (246, 244, 238))
    canvas.paste(qr, (220, 220))
    rotated = canvas.rotate(degrees, resample=Image.BICUBIC, fillcolor=(246, 244, 238))
    rotated.save(path, format="JPEG", quality=92)
    return path


def _jpg_low_resolution(path: Path, payload: str) -> Path:
    """A small, soft, JPEG-compressed scan: needs the ladder's upscale rung."""
    from PIL import Image, ImageFilter

    qr = Image.open(io.BytesIO(_qr_png(payload, scale=3, border=2))).convert("RGB")
    small = qr.resize((qr.width // 2, qr.height // 2), resample=Image.BILINEAR)
    blurred = small.filter(ImageFilter.GaussianBlur(radius=0.4))
    blurred.save(path, format="JPEG", quality=45)
    return path


def _jpg_plain(path: Path, payload: str, scale: int = 8) -> Path:
    from PIL import Image

    qr = Image.open(io.BytesIO(_qr_png(payload, scale=scale))).convert("RGB")
    qr.save(path, format="JPEG", quality=95)
    return path


def _jpg_no_qr(path: Path) -> Path:
    from PIL import Image, ImageDraw

    canvas = Image.new("RGB", (600, 400), (250, 250, 248))
    draw = ImageDraw.Draw(canvas)
    draw.text((40, 40), "Certificate of Accreditation (demo)", fill=(20, 20, 20))
    draw.text((40, 70), f"Certificate Number: {CERT_ID}", fill=(20, 20, 20))
    draw.rectangle([40, 120, 560, 360], outline=(180, 180, 180), width=2)
    canvas.save(path, format="JPEG", quality=90)
    return path


# Each entry is a filename and the payload (or builder) behind it. Keeping the
# table declarative makes the set of fraud patterns readable at a glance.
_URL_CASES: dict[str, str] = {
    "qr_wrong_domain.jpg": verify_url(host="verify.other-issuer.test"),
    # The lookalike: the allowlisted string appears, but as a subdomain of a
    # host nobody allowlisted. A naive `contains` or `endswith` accepts it.
    "qr_lookalike_domain.jpg": verify_url(host="verify.nbta-demo.test.evil.test"),
    "qr_lookalike_prefix.jpg": verify_url(host="official.com.evil.test"),
    # Everything before the @ is decoration; the request goes to evil.test.
    "qr_userinfo.jpg": (
        f"https://verify.nbta-demo.test@evil.test/c/"
        f"{CERT_ID.replace('/', '%2F')}"
    ),
    # xn--80ak6aa92e decodes to a Cyrillic homoglyph of "apple".
    "qr_punycode.jpg": verify_url(host="xn--80ak6aa92e.test"),
    "qr_ip_host.jpg": verify_url(host="192.0.2.77"),
    "qr_http_scheme.jpg": verify_url(scheme="http"),
    "qr_shortener.jpg": "https://bit.ly/3xTestDemo",
    "qr_deep_subdomains.jpg": verify_url(host="a.b.c.d.nbta-demo.test"),
    "qr_non_standard_port.jpg": f"https://{GOOD_HOST}:8443/c/{CERT_ID.replace('/', '%2F')}",
    "qr_plain_text.jpg": CERT_ID,
    "qr_altered_id.jpg": verify_url(cert_id=ALTERED_CERT_ID),
}


def build_all() -> dict[str, Path]:
    """Write every fixture and return a name -> path map."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    built: dict[str, Path] = {}

    built["valid_pdf"] = _pdf_with_embedded_qr(
        FIXTURE_DIR / "certificate_valid_qr.pdf", verify_url()
    )
    built["vector_pdf"] = _pdf_with_vector_qr(
        FIXTURE_DIR / "certificate_vector_qr.pdf", verify_url()
    )
    built["two_qrs_pdf"] = _pdf_with_two_qrs(
        FIXTURE_DIR / "certificate_two_qrs.pdf",
        verify_url(),
        verify_url(cert_id=ALTERED_CERT_ID),
    )
    built["no_qr_pdf"] = _pdf_without_qr(FIXTURE_DIR / "certificate_no_qr.pdf")
    # Certificate text printing 00417, with a QR encoding 00418. The altered
    # digit is only detectable when both sources are present, so a bare QR
    # image cannot exercise the cross-check.
    built["altered_id_pdf"] = _pdf_with_embedded_qr(
        FIXTURE_DIR / "certificate_altered_qr.pdf",
        verify_url(cert_id=ALTERED_CERT_ID),
    )
    built["link_mismatch_pdf"] = _pdf_with_embedded_qr(
        FIXTURE_DIR / "certificate_link_mismatch.pdf",
        verify_url(),
        link_uri="https://verify.other-issuer.test/lookup",
    )
    built["link_match_pdf"] = _pdf_with_embedded_qr(
        FIXTURE_DIR / "certificate_link_match.pdf",
        verify_url(),
        link_uri=f"https://{GOOD_HOST}/lookup",
    )
    built["rotated_jpg"] = _jpg_rotated(
        FIXTURE_DIR / "qr_rotated.jpg", verify_url()
    )
    built["low_resolution_jpg"] = _jpg_low_resolution(
        FIXTURE_DIR / "qr_low_resolution.jpg", verify_url()
    )
    built["no_qr_jpg"] = _jpg_no_qr(FIXTURE_DIR / "no_qr.jpg")
    built["valid_jpg"] = _jpg_plain(FIXTURE_DIR / "qr_valid.jpg", verify_url())

    for filename, payload in _URL_CASES.items():
        key = filename.rsplit(".", 1)[0]
        built[key] = _jpg_plain(FIXTURE_DIR / filename, payload)

    return built


if __name__ == "__main__":
    for name, path in build_all().items():
        print(f"{name} -> {path}")
