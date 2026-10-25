"""Reading what a photo says about itself: EXIF, PNG text chunks, XMP.

Everything here is a *claim the file makes*. EXIF is trivially editable, so a
plausible capture date proves nothing - but an implausible one (after the
upload, hundreds of kilometres from campus, written by an editor) is a
question worth a reviewer's minute. And its absence is weak evidence only:
messaging apps, screenshots and many upload tools strip it routinely.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

# EXIF tag ids, from the EXIF 2.32 specification.
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_SOFTWARE = 0x0131
_TAG_DATETIME = 0x0132
_TAG_EXIF_IFD = 0x8769
_TAG_GPS_IFD = 0x8825
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_PROCESSING_SOFTWARE = 0x000B  # in IFD0 for some writers

_GPS_LAT_REF = 1
_GPS_LAT = 2
_GPS_LON_REF = 3
_GPS_LON = 4


def read_metadata(image: Any, raw_bytes: bytes | None = None) -> dict[str, Any]:
    """Pull the metadata fields the checks use out of a PIL image."""
    meta: dict[str, Any] = {
        "format": image.format,
        "width": image.width,
        "height": image.height,
        "has_exif": False,
        "make": None,
        "model": None,
        "software": None,
        "capture_datetime": None,
        "capture_datetime_source": None,
        "gps": None,
        "text_chunks": {},
        "xmp_present": False,
        "xmp_excerpt": None,
    }

    try:
        exif = image.getexif()
    except Exception:
        exif = None

    if exif and len(exif) > 0:
        meta["has_exif"] = True
        meta["make"] = _clean(exif.get(_TAG_MAKE))
        meta["model"] = _clean(exif.get(_TAG_MODEL))
        meta["software"] = _clean(exif.get(_TAG_SOFTWARE)) or _clean(exif.get(_TAG_PROCESSING_SOFTWARE))
        try:
            sub = exif.get_ifd(_TAG_EXIF_IFD)
        except Exception:
            sub = {}
        original = _clean(sub.get(_TAG_DATETIME_ORIGINAL)) if sub else None
        if original:
            meta["capture_datetime"] = _exif_datetime(original)
            meta["capture_datetime_source"] = "DateTimeOriginal"
        elif _clean(exif.get(_TAG_DATETIME)):
            meta["capture_datetime"] = _exif_datetime(_clean(exif.get(_TAG_DATETIME)))
            meta["capture_datetime_source"] = "DateTime"
        try:
            gps = exif.get_ifd(_TAG_GPS_IFD)
        except Exception:
            gps = {}
        if gps:
            meta["gps"] = _gps(gps)

    # PNG tEXt/iTXt chunks land in image.info. Generators often write their
    # parameters there. Keys and short values only - never the whole blob.
    for key, value in (getattr(image, "info", {}) or {}).items():
        if key in ("exif", "icc_profile", "dpi", "jfif", "jfif_version", "jfif_unit", "jfif_density", "adobe", "adobe_transform", "progressive", "progression"):
            continue
        if isinstance(value, (str, bytes)):
            text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
            if key == "xmp":
                meta["xmp_present"] = True
                meta["xmp_excerpt"] = text[:400]
            else:
                meta["text_chunks"][str(key)] = text[:300]

    if raw_bytes is not None and not meta["xmp_present"]:
        start = raw_bytes.find(b"<x:xmpmeta")
        if start >= 0:
            meta["xmp_present"] = True
            meta["xmp_excerpt"] = raw_bytes[start : start + 400].decode("utf-8", "replace")

    return meta


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def searchable_text(meta: dict[str, Any]) -> str:
    """Every self-description in the file, lowercased, for marker search."""
    parts = [
        meta.get("software") or "",
        meta.get("make") or "",
        meta.get("model") or "",
        meta.get("xmp_excerpt") or "",
    ]
    for key, value in (meta.get("text_chunks") or {}).items():
        parts.append(f"{key} {value}")
    return " ".join(parts).lower()


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).replace("\x00", "").strip()
    return text or None


def _exif_datetime(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(value[:19], fmt).isoformat()
        except ValueError:
            continue
    return None


def _ratio(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            numerator, denominator = value
            return float(numerator) / float(denominator)
        except Exception:
            return math.nan


def _dms(values: Any) -> float:
    degrees, minutes, seconds = (_ratio(v) for v in values)
    return degrees + minutes / 60.0 + seconds / 3600.0


def _gps(gps: dict[int, Any]) -> dict[str, Any] | None:
    try:
        lat = _dms(gps[_GPS_LAT])
        lon = _dms(gps[_GPS_LON])
    except Exception:
        return None
    if str(gps.get(_GPS_LAT_REF, "N")).upper().startswith("S"):
        lat = -lat
    if str(gps.get(_GPS_LON_REF, "E")).upper().startswith("W"):
        lon = -lon
    if math.isnan(lat) or math.isnan(lon):
        return None
    return {"lat": round(lat, 6), "lon": round(lon, 6)}
