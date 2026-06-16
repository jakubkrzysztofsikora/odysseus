import json

import pytest
from fastapi import Request
from fastapi.datastructures import State

from routes.skills_routes import setup_skills_routes
from services.memory.skills import SkillsManager


def _request(auth_mode: str = "cloudflare_access") -> Request:
    return Request(scope={
        "type": "http",
        "method": "GET",
        "app": type("App", (), {"state": State()})(),
        "state": {"auth_mode": auth_mode, "current_user": "jakub.sikora@circit.io"},
        "headers": [],
    })


def _handler(router, path: str, method: str):
    return next(r.endpoint for r in router.routes if r.path == path and method in r.methods)


@pytest.mark.asyncio
async def test_cloudflare_builtin_skill_catalog_is_circitron_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_AUTH_MODE", "cloudflare_access")
    monkeypatch.setattr("src.agent_loop.get_builtin_overrides", lambda: {})
    router = setup_skills_routes(SkillsManager(str(tmp_path)))

    list_builtin = _handler(router, "/api/skills/builtin", "GET")
    result = await list_builtin(_request())
    serialized = json.dumps(result)

    assert result["count"] > 0
    assert "Circitron" in serialized
    assert "Odysseus" not in serialized
    assert "Qwen" not in serialized
    assert "qwen" not in serialized
    assert "gpt-4o" not in serialized

    descriptions = {item["name"]: item["description"] for item in result["builtin"]}
    assert descriptions["list_models"] == (
        "Show the available public chat capability. In this deployment it returns Circitron."
    )
    assert "Regular users cannot add or change model providers" in descriptions["manage_endpoints"]

    get_builtin = _handler(router, "/api/skills/builtin/{name}", "GET")
    detail = await get_builtin("app_api", _request())
    assert detail["text"] == (
        "Generic loopback to Circitron internal endpoints. "
        "Use only when no named tool covers a UI action."
    )
