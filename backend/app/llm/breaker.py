"""A small in-memory circuit breaker for the primary LLM provider.

When the network is down, every document in a run would otherwise wait out a
connection attempt before falling back to the offline reader. The breaker
stops that: after a few consecutive transport failures it marks the primary
"open" and the extractor goes straight to the fallback for a cool-down period,
so only the first document or two pay the wait. A later success closes it.

State lives in memory, per process and per provider name - it is a speed
optimisation, not a durable record, and losing it on restart is harmless. The
clock is injectable so a test can open and close the breaker without real time
passing.

Which failures count is the caller's decision (it passes `record_failure` only
for reachability categories); a 401 or a 404 will not come good in sixty
seconds and says nothing about whether the endpoint is reachable, so the
extractor does not trip the breaker on those.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(
        self,
        threshold: int = 2,
        cool_down_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.cool_down_seconds = float(cool_down_seconds)
        import time

        self._clock = clock or time.monotonic
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()

    def is_open(self, provider: str) -> bool:
        """True while the primary should be skipped for this provider.

        Reading it also lets an elapsed cool-down close the breaker, so the
        next call tries the primary again - a half-open probe, in effect.
        """
        with self._lock:
            state = self._states.get(provider)
            if state is None or state.opened_at is None:
                return False
            if self._clock() - state.opened_at >= self.cool_down_seconds:
                # Cool-down elapsed: let the next call try the primary.
                state.opened_at = None
                state.consecutive_failures = 0
                return False
            return True

    def record_failure(self, provider: str) -> bool:
        """Count a transport failure; open the breaker at the threshold.

        Returns whether the breaker is open after this failure.
        """
        with self._lock:
            state = self._states.setdefault(provider, _State())
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.threshold and state.opened_at is None:
                state.opened_at = self._clock()
            return state.opened_at is not None

    def record_success(self, provider: str) -> None:
        """A reaching call closes the breaker and clears the failure run."""
        with self._lock:
            self._states[provider] = _State()

    def snapshot(self, provider: str) -> dict[str, object]:
        with self._lock:
            state = self._states.get(provider) or _State()
            return {
                "open": state.opened_at is not None,
                "consecutive_failures": state.consecutive_failures,
            }


# The process-wide breaker the extractor uses by default, so its state is
# shared across every document in a run. Tests construct their own with an
# injected clock and pass it in rather than touching this one.
_DEFAULT = CircuitBreaker()


def default_breaker() -> CircuitBreaker:
    return _DEFAULT


def configure_default(threshold: int, cool_down_seconds: float) -> None:
    """Re-point the process breaker at the configured thresholds.

    Called from the extractor with the values in certificate_extraction.yaml.
    Keeps the accumulated state - only the thresholds change.
    """
    _DEFAULT.threshold = max(1, int(threshold))
    _DEFAULT.cool_down_seconds = float(cool_down_seconds)
