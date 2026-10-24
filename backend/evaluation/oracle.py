"""The oracle provider: a perfect model, so the other detectors can be measured.

In oracle mode the stub returns exactly the fields the generator printed, with
`source_snippet` values taken from the **real extracted page text**. That
isolates the question: how well do the deterministic detectors work when the
language model is not the weak link?

Two things this mode deliberately does *not* do.

**It does not report extraction accuracy.** The answers are the ground truth
by construction, so any accuracy number would be 100% and would mean nothing.
Extraction is measured in live mode only.

**It does not bypass the snippet guard.** The snippets are grounded in the page
text the extractor actually saw, so on a clean digital PDF every field
validates - but on a badly OCR'd scan the printed value may genuinely not
appear in the text, the snippet will not match, and step 2 will discard the
field exactly as it would for a real model. That is the honest behaviour: the
guard is part of the deterministic system under test, not part of the model.
A run therefore still shows fields lost to unreadable input, and it should.

The page text is read out of the prompt, which embeds it verbatim after the
`## Certificate text` heading. That keeps the pipeline unmodified - the oracle
is an ordinary provider and the pipeline cannot tell it apart from Groq.
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.base import LLMCompletion

# The heading the step 2 prompt puts immediately before the document text.
_TEXT_MARKER = "## Certificate text"

# Fields whose printed value is a single string. `programme` is a list in the
# schema, so it is handled separately.
_STRING_FIELDS = (
    "institution_name",
    "certificate_number",
    "issuing_body",
    "accreditation_status_or_grade",
    "issue_date",
    "valid_from",
    "valid_to",
    "signatory_name",
)


class OracleProvider:
    """Returns the printed fields, with snippets grounded in the page text."""

    name = "oracle"

    def __init__(self, printed_fields: dict[str, Any], model: str = "oracle") -> None:
        self.printed = dict(printed_fields or {})
        self.model = model
        self.calls = 0
        # Fields whose printed value could not be located in the page text.
        # Worth surfacing: it is the measurable footprint of unreadable input.
        self.ungrounded: list[str] = []

    def complete(self, prompt: str) -> LLMCompletion:
        self.calls += 1
        page_text = _document_text(prompt)
        payload = self._answer(page_text)
        text = json.dumps(payload, ensure_ascii=False)
        return LLMCompletion(
            text=text,
            model=self.model,
            provider=self.name,
            # Zero, not an estimate: the oracle costs nothing, and a fabricated
            # token count would pollute the cost totals the runner prints.
            input_tokens=0,
            output_tokens=0,
            metadata={"oracle": True, "ungrounded_fields": list(self.ungrounded)},
        )

    # ------------------------------------------------------------------

    def _answer(self, page_text: str) -> dict[str, Any]:
        answer: dict[str, Any] = {}

        for name in _STRING_FIELDS:
            value = self.printed.get(name)
            answer[name] = self._entry(name, value, page_text)

        programme = self.printed.get("programme")
        if programme:
            # The schema wants a list. The generator prints one comma-joined
            # line, and splitting it here is what a competent model would
            # return - step 4 then canonicalises both sides the same way.
            parts = [part.strip() for part in str(programme).split(",") if part.strip()]
            snippet = _line_containing(page_text, str(programme))
            if snippet is None:
                self.ungrounded.append("programme")
            answer["programme"] = {
                "value": parts or None,
                "source_snippet": snippet,
                "page": 1,
                "confidence": "high" if snippet else "low",
            }
        else:
            answer["programme"] = _null_entry()

        return answer

    def _entry(self, name: str, value: Any, page_text: str) -> dict[str, Any]:
        if value is None or str(value).strip() == "":
            # A field the generator did not print. Null is a normal, correct
            # answer and must stay null - inventing one would turn the
            # missing-grade hard negative into a mismatch.
            return _null_entry()

        snippet = _line_containing(page_text, str(value))
        if snippet is None:
            self.ungrounded.append(name)
        return {
            "value": value,
            "source_snippet": snippet,
            "page": 1,
            "confidence": "high" if snippet else "low",
        }


def _null_entry() -> dict[str, Any]:
    return {
        "value": None,
        "source_snippet": None,
        "page": None,
        "confidence": None,
    }


def _document_text(prompt: str) -> str:
    index = prompt.find(_TEXT_MARKER)
    if index < 0:
        # The prompt shape changed. Returning the whole prompt still lets the
        # snippet search work; it just searches more text than it needs to.
        return prompt
    return prompt[index + len(_TEXT_MARKER) :]


def _line_containing(page_text: str, value: str) -> str | None:
    """The line of page text that contains `value`, or None.

    Returns the whole line rather than the value alone because step 2's
    snippet guard wants a quotation from the source, and a bare value is a
    weaker claim than the line it sits on. Matching is done on a
    whitespace-collapsed copy, since OCR output wraps and re-spaces freely,
    while the snippet returned is the original line - what a reader would see.
    """
    needle = _collapse(value)
    if not needle:
        return None
    for line in page_text.splitlines():
        if needle in _collapse(line):
            stripped = line.strip()
            if stripped:
                return stripped
    return None


def _collapse(text: str) -> str:
    return " ".join(str(text).split()).casefold()
