"""Turning results into tables a person can read.

Rates print as percentages with one decimal, and a rate with nothing to divide
by prints as `n/a` rather than `0.0%`. Those are different statements - "none
of them were detected" and "there were none to detect" - and a table that
conflated them would be actively misleading.
"""

from __future__ import annotations

from typing import Any


def pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def ms(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.0f}ms"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |",
        "|" + "|".join("-" * (widths[i] + 2) for i in range(len(headers))) + "|",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |"
        )
    return "\n".join(lines)


def _case_type_table(summary: dict[str, Any]) -> str:
    rows = []
    for entry in summary.get("per_case_type", []):
        rows.append(
            [
                entry["case_type"],
                "fraud" if entry["is_fraud"] else "genuine",
                str(entry["cases"]),
                str(entry["detected"]) if entry["scorable"] else "-",
                pct(entry["detection_rate"]) if entry["scorable"] else "n/a",
                str(entry["flagged"]),
                ", ".join(f"{k}x{v}" for k, v in entry["which_fired"].items()) or "-",
            ]
        )
    return _table(
        [
            "case type",
            "kind",
            "cases",
            "detected",
            "rate",
            "flagged",
            "which expected finding fired",
        ],
        rows,
    )


def _hard_negative_table(summary: dict[str, Any]) -> str:
    rows = []
    for entry in summary.get("hard_negatives", []):
        rows.append(
            [
                entry["case_type"],
                str(entry["cases"]),
                f"{entry['false_positive_at_strong']} "
                f"({pct(entry['false_positive_rate_at_strong'])})",
                f"{entry['false_positive_at_medium_or_above']} "
                f"({pct(entry['false_positive_rate_at_medium_or_above'])})",
                ", ".join(
                    f"app {v['application_id']}:"
                    + "/".join(x["type"] for x in v["violations"])
                    for v in entry["must_not_violations"]
                )
                or "-",
            ]
        )
    return _table(
        ["hard negative", "cases", "FP at strong", "FP at medium+", "must_not violations"],
        rows,
    )


def _document_table(summary: dict[str, Any]) -> str:
    doc = summary.get("document_level", {})
    rows = [
        ["true positive", str(doc.get("true_positive"))],
        ["false negative", str(doc.get("false_negative"))],
        ["false positive", str(doc.get("false_positive"))],
        ["true negative", str(doc.get("true_negative"))],
        ["precision", pct(doc.get("precision"))],
        ["recall", pct(doc.get("recall"))],
        ["F1", pct(doc.get("f1"))],
        [
            "false positive rate, clean documents",
            f"{doc.get('clean_flagged')}/{doc.get('clean_documents')} "
            f"({pct(doc.get('clean_false_positive_rate'))})",
        ],
        [
            "false positive rate, all genuine",
            f"{doc.get('false_positive')}/{doc.get('genuine_documents')} "
            f"({pct(doc.get('genuine_false_positive_rate'))})",
        ],
        [
            "  of those, flagged as EXPECTED",
            f"{doc.get('genuine_flagged_as_expected')} "
            f"({', '.join(doc.get('genuine_flagged_as_expected_cases') or []) or '-'})",
        ],
        [
            "  of those, flagged UNEXPECTEDLY",
            str(doc.get("genuine_flagged_unexpectedly")),
        ],
        [
            "precision, counting expected flags as correct",
            pct(doc.get("precision_counting_expected_flags_as_correct")),
        ],
    ]
    return _table(["measure", "value"], rows)


def _qr_table(summary: dict[str, Any]) -> str:
    qr = summary.get("qr_decode", {})
    rows = [
        [
            f"noise: {level}",
            str(entry["expected"]),
            str(entry["decoded"]),
            pct(entry["rate"]),
        ]
        for level, entry in (qr.get("by_noise_level") or {}).items()
    ]
    rows += [
        [
            f"form: {form}",
            str(entry["expected"]),
            str(entry["decoded"]),
            pct(entry["rate"]),
        ]
        for form, entry in (qr.get("by_form") or {}).items()
    ]
    rows.append(
        [
            "overall",
            str(qr.get("expected")),
            str(qr.get("decoded")),
            pct(qr.get("rate")),
        ]
    )
    return _table(["bucket", "expected", "decoded", "rate"], rows)


