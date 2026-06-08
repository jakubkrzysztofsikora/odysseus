"""Keep src.search and services.search content extraction behavior aligned."""

import httpx
import pytest

pytest.importorskip("bs4")

from services.search import content as service_content
from src.search import content as src_content


class _FakeResponse:
    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b""

    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeStatusResponse:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b""
    text = ""

    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        request = httpx.Request("GET", "https://example.com/missing")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("missing", request=request, response=response)


@pytest.mark.parametrize("module", [src_content, service_content])
def test_content_fetcher_extracts_og_image_and_body_fallback(module, tmp_path, monkeypatch):
    html = """
    <html>
      <head>
        <title>Example</title>
        <meta property="og:image" content="https://example.com/cover.jpg">
      </head>
      <body>
        <nav>Navigation text should not win</nav>
        <div class="content">Tiny</div>
        <main>
          <p>This is the substantive body text that should be retained.</p>
          <p>It is much longer than the tiny class-matched wrapper.</p>
        </main>
        <script>window.secret = "not content";</script>
      </body>
    </html>
    """

    monkeypatch.setattr(module, "CONTENT_CACHE_DIR", tmp_path)
    module.content_cache_index.clear()
    monkeypatch.setattr(module, "_get_public_url", lambda url, headers, timeout: _FakeResponse(html))

    result = module.fetch_webpage_content("https://example.com/parity-test")

    assert result["og_image"] == "https://example.com/cover.jpg"
    assert "substantive body text" in result["content"]
    assert "much longer than the tiny" in result["content"]
    assert "window.secret" not in result["content"]


@pytest.mark.parametrize("module", [src_content, service_content])
def test_content_fetcher_http_status_error_returns_empty_result(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "CONTENT_CACHE_DIR", tmp_path)
    module.content_cache_index.clear()
    monkeypatch.setattr(
        module,
        "_get_public_url",
        lambda url, headers, timeout: _FakeStatusResponse(404),
    )

    result = module.fetch_webpage_content("https://api.anthropic.com/v1/design/h/missing")

    assert result["success"] is False
    assert result["content"] == ""
    assert "HTTPStatusError 404" in result["error"]
