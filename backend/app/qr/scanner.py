"""Scan a document for QR codes and cross-check what they say.

The order is: get images, decode them, deduplicate, parse the payloads, then
run the checks. Nothing here fetches, resolves or opens a URL — the payload is
text from an untrusted source and is only ever read.

Errors are returned as data, as everywhere else in this project: a bad file
gives a status and a reason, never an exception out of the module.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..core.config import get, load_config
from . import checks
from .decoders import DecoderFailedError, QRDecoder, build_decoders
from .images import ImageLoadError, PasswordProtectedError, SourceImage, harvest
from .models import (
    ComparisonSkipped,
    Finding,
    FindingType,
    ImageSource,
    PayloadKind,
    QRAudit,
    QRDetection,
    QRError,
    QRScanResult,
    QRStatus,
)
from .payload import classify, extract_certificate_id, parse_url
from .preprocess import apply_rung


def scan_document(
    path: str | Path,
    config: dict[str, Any] | None = None,
    certificate_number: Any = None,
    issuing_body: Any = None,
    compare_certificate_number: bool = True,
    decoders: list[QRDecoder] | None = None,
) -> QRScanResult:
    """Find and cross-check the QR codes on one document.

    `compare_certificate_number` distinguishes "step 2 gave us no number" from
    "step 2 was never run": the first is a skipped comparison on a document we
    know about, the second is a scan with that check switched off entirely.
    """
    cfg = config if config is not None else load_config("qr_checks")
    source = Path(path)

    result = QRScanResult(source_path=str(path), status=QRStatus.OK)
    digest, size = _hash_file(source)
    built = decoders if decoders is not None else build_decoders(
        get(cfg, "decoders.order", ["opencv", "zxing"])
    )
    result.audit = QRAudit(
        config_version=str(get(cfg, "config_version", "unknown")),
        decoder_versions={d.name: _safe_version(d) for d in built},
        timestamp=datetime.now(timezone.utc).isoformat(),
        input_sha256=digest,
        input_bytes=size,
        pages_examined=0,
        images_examined=0,
        render_dpi=int(get(cfg, "images.render_dpi", 300)),
    )

    if not source.exists():
        result.status = QRStatus.DECODE_ERROR
        result.error = QRError.FILE_NOT_FOUND
        result.error_detail = f"no such file: {source}"
        return result

    allowed = [
        str(ext).lower()
        for ext in (get(cfg, "images.allowed_extensions", []) or [])
    ]
    if allowed and source.suffix.lower() not in allowed:
        result.status = QRStatus.DECODE_ERROR
        result.error = QRError.UNSUPPORTED_FORMAT
        result.error_detail = (
            f"{source.suffix or 'no extension'} is not one of {', '.join(allowed)}"
        )
        return result

    if not built:
        result.status = QRStatus.DECODE_ERROR
        result.error = QRError.DECODER_UNAVAILABLE
        result.error_detail = (
            "no QR decoder is installed. Install one with: "
            "pip install opencv-python zxing-cpp"
        )
        return result

    try:
        harvested = harvest(source, cfg)
    except PasswordProtectedError as exc:
        result.status = QRStatus.DECODE_ERROR
        result.error = QRError.PASSWORD_PROTECTED
        result.error_detail = str(exc)
        return result
    except ImageLoadError as exc:
        result.status = QRStatus.DECODE_ERROR
        result.error = QRError.CORRUPT_FILE
        result.error_detail = str(exc)
        return result

    result.audit.pages_examined = harvested.pages_examined
    result.audit.images_examined = harvested.images_examined
    result.audit.caps_reached = harvested.caps_reached
    result.pdf_link_hosts = harvested.link_hosts

    detections: list[QRDetection] = []
    unreadable = 0
    for image in harvested.images:
        found, regions = _decode_image(image, built, cfg)
        detections.extend(found)
        if not found:
            unreadable += regions

    detections = _deduplicate(detections)
    for detection in detections:
        _parse_payload(detection, cfg)

    result.detections = detections
    # A region that would not decode on a document where another QR read fine
    # is almost always the same code seen in a worse image, so it is not
    # reported as a separate unreadable region.
    result.unreadable_regions = 0 if detections else unreadable

    if not detections:
        result.status = QRStatus.NO_QR_FOUND

    result.findings, skipped = _run_checks(
        detections=detections,
        harvested_link_hosts=harvested.link_hosts,
        harvested_link_uris=harvested.link_uris,
        unreadable_regions=result.unreadable_regions,
        certificate_number=certificate_number,
        issuing_body=issuing_body,
        compare_certificate_number=compare_certificate_number,
        cfg=cfg,
    )
    result.comparisons_skipped = skipped
    return result


def _run_checks(
    detections: list[QRDetection],
    harvested_link_hosts: list[str],
    harvested_link_uris: list[str],
    unreadable_regions: int,
    certificate_number: Any,
    issuing_body: Any,
    compare_certificate_number: bool,
    cfg: dict[str, Any],
) -> tuple[list[Finding], list[ComparisonSkipped]]:
    findings: list[Finding] = []
    skipped: list[ComparisonSkipped] = []
    normalise_value = _certificate_number_normaliser()

    for detection in detections:
        for check in (
            checks.check_domain,
            checks.check_deceptive_patterns,
            checks.check_scheme,
        ):
            finding = check(detection, cfg)
            if finding is not None:
                findings.append(finding)

        if not compare_certificate_number:
            continue
        outcome = checks.check_certificate_id(
            detection, certificate_number, normalise_value, cfg
        )
        if isinstance(outcome, Finding):
            findings.append(outcome)
        elif isinstance(outcome, ComparisonSkipped):
            skipped.append(outcome)

    if not compare_certificate_number:
        skipped.append(
            ComparisonSkipped(
                check=FindingType.CERTIFICATE_ID_MISMATCH.value,
                reason=(
                    "no step 2 extraction was supplied, so the QR certificate "
                    "id was not compared against the document"
                ),
            )
        )

    multiple = checks.check_multiple_distinct(detections, cfg)
    if multiple is not None:
        findings.append(multiple)

    link_mismatch = checks.check_pdf_link_mismatch(
        detections, harvested_link_hosts, harvested_link_uris, cfg
    )
    if link_mismatch is not None:
        findings.append(link_mismatch)

    if not detections:
        unreadable = checks.check_unreadable(unreadable_regions, cfg)
        if unreadable is not None:
            findings.append(unreadable)
        else:
            not_found = checks.check_not_found(issuing_body, cfg)
            if not_found is not None:
                findings.append(not_found)

    # One finding per type per document, keeping the first. Ten pages carrying
    # the same off-allowlist host is one thing for a reviewer to look at.
    return _dedupe_findings(findings), skipped


def _dedupe_findings(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    kept: list[Finding] = []
    for finding in findings:
        key = (
            finding.type.value,
            str(finding.evidence.get("raw_qr_text", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(finding)
    return kept


def _decode_image(
    image: SourceImage, decoders: list[QRDecoder], cfg: dict[str, Any]
) -> tuple[list[QRDetection], int]:
    """Decode one image, climbing the preprocessing ladder only if needed.

    Returns the detections and, when nothing decoded, how many QR-like
    regions were nonetheless present.
    """
    include_boxes = bool(get(cfg, "output.include_bounding_boxes", True))

    for decoder in decoders:
        try:
            raw = decoder.decode(image.array)
        except DecoderFailedError:
            continue
        if raw:
            return (
                [
                    _to_detection(entry, image, decoder.name, None, include_boxes)
                    for entry in raw
                ],
                0,
            )

    for rung_name in get(cfg, "preprocessing.ladder", []) or []:
        variant = apply_rung(str(rung_name), image.array)
        if variant is None:
            continue
        for decoder in decoders:
            try:
                raw = decoder.decode(variant)
            except DecoderFailedError:
                continue
            if raw:
                return (
                    [
                        _to_detection(
                            entry, image, decoder.name, str(rung_name), include_boxes
                        )
                        for entry in raw
                    ],
                    0,
                )

    return [], _count_regions(image, decoders)


def _count_regions(image: SourceImage, decoders: list[QRDecoder]) -> int:
    for decoder in decoders:
        counter = getattr(decoder, "count_regions", None)
        if counter is None:
            continue
        try:
            count = int(counter(image.array))
        except Exception:
            continue
        if count:
            return count
    return 0


def _to_detection(
    raw: Any,
    image: SourceImage,
    decoder_name: str,
    preprocessing: str | None,
    include_boxes: bool,
) -> QRDetection:
    return QRDetection(
        raw_text=raw.text,
        page=image.page,
        bounding_box=raw.bounding_box if include_boxes else None,
        decoder=decoder_name,
        source=image.source,
        preprocessing=preprocessing,
    )


def _deduplicate(detections: list[QRDetection]) -> list[QRDetection]:
    """One printed QR is one QR, however many images we reached it through.

    A code printed on a page is found twice by design: once in the rendered
    page and once in the embedded image it was pasted from. Identical payload
    on the same page is therefore the same code, and the second sighting is
    recorded on the first rather than reported as another QR.
    """
    merged: dict[tuple[str, Any], QRDetection] = {}
    order: list[tuple[str, Any]] = []

    for detection in detections:
        key = (detection.raw_text, detection.page)
        existing = merged.get(key)
        if existing is None:
            merged[key] = detection
            order.append(key)
            continue
        label = detection.source.value
        if label != existing.source.value and label not in existing.duplicate_sources:
            existing.duplicate_sources.append(label)
        # Prefer the sighting that carries a bounding box, and the one that
        # needed no preprocessing: both make the better evidence link.
        if existing.bounding_box is None and detection.bounding_box is not None:
            existing.bounding_box = detection.bounding_box
        if existing.preprocessing is not None and detection.preprocessing is None:
            existing.preprocessing = None
            existing.decoder = detection.decoder

    return [merged[key] for key in order]


def _parse_payload(detection: QRDetection, cfg: dict[str, Any]) -> None:
    detection.payload_kind = classify(detection.raw_text)
    if detection.payload_kind is PayloadKind.URL:
        detection.url = parse_url(detection.raw_text)
    cert_id, origin = extract_certificate_id(
        detection.raw_text, detection.url, detection.payload_kind, cfg
    )
    detection.certificate_id = cert_id
    detection.certificate_id_source = origin


def _certificate_number_normaliser() -> Callable[[str], str]:
    """Step 2's own normalisation, so both modules compare identically."""
    from ..certificates.snippets import normalise
    from ..core.config import load_config as load

    try:
        certificate_cfg = load("certificate_extraction")
    except Exception:
        certificate_cfg = {}

    def normalise_value(value: str) -> str:
        return normalise(value, certificate_cfg)

    return normalise_value


def _hash_file(path: Path) -> tuple[str | None, int | None]:
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    return hashlib.sha256(data).hexdigest(), len(data)


def _safe_version(decoder: QRDecoder) -> str:
    try:
        return str(decoder.version())
    except Exception:
        return "unknown"
