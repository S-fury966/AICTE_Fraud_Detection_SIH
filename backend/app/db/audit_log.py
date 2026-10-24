"""The append-only, tamper-evident audit trail.

Each entry's hash covers its own content *and* the previous entry's hash. So
altering one entry, or removing one, invalidates every entry after it:
`verify_chain` recomputes the whole chain and reports the first break. An
attacker who can write to the database can still change a row — what they
cannot do is make the chain agree with the change.

Three deliberate limits:

- **Append only.** This module offers `append` and reads. There is no update
  and no delete, and SQLite triggers reject both at the database level too.
- **No payloads.** Details carry ids, counts and statuses — never document
  text and never raw model output. An audit trail that quoted the documents
  would become a second, unguarded copy of the sensitive content it exists to
  protect.
- **Canonical hashing.** The hashed form is JSON with sorted keys and no
  whitespace, so a reformat or a key reorder cannot read as tampering.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog

# The fields covered by the hash, in a fixed order.
_HASHED_FIELDS = (
    "timestamp",
    "actor",
    "role",
    "action",
    "entity_type",
    "entity_id",
    "details",
)


def compute_entry_hash(
    timestamp: datetime,
    actor: str,
    role: str,
    action: str,
    entity_type: str,
    entity_id: str | None,
    details: Any,
    previous_hash: str | None,
) -> str:
    payload = {
        "timestamp": _canonical_timestamp(timestamp),
        "actor": actor,
        "role": role,
        "action": action,
        "entity_type": entity_type,
        "entity_id": None if entity_id is None else str(entity_id),
        "details": details,
        "previous_hash": previous_hash,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: datetime) -> str:
    """Normalise to UTC ISO-8601.

    SQLite returns naive datetimes even for values written as aware, so a
    round trip would otherwise change the hashed string and break the chain.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def last_entry(session: Session) -> AuditLog | None:
    return session.scalars(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
    ).first()


def append(
    session: Session,
    actor: str,
    role: str,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Add one entry, linked to the current tail of the chain.

    Does not commit: the caller's transaction decides, so an audit entry and
    the thing it describes land together or not at all.
    """
    previous = last_entry(session)
    previous_hash = previous.entry_hash if previous else None
    timestamp = datetime.now(timezone.utc)

    entry = AuditLog(
        timestamp=timestamp,
        actor=actor,
        role=role,
        action=action,
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        details=details,
        previous_hash=previous_hash,
        entry_hash=compute_entry_hash(
            timestamp=timestamp,
            actor=actor,
            role=role,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            previous_hash=previous_hash,
        ),
    )
    session.add(entry)
    return entry


@dataclass
class ChainVerification:
    ok: bool
    entries_checked: int
    # The id of the first entry that does not verify, if any.
    first_broken_id: int | None = None
    problem: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "entries_checked": self.entries_checked,
            "first_broken_id": self.first_broken_id,
            "problem": self.problem,
        }


def verify_chain(session: Session) -> ChainVerification:
    """Recompute the chain and report the first entry that does not verify.

    Two kinds of break are distinguished, because they mean different things:
    a wrong `entry_hash` says the entry's own content was altered, while a
    wrong `previous_hash` says an entry was removed or reordered.
    """
    entries = list(session.scalars(select(AuditLog).order_by(AuditLog.id)).all())

    expected_previous: str | None = None
    for index, entry in enumerate(entries):
        if entry.previous_hash != expected_previous:
            return ChainVerification(
                ok=False,
                entries_checked=index,
                first_broken_id=entry.id,
                problem=(
                    f"entry {entry.id} expected previous_hash "
                    f"{expected_previous!r} but holds {entry.previous_hash!r}: "
                    f"an entry was removed, reordered or inserted"
                ),
            )

        recomputed = compute_entry_hash(
            timestamp=entry.timestamp,
            actor=entry.actor,
            role=entry.role,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            details=entry.details,
            previous_hash=entry.previous_hash,
        )
        if recomputed != entry.entry_hash:
            return ChainVerification(
                ok=False,
                entries_checked=index,
                first_broken_id=entry.id,
                problem=(
                    f"entry {entry.id} content does not match its entry_hash: "
                    f"the row was altered after it was written"
                ),
            )

        expected_previous = entry.entry_hash

    return ChainVerification(ok=True, entries_checked=len(entries))


def entries_for(
    session: Session, entity_type: str, entity_id: Any
) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == entity_type,
                AuditLog.entity_id == str(entity_id),
            )
            .order_by(AuditLog.id)
        ).all()
    )


def all_entries(session: Session) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).order_by(AuditLog.id)).all())
