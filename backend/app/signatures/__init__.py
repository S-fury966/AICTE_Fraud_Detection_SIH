"""PDF signature integrity (step 6, part A).

Scope is integrity only: did this file change after it was signed? Signer
identity is never verified - no certificate chain, no trust store.
"""

from .checker import check_signatures, strength_for
from .models import (
    CoverageLevel,
    Finding,
    FindingType,
    SignatureCheckResult,
    SignatureDetail,
    SignatureError,
    SignatureStatus,
)

__all__ = [
    "check_signatures",
    "strength_for",
    "CoverageLevel",
    "Finding",
    "FindingType",
    "SignatureCheckResult",
    "SignatureDetail",
    "SignatureError",
    "SignatureStatus",
]
