import asyncio
import json

import httpx

from src import llm_core


def test_openai_base_url_normalizes_to_chat_completions():
    assert (
        llm_core._normalize_openai_chat_url("http://litellm.tail5d39b4.ts.net:4000/v1")
        == "http://litellm.tail5d39b4.ts.net:4000/v1/chat/completions"
    )
    assert (
        llm_core._normalize_openai_chat_url("http://litellm.tail5d39b4.ts.net:4000/v1/chat/completions")
        == "http://litellm.tail5d39b4.ts.net:4000/v1/chat/completions"
    )


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
