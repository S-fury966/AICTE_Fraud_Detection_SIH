"""The deterministic checks. One function per finding type.

Every check is plain code against config. No model is consulted, and no check
decides anything: each returns an observation with its evidence, and a later
step is responsible for what that is worth.

Wording follows CLAUDE.md: a finding describes a mismatch or an unexpected
pattern. It never asserts forgery, and it never calls anything fake.
"""

from __future__ import annotations

from typing import Any

from ..core.config import get
from .models import (
    ComparisonSkipped,
    Finding,
    FindingType,
    ParsedURL,
    PayloadKind,
    QRDetection,
)
from .payload import host_labels


def strength_for(finding_type: FindingType, cfg: dict[str, Any]) -> str:
    """The configured label for a finding type. Never computed, always read."""
    return str(get(cfg, f"strength.{finding_type.value}", "weak"))


def host_is_allowlisted(host: str, cfg: dict[str, Any]) -> tuple[bool, str | None]:
    """Exact host match, or a subdomain match where the entry permits one.

    The dot boundary is the whole point. A bare `endswith` on the allowlisted
    string would accept `verify.nbta-demo.test.evil.test`, and a `contains`
    would accept anything at all, so the comparison is either equality or
    equality of the part after a literal leading dot.
    """
    target = (host or "").strip().lower().rstrip(".")
    if not target:
        return False, None

    for entry in get(cfg, "allowlist", []) or []:
        if isinstance(entry, str):
            allowed, subdomains = entry, False
        else:
            allowed = str(entry.get("host", ""))
            subdomains = bool(entry.get("allow_subdomains", False))
        allowed = allowed.strip().lower().rstrip(".")
        if not allowed:
            continue
        if target == allowed:
            return True, allowed
        if subdomains and target.endswith("." + allowed):
            return True, allowed
    return False, None


def check_domain(detection: QRDetection, cfg: dict[str, Any]) -> Finding | None:
    url = detection.url
    if detection.payload_kind is not PayloadKind.URL or url is None or not url.host:
        return None

    allowed, matched = host_is_allowlisted(url.host, cfg)
    if allowed:
        return None

    return Finding(
        type=FindingType.DOMAIN_NOT_ALLOWLISTED,
        strength=strength_for(FindingType.DOMAIN_NOT_ALLOWLISTED, cfg),
        reason=(
            f"The QR code points at host '{url.host}', which is not on the "
            f"allowlist of recognised verification domains. Manual "
            f"verification needed."
        ),
        evidence={
            "raw_qr_text": detection.raw_text,
            "page": detection.page,
            "field_compared": "qr_url_host",
            "qr_value": url.host,
            "allowlist": [
                entry.get("host") if isinstance(entry, dict) else entry
                for entry in (get(cfg, "allowlist", []) or [])
            ],
            "matched_entry": matched,
            "host_as_written": url.host_raw,
            "source": detection.source.value,
        },
    )


# The evidence block keeps the pattern codes, which are stable and greppable.
# The reason line is written for a reviewer, so it gets plain English: a code
# like "userinfo_present" in a sentence is an internal name leaking into the
# report.
_PATTERN_PHRASES = {
    "userinfo_present": (
        "puts text before an '@', which browsers ignore when choosing where to go"
    ),
    "punycode_host": "has a host written in punycode (xn--) rather than plain letters",
    "mixed_script_host": "has a host mixing two writing systems within one label",
    "ip_address_host": "points at a bare IP address rather than a named domain",
    "url_shortener_host": "uses a link shortener, which hides the real destination",
    "excessive_subdomain_depth": (
        "has more subdomain levels than expected, which can hide the real host"
    ),
    "non_standard_port": "uses a non-standard port",
}


