"""Metric computation, deliberately separate from the running.

Everything here is a pure function from (labels, results) to numbers, so it can
be tested on small hand-built sets with hand-computed answers. That matters
more than it sounds: a bug in the harness quietly moves a detection rate, and
nothing else in the system would notice.

Definitions, stated once so the report cannot drift from them:

- **flagged** - the run produced at least one finding at or above
  `scoring.flagged_min_strength` (medium by default). Weak findings are
  informational by design, so counting them would make precision a measure of
  how talkative the detectors are rather than how right they are.
- **detected** - every expected group for the case was satisfied. A group is
  satisfied when any of its listed finding types fired at or above the group's
  own `min_strength`, on this application or - for `either_side` groups - on
  its pair.
- **false positive** - a genuine document that was flagged, or a `must_not`
  type that fired at medium or above. The second is the stricter and more
  useful one: it names which finding was wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_STRENGTH_ORDER = ("weak", "medium", "strong")


def strength_rank(strength: str, order: tuple[str, ...] | list[str] = DEFAULT_STRENGTH_ORDER) -> int:
    """Position on the strength ladder. Unknown labels rank lowest.

    Lowest, not highest: an unrecognised strength must not be able to satisfy
    an expectation or trip a `must_not`. A label we do not understand is not
    evidence.
    """
    try:
        return list(order).index(str(strength))
    except ValueError:
        return -1


def meets(strength: str, minimum: str, order=DEFAULT_STRENGTH_ORDER) -> bool:
    minimum_rank = strength_rank(minimum, order)
    if minimum_rank < 0:
        return False
    return strength_rank(strength, order) >= minimum_rank


# --------------------------------------------------------------------------
# Per-application evaluation
# --------------------------------------------------------------------------


@dataclass
class ApplicationScore:
    """What one application's result says against its label."""

    application_id: int
    case_type: str
    split: str
    is_fraud: bool
    # Findings as (type, strength) pairs, from the database.
    findings: list[tuple[str, str]] = field(default_factory=list)
    flagged: bool = False
    detected: bool | None = None
    satisfied_groups: list[dict[str, Any]] = field(default_factory=list)
    unsatisfied_groups: list[dict[str, Any]] = field(default_factory=list)
    must_not_violations: list[dict[str, Any]] = field(default_factory=list)
    issuer_status: str | None = None
    expected_issuer_status: str | None = None
    issuer_status_ok: bool | None = None
    duration_ms: float = 0.0
    stage_durations: dict[str, float] = field(default_factory=dict)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False
    qr_detected: bool = False
    qr_expected: bool = False
    noise_level: str = "none"
    form: str = ""
    qr_sources: list[str] = field(default_factory=list)
    field_accuracy: dict[str, bool] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "case_type": self.case_type,
            "split": self.split,
            "is_fraud": self.is_fraud,
            "findings": [
                {"type": name, "strength": strength}
                for name, strength in self.findings
            ],
            "flagged": self.flagged,
            "detected": self.detected,
            "satisfied_groups": self.satisfied_groups,
            "unsatisfied_groups": self.unsatisfied_groups,
            "must_not_violations": self.must_not_violations,
            "issuer_status": self.issuer_status,
            "expected_issuer_status": self.expected_issuer_status,
            "issuer_status_ok": self.issuer_status_ok,
            "duration_ms": round(self.duration_ms, 2),
            "stage_durations": {
                key: round(value, 2) for key, value in self.stage_durations.items()
            },
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit": self.cache_hit,
            "qr_detected": self.qr_detected,
            "qr_expected": self.qr_expected,
            "qr_sources": self.qr_sources,
            "noise_level": self.noise_level,
            "form": self.form,
            "field_accuracy": self.field_accuracy,
            "error": self.error,
        }


