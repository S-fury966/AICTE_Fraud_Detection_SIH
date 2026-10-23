"""Tests for certificate field extraction.

Everything fast runs against the stub provider, so the deterministic layer is
asserted without a network call: the schema contract, the snippet guard, date
parsing, and the date consistency report. The real API test carries the `llm`
marker and skips itself unless a key is present:

    pytest -m "not ocr and not llm"
"""

from __future__ import annotations

import copy
import json
import pytest

from app.certificates import extract_certificate_fields
from app.certificates.dates import parse_date
from app.certificates.models import FieldExtractionStatus, InvalidationReason
from app.certificates.schema import FIELD_NAMES
from app.llm import StubProvider
from tests.conftest import live_providers

# A synthetic certificate page. Fictional institution, fictional number.
CERTIFICATE_PAGE = """
NATIONAL BOARD OF TECHNICAL ACCREDITATION (DEMO)
Certificate of Accreditation

Institution: Shantiniketan Institute of Technology
Certificate Number: NBTA/DEMO/2024/00417
Issuing Body: National Board of Technical Accreditation (Demo)
Programmes Accredited:
  1. Bachelor of Technology, Computer Science and Engineering
  2. Bachelor of Technology, Electronics and Communication Engineering
Accreditation Grade: A+
Date of Issue: 12 March 2024
Valid From: 01/07/2024
Valid Until: 30 June 2027

Signed,
Dr. Ananya Mukherjee
Member Secretary

This is synthetic demo data generated for testing. It accredits nothing.
"""

# The same certificate with the issuing body line removed, to prove the model
# is not allowed to fill it in from general knowledge.
PAGE_WITHOUT_ISSUER = CERTIFICATE_PAGE.replace(
    "Issuing Body: National Board of Technical Accreditation (Demo)\n", ""
).replace("NATIONAL BOARD OF TECHNICAL ACCREDITATION (DEMO)\n", "")


def _entry(value, snippet, page=1, confidence="high"):
    return {
        "value": value,
        "source_snippet": snippet,
        "page": page,
        "confidence": confidence,
    }


def clean_payload() -> dict:
    """A well-formed response whose snippets are all really in the page."""
    return {
        "institution_name": _entry(
            "Shantiniketan Institute of Technology",
            "Institution: Shantiniketan Institute of Technology",
        ),
        "certificate_number": _entry(
            "NBTA/DEMO/2024/00417",
            "Certificate Number: NBTA/DEMO/2024/00417",
        ),
        "issuing_body": _entry(
            "National Board of Technical Accreditation (Demo)",
            "Issuing Body: National Board of Technical Accreditation (Demo)",
        ),
        "programme": _entry(
            [
                "Bachelor of Technology, Computer Science and Engineering",
                "Bachelor of Technology, Electronics and Communication Engineering",
            ],
            "1. Bachelor of Technology, Computer Science and Engineering",
            confidence="medium",
        ),
        "accreditation_status_or_grade": _entry("A+", "Accreditation Grade: A+"),
        "issue_date": _entry("12 March 2024", "Date of Issue: 12 March 2024"),
        "valid_from": _entry("01/07/2024", "Valid From: 01/07/2024"),
        "valid_to": _entry("30 June 2027", "Valid Until: 30 June 2027"),
        "signatory_name": _entry(
            "Dr. Ananya Mukherjee", "Dr. Ananya Mukherjee", confidence="medium"
        ),
    }


@pytest.fixture
def cfg() -> dict:
    """The shipped config, with the as-of date pinned for reproducibility."""
    from app.core.config import load_config

    config = copy.deepcopy(load_config("certificate_extraction"))
    config["consistency"]["as_of"] = "2026-01-15"
    return config


def run(payload_or_text, cfg, pages=None, max_retries=None):
    """Run extraction with a stub returning the given response(s)."""
    if isinstance(payload_or_text, (dict, list)) and isinstance(payload_or_text, dict):
        responses = [json.dumps(payload_or_text)]
    elif isinstance(payload_or_text, list):
        responses = [
            item if isinstance(item, str) else json.dumps(item)
            for item in payload_or_text
        ]
    else:
        responses = [payload_or_text]

    if max_retries is not None:
        cfg = copy.deepcopy(cfg)
        cfg["llm"]["max_retries"] = max_retries

    provider = StubProvider(responses=responses)
    result = extract_certificate_fields(
        pages if pages is not None else {1: CERTIFICATE_PAGE},
        config=cfg,
        provider=provider,
    )
    return result, provider


