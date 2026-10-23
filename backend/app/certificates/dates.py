"""Date parsing.

The model returns dates exactly as printed. Turning them into ISO dates is
code's job, because the result has to be predictable and reviewable: a date is
either parsed by a rule we can point at, or it is left alone and flagged.

Day-first is the default, matching Indian convention: 03/04/2025 is 3 April.
Where a numeric date is ambiguous the assumption is recorded, so a reviewer
can see that 03/04/2025 was read as 3 April and not 4 March.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..core.config import get

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# "1st", "22nd", "3rd", "4th"
_ORDINAL = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s,.]+")

# A space is accepted as a separator because normalisation above turns the
# dots of "12.03.2025" into spaces.
_NUMERIC = re.compile(r"^(\d{1,4})[/\-. ](\d{1,2})[/\-. ](\d{1,4})$")
# "12 March 2025"
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{2,4})$")
# "March 12 2025"
_MONTH_DAY_YEAR = re.compile(r"^([A-Za-z]+)\s+(\d{1,2})\s+(\d{2,4})$")
# "March 2025"
_MONTH_YEAR = re.compile(r"^([A-Za-z]+)\s+(\d{4})$")


@dataclass
class ParsedDate:
    """The outcome of parsing one printed date."""

    raw: str
    iso: str | None
    parsed: bool
    # Short machine-readable note, e.g. "ambiguous_day_month_assumed_day_first"
    # or "unrecognised_format". Carried into the output for explainability.
    note: str | None = None
    ambiguous: bool = False


def parse_date(raw: str | None, cfg: dict[str, Any] | None = None) -> ParsedDate:
    """Parse a printed date into an ISO date string.

    Returns a ParsedDate that always keeps the raw text. `iso` is None when no
    rule matched; the caller keeps the raw value and flags it rather than
    discarding it.
    """
    cfg = cfg or {}
    if raw is None:
        return ParsedDate(raw="", iso=None, parsed=False, note="no_value")

    text = _ORDINAL.sub(r"\1", raw.strip())
    text = _SEPARATORS.sub(" ", text).strip()
    # Put separators back for the numeric pattern, which ran on the original.
    compact = re.sub(r"\s*([/\-])\s*", r"\1", text)

    if not compact:
        return ParsedDate(raw=raw, iso=None, parsed=False, note="no_value")

    day_first = bool(get(cfg, "dates.day_first", True))

    for parser in (_parse_numeric, _parse_day_month_year, _parse_month_day_year):
        result = parser(compact, day_first, cfg)
        if result is not None:
            return ParsedDate(raw=raw, **result)

    # A month and year with no day is real on certificates, but it is not a
    # date. Reported as unparsed rather than invented as the 1st.
    if _MONTH_YEAR.match(compact):
        return ParsedDate(
            raw=raw, iso=None, parsed=False, note="month_and_year_only_no_day"
        )

    return ParsedDate(raw=raw, iso=None, parsed=False, note="unrecognised_format")


def _parse_numeric(
    text: str, day_first: bool, cfg: dict[str, Any]
) -> dict[str, Any] | None:
    match = _NUMERIC.match(text)
    if not match:
        return None

    first, second, third = (int(part) for part in match.groups())

    # A four-digit leading number is a year: ISO-style YYYY-MM-DD.
    if len(match.group(1)) == 4:
        year, month, day = first, second, third
        ambiguous = False
        note = None
    else:
        year = third
        if day_first:
            day, month = first, second
        else:
            month, day = first, second
        # Both candidates are valid months, so the order cannot be verified
        # from the value itself.
        ambiguous = first <= 12 and second <= 12 and first != second
        note = (
            "ambiguous_day_month_assumed_day_first"
            if ambiguous and day_first
            else "ambiguous_day_month_assumed_month_first"
            if ambiguous
            else None
        )

    year = _expand_two_digit_year(year)
    built = _build(year, month, day, cfg)
    if built is None:
        return {"iso": None, "parsed": False, "note": "out_of_range", "ambiguous": False}
    return {"iso": built, "parsed": True, "note": note, "ambiguous": ambiguous}


def _parse_day_month_year(
    text: str, day_first: bool, cfg: dict[str, Any]
) -> dict[str, Any] | None:
    match = _DAY_MONTH_YEAR.match(text)
    if not match:
        return None
    month = _MONTHS.get(match.group(2).casefold())
    if month is None:
        return None
    day = int(match.group(1))
    year = _expand_two_digit_year(int(match.group(3)))
    built = _build(year, month, day, cfg)
    if built is None:
        return {"iso": None, "parsed": False, "note": "out_of_range", "ambiguous": False}
    # A named month cannot be confused with a day.
    return {"iso": built, "parsed": True, "note": None, "ambiguous": False}


def _parse_month_day_year(
    text: str, day_first: bool, cfg: dict[str, Any]
) -> dict[str, Any] | None:
    match = _MONTH_DAY_YEAR.match(text)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).casefold())
    if month is None:
        return None
    day = int(match.group(2))
    year = _expand_two_digit_year(int(match.group(3)))
    built = _build(year, month, day, cfg)
    if built is None:
        return {"iso": None, "parsed": False, "note": "out_of_range", "ambiguous": False}
    return {"iso": built, "parsed": True, "note": None, "ambiguous": False}


def _expand_two_digit_year(year: int) -> int:
    """Expand a two-digit year, assuming the recent past or near future."""
    if year >= 100:
        return year
    return 2000 + year if year <= 70 else 1900 + year


def _build(year: int, month: int, day: int, cfg: dict[str, Any]) -> str | None:
    min_year = int(get(cfg, "dates.min_year", 1900))
    max_year = int(get(cfg, "dates.max_year", 2100))
    if not min_year <= year <= max_year:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        # 31 February and friends.
        return None
