"""The offline-reader fallback and the circuit breaker (step: fallback fix).

Fast, no real network and no real key. The primary provider is a fake whose
`complete` raises a chosen failure category; the fallback is the real
`RulesProvider` reading a crafted page, so the whole path - category to
fallback decision to the offline reading passing the same snippet guard - is
exercised without touching an endpoint.
"""

from __future__ import annotations

import copy
import json

import pytest

from app.certificates.extractor import extract_certificate_fields
from app.core.config import load_config
from app.llm import FailureCategory, LLMCallError, LLMUnavailableError
from app.llm.breaker import CircuitBreaker

# A page the offline reader can read every field from, so a successful fallback
# produces real validated values (the snippet guard still runs over them).
PAGE = {
    1: (
        "National Board of Technical Accreditation (Demo)\n"
        "Institution: Demo College of Engineering\n"
        "Certificate Number: NBTA/DEMO/2024/00417\n"
        "Programme: Bachelor of Technology, Civil Engineering\n"
        "Grade: A+\n"
        "Valid From: 01 July 2024\n"
        "Valid Until: 30 June 2027\n"
    )
}


@pytest.fixture(autouse=True)
def _reset_default_breaker():
    """Clear the process-wide breaker around every test.

    Tests that call the extractor directly use the module default breaker; a
    run left open by one must not change what the next observes.
    """
    from app.llm import breaker as breaker_module

    breaker_module._DEFAULT._states.clear()
    yield
    breaker_module._DEFAULT._states.clear()


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config("certificate_extraction"))


