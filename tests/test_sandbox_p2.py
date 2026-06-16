"""P2 sandbox tests: connector manifest (2.0), egress reuse (2.2),
admission gate (2.5), and sandbox runner argv/cancellation (2.1/2.3)."""

import asyncio
import ipaddress

import pytest


# --------------------------------------------------------------------------- #
# 2.0 connector-manifest schema + validator
# --------------------------------------------------------------------------- #

def _valid_manifest(**over):
    base = {
        "schemaVersion": "1.0.0",
        "id": "worklift",
        "name": "Worklift KB",
        "dataClass": "synthetic",
        "enabled": True,
        "egress": {"mode": "deny-all"},
    }
    base.update(over)
    return base


def test_manifest_valid_roundtrip():
    from src.connector_manifest import validate_manifest

    m = validate_manifest(_valid_manifest())
    assert m.id == "worklift"
    assert m.data_class == "synthetic"
    assert m.is_synthetic is True
    assert m.egress.is_deny_all is True


def test_manifest_missing_dataclass_defaults_banking():
    """Fail-closed: an omitted dataClass must NOT be permissive."""
    from src.connector_manifest import validate_manifest, DEFAULT_DATA_CLASS

    data = _valid_manifest()
    del data["dataClass"]
    m = validate_manifest(data)
    assert m.data_class == DEFAULT_DATA_CLASS == "banking"
    assert m.is_synthetic is False


def test_manifest_rejects_unknown_dataclass():
    from src.connector_manifest import validate_manifest, ManifestError

    with pytest.raises(ManifestError):
        validate_manifest(_valid_manifest(dataClass="public"))


def test_manifest_rejects_additional_properties():
    from src.connector_manifest import validate_manifest, ManifestError

    with pytest.raises(ManifestError):
        validate_manifest(_valid_manifest(sneaky="x"))


def test_manifest_rejects_bad_id_pattern():
    from src.connector_manifest import validate_manifest, ManifestError

    with pytest.raises(ManifestError):
        validate_manifest(_valid_manifest(id="Has Spaces!"))


def test_manifest_allowlist_requires_hosts():
    from src.connector_manifest import validate_manifest, ManifestError

    # allowlist mode with no allowedHosts is rejected by the conditional schema.
    with pytest.raises(ManifestError):
        validate_manifest(_valid_manifest(egress={"mode": "allowlist"}))

    m = validate_manifest(
        _valid_manifest(egress={"mode": "allowlist", "allowedHosts": ["api.anthropic.com"]})
    )
    assert m.egress.allowed_hosts == ("api.anthropic.com",)
    assert m.egress.is_deny_all is False


def test_manifest_load_fail_closed_on_one_bad():
    from src.connector_manifest import load_manifests, ManifestError

    good = _valid_manifest()
    bad = _valid_manifest(id="BAD ID")
    with pytest.raises(ManifestError):
        load_manifests([good, bad])


def test_vendored_schema_matches_canonical():
    """The odysseus /contracts copy must be byte-identical to the cowork
    source-of-truth (drift guard)."""
    import os

    odysseus = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "contracts",
        "connector-manifest.schema.json",
    )
    with open(odysseus, "rb") as fh:
        vendored = fh.read()

    # Canonical lives in the cowork repo; check it when present (local dev / CI
    # with both repos checked out). Skip cleanly when only odysseus is present.
    cowork = "/Users/jakubsikora/Repos/circit/circit-cowork/contracts/connector-manifest.schema.json"
    if not os.path.isfile(cowork):
        pytest.skip("cowork canonical not checked out alongside; CI drift check covers this")
    with open(cowork, "rb") as fh:
        canonical = fh.read()
    assert vendored == canonical, "vendored schema drifted from cowork canonical"


# --------------------------------------------------------------------------- #
# 2.2 egress reuse of url_security._BLOCKED_NETWORKS
# --------------------------------------------------------------------------- #

def test_egress_deny_all_blocks_everything():
    from src.sandbox_egress import evaluate_host

    d = evaluate_host("api.anthropic.com", mode="deny-all", allowed_hosts=())
    assert d.allowed is False
    assert "deny-all" in d.reason


def test_egress_allowlist_blocks_unlisted_host():
    from src.sandbox_egress import evaluate_host

    d = evaluate_host("evil.example.com", mode="allowlist", allowed_hosts=("api.anthropic.com",))
    assert d.allowed is False
    assert "allowlist" in d.reason


def test_egress_blocks_imds_even_if_allowlisted(monkeypatch):
    """169.254.169.254 (IMDS) must be blocked via url_security even when the
    connector allowlist names it -- the reuse is the whole point of 2.2."""
    from src import url_security
    from src import sandbox_egress

    monkeypatch.setattr(
        url_security, "_resolve_hostname_ips",
        lambda host: [ipaddress.ip_address("169.254.169.254")],
    )
    d = sandbox_egress.evaluate_host(
        "metadata", mode="allowlist", allowed_hosts=("metadata",)
    )
    assert d.allowed is False
    assert "blocked" in d.reason or "unresolvable" in d.reason


def test_egress_blocks_rfc1918_even_if_allowlisted(monkeypatch):
    from src import url_security
    from src import sandbox_egress

    monkeypatch.setattr(
        url_security, "_resolve_hostname_ips",
        lambda host: [ipaddress.ip_address("10.1.2.3")],
    )
    d = sandbox_egress.evaluate_host(
        "internal.svc", mode="allowlist", allowed_hosts=("internal.svc",)
    )
    assert d.allowed is False