def score_application(
    label: dict[str, Any],
    findings: list[tuple[str, str]],
    pair_findings: list[tuple[str, str]] | None = None,
    flagged_min_strength: str = "medium",
    strength_order=DEFAULT_STRENGTH_ORDER,
) -> ApplicationScore:
    """Compare one application's findings with its label.

    `pair_findings` are the other half of a duplicate pair's findings, used
    only by groups marked `either_side` - because which run discovers a
    relationship depends on processing order, and the measurement must not.
    """
    score = ApplicationScore(
        application_id=int(label["application_id"]),
        case_type=str(label["case_type"]),
        split=str(label.get("split", "dev")),
        is_fraud=bool(label.get("is_fraud")),
        findings=list(findings),
    )

    score.flagged = any(
        meets(strength, flagged_min_strength, strength_order)
        for _, strength in findings
    )

    groups = label.get("expected") or []
    for group in groups:
        pool = list(findings)
        if group.get("either_side"):
            pool += list(pair_findings or [])
        minimum = str(group.get("min_strength", "strong"))
        hit = [
            {"type": name, "strength": strength}
            for name, strength in pool
            if name in set(group.get("any_of") or [])
            and meets(strength, minimum, strength_order)
        ]
        entry = {
            "any_of": list(group.get("any_of") or []),
            "min_strength": minimum,
            "either_side": bool(group.get("either_side")),
            "fired": hit,
            "note": group.get("note", ""),
        }
        if hit:
            score.satisfied_groups.append(entry)
        else:
            score.unsatisfied_groups.append(entry)

    if groups:
        score.detected = not score.unsatisfied_groups
    else:
        # No expectation to satisfy. `detected` stays None rather than True:
        # a clean document is not "detected", and counting it as such would
        # inflate the detection rate with cases that expect nothing.
        score.detected = None

    forbidden = set(label.get("must_not") or [])
    for name, strength in findings:
        if name in forbidden and meets(strength, "medium", strength_order):
            score.must_not_violations.append({"type": name, "strength": strength})

    score.expected_issuer_status = label.get("expected_issuer_status")
    return score


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or None when there is nothing to divide by.

    None rather than 0.0 on an empty denominator. Zero would read as "nothing
    was detected" when the truth is "there was nothing to detect", and those
    print identically in a table unless the difference is kept.
    """
    if denominator <= 0:
        return None
    return numerator / denominator


def per_case_type(scores: list[ApplicationScore]) -> list[dict[str, Any]]:
    """Cases, detected, detection rate and which expected finding fired."""
    by_type: dict[str, list[ApplicationScore]] = {}
    for score in scores:
        by_type.setdefault(score.case_type, []).append(score)

    rows: list[dict[str, Any]] = []
    for case_type in sorted(by_type):
        group = by_type[case_type]
        scorable = [s for s in group if s.detected is not None]
        detected = [s for s in scorable if s.detected]
        fired: dict[str, int] = {}
        for score in detected:
            for entry in score.satisfied_groups:
                for hit in entry["fired"]:
                    fired[hit["type"]] = fired.get(hit["type"], 0) + 1
        rows.append(
            {
                "case_type": case_type,
                "is_fraud": group[0].is_fraud,
                "cases": len(group),
                "scorable": len(scorable),
                "detected": len(detected),
                "detection_rate": _rate(len(detected), len(scorable)),
                "which_fired": dict(sorted(fired.items())),
                "flagged": sum(1 for s in group if s.flagged),
                "must_not_violations": sum(
                    len(s.must_not_violations) for s in group
                ),
            }
        )
    return rows


def hard_negative_rates(scores: list[ApplicationScore]) -> list[dict[str, Any]]:
    """False positives per hard-negative category, at strong and at medium+."""
    rows: list[dict[str, Any]] = []
    by_type: dict[str, list[ApplicationScore]] = {}
    for score in scores:
        if score.case_type.startswith("hard_negative_"):
            by_type.setdefault(score.case_type, []).append(score)

    for case_type in sorted(by_type):
        group = by_type[case_type]
        at_strong = sum(
            1
            for s in group
            if any(strength == "strong" for _, strength in s.findings)
        )
        at_medium = sum(1 for s in group if s.flagged)
        rows.append(
            {
                "case_type": case_type,
                "cases": len(group),
                "false_positive_at_strong": at_strong,
                "false_positive_rate_at_strong": _rate(at_strong, len(group)),
                "false_positive_at_medium_or_above": at_medium,
                "false_positive_rate_at_medium_or_above": _rate(
                    at_medium, len(group)
                ),
                "must_not_violations": [
                    {
                        "application_id": s.application_id,
                        "violations": s.must_not_violations,
                    }
                    for s in group
                    if s.must_not_violations
                ],
            }
        )
    return rows


def document_level(scores: list[ApplicationScore]) -> dict[str, Any]:
    """Precision, recall, F1 and the clean-document false-positive rate."""
    true_positive = sum(1 for s in scores if s.is_fraud and s.flagged)
    false_negative = sum(1 for s in scores if s.is_fraud and not s.flagged)
    false_positive = sum(1 for s in scores if not s.is_fraud and s.flagged)
    true_negative = sum(1 for s in scores if not s.is_fraud and not s.flagged)

    precision = _rate(true_positive, true_positive + false_positive)
    recall = _rate(true_positive, true_positive + false_negative)
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)

    clean = [s for s in scores if s.case_type == "clean"]
    clean_flagged = sum(1 for s in clean if s.flagged)

    # The headline precision is the specified one: flagged versus is_fraud.
    # But it charges as a false positive every genuine document the system was
    # RIGHT to surface - a certificate the issuer revoked, suspended or let
    # expire is honest and still needs a reviewer. Without this breakdown the
    # number understates precision and, worse, invites someone to "fix" the
    # issuer-state findings to improve it.
    expected_flag = [
        s
        for s in scores
        if not s.is_fraud and s.flagged and s.satisfied_groups
    ]
    unexpected_flag = [
        s
        for s in scores
        if not s.is_fraud and s.flagged and not s.satisfied_groups
    ]
    true_positive_or_expected = true_positive + len(expected_flag)
    precision_counting_expected = _rate(
        true_positive_or_expected, true_positive_or_expected + len(unexpected_flag)
    )

    return {
        "genuine_flagged_as_expected": len(expected_flag),
        "genuine_flagged_as_expected_cases": sorted(
            {s.case_type for s in expected_flag}
        ),
        "genuine_flagged_unexpectedly": len(unexpected_flag),
        "genuine_flagged_unexpectedly_detail": [
            {
                "application_id": s.application_id,
                "case_type": s.case_type,
                "findings": [
                    {"type": name, "strength": strength}
                    for name, strength in s.findings
                ],
            }
            for s in unexpected_flag
        ],
        "precision_counting_expected_flags_as_correct": precision_counting_expected,
        "true_positive": true_positive,
        "false_negative": false_negative,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "clean_documents": len(clean),
        "clean_flagged": clean_flagged,
        "clean_false_positive_rate": _rate(clean_flagged, len(clean)),
        "genuine_documents": true_negative + false_positive,
        "genuine_false_positive_rate": _rate(
            false_positive, true_negative + false_positive
        ),
    }


def issuer_status_accuracy(scores: list[ApplicationScore]) -> dict[str, Any]:
    checked = [s for s in scores if s.expected_issuer_status]
    correct = [s for s in checked if s.issuer_status_ok]
    mismatches = [
        {
            "application_id": s.application_id,
            "case_type": s.case_type,
            "expected": s.expected_issuer_status,
            "actual": s.issuer_status,
        }
        for s in checked
        if not s.issuer_status_ok
    ]
    return {
        "checked": len(checked),
        "correct": len(correct),
        "accuracy": _rate(len(correct), len(checked)),
        "mismatches": mismatches,
    }


def qr_decode_rates(scores: list[ApplicationScore]) -> dict[str, Any]:
    """Decode rate by noise level and by image source.

    Broken out by noise level because an aggregate hides the thing worth
    knowing: whether failures cluster where the input is worst, or are spread
    evenly, which would point at something other than the input.
    """
    expected = [s for s in scores if s.qr_expected]

    by_noise: dict[str, dict[str, Any]] = {}
    for score in expected:
        entry = by_noise.setdefault(
            score.noise_level, {"expected": 0, "decoded": 0}
        )
        entry["expected"] += 1
        if score.qr_detected:
            entry["decoded"] += 1
    for entry in by_noise.values():
        entry["rate"] = _rate(entry["decoded"], entry["expected"])

    by_form: dict[str, dict[str, Any]] = {}
    for score in expected:
        entry = by_form.setdefault(score.form, {"expected": 0, "decoded": 0})
        entry["expected"] += 1
        if score.qr_detected:
            entry["decoded"] += 1
    for entry in by_form.values():
        entry["rate"] = _rate(entry["decoded"], entry["expected"])

    by_source: dict[str, int] = {}
    for score in expected:
        for source in score.qr_sources:
            by_source[source] = by_source.get(source, 0) + 1

    decoded = sum(1 for s in expected if s.qr_detected)
    return {
        "expected": len(expected),
        "decoded": decoded,
        "rate": _rate(decoded, len(expected)),
        "by_noise_level": dict(sorted(by_noise.items())),
        "by_form": dict(sorted(by_form.items())),
        "by_source": dict(sorted(by_source.items())),
        "failures": [
            {
                "application_id": s.application_id,
                "case_type": s.case_type,
                "noise_level": s.noise_level,
                "form": s.form,
            }
            for s in expected
            if not s.qr_detected
        ],
    }


def field_accuracy(scores: list[ApplicationScore]) -> dict[str, Any]:
    """Per-field extraction accuracy. Live mode only; empty otherwise."""
    totals: dict[str, dict[str, int]] = {}
    for score in scores:
        for name, correct in (score.field_accuracy or {}).items():
            entry = totals.setdefault(name, {"checked": 0, "correct": 0})
            entry["checked"] += 1
            if correct:
                entry["correct"] += 1
    return {
        name: {
            "checked": entry["checked"],
            "correct": entry["correct"],
            "accuracy": _rate(entry["correct"], entry["checked"]),
        }
        for name, entry in sorted(totals.items())
    }


def timing(scores: list[ApplicationScore], percentile: int = 95) -> dict[str, Any]:
    durations = sorted(s.duration_ms for s in scores)
    stage_totals: dict[str, list[float]] = {}
    for score in scores:
        for stage, value in score.stage_durations.items():
            stage_totals.setdefault(stage, []).append(value)

    return {
        "applications": len(durations),
        "mean_ms": (sum(durations) / len(durations)) if durations else None,
        f"p{percentile}_ms": _percentile(durations, percentile),
        "min_ms": durations[0] if durations else None,
        "max_ms": durations[-1] if durations else None,
        "per_stage_mean_ms": {
            stage: sum(values) / len(values)
            for stage, values in sorted(stage_totals.items())
            if values
        },
    }


def _percentile(sorted_values: list[float], percentile: int) -> float | None:
    """Nearest-rank percentile on an already-sorted list.

    Nearest-rank rather than interpolated: with sixty applications an
    interpolated p95 invents a duration that no application actually took, and
    a latency figure should be a thing that happened.
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(1, int(round(percentile / 100 * len(sorted_values))))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def cost(scores: list[ApplicationScore]) -> dict[str, Any]:
    """LLM cost, separating tokens actually spent from tokens replayed.

    A cache hit reports the token counts of the call that originally filled it,
    which is right for "what did this extraction cost" and wrong for "what did
    this run cost". Against a free tier's daily cap only the second question
    matters, so `*_billed` excludes cache hits.
    """
    billed = [s for s in scores if not s.cache_hit]
    return {
        "llm_calls": sum(s.llm_calls for s in scores),
        "cache_hits": sum(1 for s in scores if s.cache_hit),
        "input_tokens": sum(s.input_tokens for s in scores),
        "output_tokens": sum(s.output_tokens for s in scores),
        "total_tokens": sum(s.input_tokens + s.output_tokens for s in scores),
        "input_tokens_billed": sum(s.input_tokens for s in billed),
        "output_tokens_billed": sum(s.output_tokens for s in billed),
        "total_tokens_billed": sum(
            s.input_tokens + s.output_tokens for s in billed
        ),
    }


