"""One provider for every OpenAI-compatible chat completions endpoint.

Written against the REST shape rather than a vendor SDK, because the shape is
what the vendors share. Mistral, Groq, OpenRouter, Together and a local
llama.cpp server all speak it, so each is a `base_url` and an `api_key_env` in
config rather than another file here. That is what the project's "free LLM
APIs" decision needs: free tiers come and go, and swapping one for another
should not be a code change.

`name` comes from settings purely so errors say which endpoint refused.

This provider does send `temperature`: these endpoints accept sampling
parameters, and 0 is what the config asks for.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .base import LLMCallError, LLMCompletion, LLMUnavailableError

DEFAULT_MAX_TOKENS = 4096
DEFAULT_RATE_LIMIT_RETRIES = 3
DEFAULT_RATE_LIMIT_BACKOFF = 2.0


class OpenAICompatibleProvider:
    """Chat completions over any endpoint that speaks the OpenAI shape."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = settings or {}
        self.name = str(self._settings.get("name", "openai-compatible"))
        self.model = str(self._settings.get("model", ""))
        self.base_url = str(self._settings.get("base_url", "")).rstrip("/")
        if not self.base_url:
            raise LLMUnavailableError(
                f"provider {self.name!r} has no base_url configured"
            )
        if not self.model:
            raise LLMUnavailableError(
                f"provider {self.name!r} has no model configured"
            )

    @property
    def api_key_env(self) -> str:
        return str(self._settings.get("api_key_env", ""))

    def is_configured(self) -> bool:
        return bool(os.environ.get(self.api_key_env))

    def _api_key(self) -> str:
        if not self.api_key_env:
            raise LLMUnavailableError(
                f"provider {self.name!r} has no api_key_env configured"
            )
        key = os.environ.get(self.api_key_env)
        if not key:
            raise LLMUnavailableError(
                f"no API key found in environment variable {self.api_key_env}"
            )
        return key

    def _http(self):
        """The HTTP module to post with. A seam, so tests need no network."""
        try:
            import httpx2 as httpx
        except ImportError:
            try:
                import httpx
            except ImportError as exc:
                raise LLMUnavailableError(
                    "no HTTP client available. Install one with: pip install httpx2"
                ) from exc
        return httpx

    def complete(self, prompt: str) -> LLMCompletion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(self._settings.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "temperature": float(self._settings.get("temperature", 0)),
        }
        if bool(self._settings.get("json_mode", True)):
            # Server-side guarantee that the body parses as JSON. The schema
            # still has to be validated by our code: JSON mode constrains the
            # syntax, not the contents.
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }
        timeout = float(self._settings.get("timeout_seconds", 60))
        http = self._http()

        # Free tiers are rate limited hard (often a request per second), so a
        # 429 is an expected part of normal operation rather than a failure.
        # Waiting and retrying here keeps it out of the extractor's schema
        # retry budget, which exists for a different problem.
        retries = int(
            self._settings.get("rate_limit_retries", DEFAULT_RATE_LIMIT_RETRIES)
        )
        backoff = float(
            self._settings.get("rate_limit_backoff_seconds", DEFAULT_RATE_LIMIT_BACKOFF)
        )

        response = None
        for attempt in range(retries + 1):
            try:
                response = http.post(
                    url, headers=headers, json=payload, timeout=timeout
                )
            except Exception as exc:
                raise LLMCallError(f"could not reach {url}: {exc}") from exc

            if response.status_code != 429:
                break

            # A quota of zero is not congestion: the workspace has no request
            # allowance at all, and no amount of waiting will produce one.
            if _header(response, "x-ratelimit-limit-req-minute") == "0":
                raise LLMUnavailableError(
                    f"{self.name} reports a request quota of 0/minute for the key "
                    f"from {self.api_key_env}. The key authenticates but the account "
                    f"has no request allowance, so waiting cannot help: activate or "
                    f"verify the plan in the provider's console. "
                    f"Body: {_body(response)}"
                )
            if attempt >= retries:
                break
            self._sleep(_retry_after(response, backoff * (2**attempt)))

        # 401/403/404 mean the setup is wrong, not that this call was unlucky,
        # so they are unavailable rather than retryable call errors.
        if response.status_code in (401, 403):
            raise LLMUnavailableError(
                f"{self.name} rejected the key from {self.api_key_env} "
                f"(HTTP {response.status_code}): {_body(response)}"
            )
        if response.status_code == 404:
            raise LLMUnavailableError(
                f"{self.name} has no endpoint or model at {url} for model "
                f"{self.model!r} (HTTP 404): {_body(response)}"
            )
        if response.status_code != 200:
            raise LLMCallError(
                f"{self.name} returned HTTP {response.status_code}: {_body(response)}"
            )

        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMCallError(
                f"{self.name} returned an unexpected response shape: {exc}"
            ) from exc

        usage = data.get("usage") or {}
        return LLMCompletion(
            text=text,
            model=data.get("model", self.model),
            provider=self.name,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            metadata={
                "finish_reason": choice.get("finish_reason"),
                "response_id": data.get("id"),
            },
        )

    def _sleep(self, seconds: float) -> None:
        """Overridden in tests so the backoff costs no wall-clock time."""
        time.sleep(seconds)


def _header(response: Any, name: str) -> str | None:
    try:
        return response.headers.get(name)
    except Exception:
        return None


def _retry_after(response: Any, fallback: float) -> float:
    """Honour the server's own wait hint when it sends one."""
    raw = _header(response, "retry-after")
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        return fallback
    # Cap it: a server asking for minutes should surface as a failure the
    # caller can see, not a silent stall.
    return max(0.0, min(wait, 30.0))


def _body(response: Any) -> str:
    """A short, safe excerpt of an error body for the message."""
    try:
        return response.text[:300]
    except Exception:
        return "<unreadable body>"
