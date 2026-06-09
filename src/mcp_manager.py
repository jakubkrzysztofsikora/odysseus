"""
mcp_manager.py

Manages connections to MCP (Model Context Protocol) tool servers.
Each server exposes tools that are made available to the agent loop.
"""

import asyncio
import copy
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REMOTE_HTTP_TRANSPORTS = {"http", "streamable-http", "streamable_http"}


def normalize_mcp_transport(transport: str) -> str:
    """Normalize user-facing MCP transport aliases to internal names."""
    value = (transport or "").strip().lower()
    if value in REMOTE_HTTP_TRANSPORTS:
        return "streamable_http"
    return value


def _startup_mcp_remote_args(args: List[str], per_server_timeout: float) -> List[str]:
    """Bound mcp-remote's interactive OAuth wait during app startup.

    Normal manual reconnects keep the configured args untouched. Startup should
    never hang all user MCP discovery behind a browser auth prompt.
    """
    if "--auth-timeout" in args:
        return args
    if not any(arg == "mcp-remote" or arg.startswith("mcp-remote@") for arg in args):
        return args
    auth_timeout = max(1, min(10, int(per_server_timeout) - 2))
    return [*args, "--auth-timeout", str(auth_timeout)]


def _is_mcp_remote_args(args: List[str]) -> bool:
    return any(arg == "mcp-remote" or arg.startswith("mcp-remote@") for arg in args)


def _has_mcp_remote_header(args: List[str]) -> bool:
    return any(arg == "--header" or arg.startswith("--header=") for arg in args)


def _skip_interactive_mcp_remote_startup(args: List[str], env: Dict[str, str]) -> bool:
    """Whether startup should avoid launching an interactive mcp-remote OAuth flow."""
    if not _is_mcp_remote_args(args) or _has_mcp_remote_header(args):
        return False
    override = str((env or {}).get("ODYSSEUS_MCP_AUTOSTART_INTERACTIVE", "")).lower()
    return override not in {"1", "true", "yes", "on"}


def _format_mcp_connection_error(name: str, command: str = "", args: Optional[List[str]] = None, error: Exception = None) -> str:
    """Return a user-actionable MCP connection error message."""
    args = args or []
    raw_error = str(error) if error else "Unknown error"
    command_line = " ".join([command or "", *args]).strip()
    lower_command = command_line.lower()

    if "@playwright/mcp" in lower_command:
        return (
            f"{raw_error}\n\n"
            "Browser MCP could not start. On fresh installs, cache the Playwright MCP package once before connecting:\n\n"
            "npx -y @playwright/mcp@latest --version\n\n"
            "Then restart Odysseus and reconnect the Browser MCP server."
        )

    return raw_error


# Caps for rendering untrusted MCP tool schemas into the agent prompt.
_MCP_PARAM_MAX = 12
_MCP_TOKEN_MAX = 40
_MCP_HINT_MAX = 300


def _sanitize_schema_token(value: Any, limit: int = _MCP_TOKEN_MAX) -> str:
    """Make an untrusted JSON-Schema token safe to splice into the prompt."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def _format_mcp_params(input_schema: Any) -> str:
    """Render an MCP tool's JSON-Schema inputs as a compact prompt hint."""
    if not isinstance(input_schema, dict):
        return ""
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(input_schema.get("required") or [])
    parts = []
    for pname, pinfo in list(props.items())[:_MCP_PARAM_MAX]:
        pinfo = pinfo if isinstance(pinfo, dict) else {}
        ptype = pinfo.get("type") or "any"
        if isinstance(ptype, list):
            ptype = "|".join(str(x) for x in ptype)
        tag = f'"{_sanitize_schema_token(pname)}": {_sanitize_schema_token(ptype)}'
        if pname in required:
            tag += " (required)"
        parts.append(tag)
    extra = len(props) - len(parts)
    if extra > 0:
        parts.append(f"...+{extra} more")
    hint = " Args (JSON): {" + ", ".join(parts) + "}"
    if len(hint) > _MCP_HINT_MAX:
        hint = hint[:_MCP_HINT_MAX - 3].rstrip() + "..."
    return hint


def _mcp_prompt_alias(value: Any) -> str:
    """Normalize a server name/id into a fence-tag alias the parser accepts."""
    return re.sub(r"[^a-z0-9_.-]+", "", str(value or "").lower())