def test_egress_allows_public_allowlisted_host(monkeypatch):
    from src import url_security
    from src import sandbox_egress

    monkeypatch.setattr(
        url_security, "_resolve_hostname_ips",
        lambda host: [ipaddress.ip_address("160.79.104.10")],
    )
    d = sandbox_egress.evaluate_host(
        "api.anthropic.com", mode="allowlist", allowed_hosts=("api.anthropic.com",)
    )
    assert d.allowed is True


# --------------------------------------------------------------------------- #
# 2.5 fail-closed admission gate
# --------------------------------------------------------------------------- #

def _conn(id_, data_class, enabled=True):
    from src.connector_manifest import ConnectorManifest, EgressPolicy

    return ConnectorManifest(
        id=id_, name=id_, data_class=data_class, enabled=enabled,
        egress=EgressPolicy(),
    )


def test_admission_local_plus_banking_refuses_boot():
    """THE core 2.5 case: SANDBOX_MODE=local + a banking connector => refuse."""
    from src.sandbox_admission import assert_admission, AdmissionError

    with pytest.raises(AdmissionError) as ei:
        assert_admission([_conn("banking-kb", "banking")], mode="local")
    assert "banking-kb" in str(ei.value)


def test_admission_local_plus_internal_refuses_boot():
    from src.sandbox_admission import evaluate_admission

    r = evaluate_admission([_conn("internal-kb", "internal")], mode="local")
    assert r.admitted is False
    assert r.offending == ("internal-kb",)


def test_admission_local_plus_synthetic_only_ok():
    from src.sandbox_admission import assert_admission

    r = assert_admission([_conn("demo", "synthetic")], mode="local")
    assert r.admitted is True


def test_admission_local_ignores_disabled_banking():
    """A disabled banking connector does not block local boot."""
    from src.sandbox_admission import assert_admission

    r = assert_admission([_conn("banking-kb", "banking", enabled=False)], mode="local")
    assert r.admitted is True


def test_admission_isolated_permits_banking():
    from src.sandbox_admission import assert_admission

    r = assert_admission([_conn("banking-kb", "banking")], mode="isolated")
    assert r.admitted is True


def test_admission_unknown_mode_defaults_local_failclosed():
    from src.sandbox_admission import evaluate_admission

    # garbage SANDBOX_MODE must resolve to the restrictive default, not isolated.
    r = evaluate_admission([_conn("banking-kb", "banking")], env_value="ISOLATED_TYPO")
    assert r.mode == "local"
    assert r.admitted is False


# --------------------------------------------------------------------------- #
# 2.1 / 2.3 sandbox runner argv + cancellation
# --------------------------------------------------------------------------- #

def test_docker_argv_hardening_flags():
    from src.sandbox_runner import _docker_argv, SandboxSpec

    argv = _docker_argv(SandboxSpec(), "odys-sbx-test", "/tmp/ws", ["/bin/sh", "-c", "echo hi"])
    joined = " ".join(argv)
    assert "--user 10001:10001" in joined
    assert "--network none" in joined
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--rm" in joined
    # workspace bind present
    assert "/tmp/ws:/sandbox/work:rw" in joined


def test_docker_argv_strips_host_env():
    """The container env must NOT inherit os.environ (no ambient creds)."""
    import os
    from src.sandbox_runner import _docker_argv, SandboxSpec

    os.environ["TOTALLY_SECRET_TOKEN"] = "leak-me"
    try:
        argv = _docker_argv(SandboxSpec(), "n", None, ["/bin/sh", "-c", "true"])
    finally:
        del os.environ["TOTALLY_SECRET_TOKEN"]
    assert not any("TOTALLY_SECRET_TOKEN" in a for a in argv)
    # only the sanitized allowlist is injected
    env_flags = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    keys = {e.split("=", 1)[0] for e in env_flags}
    assert keys <= {"TERM", "COLUMNS", "LINES", "HOME", "PATH"}


def test_sandboxed_unavailable_when_no_docker(monkeypatch):
    from src import sandbox_runner

    monkeypatch.setattr(sandbox_runner, "docker_available", lambda: False)
    with pytest.raises(sandbox_runner.SandboxUnavailableError):
        asyncio.run(sandbox_runner.spawn_sandboxed("bash", "echo hi", timeout=5))


def test_sandboxed_cancellation_kills_container(monkeypatch):
    """On SSE teardown (CancelledError) the runner must docker-kill the container."""
    from src import sandbox_runner

    monkeypatch.setattr(sandbox_runner, "docker_available", lambda: True)

    killed = {}

    async def fake_kill(name):
        killed["name"] = name

    monkeypatch.setattr(sandbox_runner, "_docker_kill", fake_kill)

    class _FakeProc:
        returncode = None

    async def fake_create(*a, **k):
        return _FakeProc()

    async def fake_stream(proc, *, timeout, progress_cb=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(sandbox_runner.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(sandbox_runner, "_run_subprocess_streaming", fake_stream)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sandbox_runner.spawn_sandboxed("bash", "sleep 999", timeout=5))
    assert killed.get("name", "").startswith("odys-sbx-")


# --------------------------------------------------------------------------- #
# tool_execution dispatch wiring (SANDBOX_MODE switch)
# --------------------------------------------------------------------------- #

def test_exec_dispatch_isolated_failclosed_when_no_docker(monkeypatch):
    """SANDBOX_MODE=isolated but Docker missing => failed run, NOT a silent
    downgrade to the env-inherited local subprocess."""
    from src import tool_execution

    monkeypatch.setenv("SANDBOX_MODE", "isolated")
    import src.sandbox_runner as sr
    monkeypatch.setattr(sr, "docker_available", lambda: False)

    out, err, rc, timed = asyncio.run(
        tool_execution._exec_bash_or_python("bash", "echo hi", timeout=5)
    )
    assert rc == 126
    assert "fail-closed" in err
