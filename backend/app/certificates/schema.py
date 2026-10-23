"""The contract the model's response must satisfy.

Validation here is purely structural: are the right keys present, with values
of the right type. Whether a value is *true* is decided elsewhere.
"""

from __future__ import annotations

import json
from typing import Any

# Order matters only for readable output.
FIELD_NAMES: tuple[str, ...] = (
    "institution_name",
    "certificate_number",
    "issuing_body",
    "programme",
    "accreditation_status_or_grade",
    "issue_date",
    "valid_from",
    "valid_to",
    "signatory_name",
)

# Fields whose value is a list of strings rather than a single string.
LIST_FIELDS: frozenset[str] = frozenset({"programme"})

# Fields our code parses into ISO dates.
DATE_FIELDS: tuple[str, ...] = ("issue_date", "valid_from", "valid_to")

CONFIDENCE_VALUES: frozenset[str] = frozenset({"low", "medium", "high"})

_ENTRY_KEYS: tuple[str, ...] = ("value", "source_snippet", "page", "confidence")


class SchemaError(ValueError):
    """The response does not match the contract."""


def parse_response(raw: str) -> dict[str, Any]:
    """Parse the model's raw text into JSON.

    Tolerates a markdown code fence and surrounding whitespace, because that
    is the one deviation models make most often and recovering from it
    deterministically is cheaper than a retry. Anything else is an error.
    """
    text = raw.strip()
    if not text:
        raise SchemaError("response was empty")

    if text.startswith("```"):
        lines = text.splitlines()
        # Drop the opening fence (with or without a language tag) and, if
        # present, the closing one.
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"response is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SchemaError(
            f"response must be a JSON object, got {type(data).__name__}"
        )
    return data


def validate_payload(data: dict[str, Any]) -> list[str]:
    """Return a list of schema problems. Empty means the payload is usable."""
    problems: list[str] = []

    missing = [name for name in FIELD_NAMES if name not in data]
    if missing:
        problems.append(f"missing field keys: {', '.join(missing)}")

    unexpected = [key for key in data if key not in FIELD_NAMES]
    if unexpected:
        problems.append(f"unexpected keys: {', '.join(sorted(unexpected))}")

    for name in FIELD_NAMES:
        if name not in data:
            continue
        problems.extend(_validate_entry(name, data[name]))

    return problems


def _validate_entry(name: str, entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{name}: must be an object, got {type(entry).__name__}"]

    problems: list[str] = []
    for key in _ENTRY_KEYS:
        if key not in entry:
            problems.append(f"{name}: missing key {key!r}")

    value = entry.get("value")
    if name in LIST_FIELDS:
        if value is not None:
            if not isinstance(value, list):
                problems.append(
                    f"{name}: value must be a list or null, got {type(value).__name__}"
                )
            elif not all(isinstance(item, str) for item in value):
                problems.append(f"{name}: value must be a list of strings")
    elif value is not None and not isinstance(value, str):
        problems.append(
            f"{name}: value must be a string or null, got {type(value).__name__}"
        )

    snippet = entry.get("source_snippet")
    if snippet is not None and not isinstance(snippet, str):
        problems.append(
            f"{name}: source_snippet must be a string or null, got "
            f"{type(snippet).__name__}"
        )

    page = entry.get("page")
    # bool is an int subclass; reject it explicitly.
    if page is not None and (isinstance(page, bool) or not isinstance(page, int)):
        problems.append(
            f"{name}: page must be an integer or null, got {type(page).__name__}"
        )

    confidence = entry.get("confidence")
    if confidence is not None and confidence not in CONFIDENCE_VALUES:
        problems.append(
            f"{name}: confidence must be one of low, medium, high or null, "
            f"got {confidence!r}"
        )

    return problems