# --------------------------------------------------------------------------- #
# Clean extraction
# --------------------------------------------------------------------------- #


def test_clean_extraction(cfg):
    result, provider = run(clean_payload(), cfg)

    assert result.status is FieldExtractionStatus.OK
    assert provider.calls == 1
    assert result.missing_fields == []
    assert result.rejected_fields == []

    fields = result.fields
    assert set(fields) == set(FIELD_NAMES)
    assert all(field.validated for field in fields.values())

    assert fields["institution_name"].value == "Shantiniketan Institute of Technology"
    assert fields["certificate_number"].value == "NBTA/DEMO/2024/00417"
    assert fields["accreditation_status_or_grade"].value == "A+"
    assert fields["signatory_name"].value == "Dr. Ananya Mukherjee"
    assert len(fields["programme"].value) == 2
    assert fields["institution_name"].snippet_match == "exact"
    # The model's own confidence is carried through untouched.
    assert fields["programme"].confidence == "medium"


def test_dates_are_parsed_to_iso(cfg):
    result, _ = run(clean_payload(), cfg)

    assert result.fields["issue_date"].value_iso == "2024-03-12"
    # Day-first: 01/07/2024 is 1 July, not 7 January.
    assert result.fields["valid_from"].value_iso == "2024-07-01"
    assert result.fields["valid_to"].value_iso == "2027-06-30"
    # The raw printed string is preserved alongside the ISO form.
    assert result.fields["valid_from"].value == "01/07/2024"


def test_consistency_report_on_a_valid_certificate(cfg):
    result, _ = run(clean_payload(), cfg)

    consistency = result.consistency
    assert consistency.as_of == "2026-01-15"
    assert consistency.valid_from_before_valid_to is True
    assert consistency.expired is False
    assert consistency.days_until_expiry > 0
    assert consistency.issue_date_after_valid_from is False


def test_audit_block_is_populated(cfg):
    result, _ = run(clean_payload(), cfg)

    audit = result.audit
    assert audit.provider == "stub"
    assert audit.prompt_version == "certificate_extraction.v1"
    assert audit.config_version == cfg["config_version"]
    assert len(audit.input_text_sha256) == 64
    assert audit.retries_used == 0
    assert audit.attempts == 1
    assert audit.timestamp.endswith("+00:00")
    # The raw response is kept, separate from the validated fields.
    assert len(audit.raw_responses) == 1
    assert "NBTA/DEMO/2024/00417" in audit.raw_responses[0]


def test_prompt_contains_the_document_text_with_page_markers(cfg):
    _, provider = run(clean_payload(), cfg, pages={1: "page one", 2: "page two"})

    prompt = provider.prompts[0]
    assert "[page 1]\npage one" in prompt
    assert "[page 2]\npage two" in prompt
    # The prompt must forbid filling the issuer from general knowledge.
    assert "own knowledge" in prompt


# --------------------------------------------------------------------------- #
# The snippet guard
# --------------------------------------------------------------------------- #


def test_snippet_not_in_source_is_nulled(cfg):
    """The hallucination guard: a value the model cannot point to is discarded."""
    payload = clean_payload()
    payload["signatory_name"] = _entry(
        "Dr. Rajesh Verma", "Signed, Dr. Rajesh Verma, Chairman"
    )

    result, _ = run(payload, cfg)

    field = result.fields["signatory_name"]
    assert field.value is None
    assert field.validated is False
    assert field.invalidation_reason is InvalidationReason.SNIPPET_NOT_IN_SOURCE
    # What the model claimed is retained for the reviewer.
    assert field.model_value == "Dr. Rajesh Verma"
    assert field.source_snippet == "Signed, Dr. Rajesh Verma, Chairman"
    assert "signatory_name" in result.missing_fields
    assert "signatory_name" in result.rejected_fields
    # Other fields are unaffected.
    assert result.fields["institution_name"].validated is True


