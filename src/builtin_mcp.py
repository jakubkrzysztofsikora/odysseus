"""
builtin_mcp.py

Auto-registration of built-in MCP servers on startup.
Each server runs as a stdio subprocess managed by McpManager.
"""

import logging
import os
import shutil
import sys
import asyncio
import json
from pathlib import Path

from core.constants import DATA_DIR
from core.platform_compat import IS_WINDOWS, which_tool

logger = logging.getLogger(__name__)


def _find_npx() -> str:
    """Find the npx binary, checking common locations if not on PATH.

    On Windows the shim is `npx.cmd`, which `which_tool` resolves via PATHEXT.
    """
    npx = which_tool("npx")
    if npx:
        return npx
    if IS_WINDOWS:
        # Minimal-PATH fallbacks: npm's global bin lives under %APPDATA%\npm,
        # and node's installer dir carries npx.cmd alongside node.exe.
        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        for candidate in (
            os.path.join(appdata, "npm", "npx.cmd"),
            r"C:\Program Files\nodejs\npx.cmd",
        ):
            if os.path.isfile(candidate):
                return candidate
        node = which_tool("node")
        if node:
            cand = os.path.join(os.path.dirname(node), "npx.cmd")
            if os.path.isfile(cand):
                return cand
        return "npx.cmd"  # fallback, will fail with a clear error
    # Common POSIX locations when PATH is minimal (e.g. systemd)
    for candidate in [
        os.path.expanduser("~/.npm-global/bin/npx"),
        os.path.expanduser("~/.local/bin/npx"),
        "/usr/local/bin/npx",
        "/usr/bin/npx",
    ]:
        if os.path.isfile(candidate):
            return candidate
    # Try to find node and use npx from same dir
    node = shutil.which("node")
    if node:
        npx_candidate = os.path.join(os.path.dirname(node), "npx")
        if os.path.isfile(npx_candidate):
            return npx_candidate
    return "npx"  # fallback, will fail with a clear error

# Server definitions: id -> (script path relative to project root, display name)
#
# bash / python / filesystem / web_search were folded into native in-process
# execution (src/tool_execution.py:_direct_fallback). Those trivial subprocess
# wrappers are gone.
#
# image_gen / memory / rag / email still run as stdio MCP servers — each
# carries hundreds of LOC of unique IMAP / HTTP / manager logic not worth
# duplicating into the native path right now.
_BUILTIN_SERVERS = {
    "image_gen":  ("mcp_servers/image_gen_server.py",  "Built-in: Image Generation"),
    "memory":     ("mcp_servers/memory_server.py",     "Built-in: Memory"),
    "rag":        ("mcp_servers/rag_server.py",        "Built-in: RAG"),
    "email":      ("mcp_servers/email_server.py",      "Built-in: Email"),
}

# NPX-based built-in servers (run via npx, not Python)
_BUILTIN_NPX_SERVERS = {
    "builtin_browser": {
        "name": "Built-in: Browser",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest", "--headless", "--caps", "vision"],
    },
}

# Global flag to disable MCP if there are compatibility issues
MCP_DISABLED = os.environ.get("ODYSSEUS_DISABLE_MCP", "").lower() in ("1", "true", "yes")


def _mcp_oauth_token_file(server_id: str) -> str:
    return str((Path(DATA_DIR) / "mcp_oauth" / f"{server_id}.json").resolve(strict=False))


DEFAULT_CIRCIT_TENANT_ID = "b6560c52-065a-424b-90b1-5340eab75de9"


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return default


def _remote_oauth_config(
    server_id: str,
    *,
    client_name: str = "Circitron",
    client_id: str = "",
    client_secret: str = "",
    token_endpoint_auth_method: str = "",
) -> dict:
    public_url = (
        os.environ.get("APP_PUBLIC_URL")
        or os.environ.get("OAUTH_REDIRECT_BASE_URL")
        or "http://localhost:7000"
    ).rstrip("/")
    cfg = {
        "provider": "mcp",
        "client_name": client_name,
        "token_file": _mcp_oauth_token_file(server_id),
        "redirect_uris": [f"{public_url}/api/mcp/oauth/callback"],
    }
    if client_id:
        cfg["client_id"] = client_id
        cfg["token_endpoint_auth_method"] = token_endpoint_auth_method or (
            "client_secret_post" if client_secret else "none"
        )
    if client_secret:
        cfg["client_secret"] = client_secret
    return cfg


