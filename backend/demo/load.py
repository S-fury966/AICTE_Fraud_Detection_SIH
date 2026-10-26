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

_GENERATE_HINT = "Run: python -m demo.generate"


class DatasetNotFound(RuntimeError):
    """The generated demo dataset is missing or incomplete.

    Carries a message ending in the generate hint, so a CLI can print it and
    exit without a traceback - the generated files are gitignored output, and
    a missing file here is an operator step not yet run, not a bug.
    """


def ensure_demo_dataset(data_dir: Path) -> dict[str, Any]:
    """Check the dataset exists and every file it references is present.

    Returns the parsed labels on success. Raises `DatasetNotFound` naming the
    first thing missing - the applications folder, `labels.json`, or the first
    referenced document - so the message is specific rather than a bare
    FileNotFoundError surfacing later from deep in the pipeline.
    """
    labels_path = data_dir / "labels.json"
    applications = data_dir / "applications"
    if not applications.is_dir() or not labels_path.is_file():
        missing = applications if not applications.is_dir() else labels_path
        raise DatasetNotFound(
            f"Generated demo data not found at {data_dir} (missing {missing.name}). "
            f"{_GENERATE_HINT}"
        )
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    for application in labels.get("applications", []):
        for document in application.get("documents", []):
            referenced = data_dir / document["file"]
            if not referenced.is_file():
                raise DatasetNotFound(
                    f"Generated demo data at {data_dir} is incomplete: "
                    f"missing {document['file']}. {_GENERATE_HINT}"
                )
    return labels


def make_pipeline(database_url: str | None = None, database=None):
    from fastapi.testclient import TestClient

    from app.core.config import load_config
    from app.db.engine import Database
    from app.mock_issuer.registry import load_registry
    from app.mock_issuer.service import create_app
    from app.pipeline.runner import Pipeline

    registry = load_registry()
    persistence = load_config("persistence")
    # A prebuilt Database (from the temporary-database helper) wins; otherwise
    # build one from the url, or let the Pipeline use the configured default.
    if database is None and database_url:
        database = Database(persistence, url=database_url)
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
            fallback_used = any(
                st.stage == "certificate_fields"
                and bool((st.payload or {}).get("audit", {}).get("fallback_used"))
                for st in summary.stages
            )
            documents.append({"file": document["file"], "run_id": summary.run_id, "status": summary.status, "findings": summary.finding_types, "fallback_used": fallback_used})
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
                "fallback_documents": sum(1 for d in documents if d.get("fallback_used")),
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

    try:
        ensure_demo_dataset(Path(args.data))
    except DatasetNotFound as exc:
        print(exc, file=sys.stderr)
        return 1

    url = args.database_url or resolve_url(load_config("persistence"))
    if args.fresh and url.startswith("sqlite:///"):
        path = Path(url[len("sqlite:///") :])
        if path.exists() and path.suffix == ".db":
            path.unlink()
            print(f"removed {path}", file=sys.stderr)

    pipeline = make_pipeline(args.database_url)
    results = run_scenarios(pipeline, Path(args.data), only=args.only.split(",") if args.only else None)
    for row in results:
        fb = f"  offline-reader×{row['fallback_documents']}" if row.get("fallback_documents") else ""
        print(f"{row['application_id']:>3}  {row['risk_level']:<6} {row['risk_score']:>6}  {row['seconds']:>6}s  {row['institution']}{fb}")
    total_fallback = sum(r.get("fallback_documents", 0) for r in results)
    print(
        f"certificate fields: {total_fallback} document(s) read by the offline "
        f"reader, the rest by {pipeline.configs['certificate_extraction']['llm']['provider']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
