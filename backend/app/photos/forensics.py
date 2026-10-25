"""Pixel-level checks: error level analysis and copy-move detection.

Both are *hints*. ELA lights up on high-contrast edges and text as readily as
on a pasted patch, and copy-move fires on genuinely repetitive scenes - rows of
identical monitors, tiled floors. That is why neither is ever strong, why each
reports its measurements rather than a verdict, and why ELA saves a heatmap:
the reviewer judges the picture, not the number.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np


def error_level_analysis(
    image: Any, quality: int = 90, block_size: int = 16, hot_factor: float = 9.0
) -> dict[str, Any]:
    """Re-save at a known JPEG quality and measure where the image changed most.

    Returns block statistics plus the per-pixel difference image (as a numpy
    array, for the heatmap). The ratio compares the most-changed blocks with
    the median block: a uniformly compressed photo has a low ratio, a region
    compressed differently from its surroundings raises it.
    """
    from PIL import Image

    rgb = image.convert("RGB")
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=int(quality))
    buffer.seek(0)
    resaved = Image.open(buffer).convert("RGB")

    original = np.asarray(rgb, dtype=np.int16)
    again = np.asarray(resaved, dtype=np.int16)
    diff = np.abs(original - again).max(axis=2).astype(np.float32)

    height, width = diff.shape
    rows, cols = height // block_size, width // block_size
    if rows == 0 or cols == 0:
        return {"computed": False, "reason": "image smaller than one ELA block"}

    trimmed = diff[: rows * block_size, : cols * block_size]
    blocks = trimmed.reshape(rows, block_size, cols, block_size).mean(axis=(1, 3))
    median = float(np.median(blocks))
    p995 = float(np.percentile(blocks, 99.5))
    floor = max(median, 0.5)
    ratio = p995 / floor
    hot = blocks > (floor * float(hot_factor))
    hot_count = int(hot.sum())

    region = None
    if hot_count:
        ys, xs = np.nonzero(hot)
        region = [
            int(xs.min() * block_size),
            int(ys.min() * block_size),
            int((xs.max() - xs.min() + 1) * block_size),
            int((ys.max() - ys.min() + 1) * block_size),
        ]

    return {
        "computed": True,
        "jpeg_quality": int(quality),
        "block_size": int(block_size),
        "median_block_error": round(median, 3),
        "p99_5_block_error": round(p995, 3),
        "max_block_error": round(float(blocks.max()), 3),
        "ratio": round(ratio, 3),
        "hot_blocks": hot_count,
        "hot_region_xywh": region,
        "_diff": diff,
    }


def ela_heatmap(diff: np.ndarray) -> Any:
    """The ELA difference as a viewable image: amplified, colour-mapped."""
    import cv2
    from PIL import Image

    scale = 255.0 / max(float(np.percentile(diff, 99.9)), 1.0)
    amplified = np.clip(diff * scale, 0, 255).astype(np.uint8)
    coloured = cv2.applyColorMap(amplified, cv2.COLORMAP_INFERNO)
    return Image.fromarray(cv2.cvtColor(coloured, cv2.COLOR_BGR2RGB))


def copy_move(
    image: Any,
    max_features: int = 3000,
    ratio_test: float = 0.75,
    min_offset_px: float = 40,
    displacement_bin_px: int = 6,
    min_cluster_matches: int = 14,
    max_dimension: int = 1600,
) -> dict[str, Any]:
    """Look for one region of the image repeated elsewhere in the same image.

    ORB keypoints are matched against the image's own keypoints (skipping each
    point's match to itself). A cloned region produces many matches sharing
    one displacement vector; ordinary texture produces scattered ones.
    """
    import cv2

    rgb = np.asarray(image.convert("L"))
    scale = 1.0
    longest = max(rgb.shape)
    if longest > max_dimension:
        scale = max_dimension / float(longest)
        rgb = cv2.resize(rgb, (int(rgb.shape[1] * scale), int(rgb.shape[0] * scale)))

    orb = cv2.ORB_create(nfeatures=int(max_features))
    keypoints, descriptors = orb.detectAndCompute(rgb, None)
    if descriptors is None or len(keypoints) < 10:
        return {"computed": True, "keypoints": len(keypoints or []), "largest_cluster": 0, "suspicious": False}

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    knn = matcher.knnMatch(descriptors, descriptors, k=3)

    vectors: dict[tuple[int, int], list[tuple[tuple[float, float], tuple[float, float]]]] = {}
    for group in knn:
        # group[0] is the point matched to itself.
        candidates = [m for m in group if m.queryIdx != m.trainIdx]
        if len(candidates) < 2:
            if len(candidates) == 1 and candidates[0].distance < 20:
                best = candidates[0]
            else:
                continue
        else:
            best, second = candidates[0], candidates[1]
            if best.distance > ratio_test * second.distance:
                continue
        p1 = keypoints[best.queryIdx].pt
        p2 = keypoints[best.trainIdx].pt
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        if (dx * dx + dy * dy) ** 0.5 < min_offset_px:
            continue
        # One direction only, so A->B and B->A fall in one bin.
        if dx < 0 or (dx == 0 and dy < 0):
            dx, dy, p1, p2 = -dx, -dy, p2, p1
        key = (int(round(dx / displacement_bin_px)), int(round(dy / displacement_bin_px)))
        vectors.setdefault(key, []).append((p1, p2))

    if not vectors:
        return {"computed": True, "keypoints": len(keypoints), "largest_cluster": 0, "suspicious": False}

    key, pairs = max(vectors.items(), key=lambda item: len(item[1]))
    largest = len(pairs)
    suspicious = largest >= int(min_cluster_matches)

    def box(points: list[tuple[float, float]]) -> list[int]:
        xs = [p[0] / scale for p in points]
        ys = [p[1] / scale for p in points]
        return [int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))]

    return {
        "computed": True,
        "keypoints": len(keypoints),
        "largest_cluster": largest,
        "displacement_px": [round(key[0] * displacement_bin_px / scale), round(key[1] * displacement_bin_px / scale)],
        "source_region_xywh": box([p[0] for p in pairs]) if suspicious else None,
        "target_region_xywh": box([p[1] for p in pairs]) if suspicious else None,
        "suspicious": suspicious,
    }


def spectral_flatness(image: Any) -> float:
    """High-frequency energy share of the luminance spectrum. Experimental.

    Kept for completeness and OFF in config: it has not been validated on real
    photographs, and a heuristic nobody has measured is not evidence.
    """
    gray = np.asarray(image.convert("L").resize((256, 256)), dtype=np.float32)
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(gray)))
    total = float(spectrum.sum()) or 1.0
    centre = spectrum[96:160, 96:160].sum()
    return round(1.0 - float(centre) / total, 4)
