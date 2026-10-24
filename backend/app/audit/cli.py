"""Inspect and verify the audit trail.

    python -m app.audit verify
    python -m app.audit list [--limit N]

The chain logic lives in `app.db.audit_log`; this is only its command line.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..db import audit_log
from ..db.engine import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.audit",
        description="Verify or list the append-only, hash-chained audit trail.",
    )
    # --compact is declared on the parent and on each subcommand, so both
    # "app.audit --compact list" and the natural "app.audit list --compact"
    # work. argparse does not share a parent's optionals with subparsers.
    parser.add_argument("--compact", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser(
        "verify", help="recompute the hash chain and report any break"
    )
    verify.add_argument("--compact", action="store_true")

    listing = sub.add_parser("list", help="print audit entries oldest first")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--compact", action="store_true")

    args = parser.parse_args(argv)

    database = Database()
    with database.session() as session:
        if args.command == "verify":
            verification = audit_log.verify_chain(session)
            _emit(verification.to_dict(), args.compact)
            # Non-zero when the chain is broken, so this is usable in a check.
            return 0 if verification.ok else 1

        entries = audit_log.all_entries(session)
        payload = {
            "entry_count": len(entries),
            "entries": [
                {
                    "id": entry.id,
                    "timestamp": str(entry.timestamp),
                    "actor": entry.actor,
                    "role": entry.role,
                    "action": entry.action,
                    "entity_type": entry.entity_type,
                    "entity_id": entry.entity_id,
                    "details": entry.details,
                    "entry_hash": entry.entry_hash[:16] + "...",
                }
                for entry in entries[-args.limit :]
            ],
        }
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
