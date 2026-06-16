"""Normalize Odysseus's native SSE frames + ui_event control verbs into the
canonical agent-event v1 schema (plan v3.3 §1.4).

Canonical schema source-of-truth: circit-cowork /contracts/agent-event.v1.schema.json
(vendored). Odysseus is one of two producers normalized into it; the .NET
AgentCore Host is the other.

Per native type the decision is PASS-THROUGH (map to a kind), NORMALIZE (rename
+ reshape), or DROP (internal-only frames the SPA must not see). Returns ``None``
for DROP. correlationId = the Odysseus sessionId, linking a run to a cowork task
for a unified transcript + audit.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

SCHEMA_VERSION = "1"
SOURCE = "odysseus"

# --- native SSE "type" -> canonical kind (or None to DROP) ------------------
# Verified against grep of "type": "..." emitters in src/agent_loop.py,
# src/agent_runs.py, routes/ (28 distinct native types observed).
_SSE_TYPE_MAP: dict[str, Optional[str]] = {
    "text": "text.delta",
    "say": "text.delta",
    "agent_step": "step.started",
    "tool_start": "tool.call",
    "tool_progress": "tool.call",
    "tool_output": "tool.result",
    "function": "tool.call",
    "object": "tool.result",
    "serve": "tool.result",
    "image": "tool.result",
    "image_url": "tool.result",
    "web_sources": "web.sources",
    "doc_stream_open": "document.update",
    "doc_stream_delta": "document.delta",
    "doc_update": "document.update",
    "doc_suggestions": "document.update",
    "model_info": "model.info",
    "metrics": "metrics",
    "message_saved": "step.completed",
    "evaluating": "step.started",
    "skill_test_start": "step.started",
    "fallback": "step.started",
    "rounds_exhausted": "task.failed",
    "budget_exceeded": "task.failed",
    "error": "error",
    "ui_control": "ui.control",
    # Internal-only frames the SPA must not render -> DROP.
    "ping": None,
    "heartbeat": None,
}

# --- ui_event control verbs -> canonical ui.control sub-action --------------
# Verified against grep of '"ui_event": "..."' in src/, routes/.
_UI_EVENT_VERBS: frozenset[str] = frozenset(
    {
        "toggle",
        "set_mode",
        "switch_model",
        "set_theme",
        "create_theme",
        "highlight",
        "clear_highlight",
        "open_panel",
        "open_email_reply",
        "research_started",
    }
)


def map_kind(native_type: str) -> Optional[str]:
    """Return the canonical kind for a native SSE type, or None to DROP.

    Unknown types are conservatively passed through as ``error`` so the SPA can
    surface a protocol-mismatch rather than silently swallowing a frame the
    backend believed was meaningful. (Unknown != heartbeat; heartbeats are
    explicitly mapped to DROP above.)
    """
    if native_type in _SSE_TYPE_MAP:
        return _SSE_TYPE_MAP[native_type]
    return "error"


def is_ui_event_verb(verb: str) -> bool:
    return verb in _UI_EVENT_VERBS


def normalize(
    frame: dict[str, Any],
    *,
    correlation_id: str,
    sequence: Optional[int] = None,
    actor: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Normalize one native SSE frame into the canonical event dict.

    Returns None when the frame maps to DROP. A frame carrying a ui_event verb
    is normalized to kind ``ui.control`` with the verb preserved under
    ``data.uiEvent`` (the 10 control verbs the SPA acts on).
    """
    native_type = str(frame.get("type", ""))

    # ui_event frames ride on tool_output today; classify by the verb.
    ui_event = frame.get("ui_event")
    if ui_event is not None:
        kind = "ui.control"
    else:
        kind = map_kind(native_type)

    if kind is None:
        return None

    summary = str(frame.get("summary") or frame.get("text") or native_type or "")

    # Carry the residual native payload opaquely; tool.result / subagent shapes
    # finalize in P4.1.
    data: dict[str, Any] = {
        k: v for k, v in frame.items() if k not in ("type", "summary")
    }
    if ui_event is not None:
        data["uiEvent"] = ui_event

    event: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": kind,
        "id": str(frame.get("id") or uuid.uuid4().hex),
        "correlationId": correlation_id,
        "source": SOURCE,
        "summary": summary,
    }
    if sequence is not None:
        event["sequence"] = sequence
    if actor is not None:
        event["actor"] = actor
    if data:
        event["data"] = data
    return event
