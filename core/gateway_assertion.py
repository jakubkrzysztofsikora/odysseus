"""Gateway signed-JWT-assertion verifier (cross-runtime trust spine, plan v3.3 §1.1/§1.2).

The edge (Front Door + APIM + Entra) validates the end-user OIDC token, then
mints a short-lived JWT assertion this backend verifies. Mirrors the .NET
``GatewayAssertionAuthHandler`` so both runtimes agree on the same logical
identity (oid / user / team): a correlationId-linked transcript and audit
ledger attribute to the same actor regardless of which runtime served the turn.

Fail-closed: no trust material configured, or a missing/malformed/unsigned/
wrong-issuer/wrong-audience/expired token, raises :class:`AssertionError`-free
:class:`GatewayAssertionError`. There is no loopback bypass here.

Unit-testable WITHOUT a live tenant: tests inject an HS256 / RS256 signing key
via ``signing_key=`` (the "mocked JWKS" seam); production sets ``jwks_url`` and
the verifier pulls keys from Entra/APIM metadata.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import jwt
from jwt import PyJWKClient


# The pre-Entra constant identity. Kept only so a test can assert we never mint
# or accept it. Production must never see this as a subject.
LEGACY_CONSTANT_IDENTITY = "cowork-client"


class GatewayAssertionError(Exception):
    """Raised when an assertion is absent, malformed, or fails verification."""


@dataclass(frozen=True)
class CallerPrincipal:
    """Typed caller identity extracted from the gateway assertion."""

    oid: str
    user: str
    team: Optional[str] = None

    @property
    def is_legacy_constant(self) -> bool:
        return self.oid == LEGACY_CONSTANT_IDENTITY


class GatewayAssertionVerifier:
    """Verifies a gateway JWT assertion and returns a :class:`CallerPrincipal`.

    Parameters
    ----------
    issuer, audience:
        Expected ``iss`` / ``aud``. Validated when non-empty.
    jwks_url:
        Production OIDC JWKS endpoint. Mutually exclusive in practice with
        ``signing_key`` (tests use the latter).
    signing_key:
        Test/offline symmetric or public key. When set, no network fetch occurs.
    algorithms:
        Accepted signature algorithms. Defaults to RS256 for JWKS; tests pass
        ``["HS256"]`` with a shared secret.
    leeway:
        Clock-skew tolerance in seconds.
    """

    def __init__(
        self,
        *,
        issuer: str = "",
        audience: str = "",
        jwks_url: Optional[str] = None,
        signing_key: Optional[object] = None,
        algorithms: Optional[list[str]] = None,
        leeway: int = 30,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._signing_key = signing_key
        self._algorithms = algorithms or (["HS256"] if signing_key else ["RS256"])
        self._leeway = leeway
        self._jwk_client = PyJWKClient(jwks_url) if jwks_url else None

    @classmethod
    def from_env(cls) -> "GatewayAssertionVerifier":
        """Build a production verifier from environment configuration."""
        return cls(
            issuer=os.environ.get("ODYSSEUS_GATEWAY_ISSUER", ""),
            audience=os.environ.get("ODYSSEUS_GATEWAY_AUDIENCE", ""),
            jwks_url=os.environ.get("ODYSSEUS_GATEWAY_JWKS_URL") or None,
        )

    @property
    def has_trust_material(self) -> bool:
        return self._signing_key is not None or self._jwk_client is not None

    def verify(self, token: str) -> CallerPrincipal:
        """Verify the bearer token; raise GatewayAssertionError on any failure."""
        if not self.has_trust_material:
            raise GatewayAssertionError(
                "No gateway trust material configured — backend requires a signed assertion."
            )
        if not token or not token.strip():
            raise GatewayAssertionError("Empty gateway assertion")

        if self._signing_key is not None:
            key = self._signing_key
        else:
            try:
                key = self._jwk_client.get_signing_key_from_jwt(token).key  # type: ignore[union-attr]
            except Exception as exc:  # pragma: no cover - network path
                raise GatewayAssertionError(f"Could not resolve signing key: {exc}") from exc

        options = {
            "require": ["exp"],
            "verify_aud": bool(self._audience),
            "verify_iss": bool(self._issuer),
            "verify_signature": True,
        }
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self._algorithms,
                audience=self._audience or None,
                issuer=self._issuer or None,
                leeway=self._leeway,
                options=options,
            )
        except jwt.PyJWTError as exc:
            raise GatewayAssertionError(f"Assertion failed validation: {exc}") from exc

        return self._extract(claims)

    @staticmethod
    def _extract(claims: dict) -> CallerPrincipal:
        oid = (
            claims.get("oid")
            or claims.get("http://schemas.microsoft.com/identity/claims/objectidentifier")
            or claims.get("sub")
        )
        if not oid or not str(oid).strip():
            raise GatewayAssertionError("Gateway assertion missing required oid claim")
        oid = str(oid).strip()

        user = (
            claims.get("preferred_username")
            or claims.get("upn")
            or claims.get("email")
            or oid
        )
        team = claims.get("team") or claims.get("groups")
        if isinstance(team, (list, tuple)):
            team = team[0] if team else None

        principal = CallerPrincipal(oid=oid, user=str(user), team=str(team) if team else None)
        # Defence in depth: never accept the pre-Entra constant identity.
        if principal.is_legacy_constant:
            raise GatewayAssertionError(
                "Legacy constant identity is not a valid gateway assertion subject"
            )
        return principal