def _timing_table(summary: dict[str, Any]) -> str:
    timing = summary.get("timing", {})
    rows = [
        ["applications", str(timing.get("applications"))],
        ["mean", ms(timing.get("mean_ms"))],
    ]
    for key, value in timing.items():
        # The percentile key is named e.g. "p95_ms". Guard on the digits, not
        # just the prefix: "per_stage_mean_ms" also starts with "p" and ends
        # with "_ms", and it holds a dict.
        if key.startswith("p") and key.endswith("_ms") and key[1:-3].isdigit():
            rows.append([key.replace("_ms", ""), ms(value)])
    rows += [["min", ms(timing.get("min_ms"))], ["max", ms(timing.get("max_ms"))]]
    for stage, value in (timing.get("per_stage_mean_ms") or {}).items():
        rows.append([f"stage mean: {stage}", ms(value)])
    return _table(["measure", "value"], rows)


def _split_section(name: str, summary: dict[str, Any], mode: str) -> str:
    parts = [f"## {name}", ""]
    parts.append(
        f"{summary.get('applications')} applications "
        f"({summary.get('fraud_cases')} fraud, "
        f"{summary.get('genuine_cases')} genuine)"
    )
    parts += ["", "### Per case type", "", _case_type_table(summary)]
    if summary.get("hard_negatives"):
        parts += ["", "### Hard negatives", "", _hard_negative_table(summary)]
    parts += ["", "### Document level (flagged at medium or above)", ""]
    parts.append(_document_table(summary))

    issuer = summary.get("issuer_status", {})
    parts += [
        "",
        "### Issuer status accuracy",
        "",
        f"{issuer.get('correct')}/{issuer.get('checked')} correct "
        f"({pct(issuer.get('accuracy'))})",
    ]
    if issuer.get("mismatches"):
        parts.append("")
        parts.append(
            _table(
                ["app", "case type", "expected", "actual"],
                [
                    [
                        str(entry["application_id"]),
                        entry["case_type"],
                        str(entry["expected"]),
                        str(entry["actual"]),
                    ]
                    for entry in issuer["mismatches"]
                ],
            )
        )

    parts += ["", "### QR decode rate", "", _qr_table(summary)]

    if summary.get("field_accuracy"):
        parts += ["", "### Extraction field accuracy (live mode)", ""]
        parts.append(
            _table(
                ["field", "checked", "correct", "accuracy"],
                [
                    [
                        name,
                        str(entry["checked"]),
                        str(entry["correct"]),
                        pct(entry["accuracy"]),
                    ]
                    for name, entry in summary["field_accuracy"].items()
                ],
            )
        )
    elif mode == "oracle":
        parts += [
            "",
            "### Extraction field accuracy",
            "",
            "Not reported in oracle mode: the model's answers are the ground "
            "truth by construction, so any accuracy figure would be 100% and "
            "would mean nothing.",
        ]

    parts += ["", "### Processing time", "", _timing_table(summary)]

    cost = summary.get("cost", {})
    parts += [
        "",
        "### LLM cost",
        "",
        f"calls {cost.get('llm_calls')}, cache hits {cost.get('cache_hits')}",
        "",
        f"- tokens recorded: {cost.get('input_tokens')} in / "
        f"{cost.get('output_tokens')} out / {cost.get('total_tokens')} total",
        f"- tokens actually billed this run (cache hits excluded): "
        f"**{cost.get('total_tokens_billed')}**",
    ]

    if summary.get("misses"):
        parts += ["", "### Misses", ""]
        for entry in summary["misses"]:
            expected = "; ".join(
                "/".join(group["any_of"]) + f" at {group['min_strength']}"
                for group in entry["expected_but_missing"]
            )
            fired = (
                ", ".join(
                    f"{f['type']}({f['strength']})"
                    for f in entry["findings_that_did_fire"]
                )
                or "nothing"
            )
            parts.append(
                f"- **app {entry['application_id']}** "
                f"({entry['case_type']}, {entry['split']}): expected "
                f"{expected or 'to be flagged'}; fired {fired}; issuer status "
                f"{entry['issuer_status']}"
            )
    else:
        parts += ["", "### Misses", "", "None."]

    if summary.get("false_positives"):
        parts += ["", "### False positives", ""]
        for entry in summary["false_positives"]:
            violations = (
                ", ".join(
                    f"{v['type']}({v['strength']})"
                    for v in entry["must_not_violations"]
                )
                or "-"
            )
            fired = (
                ", ".join(
                    f"{f['type']}({f['strength']})"
                    for f in entry["findings_that_did_fire"]
                )
                or "nothing"
            )
            parts.append(
                f"- **app {entry['application_id']}** "
                f"({entry['case_type']}, {entry['split']}): must_not "
                f"violations {violations}; fired {fired}"
            )
    else:
        parts += ["", "### False positives", "", "None."]

    return "\n".join(parts)


