"""Certificate field extraction.

The model reads the page text and reports what it finds. Everything after
that is deterministic: schema validation, the snippet check that discards
anything the model cannot point to in the source, date parsing, and the
logical date checks. No judgement about the certificate is made here.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import get, load_config
from ..llm import (
    LLMCallError,
    LLMCompletion,
    LLMUnavailableError,
    get_provider,
    load_prompt,
)
from ..llm.base import LLMProvider
from . import schema
from .dates import parse_date
from .models import (
    AuditRecord,
    CertificateExtractionResult,
    ConsistencyReport,
    FieldExtractionStatus,
    FieldResult,
    InvalidationReason,
)
from .snippets import find_snippet, normalise

CONFIG_NAME = "certificate_extraction"

_REPAIR_INSTRUCTION = """

## Correction required

Your previous response did not match the required schema:

{problems}

Return the corrected JSON object only. No prose, no code fences. Every one of
the nine field keys must be present, and each must be an object with the keys
value, source_snippet, page and confidence.
"""


def extract_certificate_fields(
    pages: dict[int, str] | list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    provider: LLMProvider | None = None,
    source_path: str | None = None,
) -> CertificateExtractionResult:
    """Extract certificate fields from per-page text.

    `pages` is either {page_number: text} or the `pages` list of a step 1
    DocumentResult. `config` and `provider` are injectable for tests.
    """
    cfg = config if config is not None else load_config(CONFIG_NAME)
    page_text = _normalise_pages(pages)

    prompt = load_prompt(
        str(get(cfg, "prompt.name", "certificate_extraction")),
        str(get(cfg, "prompt.version", "v1")),
    )
    document_text = _assemble_document_text(page_text)
    rendered = prompt.render(DOCUMENT_TEXT=document_text)

    if provider is None:
        provider_name = str(get(cfg, "llm.provider", "groq"))
        settings = dict(get(cfg, f"llm.providers.{provider_name}", {}) or {})
        # Carried through for providers that accept sampling parameters.
        # A provider whose API rejects it is free to drop it.
        settings.setdefault("temperature", get(cfg, "llm.temperature", 0))
        try:
            provider = get_provider(provider_name, settings)
        except LLMUnavailableError as exc:
            return _failed_result(
                source_path,
                cfg,
                prompt.prompt_version,
                document_text,
                provider_name,
                "unknown",
                FieldExtractionStatus.PROVIDER_UNAVAILABLE,
                str(exc),
            )

    max_retries = int(get(cfg, "llm.max_retries", 1))
    raw_responses: list[str] = []
    schema_errors: list[list[str]] = []
    payload: dict[str, Any] | None = None
    completion: LLMCompletion | None = None
    attempts = 0
    current_prompt = rendered

    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            completion = provider.complete(current_prompt)
        except LLMUnavailableError as exc:
            return _failed_result(
                source_path,
                cfg,
                prompt.prompt_version,
                document_text,
                getattr(provider, "name", "unknown"),
                getattr(provider, "model", "unknown"),
                FieldExtractionStatus.PROVIDER_UNAVAILABLE,
                str(exc),
                attempts=attempts,
                raw_responses=raw_responses,
                schema_errors=schema_errors,
            )
        except LLMCallError as exc:
            schema_errors.append([f"provider call failed: {exc}"])
            if attempt >= max_retries:
                return _failed_result(
                    source_path,
                    cfg,
                    prompt.prompt_version,
                    document_text,
                    getattr(provider, "name", "unknown"),
                    getattr(provider, "model", "unknown"),
                    FieldExtractionStatus.EXTRACTION_FAILED,
                    str(exc),
                    attempts=attempts,
                    raw_responses=raw_responses,
                    schema_errors=schema_errors,
                )
            continue

        raw_responses.append(completion.text)

        try:
            candidate = schema.parse_response(completion.text)
            problems = schema.validate_payload(candidate)
        except schema.SchemaError as exc:
            problems = [str(exc)]
            candidate = None

        if not problems and candidate is not None:
            payload = candidate
            break

        schema_errors.append(problems)
        current_prompt = rendered + _REPAIR_INSTRUCTION.format(
            problems="\n".join(f"- {problem}" for problem in problems)
        )

    audit = _build_audit(
        cfg=cfg,
        prompt_version=prompt.prompt_version,
        document_text=document_text,
        provider_name=getattr(provider, "name", "unknown"),
        model=completion.model if completion else getattr(provider, "model", "unknown"),
        attempts=attempts,
        raw_responses=raw_responses,
        schema_errors=schema_errors,
        completion=completion,
    )

    if payload is None:
        return CertificateExtractionResult(
            source_path=source_path,
            status=FieldExtractionStatus.EXTRACTION_FAILED,
            missing_fields=list(schema.FIELD_NAMES),
            audit=audit,
            error=(
                "the model did not return a schema-valid response after "
                f"{attempts} attempt(s)"
            ),
        )

    fields = {
        name: _validate_field(name, payload[name], page_text, cfg)
        for name in schema.FIELD_NAMES
    }

    missing = [name for name, result in fields.items() if _is_empty(result.value)]
    rejected = [
        name
        for name, result in fields.items()
        if _is_empty(result.value) and not _is_empty(result.model_value)
    ]

    return CertificateExtractionResult(
        source_path=source_path,
        status=FieldExtractionStatus.OK,
        fields=fields,
        missing_fields=missing,
        rejected_fields=rejected,
        consistency=_check_consistency(fields, cfg),
        audit=audit,
    )


def extract_from_document_result(
    document: dict[str, Any],
    config: dict[str, Any] | None = None,
    provider: LLMProvider | None = None,
) -> CertificateExtractionResult:
    """Run extraction over a step 1 DocumentResult dict."""
    return extract_certificate_fields(
        document.get("pages") or [],
        config=config,
        provider=provider,
        source_path=document.get("source_path"),
    )


# --------------------------------------------------------------------------- #
# Per-field validation
# --------------------------------------------------------------------------- #


def _validate_field(
    name: str,
    entry: dict[str, Any],
    pages: dict[int, str],
    cfg: dict[str, Any],
) -> FieldResult:
    model_value = entry.get("value")
    snippet = entry.get("source_snippet")
    claimed_page = entry.get("page")

    result = FieldResult(
        name=name,
        value=model_value,
        source_snippet=snippet,
        page=claimed_page,
        confidence=entry.get("confidence"),
        validated=False,
        model_value=model_value,
    )

    if _is_empty(model_value):
        # A field the model did not find is a normal outcome, not an error.
        result.value = None
        if model_value is not None:
            result.invalidation_reason = InvalidationReason.EMPTY_VALUE
        return result

    match = find_snippet(snippet, pages, claimed_page, cfg)
    result.snippet_match = match.method
    result.snippet_similarity = match.similarity

    if not match.found:
        # The guard: the model cannot point to this value in the source, so
        # the value does not survive. Both the claimed value and the snippet
        # are kept for review, but `value` is cleared.
        result.value = None
        result.invalidation_reason = {
            "no_snippet": InvalidationReason.SNIPPET_MISSING,
            "skipped": InvalidationReason.SNIPPET_TOO_SHORT,
        }.get(match.method, InvalidationReason.SNIPPET_NOT_IN_SOURCE)
        return result

    if match.page is not None and match.page != claimed_page:
        result.page = match.page
        result.page_corrected = True

    exact_fields = set(get(cfg, "snippet_validation.exact_value_fields", []) or [])
    if name in exact_fields and isinstance(model_value, str):
        # On an identifier, a near miss is the thing we are looking for, not
        # noise to forgive: one altered digit scores ~0.97 against the
        # original line. The value must be present character for character.
        needle = normalise(model_value, cfg)
        haystacks = (
            [pages[result.page]] if result.page in pages else list(pages.values())
        )
        present = any(needle in normalise(text, cfg) for text in haystacks)
        result.value_match = "exact" if present else "absent"
        if not present:
            result.value = None
            result.invalidation_reason = InvalidationReason.VALUE_NOT_IN_SOURCE
            return result

    if name in schema.DATE_FIELDS:
        parsed = parse_date(model_value, cfg)
        result.value_iso = parsed.iso
        result.date_note = parsed.note
        result.date_ambiguous = parsed.ambiguous
        if not parsed.parsed:
            # The raw string is kept, as instructed, and flagged. The value is
            # real text from the document; only our normalisation failed.
            result.invalidation_reason = InvalidationReason.DATE_UNPARSED
            return result

    result.validated = True
    return result


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not value
    return False


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #


def _check_consistency(
    fields: dict[str, FieldResult], cfg: dict[str, Any]
) -> ConsistencyReport:
    as_of_raw = get(cfg, "consistency.as_of")
    as_of = _resolve_as_of(as_of_raw)
    report = ConsistencyReport(as_of=as_of.isoformat())

    valid_from = _iso_date(fields.get("valid_from"))
    valid_to = _iso_date(fields.get("valid_to"))
    issue_date = _iso_date(fields.get("issue_date"))

    if valid_from is not None and valid_to is not None:
        report.valid_from_before_valid_to = valid_from < valid_to
        if valid_from > valid_to:
            report.notes.append(
                f"valid_from ({valid_from.isoformat()}) is after valid_to "
                f"({valid_to.isoformat()})"
            )
        elif valid_from == valid_to:
            report.notes.append("valid_from and valid_to are the same date")
    else:
        report.notes.append(
            "validity window could not be checked: "
            f"valid_from={'present' if valid_from else 'absent'}, "
            f"valid_to={'present' if valid_to else 'absent'}"
        )

    if valid_to is not None:
        report.expired = valid_to < as_of
        report.days_until_expiry = (valid_to - as_of).days
        if report.expired:
            report.notes.append(
                f"valid_to ({valid_to.isoformat()}) is before the as-of date "
                f"({as_of.isoformat()})"
            )

    if issue_date is not None and valid_from is not None:
        report.issue_date_after_valid_from = issue_date > valid_from
        if issue_date > valid_from:
            report.notes.append(
                f"issue_date ({issue_date.isoformat()}) is after valid_from "
                f"({valid_from.isoformat()})"
            )

    return report


def _resolve_as_of(raw: Any) -> date:
    if raw is None:
        return datetime.now(timezone.utc).date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


def _iso_date(result: FieldResult | None) -> date | None:
    if result is None or not result.value_iso:
        return None
    try:
        return date.fromisoformat(result.value_iso)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Input and audit
# --------------------------------------------------------------------------- #


def _normalise_pages(
    pages: dict[int, str] | list[dict[str, Any]],
) -> dict[int, str]:
    """Accept either a {page: text} mapping or step 1's pages list."""
    if isinstance(pages, dict):
        return {int(number): (text or "") for number, text in pages.items()}

    result: dict[int, str] = {}
    for index, page in enumerate(pages, start=1):
        if isinstance(page, dict):
            number = int(page.get("page_number", index))
            result[number] = page.get("text") or ""
        else:  # a step 1 PageResult dataclass
            number = int(getattr(page, "page_number", index))
            result[number] = getattr(page, "text", "") or ""
    return result


