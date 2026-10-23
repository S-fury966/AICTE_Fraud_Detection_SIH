"""The OCR engine seam.

An engine is anything that turns one page image into text plus a confidence.
Keeping it to a single method means the engine can be swapped (RapidOCR,
Tesseract, a cloud OCR) without touching the extraction pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class OCRUnavailableError(RuntimeError):
    """The engine's dependencies are not installed or models are missing."""


class OCRFailedError(RuntimeError):
    """The engine was available but failed on this particular image."""


@dataclass
class OCRResult:
    """Text recognised from one image."""

    text: str
    # Mean of the per-line scores, 0.0-1.0. None when the engine reports no
    # confidence at all; 0.0 when nothing was recognised.
    confidence: float | None
    line_count: int
    engine: str


@runtime_checkable
class OCREngine(Protocol):
    """Image in, text and confidence out."""

    name: str

    def recognize(self, image_png: bytes) -> OCRResult:
        """Recognise text in a PNG-encoded image.

        Raises OCRUnavailableError if the engine cannot run at all, and
        OCRFailedError if this image could not be processed.
        """
        ...
