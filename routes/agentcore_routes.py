"""Routes that bridge Odysseus to the private Circit AgentCore service."""

from __future__ import annotations

import httpx
import json
import re
import time
import uuid
from starlette.background import BackgroundTask
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from src.agentcore_client import agentcore_request, build_agentcore_request, get_agentcore_config
from src.agentcore_model_seed import agentcore_public_model_alias
from src.action_intents import is_plain_chat_turn


ALLOWED_AGENTCORE_ROOTS = {
    "health",
    "config",
    "chat",
    "tasks",
    "skills",
    "agents",
    "workspaces",
    "auth",
    "audit-verify",
}


def _model_name_from_config(config_payload: dict) -> str:
    return agentcore_public_model_alias()


def _redacted_agentcore_config(config_payload: dict) -> dict:
    """Expose AgentCore runtime config without leaking its hosted model id."""
    alias = agentcore_public_model_alias()
    redacted = dict(config_payload or {})
    for key in (
        "model",
        "modelName",
        "ModelName",
        "deployment",
        "deploymentName",
        "DeploymentName",
        "azureDeployment",
        "AzureDeployment",
    ):
        if key in redacted:
            redacted[key] = alias
    for key in (
        "serverUrl",
        "ServerUrl",
        "aiOrchestratorEndpoint",
        "AiOrchestratorEndpoint",
        "apiKey",
        "ApiKey",
    ):
        redacted.pop(key, None)
    redacted["modelName"] = alias
    return redacted


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "text" and isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


_RAW_TOOL_CALL_PREFIX_RE = re.compile(
    r"^\s*(?:mcp__[A-Za-z0-9_-]+__)?[A-Za-z][A-Za-z0-9_.-]*"
    r"(?:-[A-Za-z0-9_.-]+)+\s*\{[\s\S]*\}\s*$"
)


def _looks_like_tool_call_text(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False
    if text.startswith("```"):
        text = text.strip("` \n")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("name"), str)
            and isinstance(payload.get("arguments"), dict)
        ):
            return True
    return bool(_RAW_TOOL_CALL_PREFIX_RE.match(text))


def _latest_user_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        text = _content_to_text(message.get("content")).strip()
        if text:
            return text
    return ""


def _sanitize_agentcore_direct_content(content: str, messages: list[dict]) -> str:
    if not _looks_like_tool_call_text(content):
        return content
    latest = _latest_user_text(messages)
    if is_plain_chat_turn(latest):
        return "Hi. How can I help?"
    return (
        "I cannot use a tool call as the chat answer. Please ask again in plain "
        "language, or switch to an agent workflow for tool actions."
    )


def _direct_chat_max_tokens(value) -> int:
    try:
        requested = int(value or 0)
    except (TypeError, ValueError):
        requested = 0
    return max(requested, 512)


def _messages_to_task_description(messages: list[dict]) -> str:
    visible_turns: list[tuple[str, str]] = []
    lines = [
        "You are Circitron, the Circit AI coworker. Execute the conversation through Circit AgentCore.",
        "Return the direct assistant answer for the user.",
        "",
        "Conversation:",
    ]
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip() or "user"
        if role not in {"user", "assistant"}:
            continue
        content = _content_to_text(message.get("content")).strip()
        if not content:
            continue
        if role == "assistant" and _looks_like_tool_call_text(content):
            continue
        visible_turns.append((role, content))
    last_user_index = next(
        (idx for idx in range(len(visible_turns) - 1, -1, -1) if visible_turns[idx][0] == "user"),
        -1,
    )
    if last_user_index >= 0:
        visible_turns = visible_turns[max(0, last_user_index - 4):last_user_index + 1]
    else:
        visible_turns = visible_turns[-5:]
    for role, content in visible_turns:
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines).strip()


def _messages_to_direct_chat_messages(messages: list[dict]) -> list[dict]:
    visible_turns: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are Circitron, the Circit AI coworker. Answer the user's "
                "latest message directly and conversationally. Do not invent "
                "tool calls, task plans, or JSON function-call payloads."
            ),
        }
    ]
    chat_turns: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower() or "user"
        if role not in {"user", "assistant"}:
            continue
        content = _content_to_text(message.get("content")).strip()
        if content and not (role == "assistant" and _looks_like_tool_call_text(content)):
            chat_turns.append((role, content))

    last_user_index = next(
        (idx for idx in range(len(chat_turns) - 1, -1, -1) if chat_turns[idx][0] == "user"),
        -1,
    )
    if last_user_index >= 0:
        chat_turns = chat_turns[max(0, last_user_index - 4):last_user_index + 1]
    else:
        chat_turns = chat_turns[-5:]

    visible_turns.extend({"role": role, "content": content} for role, content in chat_turns)
    return visible_turns


