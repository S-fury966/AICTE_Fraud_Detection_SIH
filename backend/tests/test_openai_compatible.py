"""Provider-level tests for the OpenAI-compatible transport.

No network: a fake HTTP module stands in, so the retry and status-mapping
rules can be checked exactly.
"""

from __future__ import annotations

import pytest

from app.llm.base import LLMCallError, LLMUnavailableError
from app.llm.openai_compatible import OpenAICompatibleProvider

API_KEY_ENV = "TEST_PROVIDER_KEY"
BASE = "https://api.example.test/v1"
MODEL = "test-model"


class FakeResponse:
    def __init__(self, status_code, body="", headers=None, payload=None):
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def ok_payload(text='{"ok": true}'):
    return {
        "id": "resp-1",
        "model": MODEL,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class FakeHTTP:
    """Returns the queued responses in order, recording every request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self._responses.pop(0)


def build(responses, settings=None, monkeypatch=None):
    provider = OpenAICompatibleProvider(
        {
            "name": "testprovider",
            "base_url": BASE,
            "model": MODEL,
            "api_key_env": API_KEY_ENV,
            **(settings or {}),
        }
    )
    http = FakeHTTP(responses)
    provider._http = lambda: http
    provider.slept = []
    provider._sleep = provider.slept.append
    return provider, http


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "test-key-value")


def test_successful_call_returns_text_and_usage():
    provider, http = build([FakeResponse(200, payload=ok_payload())])

    result = provider.complete("hello")

    assert result.text == '{"ok": true}'
    assert result.provider == "testprovider"
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    assert result.metadata["finish_reason"] == "stop"


def test_request_carries_the_key_json_mode_and_temperature():
    provider, http = build([FakeResponse(200, payload=ok_payload())])

    provider.complete("hello")

    sent = http.requests[0]
    assert sent["url"].endswith("/chat/completions")
    assert sent["headers"]["Authorization"] == "Bearer test-key-value"
    assert sent["json"]["temperature"] == 0
    assert sent["json"]["response_format"] == {"type": "json_object"}


def test_json_mode_can_be_switched_off():
    provider, http = build(
        [FakeResponse(200, payload=ok_payload())], {"json_mode": False}
    )

    provider.complete("hello")

    assert "response_format" not in http.requests[0]["json"]


def test_missing_key_is_unavailable(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    provider, _ = build([FakeResponse(200, payload=ok_payload())])

    with pytest.raises(LLMUnavailableError) as exc:
        provider.complete("hello")

    assert API_KEY_ENV in str(exc.value)


def test_rejected_key_is_unavailable_not_retried():
    provider, http = build([FakeResponse(401, '{"detail":"Invalid API Key"}')])

    with pytest.raises(LLMUnavailableError) as exc:
        provider.complete("hello")

    assert "rejected the key" in str(exc.value)
    assert len(http.requests) == 1


def test_rate_limit_is_retried_then_succeeds():
    provider, http = build(
        [
            FakeResponse(429, "Rate limit exceeded"),
            FakeResponse(429, "Rate limit exceeded"),
            FakeResponse(200, payload=ok_payload()),
        ]
    )

    result = provider.complete("hello")

    assert result.text == '{"ok": true}'
    assert len(http.requests) == 3
    assert provider.slept == [2.0, 4.0]


def test_retry_after_header_overrides_the_backoff():
    provider, http = build(
        [
            FakeResponse(429, "slow down", headers={"retry-after": "5"}),
            FakeResponse(200, payload=ok_payload()),
        ]
    )

    provider.complete("hello")

    assert provider.slept == [5.0]


def test_an_absurd_retry_after_is_capped():
    provider, _ = build(
        [
            FakeResponse(429, "slow down", headers={"retry-after": "900"}),
            FakeResponse(200, payload=ok_payload()),
        ]
    )

    provider.complete("hello")

    assert provider.slept == [30.0]


def test_rate_limit_retries_are_configurable():
    provider, http = build(
        [FakeResponse(429, "Rate limit exceeded") for _ in range(2)],
        {"rate_limit_retries": 1},
    )

    with pytest.raises(LLMCallError) as exc:
        provider.complete("hello")

    assert "429" in str(exc.value)
    assert len(http.requests) == 2


def test_zero_quota_is_unavailable_and_not_waited_out():
    """A limit of 0/minute is a provisioning problem, not congestion."""
    provider, http = build(
        [
            FakeResponse(
                429,
                "Rate limit exceeded",
                headers={"x-ratelimit-limit-req-minute": "0"},
            )
        ]
    )

    with pytest.raises(LLMUnavailableError) as exc:
        provider.complete("hello")

    message = str(exc.value)
    assert "quota of 0/minute" in message
    assert API_KEY_ENV in message
    assert len(http.requests) == 1
    assert provider.slept == []


def test_unknown_model_is_unavailable():
    provider, _ = build([FakeResponse(404, '{"detail":"model not found"}')])

    with pytest.raises(LLMUnavailableError) as exc:
        provider.complete("hello")

    assert "no endpoint or model" in str(exc.value)


def test_server_error_is_a_call_error():
    provider, _ = build([FakeResponse(500, "upstream exploded")])

    with pytest.raises(LLMCallError) as exc:
        provider.complete("hello")

    assert "500" in str(exc.value)


def test_unexpected_body_shape_is_a_call_error():
    provider, _ = build([FakeResponse(200, payload={"choices": []})])

    with pytest.raises(LLMCallError) as exc:
        provider.complete("hello")

    assert "unexpected response shape" in str(exc.value)


def test_transport_failure_is_a_call_error():
    provider = OpenAICompatibleProvider(
        {
            "name": "testprovider",
            "base_url": BASE,
            "model": MODEL,
            "api_key_env": API_KEY_ENV,
        }
    )

    class Broken:
        def post(self, *args, **kwargs):
            raise OSError("connection reset")

    provider._http = lambda: Broken()

    with pytest.raises(LLMCallError) as exc:
        provider.complete("hello")

    assert "could not reach" in str(exc.value)


def test_missing_base_url_is_unavailable():
    with pytest.raises(LLMUnavailableError) as exc:
        OpenAICompatibleProvider({"name": "nowhere", "model": MODEL})

    assert "no base_url configured" in str(exc.value)


def test_missing_model_is_unavailable():
    with pytest.raises(LLMUnavailableError) as exc:
        OpenAICompatibleProvider({"name": "nowhere", "base_url": BASE})

    assert "no model configured" in str(exc.value)


def test_errors_name_the_configured_provider():
    """The name exists so a failure says which endpoint refused."""
    provider, _ = build([FakeResponse(401, "nope")], {"name": "groq"})

    with pytest.raises(LLMUnavailableError) as exc:
        provider.complete("hello")

    assert str(exc.value).startswith("groq rejected the key")
