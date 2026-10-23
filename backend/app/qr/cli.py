"""Command line entry point for step 3.

    python -m app.qr <file> [--extraction <step2_json>]

Without --extraction the certificate id comparison cannot run; the output
says so in `comparisons_skipped` rather than quietly omitting the check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .models import QRStatus
from .scanner import scan_document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.qr",
        description=(
            "Find QR codes on a certificate and cross-check them against the "
            "step 2 extracted fields. Never fetches or resolves any URL."
        ),
    )
    parser.add_argument("input", help="path to a .pdf, .jpg, .jpeg or .png")
    parser.add_argument(
        "--extraction",
        help=(
            "path to a step 2 certificate extraction .json file. Without it, "
            "the certificate id comparison is skipped."
        ),
    )
    parser.add_argument("--compact", action="store_true", help="emit single-line JSON")
    args = parser.parse_args(argv)

    certificate_number: Any = None
    issuing_body: Any = None
    compare = False

    if args.extraction:
        extraction_path = Path(args.extraction)
        if not extraction_path.exists():
            _emit(
                {
                    "status": "decode_error",
                    "error": "file_not_found",
                    "error_detail": f"no such extraction file: {extraction_path}",
                },
                args.compact,
            )
            return 1
        try:
            extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _emit(
                {
                    "status": "decode_error",
                    "error": "corrupt_file",
                    "error_detail": f"could not read extraction JSON: {exc}",
                },
                args.compact,
            )
            return 1
        certificate_number = _field_value(extraction, "certificate_number")
        issuing_body = _field_value(extraction, "issuing_body")
        compare = True

    result = scan_document(
        args.input,
        certificate_number=certificate_number,
        issuing_body=issuing_body,
        compare_certificate_number=compare,
    )
    _emit(result.to_dict(), args.compact)
    # A document with no QR is a normal outcome, so only a decode error is a
    # non-zero exit.
    return 0 if result.status is not QRStatus.DECODE_ERROR else 1


def _field_value(extraction: dict[str, Any], name: str) -> Any:
    """Read one validated value out of a step 2 result."""
    fields = extraction.get("fields") or {}
    entry = fields.get(name) or {}
    if not isinstance(entry, dict):
        return None
    # Only a value step 2 validated is worth comparing against. An
    # unvalidated one was already rejected as untraceable to the source.
    if entry.get("validated") is not True:
        return None
    return entry.get("value")


def _emit(payload: dict[str, Any], compact: bool) -> None:
    if compact:
        json.dump(payload, sys.stdout, ensure_ascii=False)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
