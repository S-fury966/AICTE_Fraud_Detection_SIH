"""On-disk response cache, as a provider decorator.

Free tiers have daily token caps, and an evaluation run over sixty
applications would spend the whole day's allowance on prompts it has already
sent once. This wraps any provider and keeps responses on disk, so a re-run
of the same set costs nothing.

**The key is the thing that decides correctness.** It covers the exact prompt
text, the model, the provider and the prompt version. Leave any of those out
and a cached answer will eventually be served for a different question:

- the prompt text, because that is the question;
- the model, because two models answer the same prompt differently;
- the provider, because the same model name on two endpoints is not the same
  deployment;
- the prompt version, because step 2's prompt is versioned precisely so a
  change to it invalidates earlier results — a cache that ignored it would
  silently keep returning answers to the old prompt.

Temperature is deliberately *not* in the key. Config pins it at 0, and
including it would invite someone to read a non-zero-temperature cache as
reproducible when it is only the first sample.

A cache hit is recorded in `metadata["cache_hit"]`, which flows through the
provider's completion into step 2's audit record, so a stored run always says
whether the model was actually asked.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .base import LLMCompletion, LLMProvider


class CachingProvider:
    """Wraps a provider and serves repeated prompts from disk."""

    def __init__(
        self,
        inner: LLMProvider,
        root: str | Path,
        prompt_version: str,
        enabled: bool = True,
        read_only: bool = False,
    ) -> None:
        self._inner = inner
        self._root = Path(root)
        self._prompt_version = str(prompt_version)
        self._enabled = bool(enabled)
        # For a measurement run that must not add to the cache.
        self._read_only = bool(read_only)

        self.hits = 0
        self.misses = 0
        self.writes = 0

    # The wrapped provider's identity is the one that matters to callers: a
    # cached Groq response is still a Groq response.
    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "unknown")

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "unknown")

    @property
    def inner(self) -> LLMProvider:
        return self._inner

    def key_for(self, prompt: str) -> str:
        """The cache key. See the module docstring for why each part is in it."""
        payload = json.dumps(
            {
                "provider": self.name,
                "model": self.model,
                "prompt_version": self._prompt_version,
                "prompt": prompt,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def path_for(self, prompt: str) -> Path:
        key = self.key_for(prompt)
        # Two-character fan-out, same shape as the document store, so one
        # directory does not accumulate thousands of entries.
        return self._root / key[:2] / f"{key}.json"

    def complete(self, prompt: str) -> LLMCompletion:
        if not self._enabled:
            return self._inner.complete(prompt)

        path = self.path_for(prompt)
        cached = self._read(path)
        if cached is not None:
            self.hits += 1
            return cached

        self.misses += 1
        completion = self._inner.complete(prompt)
        if not self._read_only:
            self._write(path, prompt, completion)
        return completion

    # ------------------------------------------------------------------

    def _read(self, path: Path) -> LLMCompletion | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return None
        try:
            entry = json.loads(raw)
        except ValueError:
            # A truncated or hand-edited entry is treated as absent rather
            # than raising: a damaged cache must never fail a run, it should
            # only cost a call.
            return None

        metadata = dict(entry.get("metadata") or {})
        metadata["cache_hit"] = True
        return LLMCompletion(
            text=str(entry.get("text", "")),
            model=str(entry.get("model", self.model)),
            provider=str(entry.get("provider", self.name)),
            input_tokens=entry.get("input_tokens"),
            output_tokens=entry.get("output_tokens"),
            metadata=metadata,
        )

    def _write(self, path: Path, prompt: str, completion: LLMCompletion) -> None:
        entry: dict[str, Any] = {
            "text": completion.text,
            "model": completion.model,
            "provider": completion.provider,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "metadata": {
                **(completion.metadata or {}),
                # Recorded so a cached entry can be traced back, without
                # storing the prompt itself: the prompt contains document text.
                "prompt_sha256": hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest(),
                "prompt_version": self._prompt_version,
            },
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written through a temporary file in the same directory and then
            # renamed, so an interrupted run cannot leave a half-written entry
            # that a later run would read as a real answer.
            handle, temporary = tempfile.mkstemp(dir=str(path.parent))
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(entry, stream, ensure_ascii=False)
            os.replace(temporary, path)
            self.writes += 1
        except OSError:
            # An unwritable cache costs money, not correctness.
            pass

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "read_only": self._read_only,
            "root": str(self._root),
            "prompt_version": self._prompt_version,
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
        }
