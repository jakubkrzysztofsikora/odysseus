# routes/mcp_routes.py
"""MCP (Model Context Protocol) server management routes."""
import asyncio
import json
import os
import secrets
import uuid
import urllib.parse
import html
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
import logging
import httpx

from core.database import McpServer, SessionLocal
from core.middleware import require_admin
from src.mcp_manager import McpManager, _oauth_token_expired, normalize_mcp_transport

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

MCP_OAUTH_DEFAULT_REDIRECT = os.getenv(
    "MCP_OAUTH_REDIRECT_URI",
    "http://localhost:7860/api/mcp/oauth/callback",
)
_PENDING_MCP_OAUTH: dict[str, dict] = {}
_MCP_OAUTH_STATE_TO_FLOW: dict[str, str] = {}


def _json_or(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _server_runtime_config(srv: McpServer) -> tuple[list, dict, dict | None]:
    return (
        _json_or(srv.args, []),
        _json_or(srv.env, {}),
        _json_or(srv.oauth_config, None),
    )


def _oauth_provider(oauth_cfg: dict | None) -> str:
    return str((oauth_cfg or {}).get("provider") or "").strip().lower()


def _oauth_token_missing(oauth_cfg: dict | None) -> bool:
    if not oauth_cfg:
        return False
    token_file = os.path.expanduser(oauth_cfg.get("token_file", ""))
    return bool(token_file) and not os.path.exists(token_file)


def _oauth_token_needs_authorization(oauth_cfg: dict | None) -> bool:
    if not oauth_cfg:
        return False
    token_file = os.path.expanduser(oauth_cfg.get("token_file", ""))
    return bool(token_file) and (not os.path.exists(token_file) or _oauth_token_expired(token_file))


def _default_remote_mcp_oauth_config(server_id: str) -> dict:
    return {
        "provider": "mcp",
        "client_name": "Odysseus",
        "token_file": f"~/.odysseus/mcp-oauth/{server_id}.json",
        "redirect_uris": [MCP_OAUTH_DEFAULT_REDIRECT],
    }


def _looks_like_remote_auth_error(error: str | None) -> bool:
    lower = (error or "").lower()
    return any(marker in lower for marker in ("401", "unauthorized", "authentication", "oauth"))


def _mcp_remote_url(args: list) -> str | None:
    for idx, arg in enumerate(args or []):
        if arg == "mcp-remote" or str(arg).startswith("mcp-remote@"):
            for candidate in args[idx + 1:]:
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    return candidate
    return None


def _mcp_remote_has_header(args: list) -> bool:
    return any(arg == "--header" or str(arg).startswith("--header=") for arg in (args or []))


def _is_interactive_mcp_remote(srv: McpServer, args: list) -> bool:
    return (
        normalize_mcp_transport(srv.transport) == "stdio"
        and _mcp_remote_url(args) is not None
        and not _mcp_remote_has_header(args)
    )


def _oauth_runtime_config(srv: McpServer, args: list, env: dict, oauth_cfg: dict | None) -> dict:
    """Return a connect_server config, converting mcp-remote stdio to native remote OAuth."""
    remote_url = _mcp_remote_url(args)
    if oauth_cfg and _is_interactive_mcp_remote(srv, args) and remote_url:
        return {
            "transport": "streamable_http",
            "command": None,
            "args": [],
            "env": env,
            "url": remote_url,
            "oauth_config": oauth_cfg,
        }
    return {
        "transport": srv.transport,
        "command": srv.command,
        "args": args,
        "env": env,
        "url": srv.url,
        "oauth_config": oauth_cfg,
    }


async def _remote_advertises_oauth(url: str | None) -> bool:
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            resp = await client.get(url, headers={"Accept": "application/json, text/event-stream"})
        www_auth = resp.headers.get("www-authenticate", "")
        return resp.status_code == 401 and "bearer" in www_auth.lower()
    except Exception as e:
        logger.debug("Remote MCP OAuth probe failed for %s: %s", url, e)
        return False


def _load_disabled_map():
    """Load per-server disabled tool sets from DB."""
    db = SessionLocal()
    try:
        disabled_map = {}
        for srv in db.query(McpServer).all():
            if srv.disabled_tools:
                try:
                    names = json.loads(srv.disabled_tools)
                    if names:
                        disabled_map[srv.id] = set(names)
                except (json.JSONDecodeError, TypeError):
                    pass
        return disabled_map
    finally:
        db.close()


def setup_mcp_routes(mcp_manager: McpManager):
    """Setup MCP routes with the provided manager."""

    @router.get("/servers")
    def list_servers(request: Request):
        """List all configured MCP servers with connection status."""
        require_admin(request)
        db = SessionLocal()
        try:
            servers = db.query(McpServer).all()
            result = []
            for srv in servers:
                status = mcp_manager.get_server_status(srv.id)
                args = json.loads(srv.args) if srv.args else []
                env = json.loads(srv.env) if srv.env else {}
                oauth_cfg = json.loads(srv.oauth_config) if srv.oauth_config else None
                is_interactive_remote = _is_interactive_mcp_remote(srv, args)
                needs_oauth = (
                    status.get("status") == "needs_oauth"
                    or _oauth_token_needs_authorization(oauth_cfg)
                    or (is_interactive_remote and status.get("status") != "connected")
                )
                disabled_list = json.loads(srv.disabled_tools) if srv.disabled_tools else []
                total_tools = status.get("tool_count", 0)
                result.append({
                    "id": srv.id,
                    "name": srv.name,
                    "transport": srv.transport,
                    "command": srv.command,
                    "args": args,
                    "env": env,
                    "url": srv.url,
                    "is_enabled": srv.is_enabled,
                    "status": status.get("status", "disconnected"),
                    "tool_count": total_tools,
                    "disabled_tool_count": len(disabled_list),
                    "enabled_tool_count": max(0, total_tools - len(disabled_list)),
                    "error": status.get("error"),
                    "has_oauth": oauth_cfg is not None or is_interactive_remote,
                    "needs_oauth": needs_oauth,
                })
            return result
        finally:
            db.close()

    @router.post("/servers")
    async def add_server(
        request: Request,
        name: str = Form(...),
        transport: str = Form("stdio"),
        command: str = Form(None),
        args: str = Form("[]"),
        env: str = Form("{}"),
        url: str = Form(None),
        oauth_file: str = Form(None),
        oauth_config: str = Form(None),
    ):
        """Add a new MCP server config and attempt connection. Admin-only:
        registering a stdio server is equivalent to executing arbitrary
        binaries on the host."""
        require_admin(request)
        server_id = str(uuid.uuid4())[:8]

        # Validate
        transport = normalize_mcp_transport(transport)
        if transport == "stdio" and not command:
            raise HTTPException(400, "command is required for stdio transport")
        if transport in {"sse", "streamable_http"} and not url:
            raise HTTPException(400, "url is required for remote MCP transport")
        if transport not in {"stdio", "sse", "streamable_http"}:
            raise HTTPException(400, f"unsupported MCP transport: {transport}")

        # Parse JSON fields
        try:
            parsed_args = json.loads(args) if args else []
        except json.JSONDecodeError:
            parsed_args = []
        try:
            parsed_env = json.loads(env) if env else {}
        except json.JSONDecodeError:
            parsed_env = {}

        # Parse OAuth config
        parsed_oauth_config = None
        if oauth_config:
            try:
                parsed_oauth_config = json.loads(oauth_config)
            except json.JSONDecodeError:
                pass
        elif transport in {"sse", "streamable_http"} and await _remote_advertises_oauth(url):
            parsed_oauth_config = _default_remote_mcp_oauth_config(server_id)

        # Write OAuth credentials file if provided (for Google MCP servers)
        logger.info(f"MCP add_server: oauth_file={oauth_file!r}")
        if oauth_file:
            try:
                oauth_data = json.loads(oauth_file)
                oauth_dir = os.path.expanduser(oauth_data.get("dir", ""))
                oauth_filename = oauth_data.get("filename", "")
                client_id = oauth_data.get("client_id", "")
                client_secret = oauth_data.get("client_secret", "")
                if oauth_dir and oauth_filename and client_id and client_secret:
                    os.makedirs(oauth_dir, exist_ok=True)
                    creds = {
                        "installed": {
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "redirect_uris": ["http://localhost"],
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://accounts.google.com/o/oauth2/token",
                        }
                    }
                    filepath = os.path.join(oauth_dir, oauth_filename)
                    with open(filepath, "w", encoding="utf-8") as f:
                        json.dump(creds, f, indent=2)
                    logger.info(f"Wrote OAuth credentials to {filepath}")
                    parsed_env.pop("GOOGLE_CLIENT_ID", None)
                    parsed_env.pop("GOOGLE_CLIENT_SECRET", None)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to write OAuth file: {e}")

        # Save to DB
        db = SessionLocal()
        try:
            srv = McpServer(
                id=server_id,
                name=name,
                transport=transport,
                command=command,
                args=json.dumps(parsed_args),
                env=json.dumps(parsed_env),
                url=url,
                is_enabled=True,
                oauth_config=json.dumps(parsed_oauth_config) if parsed_oauth_config else None,
            )
            db.add(srv)
            db.commit()
        finally:
            db.close()

        # Check if OAuth token already exists — skip connection attempt if not
        needs_oauth = False
        if parsed_oauth_config and _oauth_token_missing(parsed_oauth_config):
            needs_oauth = True

        connected = False
        if not needs_oauth:
            connected = await mcp_manager.connect_server(
                server_id=server_id,
                name=name,
                transport=transport,
                command=command,
                args=parsed_args,
                env=parsed_env,
                url=url,
                oauth_config=parsed_oauth_config,
            )

        status = mcp_manager.get_server_status(server_id)
        if (
            not connected
            and not parsed_oauth_config
            and transport in {"sse", "streamable_http"}
            and _looks_like_remote_auth_error(status.get("error"))
        ):
            parsed_oauth_config = _default_remote_mcp_oauth_config(server_id)
            db = SessionLocal()
            try:
                srv = db.query(McpServer).filter(McpServer.id == server_id).first()
                if srv:
                    srv.oauth_config = json.dumps(parsed_oauth_config)
                    db.commit()
            finally:
                db.close()
            needs_oauth = True

        return {
            "id": server_id,
            "name": name,
            "connected": connected,
            "status": "needs_oauth" if needs_oauth else status.get("status", "disconnected"),
            "tool_count": status.get("tool_count", 0),
            "error": "OAuth authorization required" if needs_oauth else status.get("error"),
            "needs_oauth": needs_oauth,
        }

    @router.post("/servers/{server_id}/reconnect")
    async def reconnect_server(server_id: str, request: Request):
        """Reconnect to an MCP server."""
        require_admin(request)
        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == server_id).first()
            if not srv:
                raise HTTPException(404, "Server not found")

            await mcp_manager.disconnect_server(server_id)

            args, env, oauth_config = _server_runtime_config(srv)
            if _is_interactive_mcp_remote(srv, args) and not oauth_config:
                return {
                    "connected": False,
                    "status": "needs_oauth",
                    "tool_count": 0,
                    "error": "OAuth authorization required; use Authorize",
                    "needs_oauth": True,
                }
            if _oauth_token_needs_authorization(oauth_config):
                return {
                    "connected": False,
                    "status": "needs_oauth",
                    "tool_count": 0,
                    "error": "OAuth authorization required; use Authorize",
                    "needs_oauth": True,
                }
            runtime = _oauth_runtime_config(srv, args, env, oauth_config)
            try:
                connected = await mcp_manager.connect_server(
                    server_id=server_id,
                    name=srv.name,
                    **runtime,
                )
            except BaseException as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.warning("MCP reconnect needs OAuth for %s: %s", server_id, e)
                if oauth_config:
                    return {
                        "connected": False,
                        "status": "needs_oauth",
                        "tool_count": 0,
                        "error": "OAuth authorization required; use Authorize",
                        "needs_oauth": True,
                    }
                raise

            status = mcp_manager.get_server_status(server_id)
            if oauth_config and not connected and _looks_like_remote_auth_error(status.get("error")):
                return {
                    "connected": False,
                    "status": "needs_oauth",
                    "tool_count": 0,
                    "error": "OAuth authorization required; use Authorize",
                    "needs_oauth": True,
                }
            return {
                "connected": connected,
                "status": status.get("status", "disconnected"),
                "tool_count": status.get("tool_count", 0),
                "error": status.get("error"),
                "needs_oauth": status.get("status") == "needs_oauth",
            }
        finally:
            db.close()

    @router.patch("/servers/{server_id}")
    async def toggle_server(server_id: str, request: Request, is_enabled: str = Form(...)):
        """Enable or disable an MCP server."""
        require_admin(request)
        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == server_id).first()
            if not srv:
                raise HTTPException(404, "Server not found")

            enabled = str(is_enabled).lower() == "true"
            srv.is_enabled = enabled
            db.commit()

            if enabled:
                args, env, oauth_config = _server_runtime_config(srv)
                if not _oauth_token_needs_authorization(oauth_config) and not (
                    _is_interactive_mcp_remote(srv, args) and not oauth_config
                ):
                    runtime = _oauth_runtime_config(srv, args, env, oauth_config)
                    await mcp_manager.connect_server(
                        server_id=server_id,
                        name=srv.name,
                        **runtime,
                    )
            else:
                await mcp_manager.disconnect_server(server_id)

            return {"id": server_id, "is_enabled": enabled}
        finally:
            db.close()

    @router.delete("/servers/{server_id}")
    async def delete_server(server_id: str, request: Request):
        """Remove an MCP server."""
        require_admin(request)
        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == server_id).first()
            if not srv:
                raise HTTPException(404, "Server not found")

            await mcp_manager.disconnect_server(server_id)

            db.delete(srv)
            db.commit()
            return {"status": "deleted"}
        finally:
            db.close()

    @router.get("/tools")
    def list_tools(request: Request):
        """List all discovered MCP tools across all connected servers."""
        require_admin(request)
        disabled_map = _load_disabled_map()
        return mcp_manager.get_all_tools(disabled_map)

    @router.get("/servers/{server_id}/tools")
    def list_server_tools(server_id: str, request: Request):
        """List all tools for a specific MCP server with enabled/disabled state."""
        require_admin(request)
        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == server_id).first()
            if not srv:
                raise HTTPException(404, "Server not found")
            disabled_list = json.loads(srv.disabled_tools) if srv.disabled_tools else []
            disabled_set = set(disabled_list)
        finally:
            db.close()

        all_tools = mcp_manager.get_all_tools()
        server_tools = [t for t in all_tools if t["server_id"] == server_id]
        for t in server_tools:
            t["is_disabled"] = t["name"] in disabled_set
        return server_tools

    @router.patch("/servers/{server_id}/tools")
    async def update_disabled_tools(server_id: str, request: Request):
        """Bulk update disabled tools list for a server.

        Expects JSON body: {"disabled": ["tool_name_1", "tool_name_2"]}
        """
        require_admin(request)
        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == server_id).first()
            if not srv:
                raise HTTPException(404, "Server not found")

            body = await request.json()
            disabled = body.get("disabled", [])
            if not isinstance(disabled, list):
                raise HTTPException(400, "disabled must be a list of tool names")

            srv.disabled_tools = json.dumps(disabled) if disabled else None
            db.commit()

            return {"id": server_id, "disabled_count": len(disabled)}
        finally:
            db.close()

    # ── OAuth flow for Google MCP servers ──────────────────────────

    @router.get("/oauth/authorize/{server_id}")
    async def oauth_authorize(server_id: str, request: Request):
        """Show OAuth authorization page with Google sign-in link."""
        require_admin(request)
        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == server_id).first()
            if not srv:
                raise HTTPException(404, "Server not found")
            args, _env, _oauth = _server_runtime_config(srv)
            remote_url = _mcp_remote_url(args)
            if _is_interactive_mcp_remote(srv, args) and remote_url:
                if not srv.oauth_config:
                    srv.oauth_config = json.dumps(_default_remote_mcp_oauth_config(srv.id))
                # Normalize legacy mcp-remote proxy entries to native remote MCP
                # so Odysseus can own callback state and token refresh.
                srv.transport = "streamable_http"
                srv.command = None
                srv.args = "[]"
                srv.url = remote_url
                db.commit()
                db.refresh(srv)
            if not srv.oauth_config:
                raise HTTPException(400, "Server has no OAuth config")

            oauth_cfg = json.loads(srv.oauth_config)
            if _oauth_provider(oauth_cfg) != "google":
                return await _mcp_oauth_authorize(srv, oauth_cfg, request)

            keys_file = os.path.expanduser(oauth_cfg.get("keys_file", ""))
            if not keys_file or not os.path.exists(keys_file):
                raise HTTPException(400, "OAuth keys file not found")

            with open(keys_file, encoding="utf-8") as f:
                keys_data = json.load(f)
            keys = keys_data.get("installed") or keys_data.get("web")
            if not keys:
                raise HTTPException(400, "Invalid OAuth keys file format")

            client_id = keys["client_id"]
            scopes = oauth_cfg.get("scopes", [])

            # For Desktop App creds, redirect to localhost — the user will
            # paste the resulting URL back if they're on a different device.
            redirect_uri = "http://localhost:7000/api/mcp/oauth/callback"

            params = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "access_type": "offline",
                "prompt": "consent",
                "state": server_id,
            }
            auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

            # Determine if user is accessing from the same machine
            host = request.headers.get("host", "")
            is_local = host.startswith("localhost") or host.startswith("127.0.0.1")

            if is_local:
                # Same machine — just redirect, callback will work directly
                return RedirectResponse(auth_url)
            else:
                # Remote device — show paste-back page
                return HTMLResponse(_oauth_authorize_page(auth_url, server_id, host, provider_name="Google"))
        finally:
            db.close()

    async def _mcp_oauth_authorize(srv: McpServer, oauth_cfg: dict, request: Request):
        """Start an MCP SDK OAuth flow and expose its authorization URL."""
        args, env, _ = _server_runtime_config(srv)
        loop = asyncio.get_running_loop()
        flow_id = secrets.token_urlsafe(18)
        flow = {
            "server_id": srv.id,
            "auth_url": loop.create_future(),
            "callback": loop.create_future(),
            "result": loop.create_future(),
        }
        _PENDING_MCP_OAUTH[flow_id] = flow

        async def redirect_handler(auth_url: str) -> None:
            state = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query).get("state", [None])[0]
            if state:
                _MCP_OAUTH_STATE_TO_FLOW[state] = flow_id
            if not flow["auth_url"].done():
                flow["auth_url"].set_result(auth_url)

        async def callback_handler() -> tuple[str, str | None]:
            return await asyncio.wait_for(flow["callback"], timeout=float(oauth_cfg.get("timeout", 300)))

        oauth_for_connect = dict(oauth_cfg)
        oauth_for_connect["_redirect_handler"] = redirect_handler
        oauth_for_connect["_callback_handler"] = callback_handler
        runtime = _oauth_runtime_config(srv, args, env, oauth_for_connect)

        async def run_connect():
            try:
                await mcp_manager.disconnect_server(srv.id)
                connected = await mcp_manager.connect_server(
                    server_id=srv.id,
                    name=srv.name,
                    **runtime,
                )
                status = mcp_manager.get_server_status(srv.id)
                result = {
                    "connected": connected,
                    "tool_count": status.get("tool_count", 0),
                    "error": status.get("error"),
                }
            except BaseException as e:
                logger.exception("MCP OAuth connect failed for %s", srv.id)
                result = {"connected": False, "tool_count": 0, "error": str(e)}
            if not flow["result"].done():
                flow["result"].set_result(result)

        asyncio.create_task(run_connect())

        try:
            auth_url = await asyncio.wait_for(flow["auth_url"], timeout=30)
        except asyncio.TimeoutError:
            error = "The MCP server did not provide an OAuth authorization URL. Check the server URL and transport."
            if flow["result"].done():
                result = flow["result"].result()
                _PENDING_MCP_OAUTH.pop(flow_id, None)
                if result.get("connected"):
                    return HTMLResponse(_oauth_result_page(
                        "Authorization Successful",
                        f"{srv.name} connected with {result.get('tool_count', 0)} tools. You can close this window.",
                        success=True,
                    ))
                error = result.get("error") or error
            _PENDING_MCP_OAUTH.pop(flow_id, None)
            return HTMLResponse(
                _oauth_result_page(
                    "Authorization Failed",
                    error,
                ),
                status_code=504,
            )

        host = request.headers.get("host", "")
        return HTMLResponse(_oauth_authorize_page(auth_url, srv.id, host, provider_name=srv.name))

    @router.get("/oauth/callback")
    async def oauth_callback(code: str, state: str, request: Request):
        """Handle OAuth callback from Google — exchange code for tokens."""
        if state in _MCP_OAUTH_STATE_TO_FLOW:
            return await _complete_mcp_oauth_callback(code, state)
        require_admin(request)
        server_id = state
        return await _exchange_and_connect(server_id, code, request)

    @router.post("/oauth/exchange/{server_id}")
    async def oauth_exchange(server_id: str, request: Request, callback_url: str = Form(...)):
        """Manual code exchange — user pastes the callback URL from their browser."""
        require_admin(request)
        try:
            parsed = urllib.parse.urlparse(callback_url)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]
            if not code:
                return HTMLResponse(_oauth_result_page("Error", "No authorization code found in the URL. Make sure you copied the full URL from your browser."), status_code=400)
        except Exception:
            return HTMLResponse(_oauth_result_page("Error", "Invalid URL format."), status_code=400)

        if state in _MCP_OAUTH_STATE_TO_FLOW:
            return await _complete_mcp_oauth_callback(code, state)
        return await _exchange_and_connect(server_id, code, request)

    async def _complete_mcp_oauth_callback(code: str, state: str):
        flow_id = _MCP_OAUTH_STATE_TO_FLOW.get(state)
        flow = _PENDING_MCP_OAUTH.get(flow_id) if flow_id else None
        if not flow:
            return HTMLResponse(
                _oauth_result_page("Authorization Failed", "OAuth flow expired or was not started from Odysseus."),
                status_code=400,
            )

        if not flow["callback"].done():
            flow["callback"].set_result((code, state))

        try:
            result = await asyncio.wait_for(flow["result"], timeout=30)
        except asyncio.TimeoutError:
            return HTMLResponse(
                _oauth_result_page("Authorization Pending", "Authorization was received, but the MCP server has not finished reconnecting yet. Try Reconnect in Settings."),
                status_code=202,
            )
        finally:
            _PENDING_MCP_OAUTH.pop(flow_id, None)
            _MCP_OAUTH_STATE_TO_FLOW.pop(state, None)

        if result.get("connected"):
            return HTMLResponse(_oauth_result_page(
                "Authorization Successful",
                f"MCP server connected with {result.get('tool_count', 0)} tools. You can close this window.",
                success=True,
            ))

        return HTMLResponse(
            _oauth_result_page(
                "Authorized but Connection Failed",
                f"Tokens were received, but the server failed to connect: {result.get('error') or 'unknown error'}. Try reconnecting from Settings.",
            ),
            status_code=400,
        )

    async def _exchange_and_connect(server_id: str, code: str, request: Request):
        """Exchange auth code for tokens and connect the MCP server."""
        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == server_id).first()
            if not srv:
                return HTMLResponse(_oauth_result_page("Error", "Server not found."), status_code=404)
            if not srv.oauth_config:
                return HTMLResponse(_oauth_result_page("Error", "No OAuth config."), status_code=400)

            oauth_cfg = json.loads(srv.oauth_config)
            keys_file = os.path.expanduser(oauth_cfg.get("keys_file", ""))
            token_file = os.path.expanduser(oauth_cfg.get("token_file", ""))

            with open(keys_file, encoding="utf-8") as f:
                keys_data = json.load(f)
            keys = keys_data.get("installed") or keys_data.get("web")
            client_id = keys["client_id"]
            client_secret = keys["client_secret"]

            redirect_uri = "http://localhost:7000/api/mcp/oauth/callback"

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "code": code,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )

            if resp.status_code != 200:
                err = resp.text
                logger.error(f"OAuth token exchange failed: {err}")
                return HTMLResponse(_oauth_result_page("Authorization Failed", f"Google returned an error: {err}"), status_code=400)

            tokens = resp.json()
            logger.info(f"OAuth tokens received for server {server_id}")

            # Save tokens to the file the MCP package expects
            os.makedirs(os.path.dirname(token_file), exist_ok=True)
            with open(token_file, "w", encoding="utf-8") as f:
                json.dump(tokens, f, indent=2)
            logger.info(f"Saved OAuth tokens to {token_file}")

            # Attempt to connect the MCP server now
            args, env, oauth_config = _server_runtime_config(srv)
            runtime = _oauth_runtime_config(srv, args, env, oauth_config)
            connected = await mcp_manager.connect_server(
                server_id=server_id,
                name=srv.name,
                **runtime,
            )

            if connected:
                status = mcp_manager.get_server_status(server_id)
                tool_count = status.get("tool_count", 0)
                return HTMLResponse(_oauth_result_page(
                    "Authorization Successful",
                    f"{srv.name} connected with {tool_count} tools. You can close this window.",
                    success=True,
                ))
            else:
                status = mcp_manager.get_server_status(server_id)
                return HTMLResponse(_oauth_result_page(
                    "Authorized but Connection Failed",
                    f"Tokens saved, but the server failed to connect: {status.get('error', 'unknown error')}. Try reconnecting from Settings.",
                ))
        except Exception as e:
            logger.exception(f"OAuth callback error: {e}")
            return HTMLResponse(_oauth_result_page("Error", str(e)), status_code=500)
        finally:
            db.close()

    return router


