"""Perceptual hashing, shared between detectors.

Document duplicate detection uses it now; the photo detector will use it for
reused lab and classroom images later.
"""

from .hashing import (
    AHASH,
    DHASH,
    PHASH,
    SUPPORTED_KINDS,
    PerceptualHash,
    UnsupportedHashKind,
    compute,
    compute_all,
    hamming_distance,
)

__all__ = [
    "AHASH",
    "DHASH",
    "PHASH",
    "SUPPORTED_KINDS",
    "PerceptualHash",
    "UnsupportedHashKind",
    "compute",
    "compute_all",
    "hamming_distance",
]
