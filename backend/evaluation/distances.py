"""Re-measure the perceptual-hash thresholds across varied layouts.

    python -m evaluation.distances [--out DIR] [--kind dhash] [--size 16]

Step 6's `near_duplicate_max_distance: 12` was measured on **one** synthetic
template rendered by one script. A perceptual hash is sensitive to layout, so
that number was calibrated rather than validated. This module measures the
same thing across the five evaluation layouts, four forms and four noise
levels, and reports whether 12 still separates what it is supposed to.

**Report only.** Nothing here writes config. If the numbers say the threshold
is wrong, that is a finding to hand to a human, not a value to quietly move -
and a threshold that moved itself would make every earlier run
incomparable.

Pair types, which are the whole point of the exercise:

- `same_document_recompressed` - the same certificate arriving in another
  form. **Must** be inside the threshold or a resubmission goes unnoticed.
- `same_template_other_institution` - two genuine certificates from one body.
  Inside the threshold is tolerable (the finding is weak by design); far
  outside would mean the hash is not seeing the template at all.
- `edited_copy` - a source document with one field changed. Should behave like
  the recompressed case, because it nearly is one.
- `unrelated` - different institution, different layout, different content.
  **Must** be outside the threshold, with margin.
- `same_number_different_layout` - the same certificate number re-rendered on
  a different template. Measured and reported, but **excluded from the
  verdict**: these pages are not meant to look alike, and the certificate
  number check is what catches them. Charging the perceptual hash with a miss
  here would be measuring it against a job it was never given.
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from app.core.config import get, load_config
from app.qr.images import render_page_image
from app.similarity import hashing


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _classify(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    """Which pair type two applications form, or None to skip the pair.

    Returns None rather than a catch-all for pairs whose relationship is
    ambiguous - a fraud case against another fraud case, say. A measurement
    bucket that quietly absorbed everything unclassified would stop meaning
    what its name says.
    """
    left_source = left.get("source_application_id")
    right_source = right.get("source_application_id")

    related = (
        left_source == right["application_id"]
        or right_source == left["application_id"]
        or (left_source is not None and left_source == right_source)
    )

    if related:
        # `number_reuse_other_institution` deliberately re-renders the same
        # number on a DIFFERENT layout, so the two pages are not supposed to
        # look alike - the number check is what catches it, not the image.
        # Bucketing it as a recompressed copy would charge the perceptual hash
        # with a miss it was never meant to catch.
        relayout = {"number_reuse_other_institution"}
        if left["case_type"] in relayout or right["case_type"] in relayout:
            return "same_number_different_layout"
        edited = {"resaved_copy_with_edit"}
        if left["case_type"] in edited or right["case_type"] in edited:
            return "edited_copy"
        return "same_document_recompressed"

    if left["case_type"] == "clean" and right["case_type"] == "clean":
        if left["template"] == right["template"]:
            if left["institution_id"] != right["institution_id"]:
                return "same_template_other_institution"
            return None
        return "unrelated"

    if (
        left["case_type"].startswith("hard_negative_same_template")
        and right["case_type"].startswith("hard_negative_same_template")
        and left["institution_id"] != right["institution_id"]
    ):
        return "same_template_other_institution"

    return None


def measure(
    out_root: Path | None = None,
    kind: str | None = None,
    hash_size: int | None = None,
) -> dict[str, Any]:
    """Hash every generated document and report the distance distributions."""
    generation_config = load_config("eval_generation")
    duplicate_config = load_config("duplicate_checks")

    root = Path(
        out_root
        if out_root is not None
        else _repo_root() / str(get(generation_config, "output.root", "data/eval"))
    )
    manifest_path = root / str(get(generation_config, "output.manifest", "manifest.json"))
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"no manifest at {manifest_path}. Run `python -m evaluation.generate`."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Production settings by default, so the numbers are about the threshold
    # actually in use rather than one chosen to look good.
    compare_kind = kind or str(get(duplicate_config, "perceptual_hash.compare_kind", "dhash"))
    size = int(
        hash_size or get(duplicate_config, "perceptual_hash.hash_size", 16)
    )
    dpi = int(get(duplicate_config, "perceptual_hash.render_dpi", 150))
    page = int(get(duplicate_config, "perceptual_hash.page", 1))
    threshold = int(
        get(duplicate_config, "perceptual_hash.near_duplicate_max_distance", 12)
    )

    applications = list(manifest.get("applications") or [])
    labels_path = root / str(get(generation_config, "output.labels", "labels.json"))
    labels = {
        int(entry["application_id"]): entry
        for entry in json.loads(labels_path.read_text(encoding="utf-8"))
    }
    for entry in applications:
        label = labels.get(int(entry["application_id"])) or {}
        entry["institution_id"] = label.get("institution_id", "")

    hashes: dict[int, Any] = {}
    failures: list[dict[str, Any]] = []
    for entry in applications:
        document = root / str(entry["document"])
        try:
            image = render_page_image(document, page, dpi)
            hashes[int(entry["application_id"])] = hashing.compute(
                image, compare_kind, hash_size=size
            )
        except Exception as exc:
            failures.append(
                {
                    "application_id": entry["application_id"],
                    "document": entry["document"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    buckets: dict[str, list[dict[str, Any]]] = {}
    for left, right in itertools.combinations(applications, 2):
        left_id = int(left["application_id"])
        right_id = int(right["application_id"])
        if left_id not in hashes or right_id not in hashes:
            continue
        pair_type = _classify(left, right)
        if pair_type is None:
            continue
        distance = hashing.hamming_distance(hashes[left_id], hashes[right_id])
        buckets.setdefault(pair_type, []).append(
            {
                "left": left_id,
                "right": right_id,
                "distance": distance,
                "left_case": left["case_type"],
                "right_case": right["case_type"],
                "left_template": left["template"],
                "right_template": right["template"],
                "left_form": left["form"],
                "right_form": right["form"],
            }
        )

    report: dict[str, Any] = {}
    for pair_type in sorted(buckets):
        pairs = buckets[pair_type]
        values = sorted(entry["distance"] for entry in pairs)
        report[pair_type] = {
            "pairs": len(values),
            "min": values[0],
            "max": values[-1],
            "median": statistics.median(values),
            "inside_threshold": sum(1 for v in values if v <= threshold),
            "outside_threshold": sum(1 for v in values if v > threshold),
            # The extremes are what a threshold lives or dies by, so they are
            # listed rather than summarised away.
            "closest_pairs": pairs[:0]
            or sorted(pairs, key=lambda e: e["distance"])[:3],
            "furthest_pairs": sorted(pairs, key=lambda e: -e["distance"])[:3],
        }

    verdict = _verdict(report, threshold)

    return {
        "hash_kind": compare_kind,
        "hash_size": size,
        "hash_bits": size * size,
        "render_dpi": dpi,
        "page": page,
        "threshold_in_config": threshold,
        "documents_hashed": len(hashes),
        "render_failures": failures,
        "templates": manifest.get("template_counts"),
        "forms": manifest.get("form_counts"),
        "pair_types": report,
        "verdict": verdict,
    }


def _verdict(report: dict[str, Any], threshold: int) -> dict[str, Any]:
    """Does the configured threshold still separate what it should?

    The question is asked as two one-sided checks rather than one accuracy
    figure, because the two errors are not interchangeable: a missed
    resubmission costs a reviewer nothing to catch later, and an unrelated
    document flagged as a duplicate costs an honest institution a review.
    """
    same = report.get("same_document_recompressed") or {}
    edited = report.get("edited_copy") or {}
    unrelated = report.get("unrelated") or {}
    sibling = report.get("same_template_other_institution") or {}

    must_be_inside = [v for v in (same.get("max"), edited.get("max")) if v is not None]
    worst_inside = max(must_be_inside) if must_be_inside else None
    closest_unrelated = unrelated.get("min")

    separates = (
        worst_inside is not None
        and closest_unrelated is not None
        and worst_inside <= threshold < closest_unrelated
    )
    margin = (
        (closest_unrelated - threshold)
        if closest_unrelated is not None
        else None
    )

    notes: list[str] = []
    if separates:
        notes.append(
            f"the threshold of {threshold} still separates copies (worst "
            f"{worst_inside}) from unrelated documents (closest "
            f"{closest_unrelated})"
        )
    else:
        if worst_inside is not None and worst_inside > threshold:
            notes.append(
                f"a genuine copy sits at {worst_inside}, ABOVE the threshold "
                f"of {threshold}: resubmissions in that form are being missed"
            )
        if closest_unrelated is not None and closest_unrelated <= threshold:
            notes.append(
                f"an unrelated document sits at {closest_unrelated}, at or "
                f"BELOW the threshold of {threshold}: this is a false "
                "positive against an honest applicant"
            )
    if sibling.get("min") is not None:
        notes.append(
            f"template siblings span {sibling['min']}-{sibling['max']}, "
            f"{sibling['inside_threshold']} of {sibling['pairs']} inside the "
            "threshold - they remain indistinguishable from copies by "
            "distance alone, which is why the finding is weak by design"
        )

    return {
        "threshold": threshold,
        "separates_copies_from_unrelated": separates,
        "worst_copy_distance": worst_inside,
        "closest_unrelated_distance": closest_unrelated,
        "margin_to_unrelated": margin,
        "notes": notes,
        "action": "report only; this module never writes config",
    }


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        f"perceptual hash: {payload['hash_kind']}, hash_size="
        f"{payload['hash_size']} ({payload['hash_bits']} bits), "
        f"page {payload['page']} at {payload['render_dpi']} dpi",
        f"documents hashed: {payload['documents_hashed']}",
        f"threshold in config: {payload['threshold_in_config']}",
        "",
        f"{'pair type':<34} {'pairs':>6} {'min':>5} {'median':>7} {'max':>5} "
        f"{'<=thr':>6} {'>thr':>5}",
        "-" * 76,
    ]
    for pair_type, entry in payload["pair_types"].items():
        lines.append(
            f"{pair_type:<34} {entry['pairs']:>6} {entry['min']:>5} "
            f"{entry['median']:>7.1f} {entry['max']:>5} "
            f"{entry['inside_threshold']:>6} {entry['outside_threshold']:>5}"
        )

    verdict = payload["verdict"]
    lines += [
        "",
        f"separates copies from unrelated: {verdict['separates_copies_from_unrelated']}",
        f"  worst copy distance:      {verdict['worst_copy_distance']}",
        f"  closest unrelated:        {verdict['closest_unrelated_distance']}",
        f"  margin above threshold:   {verdict['margin_to_unrelated']}",
    ]
    for note in verdict["notes"]:
        lines.append(f"  - {note}")
    lines.append(f"  {verdict['action']}")

    if payload["render_failures"]:
        lines += ["", "render failures:"]
        for failure in payload["render_failures"]:
            lines.append(
                f"  app {failure['application_id']}: {failure['error']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.distances",
        description=(
            "Re-measure the perceptual-hash distance distributions across the "
            "generated layouts. Reports only; never writes config."
        ),
    )
    parser.add_argument("--out", default=None)
    parser.add_argument("--kind", default=None, help="dhash | phash | ahash")
    parser.add_argument("--size", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="emit JSON instead")
    args = parser.parse_args(argv)

    payload = measure(
        out_root=Path(args.out) if args.out else None,
        kind=args.kind,
        hash_size=args.size,
    )

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_report(payload))

    generation_config = load_config("eval_generation")
    root = (
        Path(args.out)
        if args.out
        else _repo_root() / str(get(generation_config, "output.root", "data/eval"))
    )
    target = root / str(get(generation_config, "output.results_dir", "results"))
    target.mkdir(parents=True, exist_ok=True)
    (target / "distances.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