def test_invented_certificate_number_is_nulled(cfg):
    payload = clean_payload()
    payload["certificate_number"] = _entry(
        "NBTA/DEMO/2024/99999", "Certificate Number: NBTA/DEMO/2024/99999"
    )

    result, _ = run(payload, cfg)

    assert result.fields["certificate_number"].value is None
    assert (
        result.fields["certificate_number"].invalidation_reason
        is InvalidationReason.SNIPPET_NOT_IN_SOURCE
    )


def test_snippet_tolerates_whitespace_and_case(cfg):
    payload = clean_payload()
    payload["institution_name"] = _entry(
        "Shantiniketan Institute of Technology",
        "institution:    SHANTINIKETAN   institute of technology",
    )

    result, _ = run(payload, cfg)

    field = result.fields["institution_name"]
    assert field.validated is True
    assert field.snippet_match == "exact"


def test_value_without_a_snippet_is_nulled(cfg):
    payload = clean_payload()
    payload["issuing_body"] = _entry(
        "National Board of Technical Accreditation (Demo)", None
    )

    result, _ = run(payload, cfg)

    field = result.fields["issuing_body"]
    assert field.value is None
    assert field.invalidation_reason is InvalidationReason.SNIPPET_MISSING


def test_wrong_page_number_is_corrected_not_discarded(cfg):
    result, _ = run(
        clean_payload(),
        cfg,
        pages={1: "Annexure cover sheet, no fields here.", 2: CERTIFICATE_PAGE},
    )

    field = result.fields["certificate_number"]
    assert field.validated is True
    assert field.page == 2
    assert field.page_corrected is True


def test_strict_similarity_threshold_rejects_a_near_miss(cfg):
    """The tolerance is config, so tightening it changes the outcome."""
    payload = clean_payload()
    payload["accreditation_status_or_grade"] = _entry(
        "A+", "Accreditation Grading: A+"  # "Grading", not "Grade"
    )

    loose = copy.deepcopy(cfg)
    loose["snippet_validation"]["min_similarity"] = 0.80
    result_loose, _ = run(payload, loose)
    assert result_loose.fields["accreditation_status_or_grade"].validated is True
    assert result_loose.fields["accreditation_status_or_grade"].snippet_match == "fuzzy"

    strict = copy.deepcopy(cfg)
    strict["snippet_validation"]["min_similarity"] = 0.99
    result_strict, _ = run(payload, strict)
    assert result_strict.fields["accreditation_status_or_grade"].value is None


# --------------------------------------------------------------------------- #
# Missing fields are a normal result
# --------------------------------------------------------------------------- #


def test_missing_issuing_body_returns_null_not_a_guess(cfg):
    """With the issuer absent from the text, the field must come back null.

    The stub returns exactly what a compliant model should return for this
    page. The assertion is that a null field is a clean, successful result
    which later steps can see and fall back from, for example to the QR
    payload.
    """
    payload = clean_payload()
    payload["issuing_body"] = _entry(None, None, page=None, confidence=None)

    result, _ = run(payload, cfg, pages={1: PAGE_WITHOUT_ISSUER})

    assert result.status is FieldExtractionStatus.OK
    field = result.fields["issuing_body"]
    assert field.value is None
    assert field.validated is False
    # Not a rejection: the model correctly reported nothing.
    assert field.invalidation_reason is None
    assert "issuing_body" in result.missing_fields
    assert "issuing_body" not in result.rejected_fields


def test_issuer_filled_from_general_knowledge_is_caught(cfg):
    """If the model does fill the issuer in anyway, the guard catches it."""
    payload = clean_payload()
    payload["issuing_body"] = _entry(
        "National Board of Accreditation",
        "Issuing Body: National Board of Accreditation",
    )

    result, _ = run(payload, cfg, pages={1: PAGE_WITHOUT_ISSUER})

    field = result.fields["issuing_body"]
    assert field.value is None
    assert field.invalidation_reason is InvalidationReason.SNIPPET_NOT_IN_SOURCE
    assert "issuing_body" in result.rejected_fields


def test_empty_programme_list_counts_as_missing(cfg):
    payload = clean_payload()
    payload["programme"] = _entry([], None, page=None, confidence=None)

    result, _ = run(payload, cfg)

    assert result.fields["programme"].value is None
    assert "programme" in result.missing_fields


