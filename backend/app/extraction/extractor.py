"""Per-page text extraction from PDFs and images.

For a PDF, each page is decided independently: if its embedded text layer
clears the configured thresholds it is used as-is, otherwise the page is
rendered at the configured DPI and handed to the OCR engine. Image files go
straight to OCR.

All failures are reported as a status on the result. Nothing here raises for a
bad input file.
"""

from __future__ import annotations

import io
import string
from pathlib import Path
from typing import Any

from ..core.config import get, load_config
from .models import (
    DocumentResult,
    ExtractionError,
    ExtractionMethod,
    ExtractionStatus,
    PageResult,
)
from .ocr import OCRFailedError, OCRUnavailableError, get_engine
from .ocr.base import OCREngine

CONFIG_NAME = "extraction"

# Characters treated as legible when scoring a text layer. Anything else
# (control codes, replacement glyphs, stray scripts in an English certificate)
# counts against the ratio.
_READABLE_CHARS = frozenset(
    string.ascii_letters + string.digits + string.whitespace + string.punctuation
)


def extract_document(
    path: str | Path,
    config: dict[str, Any] | None = None,
    engine: OCREngine | None = None,
) -> DocumentResult:
    """Extract text from one PDF or image file.

    `config` and `engine` are injectable for tests; both default to what
    config/extraction.yaml specifies.
    """
    cfg = config if config is not None else load_config(CONFIG_NAME)
    source = Path(path)

    engine_name = get(cfg, "ocr.engine", "rapidocr")
    dpi = int(get(cfg, "pdf.render_dpi", 300))
    base = DocumentResult(
        source_path=str(source),
        file_type="unknown",
        status=ExtractionStatus.FAILED,
        page_count=0,
        ocr_engine=engine.name if engine is not None else engine_name,
        config_version=cfg.get("config_version"),
        render_dpi=dpi,
    )

    if not source.exists():
        base.error = ExtractionError.FILE_NOT_FOUND
        base.error_detail = f"no such file: {source}"
        return base
    if not source.is_file():
        base.error = ExtractionError.UNSUPPORTED_FORMAT
        base.error_detail = f"not a file: {source}"
        return base

    suffix = source.suffix.lower()
    image_exts = [str(e).lower() for e in get(cfg, "images.allowed_extensions", [])]

    if suffix == ".pdf":
        base.file_type = "pdf"
        return _extract_pdf(source, cfg, base, engine)
    if suffix in image_exts:
        base.file_type = "image"
        return _extract_image(source, cfg, base, engine)

    base.error = ExtractionError.UNSUPPORTED_FORMAT
    base.error_detail = (
        f"unsupported extension {suffix!r}; expected .pdf or one of "
        f"{', '.join(image_exts)}"
    )
    return base


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def _open_pymupdf() -> Any:
    """Import PyMuPDF under either of its module names."""
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        import fitz

        return fitz


def _extract_pdf(
    source: Path,
    cfg: dict[str, Any],
    result: DocumentResult,
    engine: OCREngine | None,
) -> DocumentResult:
    try:
        fitz = _open_pymupdf()
    except ImportError:
        result.error = ExtractionError.OCR_ENGINE_UNAVAILABLE
        result.error_detail = (
            "PyMuPDF is not installed. Install it with: pip install pymupdf"
        )
        return result

    try:
        doc = fitz.open(source)
    except Exception as exc:
        result.error = ExtractionError.CORRUPT_FILE
        result.error_detail = f"could not open PDF: {exc}"
        return result

    try:
        if doc.needs_pass:
            result.error = ExtractionError.PASSWORD_PROTECTED
            result.error_detail = "PDF is encrypted and no password was supplied"
            return result

        try:
            page_count = doc.page_count
        except Exception as exc:
            result.error = ExtractionError.CORRUPT_FILE
            result.error_detail = f"could not read page count: {exc}"
            return result

        result.page_count = page_count
        if page_count == 0:
            result.error = ExtractionError.EMPTY_DOCUMENT
            result.error_detail = "PDF contains no pages"
            return result

        max_pages = int(get(cfg, "pdf.max_pages", 50))
        if page_count > max_pages:
            result.error = ExtractionError.PAGE_LIMIT_EXCEEDED
            result.error_detail = (
                f"PDF has {page_count} pages, above the configured limit "
                f"of {max_pages}"
            )
            return result

        lazy = _LazyEngine(cfg, engine)
        also_ocr_text_layer = bool(get(cfg, "output.ocr_text_layer_pages_too", False))

        for index in range(page_count):
            result.pages.append(
                _extract_pdf_page(doc, index, cfg, lazy, also_ocr_text_layer)
            )

        result.ocr_engine = lazy.name
        result.status = _roll_up_status(result.pages)
        return result
    finally:
        doc.close()


