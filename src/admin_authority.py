"""Server-derived primary-admin capability for privileged dormant writes.

The stable actor id is audit identity, not authentication input.  A capability
can only be minted from an already authenticated server-side AdminSession whose
login is explicitly mapped by protected configuration.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass

from .security import is_server_authenticated_admin_session


class PrimaryAdminAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class PrimaryAdminCapability:
    actor_id: str
    _seal: str


class PrimaryAdminAuthority:
    def __init__(self, actor_id: str, admin_login: str):
        self._actor_id = (actor_id or "").strip()
        self._admin_login = (admin_login or "").strip()
        self._seal = secrets.token_urlsafe(32)

    @property
    def enabled(self) -> bool:
        return bool(self._actor_id and self._admin_login)

    def authorize_session(self, session) -> PrimaryAdminCapability:
        username = getattr(session, "username", "")
        if (
            not self.enabled
            or not is_server_authenticated_admin_session(session)
            or not isinstance(username, str)
            or not hmac.compare_digest(username, self._admin_login)
        ):
            raise PrimaryAdminAuthorizationError(
                "authenticated primary MGBoost admin session required"
            )
        return PrimaryAdminCapability(self._actor_id, self._seal)

    def require(self, capability: PrimaryAdminCapability) -> str:
        if (
            not self.enabled
            or not isinstance(capability, PrimaryAdminCapability)
            or not hmac.compare_digest(capability.actor_id, self._actor_id)
            or not hmac.compare_digest(capability._seal, self._seal)
        ):
            raise PrimaryAdminAuthorizationError(
                "server-derived primary MGBoost admin capability required"
            )
        return self._actor_id