def check_deceptive_patterns(
    detection: QRDetection, cfg: dict[str, Any]
) -> Finding | None:
    """Patterns whose only purpose is to make a URL read as something else."""
    url = detection.url
    if detection.payload_kind is not PayloadKind.URL or url is None or not url.host:
        return None

    patterns: list[str] = []
    details: dict[str, Any] = {}

    if url.userinfo is not None:
        patterns.append("userinfo_present")
        details["userinfo"] = url.userinfo

    if url.punycode:
        patterns.append("punycode_host")
        details["host_as_written"] = url.host_raw

    if url.mixed_script:
        patterns.append("mixed_script_host")

    if url.ip_host:
        patterns.append("ip_address_host")

    shorteners = {
        str(name).strip().lower()
        for name in (get(cfg, "deceptive.shorteners", []) or [])
    }
    if url.host in shorteners:
        patterns.append("url_shortener_host")
        details["shortener"] = url.host

    max_labels = int(get(cfg, "deceptive.max_host_labels", 4))
    labels = host_labels(url.host)
    if len(labels) > max_labels:
        patterns.append("excessive_subdomain_depth")
        details["host_label_count"] = len(labels)
        details["max_host_labels"] = max_labels

    standard_ports = {
        int(port) for port in (get(cfg, "deceptive.standard_ports", []) or [])
    }
    if url.port is not None and url.port not in standard_ports:
        patterns.append("non_standard_port")
        details["port"] = url.port

    if not patterns:
        return None

    return Finding(
        type=FindingType.DECEPTIVE_URL_PATTERN,
        strength=strength_for(FindingType.DECEPTIVE_URL_PATTERN, cfg),
        reason=(
            f"The QR URL {_join(_PATTERN_PHRASES[p] for p in patterns)}. This can "
            f"make the destination read as a different domain than the one it "
            f"actually points at. Manual verification needed."
        ),
        evidence={
            "raw_qr_text": detection.raw_text,
            "page": detection.page,
            "field_compared": "qr_url",
            "patterns": patterns,
            "host": url.host,
            **details,
        },
    )


def check_scheme(detection: QRDetection, cfg: dict[str, Any]) -> Finding | None:
    url = detection.url
    if detection.payload_kind is not PayloadKind.URL or url is None:
        return None
    if (url.scheme or "") != "http":
        return None

    return Finding(
        type=FindingType.SCHEME_INSECURE,
        strength=strength_for(FindingType.SCHEME_INSECURE, cfg),
        reason=(
            "The QR URL uses plain http rather than https, so a verification "
            "response could be altered in transit."
        ),
        evidence={
            "raw_qr_text": detection.raw_text,
            "page": detection.page,
            "field_compared": "qr_url_scheme",
            "qr_value": url.scheme,
            "expected": "https",
        },
    )


def check_certificate_id(
    detection: QRDetection,
    extracted_number: Any,
    normalise_value: Any,
    cfg: dict[str, Any],
) -> Finding | ComparisonSkipped | None:
    """Compare the QR's certificate id with the step 2 certificate_number.

    `normalise_value` is step 2's own normalisation, passed in rather than
    reimplemented: if the two modules normalised differently, the comparison
    would be meaningless in exactly the cases that matter.
    """
    if detection.certificate_id is None:
        return ComparisonSkipped(
            check=FindingType.CERTIFICATE_ID_MISMATCH.value,
            reason=(
                "no certificate id could be read from the QR payload, so there "
                "was nothing to compare"
            ),
        )

    if extracted_number is None:
        return ComparisonSkipped(
            check=FindingType.CERTIFICATE_ID_MISMATCH.value,
            reason=(
                "step 2 returned no validated certificate_number, so the QR "
                "certificate id could not be compared against the document"
            ),
        )

    qr_value = normalise_value(str(detection.certificate_id))
    document_value = normalise_value(str(extracted_number))
    if qr_value == document_value:
        return None

    return Finding(
        type=FindingType.CERTIFICATE_ID_MISMATCH,
        strength=strength_for(FindingType.CERTIFICATE_ID_MISMATCH, cfg),
        reason=(
            f"Mismatch detected between the certificate number printed on the "
            f"document ('{extracted_number}') and the one encoded in the QR "
            f"code ('{detection.certificate_id}')."
        ),
        evidence={
            "raw_qr_text": detection.raw_text,
            "page": detection.page,
            "field_compared": "certificate_number",
            "qr_value": detection.certificate_id,
            "document_value": extracted_number,
            "qr_value_normalised": qr_value,
            "document_value_normalised": document_value,
            "certificate_id_source": detection.certificate_id_source,
        },
    )


