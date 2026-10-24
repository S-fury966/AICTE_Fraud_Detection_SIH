"""Command line entry point for step 6, part B.

    python -m app.duplicates <document_id>
    python -m app.duplicates --relationships <document_id>

Runs against documents already stored by the pipeline, which is why it takes a
document id rather than a file: a duplicate only exists relative to a corpus.

`--relationships` reads the stored `cross_references` for a document from both
ends without running any comparison, which is how order independence is
checked by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..core.config import load_config
from ..db import repository
from ..db.engine import Database
from ..db.roles import SYSTEM_ACTOR, SYSTEM_ROLE
from .detector import cross_references_for_document, detect_duplicates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.duplicates",
        description=(
            "Compare one stored document against the rest of the corpus. "
            "Comparison is internal only; no external image service is used."
        ),
    )
    parser.add_argument("document_id", type=int)
    parser.add_argument(
        "--relationships",
        action="store_true",
        help=(
            "print the stored cross-references for this document, from both "
            "ends, without running a new comparison"
        ),
    )
    parser.add_argument("--actor", default=SYSTEM_ACTOR)
    parser.add_argument("--role", default=SYSTEM_ROLE)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    persistence = load_config("persistence")
    database = Database(persistence)

    if args.relationships:
        with database.session() as session:
            payload = {
                "document_id": args.document_id,
                "relationships": cross_references_for_document(
                    session, args.document_id
                ),
            }
        _emit(payload, args.compact)
        return 0

    duplicates_cfg = load_config("duplicate_checks")

    # A standalone comparison is still a check, so it gets its own run: there
    # is no code path anywhere that adds results to a finished run.
    with database.session() as session:
        with session.begin():
            run = repository.start_check_run(
                session,
                document_id=args.document_id,
                actor=args.actor,
                role=args.role,
                config_versions={
                    "duplicate_checks": duplicates_cfg.get("config_version")
                },
                config_hashes={
                    "duplicate_checks": repository.config_hash(duplicates_cfg)
                },
                config=persistence,
            )
        run_id = run.id

    with database.session() as session:
        with session.begin():
            result = detect_duplicates(
                session,
                document_id=args.document_id,
                run_id=run_id,
                config=duplicates_cfg,
                certificate_number=repository.latest_certificate_number(
                    session, args.document_id
                ),
                qr_certificate_ids=repository.latest_qr_certificate_ids(
                    session, args.document_id
                ),
                qr_raw_payloads=repository.latest_qr_payloads(
                    session, args.document_id
                ),
            )
            repository.save_findings(
                session,
                run_id,
                detector=str(
                    duplicates_cfg.get("detector_name") or "duplicate_detection"
                ),
                findings=[finding.to_dict() for finding in result.findings],
                actor=args.actor,
                role=args.role,
                config=persistence,
            )

    with database.session() as session:
        with session.begin():
            repository.finish_check_run(
                session,
                run_id=run_id,
                status="completed",
                actor=args.actor,
                role=args.role,
                summary={"finding_count": len(result.findings)},
                config=persistence,
            )

    payload = result.to_dict()
    payload["run_id"] = run_id
    _emit(payload, args.compact)
    return 0


def _emit(payload: dict[str, Any], compact: bool) -> None:
    if compact:
        json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
