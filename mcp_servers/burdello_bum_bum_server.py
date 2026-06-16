"""
MCP bridge for the local Burdello Bum-Bum REST API.

The Burdello backend already exposes the transcript/project/task surfaces over
FastAPI. This server keeps Odysseus' MCP integration narrow by wrapping those
endpoints as read-oriented tools instead of duplicating Burdello's internals.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_servers._common import truncate


server = Server("burdello-bum-bum")

DEFAULT_API_URL = "http://[::1]:8000/api/v1"


def _base_url() -> str:
    return os.environ.get("BURDELLO_API_URL", DEFAULT_API_URL).rstrip("/")


def _make_url(path: str, query: dict[str, Any] | None = None) -> str:
    url = _base_url() + "/" + path.lstrip("/")
    clean_query = {
        key: value
        for key, value in (query or {}).items()
        if value not in (None, "", [], {})
    }
    if clean_query:
        url += "?" + urllib.parse.urlencode(clean_query, doseq=True)
    return url


def _request(method: str, path: str, *, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_make_url(path, query), data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return {
            "error": f"Burdello API returned HTTP {exc.code}",
            "body": _try_json(raw),
        }
    except Exception as exc:
        return {"error": f"Burdello API request failed: {type(exc).__name__}: {exc}"}
    return _try_json(raw)


def _try_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _text(payload: Any) -> list[TextContent]:
    return [TextContent(type="text", text=truncate(json.dumps(payload, indent=2, ensure_ascii=False), 20_000))]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="burdello_status",
            description="Return Burdello Bum-Bum aggregate counts and backend status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="burdello_search",
            description="Search indexed Claude/Codex transcripts and mined artifacts in Burdello.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "filters": {"type": "object", "description": "Optional Burdello search filters"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="burdello_list_transcripts",
            description="List Burdello transcripts with optional project/status/provider/search filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "status": {"type": "string"},
                    "provider": {"type": "string"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "skip": {"type": "integer", "minimum": 0, "default": 0},
                },
            },
        ),
        Tool(
            name="burdello_get_transcript",
            description="Fetch one Burdello transcript by id, including messages when available.",
            inputSchema={
                "type": "object",
                "properties": {
                    "transcript_id": {"type": "string", "description": "Transcript UUID"},
                },
                "required": ["transcript_id"],
            },
        ),
        Tool(
            name="burdello_list_projects",
            description="List mined Burdello projects with task counts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "skip": {"type": "integer", "minimum": 0, "default": 0},
                },
            },
        ),
        Tool(
            name="burdello_list_tasks",
            description="List mined Burdello tasks with optional status, priority, project, and text filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "status": {"type": "string"},
                    "priority": {"type": "string"},
                    "project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "skip": {"type": "integer", "minimum": 0, "default": 0},
                },
            },
        ),
        Tool(
            name="burdello_list_skills",
            description="List Burdello's transcript ingestion skills.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    arguments = arguments or {}
    if name == "burdello_status":
        return _text(await asyncio.to_thread(_request, "GET", "stats"))
    if name == "burdello_search":
        payload = {
            "query": arguments.get("query", ""),
            "limit": int(arguments.get("limit") or 10),
            "offset": int(arguments.get("offset") or 0),
        }
        filters = arguments.get("filters")
        if isinstance(filters, dict) and filters:
            payload["filters"] = filters
        return _text(await asyncio.to_thread(_request, "POST", "search/", body=payload))
    if name == "burdello_list_transcripts":
        return _text(await asyncio.to_thread(_request, "GET", "transcripts/", query=arguments))
    if name == "burdello_get_transcript":
        transcript_id = str(arguments.get("transcript_id") or "").strip()
        if not transcript_id:
            return _text({"error": "transcript_id is required"})
        return _text(await asyncio.to_thread(_request, "GET", f"transcripts/{urllib.parse.quote(transcript_id)}"))
    if name == "burdello_list_projects":
        return _text(await asyncio.to_thread(_request, "GET", "projects/", query=arguments))
    if name == "burdello_list_tasks":
        return _text(await asyncio.to_thread(_request, "GET", "tasks/", query=arguments))
    if name == "burdello_list_skills":
        return _text(await asyncio.to_thread(_request, "GET", "skills/"))
    return _text({"error": f"Unknown tool: {name}"})


async def run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
