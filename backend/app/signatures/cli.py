"""Command line entry point for step 6, part A.

    python -m app.signatures <file> [--document-type T] [--issuing-body B]

Integrity only. The output always carries `scope_note`, and every signer name
and signing time it prints is labelled unverified, because no certificate
chain and no trust store is consulted.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .checker import check_signatures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.signatures",
        description=(
            "Check whether a signed PDF was modified after signing. Does NOT "
            "verify the signer's identity: no certificate chain or trust "
            "store is consulted."
        ),
    )
    parser.add_argument("input", help="path to a .pdf")
    parser.add_argument(
        "--document-type",
        help=(
            "document type, used only to decide whether an unsigned document "
            "should be flagged (off unless config lists the type)"
        ),
    )
    parser.add_argument(
        "--issuing-body",
        help=(
            "issuing body as extracted in step 2, used only for the same "
            "unsigned-where-expected decision"
        ),
    )
    parser.add_argument("--compact", action="store_true", help="emit single-line JSON")
    args = parser.parse_args(argv)

    result = check_signatures(
        args.input,
        document_type=args.document_type,
        issuing_body=args.issuing_body,
    )
    _emit(result.to_dict(), args.compact)
    # A finding is not an error: the check ran and reported. Only a file we
    # could not read at all is a non-zero exit.
    return 1 if result.error is not None else 0


def _emit(payload: dict[str, Any], compact: bool) -> None:
    if compact:
        json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
