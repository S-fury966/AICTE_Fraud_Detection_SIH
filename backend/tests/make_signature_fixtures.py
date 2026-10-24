"""Build the synthetic signed-PDF fixtures for step 6, part A.

Everything here is generated. The signing key is a throwaway RSA key created
fresh **in memory on every run** and never written to disk: there is no key
file to commit, so there is nothing to leak. The generated PDFs are likewise
not committed - `tests/fixtures/signatures/` is gitignored, and the fixture
builder runs on demand.

Why a real signing library produces the fixtures when our checker does the
digest comparison itself: if one piece of code both signed and verified, a
shared misreading of the spec would pass its own tests. pyHanko is the
reference implementation, so a fixture it produces is an independent check that
our reading of `/ByteRange` and `messageDigest` is the standard one.

The base document is written out by hand as an **uncompressed** PDF. PyMuPDF
compresses content streams, which would hide the certificate number inside a
deflate blob - and the tamper fixture has to be a real edit to a real printed
digit, not a byte flipped at an arbitrary offset.
"""

from __future__ import annotations

import datetime
import io
import re
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "signatures"

CERT_ID = "NBTA/DEMO/2024/00417"
ALTERED_CERT_ID = "NBTA/DEMO/2024/00418"
INSTITUTION = "Shantiniketan Institute of Technology"
SIGNER_COMMON_NAME = "NBTA Demo Signing Key (throwaway, not a real authority)"

CERTIFICATE_LINES = [
    "NATIONAL BOARD OF TECHNICAL ACCREDITATION (DEMO)",
    "Certificate of Accreditation",
    f"Institution: {INSTITUTION}",
    f"Certificate Number: {CERT_ID}",
    "Programme: Bachelor of Technology, Computer Science and Engineering",
    "Issuing Body: National Board of Technical Accreditation (Demo)",
    "Valid From: 01 July 2024",
    "Valid Until: 30 June 2027",
    "This is synthetic demo data generated for testing. It accredits nothing.",
]


# --------------------------------------------------------------------------
# A minimal, uncompressed PDF
# --------------------------------------------------------------------------