def _exception_text(error: BaseException) -> str:
    message = str(error).strip()
    if message:
        return f"{type(error).__name__}: {message}"
    return f"{type(error).__name__}: {error!r}"


def _normalize_schema_for_openai(schema: Any) -> Dict[str, Any]:
    """Return a conservative OpenAI-tool-safe copy of an MCP input schema."""
    if not isinstance(schema, dict):
        schema = {}
    out = copy.deepcopy(schema)
    if not isinstance(out, dict):
        out = {}

    if "properties" in out and "type" not in out:
        out["type"] = "object"

    schema_type = out.get("type")
    type_values = set(schema_type if isinstance(schema_type, list) else [schema_type])
    is_object = "object" in type_values or "properties" in out

    if is_object:
        props = out.get("properties")
        if not isinstance(props, dict):
            props = {}
        out["properties"] = {
            str(name): _normalize_schema_for_openai(prop)
            for name, prop in props.items()
        }
        required = out.get("required")
        if not isinstance(required, list):
            required = []
        out["required"] = [
            str(name)
            for name in required
            if str(name) in out["properties"]
        ]
        out["additionalProperties"] = False

    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _normalize_schema_for_openai(items)
    elif isinstance(items, list):
        out["items"] = [
            _normalize_schema_for_openai(item) if isinstance(item, dict) else item
            for item in items
        ]

    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(out.get(key), list):
            out[key] = [
                _normalize_schema_for_openai(item) if isinstance(item, dict) else item
                for item in out[key]
            ]

    if not out:
        out = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    return out


def _schema_is_strict_compatible(schema: Any) -> bool:
    """Whether a schema can safely be sent with OpenAI `strict: true`."""
    if not isinstance(schema, dict):
        return True
    schema_type = schema.get("type")
    type_values = set(schema_type if isinstance(schema_type, list) else [schema_type])
    is_object = "object" in type_values or "properties" in schema
    if is_object:
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        if set(required) != set(props):
            return False
        if schema.get("additionalProperties") is not False:
            return False
        if not all(_schema_is_strict_compatible(prop) for prop in props.values()):
            return False
    items = schema.get("items")
    if isinstance(items, dict) and not _schema_is_strict_compatible(items):
        return False
    if isinstance(items, list) and not all(
        _schema_is_strict_compatible(item) for item in items if isinstance(item, dict)
    ):
        return False
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and not all(
            _schema_is_strict_compatible(item) for item in variants if isinstance(item, dict)
        ):
            return False
    return True


def _blank_required_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _example_for_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return "..."
    schema_type = schema.get("type")
    type_values = schema_type if isinstance(schema_type, list) else [schema_type]
    type_values = [t for t in type_values if t != "null"]
    primary = type_values[0] if type_values else None
    if primary == "array":
        return []
    if primary == "object":
        return {}
    if primary == "integer":
        return 0
    if primary == "number":
        return 0
    if primary == "boolean":
        return False
    return "..."


def _oauth_token_expired(token_file: str, skew_seconds: int = 60) -> bool:
    """Best-effort expiry check for MCP OAuth token files.

    The MCP SDK writes OAuthToken JSON that may include expires_in without an
    absolute issued/expires timestamp. In that case use the token file mtime as
    issued_at so app startup does not try an obviously stale token and trigger
    an interactive OAuth flow with no redirect handler.
    """
    if not token_file or not os.path.exists(token_file):
        return False
    try:
        with open(token_file) as f:
            data = json.load(f)
    except Exception:
        return False

    now = time.time()
    expires_at = data.get("expires_at")
    if isinstance(expires_at, (int, float)):
        return expires_at <= now + skew_seconds

    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        issued_at = data.get("issued_at")
        if not isinstance(issued_at, (int, float)):
            issued_at = os.path.getmtime(token_file)
        return issued_at + expires_in <= now + skew_seconds

    return False


