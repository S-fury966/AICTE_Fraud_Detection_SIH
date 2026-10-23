"""Deterministic normalisation and similarity for the field comparison.

Institution names reach us through OCR and a language model, so they arrive
with inconsistent punctuation, abbreviations and spacing. The job here is to
absorb exactly that and nothing more: "Konkan Coast Inst. of Engg." and
"Konkan Coast Institute of Engineering" are the same college, while two
genuinely different colleges must stay different.

Grades and dates are *not* normalised this way. A grade is an identifier where
one character changes the meaning, and a date is compared as an ISO date.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from ..core.config import get

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalise_name(value: str, cfg: dict[str, Any]) -> str:
    """Case, punctuation, abbreviations and filler words, in that order.

    Order matters: punctuation has to go before the abbreviation lookup, or
    "inst." never matches the key "inst".
    """
    text = str(value or "")
    if bool(get(cfg, "comparison.names.ignore_case", True)):
        text = text.casefold()
    if bool(get(cfg, "comparison.names.strip_punctuation", True)):
        text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return text

    abbreviations = get(cfg, "comparison.names.abbreviations", {}) or {}
    lookup = {
        str(key).casefold(): str(value) for key, value in abbreviations.items()
    }
    drop = {
        str(word).casefold() for word in (get(cfg, "comparison.names.drop_words", []) or [])
    }

    words: list[str] = []
    for word in text.split(" "):
        expanded = lookup.get(word, word)
        # Expansion can itself introduce a dropped word, so the drop test
        # comes after.
        for part in expanded.split(" "):
            if part and part not in drop:
                words.append(part)
    return " ".join(words)


def similarity(left: str, right: str) -> float:
    """Ratio between two already-normalised strings."""
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def names_match(
    submitted: str, issuer: str, cfg: dict[str, Any]
) -> tuple[bool, float, str]:
    """Compare two names. Returns (matched, similarity, method)."""
    left = normalise_name(submitted, cfg)
    right = normalise_name(issuer, cfg)
    if left == right:
        return True, 1.0, "exact_after_normalisation"

    threshold = float(get(cfg, "comparison.names.min_similarity", 0.90))
    ratio = similarity(left, right)
    if ratio >= threshold:
        return True, ratio, "similarity_above_threshold"
    return False, ratio, "similarity_below_threshold"


def programme_set(values: Any, cfg: dict[str, Any]) -> set[str]:
    """Canonicalise a programme value into a set of normalised entries.

    A single string, a list of strings, or a comma-joined line all reduce to
    the same set. The comma is the reason: a certificate printing
    "Bachelor of Technology, Computer Science and Engineering" is one
    programme on paper, but an extraction may legitimately read it as two
    items, and a registry may hold either form. Splitting both sides on commas
    makes the comparison answer the question actually being asked — are these
    the same programmes — instead of how the text happened to be punctuated.
    """
    if values is None:
        return set()
    items = values if isinstance(values, (list, tuple, set)) else [values]

    split = bool(get(cfg, "comparison.programmes.split_on_commas", True))
    ignore_case = bool(get(cfg, "comparison.programmes.ignore_case", True))

    result: set[str] = set()
    for item in items:
        text = str(item or "")
        pieces = text.split(",") if split else [text]
        for piece in pieces:
            cleaned = _WHITESPACE.sub(" ", piece).strip()
            if not cleaned:
                continue
            result.add(cleaned.casefold() if ignore_case else cleaned)
    return result


def programmes_match(
    submitted: Any, issuer: Any, cfg: dict[str, Any]
) -> tuple[bool, float, str, dict[str, Any]]:
    """Compare programmes as sets. Returns (matched, score, method, detail)."""
    left = programme_set(submitted, cfg)
    right = programme_set(issuer, cfg)

    detail: dict[str, Any] = {
        "submitted_set": sorted(left),
        "issuer_set": sorted(right),
    }

    if not left:
        return False, 0.0, "submitted_empty", detail
    if left == right:
        return True, 1.0, "exact_set_match", detail

    threshold = float(get(cfg, "comparison.programmes.min_similarity", 0.90))
    allow_subset = bool(get(cfg, "comparison.programmes.allow_subset", True))

    # Each submitted programme has to find a home in the issuer's set.
    unmatched: list[str] = []
    for entry in sorted(left):
        best = max((similarity(entry, other) for other in right), default=0.0)
        if best < threshold:
            unmatched.append(entry)
    detail["unmatched_submitted"] = unmatched

    if unmatched:
        return False, 0.0, "programme_not_in_issuer_record", detail

    extra = sorted(right - left)
    detail["issuer_only"] = extra
    if extra and not allow_subset:
        return False, 0.0, "issuer_has_additional_programmes", detail

    method = "subset_match" if extra else "fuzzy_set_match"
    return True, 1.0, method, detail


def grades_match(submitted: str, issuer: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    """Grades are identifiers: A+ and A++ are different outcomes."""
    left = str(submitted or "")
    right = str(issuer or "")
    if bool(get(cfg, "comparison.grade.ignore_case", True)):
        left, right = left.casefold(), right.casefold()
    if bool(get(cfg, "comparison.grade.strip_punctuation", False)):
        left = _PUNCTUATION.sub("", left)
        right = _PUNCTUATION.sub("", right)
    left, right = left.strip(), right.strip()
    return left == right, "exact"
