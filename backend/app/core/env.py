"""Minimal .env loading.

Keys live in a .env file at the repository root, which is gitignored. Loaded
without a dependency, and without overriding anything already set in the real
environment: an explicitly exported variable always wins over the file.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    # backend/app/core/env.py -> repository root
    return Path(__file__).resolve().parents[3]


def load_dotenv(path: str | Path | None = None, override: bool = False) -> list[str]:
    """Read KEY=value lines into os.environ.

    Returns the names of the variables that were set, never their values, so
    this can be logged safely. A missing file is not an error.
    """
    target = Path(path) if path is not None else repo_root() / ".env"
    try:
        content = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []

    loaded: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if not name:
            continue
        # Strip surrounding quotes, and any stray carriage return from a file
        # written with Windows line endings.
        value = value.strip().strip("\r")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded
