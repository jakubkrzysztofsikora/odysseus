from src.mcp_manager import McpManager, _format_mcp_connection_error, normalize_mcp_transport


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