def _extract_pdf_page(
    doc: Any,
    index: int,
    cfg: dict[str, Any],
    lazy: "_LazyEngine",
    also_ocr_text_layer: bool,
) -> PageResult:
    page_number = index + 1
    try:
        page = doc.load_page(index)
        raw_text = page.get_text("text") or ""
    except Exception as exc:
        return PageResult(
            page_number=page_number,
            text="",
            extraction_method=None,
            avg_confidence=None,
            text_layer_present=False,
            decision_reason="page could not be read",
            error=ExtractionError.CORRUPT_FILE,
            error_detail=f"could not read page: {exc}",
        )

    chars, words, ratio = _score_text_layer(raw_text)
    usable, reason = _text_layer_is_usable(chars, words, ratio, cfg)

    result = PageResult(
        page_number=page_number,
        text="",
        extraction_method=None,
        avg_confidence=None,
        text_layer_present=chars > 0,
        text_layer_chars=chars,
        text_layer_words=words,
        text_layer_readable_ratio=ratio,
        decision_reason=reason,
    )

    if usable:
        result.text = raw_text
        result.extraction_method = ExtractionMethod.TEXT_LAYER
        # avg_confidence stays None: an embedded text layer is read, not guessed.
        if also_ocr_text_layer:
            ocr = _ocr_pdf_page(page, cfg, lazy)
            if ocr is not None and ocr.error is None:
                result.ocr_text = ocr.text
        return result

    ocr = _ocr_pdf_page(page, cfg, lazy)
    if ocr is None:
        result.error = ExtractionError.PAGE_RENDER_FAILED
        result.error_detail = "page could not be rendered for OCR"
        return result

    result.text = ocr.text
    result.extraction_method = None if ocr.error else ExtractionMethod.OCR
    result.avg_confidence = ocr.avg_confidence
    result.low_confidence = ocr.low_confidence
    result.error = ocr.error
    result.error_detail = ocr.error_detail
    return result


def _ocr_pdf_page(
    page: Any, cfg: dict[str, Any], lazy: "_LazyEngine"
) -> PageResult | None:
    """Render one page and OCR it. Returns None if rendering failed."""
    dpi = int(get(cfg, "pdf.render_dpi", 300))
    try:
        pixmap = page.get_pixmap(dpi=dpi)
        image_png = pixmap.tobytes("png")
    except Exception:
        return None
    return _run_ocr(image_png, cfg, lazy)


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #


def _extract_image(
    source: Path,
    cfg: dict[str, Any],
    result: DocumentResult,
    engine: OCREngine | None,
) -> DocumentResult:
    try:
        image_png = _to_png_bytes(source)
    except Exception as exc:
        result.error = ExtractionError.CORRUPT_FILE
        result.error_detail = f"could not read image: {exc}"
        return result

    lazy = _LazyEngine(cfg, engine)
    page = _run_ocr(image_png, cfg, lazy)
    page.decision_reason = "image file: OCR is the only option"

    result.page_count = 1
    result.pages = [page]
    result.ocr_engine = lazy.name
    result.status = _roll_up_status(result.pages)
    return result


