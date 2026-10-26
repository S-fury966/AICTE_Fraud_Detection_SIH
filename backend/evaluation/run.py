"""Run the real pipeline over the generated set and score it.

    python -m evaluation.run [--mode oracle|live] [--split dev|holdout|all]
                             [--limit N] [--provider NAME] [--model NAME]
                             [--shuffle-seed N] [--resume] [--out DIR]

**What this module may and may not do.** It calls the real pipeline entry
point, `process_document()`, and it reads results back out of the database.
That is all it touches in `app`. It never asks a detector what a document is -
the labels already say, and they were written by the generator from the recipe.

**A fresh database per run.** Duplicate detection is defined against a corpus,
so a run that inherited last run's documents would score differently every
time, and "detection rate" would stop being a property of the detectors.

**Nothing leaves the process.** The mock issuer runs in-process through
Starlette's `TestClient`; no port is opened and no host outside this process is
contacted. In oracle mode there is no model call at all. A test arms a socket
trap over a short run and asserts the trap fires first.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import get, load_config
from app.db import repository
from app.db.engine import Database
from app.pipeline.runner import Pipeline, ensure_application

from . import metrics, report

HOLDOUT_WARNING = (
    "=" * 72
    + "\n"
    + "  TUNE ON DEV ONLY. Read the holdout numbers once, at the end.\n"
    + "  Every time a threshold is changed after looking at holdout, the\n"
    + "  holdout number stops being an estimate of unseen performance and\n"
    + "  becomes another dev number. Nothing in the code can enforce this.\n"
    + "=" * 72
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass
class RunContext:
    mode: str
    split: str
    out_root: Path
    labels: list[dict[str, Any]]
    manifest: dict[str, Any]
    run_config: dict[str, Any]
    generation_config: dict[str, Any]
    shuffle_seed: int | None = None


# --------------------------------------------------------------------------
# Loading the dataset
# --------------------------------------------------------------------------


_GENERATE_HINT = "Run: python -m evaluation.generate"


class DatasetNotFound(RuntimeError):
    """The generated evaluation dataset is missing or incomplete.

    Its message ends in the generate hint, so the CLI can print it and exit
    without a traceback - the dataset is gitignored output, and its absence is
    a step not yet run rather than a bug.
    """


def load_dataset(out_root: Path, generation_config: dict[str, Any]) -> tuple[list, dict]:
    labels_path = out_root / str(get(generation_config, "output.labels", "labels.json"))
    manifest_path = out_root / str(
        get(generation_config, "output.manifest", "manifest.json")
    )
    if not labels_path.exists() or not manifest_path.exists():
        missing = labels_path if not labels_path.exists() else manifest_path
        raise DatasetNotFound(
            f"Generated evaluation data not found at {out_root} "
            f"(missing {missing.name}). {_GENERATE_HINT}"
        )
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # A partially generated set (metadata present, a document gone) gets a
    # message naming the first missing file, not a bare FileNotFoundError from
    # inside the pipeline.
    for entry in labels:
        document = entry.get("document")
        if document and not (out_root / document).is_file():
            raise DatasetNotFound(
                f"Generated evaluation data at {out_root} is incomplete: "
                f"missing {document}. {_GENERATE_HINT}"
            )
    return labels, manifest


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


def build_provider(
    context: RunContext, label: dict[str, Any], certificate_config: dict[str, Any]
) -> Any:
    """The provider for one application.

    In oracle mode this is a fresh oracle per application, because the answer
    is that application's printed fields. In live mode it is the configured
    provider, optionally wrapped in the on-disk cache.
    """
    if context.mode == "oracle":
        from .oracle import OracleProvider

        return OracleProvider(label.get("printed_fields") or {})

    from app.llm.registry import get_provider

    # Resolved exactly the way step 2 resolves it, from the same keys. An
    # earlier version of this function guessed at the config shape and silently
    # produced provider "None"; reading `llm.provider` and
    # `llm.providers.<name>` is not optional cleverness, it is the contract.
    name = str(
        get(context.run_config, "provider.override", None)
        or get(certificate_config, "llm.provider", "groq")
    )
    settings = dict(get(certificate_config, f"llm.providers.{name}", {}) or {})
    settings.setdefault("temperature", get(certificate_config, "llm.temperature", 0))

    model_override = get(context.run_config, "provider.model_override", None)
    if model_override:
        settings["model"] = model_override

    provider = get_provider(name, settings)

    if bool(get(context.run_config, "cache.enabled", True)):
        from app.llm.cache import CachingProvider

        root = _repo_root() / str(
            get(context.run_config, "cache.root", "data/eval/llm_cache")
        )
        provider = CachingProvider(
            provider,
            root=root,
            prompt_version=str(get(certificate_config, "prompt.version", "v1")),
            enabled=True,
            read_only=bool(get(context.run_config, "cache.read_only", False)),
        )
    return provider


# --------------------------------------------------------------------------
# Reading one application's results back
# --------------------------------------------------------------------------


def qr_config_for_world(
    out_root: Path, generation_config: dict[str, Any]
) -> dict[str, Any] | None:
    """Production QR config, with this world's issuers on the allowlist.

    The shipped allowlist names the demo issuer only, which is right for the
    demo and wrong here: the evaluation world has five issuing bodies, and
    without this every genuine certificate from four of them would be reported
    as pointing at an unapproved domain. An allowlist is **policy**, and the
    policy for a world is the set of bodies that exist in it.

    What this deliberately does NOT do is weaken the matching rule. Each host
    is added with `allow_subdomains: false`, so step 3's equality-or-dot-
    boundary check still rejects `verify.nbta-demo.test.evil-mirror.test` -
    the lookalike fraud case stays detectable, and a test asserts it.
    """
    world_path = out_root / str(get(generation_config, "output.world", "world.json"))
    if not world_path.exists():
        return None

    world = json.loads(world_path.read_text(encoding="utf-8"))
    config = dict(load_config("qr_checks"))
    hosts = sorted(
        {
            str(body["domain"])
            for body in world.get("issuing_bodies", [])
            if body.get("domain")
        }
    )
    config["allowlist"] = [
        {"host": host, "allow_subdomains": False} for host in hosts
    ]
    config["config_version"] = (
        f"{config.get('config_version', 'qr-checks')}+eval-world-allowlist"
    )
    return config


def _findings_for(database: Database, run_id: int) -> list[tuple[str, str]]:
    with database.session() as session:
        return [
            (row.type, row.strength)
            for row in repository.findings_for_run(session, run_id)
        ]


def _qr_facts(summary: Any) -> tuple[bool, list[str]]:
    for stage in summary.stages:
        if stage.stage != "qr_checks":
            continue
        payload = stage.payload or {}
        detections = payload.get("detections") or []
        return bool(detections), sorted(
            {str(entry.get("source")) for entry in detections if entry.get("source")}
        )
    return False, []


def _llm_facts(summary: Any) -> tuple[int, int, int, bool]:
    for stage in summary.stages:
        if stage.stage != "certificate_fields":
            continue
        audit = (stage.payload or {}).get("audit") or {}
        return (
            int(audit.get("attempts") or 0),
            int(audit.get("input_tokens") or 0),
            int(audit.get("output_tokens") or 0),
            bool(audit.get("cache_hit")),
        )
    return 0, 0, 0, False


def _extracted_values(summary: Any) -> dict[str, Any]:
    for stage in summary.stages:
        if stage.stage != "certificate_fields":
            continue
        fields = (stage.payload or {}).get("fields") or {}
        return {
            name: entry.get("value")
            for name, entry in fields.items()
            if isinstance(entry, dict) and entry.get("validated")
        }
    return {}


def _compare_fields(
    printed: dict[str, Any], extracted: dict[str, Any]
) -> dict[str, bool]:
    """Per-field extraction accuracy against what was printed.

    Compared after whitespace and case normalisation, and `programme` is
    compared as a comma-split set, because the schema asks for a list where
    the page prints one line - exactly the canonicalisation step 4 already
    applies to both sides.
    """
    result: dict[str, bool] = {}
    for name, expected in (printed or {}).items():
        actual = extracted.get(name)
        if expected is None:
            # A field the generator did not print. Correct means the extractor
            # also returned nothing - inventing a value is an error, not a
            # neutral outcome.
            result[name] = actual in (None, "", [])
            continue
        if name == "programme":
            want = {
                part.strip().casefold()
                for part in str(expected).split(",")
                if part.strip()
            }
            got: set[str] = set()
            for item in actual if isinstance(actual, list) else [actual]:
                for part in str(item or "").split(","):
                    if part.strip():
                        got.add(part.strip().casefold())
            result[name] = bool(want) and want == got
            continue
        result[name] = _norm(expected) == _norm(actual)
    return result


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def run_once(context: RunContext, verbose: bool = True) -> dict[str, Any]:
    """Process every selected application and score the results."""
    from fastapi.testclient import TestClient

    from app.mock_issuer.registry import load_registry
    from app.mock_issuer.service import create_app

    run_config = context.run_config
    results_dir = context.out_root / str(
        get(context.generation_config, "output.results_dir", "results")
    )
    results_dir.mkdir(parents=True, exist_ok=True)

    # A fresh, file-backed database per run, under the results folder, so a
    # finished run can still be inspected with the ordinary report CLI.
    persistence = dict(load_config("persistence"))
    persistence["database"] = dict(persistence["database"])
    # The suffix must include the shuffle seed: a shuffled run is a second,
    # independent run over the same set, and sharing one database file would
    # have it delete the normal run's results before they could be inspected.
    suffix = run_suffix(context)
    database_path = results_dir / f"eval-{suffix}.db"
    if database_path.exists():
        database_path.unlink()
    persistence["database"]["url"] = f"sqlite:///{database_path.as_posix()}"
    persistence["storage"] = dict(persistence["storage"])
    persistence["storage"]["root"] = str(results_dir / f"store-{suffix}")

    certificate_config = dict(load_config("certificate_extraction"))
    qr_config = qr_config_for_world(context.out_root, context.generation_config)

    registry_path = _repo_root() / str(
        get(run_config, "issuer.registry", "data/eval/issuer_registry.json")
    )
    registry = load_registry(registry_path)
    transport = TestClient(create_app(registry=registry))

    database = Database(persistence)

    selected = _select(context)
    if verbose:
        print(
            f"running {len(selected)} applications | mode={context.mode} "
            f"| split={context.split} | registry={registry_path.name} "
            f"({registry.data.get('record_count')} records)"
        )

    scores: list[metrics.ApplicationScore] = []
    by_application: dict[int, metrics.ApplicationScore] = {}
    findings_by_application: dict[int, list[tuple[str, str]]] = {}
    consecutive_failures = 0
    failure_limit = int(get(run_config, "limits.consecutive_failure_limit", 5))
    cache_stats: dict[str, Any] = {}

    for position, label in enumerate(selected, start=1):
        application_label = int(label["application_id"])
        document = context.out_root / str(label["document"])

        institution = str(label["institution"])
        application_id = ensure_application(
            database,
            institution_name=institution,
            category="extension_of_approval",
            actor=str(get(run_config, "pipeline.actor", "evaluation.harness")),
            role=str(get(run_config, "pipeline.role", "system")),
            config=persistence,
        )

        provider = build_provider(context, label, certificate_config)
        pipeline = Pipeline(
            database=database,
            llm_provider=provider,
            issuer_transport=transport,
            # Captured, never performed: a retry must not cost wall time in a
            # measurement, and the retry schedule is step 4's business anyway.
            issuer_sleep=lambda seconds: None,
            configs=(
                {"persistence": persistence, "qr_checks": qr_config}
                if qr_config
                else {"persistence": persistence}
            ),
            registry_hash=registry.sha256,
            as_of=str(get(run_config, "pipeline.as_of", "2026-07-01")),
        )

        started = time.perf_counter()
        error: str | None = None
        summary = None
        try:
            summary = pipeline.process_document(
                application_id=application_id,
                file_path=document,
                document_type=str(
                    get(run_config, "pipeline.document_type", "accreditation_certificate")
                ),
                actor=str(get(run_config, "pipeline.actor", "evaluation.harness")),
                role=str(get(run_config, "pipeline.role", "system")),
            )
            consecutive_failures = 0
        except Exception as exc:
            # The pipeline is built to return errors as data, so reaching here
            # means the harness is wrong, not the document.
            error = f"{type(exc).__name__}: {exc}"
            consecutive_failures += 1
        duration_ms = (time.perf_counter() - started) * 1000

        findings = _findings_for(database, summary.run_id) if summary else []
        findings_by_application[application_label] = findings

        score = metrics.score_application(
            label,
            findings,
            pair_findings=None,
            flagged_min_strength=str(
                get(run_config, "scoring.flagged_min_strength", "medium")
            ),
            strength_order=tuple(
                get(run_config, "scoring.strength_order", metrics.DEFAULT_STRENGTH_ORDER)
            ),
        )
        score.duration_ms = duration_ms
        score.error = error
        score.noise_level = str((label.get("noise") or {}).get("level", "none"))
        score.form = str(label.get("form", ""))
        score.qr_expected = label.get("qr_payload") is not None

        if summary is not None:
            score.stage_durations = {
                stage.stage: stage.duration_ms for stage in summary.stages
            }
            score.issuer_status = summary.verification_status
            score.qr_detected, score.qr_sources = _qr_facts(summary)
            calls, input_tokens, output_tokens, cache_hit = _llm_facts(summary)
            score.llm_calls = calls
            score.input_tokens = input_tokens
            score.output_tokens = output_tokens
            score.cache_hit = cache_hit
            if context.mode == "live":
                score.field_accuracy = _compare_fields(
                    label.get("printed_fields") or {}, _extracted_values(summary)
                )

        if score.expected_issuer_status:
            score.issuer_status_ok = (
                score.issuer_status == score.expected_issuer_status
            )

        scores.append(score)
        by_application[application_label] = score

        if verbose:
            mark = "." if not error else "E"
            flag = "F" if score.flagged else "-"
            print(
                f"  [{position:>3}/{len(selected)}] app {application_label:>3} "
                f"{label['case_type']:<42} {mark}{flag} "
                f"{duration_ms:>7.0f}ms  findings={len(findings)}"
            )

        if hasattr(provider, "stats"):
            # Accumulated, not overwritten. Each application gets its own
            # provider instance, so the last one's counters describe one
            # application rather than the run - an earlier version reported
            # "1 miss" for a five-application run.
            stats = provider.stats()
            for counter in ("hits", "misses", "writes"):
                cache_stats[counter] = cache_stats.get(counter, 0) + int(
                    stats.get(counter, 0)
                )
            for field in ("enabled", "read_only", "root", "prompt_version"):
                cache_stats[field] = stats.get(field)

        if consecutive_failures >= failure_limit:
            print(
                f"\nstopping: {consecutive_failures} consecutive harness "
                "failures. This is a harness problem, not a document problem - "
                "the pipeline returns errors as data."
            )
            break

    # Pair findings, resolved after the loop because the second half of a
    # duplicate pair may be processed later than the first.
    for score in scores:
        label = next(
            entry
            for entry in context.labels
            if int(entry["application_id"]) == score.application_id
        )
        pair = label.get("pair_application_id")
        if pair is None:
            continue
        rescored = metrics.score_application(
            label,
            findings_by_application.get(score.application_id, []),
            pair_findings=findings_by_application.get(int(pair), []),
            flagged_min_strength=str(
                get(run_config, "scoring.flagged_min_strength", "medium")
            ),
        )
        score.satisfied_groups = rescored.satisfied_groups
        score.unsatisfied_groups = rescored.unsatisfied_groups
        score.detected = rescored.detected

    database.dispose()

    percentile = int(get(run_config, "output.latency_percentile", 95))
    include_fields = context.mode == "live"

    dev = [s for s in scores if s.split == "dev"]
    holdout = [s for s in scores if s.split == "holdout"]

    payload = {
        "config_version": str(run_config.get("config_version", "unknown")),
        "generation_config_version": context.manifest.get("config_version"),
        "generation_seed": context.manifest.get("seed"),
        "mode": context.mode,
        "split_selected": context.split,
        "shuffle_seed": context.shuffle_seed,
        "applications_run": len(scores),
        "registry": str(registry_path),
        "registry_records": registry.data.get("record_count"),
        "qr_allowlist_hosts": [
            entry["host"] for entry in (qr_config or {}).get("allowlist", [])
        ],
        "cache": cache_stats,
        "extraction_accuracy_reported": include_fields,
        "note_on_oracle_mode": (
            "In oracle mode the model's answers ARE the ground truth, so "
            "extraction accuracy is not reported. The snippet guard still "
            "applies, so fields can still be lost to unreadable input."
        ),
        "dev": metrics.summarise(dev, percentile, include_fields),
        "holdout": metrics.summarise(holdout, percentile, include_fields),
        "all": metrics.summarise(scores, percentile, include_fields),
        "per_application": [score.to_dict() for score in scores],
    }

    _write_results(context, results_dir, payload, scores)
    return payload


def _select(context: RunContext) -> list[dict[str, Any]]:
    """The applications to run, in processing order unless shuffled."""
    labels = list(context.labels)
    if context.split != "all":
        labels = [
            entry for entry in labels if str(entry.get("split")) == context.split
        ]
    labels.sort(key=lambda entry: int(entry["processing_order"]))

    limit = get(context.run_config, "limits.max_applications", None)
    if limit:
        labels = labels[: int(limit)]

    if context.shuffle_seed is not None:
        rng = random.Random(context.shuffle_seed)
        rng.shuffle(labels)
    return labels


def run_suffix(context: RunContext) -> str:
    """Names every artefact of one run, so two runs never collide."""
    suffix = context.mode
    if context.shuffle_seed is not None:
        suffix += f"-shuffled{context.shuffle_seed}"
    return suffix


def _write_results(
    context: RunContext,
    results_dir: Path,
    payload: dict[str, Any],
    scores: list[metrics.ApplicationScore],
) -> None:
    suffix = run_suffix(context)

    results_path = results_dir / f"results-{suffix}.json"
    results_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    csv_path = results_dir / f"per_application-{suffix}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "application_id",
                "case_type",
                "split",
                "is_fraud",
                "flagged",
                "detected",
                "finding_count",
                "finding_types",
                "must_not_violations",
                "issuer_status",
                "expected_issuer_status",
                "issuer_status_ok",
                "qr_expected",
                "qr_detected",
                "noise_level",
                "form",
                "duration_ms",
                "llm_calls",
                "input_tokens",
                "output_tokens",
                "cache_hit",
                "error",
            ]
        )
        for score in scores:
            writer.writerow(
                [
                    score.application_id,
                    score.case_type,
                    score.split,
                    score.is_fraud,
                    score.flagged,
                    "" if score.detected is None else score.detected,
                    len(score.findings),
                    "|".join(name for name, _ in score.findings),
                    "|".join(v["type"] for v in score.must_not_violations),
                    score.issuer_status or "",
                    score.expected_issuer_status or "",
                    "" if score.issuer_status_ok is None else score.issuer_status_ok,
                    score.qr_expected,
                    score.qr_detected,
                    score.noise_level,
                    score.form,
                    round(score.duration_ms, 1),
                    score.llm_calls,
                    score.input_tokens,
                    score.output_tokens,
                    score.cache_hit,
                    score.error or "",
                ]
            )

    summary_path = results_dir / f"summary-{suffix}.md"
    summary_path.write_text(report.summary_markdown(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# Order independence
# --------------------------------------------------------------------------


RELATIONSHIP_FINDINGS = (
    "duplicate_file_exact",
    "certificate_number_reuse",
    "qr_payload_reuse",
    "near_duplicate_page_image",
)


def compare_orderings(
    normal: dict[str, Any],
    shuffled: dict[str, Any],
    labels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Do the duplicate relationships survive a different processing order?

    Each relationship is keyed by the **unordered pair of case types** it
    joins, plus the finding type. Getting that key right took two attempts and
    the first one is worth recording, because it produced a false alarm that
    looked exactly like a real bug.

    Keying on application id is obviously wrong: ids are assigned in processing
    order, so a shuffled run numbers the same documents differently.

    Keying on the *discovering* document's case type is subtly wrong, which is
    worse. A relationship is recorded by whichever of the two documents is
    processed second, so reversing the order moves the finding from the
    duplicate to its source - and the comparison reported three relationships
    "only in normal order" and three "only in shuffled order" when they were
    the same three relationships seen from the other end.

    The unordered pair of case types is invariant under reordering, which is
    what the claim actually is: the same relationships are found, whoever finds
    them.
    """
    case_types = {
        int(entry["application_id"]): str(entry["case_type"])
        for entry in (labels or [])
    }
    pairs = {
        int(entry["application_id"]): entry.get("pair_application_id")
        for entry in (labels or [])
    }

    def relationships(payload: dict[str, Any]) -> set[tuple[str, str, str]]:
        found: set[tuple[str, str, str]] = set()
        for entry in payload.get("per_application", []):
            application_id = int(entry["application_id"])
            mine = case_types.get(application_id, str(entry["case_type"]))
            pair_id = pairs.get(application_id)
            theirs = case_types.get(int(pair_id)) if pair_id else None
            for finding in entry.get("findings", []):
                if finding["type"] not in RELATIONSHIP_FINDINGS:
                    continue
                ends = sorted(filter(None, (mine, theirs))) or [mine]
                found.add(
                    (ends[0], ends[-1] if len(ends) > 1 else ends[0],
                     str(finding["type"]))
                )
        return found

    left = relationships(normal)
    right = relationships(shuffled)
    return {
        "identical": left == right,
        "key": "unordered pair of case types plus finding type",
        "only_in_normal_order": sorted(left - right),
        "only_in_shuffled_order": sorted(right - left),
        "shared": sorted(left & right),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.run",
        description=(
            "Run the real pipeline over the generated evaluation set and "
            "score the results against the generator's labels."
        ),
    )
    parser.add_argument("--mode", choices=["oracle", "live"], default=None)
    parser.add_argument(
        "--split", choices=["dev", "holdout", "all"], default="all"
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--shuffle-seed",
        type=int,
        default=None,
        help=(
            "run the same set in a different order and compare the duplicate "
            "relationships found against the normal order"
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the on-disk LLM response cache (live mode)",
    )
    parser.add_argument("--out", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    # The CLI loads .env itself, so a live run needs no exported variables.
    # This is the project's own loader (app/core/env.py), not python-dotenv:
    # the dependency is not installed and is not needed for reading one file.
    from app.core.env import load_dotenv

    load_dotenv()

    generation_config = dict(load_config("eval_generation"))
    run_config = dict(load_config("eval_run"))
    run_config["provider"] = dict(run_config.get("provider") or {})
    run_config["limits"] = dict(run_config.get("limits") or {})
    run_config["cache"] = dict(run_config.get("cache") or {})

    if args.provider:
        run_config["provider"]["override"] = args.provider
    if args.model:
        run_config["provider"]["model_override"] = args.model
    if args.limit is not None:
        run_config["limits"]["max_applications"] = args.limit
    if args.no_cache:
        run_config["cache"]["enabled"] = False

    mode = args.mode or str(get(run_config, "mode.default", "oracle"))
    out_root = (
        Path(args.out)
        if args.out
        else _repo_root() / str(get(generation_config, "output.root", "data/eval"))
    )

    try:
        labels, manifest = load_dataset(out_root, generation_config)
    except DatasetNotFound as exc:
        print(exc, file=sys.stderr)
        return 1

    context = RunContext(
        mode=mode,
        split=args.split,
        out_root=out_root,
        labels=labels,
        manifest=manifest,
        run_config=run_config,
        generation_config=generation_config,
    )

    if bool(get(generation_config, "split.holdout_warning", True)) and not args.quiet:
        print(HOLDOUT_WARNING)

    payload = run_once(context, verbose=not args.quiet)

    if args.shuffle_seed is not None:
        shuffled_context = RunContext(
            mode=mode,
            split=args.split,
            out_root=out_root,
            labels=labels,
            manifest=manifest,
            run_config=run_config,
            generation_config=generation_config,
            shuffle_seed=args.shuffle_seed,
        )
        if not args.quiet:
            print(f"\nre-running in shuffled order (seed {args.shuffle_seed})")
        shuffled = run_once(shuffled_context, verbose=not args.quiet)
        comparison = compare_orderings(payload, shuffled, labels)
        payload["order_independence"] = comparison
        if not args.quiet:
            print("\norder independence:")
            print(f"  identical relationship set: {comparison['identical']}")
            if not comparison["identical"]:
                print(f"  only in normal order:   {comparison['only_in_normal_order']}")
                print(f"  only in shuffled order: {comparison['only_in_shuffled_order']}")

    if not args.quiet:
        print()
        print(report.console_table(payload))
        print(HOLDOUT_WARNING)

    return 0


if __name__ == "__main__":
    sys.exit(main())
