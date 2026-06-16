"""Sandbox egress policy (P2.2) -- REUSE of the existing url_security blocklist.

The plan's correction #3 is explicit: do NOT build a new blocklist. ``url_security``
already blocks RFC-1918, loopback, link-local (incl. 169.254/16 IMDS), CGNAT, ULA,
etc. via ``_BLOCKED_NETWORKS`` + ``_blocked_ip``. This module is a thin adapter that
re-uses those primitives to decide whether a sandbox run may reach a given host.

Policy:
  * deny-by-default. ``mode="deny-all"`` => nothing is reachable.
  * ``mode="allowlist"`` => a host is reachable iff it is on the connector's
    allowlist AND it resolves only to public IPs (every resolved address passes
    ``url_security._blocked_ip`` == False). The model-provider hosts and approved
    connectors are added to the allowlist by the connector manifest.

This keeps the internal-blocking authority in ONE place (url_security); the sandbox
cannot reach IMDS / internal services even if a connector allowlist is mis-typed.
"""

from __future__ import annotations

from dataclasses import dataclass

from src import url_security


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str


def _resolves_public(host: str) -> bool:
    """True iff `host` resolves and EVERY resolved address is public (not blocked
    by url_security). Fail-closed: a resolution error or any blocked address -> False.
    """
    try:
        ips = url_security._resolve_hostname_ips(host)
    except Exception:
        return False
    if not ips:
        return False
    return all(not url_security._blocked_ip(ip) for ip in ips)


def evaluate_host(host: str, *, mode: str, allowed_hosts: tuple[str, ...]) -> EgressDecision:
    """Decide whether the sandbox may open an outbound connection to `host`.

    `mode` / `allowed_hosts` come from the connector manifest's egress policy.
    Host may include a port suffix (`api.example.com:443`) -- the port is ignored
    for the address check (network egress is gated on destination IP, not port).
    """
    host = (host or "").strip().lower()
    if not host:
        return EgressDecision(False, "empty host")

    # Strip an optional :port for IP resolution.
    bare = host.rsplit(":", 1)[0] if host.count(":") == 1 and not host.startswith("[") else host

    if mode != "allowlist":
        return EgressDecision(False, "deny-all egress policy")

    allow = {h.strip().lower() for h in allowed_hosts}
    if host not in allow and bare not in allow:
        return EgressDecision(False, f"host '{host}' not on connector allowlist")

    if not _resolves_public(bare):
        # Same authority as url_security: internal / IMDS / unresolved => blocked.
        return EgressDecision(False, f"host '{bare}' resolves to a blocked or unresolvable address")

    return EgressDecision(True, "allowed")
