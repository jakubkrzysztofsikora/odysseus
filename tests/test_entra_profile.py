import base64

import httpx
import pytest

from src import entra_profile


class _FakeAsyncClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, data=None, headers=None):
        self.calls.append(("POST", url, data, headers))
        return httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3600})

    async def get(self, url, *, headers=None):
        self.calls.append(("GET", url, None, headers))
        return httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    entra_profile.clear_entra_profile_cache()
    _FakeAsyncClient.calls = []
    for key in (
        "ENTRA_PROFILE_CLIENT_ID",
        "ENTRA_PROFILE_CLIENT_SECRET",
        "WORKIQ_MCP_CLIENT_ID",
        "WORKIQ_MCP_CLIENT_SECRET",
        "MICROSOFT_GRAPH_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    entra_profile.clear_entra_profile_cache()


@pytest.mark.asyncio
async def test_entra_profile_photo_returns_data_url_and_caches(monkeypatch):
    monkeypatch.setenv("ENTRA_PROFILE_CLIENT_ID", "client-id")
    monkeypatch.setenv("ENTRA_PROFILE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(entra_profile.httpx, "AsyncClient", _FakeAsyncClient)

    avatar = await entra_profile.get_entra_profile_avatar_url("Jakub.Sikora@Circit.IO")
    again = await entra_profile.get_entra_profile_avatar_url("jakub.sikora@circit.io")

    expected = "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii")
    assert avatar == expected
    assert again == expected
    assert [call[0] for call in _FakeAsyncClient.calls] == ["POST", "GET"]
    assert "/users/jakub.sikora%40circit.io/photo/$value" in _FakeAsyncClient.calls[1][1]


@pytest.mark.asyncio
async def test_entra_profile_photo_skips_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(entra_profile.httpx, "AsyncClient", _FakeAsyncClient)

    avatar = await entra_profile.get_entra_profile_avatar_url("jakub.sikora@circit.io")

    assert avatar == ""
    assert _FakeAsyncClient.calls == []
