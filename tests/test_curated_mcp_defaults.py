import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.builtin_mcp import _curated_remote_mcp_servers, _remote_oauth_config  # noqa: E402


def test_curated_remote_mcp_defaults_include_circit_integrations(monkeypatch):
    monkeypatch.delenv("M365_EXCHANGE_MCP_URL", raising=False)
    monkeypatch.delenv("WORKIQ_MCP_TENANT_ID", raising=False)
    servers = _curated_remote_mcp_servers()

    assert servers["circit_sentry"]["url"] == "https://mcp.sentry.dev/mcp"
    assert servers["circit_atlassian"]["url"] == "https://mcp.atlassian.com/v1/mcp/authv2"
    assert (
        servers["circit_slack"]["url"]
        == "https://circitron-app.nicestone-ab7e633c.northeurope.azurecontainerapps.io/api/mcp"
    )
    assert servers["circit_slack"]["enabled"] is True
    assert servers["circit_microsoft_graph"]["url"] == "https://mcp.svc.cloud.microsoft/enterprise"
    assert (
        servers["circit_workiq_mail"]["url"]
        == "https://agent365.svc.cloud.microsoft/agents/tenants/b6560c52-065a-424b-90b1-5340eab75de9/servers/mcp_MailTools"
    )
    assert (
        servers["circit_workiq_calendar"]["url"]
        == "https://agent365.svc.cloud.microsoft/agents/tenants/b6560c52-065a-424b-90b1-5340eab75de9/servers/mcp_CalendarTools"
    )
    assert servers["circit_microsoft_graph"]["oauth_config"]["provider"] == "mcp"
    assert servers["circit_workiq_mail"]["enabled"] is True
    assert servers["circit_workiq_calendar"]["enabled"] is True
    assert servers["circit_m365_exchange"]["url"] == "https://mcp.svc.cloud.microsoft/enterprise"
    assert servers["circit_m365_exchange"]["enabled"] is True
    assert servers["circit_m365_exchange"]["oauth_config"]["provider"] == "mcp"


def test_exchange_mcp_url_can_be_tenant_supplied(monkeypatch):
    monkeypatch.setenv("M365_EXCHANGE_MCP_URL", "https://m365-mcp.circit.io/mcp")
    servers = _curated_remote_mcp_servers()

    assert servers["circit_m365_exchange"]["url"] == "https://m365-mcp.circit.io/mcp"
    assert servers["circit_m365_exchange"]["enabled"] is True
    assert servers["circit_m365_exchange"]["oauth_config"]["provider"] == "mcp"


def test_default_exchange_alias_is_enabled_with_oauth_config(monkeypatch, tmp_path):
    from core.database import Base, McpServer
    import core.database as database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import src.builtin_mcp as builtin_mcp

    monkeypatch.delenv("M365_EXCHANGE_MCP_URL", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)

    builtin_mcp.seed_curated_remote_mcp_servers()

    db = TestingSessionLocal()
    try:
        exchange = db.query(McpServer).filter(McpServer.id == "circit_m365_exchange").first()
        assert exchange is not None
        assert exchange.is_enabled is True
        assert exchange.url == "https://mcp.svc.cloud.microsoft/enterprise"
        assert exchange.oauth_config is not None
    finally:
        db.close()


def test_remote_oauth_config_uses_public_callback(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://cowork.circit.ai/")
    cfg = _remote_oauth_config("circit_sentry")

    assert cfg["client_name"] == "Circitron"
    assert cfg["redirect_uris"] == ["https://cowork.circit.ai/api/mcp/oauth/callback"]
    assert cfg["token_file"].endswith("circit_sentry.json")


def test_workiq_oauth_can_use_tenant_client_id(monkeypatch):
    monkeypatch.setenv("WORKIQ_MCP_CLIENT_ID", "00000000-0000-0000-0000-000000000123")
    monkeypatch.delenv("WORKIQ_MCP_CLIENT_SECRET", raising=False)
    servers = _curated_remote_mcp_servers()

    oauth = servers["circit_workiq_mail"]["oauth_config"]
    assert oauth["client_id"] == "00000000-0000-0000-0000-000000000123"
    assert oauth["token_endpoint_auth_method"] == "none"
