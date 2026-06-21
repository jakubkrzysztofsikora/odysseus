"""Publish a transient image to a public https URL via Tailscale Funnel.

Some third-party video APIs (Seedance image-to-video) require the start-frame
image to be reachable at a *public* https URL — they explicitly reject base64
data URIs and any localhost/private/tailnet address. Odysseus is a tailnet-only
deployment, so to support image-to-video we briefly expose a single downscaled
frame on the public internet, hand the URL to the video API, and tear the
exposure down the moment the job finishes.

Design constraints (security):
  * Dedicated port (FRAME_FUNNEL_PORT), never the Odysseus app port. The static
    server only serves files out of one transient directory — it cannot reach
    the app, the data dir, or anything else.
  * Frames get an unguessable random filename (no enumeration).
  * The frame is downscaled + re-encoded to JPEG before it ever leaves the box,
    stripping EXIF (GPS/camera metadata) and shrinking to a reference-frame size.
  * Funnel is per-job: brought up around a single generation and torn down in a
    ``finally`` regardless of outcome. The frame file is deleted too.
  * A short hard TTL bounds exposure even if teardown is somehow skipped.

Public surface added: while a job runs, exactly one random-named JPEG is
fetchable at ``https://<node>.<tailnet>.ts.net:<port>/<random>.jpg``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

# Tailscale Funnel only permits ports 443, 8443, 10000. 443 fronts the app, so
# frame publishing uses 10000 — a distinct, app-isolated port.
FRAME_FUNNEL_PORT = int(os.getenv("ODYSSEUS_FRAME_FUNNEL_PORT", "10000"))
# Loopback port the tiny static file server binds; funnel proxies to it.
FRAME_HTTP_PORT = int(os.getenv("ODYSSEUS_FRAME_HTTP_PORT", "7861"))
# Directory the static server is locked to. Only downscaled frames live here.
FRAME_DIR = Path(os.getenv("ODYSSEUS_FRAME_DIR", os.path.join(DATA_DIR, "i2v_frames")))
# Hard ceiling on a single funnel exposure, even if teardown is skipped.
MAX_EXPOSURE_SECONDS = int(os.getenv("ODYSSEUS_FRAME_MAX_EXPOSURE_SECONDS", "600"))
# Downscale target — Seedance only needs a reference frame, not the original.
MAX_FRAME_EDGE = int(os.getenv("ODYSSEUS_FRAME_MAX_EDGE", "1280"))
JPEG_QUALITY = int(os.getenv("ODYSSEUS_FRAME_JPEG_QUALITY", "85"))


def _tailnet_funnel_host() -> Optional[str]:
    """The node's public funnel hostname, e.g. mac-studio-jakub.<tailnet>.ts.net.

    Read from ``tailscale status --json`` (Self.DNSName) so this is not
    hardcoded to one machine. Returns None when tailscale/funnel is unavailable.
    """
    try:
        import json

        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout or "{}")
        dns = (data.get("Self", {}) or {}).get("DNSName", "") or ""
        return dns.rstrip(".") or None
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("frame_publish: could not resolve funnel host: %s", exc)
        return None


def _downscale_to_jpeg(src_path: str) -> bytes:
    """Re-encode an image to a downscaled JPEG, stripping metadata (EXIF/GPS).

    Raises on unreadable/non-image input so callers can fail the job cleanly.
    """
    from PIL import Image

    with Image.open(src_path) as im:
        im = im.convert("RGB")  # drops alpha + any embedded profile/EXIF
        im.thumbnail((MAX_FRAME_EDGE, MAX_FRAME_EDGE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()


def _wait_port(port: int, up: bool, timeout: float = 6.0) -> bool:
    """Poll a loopback port until it is (up=True) or is not (up=False) listening."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.5)
            listening = s.connect_ex(("127.0.0.1", port)) == 0
        if listening == up:
            return True
        time.sleep(0.2)
    return False


