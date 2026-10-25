"""Photo authenticity checks for lab and classroom photographs (detector 3).

Ranked by how much the signal is worth, and the strengths follow that ranking:

1. **Reuse** - the same photo already submitted elsewhere (`reuse.py`).
2. **EXIF** - capture date, GPS vs the institution's recorded location, editing
   software tags. Missing EXIF is weak only.
3. **Manipulation** - error level analysis and copy-move. Hints, never strong.
4. **Synthetic content** - a low-confidence hint only. The wording is fixed:
   "possible synthetic content, manual verification needed". Never "deepfake".
5. **Claim vs image** - via a vision-language model, advisory only; with no VLM
   configured it is recorded as `source_unavailable` and guesses nothing.

Every function here returns `(detail, findings)` and never raises on a bad
image: an unreadable photo is a recorded result, not a crash. No network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import get, load_config
from . import forensics, metadata

CONFIG_NAME = "photo_checks"
DETECTOR = "photo_authenticity"

SYNTHETIC_WORDING = "possible synthetic content, manual verification needed"


class PhotoLoadError(RuntimeError):
    pass


@dataclass
class LoadedPhoto:
    image: Any
    raw: bytes
    path: Path


def load_photo(path: str | Path) -> LoadedPhoto:
    from PIL import Image, ImageOps

    target = Path(path)
    try:
        raw = target.read_bytes()
        image = Image.open(target)
        image.load()
    except Exception as exc:
        raise PhotoLoadError(f"image could not be opened: {type(exc).__name__}") from exc
    # Keep the original object for metadata; analyses use an upright copy.
    return LoadedPhoto(image=image, raw=raw, path=target)


def upright(image: Any) -> Any:
    from PIL import ImageOps

    try:
        return ImageOps.exif_transpose(image.copy())
    except Exception:
        return image.copy()


# --------------------------------------------------------------------------
# EXIF
# --------------------------------------------------------------------------


def check_metadata(
    photo: LoadedPhoto,
    cfg: dict[str, Any] | None = None,
    uploaded_at: datetime | None = None,
    institution_location: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    meta = metadata.read_metadata(photo.image, photo.raw)
    findings: list[dict[str, Any]] = []
    detail: dict[str, Any] = {"metadata": meta, "checks": []}
    if not bool(get(cfg, "exif.enabled", True)):
        detail["checks"].append({"check": "exif", "outcome": "disabled"})
        return detail, findings

    if not meta["has_exif"]:
        findings.append(
            _finding(
                "photo_exif_missing",
                str(get(cfg, "exif.missing_exif_strength", "weak")),
                "The photo carries no EXIF metadata. This is a weak signal "
                "only: messaging apps, screenshots and many upload tools strip "
                "it routinely.",
                {"format": meta["format"], "fields_checked": ["EXIF IFD0", "EXIF SubIFD", "GPS IFD"]},
                confidence="low",
            )
        )
        detail["checks"].append({"check": "exif_present", "outcome": "absent"})
    else:
        detail["checks"].append({"check": "exif_present", "outcome": "present"})

    # Capture date
    uploaded = uploaded_at or datetime.now(timezone.utc)
    captured_raw = meta.get("capture_datetime")
    if captured_raw:
        captured = datetime.fromisoformat(captured_raw).replace(tzinfo=timezone.utc)
        age_days = (uploaded - captured).days
        max_age = int(get(cfg, "exif.capture_date.max_age_days", 1095))
        if captured > uploaded:
            findings.append(
                _finding(
                    "photo_capture_date_after_upload",
                    str(get(cfg, "exif.capture_date.after_upload_strength", "medium")),
                    f"The EXIF capture date ({captured.date().isoformat()}) is "
                    f"after the date the photo was uploaded "
                    f"({uploaded.date().isoformat()}). Mismatch detected between "
                    f"the photo's metadata and its submission; requires manual "
                    f"verification.",
                    {"capture_datetime": captured_raw, "source_tag": meta["capture_datetime_source"], "uploaded_at": uploaded.isoformat()},
                    confidence="medium",
                )
            )
            detail["checks"].append({"check": "capture_date", "outcome": "after_upload"})
        elif age_days > max_age:
            findings.append(
                _finding(
                    "photo_capture_date_old",
                    str(get(cfg, "exif.capture_date.too_old_strength", "weak")),
                    f"The EXIF capture date is {age_days} days before upload "
                    f"(limit {max_age}). The photo may not show the facility's "
                    f"current state.",
                    {"capture_datetime": captured_raw, "age_days": age_days, "max_age_days": max_age},
                    confidence="medium",
                )
            )
            detail["checks"].append({"check": "capture_date", "outcome": "old", "age_days": age_days})
        else:
            detail["checks"].append({"check": "capture_date", "outcome": "plausible", "age_days": age_days})
    else:
        detail["checks"].append({"check": "capture_date", "outcome": "not_present"})

    # GPS
    gps = meta.get("gps")
    if gps and institution_location and institution_location.get("lat") is not None:
        distance = metadata.haversine_km(gps["lat"], gps["lon"], float(institution_location["lat"]), float(institution_location["lon"]))
        far = float(get(cfg, "exif.gps.max_distance_km", 5.0))
        very_far = float(get(cfg, "exif.gps.very_far_km", 100.0))
        detail["gps_distance_km"] = round(distance, 2)
        if distance > far:
            strength = (
                str(get(cfg, "exif.gps.very_far_strength", "strong"))
                if distance > very_far
                else str(get(cfg, "exif.gps.far_strength", "medium"))
            )
            findings.append(
                _finding(
                    "photo_gps_far_from_institution",
                    strength,
                    f"Mismatch detected between the photo's GPS location and the "
                    f"institution's recorded location: {distance:.1f} km apart "
                    f"(limit {far:g} km). Requires manual verification.",
                    {
                        "photo_gps": gps,
                        "institution_location": {k: institution_location.get(k) for k in ("lat", "lon", "city")},
                        "distance_km": round(distance, 2),
                        "max_distance_km": far,
                        "location_source": "aicte_records (mock)",
                    },
                    confidence="medium",
                )
            )
            detail["checks"].append({"check": "gps", "outcome": "far", "distance_km": round(distance, 2)})
        else:
            detail["checks"].append({"check": "gps", "outcome": "near_institution", "distance_km": round(distance, 2)})
    elif gps:
        detail["checks"].append({"check": "gps", "outcome": "not_compared", "reason": "institution location unknown"})
    else:
        detail["checks"].append({"check": "gps", "outcome": "not_present"})

    # Editing software
    software_text = " ".join(filter(None, [meta.get("software"), meta.get("xmp_excerpt")])).lower()
    markers = [str(m).lower() for m in (get(cfg, "exif.editing_software.markers", []) or [])]
    hits = sorted({m for m in markers if m in software_text})
    if hits:
        findings.append(
            _finding(
                "photo_editing_software_tag",
                str(get(cfg, "exif.editing_software.strength", "weak")),
                f"The photo's metadata names editing software ({', '.join(hits)}). "
                f"Editing is often innocent - cropping, brightness - but means "
                f"the image is not straight from the camera.",
                {"software": meta.get("software"), "markers_matched": hits},
                confidence="medium",
            )
        )
        detail["checks"].append({"check": "editing_software", "outcome": "tag_present", "markers": hits})
    else:
        detail["checks"].append({"check": "editing_software", "outcome": "none_named"})

    return detail, findings


# --------------------------------------------------------------------------
# Manipulation: ELA and copy-move
# --------------------------------------------------------------------------


def check_manipulation(
    photo: LoadedPhoto,
    cfg: dict[str, Any] | None = None,
    heatmap_path: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    findings: list[dict[str, Any]] = []
    detail: dict[str, Any] = {"ela": None, "copy_move": None}
    if not bool(get(cfg, "manipulation.enabled", True)):
        return {"disabled": True}, findings

    image = upright(photo.image)

    # ELA - JPEG only.
    if str(photo.image.format).upper() in ("JPEG", "MPO"):
        ela = forensics.error_level_analysis(
            image,
            quality=int(get(cfg, "manipulation.ela.jpeg_quality", 90)),
            block_size=int(get(cfg, "manipulation.ela.block_size", 16)),
            hot_factor=float(get(cfg, "manipulation.ela.suspicious_ratio", 8.0)),
        )
        diff = ela.pop("_diff", None)
        if diff is not None and heatmap_path is not None and bool(get(cfg, "manipulation.ela.save_heatmap", True)):
            heatmap_path.parent.mkdir(parents=True, exist_ok=True)
            forensics.ela_heatmap(diff).save(heatmap_path, format="PNG")
            ela["heatmap"] = heatmap_path.name
        detail["ela"] = ela
        threshold = float(get(cfg, "manipulation.ela.suspicious_ratio", 8.0))
        min_blocks = int(get(cfg, "manipulation.ela.min_suspicious_blocks", 20))
        if ela.get("computed") and ela["ratio"] >= threshold and ela["hot_blocks"] >= min_blocks:
            findings.append(
                _finding(
                    "photo_ela_inconsistency",
                    str(get(cfg, "manipulation.ela.strength", "weak")),
                    f"Error level analysis shows a region that re-compresses "
                    f"differently from the rest of the photo (ratio "
                    f"{ela['ratio']:.1f}, threshold {threshold:g}). This can "
                    f"indicate a local edit, but also appears on sharp edges and "
                    f"text. Compare the heatmap with the photo; requires manual "
                    f"verification.",
                    {
                        "ratio": ela["ratio"],
                        "threshold": threshold,
                        "hot_blocks": ela["hot_blocks"],
                        "region_xywh": ela.get("hot_region_xywh"),
                        "heatmap": ela.get("heatmap"),
                    },
                    confidence="low",
                )
            )
    else:
        detail["ela"] = {"computed": False, "reason": f"ELA applies to JPEG only; this file is {photo.image.format}"}

    # Copy-move
    move = forensics.copy_move(
        image,
        max_features=int(get(cfg, "manipulation.copy_move.max_features", 3000)),
        ratio_test=float(get(cfg, "manipulation.copy_move.ratio_test", 0.75)),
        min_offset_px=float(get(cfg, "manipulation.copy_move.min_offset_px", 40)),
        displacement_bin_px=int(get(cfg, "manipulation.copy_move.displacement_bin_px", 6)),
        min_cluster_matches=int(get(cfg, "manipulation.copy_move.min_cluster_matches", 100)),
        max_dimension=int(get(cfg, "manipulation.copy_move.max_dimension", 1600)),
    )
    detail["copy_move"] = move
    if move.get("suspicious"):
        findings.append(
            _finding(
                "photo_copy_move_region",
                str(get(cfg, "manipulation.copy_move.strength", "medium")),
                f"A region of the photo appears to be repeated elsewhere in the "
                f"same photo ({move['largest_cluster']} matching features "
                f"sharing one offset). Cloning can hide or duplicate equipment; "
                f"genuinely repetitive scenes can also cause this. Requires "
                f"manual verification.",
                {
                    "matching_features": move["largest_cluster"],
                    "displacement_px": move.get("displacement_px"),
                    "source_region_xywh": move.get("source_region_xywh"),
                    "target_region_xywh": move.get("target_region_xywh"),
                },
                confidence="medium",
            )
        )
    return detail, findings


# --------------------------------------------------------------------------
# Synthetic-content hint
# --------------------------------------------------------------------------


def check_synthetic(
    photo: LoadedPhoto, meta: dict[str, Any] | None, cfg: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    findings: list[dict[str, Any]] = []
    if not bool(get(cfg, "synthetic_content.enabled", True)):
        return {"disabled": True}, findings

    meta = meta or metadata.read_metadata(photo.image, photo.raw)
    text = metadata.searchable_text(meta)
    markers = [str(m).lower() for m in (get(cfg, "synthetic_content.metadata_markers", []) or [])]
    hits = sorted({m for m in markers if m in text})
    detail: dict[str, Any] = {"metadata_markers_matched": hits, "wording": SYNTHETIC_WORDING}

    if hits:
        findings.append(
            _finding(
                "photo_possible_synthetic_content",
                str(get(cfg, "synthetic_content.metadata_marker_strength", "medium")),
                f"Possible synthetic content, manual verification needed: the "
                f"file's own metadata names an image-generation tool "
                f"({', '.join(hits)}). This is a low-confidence hint, not a "
                f"determination.",
                {"markers_matched": hits, "metadata_fields": sorted((meta.get("text_chunks") or {}).keys()) + (["Software"] if meta.get("software") else [])},
                confidence="low",
            )
        )

    spectral_cfg = get(cfg, "synthetic_content.spectral_heuristic", {}) or {}
    if spectral_cfg.get("enabled"):
        value = forensics.spectral_flatness(upright(photo.image))
        detail["spectral_high_frequency_share"] = value
        if value >= float(spectral_cfg.get("threshold", 0.35)):
            findings.append(
                _finding(
                    "photo_possible_synthetic_content",
                    str(spectral_cfg.get("strength", "weak")),
                    f"Possible synthetic content, manual verification needed: an "
                    f"experimental frequency heuristic scored {value:.2f}. Low "
                    f"confidence; not validated on real photographs.",
                    {"spectral_high_frequency_share": value, "threshold": spectral_cfg.get("threshold")},
                    confidence="low",
                )
            )
    else:
        detail["spectral_heuristic"] = "disabled in config (not validated)"
    return detail, findings


# --------------------------------------------------------------------------
# Claim vs image (VLM, advisory)
# --------------------------------------------------------------------------


def check_claim(
    photo: LoadedPhoto, claim: str | None, cfg: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Returns (detail, findings, verification_status).

    With no VLM configured nothing is guessed: the status is
    `source_unavailable` and there is no finding. A configured VLM would
    describe the image; any comparison with the claim would be deterministic
    code reading that description, never the model's verdict.
    """
    cfg = cfg if cfg is not None else load_config(CONFIG_NAME)
    if not claim:
        return {"claim": None, "outcome": "no_claim_submitted"}, [], "manual_review_required"
    provider = get(cfg, "claim_consistency.vlm_provider")
    if not bool(get(cfg, "claim_consistency.enabled", True)) or not provider:
        return (
            {
                "claim": claim,
                "outcome": "not_checked",
                "reason": "no vision-language model is configured; the claim "
                "was recorded for the reviewer and not compared",
            },
            [],
            "source_unavailable",
        )
    return (
        {"claim": claim, "outcome": "not_checked", "reason": f"VLM provider {provider!r} is not implemented in this prototype"},
        [],
        "source_unavailable",
    )


def _finding(
    type_: str,
    strength: str,
    reason: str,
    evidence: dict[str, Any],
    confidence: str | None = None,
) -> dict[str, Any]:
    return {"type": type_, "strength": strength, "reason": reason, "evidence": evidence, "confidence": confidence}