def summary_markdown(payload: dict[str, Any]) -> str:
    mode = str(payload.get("mode", "oracle"))
    lines = [
        "# Evaluation summary",
        "",
        f"- mode: **{mode}**",
        f"- split selected: {payload.get('split_selected')}",
        f"- applications run: {payload.get('applications_run')}",
        f"- generation seed: {payload.get('generation_seed')}",
        f"- generation config: {payload.get('generation_config_version')}",
        f"- run config: {payload.get('config_version')}",
        f"- issuer registry: {payload.get('registry')} "
        f"({payload.get('registry_records')} records)",
        "",
        "> **Tune on dev only. Read holdout once, at the end.** Changing a "
        "threshold after looking at holdout turns the holdout number into "
        "another dev number. Nothing in the code can enforce this.",
        "",
    ]

    if mode == "oracle":
        lines += [
            "> Oracle mode: the stub provider returns exactly the fields the "
            "generator printed, with snippets grounded in the real extracted "
            "page text. The deterministic detectors are therefore measured "
            "without model noise. The snippet guard still applies, so fields "
            "can still be lost to unreadable input - that is part of the "
            "system under test, not part of the model.",
            "",
        ]

    if payload.get("order_independence"):
        comparison = payload["order_independence"]
        lines += [
            "## Order independence",
            "",
            f"- identical relationship set: **{comparison['identical']}**",
            f"- only in normal order: {comparison['only_in_normal_order'] or 'none'}",
            f"- only in shuffled order: {comparison['only_in_shuffled_order'] or 'none'}",
            "",
        ]

    for name, key in (("Dev", "dev"), ("Holdout", "holdout"), ("All", "all")):
        if payload.get(key, {}).get("applications"):
            lines.append(_split_section(name, payload[key], mode))
            lines.append("")

    return "\n".join(lines)


def console_table(payload: dict[str, Any]) -> str:
    """The short table printed at the end of a run."""
    rows = []
    for name in ("dev", "holdout", "all"):
        summary = payload.get(name) or {}
        if not summary.get("applications"):
            continue
        doc = summary.get("document_level", {})
        issuer = summary.get("issuer_status", {})
        timing = summary.get("timing", {})
        rows.append(
            [
                name,
                str(summary.get("applications")),
                str(summary.get("fraud_cases")),
                pct(doc.get("recall")),
                pct(doc.get("precision")),
                pct(doc.get("f1")),
                pct(doc.get("clean_false_positive_rate")),
                pct(issuer.get("accuracy")),
                ms(timing.get("mean_ms")),
            ]
        )
    return _table(
        [
            "split",
            "apps",
            "fraud",
            "recall",
            "precision",
            "F1",
            "clean FP",
            "issuer acc",
            "mean time",
        ],
        rows,
    )
