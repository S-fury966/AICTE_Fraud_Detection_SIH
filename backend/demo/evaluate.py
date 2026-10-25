"""Measure the four detectors on the demo set: detection, false positives, time.

    python -m demo.evaluate [--out data/demo/evaluation.json]

Runs every demo application through the real pipeline into a **fresh
temporary database**, so the numbers never depend on what someone clicked in
the dashboard. Labels come from `labels.json`, which the generator wrote from
its recipes; nothing here asks a detector what a case is.

Reported:
- detection rate per injected fraud type (a type counts as detected when any
  finding in its expected group fires at or above the group's min strength);
- false positives: medium-or-stronger findings on the clean applications, and
  clean applications not at LOW review priority;
- honest-but-flag-expected cases (revoked, expired) reported separately: the
  system is right to surface them, and counting them as false positives would
  invite suppressing the issuer-state findings;
- average processing time per application and per document.

Ten applications is a demonstration, not a benchmark. The step 7 evaluation
(`python -m evaluation.run`) is the larger certificate-only measurement.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import world
from .load import make_pipeline, run_scenarios

REPO_ROOT = Path(__file__).resolve().parents[2]
_RANK = {"weak": 1, "medium": 2, "strong": 3}


def evaluate(data_dir: Path) -> dict[str, Any]:
    from app.scoring import latest_findings_for_application

    labels = json.loads((data_dir / "labels.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as scratch:
        url = f"sqlite:///{Path(scratch) / 'evaluation.db'}"
        pipeline = make_pipeline(url)
        started = time.perf_counter()
        results = run_scenarios(pipeline, data_dir)
        wall = time.perf_counter() - started

        findings_by_app: dict[str, list[dict[str, Any]]] = {}
        with pipeline.database.session() as session:
            for row in results:
                findings_by_app[row["key"]] = latest_findings_for_application(session, row["application_id"])
        pipeline.database.dispose()

    by_key = {row["key"]: row for row in results}
    per_type: dict[str, dict[str, Any]] = {}
    honest: list[dict[str, Any]] = []
    clean: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []

    for application in labels["applications"]:
        key = application["key"]
        findings = findings_by_app.get(key, [])
        fired = {(f["type"], f["strength"]) for f in findings}
        case = {
            "key": key,
            "institution": application["institution"],
            "is_fraud": application["is_fraud"],
            "fraud_types": application["fraud_types"],
            "risk_level": by_key[key]["risk_level"],
            "risk_score": by_key[key]["risk_score"],
            "seconds": by_key[key]["seconds"],
            "detected": {},
        }
        for group in application["expected"]:
            hit = sorted(
                t for t, s in fired if t in group["any_of"] and _RANK.get(s, 0) >= _RANK[group["min_strength"]]
            )
            entry = per_type.setdefault(group["fraud_type"], {"cases": 0, "detected": 0, "examples": []})
            entry["cases"] += 1
            entry["detected"] += 1 if hit else 0
            entry["examples"].append({"application": key, "detected": bool(hit), "by": hit})
            case["detected"][group["fraud_type"]] = hit
        for flag in application.get("expected_honest_flags", []):
            honest.append({"application": key, "flag": flag, "raised": any(t == flag for t, _ in fired)})
        if not application["is_fraud"]:
            offending = [f for f in findings if _RANK.get(f["strength"], 0) >= _RANK["medium"]]
            clean.append(
                {
                    "application": key,
                    "risk_level": by_key[key]["risk_level"],
                    "medium_or_strong_findings": [f["type"] for f in offending],
                    "weak_findings": len(findings) - len(offending),
                }
            )
        cases.append(case)

    for entry in per_type.values():
        entry["detection_rate"] = round(entry["detected"] / entry["cases"], 3) if entry["cases"] else None

    documents = sum(len(row["documents"]) for row in results)
    fraud_apps = [c for c in cases if c["is_fraud"]]
    return {
        "dataset": "sih-2026-demo",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": world.AS_OF,
        "applications": len(results),
        "documents": documents,
        "per_fraud_type": dict(sorted(per_type.items())),
        "overall_detection_rate": round(
            sum(e["detected"] for e in per_type.values()) / max(1, sum(e["cases"] for e in per_type.values())), 3
        ),
        "false_positives": {
            "clean_applications": len(clean),
            "clean_with_medium_or_strong_finding": sum(1 for c in clean if c["medium_or_strong_findings"]),
            "clean_not_low_priority": sum(1 for c in clean if c["risk_level"] != "low"),
            "medium_or_strong_findings_on_clean": sum(len(c["medium_or_strong_findings"]) for c in clean),
            "detail": clean,
        },
        "honest_flags_expected": {
            "count": len(honest),
            "raised": sum(1 for h in honest if h["raised"]),
            "detail": honest,
        },
        "prioritisation": {
            "fraud_applications_at_high": sum(1 for c in fraud_apps if c["risk_level"] == "high"),
            "fraud_applications": len(fraud_apps),
        },
        "timing": {
            "wall_seconds": round(wall, 2),
            "avg_seconds_per_application": round(sum(r["seconds"] for r in results) / max(1, len(results)), 3),
            "avg_seconds_per_document": round(sum(r["seconds"] for r in results) / max(1, documents), 3),
        },
        "cases": cases,
        "caveat": (
            "Ten synthetic applications built to demonstrate each detector. "
            "Thresholds were measured on the same synthetic renders, so these "
            "rates show the detectors work as designed, not how they would "
            "perform on real submissions."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo.evaluate", description="Evaluate the detectors on the demo set.")
    parser.add_argument("--data", default=str(REPO_ROOT / "data" / "demo"))
    parser.add_argument("--out", default=str(REPO_ROOT / "data" / "demo" / "evaluation.json"))
    args = parser.parse_args(argv)
    report = evaluate(Path(args.data))
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = {k: report[k] for k in ("overall_detection_rate", "prioritisation", "timing")}
    summary["per_fraud_type"] = {k: v["detection_rate"] for k, v in report["per_fraud_type"].items()}
    summary["false_positives"] = {k: v for k, v in report["false_positives"].items() if k != "detail"}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
