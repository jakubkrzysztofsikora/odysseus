from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mcp_routes_forward_oauth_config_on_connect_paths():
    text = (ROOT / "routes" / "mcp_routes.py").read_text()
    assert "oauth_config=parsed_oauth_config" in text
    assert text.count("oauth_config=oauth_config") >= 3
    assert "_server_runtime_config" in text
    assert "_default_remote_mcp_oauth_config" in text
    assert "_remote_advertises_oauth" in text
    assert "_looks_like_remote_auth_error" in text
    assert '"status": "needs_oauth"' in text
    assert "_complete_mcp_oauth_callback" in text


def test_mcp_ui_exposes_streamable_http_transport():
    admin_js = (ROOT / "static" / "js" / "admin.js").read_text()
    settings_js = (ROOT / "static" / "js" / "settings.js").read_text()

    assert "streamable_http" in settings_js
    assert "transport === 'streamable_http'" in admin_js


def test_mcp_oauth_page_uses_relative_exchange_action():
    text = (ROOT / "routes" / "mcp_routes.py").read_text()
    assert 'action="/api/mcp/oauth/exchange/{server_id}"' in text
    assert "Authorize {provider_name}" in text


def test_mcp_oauth_callback_reaches_route_before_auth_middleware():
    text = (ROOT / "app.py").read_text()
    assert '"/api/mcp/oauth/callback"' in text
