import httpx

from src.chat_processor import ChatProcessor
from src.search import content as search_content


class _MemoryManager:
    def load(self, owner=None):
        return []


class _DocsManager:
    rag_manager = None


class _FakeStatusResponse:
    status_code = 404
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b""
    text = ""

    def raise_for_status(self):
        request = httpx.Request("GET", "https://api.anthropic.com/v1/design/h/missing")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("missing", request=request, response=response)


def test_context_url_prefetch_http_status_error_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(search_content, "CONTENT_CACHE_DIR", tmp_path)
    search_content.content_cache_index.clear()
    monkeypatch.setattr(
        search_content,
        "_get_public_url",
        lambda url, headers, timeout: _FakeStatusResponse(),
    )

    processor = ChatProcessor(_MemoryManager(), _DocsManager())

    preface, rag_sources, web_sources = processor.build_context_preface(
        "read https://api.anthropic.com/v1/design/h/tfcgi3SRbdDTwrkxvsRAgg",
        session=None,
        use_memory=False,
        use_rag=False,
        use_web=False,
    )

    assert preface
    assert rag_sources == []
    assert web_sources == []
    assert not any(
        "Content from https://api.anthropic.com/v1/design/h/tfcgi3SRbdDTwrkxvsRAgg"
        in item.get("content", "")
        for item in preface
    )
