"""QR decoding with OpenCV's built-in detector.

The primary decoder: it ships in the same wheel as the rest of OpenCV, needs
no native DLL on the PATH, and reports corner points we can turn into a
bounding box for the evidence link. OpenCV is already a dependency through
step 1's OCR engine, so this adds nothing to install.
"""

from __future__ import annotations

from typing import Any

from .base import DecoderFailedError, DecoderUnavailableError, RawDetection


class OpenCVDecoder:
    name = "opencv"

    def __init__(self) -> None:
        self._cv2 = None
        self._detector = None

    def _load(self) -> Any:
        if self._cv2 is None:
            try:
                import cv2
            except ImportError as exc:
                raise DecoderUnavailableError(
                    "opencv is not installed. Install it with: "
                    "pip install opencv-python"
                ) from exc
            self._cv2 = cv2
            self._detector = cv2.QRCodeDetector()
        return self._cv2

    def version(self) -> str:
        return str(self._load().__version__)

    def decode(self, image: Any) -> list[RawDetection]:
        self._load()
        try:
            ok, texts, points, _ = self._detector.detectAndDecodeMulti(image)
        except Exception as exc:
            # A detector failure on one image is not a failure of the run: the
            # ladder has other variants to try.
            raise DecoderFailedError(f"opencv detector failed: {exc}") from exc

        if not ok or texts is None:
            return []

        found: list[RawDetection] = []
        for index, text in enumerate(texts):
            # detectAndDecodeMulti returns an entry per detected region,
            # with an empty string where the region would not decode. Those
            # are counted as unreadable regions elsewhere, not as detections.
            if not text:
                continue
            box = None
            if points is not None and index < len(points):
                box = _bounding_box(points[index])
            found.append(RawDetection(text=text, bounding_box=box))
        return found

    def count_regions(self, image: Any) -> int:
        """How many QR-like regions are present, decodable or not.

        Used to tell "no QR on this document" apart from "a QR we could not
        read", which are different findings for a reviewer.
        """
        self._load()
        try:
            ok, points = self._detector.detectMulti(image)
        except Exception:
            return 0
        if not ok or points is None:
            return 0
        return len(points)


def _bounding_box(corners: Any) -> list[int] | None:
    """Turn four corner points into an axis-aligned [x, y, w, h]."""
    try:
        xs = [float(point[0]) for point in corners]
        ys = [float(point[1]) for point in corners]
    except (TypeError, IndexError, ValueError):
        return None
    if not xs or not ys:
        return None
    left, top = min(xs), min(ys)
    return [
        int(round(left)),
        int(round(top)),
        int(round(max(xs) - left)),
        int(round(max(ys) - top)),
    ]