def _workiq_oauth_config(server_id: str) -> dict:
    return _remote_oauth_config(
        server_id,
        client_id=_first_env(
            "WORKIQ_MCP_CLIENT_ID",
            "M365_MCP_CLIENT_ID",
            "MICROSOFT_365_MCP_CLIENT_ID",
            "MICROSOFT_MCP_CLIENT_ID",
        ),
        client_secret=_first_env(
            "WORKIQ_MCP_CLIENT_SECRET",
            "M365_MCP_CLIENT_SECRET",
            "MICROSOFT_365_MCP_CLIENT_SECRET",
            "MICROSOFT_MCP_CLIENT_SECRET",
        ),
        token_endpoint_auth_method=_first_env(
            "WORKIQ_MCP_TOKEN_ENDPOINT_AUTH_METHOD",
            "M365_MCP_TOKEN_ENDPOINT_AUTH_METHOD",
        ),
    )


def _workiq_url(server_name: str) -> str:
    tenant_id = _first_env(
        "WORKIQ_MCP_TENANT_ID",
        "MICROSOFT_TENANT_ID",
        "AZURE_TENANT_ID",
        "ENTRA_TENANT_ID",
        "CIRCIT_TENANT_ID",
        default=DEFAULT_CIRCIT_TENANT_ID,
    )
    base_url = _first_env(
        "WORKIQ_MCP_BASE_URL",
        default="https://agent365.svc.cloud.microsoft/agents",
    ).rstrip("/")
    return f"{base_url}/tenants/{tenant_id}/servers/{server_name}"


def _curated_remote_mcp_servers() -> dict:
    exchange_url = (
        os.environ.get("M365_EXCHANGE_MCP_URL")
        or os.environ.get("MICROSOFT_365_EXCHANGE_MCP_URL")
        or ""
    ).strip()
    exchange_url = exchange_url or "https://mcp.svc.cloud.microsoft/enterprise"
    servers = {
        "circit_sentry": {
            "name": "Sentry",
            "url": "https://mcp.sentry.dev/mcp",
            "enabled": True,
        },
        "circit_atlassian": {
            "name": "Atlassian",
            "url": "https://mcp.atlassian.com/v1/mcp/authv2",
            "enabled": True,
        },
        "circit_slack": {
            "name": "Slack",
            "url": "https://circitron-app.nicestone-ab7e633c.northeurope.azurecontainerapps.io/api/mcp",
            "enabled": True,
        },
        "circit_microsoft_graph": {
            "name": "Microsoft Graph",
            "url": "https://mcp.svc.cloud.microsoft/enterprise",
            "enabled": True,
            "oauth_config": _workiq_oauth_config("circit_microsoft_graph"),
        },
        "circit_workiq_mail": {
            "name": "Microsoft 365 Exchange Mail",
            "url": _workiq_url("mcp_MailTools"),
            "enabled": True,
            "oauth_config": _workiq_oauth_config("circit_workiq_mail"),
        },
        "circit_workiq_calendar": {
            "name": "Microsoft 365 Exchange Calendar",
            "url": _workiq_url("mcp_CalendarTools"),
            "enabled": True,
            "oauth_config": _workiq_oauth_config("circit_workiq_calendar"),
        },
        "circit_m365_exchange": {
            "name": "Microsoft 365 Exchange",
            "url": exchange_url,
            "enabled": True,
            "oauth_config": _workiq_oauth_config("circit_m365_exchange"),
        },
    }
    return servers


def seed_curated_remote_mcp_servers() -> None:
    """Seed Circit-wide remote MCP entries into the shared MCP catalog.

    These rows are global, so every user sees the same curated integrations and
    completes per-user OAuth where the remote server requires it.
    """
    if MCP_DISABLED:
        return
    from core.database import McpServer, SessionLocal

    db = SessionLocal()
    try:
        for server_id, cfg in _curated_remote_mcp_servers().items():
            existing = db.query(McpServer).filter(McpServer.id == server_id).first()
            oauth_config = (
                json.dumps(cfg.get("oauth_config") or _remote_oauth_config(server_id))
                if cfg["enabled"]
                else None
            )
            if existing:
                was_placeholder = str(existing.url or "").endswith(".invalid/mcp")
                existing.name = cfg["name"]
                existing.transport = "streamable_http"
                existing.command = None
                existing.args = "[]"
                existing.env = "{}"
                existing.url = cfg["url"]
                existing.oauth_config = oauth_config
                if not cfg["enabled"]:
                    existing.is_enabled = False
                elif was_placeholder:
                    existing.is_enabled = True
                continue
            db.add(McpServer(
                id=server_id,
                name=cfg["name"],
                transport="streamable_http",
                command=None,
                args="[]",
                env="{}",
                url=cfg["url"],
                is_enabled=bool(cfg["enabled"]),
                oauth_config=oauth_config,
            ))
        db.commit()
    finally:
        db.close()


