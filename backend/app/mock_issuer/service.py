"""The mock issuer service: a stand-in for an accrediting body's lookup API.

This exists because the real thing is a restricted government database we do
not have and must not pretend to have. Everything it serves is synthetic.

Two things it deliberately gets right, because the verification client is only
as good as what it was tested against:

- **Certificate ids contain slashes.** The id is taken from a query parameter,
  so `NBTA/DEMO/2024/00417` survives without percent-encoding games. A
  percent-encoded path route is also provided, and both are tested, because a
  real issuer could expose either.
- **Failure is a first-class response.** Timeouts, 429s, 500s and malformed
  bodies are what a client meets in the field, so the service can produce all
  of them on demand — never by default, and only through an explicitly named
  switch.

Binds to localhost only.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .registry import Registry, load_registry


@dataclass
class FaultInjection:
    """Test-only failure switches. Every field is off unless explicitly set.

    Named `FaultInjection` and defaulted to inert so no code path can produce
    a failure by accident: a mock that fails by default would make a passing
    test meaningless.
    """

    # Return this HTTP status instead of serving the record.
    force_status: int | None = None
    # Serve a body that is not valid JSON.
    malformed_body: bool = False
    # Sleep this long before responding, to drive the client's timeout.
    delay_seconds: float = 0.0
    # Value for the Retry-After header on a 429.
    retry_after: str | None = None
    # Fail this many requests before behaving normally. Lets a test assert the
    # retry schedule and then a success.
    fail_first_n: int = 0
    # Counter for fail_first_n. Reset between tests by building a new app.
    _served: int = field(default=0, repr=False)

    def active(self) -> bool:
        return bool(
            self.force_status
            or self.malformed_body
            or self.delay_seconds
            or self.fail_first_n
        )

    def should_fail_now(self) -> bool:
        if self.fail_first_n:
            if self._served < self.fail_first_n:
                self._served += 1
                return True
            return False
        return self.active()

    def reset(self) -> None:
        self._served = 0


def create_app(
    registry: Registry | None = None,
    registry_path: str | Path | None = None,
    faults: FaultInjection | None = None,
) -> FastAPI:
    """Build the service. `faults` is for tests; production callers omit it."""
    loaded = registry if registry is not None else load_registry(registry_path)
    injection = faults if faults is not None else FaultInjection()

    app = FastAPI(
        title="Mock Issuer Verification Service (Demo)",
        description=(
            "Synthetic stand-in for an accrediting body's certificate lookup "
            "API. All data is fictional and accredits nothing."
        ),
        version=loaded.version,
    )
    app.state.registry = loaded
    app.state.faults = injection

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "registry_version": loaded.version,
            "record_count": len(loaded.records),
        }

    def _serve(certificate_id: str, request: Request) -> Response:
        fault = request.app.state.faults

        if fault.delay_seconds:
            # Drives the client's timeout. Synchronous on purpose: the client
            # should time out regardless of how the server is blocked.
            time.sleep(float(fault.delay_seconds))

        if fault.should_fail_now():
            status = int(fault.force_status or 500)
            if fault.malformed_body:
                return PlainTextResponse(
                    "{not json at all", status_code=fault.force_status or 200
                )
            headers = {}
            if status == 429 and fault.retry_after is not None:
                headers["Retry-After"] = str(fault.retry_after)
            return JSONResponse(
                {
                    "error": "injected failure",
                    "detail": f"fault injection returned {status}",
                },
                status_code=status,
                headers=headers,
            )

        wanted = (certificate_id or "").strip()
        if not wanted:
            return JSONResponse(
                {"error": "missing certificate_id"}, status_code=400
            )

        registry_now: Registry = request.app.state.registry

        if wanted in registry_now.pending_ids:
            # 202: the issuer has the application but has not finished with
            # it. Not a yes, not a no, and not an error.
            return JSONResponse(
                {
                    "certificate_id": wanted,
                    "lookup_status": "pending",
                    "detail": (
                        "verification is in progress at the issuer; no "
                        "determination yet"
                    ),
                    "registry_version": registry_now.version,
                },
                status_code=202,
            )

        record = registry_now.find(wanted)
        if record is None:
            return JSONResponse(
                {
                    "certificate_id": wanted,
                    "lookup_status": "not_found",
                    "detail": "no record with this certificate id",
                    "registry_version": registry_now.version,
                },
                status_code=404,
            )

        return JSONResponse(
            {
                "certificate_id": wanted,
                "lookup_status": "found",
                "record": record,
                "registry_version": registry_now.version,
            },
            status_code=200,
        )

    @app.get("/v1/certificates")
    def lookup_by_query(
        request: Request,
        certificate_id: str = Query(
            ...,
            description=(
                "The full certificate id. Slashes need no encoding in a query "
                "parameter value."
            ),
        ),
    ) -> Response:
        return _serve(certificate_id, request)

    @app.get("/v1/certificates/{encoded_id:path}")
    def lookup_by_path(encoded_id: str, request: Request) -> Response:
        # The alternative shape: the id percent-encoded into the path. Starlette
        # has already decoded it once by the time it arrives, so a doubly
        # encoded id is unquoted again here.
        return _serve(unquote(encoded_id), request)

    return app


def main() -> int:
    """Run the service on localhost. Never binds a public interface."""
    import uvicorn

    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8088, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