# --------------------------------------------------------------------------- #
# Schema failures and retries
# --------------------------------------------------------------------------- #


def test_malformed_json_then_successful_retry(cfg):
    result, provider = run(
        ["this is not JSON at all", json.dumps(clean_payload())], cfg
    )

    assert result.status is FieldExtractionStatus.OK
    assert provider.calls == 2
    assert result.audit.retries_used == 1
    assert result.audit.attempts == 2
    # Both responses are kept, and the schema error that triggered the retry.
    assert len(result.audit.raw_responses) == 2
    assert len(result.audit.schema_errors) == 1
    assert result.fields["certificate_number"].value == "NBTA/DEMO/2024/00417"
    # The retry prompt carries the correction.
    assert "Correction required" in provider.prompts[1]


def test_malformed_json_twice_is_extraction_failed(cfg):
    result, provider = run(["not JSON", "still not JSON"], cfg)

    assert result.status is FieldExtractionStatus.EXTRACTION_FAILED
    assert provider.calls == 2
    assert result.fields == {}
    assert result.missing_fields == list(FIELD_NAMES)
    assert result.audit.retries_used == 1
    assert len(result.audit.schema_errors) == 2
    assert "schema-valid" in result.error


def test_missing_field_key_is_a_schema_error(cfg):
    payload = clean_payload()
    del payload["valid_to"]

    result, provider = run([json.dumps(payload), json.dumps(clean_payload())], cfg)

    assert result.status is FieldExtractionStatus.OK
    assert provider.calls == 2
    assert "missing field keys: valid_to" in result.audit.schema_errors[0][0]


def test_wrong_value_type_is_a_schema_error(cfg):
    payload = clean_payload()
    payload["certificate_number"]["value"] = 417  # must be a string

    result, _ = run([json.dumps(payload), json.dumps(payload)], cfg)

    assert result.status is FieldExtractionStatus.EXTRACTION_FAILED
    assert any(
        "must be a string" in problem
        for problems in result.audit.schema_errors
        for problem in problems
    )


def test_bad_confidence_value_is_a_schema_error(cfg):
    payload = clean_payload()
    payload["institution_name"]["confidence"] = "very high"

    result, _ = run([json.dumps(payload), json.dumps(payload)], cfg)

    assert result.status is FieldExtractionStatus.EXTRACTION_FAILED
    assert any(
        "confidence must be one of" in problem
        for problems in result.audit.schema_errors
        for problem in problems
    )


def test_code_fence_is_tolerated_without_a_retry(cfg):
    fenced = "```json\n" + json.dumps(clean_payload()) + "\n```"

    result, provider = run(fenced, cfg)

    assert result.status is FieldExtractionStatus.OK
    assert provider.calls == 1


def test_retries_are_configurable(cfg):
    """max_retries 0 means one attempt and no repair prompt."""
    result, provider = run(["not JSON"], cfg, max_retries=0)

    assert result.status is FieldExtractionStatus.EXTRACTION_FAILED
    assert provider.calls == 1
    assert result.audit.retries_used == 0


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12/03/2025", "2025-03-12"),
        ("12-03-2025", "2025-03-12"),
        ("12.03.2025", "2025-03-12"),
        ("1/7/2024", "2024-07-01"),
        ("2025-03-12", "2025-03-12"),
        ("12 March 2025", "2025-03-12"),
        ("12 Mar 2025", "2025-03-12"),
        ("12th March 2025", "2025-03-12"),
        ("March 12, 2025", "2025-03-12"),
        ("30 June 2027", "2027-06-30"),
        ("  12  March   2025  ", "2025-03-12"),
        ("31/12/2024", "2024-12-31"),
    ],
)
def test_date_formats(raw, expected):
    parsed = parse_date(raw, {"dates": {"day_first": True}})
    assert parsed.parsed is True
    assert parsed.iso == expected
    assert parsed.raw == raw


def test_day_first_is_configurable():
    day_first = parse_date("03/04/2025", {"dates": {"day_first": True}})
    month_first = parse_date("03/04/2025", {"dates": {"day_first": False}})

    assert day_first.iso == "2025-04-03"
    assert month_first.iso == "2025-03-04"
    # Both record that the order could not be verified from the value.
    assert day_first.ambiguous is True
    assert day_first.note == "ambiguous_day_month_assumed_day_first"


