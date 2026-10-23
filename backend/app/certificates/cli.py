"""Command line entry point for certificate field extraction.

Takes either a step 1 extraction JSON file, or a document (PDF or image),
which is run through step 1 first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..core.env import load_dotenv
from ..extraction import extract_document
from ..extraction.models import ExtractionStatus
from .extractor import extract_from_document_result
from .models import FieldExtractionStatus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.certificates",
        description=(
            "Extract accreditation certificate fields from a step 1 extraction "
            "JSON file, or straight from a PDF or image."
        ),
    )
    parser.add_argument(
        "input",
        help="path to a step 1 extraction .json file, or a .pdf/.jpg/.jpeg/.png",
    )
    parser.add_argument("--compact", action="store_true", help="emit single-line JSON")
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="omit the raw model responses from the audit block",
    )
    args = parser.parse_args(argv)

    # The API key lives in the repository .env. Real environment variables
    # already set are left alone, so a deployment can override it.
    load_dotenv()

    path = Path(args.input)
    if not path.exists():
        json.dump({"status": "failed", "error": f"no such file: {path}"}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    if path.suffix.lower() == ".json":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            json.dump(
                {"status": "failed", "error": f"could not read {path}: {exc}"},
                sys.stdout,
            )
            sys.stdout.write("\n")
            return 1
    else:
        extraction = extract_document(path)
        if extraction.status is ExtractionStatus.FAILED:
            json.dump(
                {
                    "status": "failed",
                    "error": "step 1 text extraction failed",
                    "extraction": extraction.to_dict(),
                },
                sys.stdout,
                indent=None if args.compact else 2,
            )
            sys.stdout.write("\n")
            return 1
        document = extraction.to_dict()

    result = extract_from_document_result(document)
    payload = result.to_dict()

    if args.no_raw and payload.get("audit"):
        payload["audit"]["raw_responses"] = []

    json.dump(
        payload,
        sys.stdout,
        indent=None if args.compact else 2,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")

    return 0 if result.status is FieldExtractionStatus.OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