class FileTokenStorage:
    """File-backed token storage implementing the MCP SDK TokenStorage protocol."""

    def __init__(self, token_file: str):
        self._token_file = token_file
        self._client_info_file = token_file.replace(".json", "_client.json")

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        if os.path.exists(self._token_file):
            with open(self._token_file) as f:
                return OAuthToken.model_validate(json.load(f))
        return None

    async def set_tokens(self, tokens) -> None:
        os.makedirs(os.path.dirname(self._token_file), exist_ok=True)
        data = tokens.model_dump(mode="json")
        if data.get("expires_in") and not data.get("expires_at"):
            data["issued_at"] = int(time.time())
            data["expires_at"] = data["issued_at"] + int(data["expires_in"])
        with open(self._token_file, "w") as f:
            json.dump(data, f, indent=2)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        if os.path.exists(self._client_info_file):
            with open(self._client_info_file) as f:
                return OAuthClientInformationFull.model_validate(json.load(f))
        return None

    async def set_client_info(self, client_info) -> None:
        os.makedirs(os.path.dirname(self._client_info_file), exist_ok=True)
        with open(self._client_info_file, "w") as f:
            json.dump(client_info.model_dump(mode="json"), f, indent=2)


class McpManager:
    """Manages MCP server connections and tool routing."""

    def __init__(self):
        # server_id -> connection state
        self._connections: Dict[str, Dict[str, Any]] = {}
        # server_id -> list of tool schemas
        self._tools: Dict[str, List[Dict]] = {}
        # server_id -> MCP ClientSession
        self._sessions: Dict[str, Any] = {}
        # server_id -> exit stack (for cleanup)
        self._stacks: Dict[str, Any] = {}
        # server_id -> background connect task (HTTP transport / OAuth)
        self._connect_tasks: Dict[str, Any] = {}
        # Tracking updates to tools/connections for RAG indexing / prompt cache
        self._generation = 0

    async def connect_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
        oauth_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Connect to an MCP server via stdio, SSE, or streamable_http transport."""
        try:
            transport = normalize_mcp_transport(transport)
            if transport == "stdio":
                res = await self._connect_stdio(server_id, name, command, args or [], env or {})
            elif transport == "sse":
                res = await self._connect_sse(server_id, name, url, oauth_config)
            elif transport == "streamable_http":
                res = await self._connect_streamable_http(server_id, name, url, oauth_config)
            else:
                logger.error(f"Unknown MCP transport: {transport}")
                res = False
            if res:
                self._generation += 1
            return res
        except Exception as e:
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {e}")
            error_message = _format_mcp_connection_error(name, command or "", args or [], e)
            self._connections[server_id] = {"status": "error", "error": error_message, "name": name}
            self._generation += 1
            return False

    async def _connect_stdio(self, server_id: str, name: str, command: str, args: List[str], env: Dict[str, str]) -> bool:
        """Connect to an MCP server via stdio transport."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from contextlib import AsyncExitStack

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env={**os.environ, **env} if env else None,
            )

            stack = AsyncExitStack()
            try:
                transport = await stack.enter_async_context(stdio_client(server_params))
                read_stream, write_stream = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

                await session.initialize()

                # Discover tools
                tools_result = await session.list_tools()
            except Exception:
                await stack.aclose()
                raise
            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                })

            self._sessions[server_id] = session
            self._stacks[server_id] = stack
            self._tools[server_id] = tools
            # Extract identity hints from env vars (e.g. email address, API name)
            # so tool descriptions can distinguish between multiple instances of
            # the same MCP server (e.g. two email accounts).
            identity_hints = []
            for k, v in (env or {}).items():
                k_lower = k.lower()
                if any(x in k_lower for x in ['email_address', 'account', 'user', 'username']):
                    identity_hints.append(v)
            identity = ", ".join(identity_hints) if identity_hints else ""

            self._connections[server_id] = {
                "status": "connected",
                "name": name,
                "transport": "stdio",
                "tool_count": len(tools),
                "identity": identity,
            }

            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via stdio")
            return True

        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

    def _build_oauth_auth(self, url: str, oauth_config: dict):
        """Build OAuthClientProvider from oauth_config dict, or return None."""
        from pydantic import AnyUrl
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        token_file = os.path.expanduser(oauth_config.get("token_file", ""))
        redirect_uris_raw = oauth_config.get("redirect_uris") or [
            "http://localhost:7000/api/mcp/oauth/callback"
        ]
        redirect_uris = [
            u if isinstance(u, AnyUrl) else AnyUrl(u) for u in redirect_uris_raw
        ]
        scope = oauth_config.get("scope") or oauth_config.get("scopes")
        if isinstance(scope, list):
            scope = " ".join(scope)

        storage = FileTokenStorage(token_file)
        client_metadata = OAuthClientMetadata(
            client_name=oauth_config.get("client_name", "Odysseus"),
            redirect_uris=redirect_uris,
            grant_types=oauth_config.get("grant_types", ["authorization_code", "refresh_token"]),
            response_types=oauth_config.get("response_types", ["code"]),
            scope=scope,
        )
        return OAuthClientProvider(
            server_url=url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=oauth_config.get("_redirect_handler"),
            callback_handler=oauth_config.get("_callback_handler"),
            client_metadata_url=oauth_config.get("client_metadata_url"),
        )

    async def _connect_sse(self, server_id: str, name: str, url: str, oauth_config: dict = None) -> bool:
        """Connect to an MCP server via SSE transport."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from contextlib import AsyncExitStack

            auth = self._build_oauth_auth(url, oauth_config) if oauth_config else None

            stack = AsyncExitStack()
            try:
                transport = await stack.enter_async_context(sse_client(url, auth=auth))
                read_stream, write_stream = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

                await session.initialize()

                # Discover tools
                tools_result = await session.list_tools()
            except Exception:
                await stack.aclose()
                raise
            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                })

            self._sessions[server_id] = session
            self._stacks[server_id] = stack
            self._tools[server_id] = tools
            self._connections[server_id] = {
                "status": "connected",
                "name": name,
                "transport": "sse",
                "tool_count": len(tools),
            }

            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via SSE")
            return True

        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

    async def _connect_streamable_http(self, server_id: str, name: str, url: str, oauth_config: dict = None) -> bool:
        """Connect to an MCP server via Streamable HTTP transport."""
        try:
            from contextlib import AsyncExitStack
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            auth = self._build_oauth_auth(url, oauth_config) if oauth_config else None

            stack = AsyncExitStack()
            try:
                transport = await stack.enter_async_context(streamablehttp_client(url, auth=auth))
                read_stream, write_stream, get_session_id = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

                await session.initialize()
                tools_result = await session.list_tools()
            except Exception:
                await stack.aclose()
                raise

            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                })

            self._sessions[server_id] = session
            self._stacks[server_id] = stack
            self._tools[server_id] = tools
            self._connections[server_id] = {
                "status": "connected",
                "name": name,
                "transport": "streamable_http",
                "tool_count": len(tools),
                "session_id": get_session_id() if callable(get_session_id) else None,
            }

            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via Streamable HTTP")
            return True

        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

    async def disconnect_server(self, server_id: str):
        """Disconnect from an MCP server."""
        # Cancel any in-flight HTTP/OAuth background connect so it stops
        # publishing status for a server that may be getting deleted.
        task = self._connect_tasks.pop(server_id, None)
        if task is not None and not task.done():
            task.cancel()
        try:
            from src.mcp_oauth import clear_auth_url
            clear_auth_url(server_id)
        except Exception:
            pass

        stack = self._stacks.pop(server_id, None)
        if stack:
            try:
                await stack.aclose()
            except Exception as e:
                logger.warning(f"Error closing MCP server {server_id}: {e}")

        self._sessions.pop(server_id, None)
        self._tools.pop(server_id, None)
        self._connections.pop(server_id, None)
        self._generation += 1
        logger.info(f"MCP server disconnected: {server_id}")

    async def disconnect_all(self):
        """Disconnect from all MCP servers."""
        ids = list(self._sessions.keys())
        for sid in ids:
            await self.disconnect_server(sid)

    async def connect_all_enabled(self, per_server_timeout: float = 12.0):
        """Connect to all enabled MCP servers from the database."""
        from src.database import McpServer, SessionLocal

        db = SessionLocal()
        try:
            servers = db.query(McpServer).filter(McpServer.is_enabled == True).all()
            configs = []
            for srv in servers:
                env = json.loads(srv.env) if srv.env else {}
                args = json.loads(srv.args) if srv.args else []
                if normalize_mcp_transport(srv.transport) == "stdio":
                    if _skip_interactive_mcp_remote_startup(args, env):
                        self._connections[srv.id] = {
                            "status": "needs_oauth",
                            "name": srv.name,
                            "error": "Interactive OAuth required; reconnect from MCP settings",
                        }
                        self._generation += 1
                        continue
                    args = _startup_mcp_remote_args(args, per_server_timeout)
                oauth_config = json.loads(srv.oauth_config) if srv.oauth_config else None
                token_file = os.path.expanduser(oauth_config.get("token_file", "")) if oauth_config else ""
                if token_file and not os.path.exists(token_file):
                    self._connections[srv.id] = {
                        "status": "needs_oauth",
                        "name": srv.name,
                        "error": "OAuth authorization required",
                    }
                    continue
                if token_file and _oauth_token_expired(token_file):
                    self._connections[srv.id] = {
                        "status": "needs_oauth",
                        "name": srv.name,
                        "error": "OAuth token expired; reconnect from MCP settings",
                    }
                    self._generation += 1
                    continue
                configs.append({
                    "server_id": srv.id,
                    "name": srv.name,
                    "transport": srv.transport,
                    "command": srv.command,
                    "args": args,
                    "env": env,
                    "url": srv.url,
                    "oauth_config": oauth_config,
                })
        finally:
            db.close()

        if configs:
            for config in configs:
                self._connections[config["server_id"]] = {
                    "status": "connecting",
                    "name": config["name"],
                }
            self._generation += 1

        async def _connect_one(config: Dict[str, Any]) -> bool:
            try:
                ok = await asyncio.wait_for(
                    self.connect_server(**config),
                    timeout=per_server_timeout,
                )
                if not ok:
                    current = self._connections.get(config["server_id"], {})
                    if current.get("status") == "connecting":
                        self._connections[config["server_id"]] = {
                            "status": "error",
                            "error": "Connection failed",
                            "name": config["name"],
                        }
                        self._generation += 1
                return ok
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP startup timed out for %s (%s) after %.1fs",
                    config["name"],
                    config["server_id"],
                    per_server_timeout,
                )
                self._connections[config["server_id"]] = {
                    "status": "error",
                    "error": f"Connection timed out after {per_server_timeout:.1f}s",
                    "name": config["name"],
                }
                self._generation += 1
                return False
            except Exception as e:
                logger.error(
                    "MCP startup failed for %s (%s): %s",
                    config["name"],
                    config["server_id"],
                    e,
                )
                self._connections[config["server_id"]] = {
                    "status": "error",
                    "error": str(e),
                    "name": config["name"],
                }
                self._generation += 1
                return False
            except BaseException as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                err_text = _exception_text(e)
                status = "needs_oauth" if config.get("oauth_config") else "error"
                error = (
                    "OAuth authorization required; reconnect from MCP settings"
                    if config.get("oauth_config")
                    else err_text
                )
                logger.warning(
                    "MCP startup aborted for %s (%s): %s",
                    config["name"],
                    config["server_id"],
                    err_text,
                )
                self._connections[config["server_id"]] = {
                    "status": status,
                    "error": error,
                    "name": config["name"],
                }
                self._generation += 1
                return False

        if not configs:
            return []
        return await asyncio.gather(*(_connect_one(config) for config in configs))

    async def call_tool(self, qualified_name: str, arguments: Dict) -> Dict:
        """Call an MCP tool by its qualified name (mcp__{server_id}__{tool_name}).

        Returns a result dict compatible with agent_tools format.
        """
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return {"error": f"Invalid MCP tool name: {qualified_name}", "exit_code": 1}

        server_id = parts[1]
        tool_name = parts[2]

        session = self._sessions.get(server_id)
        if not session:
            return {"error": f"MCP server not connected: {server_id}", "exit_code": 1}

        arguments = self.coerce_tool_arguments(qualified_name, arguments)
        missing = self.missing_required_arguments(qualified_name, arguments)
        if missing:
            example = self.required_argument_example(qualified_name, missing)
            logger.warning(
                "MCP tool called with missing required arguments: %s missing=%s args=%r",
                qualified_name,
                missing,
                arguments,
            )
            return {
                "error": (
                    f"Tool '{qualified_name}' was called with empty or missing required "
                    f"argument(s): {', '.join(missing)}. Retry with a JSON object like "
                    f"{json.dumps(example)}. Do not say you are blocked until a correctly "
                    "populated tool call has also failed."
                ),
                "exit_code": 2,
                "empty_arguments": not bool(arguments),
                "missing_required": missing,
            }

        try:
            result = await self._do_call(session, tool_name, arguments)
        except Exception as e:
            err_text = _exception_text(e)
            # Auto-reconnect for builtin servers whose subprocess may have died
            if self.is_builtin(server_id):
                logger.warning("MCP call failed for %s, attempting reconnect: %s", qualified_name, err_text)
                reconnected = await self._reconnect_builtin(server_id)
                if reconnected:
                    session = self._sessions.get(server_id)
                    if session:
                        try:
                            result = await self._do_call(session, tool_name, arguments)
                        except Exception as e2:
                            err2_text = _exception_text(e2)
                            logger.error("MCP tool call failed after reconnect: %s: %s", qualified_name, err2_text)
                            return {"error": err2_text, "exit_code": 1}
                    else:
                        return {"error": f"Reconnected but no session for {server_id}", "exit_code": 1}
                else:
                    logger.error(f"MCP reconnect failed for {server_id}")
                    return {"error": f"MCP server crashed and reconnect failed: {server_id}", "exit_code": 1}
            else:
                logger.warning("MCP call failed for %s, attempting reconnect: %s", qualified_name, err_text)
                reconnected = await self._reconnect_configured_server(server_id)
                if reconnected:
                    session = self._sessions.get(server_id)
                    if session:
                        try:
                            result = await self._do_call(session, tool_name, arguments)
                        except Exception as e2:
                            err2_text = _exception_text(e2)
                            logger.error("MCP tool call failed after reconnect: %s: %s", qualified_name, err2_text)
                            return {"error": err2_text, "exit_code": 1}
                    else:
                        return {"error": f"Reconnected but no session for {server_id}", "exit_code": 1}
                else:
                    logger.error("MCP reconnect failed for %s after call error: %s", server_id, err_text)
                    return {"error": f"{err_text}; reconnect failed for MCP server {server_id}", "exit_code": 1}

        return result

    def _input_schema_for_tool(self, server_id: str, tool_name: str) -> Dict[str, Any]:
        for tool in self._tools.get(server_id, []):
            if tool.get("name") == tool_name:
                schema = tool.get("input_schema") or {}
                return schema if isinstance(schema, dict) else {}
        return {}

    def _split_qualified_tool_name(self, qualified_name: str) -> tuple[Optional[str], Optional[str]]:
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return None, None
        return parts[1], parts[2]

    def coerce_tool_arguments(
        self,
        qualified_name: str,
        arguments: Any,
        *,
        scalar_arg: Any = None,
    ) -> Dict[str, Any]:
        """Coerce model-emitted MCP args without dropping useful scalar values."""
        args = arguments if isinstance(arguments, dict) else {}
        server_id, tool_name = self._split_qualified_tool_name(qualified_name)
        if not server_id or not tool_name:
            return args
        schema = self._input_schema_for_tool(server_id, tool_name)
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        if scalar_arg is None:
            if isinstance(arguments, str):
                scalar_arg = arguments
            elif isinstance(arguments, list) and arguments:
                scalar_arg = arguments[0]
            elif isinstance(arguments, dict) and len(arguments) == 1:
                key, value = next(iter(arguments.items()))
                if (props or required) and str(key) not in props:
                    if isinstance(value, dict) and any(str(name) in value for name in required):
                        return value
                    scalar_arg = value
                    args = {}
        if args or scalar_arg is None:
            return args
        if len(required) == 1:
            return {str(required[0]): scalar_arg}
        if len(props) == 1:
            return {str(next(iter(props))): scalar_arg}
        return args

    def missing_required_arguments(self, qualified_name: str, arguments: Any) -> List[str]:
        server_id, tool_name = self._split_qualified_tool_name(qualified_name)
        if not server_id or not tool_name:
            return []
        schema = self._input_schema_for_tool(server_id, tool_name)
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        if not required:
            return []
        args = arguments if isinstance(arguments, dict) else {}
        return [
            str(name)
            for name in required
            if str(name) not in args or _blank_required_value(args.get(str(name)))
        ]

    def required_argument_example(self, qualified_name: str, names: Optional[List[str]] = None) -> Dict[str, Any]:
        server_id, tool_name = self._split_qualified_tool_name(qualified_name)
        if not server_id or not tool_name:
            return {}
        schema = self._input_schema_for_tool(server_id, tool_name)
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = names or schema.get("required") or []
        return {
            str(name): _example_for_schema(props.get(str(name), {}))
            for name in required
        }

    async def _do_call(self, session, tool_name: str, arguments: Dict) -> Dict:
        """Execute a single MCP tool call and return result dict."""
        result = await session.call_tool(tool_name, arguments)
        output_parts = []
        images = []
        for content in result.content:
            if hasattr(content, 'text'):
                output_parts.append(content.text)
            elif getattr(content, 'type', '') == 'image' and hasattr(content, 'data'):
                # Image content (e.g. Playwright screenshots)
                mime = getattr(content, 'mimeType', 'image/png')
                images.append({"data": content.data, "mimeType": mime})
                output_parts.append(f"[Screenshot captured ({mime})]")
            elif hasattr(content, 'data'):
                output_parts.append(str(content.data))

        output = "\n".join(output_parts)
        is_error = getattr(result, 'isError', False)
        if is_error and not output.strip():
            output = "MCP tool returned an error with no content."

        result_dict = {
            "stdout": output if not is_error else "",
            "stderr": output if is_error else "",
            "exit_code": 1 if is_error else 0,
        }
        if images:
            result_dict["images"] = images
        return result_dict

    async def _reconnect_builtin(self, server_id: str) -> bool:
        """Tear down and reconnect a crashed builtin MCP server."""
        import sys
        from src.builtin_mcp import _BUILTIN_SERVERS

        if server_id not in _BUILTIN_SERVERS:
            return False

        script_rel, name = _BUILTIN_SERVERS[server_id]
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script_path = os.path.join(base_dir, script_rel)

        # Clean up old connection
        await self.disconnect_server(server_id)

        try:
            ok = await self.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=sys.executable,
                args=[script_path],
                env={"PYTHONPATH": base_dir},
            )
            if ok:
                logger.info(f"Reconnected builtin MCP server: {name}")
            return ok
        except Exception as e:
            logger.error(f"Failed to reconnect builtin MCP server {name}: {e}")
            return False

    async def _reconnect_configured_server(self, server_id: str) -> bool:
        """Reconnect a database-configured MCP server after a call-time failure."""
        from src.database import McpServer, SessionLocal

        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == server_id).first()
            if not srv or not srv.is_enabled:
                self._connections[server_id] = {
                    "status": "error",
                    "error": "MCP server is not configured or disabled",
                    "name": server_id,
                }
                self._generation += 1
                return False

            args = json.loads(srv.args) if srv.args else []
            env = json.loads(srv.env) if srv.env else {}
            oauth_config = json.loads(srv.oauth_config) if srv.oauth_config else None
            token_file = os.path.expanduser(oauth_config.get("token_file", "")) if oauth_config else ""
            if token_file and not os.path.exists(token_file):
                self._connections[server_id] = {
                    "status": "needs_oauth",
                    "name": srv.name,
                    "error": "OAuth authorization required",
                }
                self._generation += 1
                return False
            if token_file and _oauth_token_expired(token_file):
                self._connections[server_id] = {
                    "status": "needs_oauth",
                    "name": srv.name,
                    "error": "OAuth token expired; reconnect from MCP settings",
                }
                self._generation += 1
                return False

            await self.disconnect_server(server_id)
            return await self.connect_server(
                server_id=srv.id,
                name=srv.name,
                transport=srv.transport,
                command=srv.command,
                args=args,
                env=env,
                url=srv.url,
                oauth_config=oauth_config,
            )
        finally:
            db.close()

    def get_all_openai_schemas(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return all MCP tools in OpenAI function-calling format.

        Tool names are namespaced as mcp__{server_id}__{tool_name}.
        disabled_map: optional {server_id: set_of_disabled_tool_names} to filter out.
        """
        schemas = []
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            if conn.get("status") != "connected":
                continue
            # Skip builtin Python servers — they use the code-block tool format
            # But include NPX-based builtins (like browser) which need function calling
            if self.is_builtin(server_id) and server_id != "builtin_browser":
                continue
            server_name = conn.get("name", server_id)
            disabled = (disabled_map or {}).get(server_id, set())

            identity = conn.get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name

            for tool in tools:
                if tool["name"] in disabled:
                    continue
                qualified = f"mcp__{server_id}__{tool['name']}"
                parameters = _normalize_schema_for_openai(
                    tool.get("input_schema", {"type": "object", "properties": {}})
                )
                required = parameters.get("required") if isinstance(parameters.get("required"), list) else []
                description = f"[MCP:{label}] {tool['description']}"
                if required:
                    description = (
                        f"{description}\n\nRequired JSON argument(s): {', '.join(required)}. "
                        "Never call this tool with empty arguments."
                    )
                function_schema = {
                    "name": qualified,
                    "description": description,
                    "parameters": parameters,
                }
                if required and _schema_is_strict_compatible(parameters):
                    function_schema["strict"] = True
                schema = {
                    "type": "function",
                    "function": function_schema,
                }
                schemas.append(schema)

        return schemas

    def get_all_tools(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return a flat list of all discovered tools with server info."""
        result = []
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            if conn.get("status") != "connected":
                continue
            disabled = (disabled_map or {}).get(server_id, set())
            for tool in tools:
                result.append({
                    "server_id": server_id,
                    "server_name": conn.get("name", server_id),
                    "name": tool["name"],
                    "qualified_name": f"mcp__{server_id}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or {},
                    "is_disabled": tool["name"] in disabled,
                })
        return result

    def is_builtin(self, server_id: str) -> bool:
        """Check if a server is a built-in (auto-registered) server."""
        return server_id.startswith("builtin_") or server_id in {
            "image_gen",
            "memory",
            "rag",
            "email",
        }

    def get_server_status(self, server_id: str) -> Dict:
        """Get connection status for a server."""
        return self._connections.get(server_id, {"status": "disconnected"})

    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get connection statuses for all servers."""
        return dict(self._connections)

    _cached_prompt_desc = None
    _cached_prompt_desc_key = None

    def get_tool_descriptions_for_prompt(self, disabled_map: Optional[Dict[str, set]] = None) -> str:
        """Generate text describing MCP tools for the agent system prompt. Cached."""
        cache_key = (
            frozenset((k, frozenset(v)) for k, v in (disabled_map or {}).items()),
            len(self._tools),
            self._generation,
        )
        if self._cached_prompt_desc is not None and self._cached_prompt_desc_key == cache_key:
            return self._cached_prompt_desc
        tools = self.get_all_tools(disabled_map)
        if not tools:
            return ""

        lines = [
            "\n\nYou also have access to external MCP tool servers.",
            "The listed MCP tools are available in this Odysseus session; do not claim one is unavailable when it is listed below.",
            "When native function schemas are available, call MCP tools through those schemas.",
            "When you are using text tools, call an MCP tool by writing one raw function-call line exactly like:",
            'mcp__server_id__tool_name{"required_arg":"real non-empty value"}',
            "For a search server, a server-alias fenced block is also accepted, for example:",
            '```atlassian\n{"query":"real non-empty search text"}\n```',
            "Never call MCP tools with `{}` or blank required arguments.",
        ]
        by_server = {}
        for t in tools:
            # Skip builtin Python servers — they're already in the agent prompt
            # But include NPX-based builtins (like browser) which aren't hardcoded
            if self.is_builtin(t["server_id"]) and t["server_id"] != "builtin_browser":
                continue
            if t.get("is_disabled"):
                continue
            sn = t["server_name"]
            if sn not in by_server:
                by_server[sn] = []
            by_server[sn].append(t)

        if not by_server:
            return ""

        for server_name, server_tools in by_server.items():
            # Include identity (e.g. email address) if available
            sid = server_tools[0]["server_id"] if server_tools else ""
            identity = self._connections.get(sid, {}).get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name
            lines.append(f"\n**{label}:**")
            aliases = sorted({
                alias
                for alias in (
                    _mcp_prompt_alias(sid),
                    _mcp_prompt_alias(server_name),
                )
                if alias
            })
            if aliases:
                lines.append(f"  Server aliases for text-tool fallback: {', '.join(aliases)}")
            for t in server_tools:
                # Truncate long descriptions
                desc = t['description'][:120] + '...' if len(t['description']) > 120 else t['description']
                # Include the tool's declared inputs so the model calls it with
                # real argument names instead of guessing from the description
                # alone (issue #2509).
                args_hint = _format_mcp_params(t.get("input_schema"))
                lines.append(f"  - {t['qualified_name']}: {desc}{args_hint}")

        result = "\n".join(lines)
        self._cached_prompt_desc = result
        self._cached_prompt_desc_key = cache_key
        return result
