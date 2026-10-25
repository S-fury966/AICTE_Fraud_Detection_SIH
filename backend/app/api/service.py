"""What the API needs that is not HTTP: the pipeline, identity, permissions.

One application at a time (a confirmed decision): submissions and re-runs take
a process-wide lock, so two uploads never interleave their stages. There is no
job queue and no background worker - a submission returns when its checks have
finished, which on the demo set is well under two seconds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from ..core.config import get, load_config
from ..core.env import load_dotenv
from ..db.roles import UnknownRoleError, validate_role
from ..pipeline.runner import Pipeline
from .sessions import SessionError, Sessions


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@dataclass
class Identity:
    actor: str
    role: str


class Service:
    def __init__(
        self,
        pipeline: Pipeline | None = None,
        api_config: dict[str, Any] | None = None,
    ) -> None:
        load_dotenv()
        self.config = api_config if api_config is not None else load_config("api")
        self.pipeline = pipeline if pipeline is not None else self._build_pipeline()
        self.lock = threading.Lock()
        self.sessions = Sessions(self.config)

    def _build_pipeline(self) -> Pipeline:
        transport = None
        registry_hash = None
        if bool(get(self.config, "issuer.in_process", True)):
            from fastapi.testclient import TestClient

            from ..mock_issuer.registry import load_registry
            from ..mock_issuer.service import create_app

            registry = load_registry()
            registry_hash = registry.sha256
            transport = TestClient(create_app(registry=registry))
        return Pipeline(
            issuer_transport=transport,
            registry_hash=registry_hash,
            as_of=get(self.config, "as_of"),
        )

    @property
    def database(self):
        return self.pipeline.database

    @property
    def persistence(self) -> dict[str, Any]:
        return self.pipeline.persistence

    # ------------------------------------------------------------------
    # Identity and permissions
    # ------------------------------------------------------------------

    def identity(self, request: Request) -> Identity:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            # A signed-in session: the token, not any header, says who this is.
            try:
                session = self.sessions.decode(authorization[7:].strip())
            except SessionError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            return Identity(actor=session.actor, role=session.role)
        actor = request.headers.get(str(get(self.config, "identity.actor_header", "X-Actor"))) or str(
            get(self.config, "identity.default_actor", "officer.demo")
        )
        role = request.headers.get(str(get(self.config, "identity.role_header", "X-Role"))) or str(
            get(self.config, "identity.default_role", "officer")
        )
        actor = actor.strip()[:128]
        try:
            role = validate_role(role, self.persistence)
        except UnknownRoleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if role == "system":
            # The pipeline's own role; a person cannot assert it.
            raise HTTPException(status_code=403, detail="the 'system' role is reserved for the pipeline")
        if self.sessions.requires_sign_in(role):
            raise HTTPException(status_code=401, detail=f"the {role.replace('_', ' ')} role requires sign-in with its passcode")
        return Identity(actor=actor or "anonymous", role=role)

    def require(self, permission: str, who: Identity) -> None:
        allowed = [str(r) for r in (get(self.config, f"permissions.{permission}", []) or [])]
        if who.role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"role {who.role!r} may not {permission.replace('_', ' ')}; allowed: {', '.join(allowed)}",
            )

    def path_from_config(self, key: str, default: str) -> Path:
        raw = str(get(self.config, key, default))
        path = Path(raw)
        return path if path.is_absolute() else repo_root() / path
