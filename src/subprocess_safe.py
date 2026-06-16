"""Sanitized environment for subprocess spawns (Circit P6.2).

The locked decision is: strip the ambient environment on **all** subprocess
spawns, not only the Docker sandbox. A bare ``create_subprocess_exec(...)`` with
no ``env=`` argument inherits the *entire* parent process environment, which in
this app includes provider API keys, ``COWORK_AUTH_TOKEN``-style secrets,
``LLM_*`` config, OAuth/OBO material, cloud credentials, etc. Any tool that
shells out (``npx`` probes, ``yt-dlp``, ffmpeg, model-discovery curls, user
``bash``/``python`` tools) therefore hands those secrets to the child unless the
spawn explicitly scrubs them.

``src/sandbox_runner.py`` already does this for the container path via its own
``_sanitized_env`` (TERM/COLUMNS/LINES/HOME only). This module is the
*non-sandbox* counterpart: the same fail-closed default for in-process spawns,
exposed as one helper so every spawn site converges on the same allowlist
instead of re-deriving it.

Policy: **deny by default.** Only an explicit allowlist of innocuous,
non-secret variables is passed through (locale, terminal sizing, HOME/PATH so
the child can find its binaries). A caller that genuinely needs an extra var
must name it via ``extra`` / ``passthrough`` — secrets never ride along
implicitly.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, Mapping, Optional

# Variables that are safe to inherit: they carry no credentials and breaking
# them breaks legitimate child behavior (locale, terminal sizing, binary
# discovery). PATH is included so spawns like `npx`/`yt-dlp` resolve; if a
# deployment wants a pinned PATH it can override via `extra`.
_SAFE_PASSTHROUGH = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "COLUMNS",
    "LINES",
    "TMPDIR",
    "TZ",
    # SystemRoot/COMSPEC are required on Windows for many child processes to
    # start at all; they carry no secret.
    "SystemRoot",
    "COMSPEC",
    "PATHEXT",
)

# Substrings that mark a variable as sensitive. Used only by `audit_leaky_env`
# (a diagnostic), never to *build* the child env — the child env is built from
# the positive allowlist above, so a new secret-shaped var is excluded by
# default even if it doesn't match these.
_SECRET_MARKERS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
    "SESSION", "COOKIE", "AUTH", "PRIVATE", "OBO", "BEARER",
)


def clean_subprocess_env(
    extra: Optional[Mapping[str, str]] = None,
    *,
    passthrough: Optional[Iterable[str]] = None,
    base_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build a minimal, secret-free environment for a subprocess.

    Start from nothing, copy only the ``_SAFE_PASSTHROUGH`` allowlist (plus any
    explicitly-named ``passthrough`` keys) out of ``base_env`` (defaults to the
    real ``os.environ``), then layer ``extra`` on top. The result is what the
    child should receive as ``env=``.

    This is deliberately NOT ``{**os.environ, **extra}`` — that anti-pattern is
    exactly what leaks credentials to children. Pass the return value as the
    spawn's ``env=`` argument."""
    src = os.environ if base_env is None else base_env
    allow = set(_SAFE_PASSTHROUGH)
    if passthrough:
        allow.update(passthrough)
    env: Dict[str, str] = {}
    for key in allow:
        val = src.get(key)
        if val is not None:
            env[key] = val
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def audit_leaky_env(env: Mapping[str, str]) -> list:
    """Return the names of variables in ``env`` that look secret-bearing.

    Diagnostic for tests / the P6 spawn-site sweep: assert that a spawn's
    computed env contains none of these. Not used in the hot path."""
    leaks = []
    for key in env:
        up = key.upper()
        if any(marker in up for marker in _SECRET_MARKERS):
            leaks.append(key)
    return leaks
