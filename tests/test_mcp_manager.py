import asyncio
import httpx
import json
import os
import time
import sys
from types import SimpleNamespace

from src.mcp_manager import (
    FileTokenStorage,
    McpManager,
    _format_mcp_connection_error,
    _oauth_access_token_expired,
    _oauth_authorization_required_error,
    _oauth_token_expired,
    _skip_interactive_mcp_remote_startup,
    _startup_mcp_remote_args,
    normalize_mcp_transport,
)


def test_playwright_mcp_connection_error_includes_install_hint():
    msg = _format_mcp_connection_error(
        "Browser (Playwright)",
        "npx",
        ["-y", "@playwright/mcp@latest", "--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "Browser MCP could not start" in msg
    assert "npx -y @playwright/mcp@latest --version" in msg
    assert "restart Odysseus" in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


def test_remote_http_transport_aliases_normalize_to_streamable_http():
    assert normalize_mcp_transport("http") == "streamable_http"
    assert normalize_mcp_transport("streamable-http") == "streamable_http"
    assert normalize_mcp_transport("streamable_http") == "streamable_http"
    assert normalize_mcp_transport("sse") == "sse"


def test_streamable_http_connector_is_implemented():
    assert hasattr(McpManager, "_connect_streamable_http")


def test_startup_mcp_remote_args_adds_auth_timeout():
    args = ["-y", "mcp-remote", "https://example.test/mcp"]

    assert _startup_mcp_remote_args(args, per_server_timeout=12) == [
        "-y",
        "mcp-remote",
        "https://example.test/mcp",
        "--auth-timeout",
        "10",
    ]


def test_startup_mcp_remote_args_preserves_existing_auth_timeout():
    args = ["mcp-remote", "https://example.test/mcp", "--auth-timeout", "3"]

    assert _startup_mcp_remote_args(args, per_server_timeout=12) is args


def test_interactive_mcp_remote_is_skipped_at_startup():
    assert _skip_interactive_mcp_remote_startup(
        ["-y", "mcp-remote", "https://example.test/mcp"],
        {},
    ) is True


def test_header_mcp_remote_can_autostart():
    assert _skip_interactive_mcp_remote_startup(
        ["mcp-remote", "https://example.test/mcp", "--header", "Authorization:${TOKEN}"],
        {},
    ) is False


def test_interactive_mcp_remote_can_opt_into_autostart():
    assert _skip_interactive_mcp_remote_startup(
        ["mcp-remote", "https://example.test/mcp"],
        {"ODYSSEUS_MCP_AUTOSTART_INTERACTIVE": "true"},
    ) is False


def test_oauth_token_expired_uses_expires_at(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"expires_at": time.time() - 10}))

    assert _oauth_token_expired(str(token_file)) is True


def test_oauth_token_expired_uses_file_mtime_with_expires_in(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"expires_in": 3600}))
    old = time.time() - 7200
    os.utime(token_file, (old, old))

    assert _oauth_token_expired(str(token_file)) is True


def test_oauth_token_expired_false_for_fresh_expires_in(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"expires_in": 3600}))

    assert _oauth_token_expired(str(token_file)) is False


def test_oauth_token_expired_false_for_refreshable_expired_token(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({
        "access_token": "stale",
        "refresh_token": "refreshable",
        "expires_at": time.time() - 10,
    }))

    assert _oauth_token_expired(str(token_file)) is False


def test_oauth_access_token_expired_tracks_refreshable_stale_token(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({
        "access_token": "stale",
        "refresh_token": "refreshable",
        "expires_at": time.time() - 10,
    }))

    assert _oauth_access_token_expired(str(token_file)) is True
    assert FileTokenStorage(str(token_file)).access_token_expired() is True


def test_oauth_authorization_required_error_detects_sdk_headless_failure():
    assert _oauth_authorization_required_error(
        RuntimeError("No redirect handler provided for authorization code grant")
    )
    assert _oauth_authorization_required_error(
        RuntimeError("Client error '401 Unauthorized' for url 'https://example.test/mcp'")
    )


