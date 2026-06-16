"""Per-run sandbox spawn wrapper (P2.1 + P2.3).

Replaces the bare ``asyncio.create_subprocess_shell`` / ``create_subprocess_exec``
calls in ``tool_execution.py`` (lines 651 / 676) for the bash/python tools when
``SANDBOX_MODE`` requires container isolation. Each tool call runs inside a
throwaway Docker container built from ``docker/sandbox.Dockerfile``:

  * non-root (``--user 10001:10001``), all caps dropped, ``--read-only`` rootfs
    with a tmpfs scratch, ``--security-opt no-new-privileges``.
  * env is STRIPPED -- only TERM/COLUMNS/LINES/HOME are injected, so no ambient
    host credentials (the ``{**os.environ, ...}`` anti-pattern) reach the run.
  * network is egress-controlled (default ``--network none``); reachable hosts are
    decided by ``sandbox_egress`` which REUSES ``url_security._BLOCKED_NETWORKS``.

P2.3 bidi-stream + cancellation: the container's stdout/stderr stream back through
the existing ``_run_subprocess_streaming`` helper (line-buffered ring buffer +
progress callback). On SSE teardown the agent loop cancels the asyncio task ->
``CancelledError`` propagates -> we ``docker kill --signal=KILL`` the named
container (SIGKILL), so a torn-down stream never leaves an orphaned container.

P2.4 cloud-ready seam: ``spawn_sandboxed`` is the single entry point; a future
remote/Firecracker backend swaps the ``_docker_argv`` builder without touching
``tool_execution``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional

# Reuse the streaming/cancel mechanics already proven for local subprocesses.
from src.tool_execution import _run_subprocess_streaming

SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "circit/odysseus-sandbox:local")
# Container-internal mount point for the run's workspace (matches sandbox.Dockerfile).
_CONTAINER_WORK = "/sandbox/work"


class SandboxUnavailableError(RuntimeError):
    """Raised when container isolation is required but Docker is not usable."""


@dataclass(frozen=True)
class SandboxSpec:
    """Hardened-run parameters. Defaults are the locked-down profile."""

    image: str = SANDBOX_IMAGE
    network: str = "none"          # P2.2: deny-by-default; egress gated separately.
    read_only_rootfs: bool = True
    drop_all_caps: bool = True
    no_new_privileges: bool = True
    memory: str = "512m"
    pids_limit: int = 256
    scratch_tmpfs: str = "/sandbox/scratch"
    extra_env: Dict[str, str] = field(default_factory=dict)


def docker_available() -> bool:
    return shutil.which("docker") is not None


def _sanitized_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The ONLY env the container sees. Deliberately NOT ``{**os.environ}`` --
    that anti-pattern (tool_execution.py:~640, tool_implementations.py:4359) leaks
    every host secret into the run. Mirror the terminal niceties only."""
    env = {
        "TERM": "xterm-256color",
        "COLUMNS": "120",
        "LINES": "40",
        "HOME": _CONTAINER_WORK,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    if extra:
        env.update(extra)
    return env


def _docker_argv(
    spec: SandboxSpec,
    container_name: str,
    workspace: Optional[str],
    payload_argv: list[str],
) -> list[str]:
    """Build the `docker run` argv applying every runtime hardening flag."""
    argv: list[str] = [
        "docker", "run",
        "--rm",
        "--name", container_name,
        "--user", "10001:10001",
        "--network", spec.network,
        "--memory", spec.memory,
        "--pids-limit", str(spec.pids_limit),
        "--workdir", _CONTAINER_WORK,
    ]
    if spec.read_only_rootfs:
        # uid/gid pin the tmpfs to the non-root sandbox user; a default tmpfs
        # mounts root-owned and the dropped-cap process can't write to it.
        argv += [
            "--read-only",
            "--tmpfs", f"{spec.scratch_tmpfs}:rw,noexec,nosuid,uid=10001,gid=10001",
        ]
    if spec.drop_all_caps:
        argv += ["--cap-drop", "ALL"]
    if spec.no_new_privileges:
        argv += ["--security-opt", "no-new-privileges"]

    # Strip env, then inject only the sanitized allowlist.
    for k, v in _sanitized_env(spec.extra_env).items():
        argv += ["--env", f"{k}={v}"]

    # Mount the run workspace read-write so write_file/bash output persists for the
    # run, but nothing else from the host is visible.
    if workspace:
        argv += ["--volume", f"{workspace}:{_CONTAINER_WORK}:rw"]

    argv.append(spec.image)
    argv += payload_argv
    return argv


async def _docker_kill(container_name: str) -> None:
    """Best-effort SIGKILL of the named container (P2.3 teardown path)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", "--signal=KILL", container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


async def spawn_sandboxed(
    tool: str,
    content: str,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    workspace: Optional[str] = None,
    spec: Optional[SandboxSpec] = None,
    python_executable: str = "python",
) -> tuple[str, str, Optional[int], bool]:
    """Run a bash/python tool call inside a throwaway hardened container.

    Returns ``(stdout, stderr, return_code, timed_out)`` -- the SAME shape as
    ``_run_subprocess_streaming`` so ``tool_execution._direct_fallback`` can swap
    its ``create_subprocess_*`` call for this without changing downstream parsing.

    On ``asyncio.CancelledError`` (SSE teardown) the container is SIGKILLed and the
    cancellation is re-raised, matching the local path's behaviour.
    """
    spec = spec or SandboxSpec()
    if not docker_available():
        # Caller (tool_execution) decides whether to fall back to the constrained
        # local subprocess (only permitted on dev boxes) or fail-closed.
        raise SandboxUnavailableError("docker not on PATH; container isolation unavailable")

    if tool == "bash":
        payload = ["/bin/sh", "-c", content]
    elif tool == "python":
        # -I isolated mode mirrors the local python path (no site, no PYTHONPATH).
        payload = [python_executable, "-I", "-c", content]
    else:
        raise ValueError(f"sandbox_runner only handles bash/python, got {tool!r}")

    container_name = f"odys-sbx-{uuid.uuid4().hex[:12]}"
    argv = _docker_argv(spec, container_name, workspace, payload)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        return await _run_subprocess_streaming(
            proc, timeout=timeout, progress_cb=progress_cb,
        )
    except asyncio.CancelledError:
        # _run_subprocess_streaming already kills the `docker run` client proc;
        # also SIGKILL the container itself in case it detached from the client.
        await _docker_kill(container_name)
        raise
    finally:
        # If the run timed out, _run_subprocess_streaming killed the client but the
        # container may linger -- reap it.
        if proc.returncode is None or (proc.returncode and proc.returncode != 0):
            await _docker_kill(container_name)
