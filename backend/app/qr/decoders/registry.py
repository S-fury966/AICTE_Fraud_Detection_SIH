"""Decoder lookup by the name given in config."""

from __future__ import annotations

from typing import Any, Callable

from .base import DecoderUnavailableError, QRDecoder
from .opencv_decoder import OpenCVDecoder
from .zxing_decoder import ZXingDecoder

_DECODERS: dict[str, Callable[[], QRDecoder]] = {
    "opencv": OpenCVDecoder,
    "zxing": ZXingDecoder,
}


def available_decoders() -> list[str]:
    return sorted(_DECODERS)


def get_decoder(name: str) -> QRDecoder:
    try:
        factory = _DECODERS[name]
    except KeyError as exc:
        raise DecoderUnavailableError(
            f"unknown QR decoder {name!r}; available: "
            f"{', '.join(available_decoders())}"
        ) from exc
    return factory()


def build_decoders(names: Any) -> list[QRDecoder]:
    """Build the decoders named in config, in order, skipping missing ones.

    A decoder whose library is absent is left out rather than fatal: one
    working decoder is enough to scan a document, and the audit record says
    which ones ran.
    """
    built: list[QRDecoder] = []
    for name in names or []:
        try:
            decoder = get_decoder(str(name))
            decoder.version()
        except DecoderUnavailableError:
            continue
        built.append(decoder)
    return built