def _chat_content_from_agentcore_payload(payload: dict) -> str:
    if isinstance(payload.get("content"), str):
        return payload["content"]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(first.get("text"), str):
                return first["text"]
    return ""


def _parse_agentcore_event(event_name: str, data: str) -> tuple[str, dict]:
    try:
        payload = json.loads(data)
    except Exception:
        payload = {"summary": data}
    if not isinstance(payload, dict):
        payload = {"summary": str(payload)}
    event_type = str(event_name or "").strip().lower()
    raw_type = payload.get("type")
    if event_type:
        return event_type, payload
    if raw_type == 0:
        return "textdelta", payload
    if raw_type == 10:
        return "taskcompleted", payload
    if raw_type in (11, 13):
        return "error", payload
    return str(raw_type or "").strip().lower(), payload


async def _iter_sse_events(resp: httpx.Response):
    event_name = ""
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _openai_chunk(chat_id: str, model: str, content: str = "", finish_reason=None) -> str:
    choice = {
        "index": 0,
        "delta": {"content": content} if content else {},
        "finish_reason": finish_reason,
    }
    payload = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _agentcore_stream_error_message(exc: Exception) -> str:
    return f"AgentCore stream failed: {exc}"


def setup_agentcore_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agentcore", tags=["agentcore"])

    @router.get("/status")
    async def status(request: Request):
        try:
            config = get_agentcore_config()
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={"configured": False, "connected": False, "error": str(exc)},
            )

        try:
            resp = await agentcore_request(
                method="GET",
                path="health",
                request=request,
            )
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "configured": True,
                    "connected": False,
                    "base_url": config.base_url,
                    "error": str(exc),
                },
            )

        return {
            "configured": True,
            "connected": resp.status_code == 200,
            "status_code": resp.status_code,
            "base_url": config.base_url,
        }

    @router.get("/openai/v1/models")
    async def openai_models(request: Request):
        try:
            resp = await agentcore_request(method="GET", path="config", request=request)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"AgentCore config unavailable: {exc}") from exc
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
        model = _model_name_from_config(resp.json())
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "circit-agentcore",
                }
            ],
        }

    @router.post("/openai/v1/chat")
    @router.post("/openai/v1/chat/completions")
    async def openai_chat_completions(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")

        model = agentcore_public_model_alias()
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="messages must be a non-empty list")
        task_description = _messages_to_task_description(messages)
        if not task_description:
            raise HTTPException(status_code=400, detail="messages did not contain text content")

        direct_messages = _messages_to_direct_chat_messages(messages)
        direct_chat_body = json.dumps(
            {
                "messages": direct_messages,
                "temperature": body.get("temperature"),
                "maxTokens": _direct_chat_max_tokens(body.get("max_tokens") or body.get("maxTokens")),
            }
        ).encode("utf-8")
        chat_id = f"chatcmpl-{uuid.uuid4().hex}"
        try:
            direct_resp = await agentcore_request(
                method="POST",
                path="chat/completions",
                request=request,
                body=direct_chat_body,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception:
            direct_resp = None

        direct_content = ""
        if direct_resp is not None and direct_resp.status_code < 400:
            direct_payload = direct_resp.json()
            direct_content = _sanitize_agentcore_direct_content(
                _chat_content_from_agentcore_payload(direct_payload).strip(),
                messages,
            )
            if not direct_content:
                retry_messages = list(direct_messages)
                retry_instruction = {
                    "role": "system",
                    "content": (
                        "The previous response was empty. Answer the latest "
                        "user message now with visible plain text. Do not "
                        "output tool-call JSON."
                    ),
                }
                retry_messages.insert(1 if retry_messages else 0, retry_instruction)
                retry_body = json.dumps(
                    {
                        "messages": retry_messages,
                        "temperature": 0.2,
                        "maxTokens": 512,
                    }
                ).encode("utf-8")
                try:
                    retry_resp = await agentcore_request(
                        method="POST",
                        path="chat/completions",
                        request=request,
                        body=retry_body,
                    )
                except Exception:
                    retry_resp = None
                if retry_resp is not None and retry_resp.status_code < 400:
                    retry_payload = retry_resp.json()
                    direct_content = _sanitize_agentcore_direct_content(
                        _chat_content_from_agentcore_payload(retry_payload).strip(),
                        messages,
                    )
            if not direct_content:
                direct_resp = None

        if direct_resp is not None and direct_resp.status_code < 400:
            if body.get("stream") is True:
                async def _stream_direct_openai():
                    yield _openai_chunk(chat_id, model)
                    if direct_content:
                        yield _openai_chunk(chat_id, model, direct_content)
                    yield _openai_chunk(chat_id, model, finish_reason="stop")
                    yield "data: [DONE]\n\n"

                return StreamingResponse(_stream_direct_openai(), media_type="text/event-stream")

            return {
                "id": chat_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": direct_content},
                        "finish_reason": "stop",
                    }
                ],
            }

        if direct_resp is not None and direct_resp.status_code not in (404, 405):
            raise HTTPException(status_code=direct_resp.status_code, detail=direct_resp.text[:1000])

        task_body = json.dumps({"taskDescription": task_description}).encode("utf-8")
        try:
            create_resp = await agentcore_request(
                method="POST",
                path="tasks",
                request=request,
                body=task_body,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"AgentCore task creation failed: {exc}") from exc
        if create_resp.status_code >= 400:
            raise HTTPException(status_code=create_resp.status_code, detail=create_resp.text[:1000])
        create_payload = create_resp.json()
        task_id = str(create_payload.get("taskId") or "").strip()
        if not task_id:
            raise HTTPException(status_code=502, detail="AgentCore did not return a taskId")

        config = get_agentcore_config()
        stream_url, headers = build_agentcore_request(config, f"tasks/{task_id}/stream", request)
        chat_id = f"chatcmpl-{uuid.uuid4().hex}"

        async def _stream_openai():
            yield _openai_chunk(chat_id, model)
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", stream_url, headers=headers) as resp:
                        if resp.status_code >= 400:
                            raw = (await resp.aread()).decode(errors="replace")
                            yield _openai_chunk(chat_id, model, f"AgentCore stream failed: {raw[:500]}")
                            yield _openai_chunk(chat_id, model, finish_reason="stop")
                            yield "data: [DONE]\n\n"
                            return
                        async for event_name, data in _iter_sse_events(resp):
                            event_type, payload = _parse_agentcore_event(event_name, data)
                            summary = str(payload.get("summary") or "")
                            if event_type == "textdelta" and summary:
                                yield _openai_chunk(chat_id, model, summary)
                            elif event_type == "error" and summary:
                                yield _openai_chunk(chat_id, model, f"\n\nAgentCore error: {summary}")
            except httpx.HTTPError as exc:
                yield _openai_chunk(chat_id, model, _agentcore_stream_error_message(exc))
            yield _openai_chunk(chat_id, model, finish_reason="stop")
            yield "data: [DONE]\n\n"

        if body.get("stream") is True:
            return StreamingResponse(_stream_openai(), media_type="text/event-stream")

        collected: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", stream_url, headers=headers) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode(errors="replace")
                        raise HTTPException(status_code=resp.status_code, detail=raw[:1000])
                    async for event_name, data in _iter_sse_events(resp):
                        event_type, payload = _parse_agentcore_event(event_name, data)
                        summary = str(payload.get("summary") or "")
                        if event_type == "textdelta" and summary:
                            collected.append(summary)
                        elif event_type == "error" and summary:
                            collected.append(f"\n\nAgentCore error: {summary}")
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=_agentcore_stream_error_message(exc)) from exc

        content = "".join(collected).strip()
        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }

    @router.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def proxy(path: str, request: Request):
        root = (path or "").split("/", 1)[0]
        if root not in ALLOWED_AGENTCORE_ROOTS:
            raise HTTPException(status_code=404, detail="AgentCore route is not proxied")

        body = await request.body()
        try:
            config = get_agentcore_config()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if path.endswith("/stream"):
            url, headers = build_agentcore_request(config, path, request, query=request.url.query)
            client = httpx.AsyncClient(timeout=None)
            outbound = client.build_request(
                request.method.upper(),
                url,
                content=body if body else None,
                headers=headers,
            )
            resp = await client.send(outbound, stream=True)

            async def _close_stream() -> None:
                await resp.aclose()
                await client.aclose()

            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
                background=BackgroundTask(_close_stream),
            )

        resp = await agentcore_request(
            method=request.method,
            path=path,
            request=request,
            body=body,
            query=request.url.query,
        )

        passthrough_headers = {}
        content_type = resp.headers.get("content-type")
        if content_type:
            passthrough_headers["content-type"] = content_type

        if content_type and content_type.startswith("text/event-stream"):
            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=resp.status_code,
                media_type=content_type,
                headers=passthrough_headers,
            )

        clean_path = (path or "").strip("/")
        if clean_path == "config" and resp.status_code < 400:
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            return JSONResponse(
                content=_redacted_agentcore_config(payload if isinstance(payload, dict) else {}),
                status_code=resp.status_code,
                headers=passthrough_headers,
            )

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=passthrough_headers,
        )

    return router
