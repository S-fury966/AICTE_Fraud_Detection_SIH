"""Run the whole pipeline over one document and store everything.

    python -m app.pipeline <file> --institution <name> --category <category>
                           --actor <name> --role <role>
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..core.env import load_dotenv
from .runner import Pipeline, ensure_application


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline",
        description=(
            "Store a document, run steps 1-4 over it, and persist every stage "
            "result, finding and audit entry."
        ),
    )
    parser.add_argument("file", help="path to a .pdf, .jpg, .jpeg or .png")
    parser.add_argument("--institution", required=True, help="institution name")
    parser.add_argument(
        "--category", default="extension_of_approval", help="application category"
    )
    parser.add_argument("--actor", required=True, help="who is running this")
    parser.add_argument(
        "--role",
        default="officer",
        help="officer | senior_reviewer | admin | system",
    )
    parser.add_argument(
        "--document-type", default="accreditation_certificate", help="document type"
    )
    parser.add_argument(
        "--application",
        type=int,
        help="add to an existing application id instead of creating one",
    )
    parser.add_argument("--as-of", help="reference date (YYYY-MM-DD) for expiry checks")
    parser.add_argument(
        "--in-process-issuer",
        action="store_true",
        help=(
            "run the mock issuer in-process rather than over HTTP, so no "
            "server or port is needed"
        ),
    )
    parser.add_argument("--compact", action="store_true", help="single-line JSON")
    args = parser.parse_args(argv)

    # Step 2 needs the API key from .env.
    load_dotenv()

    transport: Any = None
    registry_hash: str | None = None
    if args.in_process_issuer:
        from fastapi.testclient import TestClient

        from ..mock_issuer.registry import load_registry
        from ..mock_issuer.service import create_app

        registry = load_registry()
        registry_hash = registry.sha256
        transport = TestClient(create_app(registry=registry))

    pipeline = Pipeline(
        issuer_transport=transport, registry_hash=registry_hash, as_of=args.as_of
    )

    try:
        application_id = args.application or ensure_application(
            pipeline.database,
            institution_name=args.institution,
            category=args.category,
            actor=args.actor,
            role=args.role,
            config=pipeline.persistence,
        )
        summary = pipeline.process_document(
            application_id=application_id,
            file_path=args.file,
            document_type=args.document_type,
            actor=args.actor,
            role=args.role,
        )
    except (ValueError, FileNotFoundError) as exc:
        _emit({"error": type(exc).__name__, "error_detail": str(exc)}, args.compact)
        return 1

    _emit(summary.to_dict(), args.compact)
    return 0


def _emit(payload: dict[str, Any], compact: bool) -> None:
    if compact:
        json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