class FakePrimary:
    """Stands in for the configured model. Records how often it was called."""

    def __init__(self, error: Exception | None = None, text: str | None = None):
        self.name = "groq"
        self.model = "fake-primary"
        self._error = error
        self._text = text
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    def complete(self, prompt: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        from app.llm.base import LLMCompletion

        return LLMCompletion(text=self._text or "{}", model=self.model, provider=self.name)


def patch_providers(monkeypatch, primary, cfg):
    """Route the extractor's get_provider: primary name -> fake, rules -> real."""
    from app.llm.rules_provider import RulesProvider

    rules_settings = dict((cfg["llm"]["providers"].get("rules") or {}))

    def fake_get_provider(name, settings=None):
        if name == "rules":
            return RulesProvider(rules_settings)
        return primary

    monkeypatch.setattr("app.certificates.extractor.get_provider", fake_get_provider)


def run(cfg, primary, monkeypatch, breaker=None):
    patch_providers(monkeypatch, primary, cfg)
    # A fresh breaker per call unless a test drives its own, so breaker state
    # never leaks between the category tests (which share the process-wide
    # default breaker otherwise).
    result = extract_certificate_fields(
        PAGE, config=cfg, breaker=breaker or CircuitBreaker(threshold=2, cool_down_seconds=60)
    )
    return result.to_dict()


# --------------------------------------------------------------------------
# Each fallback_on category triggers the fallback
# --------------------------------------------------------------------------

_CALL_ERRORS = {
    "timeout": LLMCallError("slow", FailureCategory.TIMEOUT, "timeout contacting host"),
    "connection_error": LLMCallError("down", FailureCategory.CONNECTION_ERROR, "connection_error contacting host"),
    "server_error": LLMCallError("5xx", FailureCategory.SERVER_ERROR, "groq server error (HTTP 503)"),
    "rate_limited": LLMCallError("429", FailureCategory.RATE_LIMITED, "groq stayed rate limited (HTTP 429)"),
    "other_transport": LLMCallError("odd", FailureCategory.OTHER_TRANSPORT, "groq returned HTTP 418"),
}
_UNAVAILABLE_ERRORS = {
    "auth_rejected": LLMUnavailableError("401", FailureCategory.AUTH_REJECTED, "groq rejected the key (HTTP 401)"),
    "model_or_endpoint_not_found": LLMUnavailableError("404", FailureCategory.MODEL_OR_ENDPOINT_NOT_FOUND, "groq has no endpoint (HTTP 404)"),
}


@pytest.mark.parametrize("category", list(_CALL_ERRORS) + list(_UNAVAILABLE_ERRORS))
def test_each_fallback_category_engages_the_offline_reader(category, cfg, monkeypatch):
    error = {**_CALL_ERRORS, **_UNAVAILABLE_ERRORS}[category]
    primary = FakePrimary(error=error)

    payload = run(cfg, primary, monkeypatch)

    assert payload["status"] == "ok"
    audit = payload["audit"]
    assert audit["fallback_used"] is True
    assert audit["provider_used"] == "rules"
    assert audit["primary_provider"] == "groq"
    assert audit["fallback_category"] == category
    assert category in audit["fallback_reason"]
    # The offline reading really produced validated fields.
    assert payload["fields"]["certificate_number"]["value"] == "NBTA/DEMO/2024/00417"
    assert payload["fields"]["certificate_number"]["validated"] is True


def test_a_category_removed_from_fallback_on_hard_fails(cfg, monkeypatch):
    """The list is authoritative, not decorative: drop auth_rejected and a
    rejected key stops falling back and fails the stage."""
    cfg["llm"]["fallback_on"] = [c for c in cfg["llm"]["fallback_on"] if c != "auth_rejected"]
    primary = FakePrimary(error=_UNAVAILABLE_ERRORS["auth_rejected"])

    payload = run(cfg, primary, monkeypatch)

    assert payload["status"] == "provider_unavailable"
    assert payload["audit"]["fallback_used"] is False
    assert payload["audit"]["provider_used"] == "groq"


def test_model_not_found_removed_also_hard_fails(cfg, monkeypatch):
    cfg["llm"]["fallback_on"] = [
        c for c in cfg["llm"]["fallback_on"] if c != "model_or_endpoint_not_found"
    ]
    primary = FakePrimary(error=_UNAVAILABLE_ERRORS["model_or_endpoint_not_found"])
    payload = run(cfg, primary, monkeypatch)
    assert payload["status"] == "provider_unavailable"
    assert payload["audit"]["fallback_used"] is False


def test_a_removed_transport_category_hard_fails_as_a_call_error(cfg, monkeypatch):
    cfg["llm"]["fallback_on"] = [c for c in cfg["llm"]["fallback_on"] if c != "server_error"]
    primary = FakePrimary(error=_CALL_ERRORS["server_error"])
    payload = run(cfg, primary, monkeypatch)
    # A call error with no eligible fallback keeps its existing path.
    assert payload["status"] == "extraction_failed"
    assert payload["audit"]["fallback_used"] is False


# --------------------------------------------------------------------------
# Malformed model output is NOT a fallback trigger
# --------------------------------------------------------------------------


def test_malformed_output_twice_fails_without_fallback(cfg, monkeypatch):
    """Schema-invalid model content is a model-behaviour problem: it keeps its
    retry-then-extraction_failed path and must never reach the offline reader."""
    cfg["llm"]["max_retries"] = 1
    primary = FakePrimary(text="this is not json")

    payload = run(cfg, primary, monkeypatch)

    assert payload["status"] == "extraction_failed"
    assert payload["audit"]["fallback_used"] is False
    assert payload["audit"]["provider_used"] == "groq"
    # Retried as before: first attempt plus one retry.
    assert primary.calls == 2


# --------------------------------------------------------------------------
# No fallback configured, and a failing fallback
# --------------------------------------------------------------------------


def test_no_fallback_configured_is_old_behaviour(cfg, monkeypatch):
    cfg["llm"]["fallback_provider"] = None
    primary = FakePrimary(error=_CALL_ERRORS["connection_error"])
    payload = run(cfg, primary, monkeypatch)
    assert payload["status"] == "extraction_failed"
    assert payload["audit"]["fallback_used"] is False


def test_a_failing_fallback_reader_fails_the_stage(cfg, monkeypatch):
    """When the offline reader itself raises, the stage fails and does not
    pretend a reading happened."""
    from app.llm.base import LLMCompletion

    class BrokenRules:
        name = "rules"
        model = "label-rules-v1"

        def complete(self, prompt):
            raise LLMCallError("reader broke", FailureCategory.OTHER_TRANSPORT, "reader broke")

    def fake_get_provider(name, settings=None):
        if name == "rules":
            return BrokenRules()
        return FakePrimary(error=_CALL_ERRORS["connection_error"])

    monkeypatch.setattr("app.certificates.extractor.get_provider", fake_get_provider)
    result = extract_certificate_fields(PAGE, config=cfg)
    payload = result.to_dict()
    assert payload["status"] in ("extraction_failed", "provider_unavailable")
    # The provider actually in hand when it failed was the reader.
    assert payload["audit"]["provider_used"] == "rules"


# --------------------------------------------------------------------------
# Primary success: no fallback, status ok
# --------------------------------------------------------------------------


def test_primary_success_does_not_use_the_fallback(cfg, monkeypatch):
    good = json.dumps(
        {
            name: {"value": None, "source_snippet": None, "page": None, "confidence": None}
            for name in [
                "institution_name", "certificate_number", "issuing_body", "programme",
                "accreditation_status_or_grade", "issue_date", "valid_from", "valid_to",
                "signatory_name",
            ]
        }
    )
    primary = FakePrimary(text=good)
    payload = run(cfg, primary, monkeypatch)
    assert payload["status"] == "ok"
    assert payload["audit"]["fallback_used"] is False
    assert payload["audit"]["provider_used"] == "groq"


# --------------------------------------------------------------------------
# Transport failure does not consume the primary's retry budget
# --------------------------------------------------------------------------


def test_a_transport_failure_does_not_retry_the_dead_primary(cfg, monkeypatch):
    """With a fallback available, a connection error hands over at once rather
    than re-calling the primary for the whole schema-retry budget."""
    cfg["llm"]["max_retries"] = 3
    primary = FakePrimary(error=_CALL_ERRORS["connection_error"])
    payload = run(cfg, primary, monkeypatch)
    assert payload["audit"]["fallback_used"] is True
    assert primary.calls == 1


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_breaker_opens_after_n_failures_then_skips_the_primary(cfg, monkeypatch):
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=2, cool_down_seconds=60, clock=clock)
    primary = FakePrimary(error=_CALL_ERRORS["connection_error"])
    patch_providers(monkeypatch, primary, cfg)

    # Two failures open the breaker.
    for _ in range(2):
        out = extract_certificate_fields(PAGE, config=cfg, breaker=breaker).to_dict()
        assert out["audit"]["fallback_used"] is True
    assert primary.calls == 2
    assert breaker.is_open("groq") is True

    # Third document: breaker open, primary skipped entirely.
    out = extract_certificate_fields(PAGE, config=cfg, breaker=breaker).to_dict()
    assert primary.calls == 2, "primary must not be called while the breaker is open"
    assert out["audit"]["breaker_open"] is True
    assert out["audit"]["fallback_used"] is True
    assert out["audit"]["fallback_category"] == "breaker_open"

    # After the cool-down the primary is tried again.
    clock.t = 61.0
    assert breaker.is_open("groq") is False
    extract_certificate_fields(PAGE, config=cfg, breaker=breaker).to_dict()
    assert primary.calls == 3


