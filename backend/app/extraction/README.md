# Document text extraction

First stage of the pipeline: turn a submitted PDF or image into per-page text,
and record *how* that text was obtained. Nothing here interprets the text or
makes any judgement about a document — that is for the detectors downstream.

## What it does

- **PDF**: each page is decided on its own. If the embedded text layer clears
  the thresholds in `config/extraction.yaml` it is used as-is. Otherwise the
  page is rendered at the configured DPI (default 300) and sent to OCR.
- **Image** (`.jpg`, `.jpeg`, `.png`): OCR, since there is no text layer to try.
- Every page carries the measurements behind that decision
  (`text_layer_chars`, `text_layer_words`, `text_layer_readable_ratio`,
  `decision_reason`), so a reviewer can see why OCR did or did not run.

## Why the text layer is scored rather than just checked

A tampered certificate often has a text layer that disagrees with what is
visibly printed, and a scanned-then-"digitised" forgery can carry a text layer
that is technically present but garbage. So a page qualifies only if it has
enough characters, enough words, and a high enough ratio of legible characters.
Anything short of that is treated as untrustworthy and re-read by OCR.

`PageResult.ocr_text` is reserved for the tamper check itself: OCR of a page
that *already* had a usable text layer, so the two can be compared. Setting
`output.ocr_text_layer_pages_too: true` fills it in. **The comparison is not
implemented in this step.**

## OCR engine

The engine sits behind a one-method seam (`ocr/base.py`): PNG bytes in,
`OCRResult(text, confidence, line_count, engine)` out. Swapping engines means
adding a class and one line in `ocr/registry.py`; the pipeline does not change.

**RapidOCR is the default, chosen over Tesseract for Windows**: it installs from
PyPI with its ONNX models bundled, so there is no system binary to install and
no PATH setup on a teammate's or a judge's machine. Tesseract needs a separate
installer and `tesseract.exe` on PATH, which is a real risk on demo day.
RapidOCR also returns a per-line confidence score, which the text-layer
comparison and the risk scoring will both want. Its cost is that the current
1.4.x line declares `Requires-Python <3.13`, which is why the project targets
Python 3.10. That pin is the wrapper's own metadata, not an onnxruntime
limitation; newer Python would require the `rapidocr` 3.x package instead.

## Errors

A bad input file produces a status, never an exception: `file_not_found`,
`unsupported_format`, `corrupt_file`, `password_protected`, `empty_document`,
`page_limit_exceeded`, `ocr_engine_unavailable`, `page_render_failed`,
`ocr_failed`. Document status rolls up to `ok`, `partial` (some pages failed) or
`failed`.

These are *extraction* statuses. They are deliberately separate from the
project's verification taxonomy (`verified`, `mismatch_detected`, `expired`,
`verification_pending`, `source_unavailable`, `manual_review_required`), which
belongs to the detectors.

## Use

```python
from app.extraction import extract_document

result = extract_document("certificate.pdf")
for page in result.pages:
    print(page.page_number, page.extraction_method, page.avg_confidence)
```

CLI, from the `backend/` directory:

```
python -m app.extraction <file> [--compact] [--no-text]
```
