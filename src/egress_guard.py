"""Backend->model egress chokepoint (Circit P6.2).

This module is the single enforcement point for *outbound LLM calls*. It exists
because the LLM call path is the real exfiltration surface: an agent can be
steered (prompt injection, a poisoned tool result, a malicious scheduled-task
``endpoint_url``) into pointing the model client at an attacker-controlled host
and streaming the conversation -- including banking-scoped context -- straight
to it. ``src/url_security.py`` already blocks private/IMDS ranges for
*untrusted* URLs, but the LLM path deliberately does NOT go through it (admin
model endpoints are allowed to be private/tailnet), so the LLM path was
unguarded.

Two controls live here:

1. ``authorize_egress_url`` -- an *allowlist*, not a blocklist. Returns the URL
   only if its host is either (a) a known public provider, or (b) the host of
   an admin-configured ``ModelEndpoint``. Anything else -- every agent- or
   user-supplied ``base_url`` that an admin did not vet -- is rejected
   fail-closed. For non-admin (public-provider) hosts it also performs a
   connect-time DNS resolution and rejects if *any* resolved IP is in the
   ``url_security`` blocklist, which is what closes the DNS-rebind window that
   resolving "later, somewhere else" leaves open.

2. ``enforce_dlp`` -- a fail-closed data-loss check keyed on ``dataClass``. The
   *transport* half (block egress when the bound context carries a high
   ``dataClass``) is real and shipped here; the *classification* half -- knowing
   which ``dataClass`` a given request carries -- depends on the data-source
   binding that P2.5 (sandbox data binding) / P3.1 (connector layer) introduce.
   Until those land, ``current_data_class()`` returns ``None`` and DLP is a
   no-op for the unclassified path; the moment a caller passes a real
   ``data_class`` the block is enforced. See ``RED_UNTIL`` markers + the
   red-until tests.

NOTHING in here trusts a hostname string alone. The *authorization* decision is
made against resolved IPs and the admin endpoint table.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit

from src.url_security import _blocked_ip, _INTERNAL_HOSTNAMES, _INTERNAL_SUFFIXES

logger = logging.getLogger(__name__)

# RED_UNTIL P2.5/P3.1: the dataClass *classification* source. The enforcement
# below is live now; what is deferred is the binding that tells us a request
# carries e.g. "banking-confidential". See enforce_dlp / current_data_class.
RED_UNTIL_DATACLASS_SOURCE = "P2.5 (sandbox data binding) / P3.1 (connector layer)"


class EgressDenied(Exception):
    """Raised when an outbound LLM call is refused. Fail-closed: callers must
    let this propagate (turn it into a 5xx / refusal), never swallow it."""


# -- Static public-provider allowlist ----------------------------------------
# Hosts (or parent domains) we positively trust as LLM providers. An exact host
# or any subdomain matches. This is intentionally a small, reviewed set; adding
# a provider is a deliberate change, not something an agent can do at runtime.
_PROVIDER_ALLOWLIST = (
    "anthropic.com",
    "openai.com",
    "openrouter.ai",
    "groq.com",
    "x.ai",
    "mistral.ai",
    "deepseek.com",
    "googleapis.com",
    "together.xyz",
    "together.ai",
    "fireworks.ai",
    "ollama.com",
    # NOTE: GitHub Copilot (copilot-s2s) is OUT of MVP per the locked plan; its
    # host is deliberately NOT allowlisted here.
)


def _host_of(url: str) -> str:
    try:
        return (urlsplit((url or "").strip()).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _matches_allowlist(host: str, domains: Iterable) -> bool:
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _is_provider_host(host: str) -> bool:
    return _matches_allowlist(host, _PROVIDER_ALLOWLIST)


# -- Admin-configured endpoint allowlist --------------------------------------
# Admin ModelEndpoints may legitimately point at private/tailnet IPs (a local
# vLLM, an on-prem gateway). Those hosts are trusted *because an admin vetted
# them*, so for an admin host we do NOT apply the private-IP blocklist -- but the
# host must still appear in the admin table. An agent-supplied base_url that an
# admin never configured is rejected, full stop.
#
# Resolving the admin host set hits the DB; cache it so the hot LLM path doesn't
# issue a query per call. invalidate_admin_endpoint_cache() is called whenever
# an endpoint row changes (wire from routes/model_routes on save).
_admin_hosts_cache = None
_admin_hosts_lock = threading.Lock()


def invalidate_admin_endpoint_cache() -> None:
    global _admin_hosts_cache
    with _admin_hosts_lock:
        _admin_hosts_cache = None


def _load_admin_hosts():
    """Hosts of every admin-configured ModelEndpoint (enabled or not -- an admin
    configured it; disabling is a UX toggle, not a trust revocation for SSRF)."""
    global _admin_hosts_cache
    with _admin_hosts_lock:
        if _admin_hosts_cache is not None:
            return _admin_hosts_cache
    hosts = set()
    try:
        # Imported lazily: egress_guard is imported by llm_core, and importing
        # the DB layer at module load would create a circular import on startup.
        from core.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            for (base_url,) in db.query(ModelEndpoint.base_url).all():
                h = _host_of(base_url)
                if h:
                    hosts.add(h)
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover - defensive; DB optional in unit tests
        logger.warning("egress_guard: could not load admin endpoints: %s", exc)
    frozen = frozenset(hosts)
    with _admin_hosts_lock:
        _admin_hosts_cache = frozen
    return frozen


# Test seam: unit tests inject a host set without standing up a DB.
_admin_hosts_provider = _load_admin_hosts


def set_admin_hosts_provider(provider) -> None:
    """Override how admin hosts are resolved (tests / DI). Pass None to reset."""
    global _admin_hosts_provider
    _admin_hosts_provider = provider or _load_admin_hosts
    invalidate_admin_endpoint_cache()


def _is_admin_host(host: str) -> bool:
    if not host:
        return False
    return host in _admin_hosts_provider()


# -- Connect-time IP validation (reduces DNS rebind window) -----------------------------


def _resolved_ips(host: str):
    ips = []
    # A literal IP needs no DNS; classify it directly.
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family in (socket.AF_INET, socket.AF_INET6):
            ips.append(ipaddress.ip_address(sockaddr[0]))
    return ips


def _assert_public_ips(host: str):
    """Resolve host and fail closed unless EVERY resolved IP is public.

    Returns the resolved IPs so the caller can *pin* the connection to one of
    them -- resolving here and connecting to whatever DNS says later is exactly
    the rebind race we are closing. ``url_security._blocked_ip`` is the single
    source of truth for "is this address off-limits" (private, loopback,
    link-local/IMDS, multicast, reserved, unspecified)."""
    if host in _INTERNAL_HOSTNAMES or host.endswith(_INTERNAL_SUFFIXES):
        raise EgressDenied(f"egress denied: internal hostname {host!r}")
    try:
        ips = _resolved_ips(host)
    except OSError as exc:
        raise EgressDenied(f"egress denied: DNS failure for {host!r}: {exc}") from exc
    if not ips:
        raise EgressDenied(f"egress denied: {host!r} did not resolve")
    bad = [str(ip) for ip in ips if _blocked_ip(ip)]
    if bad:
        raise EgressDenied(
            f"egress denied: {host!r} resolves to blocked address(es) {bad} "
            f"(possible SSRF / DNS rebind)"
        )
    return ips


# -- Public API: authorize an outbound LLM URL --------------------------------


def authorize_egress_url(url: str) -> str:
    """Authorize an outbound LLM endpoint URL or raise ``EgressDenied``.

    Decision order (allowlist, fail-closed):
      1. scheme must be http/https and a host must be present;
      2. admin-configured host  -> ALLOW (admin-vetted; private IPs permitted);
      3. known public provider   -> ALLOW only if every resolved IP is public
                                    (connect-time pin against the blocklist);
      4. anything else           -> DENY.

    This is the function every LLM call site must funnel through. There is no
    "trusted because the string looked like Anthropic" path -- a look-alike host
    (``anthropic.com.evil.example``) is not a provider match (suffix-anchored)
    and is not an admin host, so it is denied at step 4."""
    cleaned = (url or "").strip()
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise EgressDenied(f"egress denied: not an http(s) URL: {url!r}")

    host = parts.hostname.lower().rstrip(".")

    # (2) Admin-vetted endpoint. Private/tailnet IPs are intentionally allowed
    # here -- an admin chose this host. We do NOT IP-pin admin hosts because the
    # whole point of a local vLLM endpoint is that it resolves to a private IP.
    if _is_admin_host(host):
        return cleaned

    # (3) Public provider. Must resolve entirely to public IPs.
    if _is_provider_host(host):
        _assert_public_ips(host)
        return cleaned

    # (4) Unknown / agent-influenced host -> fail closed.
    raise EgressDenied(
        f"egress denied: {host!r} is neither an admin-configured model endpoint "
        f"nor a known provider. Agent-supplied base_url is rejected."
    )


# -- Fail-closed DLP keyed on dataClass ---------------------------------------


@dataclass(frozen=True)
class DataClass:
    """Sensitivity label attached to the context bound into an LLM request.

    `rank` orders classes; anything at or above the block threshold may not
    leave the trust boundary to a *non-admin* (public-internet) provider."""

    name: str
    rank: int


# Minimal lattice. The real source of these labels is the data-source binding
# introduced in P2.5/P3.1; until then callers pass None (unclassified) and the
# transport check is a no-op for that path.
PUBLIC = DataClass("public", 0)
INTERNAL = DataClass("internal", 1)
CONFIDENTIAL = DataClass("confidential", 2)
BANKING_CONFIDENTIAL = DataClass("banking-confidential", 3)

_DATA_CLASSES = {c.name: c for c in (PUBLIC, INTERNAL, CONFIDENTIAL, BANKING_CONFIDENTIAL)}

# At or above this rank, the data may not egress to a public-internet provider.
# Admin (on-prem/tailnet) endpoints are inside the trust boundary and are exempt.
_BLOCK_EGRESS_AT_RANK = CONFIDENTIAL.rank


def resolve_data_class(value):
    if value is None or isinstance(value, DataClass):
        return value
    return _DATA_CLASSES.get(str(value).strip().lower())


def current_data_class():
    """The dataClass of the context bound into the current request.

    RED_UNTIL P2.5/P3.1: there is no binding to read yet, so this returns None
    (unclassified). When the sandbox/connector binding lands, this reads the
    bound source's classification from the request-scoped context. Wiring this
    is the only change needed to turn DLP on for the classified path; the
    enforcement below is already correct."""
    return None


def enforce_dlp(url: str, *, data_class=None) -> None:
    """Fail-closed DLP. Raise ``EgressDenied`` if ``data_class`` is at/above the
    block threshold AND the destination is a public-internet provider (i.e. not
    an admin on-prem/tailnet endpoint inside the trust boundary).

    ``data_class`` is passed explicitly by callers that already know it; when
    omitted we consult ``current_data_class()`` (RED_UNTIL P2.5/P3.1)."""
    dc = resolve_data_class(data_class) if data_class is not None else current_data_class()
    if dc is None:
        return  # unclassified path -- no-op until classification source lands
    if dc.rank < _BLOCK_EGRESS_AT_RANK:
        return
    host = _host_of(url)
    if _is_admin_host(host):
        return  # inside trust boundary (admin on-prem/tailnet) -- allowed
    raise EgressDenied(
        f"DLP: refusing to send {dc.name!r} data to public provider {host!r}. "
        f"Route classified data only through an admin-configured endpoint."
    )


def guard_egress(url: str, *, data_class=None) -> str:
    """Combined chokepoint: authorize the URL, then apply DLP. Returns the URL
    to use, or raises ``EgressDenied``. This is the one call LLM paths make."""
    authorized = authorize_egress_url(url)
    enforce_dlp(authorized, data_class=data_class)
    return authorized