def _to_png_bytes(source: Path) -> bytes:
    """Re-encode an image to PNG so engines receive one predictable format."""
    from PIL import Image

    with Image.open(source) as image:
        image.load()
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


class _LazyEngine:
    """Builds the OCR engine on first use, so a text-layer-only PDF never pays
    the model load cost."""

    def __init__(self, cfg: dict[str, Any], engine: OCREngine | None) -> None:
        self._cfg = cfg
        self._engine = engine
        self.name = (
            engine.name if engine is not None else get(cfg, "ocr.engine", "rapidocr")
        )

    def get(self) -> OCREngine:
        if self._engine is None:
            name = get(self._cfg, "ocr.engine", "rapidocr")
            settings = get(self._cfg, f"ocr.engines.{name}", {}) or {}
            self._engine = get_engine(name, settings)
            self.name = self._engine.name
        return self._engine


def _run_ocr(image_png: bytes, cfg: dict[str, Any], lazy: _LazyEngine) -> PageResult:
    """OCR one image into a single-page result."""
    page = PageResult(
        page_number=1,
        text="",
        extraction_method=ExtractionMethod.OCR,
        avg_confidence=None,
        text_layer_present=False,
        decision_reason="text layer unusable or absent: OCR used",
    )

    try:
        engine = lazy.get()
        ocr = engine.recognize(image_png)
    except OCRUnavailableError as exc:
        page.extraction_method = None
        page.error = ExtractionError.OCR_ENGINE_UNAVAILABLE
        page.error_detail = str(exc)
        return page
    except OCRFailedError as exc:
        page.extraction_method = None
        page.error = ExtractionError.OCR_FAILED
        page.error_detail = str(exc)
        return page
    except Exception as exc:
        page.extraction_method = None
        page.error = ExtractionError.OCR_FAILED
        page.error_detail = f"unexpected OCR error: {exc}"
        return page

    page.text = ocr.text
    page.avg_confidence = ocr.confidence
    floor = float(get(cfg, "ocr.min_avg_confidence", 0.6))
    page.low_confidence = ocr.confidence is not None and ocr.confidence < floor
    return page


def _score_text_layer(text: str) -> tuple[int, int, float]:
    """Return (char count, word count, readable character ratio)."""
    stripped = text.strip()
    chars = len(stripped)
    words = len(stripped.split())
    if chars == 0:
        return 0, 0, 0.0
    readable = sum(1 for ch in stripped if ch in _READABLE_CHARS)
    return chars, words, round(readable / chars, 4)


def _text_layer_is_usable(
    chars: int, words: int, ratio: float, cfg: dict[str, Any]
) -> tuple[bool, str]:
    """Decide whether a page's text layer can be trusted.

    Returns the decision and a human-readable reason, which is carried on the
    page result so a reviewer can see why OCR did or did not run.
    """
    min_chars = int(get(cfg, "pdf.text_layer.min_chars", 80))
    min_words = int(get(cfg, "pdf.text_layer.min_words", 15))
    min_ratio = float(get(cfg, "pdf.text_layer.min_readable_ratio", 0.70))

    if chars == 0:
        return False, "no text layer on this page"
    if chars < min_chars:
        return False, f"text layer has {chars} chars, below the minimum of {min_chars}"
    if words < min_words:
        return False, f"text layer has {words} words, below the minimum of {min_words}"
    if ratio < min_ratio:
        return False, (
            f"text layer readable ratio {ratio} is below the minimum of {min_ratio}"
        )
    return True, (
        f"text layer accepted: {chars} chars, {words} words, readable ratio {ratio}"
    )


def _roll_up_status(pages: list[PageResult]) -> ExtractionStatus:
    if not pages:
        return ExtractionStatus.FAILED
    failed = sum(1 for p in pages if p.error is not None)
    if failed == 0:
        return ExtractionStatus.OK
    if failed == len(pages):
        return ExtractionStatus.FAILED
    return ExtractionStatus.PARTIAL