async def register_builtin_servers(mcp_manager):
    """Connect all built-in MCP servers to the manager."""
    if MCP_DISABLED:
        logger.info("Built-in MCP servers disabled via ODYSSEUS_DISABLE_MCP")
        return

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python = sys.executable

    async def _connect_python_server(server_id: str, script_path: str, name: str):
        try:
            ok = await mcp_manager.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=python,
                args=[script_path],
                env={"PYTHONPATH": base_dir},
            )
            if ok:
                logger.info(f"Built-in MCP server registered: {name}")
            else:
                logger.warning(f"Built-in MCP server failed to connect: {name}")
        except asyncio.CancelledError:
            logger.warning(f"Built-in MCP server {name} cancelled")
            raise
        except BaseException as e:
            logger.warning(f"Built-in MCP server {name} error: {type(e).__name__}: {e}")

    for server_id, (script, name) in _BUILTIN_SERVERS.items():
        script_path = os.path.join(base_dir, script)
        if not os.path.exists(script_path):
            logger.warning(f"Built-in MCP server script not found: {script_path}")
            continue
        asyncio.create_task(_connect_python_server(server_id, script_path, name))

    # Register NPX-based servers in the background (they take longer to start)
    npx_path = _find_npx()
    logger.info(f"NPX binary resolved to: {npx_path}")

    async def _start_npx_servers():
        await asyncio.sleep(3)  # let Python servers finish first
        for server_id, cfg in _BUILTIN_NPX_SERVERS.items():
            # Skip the server if its npx package isn't cached. Without this
            # check, npx would try to download/install the package on first
            # use, which can take minutes (or hang) on fresh installs without
            # Playwright system deps. Wrapping that in asyncio.wait_for to
            # bound the wait sounds reasonable, but mcp.client.stdio uses an
            # internal anyio task group that can't survive the resulting
            # cross-task cancellation: it raises "Attempted to exit cancel
            # scope in a different task than it was entered in" in a sibling
            # task, which cascades cancellations into the rest of the event
            # loop and downs the app. Detecting installed-state up-front lets
            # us bail with a useful warning before we ever touch stdio_client.
            args = cfg["args"]
            pkg_spec = _npx_package_from_args(args)
            if pkg_spec and not await _is_npx_package_cached(npx_path, pkg_spec):
                logger.warning(
                    f"{cfg['name']} is not available.\n"
                    f"  Reason: npm package {pkg_spec!r} is not installed in the npx cache.\n"
                    f"  Impact: tools provided by this MCP server will be unavailable.\n"
                    f"  Fix:    {os.path.basename(npx_path)} -y {pkg_spec} --version\n"
                    f"          (run once, then restart Odysseus)\n"
                    f"  Notes:  this server is optional; see README.md "
                    f"'Built-in MCP servers' for details."
                )
                continue

            logger.info(f"Starting NPX server: {cfg['name']} ({npx_path} {' '.join(args)})")
            try:
                ok = await mcp_manager.connect_server(
                    server_id=server_id,
                    name=cfg["name"],
                    transport="stdio",
                    command=npx_path,
                    args=args,
                )
                if ok:
                    logger.info(f"Built-in NPX server registered: {cfg['name']}")
                else:
                    logger.warning(f"Built-in NPX server failed to connect: {cfg['name']}")
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                logger.warning(f"Built-in NPX server {cfg['name']} error: {type(e).__name__}: {e}")

    asyncio.create_task(_start_npx_servers())


def _npx_package_from_args(args):
    """Pick the package spec out of an npx args list shaped like
    ['-y', '<package@version>', ...flags]. Returns None if the
    convention doesn't match (we then skip the cache check and just
    try the connect)."""
    if not args:
        return None
    if "-y" in args:
        idx = args.index("-y") + 1
        if idx < len(args) and not args[idx].startswith("-"):
            return args[idx]
    # No -y prefix: first non-flag arg is the package
    for a in args:
        if not a.startswith("-"):
            return a
    return None


async def _is_npx_package_cached(npx_path, package_spec, timeout_s=5):
    """Probe whether an npx package is already in the local cache.

    Runs `npx --no-install <pkg> --version`. --no-install tells npx to
    fail instead of downloading, so a cache miss returns fast. We treat
    "exited 0 with non-empty stdout" as proof of a working cached copy.
    Anything else (non-zero exit, empty stdout, timeout, missing npx,
    network error) means we should skip the server.
    """
    try:
        # P6.2: strip the ambient env so provider keys / auth tokens never reach
        # the npx child. clean_subprocess_env() passes only the safe allowlist
        # (PATH/HOME/locale) needed for npx to resolve and run.
        from src.subprocess_safe import clean_subprocess_env
        proc = await asyncio.create_subprocess_exec(
            npx_path, "--no-install", package_spec, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clean_subprocess_env(),
        )
    except (OSError, ValueError):
        return False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return False
    return proc.returncode == 0 and bool(stdout.strip())
