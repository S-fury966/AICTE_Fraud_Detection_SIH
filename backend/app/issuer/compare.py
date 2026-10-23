"""Field-by-field comparison of the submitted document against the issuer record.

One rule governs the whole module: **a null submitted field is never a
mismatch.** Step 2 returns null both when a field is genuinely absent from the
certificate and when its value failed the snippet guard. Neither is a conflict
with the issuer, so both produce `not_compared` with the reason recorded. A
detector that treated missing evidence as adverse evidence would flag every
poorly scanned document.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..core.config import get
from .models import FieldComparison, FieldOutcome
from .normalise import grades_match, names_match, programmes_match

# Submitted field -> the key holding the same thing in an issuer record.
_ISSUER_KEYS = {
    "institution_name": "institution_name",
    "issuing_body": "issuing_body",
    "programmes": "programmes",
    "grade": "grade",
    "valid_from": "valid_from",
    "valid_to": "valid_to",
}

_NAME_FIELDS = ("institution_name", "issuing_body")
_DATE_FIELDS = ("valid_from", "valid_to")


def compare_record(
    submitted: dict[str, Any], record: dict[str, Any], cfg: dict[str, Any]
) -> list[FieldComparison]:
    """Compare every configured field. Order follows config, for a stable table."""
    results: list[FieldComparison] = []
    for name in get(cfg, "comparison.fields", []) or []:
        field_name = str(name)
        results.append(_compare_one(field_name, submitted, record, cfg))
    return results


def _compare_one(
    name: str,
    submitted: dict[str, Any],
    record: dict[str, Any],
    cfg: dict[str, Any],
) -> FieldComparison:
    issuer_key = _ISSUER_KEYS.get(name, name)
    submitted_value = submitted.get(name)
    issuer_value = record.get(issuer_key)

    if submitted_value is None or submitted_value == "" or submitted_value == []:
        return FieldComparison(
            name=name,
            outcome=FieldOutcome.NOT_COMPARED,
            submitted_value=submitted_value,
            issuer_value=issuer_value,
            reason=(
                "the submitted value is null: either absent from the document "
                "or rejected by the step 2 source check, so there is nothing "
                "to compare"
            ),
        )

    if issuer_value is None or issuer_value == "" or issuer_value == []:
        return FieldComparison(
            name=name,
            outcome=FieldOutcome.NOT_COMPARED,
            submitted_value=submitted_value,
            issuer_value=issuer_value,
            reason="the issuer record does not carry this field",
        )

    if name == "programmes":
        matched, score, method, detail = programmes_match(
            submitted_value, issuer_value, cfg
        )
        return FieldComparison(
            name=name,
            outcome=FieldOutcome.MATCH if matched else FieldOutcome.MISMATCH,
            submitted_value=detail["submitted_set"],
            issuer_value=detail["issuer_set"],
            reason=_programme_reason(matched, method, detail),
            similarity=score if matched else None,
            method=method,
        )

    if name in _NAME_FIELDS:
        matched, ratio, method = names_match(submitted_value, issuer_value, cfg)
        return FieldComparison(
            name=name,
            outcome=FieldOutcome.MATCH if matched else FieldOutcome.MISMATCH,
            submitted_value=submitted_value,
            issuer_value=issuer_value,
            reason=(
                "names agree after normalisation"
                if matched
                else "the submitted name and the issuer's name are not the same name"
            ),
            similarity=round(ratio, 4),
            method=method,
        )

    if name in _DATE_FIELDS:
        return _compare_date(name, submitted_value, issuer_value)

    if name == "grade":
        matched, method = grades_match(submitted_value, issuer_value, cfg)
        return FieldComparison(
            name=name,
            outcome=FieldOutcome.MATCH if matched else FieldOutcome.MISMATCH,
            submitted_value=submitted_value,
            issuer_value=issuer_value,
            reason=(
                "grades are identical"
                if matched
                else "the grade on the document is not the grade the issuer holds"
            ),
            method=method,
        )

    matched = str(submitted_value).strip() == str(issuer_value).strip()
    return FieldComparison(
        name=name,
        outcome=FieldOutcome.MATCH if matched else FieldOutcome.MISMATCH,
        submitted_value=submitted_value,
        issuer_value=issuer_value,
        method="exact",
    )


def _programme_reason(matched: bool, method: str, detail: dict[str, Any]) -> str:
    if matched and method == "subset_match":
        return (
            "every programme on the document appears in the issuer record, "
            "which lists additional accredited programmes"
        )
    if matched:
        return "the programme sets agree"
    if method == "programme_not_in_issuer_record":
        missing = ", ".join(detail.get("unmatched_submitted", []))
        return (
            f"the issuer record does not list: {missing}"
            if missing
            else "the programme sets do not agree"
        )
    if method == "issuer_has_additional_programmes":
        return "the issuer record lists programmes the document does not"
    return "the programme sets do not agree"


def _compare_date(name: str, submitted: Any, issuer: Any) -> FieldComparison:
    """Dates compare as ISO dates, so formatting differences are not mismatches."""
    left = _as_iso(submitted)
    right = _as_iso(issuer)

    if left is None or right is None:
        unreadable = "submitted" if left is None else "issuer"
        return FieldComparison(
            name=name,
            outcome=FieldOutcome.NOT_COMPARED,
            submitted_value=submitted,
            issuer_value=issuer,
            reason=f"the {unreadable} value is not a readable ISO date",
            method="iso_date",
        )

    matched = left == right
    return FieldComparison(
        name=name,
        outcome=FieldOutcome.MATCH if matched else FieldOutcome.MISMATCH,
        submitted_value=left,
        issuer_value=right,
        reason=(
            "the dates are the same day"
            if matched
            else "mismatch detected between the document date and the issuer's date"
        ),
        method="iso_date",
    )


def _as_iso(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def mismatched_fields(comparisons: list[FieldComparison]) -> list[FieldComparison]:
    return [c for c in comparisons if c.outcome is FieldOutcome.MISMATCH]
