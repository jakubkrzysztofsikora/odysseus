"""P7.2 — contract test: connector manifest through the REAL Python enforcer.

Plan v3.3 §7.2: the canonical connector-manifest JSON Schema in /contracts must
be enforced fail-closed by BOTH backends. This file exercises the Python
enforcer (src/connector_manifest.validate_manifest / load_manifests) against the
vendored schema (contracts/connector-manifest.schema.json).

The .NET half of the same contract lives in the cowork repo
(tests/.../Contracts/ConnectorManifestContractTests.cs). A drift guard
(test_p7_contract_schema_drift.py) asserts the two repos vendor a byte-identical
schema so the two enforcers can never diverge silently.
"""
import json
from pathlib import Path

import pytest

from src.connector_manifest import (
    ManifestError,
    validate_manifest,
    load_manifests,
)

_REPO = Path(__file__).resolve().parents[1]
_SCHEMA = _REPO / "contracts" / "connector-manifest.schema.json"


def _valid_synthetic() -> dict:
    return {
        "schemaVersion": "1.0.0",
        "id": "demo-synthetic",
        "name": "Demo synthetic source",
        "dataClass": "synthetic",
        "enabled": True,
        "egress": {"mode": "deny-all"},
    }


def test_schema_file_is_present_and_parseable():
    assert _SCHEMA.is_file(), f"vendored schema missing at {_SCHEMA}"
    json.loads(_SCHEMA.read_text())  # raises if malformed


def test_valid_manifest_passes_enforcer():
    m = validate_manifest(_valid_synthetic())
    assert m.id == "demo-synthetic"
    assert m.data_class == "synthetic"


def test_allowlist_requires_allowed_hosts_fail_closed():
    bad = _valid_synthetic()
    bad["egress"] = {"mode": "allowlist"}  # allowedHosts omitted -> invalid
    with pytest.raises(ManifestError):
        validate_manifest(bad)


def test_unknown_schema_version_is_refused():
    bad = _valid_synthetic()
    bad["schemaVersion"] = "9.9.9"
    with pytest.raises(ManifestError):
        validate_manifest(bad)


def test_unknown_data_class_is_refused():
    bad = _valid_synthetic()
    bad["dataClass"] = "top-secret"
    with pytest.raises(ManifestError):
        validate_manifest(bad)


def test_additional_properties_refused():
    bad = _valid_synthetic()
    bad["sneaky"] = "extra"
    with pytest.raises(ManifestError):
        validate_manifest(bad)


def test_non_object_input_is_refused():
    with pytest.raises(ManifestError):
        validate_manifest(["not", "an", "object"])  # type: ignore[arg-type]


def test_load_manifests_validates_each_entry():
    good = _valid_synthetic()
    bad = _valid_synthetic()
    bad["egress"] = {"mode": "allowlist"}  # invalid
    with pytest.raises(ManifestError):
        load_manifests([good, bad])
