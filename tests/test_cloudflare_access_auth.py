"""Cloudflare Access auth mode regression tests."""

import datetime as dt
import os
import sys
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cloudflare_access import (  # noqa: E402
    CloudflareAccessError,
    CloudflareAccessVerifier,
    is_cloudflare_access_issuer,
)
from core.middleware import require_admin  # noqa: E402


ISSUER = "https://circit.cloudflareaccess.com"
AUDIENCE = "cloudflare-access-app-aud"
SECRET = "cf-access-test-secret-not-for-production-0123456789"


def _mint(claims, *, exp_minutes=5, issuer=ISSUER, audience=AUDIENCE, secret=SECRET):
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now - dt.timedelta(minutes=1),
        "exp": now + dt.timedelta(minutes=exp_minutes),
        "sub": "cf-user-subject",
        "email": "alice@circit.io",
        **claims,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _verifier(**overrides):
    return CloudflareAccessVerifier(
        audience=AUDIENCE,
        issuer=ISSUER,
        allowed_email_domain="circit.io",
        signing_key=SECRET,
        algorithms=["HS256"],
        **overrides,
    )


def test_valid_cloudflare_access_token_maps_to_circit_email():
    principal = _verifier().verify(_mint({
        "email": "Alice@Circit.IO",
        "name": "Alice Example",
        "picture": "https://login.circit.test/alice.jpg",
    }))
    assert principal.email == "alice@circit.io"
    assert principal.subject == "cf-user-subject"
    assert principal.display_name == "Alice Example"
    assert principal.avatar_url == "https://login.circit.test/alice.jpg"


def test_cloudflare_access_display_name_falls_back_to_email_local_part():
    principal = _verifier().verify(_mint({"email": "jakub.sikora@circit.io"}))
    assert principal.display_name == "Jakub Sikora"


def test_missing_cloudflare_access_token_is_rejected():
    with pytest.raises(CloudflareAccessError):
        _verifier().verify("")


def test_bad_audience_is_rejected():
    token = _mint({}, audience="other-access-aud")
    with pytest.raises(CloudflareAccessError):
        _verifier().verify(token)


def test_expired_token_is_rejected():
    token = _mint({}, exp_minutes=-10)
    with pytest.raises(CloudflareAccessError):
        _verifier().verify(token)


def test_non_circit_identity_is_rejected():
    token = _mint({"email": "mallory@example.com"})
    with pytest.raises(CloudflareAccessError):
        _verifier().verify(token)


def test_allowlisted_service_token_maps_to_configured_identity():
    client_id = "e367826f93b8d71185e03fe518aff3b4.access"
    token = _mint({
        "email": None,
        "sub": "",
        "common_name": client_id,
    })

    principal = _verifier(
        service_token_identities={client_id: "e2e.smoke@circit.io"}
    ).verify(token)

    assert principal.email == "e2e.smoke@circit.io"
    assert principal.subject == f"service-token:{client_id}"
    assert principal.display_name == "E2e Smoke"


def test_unmapped_service_token_is_rejected():
    token = _mint({
        "email": None,
        "sub": "",
        "common_name": "unknown.access",
    })

    with pytest.raises(CloudflareAccessError):
        _verifier().verify(token)


def test_wrong_issuer_is_rejected():
    token = _mint({}, issuer="https://evil.cloudflareaccess.com")
    with pytest.raises(CloudflareAccessError):
        _verifier().verify(token)


def test_from_env_builds_team_domain_jwks(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCESS_AUD", AUDIENCE)
    monkeypatch.setenv("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "circit.cloudflareaccess.com")
    monkeypatch.setenv("ODYSSEUS_ALLOWED_EMAIL_DOMAIN", "circit.io")

    verifier = CloudflareAccessVerifier.from_env()
    assert verifier.issuer == ISSUER
    assert verifier.jwks_url == f"{ISSUER}/cdn-cgi/access/certs"


def test_cloudflare_access_issuer_recognizer():
    assert is_cloudflare_access_issuer("circit.cloudflareaccess.com")
    assert not is_cloudflare_access_issuer("login.microsoftonline.com/circit/v2.0")


def _cloudflare_request(email: str):
    return SimpleNamespace(
        headers={},
        state=SimpleNamespace(auth_mode="cloudflare_access", current_user=email),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def test_require_admin_allows_configured_cloudflare_admin(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_CLOUDFLARE_ADMIN_EMAILS", "jakub.sikora@circit.io")

    require_admin(_cloudflare_request("Jakub.Sikora@Circit.IO"))


def test_require_admin_rejects_unlisted_cloudflare_user(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_CLOUDFLARE_ADMIN_EMAILS", "jakub.sikora@circit.io")

    with pytest.raises(HTTPException) as exc:
        require_admin(_cloudflare_request("alice@circit.io"))

    assert exc.value.status_code == 403
