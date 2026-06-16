import asyncio
import json

import httpx
import pytest

from src import llm_core


@pytest.fixture(autouse=True)
def _reset_egress_admin_hosts():
    """P6.2: reset the egress-guard admin-host provider after each test so the
    seeded Tailscale host doesn't leak into other tests in this module."""
    yield
    from src import egress_guard
    egress_guard.set_admin_hosts_provider(None)


def test_openai_base_url_normalizes_to_chat_completions():
    assert (
        llm_core._normalize_openai_chat_url("http://litellm.tail5d39b4.ts.net:4000/v1")
        == "http://litellm.tail5d39b4.ts.net:4000/v1/chat/completions"
    )
    assert (
        llm_core._normalize_openai_chat_url("http://litellm.tail5d39b4.ts.net:4000/v1/chat/completions")
        == "http://litellm.tail5d39b4.ts.net:4000/v1/chat/completions"
    )


def test_agentcore_short_chat_url_normalizes_to_chat_completions():
    assert (
        llm_core._normalize_openai_chat_url("http://127.0.0.1:7000/api/agentcore/openai/v1/chat")
        == "http://127.0.0.1:7000/api/agentcore/openai/v1/chat/completions"
    )


def test_agentcore_loopback_url_is_openai_compatible_not_ollama():
    url = "http://127.0.0.1:7000/api/agentcore/openai/v1/chat/completions"

    assert llm_core._is_ollama_native_url(url) is False
    assert llm_core._detect_provider(url) == "openai"


def test_list_model_ids_uses_models_endpoint_when_given_openai_base(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"id": "chatgpt/gpt-5.5"}]},
        )

    monkeypatch.setattr(llm_core, "_configured_cached_model_ids", lambda *_a, **_k: [])
    monkeypatch.setattr(llm_core.httpx, "get", fake_get)

    ids = llm_core.list_model_ids("http://litellm.tail5d39b4.ts.net:4000/v1")

    assert ids == ["chatgpt/gpt-5.5"]
    assert seen["url"] == "http://litellm.tail5d39b4.ts.net:4000/v1/models"


def test_stream_llm_posts_to_chat_completions_when_given_openai_base(monkeypatch):
    seen = {}

    class FakeResp:
        status_code = 200

        async def aiter_lines(self):
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "OK"}}]})
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def stream(self, method, url, **kwargs):
            seen["method"] = method
            seen["url"] = url
            seen["json"] = kwargs.get("json")
            return FakeStreamCtx()

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: FakeClient())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda *_a, **_k: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *_a, **_k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *_a, **_k: None)
    # P6.2 egress guard: a private Tailscale endpoint is reachable ONLY because
    # an admin configured it as a ModelEndpoint. Seed that admin host so the
    # guard authorizes it (production has the row; the test DB does not).
    from src import egress_guard
    egress_guard.set_admin_hosts_provider(lambda: frozenset({"litellm.tail5d39b4.ts.net"}))

    async def collect():
        return [
            chunk
            async for chunk in llm_core.stream_llm(
                "http://litellm.tail5d39b4.ts.net:4000/v1",
                "chatgpt/gpt-5.5",
                [{"role": "user", "content": "say OK"}],
                headers={"Authorization": "Bearer key"},
            )
        ]

    chunks = asyncio.run(collect())

    assert any('"delta": "OK"' in chunk for chunk in chunks)
    assert seen["method"] == "POST"
    assert seen["url"] == "http://litellm.tail5d39b4.ts.net:4000/v1/chat/completions"
    assert seen["json"]["model"] == "chatgpt/gpt-5.5"
