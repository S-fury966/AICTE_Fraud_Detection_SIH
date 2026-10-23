"""Loading the synthetic registry and looking records up.

Separate from the service so tests and the seed script can use it without
starting an app, and so the lookup logic is not entangled with HTTP.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .seed import REGISTRY_PATH, build_registry, registry_hash


@dataclass
class Registry:
    data: dict[str, Any]
    source_path: str | None = None

    @property
    def version(self) -> str:
        return str(self.data.get("registry_version", "unknown"))

    @property
    def sha256(self) -> str:
        return registry_hash(self.data)

    @property
    def records(self) -> list[dict[str, Any]]:
        return list(self.data.get("records", []))

    @property
    def pending_ids(self) -> set[str]:
        return {str(value) for value in self.data.get("pending_certificate_ids", [])}

    def find(self, certificate_id: str) -> dict[str, Any] | None:
        """Exact lookup by certificate id.

        Exact, deliberately. A registry that answered fuzzy lookups would hand
        back someone else's record for an altered number, which is the single
        thing this detector exists to catch.
        """
        wanted = (certificate_id or "").strip()
        if not wanted:
            return None
        for record in self.records:
            if str(record.get("certificate_id", "")) == wanted:
                return dict(record)
        return None


def load_registry(path: str | Path | None = None) -> Registry:
    """Read the registry from disk, falling back to building it in memory.

    The fallback keeps the service and the tests working on a fresh checkout
    where nobody has run the seed script yet. The data is identical either
    way: the seed is fixed.
    """
    target = Path(path) if path is not None else REGISTRY_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return Registry(data=data, source_path=str(target))
    except (OSError, ValueError):
        return Registry(data=build_registry(), source_path=None)
