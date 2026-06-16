"""P3.5 — in-process typed principal seam + red-until-P1.1 impersonation removal.

The typed principal (``src/internal_principal.py``) is the replacement for the
``INTERNAL_TOOL_TOKEN`` + ``X-Odysseus-Owner`` loopback impersonation. The actual
removal of the impersonation branch is owned by P1.1; the xfail test below tracks
that removal — it currently xfails because the mechanism is still present, and
will turn into an XPASS (failing the strict marker) the moment P1.1 deletes it,
forcing this test to be promoted to a plain assertion.
"""

import pathlib

import pytest

from src.internal_principal import (
    Principal,
    current_principal,
    require_principal,
    reset_principal,
    set_principal,
)

_REPO = pathlib.Path(__file__).resolve().parents[1]


def test_principal_is_typed_and_explicit():
    p = Principal(user_id="alice", team="compliance", is_admin=False)
    assert p.user_id == "alice"
    assert p.team == "compliance"
    assert p.is_admin is False
    # admin is never inferred — it is an explicit field
    assert Principal(user_id="bob").is_admin is False


def test_principal_scopes_compose():
    p = Principal(user_id="alice").with_scopes("read:notes")
    p2 = p.with_scopes("write:calendar")
    assert "read:notes" in p2.scopes
    assert "write:calendar" in p2.scopes


def test_context_var_binding_roundtrip():
    assert current_principal() is None
    tok = set_principal(Principal(user_id="carol"))
    try:
        assert current_principal().user_id == "carol"
    finally:
        reset_principal(tok)
    assert current_principal() is None


def test_require_principal_fails_closed_when_unbound():
    # No fallback to a forged/loopback identity — unauthenticated raises.
    with pytest.raises(PermissionError):
        require_principal()


@pytest.mark.xfail(
    reason="RED-UNTIL P1.1: X-Odysseus-Owner impersonation + INTERNAL_TOOL_TOKEN "
    "still present. When P1.1 removes them, this XPASSes (strict) — promote to a "
    "plain assertion and delete the marker.",
    strict=True,
)
def test_impersonation_mechanism_is_removed():
    middleware_src = (_REPO / "core" / "middleware.py").read_text(encoding="utf-8")
    app_src = (_REPO / "app.py").read_text(encoding="utf-8")

    # The loopback impersonation primitives must be gone.
    assert "INTERNAL_TOOL_TOKEN" not in middleware_src
    assert "X-Odysseus-Owner" not in app_src
    assert "X-Odysseus-Internal-Token" not in middleware_src
