"""Unit tests for do_generate_video (Seedance 2 via litellm passthrough).

Mocks all network + DB so no credits are spent. Verifies the async
create -> poll -> download -> gallery-save contract:
  - POST {base}/videos/generations returns {"taskId": ...}
  - GET  {base}/tasks/{taskId} returns status generating then completed
  - data.results[0] mp4 url is downloaded and written under generated_images
  - a GalleryImage row is created
  - the returned dict carries the video_* fields

do_generate_video imports asyncio/httpx INSIDE the function body, so we patch
``sys.modules`` for those rather than module attributes.
"""
import sys

for _mod_name in ["src.endpoint_resolver", "src.database", "core.database"]:
    _mod = sys.modules.get(_mod_name)
    if _mod is not None and not getattr(_mod, "__file__", None):
        sys.modules.pop(_mod_name, None)

import asyncio
import hashlib
import types
from types import SimpleNamespace

import pytest

import src.ai_interaction as ai


VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"fake-mp4-payload" * 4096
MP4_URL = "https://cdn.seedance2.ai/api/videos/2026-06-21/abc.mp4"
TASK_ID = "qLXeYjktXJ2r2LsDjATbq1D8"


class _Resp:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.text = ""

    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        self._polls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, **k):
        assert url.endswith("/videos/generations"), url
        return _Resp(200, {"taskId": TASK_ID, "credits": 60})

    async def get(self, url, headers=None, **k):
        if "/tasks/" in url:
            self._polls += 1
            if self._polls < 2:
                return _Resp(200, {
                    "status": "generating", "id": TASK_ID,
                    "data": {"results": [], "video_expires_at": None},
                })
            return _Resp(200, {
                "status": "completed", "id": TASK_ID,
                "data": {"results": [MP4_URL], "video_expires_at": "2026-07-05T00:00:00Z"},
            })
        assert url == MP4_URL, url
        return _Resp(200, content=VIDEO_BYTES)


def _make_fake_httpx(client_cls):
    """Build a stand-in httpx module exposing AsyncClient + Timeout."""
    mod = types.ModuleType("httpx")
    mod.AsyncClient = client_cls

    class _Timeout:
        def __init__(self, *a, **k):
            pass

    mod.Timeout = _Timeout
    return mod


@pytest.fixture
def _patched(monkeypatch, tmp_path):
    # asyncio.sleep -> no-op (the in-function `import asyncio` gets the real
    # module, so patch the attribute on the real module).
    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    # Deterministic endpoint + auth.
    monkeypatch.setattr(
        ai, "_resolve_video_endpoint",
        lambda owner=None: ("http://litellm:4000/seedance/v1", {"Authorization": "Bearer test"}),
    )

    # Media dir -> tmp.
    monkeypatch.setattr(ai, "GENERATED_IMAGES_DIR", str(tmp_path), raising=False)

    # SSRF check -> allow (imported as `from src.url_safety import check_outbound_url`).
    import src.url_safety as us
    monkeypatch.setattr(us, "check_outbound_url", lambda *a, **k: (True, "ok"), raising=False)

    # Capture the GalleryImage row (imported as `from src.database import SessionLocal, GalleryImage`).
    created = {}

    class _Row:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class _FakeGallerySession:
        def add(self, row):
            created["row"] = row

        def commit(self):
            pass

        def close(self):
            pass

    import src.database as sdb
    monkeypatch.setattr(sdb, "SessionLocal", lambda: _FakeGallerySession(), raising=False)
    monkeypatch.setattr(sdb, "GalleryImage", _Row, raising=False)

    return SimpleNamespace(created=created, tmp_path=tmp_path)


def test_do_generate_video_happy_path(monkeypatch, _patched):
    monkeypatch.setitem(sys.modules, "httpx", _make_fake_httpx(_FakeAsyncClient))

    result = asyncio.run(
        ai.do_generate_video("a red kite over green hills\n5\n720p", owner=None)
    )

    assert "error" not in result, result
    assert result["video_model"] == "seedance-2-0"
    assert result["video_size"] == "720p"
    assert result["video_id"] == TASK_ID
    assert result["video_url"].startswith("/api/generated-image/")
    assert result["video_url"].endswith(".mp4")

    expected_name = hashlib.sha256(VIDEO_BYTES).hexdigest()[:16] + ".mp4"
    written = list(_patched.tmp_path.glob("*.mp4"))
    assert len(written) == 1, written
    assert written[0].name == expected_name
    assert written[0].read_bytes() == VIDEO_BYTES

    row = _patched.created.get("row")
    assert row is not None
    assert getattr(row, "model", None) == "seedance-2-0"
    assert getattr(row, "size", None) == "720p"


def test_do_generate_video_failed_status(monkeypatch, _patched):
    class _FailClient(_FakeAsyncClient):
        async def get(self, url, headers=None, **k):
            if "/tasks/" in url:
                return _Resp(200, {"status": "failed", "id": TASK_ID,
                                   "data": {"results": []}, "failed_reason": "nsfw"})
            return _Resp(200, content=VIDEO_BYTES)

    monkeypatch.setitem(sys.modules, "httpx", _make_fake_httpx(_FailClient))
    result = asyncio.run(ai.do_generate_video("bad prompt\n5\n720p", owner=None))
    assert "error" in result
    assert "fail" in result["error"].lower() or "generation" in result["error"].lower()
