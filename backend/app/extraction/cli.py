"""Command line entry point: print one document's extraction result as JSON."""

from __future__ import annotations

import argparse
import json
import sys

from .extractor import extract_document
from .models import ExtractionStatus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.extraction",
        description="Extract per-page text from a PDF or image as JSON.",
    )
    parser.add_argument("file", help="path to a .pdf, .jpg, .jpeg or .png file")
    parser.add_argument(
        "--compact", action="store_true", help="emit single-line JSON"
    )
    parser.add_argument(
        "--no-text",
        action="store_true",
        help="omit page text, so only the per-page decisions are shown",
    )
    args = parser.parse_args(argv)

    result = extract_document(args.file)
    payload = result.to_dict()

    if args.no_text:
        for page in payload["pages"]:
            page["text"] = f"<{len(page['text'])} chars omitted>"
            if page.get("ocr_text") is not None:
                page["ocr_text"] = f"<{len(page['ocr_text'])} chars omitted>"

    json.dump(
        payload,
        sys.stdout,
        indent=None if args.compact else 2,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")

    # Non-zero exit only when nothing could be extracted, so scripts can
    # branch on it while still accepting partial results.
    return 1 if result.status is ExtractionStatus.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
