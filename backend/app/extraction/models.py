"""Result types for document text extraction.

These statuses describe whether *extraction* succeeded. They are deliberately
separate from the project's verification status taxonomy (verified,
mismatch_detected, expired, verification_pending, source_unavailable,
manual_review_required), which belongs to the detectors that consume this
output, not to extraction itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ExtractionStatus(str, Enum):
    """Outcome of extracting a whole document."""

    OK = "ok"
    # Some pages extracted, others failed. Usable, but incomplete.
    PARTIAL = "partial"
    FAILED = "failed"


class ExtractionError(str, Enum):
    """Why a document (or page) could not be extracted."""

    FILE_NOT_FOUND = "file_not_found"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CORRUPT_FILE = "corrupt_file"
    PASSWORD_PROTECTED = "password_protected"
    EMPTY_DOCUMENT = "empty_document"
    PAGE_LIMIT_EXCEEDED = "page_limit_exceeded"
    OCR_ENGINE_UNAVAILABLE = "ocr_engine_unavailable"
    PAGE_RENDER_FAILED = "page_render_failed"
    OCR_FAILED = "ocr_failed"


class ExtractionMethod(str, Enum):
    TEXT_LAYER = "text_layer"
    OCR = "ocr"


@dataclass
class PageResult:
    """Extracted text for a single page."""

    page_number: int  # 1-based
    text: str
    extraction_method: ExtractionMethod | None
    # Average OCR confidence in 0.0-1.0. Always None for text_layer pages,
    # because an embedded text layer is read exactly, not guessed.
    avg_confidence: float | None
    text_layer_present: bool

    # Reserved for the later tamper check: OCR of a page that already had a
    # usable text layer, so the two can be compared. Step 1 never fills this in
    # unless output.ocr_text_layer_pages_too is enabled, and performs no
    # comparison.
    ocr_text: str | None = None

    # Explainability: the measurements behind the text_layer-vs-OCR decision.
    text_layer_chars: int = 0
    text_layer_words: int = 0
    text_layer_readable_ratio: float = 0.0
    decision_reason: str = ""

    # True when OCR ran but avg_confidence fell below the configured floor.
    low_confidence: bool = False
    error: ExtractionError | None = None
    error_detail: str | None = None


@dataclass
class DocumentResult:
    """Extraction result for one document."""

    source_path: str
    file_type: str  # "pdf" | "image"
    status: ExtractionStatus
    page_count: int
    pages: list[PageResult] = field(default_factory=list)
    ocr_engine: str | None = None
    config_version: str | None = None
    render_dpi: int | None = None
    error: ExtractionError | None = None
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict with enums reduced to their string values."""

        def unwrap(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {k: unwrap(v) for k, v in value.items()}
            if isinstance(value, list):
                return [unwrap(v) for v in value]
            return value

        return unwrap(asdict(self))