def minimal_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with literal, uncompressed text.

    Hand-written rather than produced by PyMuPDF so the printed text is
    addressable in the raw bytes. `/AcroForm` is present and empty because
    pyHanko adds the signature field to it.
    """
    content = "BT /F1 11 Tf 60 760 Td 18 TL\n"
    for line in lines:
        escaped = (
            line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        )
        content += f"({escaped}) Tj T*\n"
    content += "ET\n"
    stream = content.encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [] >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def throwaway_signer():
    """A self-signed RSA key and certificate, generated in memory.

    Never written to disk. The common name says what it is so that a reviewer
    looking at a fixture's `signer_name_unverified` cannot mistake it for a
    real accreditation authority.
    """
    from asn1crypto import keys as asn1keys, x509 as asn1x509
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from pyhanko.sign import signers
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, SIGNER_COMMON_NAME)]
    )
    not_before = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + datetime.timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )

    return signers.SimpleSigner(
        signing_cert=asn1x509.Certificate.load(
            certificate.public_bytes(serialization.Encoding.DER)
        ),
        signing_key=asn1keys.PrivateKeyInfo.load(
            key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        ),
        cert_registry=SimpleCertificateStore(),
    )


def sign(raw: bytes, signer, field_name: str = "Signature1") -> bytes:
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    writer = IncrementalPdfFileWriter(io.BytesIO(raw))
    output = signers.sign_pdf(
        writer, signers.PdfSignatureMetadata(field_name=field_name), signer=signer
    )
    return output.getvalue()


# --------------------------------------------------------------------------
# The damaged variants
# --------------------------------------------------------------------------


def tamper_printed_digit(signed: bytes) -> bytes:
    """Change the printed certificate number inside the signed byte range.

    A real content edit, the same length, so no offset in the file moves. This
    is the case the whole check exists for: the signature still parses, the
    byte range still covers the file, and the digest no longer matches.
    """
    index = signed.find(CERT_ID[-5:].encode())
    if index < 0:
        raise RuntimeError(
            "the certificate number is not literally present in the signed "
            "bytes; the base PDF must be uncompressed for this fixture"
        )
    altered = ALTERED_CERT_ID[-5:].encode()
    return signed[:index] + altered + signed[index + 5 :]


def append_incremental_revision(signed: bytes) -> bytes:
    """Add a proper incremental revision after signing.

    Written through pyHanko so the result is a *valid* appended revision, which
    is what a legitimate tool adding long-term validation data would produce.
    The signature over the earlier revision stays intact; the point of the
    fixture is that intact-but-not-covering is its own finding.
    """
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    writer = IncrementalPdfFileWriter(io.BytesIO(signed))
    writer.root["/DemoAppendedAfterSigning"] = generic.pdf_string(
        "appended after signing by the fixture builder"
    )
    writer.update_root()
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def shrink_byte_range(signed: bytes, reduce_by: int = 100) -> bytes:
    """Forge `/ByteRange` so it no longer reaches the end of the file.

    The replacement keeps the same text width, so nothing in the file moves.
    This is the realistic shape of the attack: shrink what the signature claims
    to cover, and hope a verifier only checks that the digest matches the range
    the document itself names.

    Both findings fire on the result - the coverage one and the digest one -
    which is correct and not a redundancy: the range was altered, so the digest
    over the altered range cannot match either.
    """
    match = re.search(rb"/ByteRange\s*\[([^\]]*)\]", signed)
    if match is None:
        raise RuntimeError("no /ByteRange found in the signed file")

    inner = match.group(1)
    parts = inner.split()
    last = parts[-1]
    shrunk = str(int(last) - reduce_by).rjust(len(last), "0").encode()
    if len(shrunk) != len(last):
        raise RuntimeError("the replacement length would move file offsets")
    forged = inner[: inner.rfind(last)] + shrunk
    return signed[: match.start(1)] + forged + signed[match.end(1) :]


def corrupt_cms(signed: bytes) -> bytes:
    """Replace the CMS blob with bytes that are not valid ASN.1.

    The signature structure is still there - the `/ByteRange` is intact and the
    field is present - but the container cannot be parsed. The file must report
    "a signature we cannot read", never "unsigned": those are opposite facts.
    """
    marker = b"/Contents <"
    index = signed.find(marker)
    if index < 0:
        raise RuntimeError("no /Contents found in the signed file")
    start = index + len(marker)
    # Same length in, same length out, so no offset moves.
    replacement = b"ff" * 60
    return signed[:start] + replacement + signed[start + len(replacement) :]


# --------------------------------------------------------------------------
# Building the set
# --------------------------------------------------------------------------


def build_all() -> dict[str, Path]:
    """Write every signature fixture and return a name -> path map."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    built: dict[str, Path] = {}

    unsigned = minimal_pdf(CERTIFICATE_LINES)
    built["unsigned"] = _write("certificate_unsigned.pdf", unsigned)

    signer = throwaway_signer()
    signed = sign(unsigned, signer)
    built["signed_clean"] = _write("certificate_signed_clean.pdf", signed)
    built["signed_tampered_digit"] = _write(
        "certificate_signed_tampered_digit.pdf", tamper_printed_digit(signed)
    )
    built["signed_incremental_update"] = _write(
        "certificate_signed_incremental_update.pdf",
        append_incremental_revision(signed),
    )
    built["signed_short_byte_range"] = _write(
        "certificate_signed_short_byte_range.pdf", shrink_byte_range(signed)
    )
    built["signed_corrupt_cms"] = _write(
        "certificate_signed_corrupt_cms.pdf", corrupt_cms(signed)
    )
    return built


def _write(filename: str, data: bytes) -> Path:
    path = FIXTURE_DIR / filename
    path.write_bytes(data)
    return path


if __name__ == "__main__":
    for name, path in build_all().items():
        print(f"{name} -> {path}")
