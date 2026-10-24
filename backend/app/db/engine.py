"""Engine and session construction, plus the two database-level guarantees.

1. **Foreign keys are enforced.** SQLite ignores them unless each connection
   says otherwise, so a `PRAGMA foreign_keys=ON` is issued on connect. Without
   it a deleted application would silently leave orphaned runs and findings,
   and the orphans would still be served to a reviewer.

2. **audit_log rejects UPDATE and DELETE.** Triggers refuse both at the
   database level. The code also exposes no way to do either; this is the
   second lock, for the case where something reaches the database directly.

Schema creation is `create_all` plus a `schema_version` row. No Alembic yet:
the prototype's schema still moves in whole steps, and a migration tool whose
history nobody trusts is worse than none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import get, load_config
from .models import Base, SchemaVersion

SCHEMA_VERSION = "schema-v1"

_AUDIT_TRIGGERS = {
    "audit_log_no_update": """
        CREATE TRIGGER IF NOT EXISTS audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is rejected');
        END;
    """,
    "audit_log_no_delete": """
        CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is rejected');
        END;
    """,
}


def repo_root() -> Path:
    # backend/app/db/engine.py -> repository root
    return Path(__file__).resolve().parents[3]


def resolve_url(config: dict[str, Any] | None = None) -> str:
    """The database URL, with a relative SQLite path anchored to the repo root.

    Without the anchoring, the database a command writes to would depend on the
    directory it was run from.
    """
    cfg = config if config is not None else load_config("persistence")
    url = str(get(cfg, "database.url", "sqlite:///data/sih_2026.db"))

    prefix = "sqlite:///"
    if url.startswith(prefix):
        raw = url[len(prefix) :]
        if raw and raw != ":memory:" and not Path(raw).is_absolute():
            absolute = (repo_root() / raw).resolve()
            absolute.parent.mkdir(parents=True, exist_ok=True)
            return prefix + absolute.as_posix()
    return url


def create_engine_from_config(
    config: dict[str, Any] | None = None, url: str | None = None
) -> Engine:
    from sqlalchemy import create_engine as _create_engine

    cfg = config if config is not None else load_config("persistence")
    target = url if url is not None else resolve_url(cfg)
    echo = bool(get(cfg, "database.echo", False))

    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if target.startswith("sqlite"):
        # check_same_thread off so a TestClient's worker thread can share the
        # session; the prototype is single-writer either way.
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in target:
            # An in-memory database lives in its connection. Without a shared
            # pool every session would get its own empty database.
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool

    engine = _create_engine(target, **kwargs)

    if target.startswith("sqlite") and bool(
        get(cfg, "database.sqlite_foreign_keys", True)
    ):
        _enable_sqlite_foreign_keys(engine)

    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def foreign_keys_enforced(engine: Engine) -> bool:
    """Read the pragma back, rather than trusting that setting it worked."""
    if not engine.url.get_backend_name().startswith("sqlite"):
        # Postgres and friends enforce them unconditionally.
        return True
    with engine.connect() as connection:
        value = connection.execute(text("PRAGMA foreign_keys")).scalar()
    return bool(value)


def install_audit_triggers(engine: Engine) -> list[str]:
    """Add the append-only triggers. Returns the names installed.

    SQLite only: Postgres would use a RULE or a privilege, which is a
    deployment decision rather than something to create from application code.
    """
    if not engine.url.get_backend_name().startswith("sqlite"):
        return []
    installed: list[str] = []
    with engine.begin() as connection:
        for name, statement in _AUDIT_TRIGGERS.items():
            connection.execute(text(statement))
            installed.append(name)
    return installed


def audit_triggers_installed(engine: Engine) -> list[str]:
    if not engine.url.get_backend_name().startswith("sqlite"):
        return []
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='trigger'")
        ).all()
    return sorted(row[0] for row in rows)


def init_database(
    engine: Engine, config: dict[str, Any] | None = None
) -> Engine:
    """Create tables, stamp the schema version, install the audit triggers."""
    cfg = config if config is not None else load_config("persistence")
    Base.metadata.create_all(engine)

    if bool(get(cfg, "audit.enforce_append_only", True)):
        install_audit_triggers(engine)

    with Session(engine) as session:
        existing = session.get(SchemaVersion, SCHEMA_VERSION)
        if existing is None:
            session.add(
                SchemaVersion(
                    version=SCHEMA_VERSION,
                    note="created by app.db.engine.init_database",
                )
            )
            session.commit()

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit off so a caller can still read the objects it just
    # wrote after the stage's transaction has closed.
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Database:
    """An initialised engine plus its session factory.

    Each pipeline stage takes its own session from here, so a failure in a
    later stage cannot roll back an earlier one's writes.
    """

    def __init__(
        self, config: dict[str, Any] | None = None, url: str | None = None
    ) -> None:
        self.config = config if config is not None else load_config("persistence")
        self.engine = create_engine_from_config(self.config, url=url)
        init_database(self.engine, self.config)
        self._sessions = session_factory(self.engine)

    def session(self) -> Session:
        return self._sessions()

    def schema_version(self) -> str | None:
        with self.session() as session:
            row = session.get(SchemaVersion, SCHEMA_VERSION)
            return row.version if row else None

    def table_names(self) -> list[str]:
        return sorted(inspect(self.engine).get_table_names())

    def dispose(self) -> None:
        self.engine.dispose()