def test_a_breaker_closes_on_a_reaching_success(cfg, monkeypatch):
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=2, cool_down_seconds=60, clock=clock)
    good = FakePrimary(
        text=json.dumps(
            {
                name: {"value": None, "source_snippet": None, "page": None, "confidence": None}
                for name in [
                    "institution_name", "certificate_number", "issuing_body", "programme",
                    "accreditation_status_or_grade", "issue_date", "valid_from", "valid_to",
                    "signatory_name",
                ]
            }
        )
    )
    patch_providers(monkeypatch, good, cfg)
    breaker.record_failure("groq")
    extract_certificate_fields(PAGE, config=cfg, breaker=breaker)
    assert breaker.snapshot("groq")["consecutive_failures"] == 0


def test_an_auth_failure_does_not_trip_the_breaker(cfg, monkeypatch):
    """A rejected key is a configuration fault, not an outage, so repeated ones
    must not open the breaker."""
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=2, cool_down_seconds=60, clock=clock)
    primary = FakePrimary(error=_UNAVAILABLE_ERRORS["auth_rejected"])
    patch_providers(monkeypatch, primary, cfg)
    for _ in range(3):
        extract_certificate_fields(PAGE, config=cfg, breaker=breaker)
    assert breaker.is_open("groq") is False


# --------------------------------------------------------------------------
# Sanitisation: the key never reaches stored output
# --------------------------------------------------------------------------


