"""P6.2 backend->model egress chokepoint tests.

Covers the three things the plan calls out for src/egress_guard.py:
  1. allowlist (admin endpoint OR known provider) — everything else denied;
  2. connect-time IP pin defeats DNS rebind (provider host that resolves to a
     blocked/private/IMDS address is refused);
  3. agent-influenced base_url is rejected fail-closed.
Plus the fail-closed DLP keyed on dataClass, with the classification-source
half marked red-until-P2.5/P3.1.
"""
import ipaddress

import pytest

from src import egress_guard as eg
from src.egress_guard import (
    EgressDenied,
    authorize_egress_url,
    enforce_dlp,
    guard_egress,
    BANKING_CONFIDENTIAL,
    CONFIDENTIAL,
    INTERNAL,
    PUBLIC,
)


@pytest.fixture(autouse=True)
def _reset_admin_hosts():
    # Default: no admin endpoints configured. Individual tests inject as needed.
    eg.set_admin_hosts_provider(lambda: frozenset())
    yield
    eg.set_admin_hosts_provider(None)


def _public_dns(monkeypatch):
    """Make every hostname resolve to a public IP so allowlist logic is tested
    without real network/DNS."""
    monkeypatch.setattr(eg, "_resolved_ips", lambda host: [ipaddress.ip_address("93.184.216.34")])


# ── allowlist: providers allowed, everything else denied ────────────────────


def test_known_provider_allowed_when_public(monkeypatch):
    _public_dns(monkeypatch)
    assert authorize_egress_url("https://api.anthropic.com/v1/messages") == \
        "https://api.anthropic.com/v1/messages"


def test_provider_subdomain_allowed(monkeypatch):
    _public_dns(monkeypatch)
    assert authorize_egress_url("https://api.openai.com/v1/chat/completions")


def test_lookalike_host_is_denied(monkeypatch):
    # anthropic.com.evil.example is NOT a suffix match for anthropic.com.
    _public_dns(monkeypatch)
    with pytest.raises(EgressDenied):
        authorize_egress_url("https://anthropic.com.evil.example/v1/messages")


def test_unknown_agent_supplied_host_denied(monkeypatch):
    _public_dns(monkeypatch)
    with pytest.raises(EgressDenied):
        authorize_egress_url("https://attacker.example/v1/chat/completions")


def test_non_http_scheme_denied():
    with pytest.raises(EgressDenied):
        authorize_egress_url("file:///etc/passwd")
    with pytest.raises(EgressDenied):
        authorize_egress_url("gopher://x/")


def test_empty_url_denied():
    with pytest.raises(EgressDenied):
        authorize_egress_url("")


# ── admin endpoints: private IPs allowed, but only if configured ────────────


def test_admin_private_endpoint_allowed_without_ip_pin():
    # A local vLLM on a private IP is fine BECAUSE an admin configured it. The
    # IP-pin (which would reject the private addr) is intentionally bypassed
    # for admin hosts.
    eg.set_admin_hosts_provider(lambda: frozenset({"vllm.internal.corp"}))
    assert authorize_egress_url("http://vllm.internal.corp:8002/v1") == \
        "http://vllm.internal.corp:8002/v1"


def test_admin_endpoint_literal_private_ip_allowed():
    eg.set_admin_hosts_provider(lambda: frozenset({"10.0.0.5"}))
    assert authorize_egress_url("http://10.0.0.5:8002/v1")


def test_unconfigured_private_host_denied():
    # Same private host but NOT in the admin table -> the agent could not have
    # added it, so it is denied.
    with pytest.raises(EgressDenied):
        authorize_egress_url("http://10.0.0.5:8002/v1")


# ── connect-time IP pin defeats DNS rebind ──────────────────────────────────


def test_provider_resolving_to_imds_is_blocked(monkeypatch):
    # A provider-allowlisted host whose DNS has been rebound to the cloud IMDS
    # address (169.254.169.254) must be refused.
    monkeypatch.setattr(
        eg, "_resolved_ips",
        lambda host: [ipaddress.ip_address("169.254.169.254")],
    )
    with pytest.raises(EgressDenied) as ei:
        authorize_egress_url("https://api.anthropic.com/v1/messages")
    assert "blocked address" in str(ei.value)


def test_provider_resolving_to_private_is_blocked(monkeypatch):
    monkeypatch.setattr(
        eg, "_resolved_ips",
        lambda host: [ipaddress.ip_address("10.1.2.3")],
    )
    with pytest.raises(EgressDenied):
        authorize_egress_url("https://api.openai.com/v1/chat/completions")