def test_headless_oauth_provider_used_without_callback_handlers(tmp_path):
    token_file = tmp_path / "token.json"
    mgr = McpManager()

    auth = mgr._build_oauth_auth(
        "https://example.test/mcp",
        {"token_file": str(token_file)},
    )

    assert auth.__class__.__name__ == "HeadlessOAuthClientProvider"


def test_interactive_oauth_provider_keeps_callback_handlers(tmp_path):
    token_file = tmp_path / "token.json"
    mgr = McpManager()

    async def redirect_handler(_url):
        pass

    async def callback_handler():
        return "code", "state"

    auth = mgr._build_oauth_auth(
        "https://example.test/mcp",
        {
            "token_file": str(token_file),
            "_redirect_handler": redirect_handler,
            "_callback_handler": callback_handler,
        },
    )

    assert auth.__class__.__name__ == "OAuthClientProvider"


def test_headless_oauth_provider_refreshes_expired_file_token_before_request(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({
        "access_token": "stale",
        "refresh_token": "refreshable",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": time.time() - 10,
    }))
    client_file = tmp_path / "token_client.json"
    client_file.write_text(json.dumps({
        "redirect_uris": ["http://localhost:7860/api/mcp/oauth/callback"],
        "client_id": "client-123",
        "token_endpoint_auth_method": "none",
    }))

    auth = McpManager()._build_oauth_auth(
        "https://example.test/mcp",
        {"token_file": str(token_file)},
    )

    async def run_flow():
        request = httpx.Request("POST", "https://example.test/mcp")
        flow = auth.async_auth_flow(request)

        prm_request = await anext(flow)
        assert "oauth-protected-resource" in str(prm_request.url)
        asm_request = await flow.asend(httpx.Response(
            200,
            json={
                "resource": "https://example.test",
                "authorization_servers": ["https://auth.example.test"],
            },
            request=prm_request,
        ))
        assert str(asm_request.url).startswith("https://auth.example.test/")

        refresh_request = await flow.asend(httpx.Response(
            200,
            json={
                "issuer": "https://auth.example.test",
                "authorization_endpoint": "https://auth.example.test/authorize",
                "token_endpoint": "https://auth.example.test/token",
            },
            request=asm_request,
        ))
        assert refresh_request.url == "https://auth.example.test/token"
        assert b"grant_type=refresh_token" in refresh_request.content

        authed_request = await flow.asend(httpx.Response(
            200,
            json={"access_token": "fresh", "token_type": "Bearer", "expires_in": 3600},
            request=refresh_request,
        ))
        assert authed_request.headers["Authorization"] == "Bearer fresh"

        try:
            await flow.asend(httpx.Response(202, request=authed_request))
        except StopAsyncIteration:
            return
        raise AssertionError("headless auth flow should finish after MCP response")

    asyncio.run(run_flow())


def test_headless_oauth_preflight_refreshes_before_stream_client(monkeypatch, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({
        "access_token": "stale",
        "refresh_token": "refreshable",
        "token_type": "Bearer",
        "expires_at": time.time() - 10,
    }))
    mgr = McpManager()
    sent_urls = []

    class FakeAuth:
        async def async_auth_flow(self, original_request):
            metadata_request = httpx.Request("GET", "https://example.test/.well-known/oauth-protected-resource/mcp")
            yield metadata_request
            refresh_request = httpx.Request("POST", "https://auth.example.test/token")
            yield refresh_request
            original_request.headers["Authorization"] = "Bearer fresh"
            yield original_request

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, request):
            sent_urls.append(str(request.url))
            return httpx.Response(200, request=request)

    monkeypatch.setattr(mgr, "_build_oauth_auth", lambda *_args, **_kwargs: FakeAuth())
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    asyncio.run(mgr._refresh_headless_oauth_if_needed(
        "https://example.test/mcp",
        {"token_file": str(token_file)},
    ))

    assert sent_urls == [
        "https://example.test/.well-known/oauth-protected-resource/mcp",
        "https://auth.example.test/token",
    ]


