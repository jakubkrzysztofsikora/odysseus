"""Fail-closed admission gate (P2.5) -- Python backend.

Boot-time + connector-enable-time assertion shared in spirit with the .NET
``SandboxAdmissionGate``. The rule (from the plan, verbatim intent):

    if SANDBOX_MODE != isolated:
        every enabled connector.dataClass MUST == synthetic, else REFUSE.

Defaults are the most restrictive so a missing/typo'd env cannot widen access:
  * ``SANDBOX_MODE`` default = ``local``   (NOT isolated)
  * connector ``dataClass``  default = ``banking`` (handled in connector_manifest)

Call ``assert_admission(...)`` from app startup BEFORE serving traffic. It raises
``AdmissionError`` (which the caller should treat as a hard boot failure) when the
combination is unsafe. ``evaluate_admission`` is the pure, testable core.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from src.connector_manifest import ConnectorManifest

VALID_MODES = ("local", "isolated")
DEFAULT_MODE = "local"


class AdmissionError(RuntimeError):
    """Raised when the sandbox mode + enabled connectors are an unsafe combination."""


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    mode: str
    offending: tuple[str, ...] = ()  # ids of enabled non-synthetic connectors
    reason: str = ""


def resolve_mode(env_value: str | None = None) -> str:
    """Resolve SANDBOX_MODE. Unknown/empty -> the restrictive default (``local``),
    so a misconfigured value never accidentally grants isolated privileges."""
    raw = (env_value if env_value is not None else os.environ.get("SANDBOX_MODE", ""))
    mode = (raw or "").strip().lower()
    return mode if mode in VALID_MODES else DEFAULT_MODE


def evaluate_admission(
    connectors: Iterable[ConnectorManifest],
    *,
    mode: str | None = None,
    env_value: str | None = None,
) -> AdmissionResult:
    """Pure decision. ``mode`` overrides env resolution when provided (tests)."""
    resolved = (mode or resolve_mode(env_value)).strip().lower()
    if resolved not in VALID_MODES:
        resolved = DEFAULT_MODE

    if resolved == "isolated":
        # Isolated containment is sufficient for any data class.
        return AdmissionResult(True, resolved, reason="isolated sandbox: all data classes permitted")

    # local mode -> only synthetic connectors may be enabled.
    offending = tuple(
        c.id for c in connectors if c.enabled and not c.is_synthetic
    )
    if offending:
        return AdmissionResult(
            False,
            resolved,
            offending=offending,
            reason=(
                f"SANDBOX_MODE={resolved} but {len(offending)} enabled connector(s) "
                f"carry non-synthetic data ({', '.join(offending)}); "
                "isolated mode is required to serve internal/banking data"
            ),
        )
    return AdmissionResult(True, resolved, reason="local sandbox: only synthetic connectors enabled")


def assert_admission(
    connectors: Iterable[ConnectorManifest],
    *,
    mode: str | None = None,
    env_value: str | None = None,
) -> AdmissionResult:
    """Boot assertion. Raises ``AdmissionError`` (fail-closed) if unsafe."""
    result = evaluate_admission(connectors, mode=mode, env_value=env_value)
    if not result.admitted:
        raise AdmissionError(result.reason)
    return result
