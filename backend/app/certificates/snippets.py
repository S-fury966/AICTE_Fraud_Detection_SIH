"""The hallucination guard.

A value is only kept if the snippet the model says it came from is actually in
the page text. This is the single most important check in the module: it is
what stops a plausible-sounding invention from entering the pipeline as
evidence.

Matching is tolerant of whitespace and case (configurable) and of small
differences, because OCR text is noisy and a model copying from it will not
always reproduce it byte for byte. The tolerance is a config value, so it can
be tightened when the inputs are clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from ..core.config import get

_WHITESPACE = re.compile(r"\s+")


@dataclass
class SnippetMatch:
    """Where a snippet was found, and how well."""

    found: bool
    similarity: float
    # "exact" after normalisation, "fuzzy" above the configured threshold,
    # "absent" otherwise, "skipped" when the snippet was too short to mean
    # anything, "no_snippet" when the model supplied none.
    method: str
    page: int | None = None


def normalise(text: str, cfg: dict[str, Any]) -> str:
    """Apply the configured tolerances to a piece of text."""
    result = text
    if bool(get(cfg, "snippet_validation.collapse_whitespace", True)):
        result = _WHITESPACE.sub(" ", result).strip()
    if bool(get(cfg, "snippet_validation.ignore_case", True)):
        result = result.casefold()
    return result


def find_snippet(
    snippet: str | None,
    pages: dict[int, str],
    claimed_page: int | None,
    cfg: dict[str, Any],
) -> SnippetMatch:
    """Look for `snippet` in the page text.

    The page the model claimed is checked first. If the snippet is not there
    but is present on another page, the match reports that page instead, so a
    misattributed page number does not cost us a correct value. Page
    correction is configurable.
    """
    if snippet is None or not snippet.strip():
        return SnippetMatch(found=False, similarity=0.0, method="no_snippet")

    min_chars = int(get(cfg, "snippet_validation.min_snippet_chars", 8))
    if len(snippet.strip()) < min_chars:
        # Too short to be evidence of anything: a three-character snippet
        # appears in almost any page of text.
        return SnippetMatch(found=False, similarity=0.0, method="skipped")

    needle = normalise(snippet, cfg)
    if not needle:
        return SnippetMatch(found=False, similarity=0.0, method="no_snippet")

    order: list[int] = []
    if claimed_page is not None and claimed_page in pages:
        order.append(claimed_page)
    if bool(get(cfg, "snippet_validation.allow_page_correction", True)):
        order.extend(page for page in sorted(pages) if page not in order)
    elif not order:
        # No page correction and no valid claimed page: nothing to search.
        return SnippetMatch(found=False, similarity=0.0, method="absent")

    threshold = float(get(cfg, "snippet_validation.min_similarity", 0.92))
    best = SnippetMatch(found=False, similarity=0.0, method="absent")

    for page_number in order:
        haystack = normalise(pages[page_number], cfg)
        if not haystack:
            continue

        if needle in haystack:
            return SnippetMatch(
                found=True, similarity=1.0, method="exact", page=page_number
            )

        similarity = _best_window_similarity(needle, haystack)
        if similarity > best.similarity:
            best = SnippetMatch(
                found=similarity >= threshold,
                similarity=round(similarity, 4),
                method="fuzzy" if similarity >= threshold else "absent",
                page=page_number if similarity >= threshold else None,
            )
        if best.found:
            return best

    return best


def _best_window_similarity(needle: str, haystack: str) -> float:
    """Highest similarity between `needle` and any same-length window of text.

    Windows are stepped rather than taken at every offset: a snippet shifted
    by a few characters still scores nearly the same, and the step keeps this
    linear enough for page-sized text.
    """
    if not needle or not haystack:
        return 0.0
    if len(needle) >= len(haystack):
        return SequenceMatcher(None, needle, haystack).ratio()

    window = len(needle)
    step = max(1, window // 8)
    best = 0.0
    for start in range(0, len(haystack) - window + 1, step):
        candidate = haystack[start : start + window]
        # quick_ratio is an upper bound and much cheaper; only pay for the
        # real comparison when a window could beat the best so far.
        matcher = SequenceMatcher(None, needle, candidate)
        if matcher.quick_ratio() <= best:
            continue
        best = max(best, matcher.ratio())
        if best >= 1.0:
            break
    return best
