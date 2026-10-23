"""Result types for QR detection and cross-checking.

Nothing here computes a risk level or a verification status. This module
produces findings: an observation, its strength label, the reason in plain
words, and the evidence behind it. Combining findings is a later step's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class QRStatus(str, Enum):
    OK = "ok"
    # A document with no QR is a normal outcome, not a failure.
    NO_QR_FOUND = "no_qr_found"
    DECODE_ERROR = "decode_error"


class QRError(str, Enum):
    FILE_NOT_FOUND = "file_not_found"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CORRUPT_FILE = "corrupt_file"
    PASSWORD_PROTECTED = "password_protected"
    DECODER_UNAVAILABLE = "decoder_unavailable"
    PAGE_RENDER_FAILED = "page_render_failed"


class FindingType(str, Enum):
    DOMAIN_NOT_ALLOWLISTED = "qr_domain_not_allowlisted"
    DECEPTIVE_URL_PATTERN = "qr_deceptive_url_pattern"
    SCHEME_INSECURE = "qr_scheme_insecure"
    CERTIFICATE_ID_MISMATCH = "qr_certificate_id_mismatch"
    MULTIPLE_DISTINCT = "qr_multiple_distinct"
    NOT_FOUND = "qr_not_found"
    UNREADABLE = "qr_unreadable"
    PDF_LINK_MISMATCH = "qr_pdf_link_mismatch"


class PayloadKind(str, Enum):
    URL = "url"
    PLAIN_TEXT = "plain_text"
    OTHER = "other"


class ImageSource(str, Enum):
    RENDERED_PAGE = "rendered_page"
    EMBEDDED_IMAGE = "embedded_image"
    # The submitted file was itself an image.
    IMAGE_FILE = "image_file"


@dataclass
class ParsedURL:
    """The parts of a QR URL that the checks look at."""

    scheme: str | None = None
    # Lowercased and IDNA-decoded where possible, so a punycode host is
    # compared as the characters it actually displays.
    host: str | None = None
    # The host exactly as written in the payload, before normalisation.
    host_raw: str | None = None
    port: int | None = None
    userinfo: str | None = None
    path: str | None = None
    query: dict[str, list[str]] = field(default_factory=dict)
    # True when the host was written in punycode (xn--).
    punycode: bool = False
    # True when the host mixes scripts, e.g. Latin with Cyrillic lookalikes.
    mixed_script: bool = False
    # True when the host is a bare IP address rather than a name.
    ip_host: bool = False
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "host_raw": self.host_raw,
            "port": self.port,
            "userinfo": self.userinfo,
            "path": self.path,
            "query": self.query,
            "punycode": self.punycode,
            "mixed_script": self.mixed_script,
            "ip_host": self.ip_host,
            "parse_error": self.parse_error,
        }


@dataclass
class QRDetection:
    """One distinct QR code found on the document."""

    raw_text: str
    page: int | None
    # [x, y, width, height] in pixels of the image it was decoded from, or
    # None when the decoder did not report a position.
    bounding_box: list[int] | None
    decoder: str
    source: ImageSource
    # None when the image decoded without any preprocessing.
    preprocessing: str | None = None
    payload_kind: PayloadKind = PayloadKind.OTHER
    url: ParsedURL | None = None
    # The certificate id read out of the payload, before any comparison.
    certificate_id: str | None = None
    # How it was located: "path", "query:<name>", "plain_text", or None.
    certificate_id_source: str | None = None
    # Other places the identical payload turned up on the same page. Recorded
    # rather than reported twice: one QR printed on a page is one QR, whether
    # we reach it by rendering the page or by pulling the embedded image.
    duplicate_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "page": self.page,
            "bounding_box": self.bounding_box,
            "decoder": self.decoder,
            "source": self.source.value,
            "preprocessing": self.preprocessing,
            "payload_kind": self.payload_kind.value,
            "url": self.url.to_dict() if self.url else None,
            "certificate_id": self.certificate_id,
            "certificate_id_source": self.certificate_id_source,
            "duplicate_sources": self.duplicate_sources,
        }


@dataclass
class Finding:
    """One observation for a reviewer, with what it rests on."""

    type: FindingType
    # "strong" or "weak", read from config. Deliberately a label and not a
    # number: this module does not score anything.
    strength: str
    # One plain sentence. Describes the mismatch, never the motive.
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "strength": self.strength,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass
class ComparisonSkipped:
    """A check that could not run, recorded so its silence is not read as a pass."""

    check: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "reason": self.reason}


@dataclass
class QRAudit:
    config_version: str
    # Name -> version, for every decoder that was available this run.
    decoder_versions: dict[str, str]
    timestamp: str
    input_sha256: str | None
    input_bytes: int | None
    pages_examined: int
    images_examined: int
    render_dpi: int
    # True when the page/image caps in config truncated the scan.
    caps_reached: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "decoder_versions": self.decoder_versions,
            "timestamp": self.timestamp,
            "input_sha256": self.input_sha256,
            "input_bytes": self.input_bytes,
            "pages_examined": self.pages_examined,
            "images_examined": self.images_examined,
            "render_dpi": self.render_dpi,
            "caps_reached": self.caps_reached,
        }


@dataclass
class QRScanResult:
    source_path: str | None
    status: QRStatus
    detections: list[QRDetection] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # Checks that could not run, and why.
    comparisons_skipped: list[ComparisonSkipped] = field(default_factory=list)
    # Regions that looked like a QR but would not decode.
    unreadable_regions: int = 0
    # Clickable link hosts found in the PDF, for the link cross-check.
    pdf_link_hosts: list[str] = field(default_factory=list)
    audit: QRAudit | None = None
    error: QRError | None = None
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "status": self.status.value,
            "detections": [d.to_dict() for d in self.detections],
            "findings": [f.to_dict() for f in self.findings],
            "comparisons_skipped": [c.to_dict() for c in self.comparisons_skipped],
            "unreadable_regions": self.unreadable_regions,
            "pdf_link_hosts": self.pdf_link_hosts,
            "audit": self.audit.to_dict() if self.audit else None,
            "error": self.error.value if self.error else None,
            "error_detail": self.error_detail,
        }
