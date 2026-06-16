import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.agentcore_routes import (  # noqa: E402
    _agentcore_stream_error_message,
    _chat_content_from_agentcore_payload,
    _direct_chat_max_tokens,
    _looks_like_tool_call_text,
    _messages_to_direct_chat_messages,
    _messages_to_task_description,
    _model_name_from_config,
    _parse_agentcore_event,
    _redacted_agentcore_config,
    _sanitize_agentcore_direct_content,
    setup_agentcore_routes,
)
from src.endpoint_resolver import build_headers, is_agentcore_openai_base  # noqa: E402


def test_messages_to_task_description_preserves_visible_chat_roles():
    prompt = _messages_to_task_description([
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "What models are connected?"},
    ])

    assert "SYSTEM: Be concise." not in prompt
    assert "USER: What models are connected?" in prompt
    assert "Circitron" in prompt


def test_messages_to_task_description_does_not_forward_agent_prompt_or_tools():
    prompt = _messages_to_task_description([
        {"role": "system", "content": "You can draft email and call tools."},
        {"role": "assistant", "content": "Previous visible answer."},
        {"role": "tool", "content": "secret tool result"},
        {"role": "user", "content": "hi"},
    ])

    assert "USER: hi" in prompt
    assert "ASSISTANT: Previous visible answer." in prompt
    assert "draft email" not in prompt
    assert "secret tool result" not in prompt


def test_direct_chat_messages_use_clean_system_prompt_and_visible_turns():
    messages = _messages_to_direct_chat_messages([
        {"role": "system", "content": "You may call SystemInfo-get_system_config."},
        {"role": "assistant", "content": "Previous answer."},
        {"role": "tool", "content": "secret tool result"},
        {"role": "user", "content": "hi"},
    ])

    serialized = json.dumps(messages)
    assert messages[0]["role"] == "system"
    assert "directly and conversationally" in messages[0]["content"]
    assert {"role": "assistant", "content": "Previous answer."} in messages
    assert {"role": "user", "content": "hi"} in messages
    assert "SystemInfo-get_system_config" not in serialized
    assert "secret tool result" not in serialized


def test_direct_chat_messages_drop_prior_tool_json_leaks():
    messages = _messages_to_direct_chat_messages([
        {"role": "assistant", "content": '{"name":"manage_skills","arguments":{"action":"view","name":"ops"}}'},
        {"role": "user", "content": "hi"},
    ])

    serialized = json.dumps(messages)
    assert "manage_skills" not in serialized
    assert {"role": "user", "content": "hi"} in messages


def test_agentcore_tool_json_leak_is_sanitized_for_greeting():
    leaked = '{"name":"Context7-resolve-library-id","arguments":{"query":"Hello"}}'

    assert _looks_like_tool_call_text(leaked)
    assert _sanitize_agentcore_direct_content(leaked, [{"role": "user", "content": "hi"}]) == "Hi. How can I help?"


def test_agentcore_direct_chat_uses_visible_output_budget():
    assert _direct_chat_max_tokens(None) == 512
    assert _direct_chat_max_tokens(32) == 512
    assert _direct_chat_max_tokens(1024) == 1024


def test_chat_content_from_agentcore_payload_accepts_openai_shape():
    content = _chat_content_from_agentcore_payload({
        "choices": [{"message": {"role": "assistant", "content": "Hello"}}]
    })

    assert content == "Hello"


def test_agentcore_textdelta_event_maps_from_wire_shape():
    event_type, payload = _parse_agentcore_event("", '{"type":0,"summary":"hello"}')

    assert event_type == "textdelta"
    assert payload["summary"] == "hello"


def test_agentcore_model_list_uses_public_alias(monkeypatch):
    monkeypatch.delenv("AGENTCORE_PUBLIC_MODEL_ALIAS", raising=False)

    model = _model_name_from_config({"modelName": "bartowski/Qwen2.5-7B-Instruct-GGUF"})

    assert model == "circitron"


def test_agentcore_config_redaction_hides_hosted_model(monkeypatch):
    monkeypatch.delenv("AGENTCORE_PUBLIC_MODEL_ALIAS", raising=False)

    redacted = _redacted_agentcore_config({
        "modelName": "bartowski/Qwen2.5-7B-Instruct-GGUF",
        "deploymentName": "gpt-4o",
        "serverUrl": "https://cee-llama-server.example/v1",
        "temperature": 0.7,
    })

    serialized = json.dumps(redacted)
    assert redacted["modelName"] == "circitron"
    assert redacted["deploymentName"] == "circitron"
    assert "serverUrl" not in redacted
    assert "bartowski" not in serialized
    assert "Qwen" not in serialized


def test_agentcore_endpoint_headers_carry_circit_user():
    base = "http://127.0.0.1:7000/api/agentcore/openai/v1"

    assert is_agentcore_openai_base(base)
    assert build_headers(None, base, owner="Jakub.Sikora@Circit.IO") == {
        "X-Circit-User": "jakub.sikora@circit.io",
    }


def test_agentcore_stream_error_message_is_actionable():
    message = _agentcore_stream_error_message(RuntimeError("upstream closed"))

    assert message == "AgentCore stream failed: upstream closed"


def test_agentcore_openai_short_chat_alias_is_registered():
    paths = {route.path for route in setup_agentcore_routes().routes}

    assert "/api/agentcore/openai/v1/chat" in paths
    assert "/api/agentcore/openai/v1/chat/completions" in paths
