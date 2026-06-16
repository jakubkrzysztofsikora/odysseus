"""Cloudflare Access JWT verification for the Circit-hosted deployment.

Cloudflare Access forwards the authenticated user's JWT to the origin in
``Cf-Access-Jwt-Assertion``. The origin must still verify that assertion so a
direct hit to the Container App FQDN cannot bypass Access.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

import jwt
from jwt import PyJWKClient


class CloudflareAccessError(Exception):
    """Raised when a Cloudflare Access assertion is absent or invalid."""


@dataclass(frozen=True)
class CloudflareAccessPrincipal:
    """Identity extracted from a verified Cloudflare Access assertion."""

    email: str
    subject: str
    display_name: str = ""
    avatar_url: str = ""


def display_name_from_email(email: str) -> str:
    local = (email or "").strip().split("@", 1)[0]
    if not local:
        return ""
    parts = [p for p in local.replace(".", " ").replace("_", " ").replace("-", " ").split() if p]
    if not parts:
        return local
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _claim_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _display_name_from_claims(claims: dict, email: str) -> str:
    for key in ("name", "display_name"):
        value = _claim_text(claims.get(key))
        if value and "@" not in value:
            return value

    given = _claim_text(claims.get("given_name"))
    family = _claim_text(claims.get("family_name"))
    combined = " ".join(part for part in (given, family) if part)
    if combined:
        return combined

    preferred = _claim_text(claims.get("preferred_username"))
    if preferred and "@" not in preferred:
        return preferred

    return display_name_from_email(email)


def _avatar_url_from_claims(claims: dict) -> str:
    for key in ("picture", "avatar_url", "photo", "profile_photo", "thumbnailPhoto"):
        value = _claim_text(claims.get(key))
        if value.startswith(("https://", "http://", "data:image/")):
            return value
    return ""


def _normalize_issuer(value: str) -> str:
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.startswith("https://"):
        return raw
    if raw.startswith("http://"):
        return "https://" + raw[len("http://") :]
    return "https://" + raw


def _jwks_url_for_issuer(issuer: str) -> str:
    return f"{issuer.rstrip('/')}/cdn-cgi/access/certs"


def _email_matches_domain(email: str, allowed_domain: str) -> bool:
    domain = (allowed_domain or "").strip().lower().lstrip("@")
    if not domain:
        return True
    email = (email or "").strip().lower()
    return email.endswith(f"@{domain}")


def _parse_service_token_identities(raw: str) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for item in (raw or "").replace(";", ",").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        token_id, email = item.split("=", 1)
        token_id = token_id.strip()
        email = email.strip().lower()
        if token_id and email:
            mappings[token_id] = email
            mappings[token_id.lower()] = email
    return mappings


class CloudflareAccessVerifier:
    """Verify Cloudflare Access JWTs and return the Circit user identity."""

    def __init__(
        self,
        *,
        audience: str,
        issuer: str,
        allowed_email_domain: str = "circit.io",
        jwks_url: Optional[str] = None,
        signing_key: Optional[object] = None,
        algorithms: Optional[list[str]] = None,
        service_token_identities: Optional[Mapping[str, str]] = None,
        leeway: int = 30,
    ) -> None:
        self._audience = (audience or "").strip()
        self._issuer = _normalize_issuer(issuer)
        self._allowed_email_domain = (allowed_email_domain or "").strip().lower().lstrip("@")
        self._signing_key = signing_key
        self._algorithms = algorithms or (["HS256"] if signing_key else ["RS256"])
        self._service_token_identities = {
            str(key).strip(): str(value).strip().lower()
            for key, value in (service_token_identities or {}).items()
            if str(key).strip() and str(value).strip()
        }
        self._leeway = leeway

        jwks = (jwks_url or "").strip()
        if not jwks and self._issuer:
            jwks = _jwks_url_for_issuer(self._issuer)
        self._jwks_url = jwks or None
        self._jwk_client = PyJWKClient(self._jwks_url) if self._jwks_url else None

    @classmethod
    def from_env(cls) -> "CloudflareAccessVerifier":
        issuer = (
            os.environ.get("CLOUDFLARE_ACCESS_ISSUER")
            or os.environ.get("CLOUDFLARE_ACCESS_TEAM_DOMAIN")
            or ""
        )
        return cls(
            audience=os.environ.get("CLOUDFLARE_ACCESS_AUD", ""),
            issuer=issuer,
            allowed_email_domain=os.environ.get("ODYSSEUS_ALLOWED_EMAIL_DOMAIN", "circit.io"),
            jwks_url=os.environ.get("CLOUDFLARE_ACCESS_JWKS_URL") or None,
            service_token_identities=_parse_service_token_identities(
                os.environ.get("ODYSSEUS_CLOUDFLARE_SERVICE_TOKEN_EMAILS")
                or os.environ.get("CLOUDFLARE_ACCESS_SERVICE_TOKEN_EMAILS")
                or ""
            ),
        )

    @property
    def has_configuration(self) -> bool:
        return bool(self._audience and self._issuer and (self._signing_key or self._jwk_client))

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def jwks_url(self) -> Optional[str]:
        return self._jwks_url

    def verify(self, token: str) -> CloudflareAccessPrincipal:
        if not self.has_configuration:
            raise CloudflareAccessError(
                "Cloudflare Access auth is enabled but audience, issuer, or JWKS trust material is missing."
            )
        if not token or not token.strip():
            raise CloudflareAccessError("Missing Cf-Access-Jwt-Assertion")

        if self._signing_key is not None:
            key = self._signing_key
        else:
            try:
                key = self._jwk_client.get_signing_key_from_jwt(token).key  # type: ignore[union-attr]
            except Exception as exc:  # pragma: no cover - live network path
                raise CloudflareAccessError(f"Could not resolve Cloudflare Access signing key: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={
                    "require": ["aud", "exp", "iss"],
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_signature": True,
                },
            )
        except jwt.PyJWTError as exc:
            raise CloudflareAccessError(f"Cloudflare Access assertion failed validation: {exc}") from exc

        email = str(claims.get("email") or "").strip().lower()
        if not email:
            common_name = str(claims.get("common_name") or "").strip()
            mapped_email = (
                self._service_token_identities.get(common_name)
                or self._service_token_identities.get(common_name.lower())
            )
            if not mapped_email:
                raise CloudflareAccessError("Cloudflare Access assertion missing email claim")
            email = mapped_email

        if not _email_matches_domain(email, self._allowed_email_domain):
            raise CloudflareAccessError("Cloudflare Access identity is outside the allowed email domain")

        subject = str(claims.get("sub") or "").strip()
        if not subject:
            common_name = str(claims.get("common_name") or "").strip()
            if not common_name or email != (
                self._service_token_identities.get(common_name)
                or self._service_token_identities.get(common_name.lower())
            ):
                raise CloudflareAccessError("Cloudflare Access assertion missing subject claim")
            subject = f"service-token:{common_name}"
        return CloudflareAccessPrincipal(
            email=email,
            subject=subject,
            display_name=_display_name_from_claims(claims, email),
            avatar_url=_avatar_url_from_claims(claims),
        )


def is_cloudflare_access_issuer(value: str) -> bool:
    """Return True for normalized Cloudflare Access team-domain issuers."""

    issuer = _normalize_issuer(value)
    parsed = urlparse(issuer)
    return parsed.scheme == "https" and parsed.netloc.endswith(".cloudflareaccess.com")
