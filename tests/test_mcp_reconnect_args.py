"""Verify that MCP reconnect via the agent tool passes full server metadata."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace


def test_reconnect_passes_full_server_config(tmp_path):
    """do_manage_mcp reconnect must pass name/transport/command/args/env/url."""
    from src.tool_implementations import do_manage_mcp

    token_file = tmp_path / "mcp-token.json"
    token_file.write_text('{"access_token":"test-token"}')

    fake_mcp = MagicMock()
    fake_mcp.disconnect_server = AsyncMock()
    fake_mcp.connect_server = AsyncMock(return_value=True)
    fake_mcp.get_server_status = MagicMock(return_value={"tool_count": 3})

    fake_srv = SimpleNamespace(
        id="srv-123",
        name="test-server",
        transport="stdio",
        command="/usr/bin/test",
        args=json.dumps(["--flag"]),
        env=json.dumps({"KEY": "val"}),
        url=None,
        oauth_config=json.dumps({"token_file": str(token_file), "scopes": ["read"]}),
    )

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.first.return_value = fake_srv

    with patch("src.tool_implementations.get_mcp_manager", return_value=fake_mcp), \
         patch("core.database.SessionLocal", return_value=fake_db):
        result = asyncio.run(do_manage_mcp(
            json.dumps({"action": "reconnect", "server_id": "srv-123"})
        ))

    assert result["exit_code"] == 0
    fake_mcp.connect_server.assert_called_once_with(
        server_id="srv-123",
        name="test-server",
        transport="stdio",
        command="/usr/bin/test",
        args=["--flag"],
        env={"KEY": "val"},
        url=None,
        oauth_config={"token_file": str(token_file), "scopes": ["read"]},
    )


def test_reconnect_reports_needs_oauth_for_missing_token(tmp_path):
    from src.tool_implementations import do_manage_mcp

    fake_mcp = MagicMock()
    fake_mcp.disconnect_server = AsyncMock()
    fake_mcp.connect_server = AsyncMock(return_value=True)

    fake_srv = SimpleNamespace(
        id="srv-123",
        name="remote-server",
        transport="streamable_http",
        command=None,
        args=json.dumps([]),
        env=json.dumps({}),
        url="https://example.test/mcp",
        oauth_config=json.dumps({"token_file": str(tmp_path / "missing-token.json")}),
    )

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.first.return_value = fake_srv

    with patch("src.tool_implementations.get_mcp_manager", return_value=fake_mcp), \
         patch("core.database.SessionLocal", return_value=fake_db):
        result = asyncio.run(do_manage_mcp(
            json.dumps({"action": "reconnect", "server_id": "srv-123"})
        ))

    assert result["exit_code"] == 1
    assert result["needs_oauth"] is True
    fake_mcp.connect_server.assert_not_called()


def test_reconnect_legacy_mcp_remote_without_oauth_does_not_launch_headless_auth():
    from src.tool_implementations import do_manage_mcp

    fake_mcp = MagicMock()
    fake_mcp.disconnect_server = AsyncMock()
    fake_mcp.connect_server = AsyncMock(return_value=True)

    fake_srv = SimpleNamespace(
        id="circitron",
        name="Circitron",
        transport="stdio",
        command="npx",
        args=json.dumps(["-y", "mcp-remote", "https://example.test/api/mcp"]),
        env=json.dumps({}),
        url=None,
        oauth_config=None,
    )

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.first.return_value = fake_srv

    with patch("src.tool_implementations.get_mcp_manager", return_value=fake_mcp), \
         patch("core.database.SessionLocal", return_value=fake_db):
        result = asyncio.run(do_manage_mcp(
            json.dumps({"action": "reconnect", "server_id": "circitron"})
        ))

    assert result["exit_code"] == 1
    assert result["needs_oauth"] is True
    fake_mcp.connect_server.assert_not_called()
