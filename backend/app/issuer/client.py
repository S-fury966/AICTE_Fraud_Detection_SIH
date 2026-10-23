"""The HTTP client for the issuer lookup.

The destination comes from config and from nowhere else. `IssuerClient` takes
a base URL at construction and offers no way to override it per request, so
there is no code path by which a URL read off a submitted document could
become the address we contact. That is the single most important property in
this module, and it is a structural guarantee rather than a rule someone has
to remember.

Retries cover transport errors, 429 and 5xx: conditions where the same request
could legitimately succeed next time. A 404 and a malformed body are answers,
and repeating the request cannot change either.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from ..core.config import get
from .models import RequestAttempt


class IssuerError(RuntimeError):
    """Base for lookup failures, carrying the request history.

    The attempts travel with the exception because a failed lookup is exactly
    when the audit record needs them: "source unavailable" is only meaningful
    to a reviewer alongside how many times we tried and what came back.
    """

    def __init__(
        self,
        message: str,
        attempts: list[RequestAttempt] | None = None,
        url: str = "",
        http_status: int | None = None,
        raw_text: str | None = None,
        latency_ms: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts or []
        self.url = url
        self.http_status = http_status
        self.raw_text = raw_text
        self.latency_ms = latency_ms


class IssuerUnreachableError(IssuerError):
    """Every attempt failed in a way that might have succeeded. Unavailable."""


class MalformedIssuerResponseError(IssuerError):
    """The issuer answered, but not with something we can read."""


@dataclass
class IssuerResponse:
    http_status: int
    body: Any
    raw_text: str
    attempts: list[RequestAttempt] = field(default_factory=list)
    url: str = ""
    # Total wall-clock time across every attempt.
    latency_ms: float = 0.0


class IssuerClient:
    def __init__(
        self,
        cfg: dict[str, Any],
        transport: Any | None = None,
        sleep: Any = None,
    ) -> None:
        self._cfg = cfg
        # The base URL is read once, here, from config.
        self._base_url = str(get(cfg, "issuer.base_url", "")).rstrip("/")
        if not self._base_url:
            raise ValueError("issuer.base_url is not configured")
        self._lookup_path = str(get(cfg, "issuer.lookup_path", "/v1/certificates"))
        self._style = str(get(cfg, "issuer.lookup_style", "query"))
        self._query_parameter = str(get(cfg, "issuer.query_parameter", "certificate_id"))
        # A caller-supplied client (FastAPI's TestClient in tests) keeps the
        # fast suite off the network without changing the code under test.
        self._transport = transport
        self._sleep = sleep if sleep is not None else time.sleep
        # TestClient dispatches in-process and rejects a `timeout` argument,
        # so it is passed only on the real HTTP path. A test that needs to
        # exercise the timeout branch raises the timeout from its transport
        # instead, which is what a real client does anyway.
        self._send_timeout = transport is None

    @property
    def base_url(self) -> str:
        return self._base_url

    def _client(self) -> Any:
        if self._transport is not None:
            return self._transport
        try:
            import httpx2 as httpx
        except ImportError:
            try:
                import httpx
            except ImportError as exc:
                raise IssuerUnreachableError(
                    "no HTTP client available. Install one with: pip install httpx2"
                ) from exc
        return httpx

    def _target(self, certificate_id: str) -> tuple[str, dict[str, str] | None]:
        """Build the request URL. Always rooted at the configured base URL."""
        if self._style == "path":
            # Percent-encode the whole id into one path segment: an unencoded
            # NBTA/DEMO/2024/00417 would read as four segments.
            encoded = quote(certificate_id, safe="")
            return f"{self._base_url}{self._lookup_path}/{encoded}", None
        return (
            f"{self._base_url}{self._lookup_path}",
            {self._query_parameter: certificate_id},
        )

    def lookup(self, certificate_id: str) -> IssuerResponse:
        """Ask the issuer about one certificate id.

        Raises IssuerUnreachableError when the retries are exhausted. A 404 is
        returned normally: "no such record" is an answer.
        """
        url, params = self._target(certificate_id)
        client = self._client()

        timeout = float(get(self._cfg, "request.timeout_seconds", 5.0))
        max_retries = int(get(self._cfg, "request.max_retries", 2))
        backoff = float(get(self._cfg, "request.backoff_seconds", 0.5))
        multiplier = float(get(self._cfg, "request.backoff_multiplier", 2.0))
        max_retry_after = float(get(self._cfg, "request.max_retry_after_seconds", 10.0))
        retry_statuses = {
            int(code) for code in (get(self._cfg, "request.retry_on_status", []) or [])
        }

        attempts: list[RequestAttempt] = []
        total_start = time.perf_counter()
        last_error: str | None = None

        for attempt_number in range(1, max_retries + 2):
            started = time.perf_counter()
            kwargs: dict[str, Any] = {"params": params}
            if self._send_timeout:
                kwargs["timeout"] = timeout
            try:
                response = client.get(url, **kwargs)
            except Exception as exc:
                elapsed = (time.perf_counter() - started) * 1000
                last_error = f"{type(exc).__name__}: {exc}"
                attempt = RequestAttempt(
                    number=attempt_number,
                    http_status=None,
                    latency_ms=elapsed,
                    error=last_error,
                )
                attempts.append(attempt)
                if attempt_number > max_retries:
                    break
                wait = backoff * (multiplier ** (attempt_number - 1))
                attempt.waited_before_next = wait
                self._sleep(wait)
                continue

            elapsed = (time.perf_counter() - started) * 1000
            attempt = RequestAttempt(
                number=attempt_number,
                http_status=response.status_code,
                latency_ms=elapsed,
            )
            attempts.append(attempt)

            if response.status_code in retry_statuses and attempt_number <= max_retries:
                wait = backoff * (multiplier ** (attempt_number - 1))
                if response.status_code == 429:
                    hinted = _retry_after(response, max_retry_after)
                    if hinted is not None:
                        wait = hinted
                attempt.waited_before_next = wait
                last_error = f"HTTP {response.status_code}"
                self._sleep(wait)
                continue

            if response.status_code in retry_statuses:
                # Out of retries on a retryable status: unavailable, not an
                # answer about the certificate.
                raise IssuerUnreachableError(
                    f"issuer returned HTTP {response.status_code} after "
                    f"{attempt_number} attempt(s)",
                    attempts=attempts,
                    url=url,
                    http_status=response.status_code,
                    raw_text=_raw_text(response),
                    latency_ms=(time.perf_counter() - total_start) * 1000,
                )

            raw_text = _raw_text(response)
            try:
                body = response.json()
            except Exception as exc:
                raise MalformedIssuerResponseError(
                    f"issuer responded with HTTP {response.status_code} but the "
                    f"body could not be read as JSON: {exc}",
                    attempts=attempts,
                    url=url,
                    http_status=response.status_code,
                    raw_text=raw_text,
                    latency_ms=(time.perf_counter() - total_start) * 1000,
                ) from exc

            return IssuerResponse(
                http_status=response.status_code,
                body=body,
                raw_text=raw_text,
                attempts=attempts,
                url=url,
                latency_ms=(time.perf_counter() - total_start) * 1000,
            )

        raise IssuerUnreachableError(
            f"issuer at {self._base_url} could not be reached after "
            f"{len(attempts)} attempt(s); last error: {last_error}",
            attempts=attempts,
            url=url,
            latency_ms=(time.perf_counter() - total_start) * 1000,
        )


def _retry_after(response: Any, cap: float) -> float | None:
    """Honour the server's wait hint, capped.

    Capped because a server asking for five minutes should surface as
    source_unavailable for a reviewer to see, not stall the run silently.
    """
    try:
        raw = response.headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(seconds, cap))


def _raw_text(response: Any) -> str:
    try:
        return response.text
    except Exception:
        return "<unreadable body>"
