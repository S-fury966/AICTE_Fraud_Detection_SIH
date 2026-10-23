"""The QR decoder seam.

A decoder takes an image array and returns what it found. Nothing more: it
does not parse payloads, judge them, or know what a certificate is. Same shape
as the OCR engine seam in `app/extraction/ocr`, and for the same reason —
swapping the library should not touch the checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class DecoderUnavailableError(RuntimeError):
    """The decoder cannot run at all: library missing or failed to load."""


class DecoderFailedError(RuntimeError):
    """This image could not be processed. Another image may still work."""


@dataclass
class RawDetection:
    """One decode, as the library reported it."""

    text: str
    # [x, y, width, height] in pixels of the image passed in, or None when the
    # decoder reports no position.
    bounding_box: list[int] | None = None


@runtime_checkable
class QRDecoder(Protocol):
    name: str

    def version(self) -> str:
        """The underlying library version, for the audit record."""
        ...

    def decode(self, image: Any) -> list[RawDetection]:
        """Find every QR code in a numpy image array.

        An image with no QR code is an empty list, not an error. Raises
        DecoderFailedError only when the image itself could not be processed.
        """
        ...
