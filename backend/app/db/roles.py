"""Actor and role validation.

There is no login in the demo (a confirmed decision), so a role is asserted by
the caller rather than proven. That makes validation against the config list
the only guard there is: it cannot stop a caller claiming `admin`, but it does
stop a typo inventing a role that no policy covers and that no later
authorisation check would recognise.
"""

from __future__ import annotations

from typing import Any

from ..core.config import get, load_config

SYSTEM_ACTOR = "pipeline"
SYSTEM_ROLE = "system"


class UnknownRoleError(ValueError):
    """The role is not in the configured list."""


def allowed_roles(config: dict[str, Any] | None = None) -> list[str]:
    cfg = config if config is not None else load_config("persistence")
    return [str(role) for role in (get(cfg, "roles.allowed", []) or [])]


def validate_role(role: str, config: dict[str, Any] | None = None) -> str:
    roles = allowed_roles(config)
    candidate = str(role or "").strip()
    if candidate not in roles:
        raise UnknownRoleError(
            f"{role!r} is not a known role; allowed: {', '.join(roles)}"
        )
    return candidate


def validate_actor(actor: str) -> str:
    candidate = str(actor or "").strip()
    if not candidate:
        raise ValueError("an actor is required for every write")
    return candidate
