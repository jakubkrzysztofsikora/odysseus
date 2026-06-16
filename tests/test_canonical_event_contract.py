"""Contract test (P7.2 slice): the Odysseus normalizer's output validates against
the vendored canonical agent-event v1 JSON Schema (contracts/agent-event.v1.schema.json).

This is the load-bearing cross-runtime guarantee — the SPA consumes ONE schema,
so both producers must emit conformant events. The .NET side has its own
shape test; this proves the Python producer.
"""

import os
import sys
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.canonical_event import normalize  # noqa: E402

jsonschema = pytest.importorskip("jsonschema")

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "contracts",
    "agent-event.v1.schema.json",
)


def _schema():
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# Representative native frames spanning text / tool / doc / ui.control / error /
# terminal kinds.
_FRAMES = [
    {"type": "text", "text": "hello"},
    {"type": "tool_start", "id": "t1", "name": "search"},
    {"type": "tool_output", "id": "t1", "result": {"k": "v"}},
    {"type": "doc_stream_delta", "delta": "abc"},
    {"type": "web_sources", "sources": ["https://x"]},
    {"type": "metrics", "tokens": 42},
    {"type": "rounds_exhausted"},
    {"type": "error", "message": "boom"},
    {"type": "tool_output", "ui_event": "set_theme", "theme": "dark"},
]


@pytest.mark.parametrize("frame", _FRAMES)
def test_normalized_frame_conforms_to_canonical_schema(frame):
    schema = _schema()
    event = normalize(
        frame,
        correlation_id="sess-1",
        sequence=1,
        actor={"oid": "oid-A", "user": "alice@circit.ie", "team": "compliance"},
    )
    assert event is not None
    jsonschema.validate(instance=event, schema=schema)


def test_dropped_frame_yields_nothing_to_validate():
    assert normalize({"type": "heartbeat"}, correlation_id="s") is None


def test_event_without_actor_still_conforms():
    schema = _schema()
    event = normalize({"type": "text", "text": "hi"}, correlation_id="sess-1")
    jsonschema.validate(instance=event, schema=schema)
