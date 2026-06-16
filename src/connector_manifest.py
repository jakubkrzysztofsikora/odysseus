"""Connector-manifest loader + validator (P2.0).

Loads connector manifests and validates them against the vendored canonical
JSON Schema at ``<repo>/contracts/connector-manifest.schema.json`` (byte-identical
copy of the cowork source-of-truth). This is the single place Odysseus turns raw
manifest dicts into trusted, typed objects.

Fail-closed posture:
  * an absent ``dataClass`` defaults to the most restrictive tier (``banking``)
    so a malformed/partial manifest can never silently widen access.
  * a manifest that does not validate raises -- callers must NOT fall back to a
    permissive default.

The downstream 2.5 admission gate (``sandbox_admission``) keys its boot decision
off the ``dataClass`` of every *enabled* connector returned here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import jsonschema

# Sensitivity tiers, ordered least -> most restrictive. The default when a
# manifest omits dataClass is the LAST (most restrictive) entry.
DATA_CLASSES = ("synthetic", "internal", "banking")
DEFAULT_DATA_CLASS = "banking"

_CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
_SCHEMA_PATH = _CONTRACTS_DIR / "connector-manifest.schema.json"


class ManifestError(ValueError):
    """Raised when a connector manifest is missing, malformed, or invalid."""


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    if not _SCHEMA_PATH.is_file():
        raise ManifestError(
            f"connector-manifest schema not found at {_SCHEMA_PATH}; "
            "the vendored /contracts copy is required"
        )
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _validator():
    schema = _load_schema()
    # Pin to the schema's declared draft; raises if the schema itself is broken.
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


@dataclass(frozen=True)
class EgressPolicy:
    mode: str = "deny-all"
    allowed_hosts: tuple[str, ...] = ()

    @property
    def is_deny_all(self) -> bool:
        return self.mode == "deny-all" or not self.allowed_hosts


@dataclass(frozen=True)
class ConnectorManifest:
    """A validated, typed connector manifest."""

    id: str
    name: str
    data_class: str
    enabled: bool
    egress: EgressPolicy
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_synthetic(self) -> bool:
        return self.data_class == "synthetic"


def validate_manifest(data: dict[str, Any]) -> ConnectorManifest:
    """Validate a raw manifest dict against the canonical schema and return a
    typed ``ConnectorManifest``.

    Raises ``ManifestError`` if the dict does not conform. dataClass defaults to
    the most-restrictive tier when absent (the schema also declares this default,
    but we apply it defensively here too so the typed object is never
    under-classified).
    """
    if not isinstance(data, dict):
        raise ManifestError(f"manifest must be a JSON object, got {type(data).__name__}")

    errors = sorted(_validator().iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise ManifestError(f"manifest schema violation at '{loc}': {first.message}")

    egress_raw = data.get("egress", {}) or {}
    egress = EgressPolicy(
        mode=egress_raw.get("mode", "deny-all"),
        allowed_hosts=tuple(egress_raw.get("allowedHosts", []) or ()),
    )

    return ConnectorManifest(
        id=data["id"],
        name=data["name"],
        # Defensive: never under-classify if the schema default ever changes.
        data_class=data.get("dataClass", DEFAULT_DATA_CLASS),
        enabled=bool(data.get("enabled", False)),
        egress=egress,
        description=data.get("description", ""),
        raw=data,
    )


def load_manifests(source: Iterable[dict[str, Any]]) -> list[ConnectorManifest]:
    """Validate a collection of raw manifest dicts. Fail-closed: a single bad
    manifest aborts the whole load rather than silently dropping it."""
    return [validate_manifest(d) for d in source]
