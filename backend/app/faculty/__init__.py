"""Faculty count consistency (detector 2): declared vs website vs AICTE records."""

from .checker import DETECTOR, FacultyCheckResult, allowed_deviation, check_faculty
from .sources import normalise_person_name

__all__ = [
    "DETECTOR",
    "FacultyCheckResult",
    "allowed_deviation",
    "check_faculty",
    "normalise_person_name",
]
