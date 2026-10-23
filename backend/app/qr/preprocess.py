"""The preprocessing ladder.

Run only when a plain decode of an image finds nothing. Each rung is a cheap,
lossless-in-intent transform that helps a decoder that is nearly there:
a scan too small for the finder patterns, a washed-out photocopy, a page fed
in sideways. The rung that succeeded is recorded on the detection, because a
QR that needed three transforms to read is itself worth knowing about.

Order matters and lives in config: the cheap rungs come first so a clean
document costs nothing extra.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np


def _cv2() -> Any:
    import cv2

    return cv2


def _grayscale(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return _cv2().cvtColor(image, _cv2().COLOR_RGB2GRAY)


def _upscale_2x(image: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    # Cubic rather than nearest: the decoders key off edge gradients, and
    # nearest-neighbour upscaling keeps the original hard pixel steps without
    # adding anything for them to work with.
    return cv2.resize(image, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)


def _contrast_boost(image: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    gray = _grayscale(image)
    # CLAHE rather than a global stretch, because the usual failure is a
    # gradient across a photographed page: one corner blown out, the opposite
    # corner in shadow. A global stretch cannot fix both at once.
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _grayscale_upscale_2x(image: np.ndarray) -> np.ndarray:
    return _upscale_2x(_grayscale(image))


def _rotate(times: int) -> Callable[[np.ndarray], np.ndarray]:
    def rung(image: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(np.rot90(image, k=times))

    return rung


# A QR code is readable at any rotation by design, so the rotation rungs are
# not here for upside-down codes. They help when a decoder's scan pattern
# happens to miss a code at one orientation and catch it at another.
LADDER: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "grayscale": _grayscale,
    "upscale_2x": _upscale_2x,
    "contrast_boost": _contrast_boost,
    "grayscale_upscale_2x": _grayscale_upscale_2x,
    "rotate_90": _rotate(1),
    "rotate_180": _rotate(2),
    "rotate_270": _rotate(3),
}


def available_rungs() -> list[str]:
    return sorted(LADDER)


def apply_rung(name: str, image: np.ndarray) -> np.ndarray | None:
    """Run one rung. Returns None when the name is unknown or it fails.

    A rung that cannot run is skipped rather than fatal: it is an attempt at
    recovery, and failing to recover is not an error.
    """
    rung = LADDER.get(name)
    if rung is None:
        return None
    try:
        return rung(image)
    except Exception:
        return None