def misses(scores: list[ApplicationScore]) -> list[dict[str, Any]]:
    """Every fraud case not fully detected, with what did fire instead."""
    rows: list[dict[str, Any]] = []
    for score in scores:
        if score.detected is False or (score.is_fraud and not score.flagged):
            rows.append(
                {
                    "application_id": score.application_id,
                    "case_type": score.case_type,
                    "split": score.split,
                    "flagged": score.flagged,
                    "expected_but_missing": score.unsatisfied_groups,
                    "findings_that_did_fire": [
                        {"type": name, "strength": strength}
                        for name, strength in score.findings
                    ],
                    "issuer_status": score.issuer_status,
                    "error": score.error,
                }
            )
    return rows


def false_positives(scores: list[ApplicationScore]) -> list[dict[str, Any]]:
    """Every genuine document flagged, or `must_not` type that fired."""
    rows: list[dict[str, Any]] = []
    for score in scores:
        if score.is_fraud:
            continue
        if not score.flagged and not score.must_not_violations:
            continue
        rows.append(
            {
                "application_id": score.application_id,
                "case_type": score.case_type,
                "split": score.split,
                "must_not_violations": score.must_not_violations,
                "findings_that_did_fire": [
                    {"type": name, "strength": strength}
                    for name, strength in score.findings
                ],
                "issuer_status": score.issuer_status,
            }
        )
    return rows


def summarise(
    scores: list[ApplicationScore],
    percentile: int = 95,
    include_field_accuracy: bool = False,
) -> dict[str, Any]:
    """One split's worth of numbers."""
    payload: dict[str, Any] = {
        "applications": len(scores),
        "fraud_cases": sum(1 for s in scores if s.is_fraud),
        "genuine_cases": sum(1 for s in scores if not s.is_fraud),
        "per_case_type": per_case_type(scores),
        "hard_negatives": hard_negative_rates(scores),
        "document_level": document_level(scores),
        "issuer_status": issuer_status_accuracy(scores),
        "qr_decode": qr_decode_rates(scores),
        "timing": timing(scores, percentile),
        "cost": cost(scores),
        "misses": misses(scores),
        "false_positives": false_positives(scores),
    }
    if include_field_accuracy:
        payload["field_accuracy"] = field_accuracy(scores)
    return payload
