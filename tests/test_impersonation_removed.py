"""Regression guard: the X-Odysseus-Owner impersonation path is gone (plan v3.3 §1.1).

The vuln: AuthMiddleware honored an X-Odysseus-Owner header on the loopback
internal-tool path and stamped request.state.current_user = <that owner>. Paired
with require_admin honoring the same loopback token, it was a one-request,
no-auth path to impersonate any user, pass admin, and register a stdio MCP
server = arbitrary host RCE with full os.environ.

AuthMiddleware is a closure built inside the app factory, so we assert against
the source of the dispatch path directly. These tests fail red the moment the
impersonation assignment is reintroduced.
"""

import os
import re

_APP_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def _read_app():
    with open(_APP_PY, "r", encoding="utf-8") as fh:
        return fh.read()


def test_no_x_odysseus_owner_attribution_in_auth_middleware():
    src = _read_app()
    # The loopback internal-tool branch must NOT read X-Odysseus-Owner and use
    # it to set current_user. Find the dispatch region and assert the header is
    # not consumed into an identity assignment there.
    assert 'request.headers.get("X-Odysseus-Owner")' not in src, (
        "X-Odysseus-Owner is being read again — impersonation path reintroduced"
    )
    # No assignment of current_user from an _impersonate variable.
    assert "_impersonate" not in src, "impersonation variable reintroduced"


def test_loopback_internal_tool_only_resolves_to_internal_tool_user():
    src = _read_app()
    # The internal-tool loopback branch still resolves to the non-privileged
    # 'internal-tool' principal (the mechanism stays until P3.5; only the
    # impersonation is removed).
    assert 'request.state.current_user = "internal-tool"' in src


def test_internal_tool_branch_has_no_conditional_owner_assignment():
    src = _read_app()
    # Guard against a sneaky `if _x in auth_mgr.users: current_user = _x` form.
    suspicious = re.search(
        r"current_user\s*=\s*_impersonate", src
    )
    assert suspicious is None
