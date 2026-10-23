"""RapidOCR engine.

Chosen for Windows because it installs from PyPI with bundled ONNX models and
needs no system-level binary or PATH entry, unlike Tesseract.
"""

from __future__ import annotations

import io
from typing import Any

from .base import OCRFailedError, OCRResult, OCRUnavailableError


class RapidOCREngine:
    name = "rapidocr"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = settings or {}
        self._reader: Any | None = None

    def _load(self) -> Any:
        """Import and construct the reader on first use.

        Deferred so that importing this module (and the whole extraction
        package) stays cheap and does not fail when RapidOCR is absent.
        """
        if self._reader is not None:
            return self._reader
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            try:
                from rapidocr import RapidOCR  # newer package name
            except ImportError as exc:
                raise OCRUnavailableError(
                    "RapidOCR is not installed. Install it with: "
                    "pip install rapidocr-onnxruntime"
                ) from exc
        try:
            self._reader = RapidOCR()
        except Exception as exc:  # model download/load problems
            raise OCRUnavailableError(f"could not initialise RapidOCR: {exc}") from exc
        return self._reader

    def recognize(self, image_png: bytes) -> OCRResult:
        reader = self._load()
        min_score = float(self._settings.get("text_score", 0.5))

        try:
            import numpy as np
            from PIL import Image

            image = Image.open(io.BytesIO(image_png)).convert("RGB")
            result = reader(np.asarray(image))
        except OCRUnavailableError:
            raise
        except Exception as exc:
            raise OCRFailedError(f"RapidOCR failed on this image: {exc}") from exc

        lines = _normalise_result(result)
        kept = [(text, score) for text, score in lines if score >= min_score]

        if not kept:
            return OCRResult(text="", confidence=0.0, line_count=0, engine=self.name)

        text = "\n".join(text for text, _ in kept)
        confidence = sum(score for _, score in kept) / len(kept)
        return OCRResult(
            text=text,
            confidence=round(confidence, 4),
            line_count=len(kept),
            engine=self.name,
        )


def _normalise_result(result: Any) -> list[tuple[str, float]]:
    """Flatten RapidOCR's output into (text, score) pairs.

    Versions differ: older releases return a `(lines, elapse)` tuple where each
    line is `[box, text, score]`; newer ones return an object exposing `.txts`
    and `.scores`.
    """
    txts = getattr(result, "txts", None)
    if txts is not None:
        scores = getattr(result, "scores", None) or []
        return [
            (str(t), float(s))
            for t, s in zip(txts, list(scores) + [1.0] * len(txts))
        ]

    lines = result[0] if isinstance(result, tuple) else result
    if not lines:
        return []

    pairs: list[tuple[str, float]] = []
    for line in lines:
        if isinstance(line, (list, tuple)) and len(line) >= 3:
            pairs.append((str(line[1]), float(line[2])))
    return pairs
