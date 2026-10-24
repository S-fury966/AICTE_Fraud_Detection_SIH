"""Print the full stored report for one application.

    python -m app.reports <application_id> [--all-runs]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..db import repository
from ..db.engine import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.reports",
        description="Print everything stored for one application, as JSON.",
    )
    parser.add_argument("application_id", type=int)
    parser.add_argument(
        "--all-runs",
        action="store_true",
        help="include every run per document, not only the latest",
    )
    parser.add_argument(
        "--actor", help="recorded in the audit log as having read the report"
    )
    parser.add_argument("--role", default="officer")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    database = Database()
    try:
        with database.session() as session:
            report = repository.get_application_report(
                session,
                application_id=args.application_id,
                all_runs=args.all_runs,
                actor=args.actor,
                role=args.role,
            )
    except ValueError as exc:
        _emit({"error": "not_found", "error_detail": str(exc)}, args.compact)
        return 1

    _emit(report, args.compact)
    return 0


def _emit(payload: dict[str, Any], compact: bool) -> None:
    if compact:
        json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    sys.exit(main())