class PublishedFrame:
    """Handle for a published frame; use as an async context manager.

    On enter: downscales the source image, starts a frame-scoped static server,
    brings up the funnel, and exposes ``.public_url``. On exit: tears the funnel
    down, stops the server, and deletes the frame — always, even on error.
    """

    def __init__(self, source_image_path: str):
        self._source = source_image_path
        self._proc: Optional[subprocess.Popen] = None
        self._frame_path: Optional[Path] = None
        self.public_url: Optional[str] = None

    async def __aenter__(self) -> "PublishedFrame":
        host = _tailnet_funnel_host()
        if not host:
            raise RuntimeError(
                "Tailscale funnel host unavailable — cannot publish a public "
                "frame for image-to-video. Is tailscale up on this node?"
            )

        # 1. Downscale + strip metadata before anything is exposed.
        jpeg = await asyncio.to_thread(_downscale_to_jpeg, self._source)

        # 2. Write to the locked frame dir under an unguessable name.
        FRAME_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(FRAME_DIR, 0o700)
        name = f"{secrets.token_urlsafe(24)}.jpg"
        self._frame_path = FRAME_DIR / name
        self._frame_path.write_bytes(jpeg)

        # 3. Start a static server locked to FRAME_DIR on a loopback port.
        #    http.server serves only files under its cwd — scoped to frames.
        self._proc = subprocess.Popen(
            ["python3", "-m", "http.server", str(FRAME_HTTP_PORT), "--bind", "127.0.0.1"],
            cwd=str(FRAME_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not await asyncio.to_thread(_wait_port, FRAME_HTTP_PORT, True):
            raise RuntimeError("frame static server did not come up")

        # 4. Bring up the funnel (per-job, background).
        rc = await asyncio.to_thread(
            lambda: subprocess.run(
                ["tailscale", "funnel", "--bg",
                 f"--https={FRAME_FUNNEL_PORT}",
                 f"http://127.0.0.1:{FRAME_HTTP_PORT}"],
                capture_output=True, text=True, timeout=15,
            )
        )
        if rc.returncode != 0:
            raise RuntimeError(f"tailscale funnel up failed: {rc.stderr[:200]}")

        self.public_url = f"https://{host}:{FRAME_FUNNEL_PORT}/{name}"
        logger.info("frame_publish: exposed %s (%d bytes)", self.public_url, len(jpeg))

        # Safety net: auto-teardown if the job overruns MAX_EXPOSURE_SECONDS.
        self._watchdog = asyncio.ensure_future(self._auto_teardown())
        return self

    async def _auto_teardown(self) -> None:
        try:
            await asyncio.sleep(MAX_EXPOSURE_SECONDS)
            logger.warning("frame_publish: max exposure reached; forcing teardown")
            await asyncio.to_thread(self._teardown)
        except asyncio.CancelledError:
            pass

    def _teardown(self) -> None:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["tailscale", "funnel", f"--https={FRAME_FUNNEL_PORT}", "off"],
                capture_output=True, text=True, timeout=10,
            )
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
                self._proc.wait(timeout=5)
        if self._frame_path is not None:
            with contextlib.suppress(Exception):
                self._frame_path.unlink(missing_ok=True)

    async def __aexit__(self, *exc) -> None:
        if getattr(self, "_watchdog", None):
            self._watchdog.cancel()
            with contextlib.suppress(Exception):
                await self._watchdog
        await asyncio.to_thread(self._teardown)
        self.public_url = None
        logger.info("frame_publish: torn down")


def cleanup_stale_frames(max_age_seconds: int = MAX_EXPOSURE_SECONDS) -> int:
    """Best-effort sweep of leftover frames (e.g. after a crash). Returns count."""
    import time

    if not FRAME_DIR.exists():
        return 0
    now = time.time()
    removed = 0
    for f in FRAME_DIR.glob("*.jpg"):
        with contextlib.suppress(Exception):
            if now - f.stat().st_mtime > max_age_seconds:
                f.unlink(missing_ok=True)
                removed += 1
    return removed
