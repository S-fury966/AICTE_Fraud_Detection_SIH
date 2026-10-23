"""Tests for the document text extraction module.

Most tests inject a stub OCR engine so the per-page text_layer-vs-OCR decision
and the error handling are asserted deterministically, with no model load. The
tests marked `ocr` run the real engine and are the ones to deselect when
RapidOCR is unavailable:

    pytest -m "not ocr"
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.extraction import extract_document
from app.extraction.models import ExtractionError, ExtractionMethod, ExtractionStatus
from app.extraction.ocr.base import OCRResult, OCRUnavailableError

STUB_TEXT = "STUB OCR: Certificate Number NBTA/DEMO/2024/00417"


class StubEngine:
    """Records every call so tests can assert whether OCR ran."""

    name = "stub"

    def __init__(self, text: str = STUB_TEXT, confidence: float | None = 0.91) -> None:
        self._text = text
        self._confidence = confidence
        self.calls = 0

    def recognize(self, image_png: bytes) -> OCRResult:
        self.calls += 1
        assert image_png[:4] == b"\x89PNG", "engine should receive a PNG"
        return OCRResult(
            text=self._text,
            confidence=self._confidence,
            line_count=1,
            engine=self.name,
        )


class BrokenEngine:
    name = "broken"

    def recognize(self, image_png: bytes) -> OCRResult:
        raise OCRUnavailableError("engine not installed")


# --------------------------------------------------------------------------- #
# The text_layer / OCR decision
# --------------------------------------------------------------------------- #


def test_text_layer_pdf_uses_text_layer(samples, config):
    engine = StubEngine()
    result = extract_document(samples["text_layer_pdf"], config=config, engine=engine)

    assert result.status is ExtractionStatus.OK
    assert result.file_type == "pdf"
    assert result.page_count == 1

    page = result.pages[0]
    assert page.page_number == 1
    assert page.extraction_method is ExtractionMethod.TEXT_LAYER
    assert page.text_layer_present is True
    # An embedded text layer is read exactly, so there is no confidence to report.
    assert page.avg_confidence is None
    assert "NBTA/DEMO/2024/00417" in page.text
    assert "Shantiniketan Institute of Technology" in page.text
    assert engine.calls == 0, "OCR must not run when the text layer is usable"


def test_text_layer_page_leaves_ocr_text_slot_empty(samples, config):
    """The ocr_text field exists for the later tamper check but stays unset."""
    result = extract_document(
        samples["text_layer_pdf"], config=config, engine=StubEngine()
    )
    assert result.pages[0].ocr_text is None


def test_ocr_text_slot_fills_when_config_enables_it(samples, config):
    cfg = copy.deepcopy(config)
    cfg["output"]["ocr_text_layer_pages_too"] = True

    engine = StubEngine()
    result = extract_document(samples["text_layer_pdf"], config=cfg, engine=engine)

    page = result.pages[0]
    # Still a text_layer page; the OCR pass is only extra material to compare.
    assert page.extraction_method is ExtractionMethod.TEXT_LAYER
    assert page.ocr_text == STUB_TEXT
    assert engine.calls == 1


def test_scanned_pdf_falls_back_to_ocr(samples, config):
    engine = StubEngine()
    result = extract_document(samples["scanned_pdf"], config=config, engine=engine)

    assert result.status is ExtractionStatus.OK
    page = result.pages[0]
    assert page.extraction_method is ExtractionMethod.OCR
    assert page.text_layer_present is False
    assert page.text == STUB_TEXT
    assert page.avg_confidence == pytest.approx(0.91)
    assert page.low_confidence is False
    assert engine.calls == 1
    assert "no text layer" in page.decision_reason


def test_thin_text_layer_falls_back_to_ocr(samples, config):
    """A text layer exists but is too sparse to trust, so OCR takes over."""
    engine = StubEngine()
    result = extract_document(samples["thin_text_pdf"], config=config, engine=engine)

    page = result.pages[0]
    assert page.text_layer_present is True
    assert page.extraction_method is ExtractionMethod.OCR
    assert page.text_layer_chars < config["pdf"]["text_layer"]["min_chars"]
    assert "below the minimum" in page.decision_reason
    assert engine.calls == 1


def test_image_file_goes_straight_to_ocr(samples, config):
    engine = StubEngine()
    result = extract_document(
        samples["certificate_image"], config=config, engine=engine
    )

    assert result.status is ExtractionStatus.OK
    assert result.file_type == "image"
    assert result.page_count == 1
    page = result.pages[0]
    assert page.extraction_method is ExtractionMethod.OCR
    assert page.text_layer_present is False
    assert page.avg_confidence == pytest.approx(0.91)
    assert engine.calls == 1


# --------------------------------------------------------------------------- #
# Configurability
# --------------------------------------------------------------------------- #


def test_raising_min_chars_flips_a_good_page_to_ocr(samples, config):
    """Thresholds come from config, so changing one changes the decision."""
    cfg = copy.deepcopy(config)
    cfg["pdf"]["text_layer"]["min_chars"] = 100_000

    engine = StubEngine()
    result = extract_document(samples["text_layer_pdf"], config=cfg, engine=engine)

    assert result.pages[0].extraction_method is ExtractionMethod.OCR
    assert engine.calls == 1


def test_low_confidence_is_flagged_against_the_configured_floor(samples, config):
    cfg = copy.deepcopy(config)
    cfg["ocr"]["min_avg_confidence"] = 0.80

    result = extract_document(
        samples["certificate_image"],
        config=cfg,
        engine=StubEngine(confidence=0.42),
    )

    page = result.pages[0]
    assert page.low_confidence is True
    # A low score is reported, never a reason to discard the text.
    assert page.text == STUB_TEXT
    assert result.status is ExtractionStatus.OK


def test_config_version_is_stamped_on_the_result(samples, config):
    result = extract_document(
        samples["text_layer_pdf"], config=config, engine=StubEngine()
    )
    assert result.config_version == config["config_version"]
    assert result.render_dpi == config["pdf"]["render_dpi"]


def test_page_limit_is_enforced(samples, config):
    cfg = copy.deepcopy(config)
    cfg["pdf"]["max_pages"] = 0

    result = extract_document(samples["text_layer_pdf"], config=cfg)

    assert result.status is ExtractionStatus.FAILED
    assert result.error is ExtractionError.PAGE_LIMIT_EXCEEDED
    assert result.pages == []


# --------------------------------------------------------------------------- #
# Error handling: a bad file is a status, never an exception
# --------------------------------------------------------------------------- #


def test_corrupt_pdf_reports_a_status(samples, config):
    result = extract_document(samples["corrupt_pdf"], config=config)

    assert result.status is ExtractionStatus.FAILED
    assert result.error is ExtractionError.CORRUPT_FILE
    assert result.error_detail


def test_password_protected_pdf_reports_a_status(samples, config):
    result = extract_document(samples["encrypted_pdf"], config=config)

    assert result.status is ExtractionStatus.FAILED
    assert result.error is ExtractionError.PASSWORD_PROTECTED


def test_zero_page_pdf_reports_a_status(samples, config):
    result = extract_document(samples["empty_pdf"], config=config)

    assert result.status is ExtractionStatus.FAILED
    assert result.error is ExtractionError.EMPTY_DOCUMENT
    assert result.page_count == 0


def test_missing_file_reports_a_status(tmp_path, config):
    result = extract_document(tmp_path / "nope.pdf", config=config)

    assert result.status is ExtractionStatus.FAILED
    assert result.error is ExtractionError.FILE_NOT_FOUND


def test_unsupported_extension_reports_a_status(tmp_path, config):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    result = extract_document(path, config=config)

    assert result.status is ExtractionStatus.FAILED
    assert result.error is ExtractionError.UNSUPPORTED_FORMAT


def test_unreadable_image_reports_a_status(tmp_path, config):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image at all")

    result = extract_document(path, config=config)

    assert result.status is ExtractionStatus.FAILED
    assert result.error is ExtractionError.CORRUPT_FILE


def test_missing_ocr_engine_reports_a_status(samples, config):
    """An absent OCR engine degrades to a clear status, it does not crash."""
    result = extract_document(
        samples["scanned_pdf"], config=config, engine=BrokenEngine()
    )

    assert result.status is ExtractionStatus.FAILED
    page = result.pages[0]
    assert page.error is ExtractionError.OCR_ENGINE_UNAVAILABLE
    assert page.extraction_method is None
    assert page.text == ""


# --------------------------------------------------------------------------- #
# Serialisation and CLI
# --------------------------------------------------------------------------- #


def test_result_serialises_to_json(samples, config):
    result = extract_document(
        samples["text_layer_pdf"], config=config, engine=StubEngine()
    )
    payload = json.loads(json.dumps(result.to_dict()))

    assert payload["status"] == "ok"
    assert payload["file_type"] == "pdf"
    page = payload["pages"][0]
    assert page["extraction_method"] == "text_layer"
    assert page["avg_confidence"] is None
    assert page["text_layer_present"] is True
    assert page["ocr_text"] is None
    assert page["error"] is None


@pytest.mark.ocr
def test_cli_prints_json(samples, capsys):
    from app.extraction.cli import main

    exit_code = main([str(samples["text_layer_pdf"])])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["pages"][0]["extraction_method"] == "text_layer"


def test_cli_reports_failure_exit_code(tmp_path, capsys):
    from app.extraction.cli import main

    exit_code = main([str(tmp_path / "missing.pdf")])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error"] == "file_not_found"


# --------------------------------------------------------------------------- #
# The real engine
# --------------------------------------------------------------------------- #


@pytest.mark.ocr
def test_real_ocr_reads_the_certificate_image(samples, config):
    """End-to-end with the configured engine.

    Asserts on structure and on a couple of robust tokens rather than exact
    text, since OCR output varies with the engine version and the font
    available on the machine.
    """
    result = extract_document(samples["certificate_image"], config=config)

    if (
        result.pages
        and result.pages[0].error is ExtractionError.OCR_ENGINE_UNAVAILABLE
    ):
        pytest.skip("OCR engine not installed")

    assert result.status is ExtractionStatus.OK
    page = result.pages[0]
    assert page.extraction_method is ExtractionMethod.OCR
    assert page.avg_confidence is not None and page.avg_confidence > 0.3
    assert len(page.text) > 50

    collapsed = page.text.lower().replace(" ", "")
    assert "institution" in collapsed
    assert "certificate" in collapsed


@pytest.mark.ocr
def test_real_ocr_on_scanned_pdf(samples, config):
    result = extract_document(samples["scanned_pdf"], config=config)

    if (
        result.pages
        and result.pages[0].error is ExtractionError.OCR_ENGINE_UNAVAILABLE
    ):
        pytest.skip("OCR engine not installed")

    page = result.pages[0]
    assert page.text_layer_present is False
    assert page.extraction_method is ExtractionMethod.OCR
    assert page.avg_confidence is not None
    assert len(page.text) > 50