def _oauth_authorize_page(auth_url: str, server_id: str, host: str, provider_name: str = "Provider") -> str:
    """Page with sign-in link and URL paste-back form for remote access."""
    # Escape values interpolated into the page: `host` comes from the request
    # Host header and `server_id` from the OAuth state — neither is trusted.
    auth_url = html.escape(auth_url, quote=True)
    server_id = html.escape(server_id, quote=True)
    host = html.escape(host, quote=True)
    provider_name = html.escape(provider_name, quote=True)
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>Authorize — Odysseus</title>
<style>
  body {{ font-family: 'Fira Code', monospace; background: #0f0f0f; color: #e0e0e0;
    display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
  .card {{ background: #1a1a1a; border: 1px solid #333; border-radius: 12px;
    padding: 2rem; max-width: 480px; text-align: center; }}
  h2 {{ color: #e06c75; margin-bottom: 0.5rem; font-size: 1.1rem; }}
  p {{ color: #aaa; font-size: 0.82rem; line-height: 1.6; margin: 0.8rem 0; }}
  .step {{ text-align: left; color: #ccc; font-size: 0.82rem; line-height: 1.7; margin: 1rem 0; }}
  .step b {{ color: #e06c75; }}
  a.auth-link {{
    display: inline-block; margin: 1rem 0; padding: 0.6rem 1.5rem;
    background: #e06c75; color: #fff; text-decoration: none; border-radius: 6px;
    font-weight: 600; font-size: 0.9rem;
  }}
  a.auth-link:hover {{ background: #c55; }}
  input[type=text] {{
    width: 100%; padding: 0.5rem; margin: 0.5rem 0;
    background: #0f0f0f; border: 1px solid #333; border-radius: 6px;
    color: #e0e0e0; font-family: 'Fira Code', monospace; font-size: 0.8rem;
  }}
  input:focus {{ outline: none; border-color: #e06c75; }}
  button {{
    padding: 0.5rem 1.5rem; border: none; border-radius: 6px;
    background: #e06c75; color: #fff; font-weight: 600; cursor: pointer;
    font-family: 'Fira Code', monospace; font-size: 0.85rem; margin-top: 0.3rem;
  }}
  button:hover {{ background: #c55; }}
  .divider {{ border-top: 1px solid #333; margin: 1.2rem 0; }}
</style></head>
<body><div class="card">
  <h2>Authorize {provider_name}</h2>
  <div class="step">
    <b>1.</b> Click the button below to sign in<br>
    <b>2.</b> After approving, your browser will show an error page — that's normal<br>
    <b>3.</b> Copy the full URL from your browser's address bar<br>
    <b>4.</b> Paste it below and click Connect
  </div>
  <a class="auth-link" href="{auth_url}" target="_blank" rel="noopener">Sign in</a>
  <div class="divider"></div>
  <form method="POST" action="/api/mcp/oauth/exchange/{server_id}">
    <p>Paste the URL from your browser after signing in:</p>
    <input type="text" name="callback_url" placeholder="http://localhost:7860/api/mcp/oauth/callback?code=..." required>
    <br><button type="submit">Connect</button>
  </form>
</div></body></html>"""


def _oauth_result_page(title: str, message: str, success: bool = False) -> str:
    """Generate a simple HTML page for the OAuth result."""
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    color = "#00661a" if success else "#e06c75"
    icon = "&#10003;" if success else "&#10007;"
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>{safe_title}</title>
<style>
  body {{ font-family: 'Fira Code', monospace; background: #0f0f0f; color: #e0e0e0;
    display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
  .card {{ background: #1a1a1a; border: 1px solid #333; border-radius: 12px;
    padding: 2rem; max-width: 420px; text-align: center; }}
  .icon {{ font-size: 3rem; color: {color}; margin-bottom: 1rem; }}
  h2 {{ color: {color}; margin-bottom: 0.5rem; font-size: 1.1rem; }}
  p {{ color: #aaa; font-size: 0.85rem; line-height: 1.5; }}
</style></head>
<body><div class="card">
  <div class="icon">{icon}</div>
  <h2>{safe_title}</h2>
  <p>{safe_message}</p>
</div></body></html>"""
