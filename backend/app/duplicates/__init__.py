"""Cross-application duplicate detection (step 6, part B).

Reverse image search is internal only: comparison is against the corpus of
documents already in our own database. No external image service is contacted.
"""

from .detector import (
    cross_references_for_document,
    detect_duplicates,
    strength_for,
)
from .models import (
    DuplicateCheckResult,
    DuplicateStatus,
    Finding,
    FindingType,
    Match,
)

__all__ = [
    "cross_references_for_document",
    "detect_duplicates",
    "strength_for",
    "DuplicateCheckResult",
    "DuplicateStatus",
    "Finding",
    "FindingType",
    "Match",
]