def _assemble_document_text(pages: dict[int, str]) -> str:
    """Build the prompt's document section, with page markers.

    The markers are what lets the model report a page number, and what the
    audit hash is taken over.
    """
    return "\n\n".join(
        f"[page {number}]\n{pages[number]}" for number in sorted(pages)
    )


def _build_audit(
    cfg: dict[str, Any],
    prompt_version: str,
    document_text: str,
    provider_name: str,
    model: str,
    attempts: int,
    raw_responses: list[str],
    schema_errors: list[list[str]],
    completion: LLMCompletion | None = None,
) -> AuditRecord:
    include_raw = bool(get(cfg, "output.include_raw_responses", True))
    return AuditRecord(
        provider=provider_name,
        model=model,
        prompt_version=prompt_version,
        config_version=cfg.get("config_version"),
        input_text_sha256=hashlib.sha256(document_text.encode("utf-8")).hexdigest(),
        input_text_chars=len(document_text),
        timestamp=datetime.now(timezone.utc).isoformat(),
        retries_used=max(0, attempts - 1),
        attempts=attempts,
        schema_errors=schema_errors,
        raw_responses=list(raw_responses) if include_raw else [],
        input_tokens=completion.input_tokens if completion else None,
        output_tokens=completion.output_tokens if completion else None,
        cache_hit=bool(
            (completion.metadata or {}).get("cache_hit") if completion else False
        ),
    )


def _failed_result(
    source_path: str | None,
    cfg: dict[str, Any],
    prompt_version: str,
    document_text: str,
    provider_name: str,
    model: str,
    status: FieldExtractionStatus,
    error: str,
    attempts: int = 0,
    raw_responses: list[str] | None = None,
    schema_errors: list[list[str]] | None = None,
) -> CertificateExtractionResult:
    return CertificateExtractionResult(
        source_path=source_path,
        status=status,
        missing_fields=list(schema.FIELD_NAMES),
        audit=_build_audit(
            cfg=cfg,
            prompt_version=prompt_version,
            document_text=document_text,
            provider_name=provider_name,
            model=model,
            attempts=attempts,
            raw_responses=raw_responses or [],
            schema_errors=schema_errors or [],
        ),
        error=error,
    )
