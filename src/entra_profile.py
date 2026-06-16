"""Microsoft Entra profile helpers for Cloudflare Access users."""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CIRCIT_TENANT_ID = "b6560c52-065a-424b-90b1-5340eab75de9"

_token_cache: dict[str, Any] = {"access_token": "", "expires_at": 0.0}
_photo_cache: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class _GraphProfileSettings:
    tenant_id: str
    client_id: str
    client_secret: str
    token_url: str
    graph_base_url: str
    cache_seconds: int
    miss_cache_seconds: int
    max_photo_bytes: int


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.getenv(name, "").strip())
    except ValueError:
        return default
    return max(minimum, value)


def _settings() -> _GraphProfileSettings:
    tenant_id = _first_env(
        "WORKIQ_MCP_TENANT_ID",
        "MICROSOFT_TENANT_ID",
        "AZURE_TENANT_ID",
        "ENTRA_TENANT_ID",
        "CIRCIT_TENANT_ID",
        default=DEFAULT_CIRCIT_TENANT_ID,
    )
    client_id = _first_env(
        "ENTRA_PROFILE_CLIENT_ID",
        "WORKIQ_MCP_CLIENT_ID",
        "M365_MCP_CLIENT_ID",
        "MICROSOFT_365_MCP_CLIENT_ID",
        "MICROSOFT_MCP_CLIENT_ID",
    )
    client_secret = _first_env(
        "ENTRA_PROFILE_CLIENT_SECRET",
        "WORKIQ_MCP_CLIENT_SECRET",
        "M365_MCP_CLIENT_SECRET",
        "MICROSOFT_365_MCP_CLIENT_SECRET",
        "MICROSOFT_MCP_CLIENT_SECRET",
    )
    authority = os.getenv("ENTRA_AUTHORITY_HOST", "https://login.microsoftonline.com").strip().rstrip("/")
    token_url = os.getenv("ENTRA_PROFILE_TOKEN_URL", "").strip()
    if not token_url:
        token_url = f"{authority}/{tenant_id}/oauth2/v2.0/token"
    return _GraphProfileSettings(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        token_url=token_url,
        graph_base_url=os.getenv("MICROSOFT_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0").strip().rstrip("/"),
        cache_seconds=_int_env("ENTRA_PROFILE_PHOTO_CACHE_SECONDS", 21_600, 60),
        miss_cache_seconds=_int_env("ENTRA_PROFILE_PHOTO_MISS_CACHE_SECONDS", 600, 30),
        max_photo_bytes=_int_env("ENTRA_PROFILE_PHOTO_MAX_BYTES", 131_072, 1024),
    )


def _configured(settings: _GraphProfileSettings) -> bool:
    return bool(settings.tenant_id and settings.client_id and settings.client_secret)


async def _get_graph_token(client: httpx.AsyncClient, settings: _GraphProfileSettings) -> str:
    now = time.time()
    cached = str(_token_cache.get("access_token") or "")
    if cached and float(_token_cache.get("expires_at") or 0) > now + 60:
        return cached

    response = await client.post(
        settings.token_url,
        data={
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
        headers={"Accept": "application/json"},
    )
    if response.status_code >= 400:
        logger.warning("Microsoft Graph token request failed with status %s", response.status_code)
        return ""

    payload = response.json()
    token = str(payload.get("access_token") or "")
    if not token:
        logger.warning("Microsoft Graph token response did not include an access token")
        return ""

    expires_in = int(payload.get("expires_in") or 3600)
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = now + max(60, expires_in - 60)
    return token


async def get_entra_profile_avatar_url(email: str) -> str:
    """Return a data URL for the user's Entra profile photo, if available."""

    normalized_email = (email or "").strip().lower()
    if not normalized_email or "@" not in normalized_email:
        return ""

    now = time.time()
    cached = _photo_cache.get(normalized_email)
    if cached and cached[0] > now:
        return cached[1]

    settings = _settings()
    if not _configured(settings):
        return ""

    timeout = httpx.Timeout(connect=4.0, read=8.0, write=4.0, pool=4.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            token = await _get_graph_token(client, settings)
            if not token:
                _photo_cache[normalized_email] = (now + settings.miss_cache_seconds, "")
                return ""

            user_id = quote(normalized_email, safe="")
            response = await client.get(
                f"{settings.graph_base_url}/users/{user_id}/photo/$value",
                headers={"Authorization": f"Bearer {token}", "Accept": "image/*"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Microsoft Graph profile photo lookup failed for %s: %s", normalized_email, exc.__class__.__name__)
        _photo_cache[normalized_email] = (now + settings.miss_cache_seconds, "")
        return ""

    if response.status_code == 404:
        _photo_cache[normalized_email] = (now + settings.miss_cache_seconds, "")
        return ""
    if response.status_code >= 400:
        logger.warning("Microsoft Graph profile photo lookup for %s failed with status %s", normalized_email, response.status_code)
        _photo_cache[normalized_email] = (now + settings.miss_cache_seconds, "")
        return ""

    content_type = (response.headers.get("content-type") or "image/jpeg").split(";", 1)[0].strip().lower()
    if not content_type.startswith("image/"):
        _photo_cache[normalized_email] = (now + settings.miss_cache_seconds, "")
        return ""

    content = response.content
    if not content or len(content) > settings.max_photo_bytes:
        _photo_cache[normalized_email] = (now + settings.miss_cache_seconds, "")
        return ""

    avatar_url = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
    _photo_cache[normalized_email] = (now + settings.cache_seconds, avatar_url)
    return avatar_url


def clear_entra_profile_cache() -> None:
    """Clear module caches for tests and one-off diagnostics."""

    _token_cache.clear()
    _token_cache.update({"access_token": "", "expires_at": 0.0})
    _photo_cache.clear()
