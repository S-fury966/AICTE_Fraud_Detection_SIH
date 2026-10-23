"""Engine lookup by the name given in config."""

from __future__ import annotations

from typing import Any, Callable

from .base import OCREngine, OCRUnavailableError
from .rapidocr_engine import RapidOCREngine

_ENGINES: dict[str, Callable[[dict[str, Any]], OCREngine]] = {
    "rapidocr": RapidOCREngine,
}


def available_engines() -> list[str]:
    return sorted(_ENGINES)


def get_engine(name: str, settings: dict[str, Any] | None = None) -> OCREngine:
    """Build the engine registered under `name`."""
    try:
        factory = _ENGINES[name]
    except KeyError as exc:
        raise OCRUnavailableError(
            f"unknown OCR engine {name!r}; available: {', '.join(available_engines())}"
        ) from exc
    return factory(settings or {})
