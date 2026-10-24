"""Scan degradation, at configured levels.

Every level is recorded per document in the labels, so the QR decode rate can
be reported *by noise level*. An aggregate decode rate hides the thing worth
knowing: whether the failures cluster in one bucket, which tells you where the
decoder gives up, or are spread evenly, which tells you something else is
wrong.

The rule for every level is that the page stays legible to a person. A
degradation so heavy that a human could not read the number either would
measure nothing about the detector - the honest answer in that case is "this
document needs rescanning", not "the detector failed".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class NoiseSettings:
    """One degradation level, exactly as recorded in the labels."""

    level: str
    rotation_degrees: float
    blur_radius: float
    jpeg_quality: int
    brightness: float
    contrast: float
    noise_sigma: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "rotation_degrees": self.rotation_degrees,
            "blur_radius": self.blur_radius,
            "jpeg_quality": self.jpeg_quality,
            "brightness": self.brightness,
            "contrast": self.contrast,
            "noise_sigma": self.noise_sigma,
        }


def settings_for(level: str, config: dict[str, Any]) -> NoiseSettings:
    levels = ((config.get("noise") or {}).get("levels") or {})
    raw = levels.get(level) or {}
    return NoiseSettings(
        level=level,
        rotation_degrees=float(raw.get("rotation_degrees", 0.0)),
        blur_radius=float(raw.get("blur_radius", 0.0)),
        jpeg_quality=int(raw.get("jpeg_quality", 95)),
        brightness=float(raw.get("brightness", 1.0)),
        contrast=float(raw.get("contrast", 1.0)),
        noise_sigma=float(raw.get("noise_sigma", 0.0)),
    )


def apply(image: Any, noise: NoiseSettings, seed: int) -> Any:
    """Degrade a page image.

    Order matters and mirrors a real scan: the page is skewed on the glass
    first, the optics blur it, the sensor adds noise and shifts the exposure,
    and only then is it compressed. Doing it in any other order (compressing
    before blurring, say) produces artefacts no scanner makes.

    `seed` drives the pixel noise, so the same document degrades identically
    on every run.
    """
    import numpy as np
    from PIL import Image, ImageEnhance, ImageFilter

    result = image.convert("RGB")

    if noise.rotation_degrees:
        result = result.rotate(
            noise.rotation_degrees,
            resample=Image.BICUBIC,
            fillcolor=(255, 255, 255),
        )

    if noise.blur_radius:
        result = result.filter(ImageFilter.GaussianBlur(radius=noise.blur_radius))

    if noise.brightness != 1.0:
        result = ImageEnhance.Brightness(result).enhance(noise.brightness)
    if noise.contrast != 1.0:
        result = ImageEnhance.Contrast(result).enhance(noise.contrast)

    if noise.noise_sigma:
        generator = np.random.default_rng(seed)
        array = np.asarray(result, dtype=np.float32)
        array += generator.normal(0.0, noise.noise_sigma, array.shape)
        result = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))

    return result


def to_jpeg(image: Any, quality: int) -> bytes:
    import io

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=int(quality))
    return buffer.getvalue()
