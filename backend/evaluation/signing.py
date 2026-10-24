"""Signing the evaluation's PDFs with a throwaway key.

The key is an RSA key generated **in memory** and never written to disk. There
is no key file in the repository, no key file in `data/eval/`, and nothing to
leak - a test asserts that generation writes no key or certificate material
anywhere.

One key is generated per generation run and reused across that run's signed
documents, which is both faster and more realistic than a fresh key per
certificate: a real issuing body signs with one key.

The signer's common name says what it is, so a reviewer looking at a stored
`signer_name_unverified` on an evaluation document cannot mistake it for a real
accreditation authority. That matters more here than in the step 6 fixtures,
because these documents go through the full pipeline and into a database.

The tampering helpers mirror the step 6 fixture builder deliberately. They are
duplicated rather than imported because `backend/tests/` is a test package and
`backend/evaluation/` is not; importing fixtures into the evaluation would tie
the dataset to the test suite's lifetime.
"""

from __future__ import annotations

import datetime
import io
import re
from typing import Any

# X.509 caps a common name at 64 characters, so this says what it needs to in
# the space available: a reviewer seeing it in `signer_name_unverified` must
# not be able to mistake it for a real accreditation authority.
SIGNER_COMMON_NAME = "Eval Throwaway Signing Key (not an authority)"


def throwaway_signer() -> Any:
    """A self-signed RSA key and certificate, generated in memory."""
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


def sign(pdf_bytes: bytes, signer: Any, field_name: str = "Signature1") -> bytes:
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers as pyhanko_signers

    writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
    output = pyhanko_signers.sign_pdf(
        writer,
        pyhanko_signers.PdfSignatureMetadata(field_name=field_name),
        signer=signer,
    )
    return output.getvalue()


def tamper_inside_signed_range(
    signed: bytes, printed: str, replacement: str
) -> bytes:
    """Edit printed text inside the signed byte range.

    A real content edit of the same length, so no offset in the file moves and
    the `/ByteRange` still describes the file correctly - which is exactly what
    makes the digest the only thing that can catch it.

    Requires the printed text to be literally present, which is why the
    templates save PDFs uncompressed. Raises rather than falling back to a
    byte flip at an arbitrary offset: a fixture that silently stopped being an
    edit to a real digit would still "pass" while measuring something else.
    """
    if len(printed) != len(replacement):
        raise ValueError(
            "the replacement must be the same length as the original, or "
            f"every later offset in the file moves ({printed!r} vs "
            f"{replacement!r})"
        )
    index = signed.find(printed.encode("latin-1"))
    if index < 0:
        raise ValueError(
            f"{printed!r} is not literally present in the signed bytes; the "
            "source PDF must be saved uncompressed for this edit to be a real "
            "content edit"
        )
    return (
        signed[:index]
        + replacement.encode("latin-1")
        + signed[index + len(printed) :]
    )


def append_incremental_revision(signed: bytes, note: str) -> bytes:
    """Add a valid incremental revision after signing.

    Written through pyHanko so the result is what a legitimate tool adding
    long-term validation data would produce. The signature over the earlier
    revision stays intact, which is the whole point: `bytes_after_signed_
    revision` is a finding about coverage, not about the digest.
    """
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    writer = IncrementalPdfFileWriter(io.BytesIO(signed))
    writer.root["/EvalAppendedAfterSigning"] = generic.pdf_string(note)
    writer.update_root()
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def byte_range_of(signed: bytes) -> list[int] | None:
    """The `/ByteRange` array, for the manifest. Read-only."""
    match = re.search(rb"/ByteRange\s*\[([^\]]*)\]", signed)
    if match is None:
        return None
    try:
        return [int(value) for value in match.group(1).split()]
    except ValueError:
        return None