def test_unambiguous_numeric_date_is_not_flagged():
    parsed = parse_date("30/06/2027", {"dates": {"day_first": True}})
    assert parsed.ambiguous is False
    assert parsed.note is None


@pytest.mark.parametrize(
    "raw,note",
    [
        ("not a date", "unrecognised_format"),
        ("31/02/2025", None),  # impossible day, rejected by the calendar
        ("March 2025", "month_and_year_only_no_day"),
        ("12/13/2025", None),  # month 13 under day-first
    ],
)
def test_unparseable_dates_keep_the_raw_string(raw, note):
    parsed = parse_date(raw, {"dates": {"day_first": True}})
    assert parsed.parsed is False
    assert parsed.iso is None
    assert parsed.raw == raw
    if note:
        assert parsed.note == note


def test_unparseable_date_in_a_field_is_flagged_and_kept(cfg):
    page = CERTIFICATE_PAGE.replace("Valid Until: 30 June 2027", "Valid Until: sometime in 2027")
    payload = clean_payload()
    payload["valid_to"] = _entry("sometime in 2027", "Valid Until: sometime in 2027")

    result, _ = run(payload, cfg, pages={1: page})

    field = result.fields["valid_to"]
    # The printed text is real, so the value survives; only normalisation failed.
    assert field.value == "sometime in 2027"
    assert field.value_iso is None
    assert field.validated is False
    assert field.invalidation_reason is InvalidationReason.DATE_UNPARSED
    assert field.date_note == "unrecognised_format"
    # A date that could not be parsed cannot be checked for expiry.
    assert result.consistency.expired is None


def test_out_of_range_year_is_rejected():
    parsed = parse_date("12 March 1823", {"dates": {"min_year": 1900, "max_year": 2100}})
    assert parsed.parsed is False
    assert parsed.note == "out_of_range"


# --------------------------------------------------------------------------- #
# Consistency: expired and inconsistent
# --------------------------------------------------------------------------- #


def test_expired_certificate_is_reported(cfg):
    page = CERTIFICATE_PAGE.replace("Valid Until: 30 June 2027", "Valid Until: 30 June 2020")
    payload = clean_payload()
    payload["valid_to"] = _entry("30 June 2020", "Valid Until: 30 June 2020")

    result, _ = run(payload, cfg, pages={1: page})

    consistency = result.consistency
    assert consistency.expired is True
    assert consistency.days_until_expiry < 0
    assert any("before the as-of date" in note for note in consistency.notes)
    # Reporting only: the field itself is still a valid extraction.
    assert result.fields["valid_to"].validated is True
    assert result.status is FieldExtractionStatus.OK


def test_valid_from_after_valid_to_is_reported(cfg):
    page = CERTIFICATE_PAGE.replace("Valid From: 01/07/2024", "Valid From: 01/07/2028")
    payload = clean_payload()
    payload["valid_from"] = _entry("01/07/2028", "Valid From: 01/07/2028")

    result, _ = run(payload, cfg, pages={1: page})

    consistency = result.consistency
    assert consistency.valid_from_before_valid_to is False
    assert any("is after valid_to" in note for note in consistency.notes)


def test_issue_date_after_valid_from_is_reported(cfg):
    page = CERTIFICATE_PAGE.replace("Date of Issue: 12 March 2024", "Date of Issue: 12 March 2026")
    payload = clean_payload()
    payload["issue_date"] = _entry("12 March 2026", "Date of Issue: 12 March 2026")

    result, _ = run(payload, cfg, pages={1: page})

    assert result.consistency.issue_date_after_valid_from is True
    assert any("is after valid_from" in note for note in result.consistency.notes)


def test_as_of_date_is_configurable(cfg):
    """The same certificate is expired or not depending on the as-of date."""
    before = copy.deepcopy(cfg)
    before["consistency"]["as_of"] = "2026-01-15"
    after = copy.deepcopy(cfg)
    after["consistency"]["as_of"] = "2028-01-15"

    result_before, _ = run(clean_payload(), before)
    result_after, _ = run(clean_payload(), after)

    assert result_before.consistency.expired is False
    assert result_after.consistency.expired is True


