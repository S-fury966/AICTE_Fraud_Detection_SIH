"""Photo authenticity for lab and classroom photographs (detector 3)."""

from .checker import (
    DETECTOR,
    SYNTHETIC_WORDING,
    LoadedPhoto,
    PhotoLoadError,
    check_claim,
    check_manipulation,
    check_metadata,
    check_synthetic,
    load_photo,
)
from .reuse import check_reuse

__all__ = [
    "DETECTOR",
    "SYNTHETIC_WORDING",
    "LoadedPhoto",
    "PhotoLoadError",
    "check_claim",
    "check_manipulation",
    "check_metadata",
    "check_reuse",
    "check_synthetic",
    "load_photo",
]
