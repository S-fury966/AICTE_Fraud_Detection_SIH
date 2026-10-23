"""YAML configuration loading.

Config is read from the repository `config/` directory and cached. Every loaded
config carries a `config_version` that callers stamp onto their results, so an
evaluation can be traced back to the settings that produced it.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_ENV_CONFIG_DIR = "SIH_CONFIG_DIR"


class ConfigError(RuntimeError):
    """Raised when a config file is missing, unreadable or malformed."""


def config_dir() -> Path:
    """Directory holding the YAML config files.

    Overridable with the SIH_CONFIG_DIR environment variable so tests and
    alternate deployments can point somewhere else.
    """
    override = os.environ.get(_ENV_CONFIG_DIR)
    if override:
        return Path(override)
    # backend/app/core/config.py -> repository root
    return Path(__file__).resolve().parents[3] / "config"


@lru_cache(maxsize=None)
def load_config(name: str) -> dict[str, Any]:
    """Load `config/<name>.yaml` and return it as a dict.

    Results are cached per name. Call `load_config.cache_clear()` after editing
    a file on disk.
    """
    path = config_dir() / f"{name}.yaml"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a YAML mapping")
    return data


def get(config: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """Read a nested value, e.g. get(cfg, "pdf.text_layer.min_chars")."""
    node: Any = config
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