def test_connect_server_marks_headless_oauth_failure_needs_oauth(monkeypatch, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"access_token": "stale", "token_type": "Bearer"}))
    mgr = McpManager()

    async def fail_connect(*_args, **_kwargs):
        raise RuntimeError("No redirect handler provided for authorization code grant")

    monkeypatch.setattr(mgr, "_connect_streamable_http", fail_connect)

    result = asyncio.run(mgr.connect_server(
        server_id="remote",
        name="Remote OAuth",
        transport="streamable_http",
        url="https://example.test/mcp",
        oauth_config={"token_file": str(token_file)},
    ))

    assert result is False
    status = mgr.get_server_status("remote")
    assert status["status"] == "needs_oauth"
    assert "OAuth authorization required" in status["error"]


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def query(self, _model):
        return _FakeQuery(self._rows)

    def close(self):
        self.closed = True


class _FakeMcpServer:
    is_enabled = True
    id = "id"


def _server(server_id, name):
    return SimpleNamespace(
        id=server_id,
        name=name,
        transport="stdio",
        command="cmd",
        args="[]",
        env="{}",
        url=None,
        oauth_config=None,
    )


def test_connect_all_enabled_marks_interactive_mcp_remote_needs_oauth(monkeypatch):
    srv = _server("remote", "Remote OAuth")
    srv.command = "npx"
    srv.args = '["-y", "mcp-remote", "https://example.test/mcp"]'
    db = _FakeDb([srv])
    monkeypatch.setitem(
        sys.modules,
        "src.database",
        SimpleNamespace(McpServer=_FakeMcpServer, SessionLocal=lambda: db),
    )

    mgr = McpManager()

    async def fail_connect_server(**_config):
        raise AssertionError("interactive mcp-remote should not autostart")

    monkeypatch.setattr(mgr, "connect_server", fail_connect_server)

    result = asyncio.run(mgr.connect_all_enabled(per_server_timeout=12))

    assert result == []
    status = mgr.get_server_status("remote")
    assert status["status"] == "needs_oauth"
    assert "Interactive OAuth required" in status["error"]


def test_connect_all_enabled_marks_expired_oauth_needs_oauth(monkeypatch, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"expires_in": 3600}))
    old = time.time() - 7200
    os.utime(token_file, (old, old))

    srv = _server("remote", "Remote OAuth")
    srv.transport = "streamable_http"
    srv.url = "https://example.test/mcp"
    srv.oauth_config = json.dumps({"token_file": str(token_file)})
    db = _FakeDb([srv])
    monkeypatch.setitem(
        sys.modules,
        "src.database",
        SimpleNamespace(McpServer=_FakeMcpServer, SessionLocal=lambda: db),
    )

    mgr = McpManager()

    async def fail_connect_server(**_config):
        raise AssertionError("expired OAuth token should not autostart")

    monkeypatch.setattr(mgr, "connect_server", fail_connect_server)

    result = asyncio.run(mgr.connect_all_enabled(per_server_timeout=12))

    assert result == []
    status = mgr.get_server_status("remote")
    assert status["status"] == "needs_oauth"
    assert "expired" in status["error"]


