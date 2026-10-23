"""Command line entry point for step 4.

    python -m app.issuer <step2_extraction_json> [--qr <step3_json>]

The issuer URL comes from config/issuer_verification.yaml and from nowhere
else. A URL in the step 3 JSON is evidence, never a destination.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..core.verification import VerificationStatus
from .verifier import qr_id_from_scan, submitted_from_extraction, verify_certificate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.issuer",
        description=(
            "Check a certificate against the mock issuer service and compare "
            "the issuer's record with the submitted details."
        ),
    )
    parser.add_argument("extraction", help="path to a step 2 extraction .json file")
    parser.add_argument(
        "--qr",
        help=(
            "path to a step 3 QR scan .json file. Supplies a fallback "
            "certificate id; its URLs are never contacted."
        ),
    )
    parser.add_argument(
        "--as-of",
        help="reference date (YYYY-MM-DD) for expiry. Defaults to the step 2 setting.",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        help=(
            "run against the mock issuer in-process via FastAPI's TestClient, "
            "with no server to start and no port to open"
        ),
    )
    parser.add_argument("--compact", action="store_true", help="emit single-line JSON")
    args = parser.parse_args(argv)

    extraction = _read_json(args.extraction, args.compact)
    if extraction is None:
        return 1

    qr_certificate_id = None
    if args.qr:
        scan = _read_json(args.qr, args.compact)
        if scan is None:
            return 1
        qr_certificate_id = qr_id_from_scan(scan)

    transport = None
    registry_version = None
    registry_sha256 = None
    if args.in_process:
        from fastapi.testclient import TestClient

        from ..mock_issuer.registry import load_registry
        from ..mock_issuer.service import create_app

        registry = load_registry()
        registry_version = registry.version
        registry_sha256 = registry.sha256
        transport = TestClient(create_app(registry=registry))

    result = verify_certificate(
        submitted_from_extraction(extraction),
        qr_certificate_id=qr_certificate_id,
        transport=transport,
        source_path=extraction.get("source_path"),
        as_of=args.as_of,
        registry_version=registry_version,
        registry_sha256=registry_sha256,
    )

    _emit(result.to_dict(), args.compact)
    # Only a status that says nothing could be checked is a non-zero exit. A
    # mismatch is a successful run that found something.
    return 1 if result.status is VerificationStatus.SOURCE_UNAVAILABLE else 0


def _read_json(path: str, compact: bool) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        _emit({"error": "file_not_found", "error_detail": f"no such file: {target}"}, compact)
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit({"error": "corrupt_file", "error_detail": str(exc)}, compact)
        return None


def _emit(payload: dict[str, Any], compact: bool) -> None:
    if compact:
        json.dump(payload, sys.stdout, ensure_ascii=False)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
