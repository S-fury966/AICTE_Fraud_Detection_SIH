"""Shared fixtures for the extraction tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.env import load_dotenv  # noqa: E402
from tests.make_fixtures import build_all  # noqa: E402

# Keys for the live-API tests come from the repository .env file.
load_dotenv()


def live_providers() -> list[str]:
    """The provider(s) the live tests should exercise.

    By default only the one the app is configured to use, because that is the
    one whose behaviour matters. Testing every provider that happens to have a
    key in .env sounds thorough but is not: a key for an endpoint nothing calls
    can redden the suite indefinitely, and a suite that is always red stops
    being read. Set SIH_LIVE_PROVIDERS=groq,mistral to check others on demand.

    A provider named here with no key is dropped, which the fixture turns into
    a skip. One with a key that does not work is left in, so it fails loudly.
    """
    import os

    from app.core.config import load_config

    llm = load_config("certificate_extraction").get("llm", {}) or {}
    settings = llm.get("providers", {}) or {}

    requested = os.environ.get("SIH_LIVE_PROVIDERS", "")
    names = (
        [n.strip() for n in requested.split(",") if n.strip()]
        if requested
        else [str(llm.get("provider", ""))]
    )

    found = []
    for name in names:
        env_name = (settings.get(name) or {}).get("api_key_env")
        if env_name and os.environ.get(env_name):
            found.append(name)
    return found


@pytest.fixture(scope="session")
def samples() -> dict[str, Path]:
    """Build the synthetic sample files once per test session."""
    return build_all()


@pytest.fixture
def config() -> dict:
    """The real extraction config, so tests exercise the shipped thresholds."""
    from app.core.config import load_config

    return load_config("extraction")
