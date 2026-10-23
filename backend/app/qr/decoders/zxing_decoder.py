"""QR decoding with zxing-cpp, the fallback decoder.

Kept as a fallback rather than the primary only because OpenCV is already a
dependency. In practice zxing-cpp reads marginal images — rotated, low
contrast, partially blurred — that OpenCV's detector gives up on, so it earns
its place on exactly the documents that matter.
"""

from __future__ import annotations

from typing import Any

from .base import DecoderFailedError, DecoderUnavailableError, RawDetection


class ZXingDecoder:
    name = "zxing"

    def __init__(self) -> None:
        self._zxing = None

    def _load(self) -> Any:
        if self._zxing is None:
            try:
                import zxingcpp
            except ImportError as exc:
                raise DecoderUnavailableError(
                    "zxing-cpp is not installed. Install it with: "
                    "pip install zxing-cpp"
                ) from exc
            self._zxing = zxingcpp
        return self._zxing

    def version(self) -> str:
        try:
            from importlib.metadata import version

            return str(version("zxing-cpp"))
        except Exception:
            return "unknown"

    def decode(self, image: Any) -> list[RawDetection]:
        zxing = self._load()
        try:
            results = zxing.read_barcodes(image, formats=zxing.BarcodeFormat.QRCode)
        except Exception as exc:
            raise DecoderFailedError(f"zxing-cpp failed: {exc}") from exc

        found: list[RawDetection] = []
        for result in results or []:
            text = getattr(result, "text", "") or ""
            if not text:
                continue
            found.append(
                RawDetection(text=text, bounding_box=_bounding_box(result))
            )
        return found


def _bounding_box(result: Any) -> list[int] | None:
    position = getattr(result, "position", None)
    if position is None:
        return None
    corners = []
    for corner in ("top_left", "top_right", "bottom_right", "bottom_left"):
        point = getattr(position, corner, None)
        if point is None:
            return None
        corners.append((getattr(point, "x", None), getattr(point, "y", None)))
    if any(x is None or y is None for x, y in corners):
        return None
    xs = [float(x) for x, _ in corners]
    ys = [float(y) for _, y in corners]
    left, top = min(xs), min(ys)
    return [
        int(round(left)),
        int(round(top)),
        int(round(max(xs) - left)),
        int(round(max(ys) - top)),
    ]
