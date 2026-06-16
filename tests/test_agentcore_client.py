import base64
import os
import sys
import time
from types import SimpleNamespace

import jwt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agentcore_client import (  # noqa: E402
    AgentCoreConfig,
    current_gateway_claims,
    get_agentcore_config,
    mint_gateway_assertion,
)


def test_get_agentcore_config_accepts_base64_gateway_key(monkeypatch):
    raw_key = b"x" * 32
    monkeypatch.setenv("AGENTCORE_BASE_URL", "https://agentcore.internal/")
    monkeypatch.setenv("AGENTCORE_GATEWAY_SIGNING_KEY", base64.b64encode(raw_key).decode())
    monkeypatch.setenv("AGENTCORE_GATEWAY_ISSUER", "https://cowork.circit.ai")
    monkeypatch.setenv("AGENTCORE_GATEWAY_AUDIENCE", "api://agentcore")

    cfg = get_agentcore_config()

    assert cfg.base_url == "https://agentcore.internal"
    assert cfg.signing_key == raw_key
    assert cfg.issuer == "https://cowork.circit.ai"
    assert cfg.audience == "api://agentcore"


def test_mint_gateway_assertion_contains_user_claims():
    cfg = AgentCoreConfig(
        base_url="https://agentcore.internal",
        audience="api://agentcore",
        issuer="https://cowork.circit.ai",
        signing_key=b"shared-secret-for-test-0123456789",
    )

    token = mint_gateway_assertion(
        cfg,
        {"oid": "cf-sub", "email": "jakub.sikora@circit.io", "preferred_username": "jakub.sikora@circit.io"},
    )
    claims = jwt.decode(
        token,
        key=cfg.signing_key,
        algorithms=["HS256"],
        audience="api://agentcore",
        issuer="https://cowork.circit.ai",
    )

    assert claims["oid"] == "cf-sub"
    assert claims["email"] == "jakub.sikora@circit.io"
    assert claims["exp"] > int(time.time())


def test_current_gateway_claims_prefers_cloudflare_identity():
    request = SimpleNamespace(
        state=SimpleNamespace(
            cloudflare_access_user="Jakub.Sikora@Circit.IO",
            cloudflare_access_sub="cf-user-subject",
            current_user="other@circit.io",
        )
    )

    claims = current_gateway_claims(request)

    assert claims["oid"] == "cf-user-subject"
    assert claims["email"] == "jakub.sikora@circit.io"


def test_get_agentcore_config_requires_base_url(monkeypatch):
    monkeypatch.delenv("AGENTCORE_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        get_agentcore_config()
