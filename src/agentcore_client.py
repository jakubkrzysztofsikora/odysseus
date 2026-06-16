"""Client helpers for the private Circit AgentCore backend.

The browser never calls AgentCore directly. Odysseus validates the user at the
Cloudflare Access edge, then mints a short-lived gateway assertion for the
internal Container Apps hop.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Mapping

import httpx
import jwt
from fastapi import HTTPException, Request


DEFAULT_AGENTCORE_AUDIENCE = "api://0a253486-d356-462e-bdcb-f057f1535ef7"
DEFAULT_AGENTCORE_ISSUER = "https://cowork.circit.ai"


@dataclass(frozen=True)
class AgentCoreConfig:
    base_url: str
    audience: str
    issuer: str
    signing_key: bytes
    timeout_seconds: float = 30.0
    assertion_ttl_seconds: int = 300


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _decode_signing_key(raw: str) -> bytes:
    value = (raw or "").strip()
    if not value:
        raise RuntimeError("AGENTCORE_GATEWAY_SIGNING_KEY is not configured")
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        return value.encode("utf-8")


def get_agentcore_config() -> AgentCoreConfig:
    base_url = os.environ.get("AGENTCORE_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("AGENTCORE_BASE_URL is not configured")
    signing_key = _decode_signing_key(
        _first_env("AGENTCORE_GATEWAY_SIGNING_KEY", "COWORK_GATEWAY_TEST_KEY")
    )
    return AgentCoreConfig(
        base_url=base_url,
        audience=_first_env(
            "AGENTCORE_GATEWAY_AUDIENCE",
            "COWORK_GATEWAY_AUDIENCE",
            default=DEFAULT_AGENTCORE_AUDIENCE,
        ),
        issuer=_first_env(
            "AGENTCORE_GATEWAY_ISSUER",
            "COWORK_GATEWAY_ISSUER",
            default=DEFAULT_AGENTCORE_ISSUER,
        ),
        signing_key=signing_key,
        timeout_seconds=float(os.environ.get("AGENTCORE_TIMEOUT_SECONDS", "30")),
        assertion_ttl_seconds=int(os.environ.get("AGENTCORE_GATEWAY_TTL_SECONDS", "300")),
    )


def current_gateway_claims(request: Request) -> dict:
    email = (
        getattr(request.state, "cloudflare_access_user", None)
        or getattr(request.state, "current_user", None)
        or ""
    )
    email = str(email).strip().lower()
    subject = (
        getattr(request.state, "cloudflare_access_sub", None)
        or getattr(request.state, "api_token_owner", None)
        or email
    )
    subject = str(subject or email).strip()
    if not email and not subject:
        raise HTTPException(status_code=401, detail="Authenticated user is required")
    oid = subject or email
    return {
        "oid": oid,
        "sub": oid,
        "preferred_username": email or oid,
        "email": email or oid,
    }


def mint_gateway_assertion(config: AgentCoreConfig, claims: Mapping[str, object]) -> str:
    now = int(time.time())
    payload = {
        "iss": config.issuer,
        "aud": config.audience,
        "iat": now,
        "nbf": now - 5,
        "exp": now + max(60, config.assertion_ttl_seconds),
        **dict(claims),
    }
    return jwt.encode(payload, config.signing_key, algorithm="HS256")


async def agentcore_request(
    *,
    method: str,
    path: str,
    request: Request,
    body: bytes = b"",
    query: str = "",
) -> httpx.Response:
    config = get_agentcore_config()
    url, headers = build_agentcore_request(config, path, request, query=query)

    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        return await client.request(
            method.upper(),
            url,
            content=body if body else None,
            headers=headers,
        )


def build_agentcore_request(
    config: AgentCoreConfig,
    path: str,
    request: Request,
    *,
    query: str = "",
) -> tuple[str, dict[str, str]]:
    assertion = mint_gateway_assertion(config, current_gateway_claims(request))
    clean_path = "/".join(part for part in path.split("/") if part)
    url = f"{config.base_url}/{clean_path}"
    if query:
        url = f"{url}?{query}"

    headers = {
        "Authorization": f"Bearer {assertion}",
        "Accept": request.headers.get("accept", "application/json"),
    }
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    return url, headers
