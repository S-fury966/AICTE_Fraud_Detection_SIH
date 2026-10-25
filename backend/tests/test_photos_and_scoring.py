"""Photo checks, risk scoring, explanations and wording rules."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from PIL import Image

from app.core.config import load_config
from app.explain import contains_banned_term, explain, template_explanation
from app.llm.stub_provider import StubProvider
from app.photos import SYNTHETIC_WORDING, check_manipulation, check_metadata, check_synthetic, load_photo
from app.scoring import score_findings
from demo import photos as kit

UPLOAD = datetime(2026, 10, 20, tzinfo=timezone.utc)
CAMPUS = {"lat": 23.6693, "lon": 87.6857}


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return load_photo(path)


def _types(findings):
    return {f["type"]: f["strength"] for f in findings}


# --------------------------------------------------------------------------
# EXIF
# --------------------------------------------------------------------------


def test_missing_exif_is_weak_only(tmp_path):
    photo = _write(tmp_path, "a.jpg", kit.save_jpeg(kit.render_room(1), None))
    _, findings = check_metadata(photo, uploaded_at=UPLOAD, institution_location=CAMPUS)
    assert _types(findings) == {"photo_exif_missing": "weak"}


def test_plausible_camera_photo_raises_nothing(tmp_path):
    exif = kit.make_exif(datetime(2026, 8, 1, 10), (CAMPUS["lat"] + 0.001, CAMPUS["lon"]))
    photo = _write(tmp_path, "a.jpg", kit.save_jpeg(kit.render_room(2), exif))
    detail, findings = check_metadata(photo, uploaded_at=UPLOAD, institution_location=CAMPUS)
    assert findings == []
    assert detail["gps_distance_km"] < 1


def test_gps_far_away_and_future_date_are_flagged(tmp_path):
    exif = kit.make_exif(datetime(2027, 1, 15, 11), (12.97, 77.59))
    photo = _write(tmp_path, "a.jpg", kit.save_jpeg(kit.render_room(3), exif))
    _, findings = check_metadata(photo, uploaded_at=UPLOAD, institution_location=CAMPUS)
    types = _types(findings)
    assert types["photo_gps_far_from_institution"] == "strong"  # > very_far_km
    assert types["photo_capture_date_after_upload"] == "medium"


def test_editing_software_tag_is_weak(tmp_path):
    exif = kit.make_exif(datetime(2026, 8, 1), None, software="Adobe Photoshop 25.1")
    photo = _write(tmp_path, "a.jpg", kit.save_jpeg(kit.render_room(4), exif))
    _, findings = check_metadata(photo, uploaded_at=UPLOAD)
    assert _types(findings)["photo_editing_software_tag"] == "weak"


# --------------------------------------------------------------------------
# Manipulation and synthetic hint
# --------------------------------------------------------------------------


@pytest.mark.qr
def test_untouched_render_raises_no_manipulation_finding(tmp_path):
    photo = _write(tmp_path, "a.jpg", kit.save_jpeg(kit.render_room(130), None))
    _, findings = check_manipulation(photo)
    assert findings == []


@pytest.mark.qr
def test_cloned_region_is_reported_with_both_regions(tmp_path):
    photo = _write(tmp_path, "a.jpg", kit.save_jpeg(kit.clone_region(kit.render_room(101), 5), None))
    detail, findings = check_manipulation(photo)
    assert _types(findings)["photo_copy_move_region"] == "medium"
    assert detail["copy_move"]["source_region_xywh"] and detail["copy_move"]["target_region_xywh"]


@pytest.mark.qr
def test_pasted_patch_shows_in_ela_and_writes_a_heatmap(tmp_path):
    photo = _write(tmp_path, "a.jpg", kit.save_jpeg(kit.paste_foreign_patch(kit.render_room(103), 7), None))
    detail, findings = check_manipulation(photo, heatmap_path=tmp_path / "ela.png")
    assert _types(findings)["photo_ela_inconsistency"] == "weak"
    assert (tmp_path / "ela.png").exists()


def test_ela_is_skipped_on_a_lossless_image(tmp_path):
    buffer = io.BytesIO()
    kit.render_room(5).save(buffer, format="PNG")
    photo = _write(tmp_path, "a.png", buffer.getvalue())
    detail, _ = check_manipulation(photo)
    assert detail["ela"]["computed"] is False


def test_generator_marker_uses_the_fixed_wording_and_never_says_deepfake(tmp_path):
    photo = _write(tmp_path, "a.png", kit.save_generated_png(kit.render_room(6), "Model: sdxl"))
    _, findings = check_synthetic(photo, None)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["confidence"] == "low"
    assert SYNTHETIC_WORDING in finding["reason"].lower()
    assert not contains_banned_term(finding["reason"])


def test_a_plain_photo_gets_no_synthetic_hint(tmp_path):
    photo = _write(tmp_path, "a.jpg", kit.save_jpeg(kit.render_room(7), None))
    assert check_synthetic(photo, None)[1] == []


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _finding(i, detector, type_, strength, status="open", confidence=None):
    return {"id": i, "detector": detector, "type": type_, "strength": strength, "review_status": status, "confidence": confidence}


def test_no_findings_scores_zero_and_low():
    result = score_findings([])
    assert result["score"] == 0 and result["level"] == "low"


def test_one_strong_finding_is_never_low_priority():
    result = score_findings([_finding(1, "photo_authenticity", "photo_gps_far_from_institution", "strong", confidence="low")])
    assert result["level"] != "low"


def test_the_strong_floor_applies_when_points_alone_stay_low():
    cfg = load_config("risk_scoring")
    cheap = {**cfg, "points": {**cfg["points"], "type_overrides": {"photo_gps_far_from_institution": 1}}}
    result = score_findings([_finding(1, "photo_authenticity", "photo_gps_far_from_institution", "strong")], cheap)
    assert result["level"] == "medium"
    assert result["floors_applied"][0]["rule"] == "floor_for_strong"


def test_strong_findings_from_two_detectors_are_high():
    result = score_findings([
        _finding(1, "certificate_authenticity", "qr_domain_not_allowlisted", "strong"),
        _finding(2, "faculty_consistency", "faculty_count_exceeds_records", "strong", confidence="low"),
    ])
    assert result["level"] == "high"


def test_dismissed_findings_stop_counting_and_are_listed():
    findings = [_finding(1, "certificate_authenticity", "qr_domain_not_allowlisted", "strong", status="dismissed")]
    result = score_findings(findings)
    assert result["score"] == 0
    assert result["excluded_findings"][0]["id"] == 1


def test_repeats_of_one_type_decay():
    one = score_findings([_finding(1, "photo_authenticity", "photo_editing_software_tag", "weak")])["score"]
    ten = score_findings([_finding(i, "photo_authenticity", "photo_editing_software_tag", "weak") for i in range(10)])["score"]
    assert one < ten < one * 2


def test_unavailable_sources_are_visible_but_worth_zero():
    result = score_findings([_finding(1, "faculty_consistency", "faculty_source_unavailable", "weak")])
    assert result["score"] == 0
    assert result["contributions"][0]["points"] == 0


def test_nothing_in_the_weights_can_lower_a_score():
    cfg = load_config("risk_scoring")
    values = list((cfg["points"]["strength"] or {}).values()) + list((cfg["points"]["type_overrides"] or {}).values())
    assert all(v >= 0 for v in values)
    assert all(v >= 0 for v in cfg["points"]["confidence_multiplier"].values())


def test_scoring_is_deterministic():
    findings = [_finding(i, "faculty_consistency", f"t{i % 3}", ["strong", "medium", "weak"][i % 3]) for i in range(9)]
    assert score_findings(findings) == score_findings(list(reversed(findings)))


# --------------------------------------------------------------------------
# Explanations
# --------------------------------------------------------------------------


def _risk_and_findings():
    findings = [{"id": 1, "detector": "certificate_authenticity", "type": "qr_domain_not_allowlisted", "strength": "strong", "review_status": "open", "confidence": None, "reason": "The QR code points at host 'x.test', which is not on the allowlist."}]
    return score_findings(findings), findings


def test_template_explanation_never_uses_banned_terms():
    risk, findings = _risk_and_findings()
    text = template_explanation(risk, findings)
    combined = text["headline"] + " ".join(p["text"] for p in text["points"]) + text["disclaimer"]
    assert not contains_banned_term(combined)


def test_an_llm_rewrite_with_a_banned_term_is_discarded():
    risk, findings = _risk_and_findings()
    stub = StubProvider(responses=["This certificate is forged and must be rejected."])
    result = explain(risk, findings, provider=stub)
    assert result["generated_by"] == "template"
    assert "banned" in result["llm_rejected"]


def test_an_llm_rewrite_that_invents_a_number_is_discarded():
    risk, findings = _risk_and_findings()
    stub = StubProvider(responses=["Placed at high priority because 7 issues were found."])
    result = explain(risk, findings, provider=stub)
    assert result["generated_by"] == "template"
    assert "numbers" in result["llm_rejected"]