def test_missing_validity_window_is_noted_not_assumed(cfg):
    payload = clean_payload()
    payload["valid_to"] = _entry(None, None, page=None, confidence=None)

    result, _ = run(payload, cfg)

    consistency = result.consistency
    assert consistency.valid_from_before_valid_to is None
    assert consistency.expired is None
    assert any("could not be checked" in note for note in consistency.notes)


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def test_result_serialises_to_json(cfg):
    result, _ = run(clean_payload(), cfg)
    payload = json.loads(json.dumps(result.to_dict()))

    assert payload["status"] == "ok"
    field = payload["fields"]["certificate_number"]
    assert field["value"] == "NBTA/DEMO/2024/00417"
    assert field["validated"] is True
    assert field["invalidation_reason"] is None
    assert payload["audit"]["prompt_version"] == "certificate_extraction.v1"


def test_raw_responses_can_be_withheld(cfg):
    config = copy.deepcopy(cfg)
    config["output"]["include_raw_responses"] = False

    result, _ = run(clean_payload(), config)

    assert result.audit.raw_responses == []


# --------------------------------------------------------------------------- #
# The real API
# --------------------------------------------------------------------------- #
#
# These run against the configured provider (llm.provider), skipping only when
# its key is absent from .env. A key that IS present but rejected is a failure,
# not a skip: silently passing over a broken credential would hide it until the
# demo. SIH_LIVE_PROVIDERS=groq,mistral checks other providers too.


def _live_config(provider: str) -> dict:
    from app.core.config import load_config

    config = copy.deepcopy(load_config("certificate_extraction"))
    config["llm"]["provider"] = provider
    config["consistency"]["as_of"] = "2026-01-15"
    return config


@pytest.fixture(params=live_providers() or [None])
def live_provider(request):
    if request.param is None:
        pytest.skip("no LLM API key configured in .env")
    return request.param


@pytest.mark.llm
def test_real_api_extraction(live_provider):
    """End-to-end against a live model. Costs money; opt in with the marker."""
    result = extract_certificate_fields(
        {1: CERTIFICATE_PAGE}, config=_live_config(live_provider)
    )

    assert result.status is FieldExtractionStatus.OK, (
        f"{live_provider} extraction failed: {result.error}"
    )
    assert result.audit.provider == live_provider

    fields = result.fields
    assert fields["certificate_number"].value == "NBTA/DEMO/2024/00417"
    assert fields["certificate_number"].validated is True
    assert "Shantiniketan" in (fields["institution_name"].value or "")
    assert fields["valid_to"].value_iso == "2027-06-30"
    assert result.consistency.expired is False


@pytest.mark.llm
def test_real_api_returns_null_for_a_missing_issuer(live_provider):
    """The prompt's hardest rule: no filling the issuer from world knowledge."""
    result = extract_certificate_fields(
        {1: PAGE_WITHOUT_ISSUER}, config=_live_config(live_provider)
    )

    assert result.status is FieldExtractionStatus.OK, (
        f"{live_provider} extraction failed: {result.error}"
    )
    # Either the model returned null, or it invented something and the
    # snippet guard caught it. Both end with no issuing body.
    assert result.fields["issuing_body"].value is None
    assert "issuing_body" in result.missing_fields


@pytest.mark.llm
def test_real_api_output_survives_the_exact_value_rule(live_provider):
    """Every identifier a live model returns must be verbatim in the source.

    A real model cannot be made to alter a digit on demand, so this asserts
    the invariant instead: whatever value survives on an exact-value field was
    found character for character in the page text.
    """
    result = extract_certificate_fields(
        {1: CERTIFICATE_PAGE}, config=_live_config(live_provider)
    )

    assert result.status is FieldExtractionStatus.OK, (
        f"{live_provider} extraction failed: {result.error}"
    )
    from app.core.config import load_config

    exact_fields = load_config("certificate_extraction")["snippet_validation"][
        "exact_value_fields"
    ]
    for name in exact_fields:
        field = result.fields[name]
        if field.value is not None:
            assert field.value_match == "exact", f"{name} kept a non-verbatim value"