def test_connect_all_enabled_attempts_servers_concurrently(monkeypatch):
    db = _FakeDb([
        _server("slow", "Slow MCP"),
        _server("fast", "Fast MCP"),
    ])
    monkeypatch.setitem(
        sys.modules,
        "src.database",
        SimpleNamespace(McpServer=_FakeMcpServer, SessionLocal=lambda: db),
    )

    mgr = McpManager()
    slow_started = asyncio.Event()
    slow_done = False
    fast_observed_slow_pending = []

    async def fake_connect_server(**config):
        nonlocal slow_done
        if config["server_id"] == "slow":
            slow_started.set()
            try:
                await asyncio.sleep(0.05)
            finally:
                slow_done = True
            return True
        await slow_started.wait()
        fast_observed_slow_pending.append(not slow_done)
        mgr._connections[config["server_id"]] = {
            "status": "connected",
            "name": config["name"],
        }
        return True

    monkeypatch.setattr(mgr, "connect_server", fake_connect_server)

    result = asyncio.run(mgr.connect_all_enabled(per_server_timeout=0.01))

    assert result == [False, True]
    assert fast_observed_slow_pending == [True]
    assert mgr.get_server_status("slow")["status"] == "error"
    assert mgr.get_server_status("fast")["status"] == "connected"
    assert db.closed is True


def test_call_tool_reconnects_configured_server_after_call_failure(monkeypatch):
    mgr = McpManager()
    mgr._sessions["remote"] = object()
    calls = []

    async def fake_do_call(_session, tool_name, arguments):
        calls.append((tool_name, arguments))
        if len(calls) == 1:
            raise RuntimeError("stale session")
        return {"stdout": "ok", "stderr": "", "exit_code": 0}

    async def fake_reconnect(server_id):
        assert server_id == "remote"
        mgr._sessions["remote"] = object()
        return True

    monkeypatch.setattr(mgr, "_do_call", fake_do_call)
    monkeypatch.setattr(mgr, "_reconnect_configured_server", fake_reconnect)

    result = asyncio.run(mgr.call_tool("mcp__remote__search", {"query": "jira"}))

    assert result == {"stdout": "ok", "stderr": "", "exit_code": 0}
    assert calls == [("search", {"query": "jira"}), ("search", {"query": "jira"})]


def test_call_tool_reports_exception_type_when_reconnect_fails(monkeypatch):
    mgr = McpManager()
    mgr._sessions["remote"] = object()

    async def fake_do_call(_session, _tool_name, _arguments):
        raise RuntimeError()

    async def fake_reconnect(_server_id):
        return False

    monkeypatch.setattr(mgr, "_do_call", fake_do_call)
    monkeypatch.setattr(mgr, "_reconnect_configured_server", fake_reconnect)

    result = asyncio.run(mgr.call_tool("mcp__remote__search", {}))

    assert result["exit_code"] == 1
    assert "RuntimeError" in result["error"]


