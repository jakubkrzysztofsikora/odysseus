"""Tests for the Odysseus->canonical agent-event v1 normalizer (P1.4)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.canonical_event import (  # noqa: E402
    normalize,
    map_kind,
    is_ui_event_verb,
    SCHEMA_VERSION,
    SOURCE,
    _SSE_TYPE_MAP,
    _UI_EVENT_VERBS,
)


def test_text_frame_normalizes_to_text_delta():
    ev = normalize({"type": "text", "text": "hi"}, correlation_id="sess-1")
    assert ev["kind"] == "text.delta"
    assert ev["schemaVersion"] == SCHEMA_VERSION
    assert ev["source"] == SOURCE
    assert ev["correlationId"] == "sess-1"


def test_heartbeat_is_dropped():
    assert normalize({"type": "heartbeat"}, correlation_id="sess-1") is None
    assert normalize({"type": "ping"}, correlation_id="sess-1") is None


def test_unknown_type_surfaces_as_error_not_silent_drop():
    ev = normalize({"type": "totally_new_frame"}, correlation_id="s")
    assert ev is not None
    assert ev["kind"] == "error"


def test_all_ten_ui_event_verbs_recognized():
    # Plan: 10 ui_event control verbs.
    assert len(_UI_EVENT_VERBS) == 10
    for verb in _UI_EVENT_VERBS:
        assert is_ui_event_verb(verb)


def test_ui_event_frame_maps_to_ui_control_with_verb_preserved():
    ev = normalize(
        {"type": "tool_output", "ui_event": "set_theme", "theme": "dark"},
        correlation_id="sess-1",
    )
    assert ev["kind"] == "ui.control"
    assert ev["data"]["uiEvent"] == "set_theme"
    assert ev["data"]["theme"] == "dark"


def test_actor_and_sequence_carried():
    ev = normalize(
        {"type": "tool_start", "id": "t1"},
        correlation_id="sess-1",
        sequence=4,
        actor={"oid": "oid-A", "user": "alice@circit.ie", "team": "compliance"},
    )
    assert ev["sequence"] == 4
    assert ev["actor"]["oid"] == "oid-A"
    assert ev["id"] == "t1"


def test_id_minted_when_absent():
    ev = normalize({"type": "text"}, correlation_id="sess-1")
    assert ev["id"]


def test_sse_map_has_no_silent_unmapped_known_types():
    # Every key resolves through map_kind without raising.
    for native in _SSE_TYPE_MAP:
        kind = map_kind(native)
        assert kind is None or isinstance(kind, str)


def test_at_least_25_native_sse_types_classified():
    # Plan says ~28 SSE types. Assert we explicitly classified a substantial set.
    assert len(_SSE_TYPE_MAP) >= 25
