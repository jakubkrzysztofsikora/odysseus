"""Unit tests for the gateway signed-JWT-assertion verifier (P1.1/P1.2).

Uses an in-test HS256 secret and an RS256 keypair as the "mocked JWKS" — no
live Entra tenant. Proves: valid assertion -> typed CallerPrincipal; every
tamper/mismatch/expiry -> GatewayAssertionError (fail-closed); the pre-Entra
constant identity is rejected; no trust material -> reject everything.
"""

import os
import sys
import datetime as dt

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util  # noqa: E402

import jwt  # noqa: E402

# Load core/gateway_assertion.py directly by path so importing it does NOT
# trigger core/__init__.py (which eagerly imports the heavy SQLAlchemy ORM
# chain). The verifier has no core dependencies of its own.
_GA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core",
    "gateway_assertion.py",
)
_spec = importlib.util.spec_from_file_location("circit_gateway_assertion", _GA_PATH)
_ga = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve cls.__module__ in sys.modules.
sys.modules["circit_gateway_assertion"] = _ga
_spec.loader.exec_module(_ga)
GatewayAssertionVerifier = _ga.GatewayAssertionVerifier
GatewayAssertionError = _ga.GatewayAssertionError
LEGACY_CONSTANT_IDENTITY = _ga.LEGACY_CONSTANT_IDENTITY

ISSUER = "https://login.microsoftonline.com/circit-tenant/v2.0"
AUDIENCE = "api://odysseus"
SECRET = "test-shared-secret-not-for-prod-0123456789abcdef"  # >=32 bytes for SHA256


def _mint(claims, *, exp_minutes=5, issuer=ISSUER, audience=AUDIENCE, secret=SECRET, alg="HS256"):
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now - dt.timedelta(minutes=1),
        "exp": now + dt.timedelta(minutes=exp_minutes),
        **claims,
    }
    return jwt.encode(payload, secret, algorithm=alg)


def _verifier(**kw):
    return GatewayAssertionVerifier(
        issuer=ISSUER, audience=AUDIENCE, signing_key=SECRET, algorithms=["HS256"], **kw
    )


def test_valid_assertion_yields_typed_principal():
    token = _mint({"oid": "oid-A", "preferred_username": "alice@circit.ie", "team": "compliance"})
    p = _verifier().verify(token)
    assert p.oid == "oid-A"
    assert p.user == "alice@circit.ie"
    assert p.team == "compliance"


def test_empty_token_fails_closed():
    with pytest.raises(GatewayAssertionError):
        _verifier().verify("")


def test_wrong_issuer_rejected():
    token = _mint({"oid": "x"}, issuer="https://evil.example/v2.0")
    with pytest.raises(GatewayAssertionError):
        _verifier().verify(token)


def test_wrong_audience_rejected():
    token = _mint({"oid": "x"}, audience="api://other")
    with pytest.raises(GatewayAssertionError):
        _verifier().verify(token)


def test_expired_assertion_rejected():
    token = _mint({"oid": "x"}, exp_minutes=-10)
    with pytest.raises(GatewayAssertionError):
        _verifier().verify(token)


def test_token_signed_by_unknown_secret_rejected():
    token = _mint({"oid": "x"}, secret="attacker-secret")
    with pytest.raises(GatewayAssertionError):
        _verifier().verify(token)


def test_assertion_without_oid_rejected():
    token = _mint({"preferred_username": "nobody"})
    with pytest.raises(GatewayAssertionError):
        _verifier().verify(token)


def test_legacy_constant_identity_subject_rejected():
    token = _mint({"oid": LEGACY_CONSTANT_IDENTITY})
    with pytest.raises(GatewayAssertionError):
        _verifier().verify(token)


def test_no_trust_material_rejects_everything():
    v = GatewayAssertionVerifier(issuer=ISSUER, audience=AUDIENCE)  # no key, no jwks
    assert v.has_trust_material is False
    with pytest.raises(GatewayAssertionError):
        v.verify(_mint({"oid": "x"}))


def test_groups_claim_collapses_to_team():
    token = _mint({"oid": "oid-A", "groups": ["team-1", "team-2"]})
    p = _verifier().verify(token)
    assert p.team == "team-1"


def test_rs256_keypair_path():
    # Prove the RS256 (asymmetric) path the production JWKS uses also verifies.
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = _mint({"oid": "oid-R", "preferred_username": "rsa@circit.ie"}, secret=priv_pem, alg="RS256")
    v = GatewayAssertionVerifier(
        issuer=ISSUER, audience=AUDIENCE, signing_key=pub_pem, algorithms=["RS256"]
    )
    p = v.verify(token)
    assert p.oid == "oid-R"