def test_call_tool_rejects_missing_required_arguments_before_network(monkeypatch):
    mgr = McpManager()
    mgr._sessions["remote"] = object()
    mgr._tools["remote"] = [
        {
            "name": "search",
            "description": "Search Atlassian",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    async def fake_do_call(*_args, **_kwargs):
        raise AssertionError("MCP network call should not run for invalid args")

    monkeypatch.setattr(mgr, "_do_call", fake_do_call)

    result = asyncio.run(mgr.call_tool("mcp__remote__search", {}))

    assert result["exit_code"] == 2
    assert result["missing_required"] == ["query"]
    assert "missing required" in result["error"]
    assert '{"query": "..."}' in result["error"]


def test_missing_required_arguments_resolves_mcp_name_case_insensitively():
    mgr = McpManager()
    mgr._sessions["Atlassian"] = object()
    mgr._tools["Atlassian"] = [
        {
            "name": "SearchJiraIssuesUsingJql",
            "description": "Search Jira",
            "input_schema": {
                "type": "object",
                "properties": {"jql": {"type": "string"}},
                "required": ["jql"],
            },
        }
    ]

    assert mgr.missing_required_arguments(
        "mcp__ATLASSIAN__searchjiraissuesusingjql",
        {},
    ) == ["jql"]


def test_call_tool_uses_canonical_mcp_tool_name_case_insensitively(monkeypatch):
    mgr = McpManager()
    session = object()
    mgr._sessions["Atlassian"] = session
    mgr._tools["Atlassian"] = [
        {
            "name": "SearchJiraIssuesUsingJql",
            "description": "Search Jira",
            "input_schema": {
                "type": "object",
                "properties": {"jql": {"type": "string"}},
                "required": ["jql"],
            },
        }
    ]
    captured = {}

    async def fake_do_call(call_session, tool_name, arguments):
        captured["session"] = call_session
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(mgr, "_do_call", fake_do_call)

    result = asyncio.run(
        mgr.call_tool("mcp__atlassian__searchjiraissuesusingjql", {"jql": "project = ABC"})
    )

    assert result == {"content": [{"type": "text", "text": "ok"}]}
    assert captured == {
        "session": session,
        "tool_name": "SearchJiraIssuesUsingJql",
        "arguments": {"jql": "project = ABC"},
    }


def test_coerce_tool_arguments_maps_scalar_to_single_required_arg():
    mgr = McpManager()
    mgr._tools["remote"] = [
        {
            "name": "search",
            "description": "Search Atlassian",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    assert mgr.coerce_tool_arguments(
        "mcp__remote__search",
        {},
        scalar_arg="circit ai",
    ) == {"query": "circit ai"}


def test_coerce_tool_arguments_maps_raw_string_to_single_required_arg():
    mgr = McpManager()
    mgr._tools["remote"] = [
        {
            "name": "search",
            "description": "Search Atlassian",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    assert mgr.coerce_tool_arguments(
        "mcp__remote__search",
        "circit ai",
    ) == {"query": "circit ai"}


def test_coerce_tool_arguments_maps_single_unknown_key_to_required_arg():
    mgr = McpManager()
    mgr._tools["remote"] = [
        {
            "name": "search",
            "description": "Search Atlassian",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    assert mgr.coerce_tool_arguments(
        "mcp__remote__search",
        {"q": "circit ai"},
    ) == {"query": "circit ai"}
    assert mgr.coerce_tool_arguments(
        "mcp__remote__search",
        {"arguments": "circit ai"},
    ) == {"query": "circit ai"}
    assert mgr.coerce_tool_arguments(
        "mcp__remote__search",
        {"query": "circit ai"},
    ) == {"query": "circit ai"}


def test_call_tool_coerces_raw_string_before_network(monkeypatch):
    mgr = McpManager()
    mgr._sessions["remote"] = object()
    mgr._tools["remote"] = [
        {
            "name": "search",
            "description": "Search Atlassian",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]
    captured = {}

    async def fake_do_call(_session, _tool_name, arguments):
        captured["arguments"] = arguments
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(mgr, "_do_call", fake_do_call)

    result = asyncio.run(mgr.call_tool("mcp__remote__search", "circit ai"))

    assert result == {"content": [{"type": "text", "text": "ok"}]}
    assert captured["arguments"] == {"query": "circit ai"}


def test_openai_schemas_normalize_mcp_required_parameters():
    mgr = McpManager()
    mgr._connections["remote"] = {"status": "connected", "name": "Atlassian"}
    mgr._tools["remote"] = [
        {
            "name": "search",
            "description": "Search Atlassian",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    schema = mgr.get_all_openai_schemas()[0]["function"]

    assert schema["parameters"]["additionalProperties"] is False
    assert schema["parameters"]["required"] == ["query"]
    assert schema["strict"] is True
    assert "Required JSON argument(s): query" in schema["description"]


def test_unavailable_mcp_server_tools_are_not_advertised():
    mgr = McpManager()
    mgr._connections["remote"] = {
        "status": "needs_oauth",
        "name": "Atlassian",
        "error": "OAuth token expired",
    }
    mgr._tools["remote"] = [
        {
            "name": "search",
            "description": "Search Atlassian",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]

    assert mgr.get_all_openai_schemas() == []
    assert mgr.get_all_tools() == []
    assert "mcp__remote__search" not in mgr.get_tool_descriptions_for_prompt()
