"""Content-addressed file storage.

Uploads are copied into `data/store/<ab>/<sha256><ext>`; the database keeps the
hash and the path, never the bytes. Two consequences, both wanted:

- **Identical files share one stored copy.** The same certificate submitted by
  two colleges is stored once, and the duplicate is visible as a shared hash
  rather than having to be discovered later.
- **The path is derived from the content.** A stored file cannot be silently
  swapped for different bytes without the name ceasing to match, which
  `verify_stored_file` checks.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import get, load_config


@dataclass
class StoredFile:
    sha256: str
    path: Path
    size_bytes: int
    # False when an identical file was already stored.
    newly_stored: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "newly_stored": self.newly_stored,
        }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def store_root(config: dict[str, Any] | None = None) -> Path:
    cfg = config if config is not None else load_config("persistence")
    raw = str(get(cfg, "storage.root", "data/store"))
    root = Path(raw)
    if not root.is_absolute():
        root = repo_root() / root
    return root


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """SHA-256 and size, read in chunks so a large scan is not loaded at once."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def store_file(
    source: str | Path, config: dict[str, Any] | None = None
) -> StoredFile:
    cfg = config if config is not None else load_config("persistence")
    origin = Path(source)
    if not origin.exists():
        raise FileNotFoundError(f"no such file: {origin}")

    digest, size = hash_file(origin)
    fanout = int(get(cfg, "storage.fanout_chars", 2))
    root = store_root(cfg)
    # The extension is kept so a stored file is still openable by name; it is
    # not part of the address.
    target = root / digest[:fanout] / f"{digest}{origin.suffix.lower()}"

    if target.exists():
        return StoredFile(
            sha256=digest, path=target, size_bytes=size, newly_stored=False
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origin, target)
    return StoredFile(sha256=digest, path=target, size_bytes=size, newly_stored=True)


def verify_stored_file(path: str | Path, expected_sha256: str) -> bool:
    target = Path(path)
    if not target.exists():
        return False
    digest, _ = hash_file(target)
    return digest == expected_sha256
