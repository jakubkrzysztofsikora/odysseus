"""P7.2 — cross-repo schema drift guard.

Plan v3.3 decision #5: the canonical schemas live in the cowork repo /contracts;
Odysseus vendors a BYTE-IDENTICAL copy under its own /contracts. If the two ever
diverge, the two backend enforcers can silently disagree on what a connector
manifest (or event) is allowed to be — a fail-open. This test pins them equal.

It locates the cowork repo via the CIRCIT_COWORK_REPO env var, else a few
conventional sibling paths. When it cannot find the cowork checkout (e.g. an
isolated CI job that only checked out Odysseus), it SKIPS rather than fails —
the canonical-side build runs the same comparison from its own direction, and
the per-PR contract job sets CIRCIT_COWORK_REPO so the guard is enforced there.
"""
import hashlib
import os
from pathlib import Path

import pytest

_ODYSSEUS_CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"

_VENDORED = ["connector-manifest.schema.json"]


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _find_cowork_contracts() -> Path | None:
    env = os.environ.get("CIRCIT_COWORK_REPO")
    candidates = []
    if env:
        candidates.append(Path(env) / "contracts")
    candidates += [
        Path.home() / "Repos" / "circit" / "circit-cowork" / "contracts",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


@pytest.mark.parametrize("name", _VENDORED)
def test_vendored_schema_matches_cowork_canonical(name):
    cowork = _find_cowork_contracts()
    if cowork is None:
        pytest.skip(
            "cowork /contracts not found (set CIRCIT_COWORK_REPO). Drift is "
            "enforced by the contract CI job which checks out both repos."
        )

    canonical = cowork / name
    vendored = _ODYSSEUS_CONTRACTS / name

    assert canonical.is_file(), f"canonical schema missing: {canonical}"
    assert vendored.is_file(), f"vendored schema missing: {vendored}"

    assert _sha256(vendored) == _sha256(canonical), (
        f"SCHEMA DRIFT: {name} differs between cowork (canonical) and Odysseus "
        f"(vendored). Re-vendor the canonical copy."
    )
