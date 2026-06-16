"""In-process typed principal for the agent tool layer (P3.5).

Replaces the loopback ``INTERNAL_TOOL_TOKEN`` + ``X-Odysseus-Owner`` header
impersonation (``app.py`` auth middleware, ``core/middleware.py`` ``require_admin``).

The old mechanism let the tool layer HTTP-loopback to admin-gated routes carrying
a shared per-process token and an attacker-controllable ``X-Odysseus-Owner`` header
— a one-request path to impersonate any user and pass ``require_admin``. The fix is
to stop loopbacking entirely: tools call the in-process service layer directly,
carrying THIS typed principal instead of forging an HTTP identity.

This module is the seam. The matching removals (kill the impersonation branch +
the token) are owned by P1.1 (gateway/trust). A red-until-P1.1 test asserts the
old mechanism is gone; see ``tests/test_internal_principal_seam.py``.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class Principal:
    """A typed, in-process identity. No HTTP header, no shared token.

    ``user_id`` is the authenticated user (Entra ``oid`` once P1.2 lands; the
    bcrypt username before that). ``team`` carries the RBAC team (P1.3).
    ``is_admin`` is an explicit capability, never inferred from a loopback token.
    """

    user_id: str
    team: Optional[str] = None
    is_admin: bool = False
    scopes: FrozenSet[str] = field(default_factory=frozenset)

    def with_scopes(self, *scopes: str) -> "Principal":
        return Principal(
            user_id=self.user_id,
            team=self.team,
            is_admin=self.is_admin,
            scopes=frozenset(self.scopes | set(scopes)),
        )


# The principal flows through async tool calls via a context var — set once when
# a request/run is admitted, read by the tool layer. It is NEVER derived from a
# request header inside the tool layer.
_current_principal: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar(
    "odysseus_current_principal", default=None
)


def set_principal(principal: Principal) -> contextvars.Token:
    """Bind the principal for the current async context. Returns a reset token."""
    return _current_principal.set(principal)


def reset_principal(token: contextvars.Token) -> None:
    _current_principal.reset(token)


def current_principal() -> Optional[Principal]:
    """The principal bound to this async context, or None if unauthenticated."""
    return _current_principal.get()


def require_principal() -> Principal:
    """Fail-closed accessor: raise if no principal is bound (never assume admin)."""
    p = _current_principal.get()
    if p is None:
        raise PermissionError(
            "No in-process principal bound. The tool layer must run under an "
            "explicit Principal (P3.5) — it must not fall back to header/token "
            "impersonation."
        )
    return p