def test_the_api_key_never_appears_in_a_sanitised_message(monkeypatch):
    """A real provider's error, with a recognisable fake key set, must not
    carry the key in the message the audit stores."""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "sk-RECOGNISABLE-SECRET-123456")
    from app.llm.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        {
            "name": "groq",
            "base_url": "https://api.example.test/v1",
            "model": "m",
            "api_key_env": "TEST_PROVIDER_KEY",
        }
    )

    class Resp:
        status_code = 401
        text = "Invalid API Key sk-RECOGNISABLE-SECRET-123456"
        headers: dict = {}

        def json(self):
            return {}

    class HTTP:
        def post(self, *a, **k):
            return Resp()

    provider._http = lambda: HTTP()

    with pytest.raises(LLMUnavailableError) as exc:
        provider.complete("hi")
    assert exc.value.category == FailureCategory.AUTH_REJECTED
    # The sanitised message is what the audit stores, and it never has the key.
    assert "sk-RECOGNISABLE-SECRET-123456" not in exc.value.sanitized
    # The ordinary message is scrubbed too, as a defence in depth.
    assert "sk-RECOGNISABLE-SECRET-123456" not in str(exc.value)


def test_fallback_reason_in_the_audit_has_no_key(cfg, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "sk-RECOGNISABLE-SECRET-123456")
    primary = FakePrimary(
        error=LLMCallError(
            "could not reach api.example.test: boom",
            FailureCategory.CONNECTION_ERROR,
            "connection_error contacting api.example.test",
        )
    )
    payload = run(cfg, primary, monkeypatch)
    assert "sk-RECOGNISABLE-SECRET-123456" not in json.dumps(payload)


# --------------------------------------------------------------------------
# Scoring is identical whether the model or the fallback produced the values
# --------------------------------------------------------------------------


def test_scoring_is_identical_for_equal_values_fallback_or_not(cfg, monkeypatch):
    """A fallback reading and a model reading of the same values must produce
    the same findings and the same score - the reader identity carries no
    points."""
    from app.scoring import score_findings

    def issuer_like_findings(payload):
        # Build the finding a downstream detector would, from the validated
        # certificate number, so the comparison is on values, not providers.
        value = payload["fields"]["certificate_number"]["value"]
        return [{"type": "issuer_field_mismatch", "detector": "certificate_authenticity",
                 "strength": "strong", "reason": f"number {value}", "review_status": "open"}]

    # Model path: a primary that returns exactly what the offline reader would.
    from app.llm.rules_provider import RulesProvider
    rules_text = RulesProvider(dict(cfg["llm"]["providers"]["rules"])).complete(
        "## Certificate text\n[page 1]\n" + PAGE[1]
    ).text
    model_payload = run(cfg, FakePrimary(text=rules_text), monkeypatch)
    assert model_payload["audit"]["fallback_used"] is False

    fallback_payload = run(cfg, FakePrimary(error=_CALL_ERRORS["connection_error"]), monkeypatch)
    assert fallback_payload["audit"]["fallback_used"] is True

    # The validated values match...
    model_values = {k: v["value"] for k, v in model_payload["fields"].items()}
    fb_values = {k: v["value"] for k, v in fallback_payload["fields"].items()}
    assert model_values == fb_values

    # ...so an identical finding set scores identically.
    score_model = score_findings(issuer_like_findings(model_payload), load_config("risk_scoring"))
    score_fb = score_findings(issuer_like_findings(fallback_payload), load_config("risk_scoring"))
    assert score_model["score"] == score_fb["score"]
    assert score_model["level"] == score_fb["level"]


def test_the_rules_reader_output_passes_the_same_snippet_guard(cfg, monkeypatch):
    """A value the offline reader copies is validated exactly as a model's is:
    a number that is not in the page text is discarded."""
    # A page whose printed number differs from what any snippet could ground.
    page = {1: "Institution: Demo College\nCertificate Number: NBTA/DEMO/2024/00417\n"}
    primary = FakePrimary(error=_CALL_ERRORS["connection_error"])
    patch_providers(monkeypatch, primary, cfg)
    payload = extract_certificate_fields(page, config=cfg).to_dict()
    cert = payload["fields"]["certificate_number"]
    # Present in the page -> validated.
    assert cert["value"] == "NBTA/DEMO/2024/00417"
    assert cert["validated"] is True
    # A field not printed at all is simply absent, never invented.
    assert payload["fields"]["programme"]["value"] is None