def check_multiple_distinct(
    detections: list[QRDetection], cfg: dict[str, Any]
) -> Finding | None:
    payloads = list(dict.fromkeys(d.raw_text for d in detections))
    if len(payloads) < 2:
        return None

    return Finding(
        type=FindingType.MULTIPLE_DISTINCT,
        strength=strength_for(FindingType.MULTIPLE_DISTINCT, cfg),
        reason=(
            f"{len(payloads)} different QR payloads were found on this "
            f"document, where one verification code is expected. Manual "
            f"verification needed."
        ),
        evidence={
            "payload_count": len(payloads),
            "payloads": payloads,
            "pages": sorted({d.page for d in detections if d.page is not None}),
            "field_compared": "qr_payload_count",
        },
    )


def expects_qr(issuing_body: Any, cfg: dict[str, Any]) -> bool:
    """Whether a document from this issuer would normally carry a QR.

    Without this, `qr_not_found` would fire on every document from an issuer
    that has never printed one — a finding that says nothing about the
    document and trains a reviewer to ignore the whole category.
    """
    expected = [
        str(name).strip().casefold()
        for name in (get(cfg, "expectations.issuers_expected_to_carry_qr", []) or [])
    ]
    if issuing_body is None:
        return bool(get(cfg, "expectations.default_expect_qr", True))

    body = str(issuing_body).casefold()
    return any(name and name in body for name in expected)


def check_not_found(issuing_body: Any, cfg: dict[str, Any]) -> Finding | None:
    if not expects_qr(issuing_body, cfg):
        return None

    return Finding(
        type=FindingType.NOT_FOUND,
        strength=strength_for(FindingType.NOT_FOUND, cfg),
        reason=(
            "No QR code was located on this document, although certificates "
            "from this issuer normally carry one. Manual verification needed."
        ),
        evidence={
            "field_compared": "qr_presence",
            "issuing_body": issuing_body,
            "expected_qr": True,
        },
    )


def check_unreadable(region_count: int, cfg: dict[str, Any]) -> Finding | None:
    if region_count < 1:
        return None

    return Finding(
        type=FindingType.UNREADABLE,
        strength=strength_for(FindingType.UNREADABLE, cfg),
        reason=(
            f"{region_count} QR-like region(s) were detected but could not be "
            f"decoded, possibly because of scan quality. Requires manual "
            f"verification."
        ),
        evidence={
            "field_compared": "qr_decodability",
            "region_count": region_count,
            "note": (
                "a region that will not decode is a read failure, not evidence "
                "about the document"
            ),
        },
    )


def check_pdf_link_mismatch(
    detections: list[QRDetection],
    link_hosts: list[str],
    link_uris: list[str],
    cfg: dict[str, Any],
) -> Finding | None:
    """Clickable link hosts in the PDF that differ from the QR host.

    A certificate whose printed QR and whose embedded hyperlink lead to
    different places is worth a reviewer's attention either way round.
    """
    qr_hosts = {
        d.url.host
        for d in detections
        if d.url is not None and d.url.host
    }
    if not qr_hosts or not link_hosts:
        return None

    differing = [host for host in link_hosts if host not in qr_hosts]
    if not differing:
        return None

    return Finding(
        type=FindingType.PDF_LINK_MISMATCH,
        strength=strength_for(FindingType.PDF_LINK_MISMATCH, cfg),
        reason=(
            f"Mismatch detected between the QR code host(s) "
            f"({_join(sorted(qr_hosts))}) and the clickable link host(s) "
            f"embedded in the PDF ({_join(differing)})."
        ),
        evidence={
            "field_compared": "qr_host_vs_pdf_link_host",
            "qr_hosts": sorted(qr_hosts),
            "pdf_link_hosts": link_hosts,
            "differing_hosts": differing,
            "pdf_link_uris": link_uris,
        },
    )


def _join(items: Any) -> str:
    values = [str(item) for item in items]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " and " + values[-1]