def test_provider_mixed_public_and_private_is_blocked(monkeypatch):
    # ALL resolved IPs must be public; one private addr in the set fails closed.
    monkeypatch.setattr(
        eg, "_resolved_ips",
        lambda host: [ipaddress.ip_address("93.184.216.34"),
                      ipaddress.ip_address("127.0.0.1")],
    )
    with pytest.raises(EgressDenied):
        authorize_egress_url("https://api.groq.com/openai/v1/chat/completions")


def test_dns_failure_fails_closed(monkeypatch):
    def _boom(host):
        raise OSError("name resolution failed")
    monkeypatch.setattr(eg, "_resolved_ips", _boom)
    with pytest.raises(EgressDenied):
        authorize_egress_url("https://api.anthropic.com/v1/messages")


def test_internal_hostname_denied():
    # localhost/metadata are internal hostnames; even though they're not on the
    # provider allowlist this asserts the internal-hostname guard short-circuits.
    with pytest.raises(EgressDenied):
        authorize_egress_url("http://metadata.google.internal/computeMetadata/v1/")


# ── fail-closed DLP keyed on dataClass ──────────────────────────────────────


def test_dlp_blocks_confidential_to_public_provider(monkeypatch):
    _public_dns(monkeypatch)
    with pytest.raises(EgressDenied) as ei:
        guard_egress("https://api.anthropic.com/v1/messages",
                     data_class=CONFIDENTIAL)
    assert "DLP" in str(ei.value)


def test_dlp_blocks_banking_to_public_provider(monkeypatch):
    _public_dns(monkeypatch)
    with pytest.raises(EgressDenied):
        guard_egress("https://api.openai.com/v1/chat/completions",
                     data_class="banking-confidential")


def test_dlp_allows_confidential_to_admin_endpoint():
    # Classified data MAY go to an admin on-prem endpoint (inside the boundary).
    eg.set_admin_hosts_provider(lambda: frozenset({"vllm.internal.corp"}))
    assert guard_egress("http://vllm.internal.corp:8002/v1",
                        data_class=BANKING_CONFIDENTIAL)


def test_dlp_allows_low_class_to_public(monkeypatch):
    _public_dns(monkeypatch)
    assert guard_egress("https://api.anthropic.com/v1/messages",
                        data_class=INTERNAL)
    assert guard_egress("https://api.anthropic.com/v1/messages",
                        data_class=PUBLIC)


def test_dlp_string_dataclass_resolves():
    enforce_dlp("https://api.anthropic.com/v1/messages", data_class="public")
    enforce_dlp("https://api.anthropic.com/v1/messages", data_class="unknown-label")  # unknown -> None -> no-op


# ── red-until-P2.5/P3.1: classification SOURCE not wired yet ─────────────────


def test_dlp_unclassified_path_is_noop_until_classification_source(monkeypatch):
    """RED_UNTIL P2.5/P3.1.

    With no explicit data_class and no binding to read, enforce_dlp must be a
    no-op (it cannot block what it can't classify). This test PASSES today
    documenting the gap; when P2.5/P3.1 wire current_data_class() to return a
    real label for a banking-bound request, flip current_data_class via the
    seam and this becomes an EgressDenied. Marked xfail(strict) on the future
    behavior so it flags the moment the source lands and this test must change.
    """
    _public_dns(monkeypatch)
    # Today: unclassified -> allowed.
    assert guard_egress("https://api.anthropic.com/v1/messages") == \
        "https://api.anthropic.com/v1/messages"


@pytest.mark.xfail(
    reason="RED_UNTIL P2.5/P3.1: current_data_class() returns None until the "
           "sandbox/connector data binding exists. When it returns a high "
           "class for a bound request, this assertion (blocked) becomes true.",
    strict=True,
)
def test_dlp_blocks_via_classification_source_RED_UNTIL_P25():
    # Simulate the FUTURE: the request-scoped binding classifies as banking.
    # This is exactly the one wiring change P2.5/P3.1 make. Today current_data_
    # class() is None so guard_egress does NOT block -> xfail(strict) fires,
    # which keeps the gap visible and forces this test to be updated then.
    eg.set_admin_hosts_provider(lambda: frozenset())
    import ipaddress as _ip
    eg._resolved_ips = lambda host: [_ip.ip_address("93.184.216.34")]
    with pytest.raises(EgressDenied):
        guard_egress("https://api.anthropic.com/v1/messages")
