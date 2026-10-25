"""Load the demo dataset into the database by running the real pipeline.

    python -m demo.load [--fresh] [--database-url URL]

Every document goes through `Pipeline.process_document` exactly as an upload
through the API would, with the mock issuer in-process (no port needed) and the
reference date pinned to `world.AS_OF`, so the demo always tells the same
story. Nothing here writes a finding or a status directly.

`--fresh` deletes the configured SQLite database file first (and only that
file) so the demo starts from an empty queue. The audit chain lives in that
file, so this is a reset of the demo, not an edit of its history.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import world

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_ACTOR = "officer.demo"
DEMO_ROLE = "officer"


def make_pipeline(database_url: str | None = None):
    from fastapi.testclient import TestClient

    from app.core.config import load_config
    from app.db.engine import Database
    from app.mock_issuer.registry import load_registry
    from app.mock_issuer.service import create_app
    from app.pipeline.runner import Pipeline

    registry = load_registry()
    persistence = load_config("persistence")
    database = Database(persistence, url=database_url) if database_url else None
    return Pipeline(
        database=database,
        issuer_transport=TestClient(create_app(registry=registry)),
        registry_hash=registry.sha256,
        as_of=world.AS_OF,
    )


def run_scenarios(pipeline: Any, data_dir: Path, only: list[str] | None = None) -> list[dict[str, Any]]:
    from app.pipeline.runner import ensure_application
    from app.scoring import assess_application

    labels = json.loads((data_dir / "labels.json").read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for application in labels["applications"]:
        if only and application["key"] not in only:
            continue
        started = time.perf_counter()
        application_id = ensure_application(
            pipeline.database,
            institution_name=application["institution"],
            category="extension_of_approval",
            actor=DEMO_ACTOR,
            role=DEMO_ROLE,
            config=pipeline.persistence,
        )
        documents = []
        for document in application["documents"]:
            summary = pipeline.process_document(
                application_id=application_id,
                file_path=data_dir / document["file"],
                document_type=document["document_type"],
                actor=DEMO_ACTOR,
                role=DEMO_ROLE,
                claim=document.get("claim"),
            )
            documents.append({"file": document["file"], "run_id": summary.run_id, "status": summary.status, "findings": summary.finding_types})
        pipeline.check_application(application_id, DEMO_ACTOR, DEMO_ROLE)
        with pipeline.database.session() as session:
            with session.begin():
                risk = assess_application(session, application_id, trigger="submission", actor=DEMO_ACTOR, role=DEMO_ROLE, persistence=pipeline.persistence)
        results.append(
            {
                "key": application["key"],
                "application_id": application_id,
                "institution": application["institution"],
                "seconds": round(time.perf_counter() - started, 2),
                "risk_level": risk["level"],
                "risk_score": risk["score"],
                "documents": documents,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    from app.core.config import load_config
    from app.db.engine import resolve_url

    parser = argparse.ArgumentParser(prog="python -m demo.load", description="Run the demo dataset through the pipeline.")
    parser.add_argument("--fresh", action="store_true", help="delete the configured SQLite database first")
    parser.add_argument("--database-url", help="override the database URL")
    parser.add_argument("--data", default=str(REPO_ROOT / "data" / "demo"))
    parser.add_argument("--only", help="comma-separated scenario keys")
    args = parser.parse_args(argv)

    url = args.database_url or resolve_url(load_config("persistence"))
    if args.fresh and url.startswith("sqlite:///"):
        path = Path(url[len("sqlite:///") :])
        if path.exists() and path.suffix == ".db":
            path.unlink()
            print(f"removed {path}", file=sys.stderr)

    pipeline = make_pipeline(args.database_url)
    results = run_scenarios(pipeline, Path(args.data), only=args.only.split(",") if args.only else None)
    for row in results:
        print(f"{row['application_id']:>3}  {row['risk_level']:<6} {row['risk_score']:>6}  {row['seconds']:>6}s  {row['institution']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
