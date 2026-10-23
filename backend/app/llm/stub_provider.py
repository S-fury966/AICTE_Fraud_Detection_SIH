"""Stub provider for tests and offline demos.

Returns canned responses in order and records the prompts it was given, so a
test can assert both what came back and what was asked.
"""

from __future__ import annotations

from typing import Any, Sequence

from .base import LLMCallError, LLMCompletion


class StubProvider:
    name = "stub"

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        responses: Sequence[str] | None = None,
    ) -> None:
        settings = settings or {}
        # Responses can come from config (a recorded fixture) or be passed
        # directly by a test.
        if responses is None:
            responses = settings.get("responses") or []
        self._responses = list(responses)
        self.model = str(settings.get("model", "stub-model"))
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, prompt: str) -> LLMCompletion:
        self.prompts.append(prompt)
        self.calls += 1
        if not self._responses:
            raise LLMCallError("stub provider has no responses left")
        text = self._responses.pop(0)
        return LLMCompletion(
            text=text,
            model=self.model,
            provider=self.name,
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
            metadata={"stub": True},
        )
