"""Versioned prompt loading.

Prompts live in files, not in code, so a prompt can be reviewed and changed
without touching logic. The version is recorded on every result: an evaluation
must be traceable to the exact prompt that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class PromptError(RuntimeError):
    """Raised when a prompt file is missing or unreadable."""


@dataclass(frozen=True)
class Prompt:
    """A prompt template and the identity to record alongside its output."""

    name: str
    version: str
    template: str

    @property
    def prompt_version(self) -> str:
        """The identifier stored in the audit trail, e.g. 'certificate_extraction.v1'."""
        return f"{self.name}.{self.version}"

    def render(self, **values: str) -> str:
        """Substitute {{PLACEHOLDER}} markers.

        Deliberately not str.format: the template contains JSON braces, which
        format() would try to interpret.
        """
        text = self.template
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        return text


@lru_cache(maxsize=None)
def load_prompt(name: str, version: str) -> Prompt:
    """Load `prompts/<name>.<version>.md`."""
    path = PROMPT_DIR / f"{name}.{version}.md"
    try:
        template = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(f"prompt file not found: {path}") from exc
    except OSError as exc:
        raise PromptError(f"could not read prompt file {path}: {exc}") from exc
    return Prompt(name=name, version=version, template=template)
