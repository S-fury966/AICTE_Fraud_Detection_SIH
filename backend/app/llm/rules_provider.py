"""Offline, label-based field reader that speaks the same contract as a model.

Exists so a demo, a laptop on a train, or a free tier that has run dry still
produces a certificate reading. It is **not** smarter than a model and does not
pretend to be: it finds a printed label ("Certificate Number", "Valid Until",
...) and copies whatever follows it on that line, or on the next non-empty line
when the layout is a two-column table.

It returns the exact JSON the step 2 prompt asks a model for, so everything
downstream is unchanged - and in particular the **snippet guard still applies**.
A value this reader copies is checked against the page text exactly as a model's
would be. The labels live in config (`llm.providers.rules.labels`), never here.

Like every provider it decides nothing. It reports what is printed after a
label; whether that is right is the deterministic validation's call.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import LLMCompletion

# The heading the step 2 prompt puts immediately before the document text.
_TEXT_MARKER = "## Certificate text"
_PAGE_MARKER = re.compile(r"^\s*\[page\s+(\d+)\]\s*$", re.IGNORECASE)

FIELDS = (
    "institution_name",
    "certificate_number",
    "issuing_body",
    "programme",
    "accreditation_status_or_grade",
    "issue_date",
    "valid_from",
    "valid_to",
    "signatory_name",
)

# Printed placeholders for "nothing here". Reported as null, never as a value.
_EMPTY_VALUES = {"-", "--", "n/a", "na", "nil", "none", "not graded"}


class RulesProvider:
    """Prompt in, label-matched JSON out. No network, no model."""

    name = "rules"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        settings = settings or {}
        self.model = str(settings.get("model", "label-rules-v1"))
        labels = settings.get("labels") or {}
        # Longest label first, so "Certificate Number" wins over "Certificate".
        self._labels: list[tuple[str, str]] = sorted(
            (
                (str(label), str(field))
                for field, names in labels.items()
                for label in (names or [])
            ),
            key=lambda pair: -len(pair[0]),
        )
        self.calls = 0

    def complete(self, prompt: str) -> LLMCompletion:
        self.calls += 1
        text = _document_text(prompt)
        answer = self._read(text)
        body = json.dumps(answer, ensure_ascii=False)
        return LLMCompletion(
            text=body,
            model=self.model,
            provider=self.name,
            # Zero, not an estimate: nothing was sent anywhere.
            input_tokens=0,
            output_tokens=0,
            metadata={"offline_rules": True},
        )

    # ------------------------------------------------------------------

    def _read(self, text: str) -> dict[str, Any]:
        answer: dict[str, Any] = {name: _null() for name in FIELDS}
        lines = text.splitlines()
        page = 1

        for index, raw in enumerate(lines):
            marker = _PAGE_MARKER.match(raw)
            if marker:
                page = int(marker.group(1))
                continue
            line = raw.strip()
            if not line:
                continue

            matched = self._match_label(line)
            if matched is None:
                continue
            field, value, snippet = matched

            if not value:
                # Table layout: the value sits on the next non-empty line.
                following = _next_content_line(lines, index + 1)
                if following is None:
                    continue
                value = following.strip()
                snippet = following.strip()

            if answer[field]["value"] is not None:
                # First occurrence wins, as a reader would take it.
                continue
            if value.strip().lower() in _EMPTY_VALUES:
                continue

            if field == "programme":
                parts = [part.strip() for part in value.split(",") if part.strip()]
                answer[field] = _entry(parts or None, snippet, page)
            else:
                answer[field] = _entry(value.strip(), snippet, page)

        # The issuing body is usually the letterhead rather than a labelled
        # line. Taken only when the first content line says so explicitly.
        if answer["issuing_body"]["value"] is None:
            for raw in lines:
                line = raw.strip()
                if not line or _PAGE_MARKER.match(line):
                    continue
                if re.search(r"\b(board|council|accreditation|authority)\b", line, re.I):
                    answer["issuing_body"] = _entry(line, line, 1, confidence="medium")
                break

        return answer

    def _match_label(self, line: str) -> tuple[str, str, str] | None:
        for label, field in self._labels:
            pattern = rf"^{re.escape(label)}\s*[:.\-]?\s*(.*)$"
            found = re.match(pattern, line, flags=re.IGNORECASE)
            if found is None:
                continue
            value = found.group(1).strip()
            # "Certificate Number: X" and a table cell "Certificate Number   X"
            # both land here; a label that is merely the start of a longer
            # word ("Grade" in "Graded ...") does not, thanks to the boundary.
            boundary = line[len(label) : len(label) + 1]
            if boundary and boundary.isalnum():
                continue
            return field, value, line
        return None


def _entry(value: Any, snippet: str, page: int, confidence: str = "high") -> dict[str, Any]:
    return {
        "value": value,
        "source_snippet": snippet,
        "page": page,
        "confidence": confidence,
    }


def _null() -> dict[str, Any]:
    return {"value": None, "source_snippet": None, "page": None, "confidence": None}


def _next_content_line(lines: list[str], start: int) -> str | None:
    for raw in lines[start : start + 3]:
        if raw.strip() and not _PAGE_MARKER.match(raw):
            return raw
    return None


def _document_text(prompt: str) -> str:
    index = prompt.find(_TEXT_MARKER)
    if index < 0:
        return prompt
    return prompt[index + len(_TEXT_MARKER) :]
