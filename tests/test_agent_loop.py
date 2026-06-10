"""Tests for agent_loop.py — _detect_admin_intent, _compute_final_metrics,
and _append_tool_results. Uses mock imports to avoid loading the full app stack."""

import sys
import collections
import json
from unittest.mock import MagicMock

_MOCKED_IMPORTS = [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database',
    'src.agent_tools',
    'core.models', 'core.database',
]
_INJECTED_IMPORT_STUBS = {}
_PREEXISTING_AGENT_LOOP = sys.modules.get("src.agent_loop")

from src.agent_loop import (
    _detect_admin_intent,
    _compute_final_metrics,
    _append_tool_results,
    _allow_native_tool_schemas_for_non_api_model,
    _include_mcp_schema_names,
    _mcp_prompt_requested,
    _mcp_prompt_cache_signature,
    _mcp_target_hints,
    _prompt_visible_mcp_tools,
    _build_mcp_target_message,
    _record_empty_argument_tool_call,
    _resolve_tool_blocks,
    _repair_empty_mcp_search_tool_blocks,
    _repair_empty_local_tool_blocks,
    _sync_repaired_native_tool_call_arguments,
    _record_repaired_empty_argument_calls,
    _tool_events_include_bash_command,
    ToolBlock,
)
from src.tool_parsing import parse_tool_blocks


# ---------------------------------------------------------------------------
# _detect_admin_intent
# ---------------------------------------------------------------------------

class TestDetectAdminIntent:
    """Test admin-intent detection from the last user message."""

    def _msgs(self, text: str):
        """Helper: wrap text in a minimal messages list."""
        return [{"role": "user", "content": text}]

    # --- Should detect admin intent ---

    def test_add_endpoint(self):
        assert _detect_admin_intent(self._msgs("add a new endpoint")) is True

    def test_create_endpoint(self):
        assert _detect_admin_intent(self._msgs("create endpoint for openai")) is True

    def test_manage_sessions(self):
        assert _detect_admin_intent(self._msgs("list all sessions")) is True

    def test_rename_session(self):
        assert _detect_admin_intent(self._msgs("rename this session")) is True

    def test_archive_session(self):
        assert _detect_admin_intent(self._msgs("archive old sessions")) is True

    def test_configure_settings(self):
        assert _detect_admin_intent(self._msgs("configure my settings")) is True

    def test_mcp_server(self):
        assert _detect_admin_intent(self._msgs("add an MCP server")) is True

    def test_api_key(self):
        assert _detect_admin_intent(self._msgs("update the API key")) is True

    def test_list_models(self):
        assert _detect_admin_intent(self._msgs("list models available")) is True

    def test_switch_model(self):
        assert _detect_admin_intent(self._msgs("switch model to gpt-4")) is True

    def test_manage_skills(self):
        assert _detect_admin_intent(self._msgs("show me my skills")) is True

    def test_schedule_task(self):
        assert _detect_admin_intent(self._msgs("schedule a cron task")) is True

    def test_case_insensitive(self):
        assert _detect_admin_intent(self._msgs("MANAGE SESSIONS")) is True

    # --- Should NOT detect admin intent ---

    def test_hello(self):
        assert _detect_admin_intent(self._msgs("hello")) is False

    def test_write_code(self):
        assert _detect_admin_intent(self._msgs("write some python code")) is False

    def test_explain_concept(self):
        assert _detect_admin_intent(self._msgs("explain how transformers work")) is False

    def test_general_question(self):
        assert _detect_admin_intent(self._msgs("what is the capital of France?")) is False

    # --- Edge cases ---

    def test_empty_messages(self):
        assert _detect_admin_intent([]) is False

    def test_no_user_message(self):
        assert _detect_admin_intent([{"role": "assistant", "content": "hi"}]) is False

    def test_multimodal_content(self):
        """Content as a list of blocks (vision messages)."""
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "rename this session please"},
        ]}]
        assert _detect_admin_intent(msgs) is True

    def test_multimodal_no_admin(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "describe this image"},
        ]}]
        assert _detect_admin_intent(msgs) is False

    def test_uses_last_user_message(self):
        """Should check only the last user message."""
        msgs = [
            {"role": "user", "content": "rename this session"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "thanks, now just say hello"},
        ]
        assert _detect_admin_intent(msgs) is False


# ---------------------------------------------------------------------------
# MCP tool visibility
# ---------------------------------------------------------------------------


class TestMcpToolVisibility:
    def _schemas(self):
        return [
            {"type": "function", "function": {"name": "mcp__atlassian__search"}},
        ]

    def test_multiagent_force_includes_mcp_schema_names(self):
        schemas = [
            {"type": "function", "function": {"name": "mcp__atlassian__search"}},
            {"type": "function", "function": {"name": "mcp__browser__navigate"}},
        ]

        tools = _include_mcp_schema_names(
            {"api_call"},
            schemas,
            force_all_mcp_tools=True,
        )

        assert tools == {
            "api_call",
            "mcp__atlassian__search",
            "mcp__browser__navigate",
        }

    def test_multiagent_force_targets_named_mcp_schemas_when_query_matches(self):
        schemas = [
            {
                "type": "function",
                "function": {
                    "name": "mcp__0ac61a6b__search",
                    "description": "[MCP:Atlassian] Search Atlassian",
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mcp__builtin_browser__navigate",
                    "description": "[MCP:Browser] Navigate pages",
                },
            },
        ]

        tools = _include_mcp_schema_names(
            {"bash"},
            schemas,
            force_all_mcp_tools=True,
            query="Sequential group handoff. Use Atlassian MCP to search Circit AI context.",
        )

        assert tools == {"bash", "mcp__0ac61a6b__search"}

    def test_normal_agent_keeps_rag_selected_tools_compact(self):
        schemas = [
            {"type": "function", "function": {"name": "mcp__atlassian__search"}},
        ]

        tools = _include_mcp_schema_names(
            {"api_call"},
            schemas,
            force_all_mcp_tools=False,
        )

        assert tools == {"api_call"}

    def test_chatgpt_route_can_get_native_mcp_schemas_when_mcp_requested(self):
        assert _allow_native_tool_schemas_for_non_api_model(
            "chatgpt/gpt-5.5",
            None,
            self._schemas(),
            "use atlassian mcp to search Circit context",
        ) is True

    def test_chatgpt_route_can_opt_into_native_mcp_schemas(self):
        assert _allow_native_tool_schemas_for_non_api_model(
            "chatgpt/gpt-5.5",
            True,
            self._schemas(),
            "use atlassian mcp to search Circit context",
        ) is True

    def test_non_api_model_still_gets_mcp_schemas_when_prompt_requests_mcp(self):
        assert _allow_native_tool_schemas_for_non_api_model(
            "mistral",
            None,
            self._schemas(),
            "use atlassian mcp to search Circit context",
        ) is True

    def test_non_api_model_skips_mcp_schemas_without_mcp_intent(self):
        assert _allow_native_tool_schemas_for_non_api_model(
            "mistral",
            None,
            self._schemas(),
            "write a short poem",
        ) is False

    def test_multiagent_force_keeps_native_mcp_schemas_without_latest_mcp_keyword(self):
        assert _allow_native_tool_schemas_for_non_api_model(
            "chatgpt/gpt-5.5",
            None,
            self._schemas(),
            "Sequential group handoff. Previous participant output: build the plan",
            force_all_mcp_tools=True,
        ) is True

    def test_mcp_prompt_wait_detects_targeted_mcp_requests(self):
        assert _mcp_prompt_requested("Use Atlassian MCP to search Jira", False) is True
        assert _mcp_target_hints("Use Atlassian MCP to search Jira") == {
            "atlassian",
            "0ac61a6b",
        }
        assert _mcp_prompt_requested("write a short poem", False) is False
        assert _mcp_prompt_requested("write a short poem", True) is True

    def test_prompt_visible_mcp_tools_skips_builtin_python_servers(self):
        class DummyMcp:
            def get_all_tools(self, _disabled):
                return [
                    {"server_id": "rag", "server_name": "RAG", "qualified_name": "mcp__rag__search"},
                    {"server_id": "builtin_browser", "server_name": "Browser", "qualified_name": "mcp__builtin_browser__navigate"},
                    {"server_id": "0ac61a6b", "server_name": "Atlassian", "qualified_name": "mcp__0ac61a6b__search"},
                ]

            def is_builtin(self, server_id):
                return server_id in {"rag", "builtin_browser"}

        visible = _prompt_visible_mcp_tools(DummyMcp(), {})

        assert [t["qualified_name"] for t in visible] == [
            "mcp__builtin_browser__navigate",
            "mcp__0ac61a6b__search",
        ]

    def test_mcp_prompt_cache_signature_tracks_generation_and_visible_tools(self):
        class DummyMcp:
            _generation = 1

            def __init__(self, tools):
                self._tools = tools

            def get_all_tools(self, _disabled):
                return self._tools

            def is_builtin(self, server_id):
                return server_id in {"rag", "builtin_browser"}

        first = DummyMcp([
            {"server_id": "0ac61a6b", "server_name": "Atlassian", "qualified_name": "mcp__0ac61a6b__search", "name": "search"},
        ])
        second = DummyMcp([
            {"server_id": "0ac61a6b", "server_name": "Atlassian", "qualified_name": "mcp__0ac61a6b__search", "name": "search"},
        ])
        second._generation = 2
        third = DummyMcp([
            {"server_id": "0ac61a6b", "server_name": "Atlassian", "qualified_name": "mcp__0ac61a6b__search", "name": "search"},
            {"server_id": "circitron", "server_name": "Circitron", "qualified_name": "mcp__circitron__ping", "name": "ping"},
        ])

        assert _mcp_prompt_cache_signature(first, {}) != _mcp_prompt_cache_signature(second, {})
        assert _mcp_prompt_cache_signature(first, {}) != _mcp_prompt_cache_signature(third, {})

    def test_mcp_target_message_surfaces_exact_callable_names(self):
        class DummyMcp:
            def get_all_tools(self, _disabled):
                return [
                    {
                        "server_id": "0ac61a6b",
                        "server_name": "Atlassian",
                        "qualified_name": "mcp__0ac61a6b__search",
                        "name": "search",
                        "description": "Search Atlassian",
                        "input_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                    {
                        "server_id": "circitron",
                        "server_name": "Circitron",
                        "qualified_name": "mcp__circitron__ping",
                        "name": "ping",
                        "description": "Ping",
                        "input_schema": {},
                    },
                ]

            def is_builtin(self, server_id):
                return False

        msg = _build_mcp_target_message(
            DummyMcp(),
            {},
            "Use Atlassian MCP to search Circit AI",
            force_all_mcp_tools=False,
        )

        assert msg["role"] == "system"
        assert "do not say it is unavailable" in msg["content"]
        assert "Never send `{}`" in msg["content"]
        assert (
            'mcp__0ac61a6b__search{"query":"Circit AI"}'
            in msg["content"]
        )
        assert "mcp__circitron__ping" not in msg["content"]


# ---------------------------------------------------------------------------
# Empty-argument tool quarantine
# ---------------------------------------------------------------------------


class TestEmptyArgumentToolQuarantine:
    def test_second_empty_arg_error_disables_tool(self):
        disabled = set()
        counts = collections.Counter()
        result = {
            "exit_code": 2,
            "error": "Tool 'bash' was called with empty arguments. Retry.",
        }

        first = _record_empty_argument_tool_call("bash", result, disabled, counts)
        second = _record_empty_argument_tool_call("bash", result, disabled, counts)
        third = _record_empty_argument_tool_call("bash", result, disabled, counts)

        assert "not a real external blocker yet" in first
        assert "not a real external blocker yet" in second
        assert "bash" in disabled
        assert "disabled for the rest of this turn" in third

    def test_missing_required_mcp_args_get_retry_note(self):
        disabled = set()
        counts = collections.Counter()
        result = {
            "exit_code": 2,
            "error": "Tool 'mcp__atlassian__search' was called with missing required arguments",
            "missing_required": ["query"],
        }

        note = _record_empty_argument_tool_call(
            "mcp__atlassian__search",
            result,
            disabled,
            counts,
        )

        assert "missing query" in note
        assert "not a real external blocker yet" in note
        assert disabled == set()

    def test_non_empty_arg_error_is_ignored(self):
        disabled = set()
        counts = collections.Counter()
        result = {"exit_code": 1, "error": "command failed"}

        note = _record_empty_argument_tool_call("bash", result, disabled, counts)

        assert note is None
        assert disabled == set()


class TestMcpSearchArgumentRepair:
    class DummyMcp:
        def missing_required_arguments(self, qualified_name, args):
            if qualified_name == "mcp__atlassian__search" and not str(args.get("query") or "").strip():
                return ["query"]
            if qualified_name == "mcp__atlassian__createIssue" and not args.get("summary"):
                return ["summary"]
            return []

    def test_empty_mcp_search_args_are_filled_from_latest_user_instruction(self):
        messages = [
            {"role": "user", "content": "Use Atlassian MCP to search Circit AI internal infrastructure"},
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": "[Tool execution results]\n\nprevious output"},
        ]
        blocks = [ToolBlock("mcp__atlassian__search", "{}")]

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, self.DummyMcp(), messages)

        assert repaired[0].tool_type == "mcp__atlassian__search"
        assert json.loads(repaired[0].content) == {
            "query": "Use Atlassian MCP to search Circit AI internal infrastructure"
        }

    def test_blank_mcp_search_args_are_repaired(self):
        messages = [{"role": "user", "content": "Find Circit Copilot usage patterns"}]
        blocks = [ToolBlock("mcp__atlassian__search", '{"query":"   "}')]

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, self.DummyMcp(), messages)

        assert json.loads(repaired[0].content) == {
            "query": "Find Circit Copilot usage patterns"
        }

    def test_empty_mcp_search_args_are_repaired_without_live_schema(self):
        messages = [{"role": "user", "content": "Search Atlassian for Circit support AI usage"}]
        blocks = [ToolBlock("mcp__0ac61a6b__search", "{}")]

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, None, messages)

        assert json.loads(repaired[0].content) == {
            "query": "Search Atlassian for Circit support AI usage"
        }

    def test_blank_mcp_search_query_is_repaired_without_live_schema(self):
        messages = [{"role": "user", "content": "Find Circit compliance AI roadmap"}]
        blocks = [ToolBlock("mcp__0ac61a6b__search", '{"query":""}')]

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, None, messages)

        assert json.loads(repaired[0].content) == {
            "query": "Find Circit compliance AI roadmap"
        }

    def test_parenthesized_empty_mcp_search_is_parsed_and_repaired(self):
        messages = [{"role": "user", "content": "Use Atlassian MCP to fetch Circit AI business context"}]
        blocks = parse_tool_blocks("mcp__0ac61a6b__search()")

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, None, messages)

        assert json.loads(repaired[0].content) == {
            "query": "Use Atlassian MCP to fetch Circit AI business context"
        }

    def test_native_empty_mcp_search_args_are_repaired_before_dispatch(self):
        messages = [{"role": "user", "content": "Use Atlassian MCP to search Circit AI usage patterns"}]
        native = [{"id": "call_1", "name": "mcp__0ac61a6b__search", "arguments": "{}"}]
        blocks, used_native = _resolve_tool_blocks("", native, 1)

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, None, messages)

        assert used_native is True
        assert json.loads(repaired[0].content) == {
            "query": "Use Atlassian MCP to search Circit AI usage patterns"
        }

    def test_empty_mcp_search_repair_uses_original_task_in_group_handoff(self):
        messages = [
            {
                "role": "user",
                "content": (
                    "Sequential group handoff.\n\n"
                    "Previous participant output:\n"
                    + ("long prior artifact line\n" * 500)
                    + "\nOriginal user task for context only:\n"
                    "Use Atlassian MCP to fetch Circit existing AI internal "
                    "infrastructure and business context.\n\n"
                    "Continue with your assigned role. If you need tools or MCP, "
                    "call them with explicit non-empty arguments."
                ),
            }
        ]
        blocks = [ToolBlock("mcp__0ac61a6b__search", "{}")]

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, None, messages)
        query = json.loads(repaired[0].content)["query"]

        assert query.startswith("Use Atlassian MCP to fetch Circit existing AI")
        assert "long prior artifact line" not in query

    def test_repaired_native_mcp_args_are_echoed_to_next_round(self):
        messages = [{"role": "user", "content": "Use Atlassian MCP to search Circit AI usage patterns"}]
        native = [{"id": "call_1", "name": "mcp__0ac61a6b__search", "arguments": "{}"}]
        blocks, used_native = _resolve_tool_blocks("", native, 1)
        repaired = _repair_empty_mcp_search_tool_blocks(blocks, None, messages)

        repaired_sigs = _sync_repaired_native_tool_call_arguments(native, repaired, used_native)

        assert json.loads(native[0]["arguments"]) == {
            "query": "Use Atlassian MCP to search Circit AI usage patterns"
        }
        assert repaired_sigs == [
            "mcp__0ac61a6b__search:{\"query\": \"Use Atlassian MCP to search Circit AI usage patterns\"}"
        ]

    def test_repaired_native_args_sync_skips_failed_native_conversions(self):
        messages = [
            {
                "role": "user",
                "content": "Run bash with command exactly: printf 'SMOKE native-bash-ok'.",
            }
        ]
        native = [
            {"id": "bad_1", "name": "not_a_tool", "arguments": "{}"},
            {"id": "call_1", "name": "bash", "arguments": "{}"},
        ]
        blocks, used_native = _resolve_tool_blocks("", native, 1)
        repaired = _repair_empty_local_tool_blocks(blocks, messages)

        repaired_sigs = _sync_repaired_native_tool_call_arguments(native, repaired, used_native)

        assert native[0]["arguments"] == "{}"
        assert json.loads(native[1]["arguments"]) == {
            "command": "printf 'SMOKE native-bash-ok'"
        }
        assert repaired_sigs == ["bash:printf 'SMOKE native-bash-ok'"]

    def test_non_search_mcp_tool_is_not_repaired_without_live_schema(self):
        messages = [{"role": "user", "content": "Create a Jira issue for the launch plan"}]
        blocks = [ToolBlock("mcp__atlassian__createIssue", "{}")]

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, None, messages)

        assert repaired == blocks

    def test_non_search_mcp_tools_keep_missing_argument_guard(self):
        messages = [{"role": "user", "content": "Create a Jira issue for the launch plan"}]
        blocks = [ToolBlock("mcp__atlassian__createIssue", "{}")]

        repaired = _repair_empty_mcp_search_tool_blocks(blocks, self.DummyMcp(), messages)

        assert repaired == blocks


class TestLocalToolArgumentRepair:
    def test_empty_bash_args_are_filled_from_explicit_latest_user_command(self):
        messages = [
            {
                "role": "user",
                "content": (
                    "First use MCP. After that call bash with command exactly: "
                    "printf 'SMOKE bash-ok'. Then answer."
                ),
            },
            {"role": "user", "content": "[Tool execution results]\n\nprevious output"},
        ]
        blocks = [ToolBlock("bash", "")]

        repaired = _repair_empty_local_tool_blocks(blocks, messages)

        assert repaired[0].tool_type == "bash"
        assert repaired[0].content == "printf 'SMOKE bash-ok'"

    def test_native_empty_bash_args_are_repaired_before_dispatch(self):
        messages = [
            {
                "role": "user",
                "content": "Run bash with command exactly: printf 'SMOKE native-bash-ok'.",
            }
        ]
        native = [{"id": "call_1", "name": "bash", "arguments": "{}"}]
        blocks, used_native = _resolve_tool_blocks("", native, 1)

        repaired = _repair_empty_local_tool_blocks(blocks, messages)

        assert used_native is True
        assert repaired[0].content == "printf 'SMOKE native-bash-ok'"

    def test_repaired_native_bash_args_are_echoed_to_next_round(self):
        messages = [
            {
                "role": "user",
                "content": "Run bash with command exactly: printf 'SMOKE native-bash-ok'.",
            }
        ]
        native = [{"id": "call_1", "name": "bash", "arguments": "{}"}]
        blocks, used_native = _resolve_tool_blocks("", native, 1)
        repaired = _repair_empty_local_tool_blocks(blocks, messages)

        repaired_sigs = _sync_repaired_native_tool_call_arguments(native, repaired, used_native)

        assert json.loads(native[0]["arguments"]) == {
            "command": "printf 'SMOKE native-bash-ok'"
        }
        assert repaired_sigs == ["bash:printf 'SMOKE native-bash-ok'"]

    def test_repeated_repaired_empty_arg_call_is_reported(self):
        counts = collections.Counter()
        first = _record_repaired_empty_argument_calls(["bash:printf ok"], counts)
        second = _record_repaired_empty_argument_calls(["bash:printf ok"], counts)

        assert first == []
        assert second == ["bash:printf ok"]

    def test_bash_command_event_match_detects_pending_exact_command(self):
        events = [
            {
                "tool": "bash",
                "command": "printf 'SMOKE native-bash-ok'",
                "output": "SMOKE native-bash-ok",
            }
        ]

        assert _tool_events_include_bash_command(
            events,
            "printf 'SMOKE native-bash-ok'",
        ) is True
        assert _tool_events_include_bash_command(events, "printf 'missing'") is False

    def test_empty_bash_repair_does_not_capture_followup_instruction(self):
        messages = [
            {
                "role": "user",
                "content": (
                    "Also run Bash with the exact command: "
                    "pwd && git status --short | head -20\n"
                    "After the tools return, summarize only the useful handoff."
                ),
            }
        ]
        blocks = [ToolBlock("bash", "")]

        repaired = _repair_empty_local_tool_blocks(blocks, messages)

        assert repaired[0].content == "pwd && git status --short | head -20"

    def test_empty_bash_repair_finds_backtick_command_without_colon(self):
        messages = [
            {
                "role": "user",
                "content": (
                    "Use Atlassian MCP search first. "
                    "Then use Bash exactly once with command "
                    "`printf SMOKE_ODYSSEUS_MCP_BASH_OK`. "
                    "After both tool outputs, write the final handoff."
                ),
            }
        ]
        blocks = [ToolBlock("bash", "")]

        repaired = _repair_empty_local_tool_blocks(blocks, messages)

        assert repaired[0].content == "printf SMOKE_ODYSSEUS_MCP_BASH_OK"

    def test_empty_bash_repair_finds_command_late_in_group_handoff(self):
        messages = [
            {
                "role": "user",
                "content": (
                    "Sequential group handoff.\n\n"
                    "Previous participant output:\n"
                    + ("long artifact line\n" * 500)
                    + "\nOriginal user task for context only:\n"
                    "Inspect the repository.\n"
                    "Continue with your assigned checker role and run Bash command: "
                    "git rev-parse --show-toplevel && rg -n \"multiagent|mcp\" "
                    "static/js/group.js src/agent_loop.py | head -20\n"
                    "After those two tools return, provide a concise checked handoff."
                ),
            }
        ]
        blocks = [ToolBlock("bash", "")]

        repaired = _repair_empty_local_tool_blocks(blocks, messages)

        assert repaired[0].content == (
            'git rev-parse --show-toplevel && rg -n "multiagent|mcp" '
            "static/js/group.js src/agent_loop.py | head -20"
        )

    def test_empty_bash_args_are_not_repaired_without_explicit_command(self):
        messages = [{"role": "user", "content": "Use bash if needed to inspect the repo"}]
        blocks = [ToolBlock("bash", "")]

        repaired = _repair_empty_local_tool_blocks(blocks, messages)

        assert repaired == blocks


# ---------------------------------------------------------------------------
# _compute_final_metrics
# ---------------------------------------------------------------------------

class TestComputeFinalMetrics:
    """Test metric computation with real and estimated usage."""

    def _base_args(self, **overrides):
        defaults = dict(
            messages=[{"role": "user", "content": "hello world"}],
            full_response="This is a test response.",
            total_duration=2.0,
            time_to_first_token=0.5,
            context_length=8192,
            real_input_tokens=100,
            real_output_tokens=50,
            has_real_usage=True,
            tool_events=[],
            round_texts=[],
            model="test-model",
            last_round_input_tokens=0,
            prep_timings=None,
        )
        defaults.update(overrides)
        return defaults

    def test_real_usage_tokens(self):
        m = _compute_final_metrics(**self._base_args())
        assert m["input_tokens"] == 100
        assert m["output_tokens"] == 50
        assert m["total_tokens"] == 150
        assert m["usage_source"] == "real"

    def test_estimated_usage_tokens(self):
        m = _compute_final_metrics(**self._base_args(
            has_real_usage=False,
            real_input_tokens=0,
            real_output_tokens=0,
        ))
        # Estimated: len("hello world\n") // 4 = 3
        assert m["input_tokens"] == 3
        assert m["usage_source"] == "estimated"

    def test_tps_calculation(self):
        m = _compute_final_metrics(**self._base_args(
            real_output_tokens=100,
            total_duration=2.0,
        ))
        assert m["tokens_per_second"] == 50.0

    def test_tps_zero_duration(self):
        m = _compute_final_metrics(**self._base_args(total_duration=0.0))
        assert m["tokens_per_second"] == 0

    def test_context_percent(self):
        m = _compute_final_metrics(**self._base_args(
            real_input_tokens=4096,
            context_length=8192,
        ))
        assert m["context_percent"] == 50.0

    def test_context_percent_capped_at_100(self):
        m = _compute_final_metrics(**self._base_args(
            real_input_tokens=10000,
            context_length=8192,
        ))
        assert m["context_percent"] == 100.0

    def test_context_percent_zero_context_length(self):
        m = _compute_final_metrics(**self._base_args(context_length=0))
        assert m["context_percent"] == 0

    def test_last_round_input_tokens_used_for_context_pct(self):
        """When last_round_input_tokens > 0, it should be used for context %."""
        m = _compute_final_metrics(**self._base_args(
            real_input_tokens=100,
            last_round_input_tokens=4096,
            context_length=8192,
        ))
        assert m["context_percent"] == 50.0

    def test_response_time(self):
        m = _compute_final_metrics(**self._base_args(total_duration=3.456))
        assert m["response_time"] == 3.46

    def test_time_to_first_token(self):
        m = _compute_final_metrics(**self._base_args(time_to_first_token=0.123))
        assert m["time_to_first_token"] == 0.12

    def test_time_to_first_token_none(self):
        m = _compute_final_metrics(**self._base_args(time_to_first_token=None))
        assert m["time_to_first_token"] == 0

    def test_model_returned(self):
        m = _compute_final_metrics(**self._base_args(model="gpt-4o"))
        assert m["model"] == "gpt-4o"

    def test_prep_timings_included(self):
        m = _compute_final_metrics(**self._base_args(
            time_to_first_token=1.25,
            prep_timings={"request_setup": 0.2, "tool_selection": 0.3, "prompt_build": 0.15},
        ))
        assert m["agent_prep_time"] == 0.65
        assert m["agent_model_wait_time"] == 0.6
        assert m["agent_prep_breakdown"] == {
            "request_setup": 0.2,
            "tool_selection": 0.3,
            "prompt_build": 0.15,
        }

    def test_tool_events_included(self):
        events = [{"tool": "bash", "duration": 1.0}]
        texts = ["round 1 text"]
        m = _compute_final_metrics(**self._base_args(
            tool_events=events,
            round_texts=texts,
        ))
        assert m["tool_events"] == events
        assert m["round_texts"] == texts

    def test_no_tool_events_excluded(self):
        m = _compute_final_metrics(**self._base_args(tool_events=[], round_texts=[]))
        assert "tool_events" not in m
        assert "round_texts" not in m


# ---------------------------------------------------------------------------
# _append_tool_results — native tool-call message shaping
# ---------------------------------------------------------------------------

class TestAppendToolResultsNativeContent:
    """After a native tool call with no prose, the assistant message's content
    must be JSON null (None), not an empty string. Google Gemini's
    OpenAI-compatible endpoint and Ollama both reject `tool_calls` + ""
    content with HTTP 400, which breaks every tool-using turn."""

    def _native(self):
        return [{"id": "call_abc", "name": "web_fetch", "arguments": '{"url": "https://example.com"}'}]

    def test_empty_text_yields_null_content(self):
        messages = []
        _append_tool_results(
            messages, "", self._native(), [{}], ["page text"],
            used_native=True, round_num=1,
        )
        assistant = messages[0]
        assert assistant["role"] == "assistant"
        assert assistant["content"] is None  # NOT ""
        assert assistant["tool_calls"][0]["id"] == "call_abc"
        assert assistant["tool_calls"][0]["type"] == "function"
        # tool result follows as a role:tool message keyed by tool_call_id
        assert messages[1]["role"] == "tool"
        assert messages[1]["tool_call_id"] == "call_abc"
        assert messages[1]["content"] == "page text"

    def test_whitespace_only_text_yields_null_content(self):
        messages = []
        _append_tool_results(
            messages, "   \n\t  ", self._native(), [{}], ["r"],
            used_native=True, round_num=2,
        )
        assert messages[0]["content"] is None

    def test_real_prose_is_preserved(self):
        messages = []
        _append_tool_results(
            messages, "Let me check that page.", self._native(), [{}], ["r"],
            used_native=True, round_num=1,
        )
        assert messages[0]["content"] == "Let me check that page."

    def test_non_native_path_unaffected(self):
        # The text-block fallback path still wraps results in a user message.
        messages = []
        _append_tool_results(
            messages, "thinking...", [], ["tool output"], [],
            used_native=False, round_num=1,
        )
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "thinking..."
        assert messages[1]["role"] == "user"
        assert "tool output" in messages[1]["content"]


class TestAppendToolResultsThoughtSignature:
    """Gemini 3 returns an opaque thought_signature (in extra_content) with each
    function call and rejects the follow-up turn with HTTP 400 unless it is
    echoed back on the assistant tool_call. _append_tool_results must replay it
    when present, and omit the field entirely otherwise (other providers never
    send it)."""

    def test_extra_content_is_replayed_when_present(self):
        native = [{
            "id": "call_g",
            "name": "app_api",
            "arguments": '{"action": "get_memory"}',
            "extra_content": {"google": {"thought_signature": "EuIDCt8DAQ=="}},
        }]
        messages = []
        _append_tool_results(
            messages, "", native, [{}], ["mem"],
            used_native=True, round_num=1,
        )
        tc = messages[0]["tool_calls"][0]
        assert tc["extra_content"] == {"google": {"thought_signature": "EuIDCt8DAQ=="}}
        # function payload is still well-formed alongside it
        assert tc["function"]["name"] == "app_api"
        assert tc["id"] == "call_g"

    def test_no_extra_content_key_when_absent(self):
        native = [{"id": "call_o", "name": "app_api", "arguments": "{}"}]
        messages = []
        _append_tool_results(
            messages, "", native, [{}], ["r"],
            used_native=True, round_num=1,
        )
        # No empty/None extra_content leaks onto non-Gemini tool calls.
        assert "extra_content" not in messages[0]["tool_calls"][0]


# ---------------------------------------------------------------------------
# web_search sources extraction — key lookup regression (#443)
# ---------------------------------------------------------------------------

import json as _json


class TestWebSearchSourcesKeyLookup:
    """The web_search tool returns {"output": ..., "exit_code": 0}.
    The sources-extraction block in stream_agent_loop must read from the
    "output" key, not only from "results"/"stdout" (which web_search never
    sets).  Without the fix the SOURCES marker is never found, no
    web_sources SSE event is emitted, and the raw JSON blob leaks into the
    LLM's round-2 context."""

    _SOURCES = [{"title": "Example", "url": "https://example.com", "snippet": "test"}]

    def _make_result(self, key: str = "output") -> dict:
        sources_json = _json.dumps(self._SOURCES)
        text = f"Search results here.\n\n<!-- SOURCES:{sources_json} -->"
        return {key: text, "exit_code": 0}

    # ── Regression: the old lookup missed "output" ──────────────────────

    def test_old_lookup_missed_output_key(self):
        """Documents the bug: result.get('results') and result.get('stdout')
        are both absent when web_search returns its canonical {"output": ...}
        shape, so _src_text was always '' and the if-block never ran."""
        result = self._make_result("output")
        old_src_text = result.get("results") or result.get("stdout") or ""
        assert old_src_text == "", "confirms the pre-fix behaviour"

    def test_fixed_lookup_finds_output_key(self):
        """After the fix, "output" is checked first so _src_text is non-empty."""
        result = self._make_result("output")
        src_text = result.get("output") or result.get("results") or result.get("stdout") or ""
        assert src_text != ""
        assert "SOURCES" in src_text

    # ── Marker extraction works once _src_text is non-empty ─────────────

    def test_sources_extracted_from_output(self):
        result = self._make_result("output")
        src_text = result.get("output") or result.get("results") or result.get("stdout") or ""
        marker = "<!-- SOURCES:"
        idx = src_text.find(marker)
        end = src_text.find(" -->", idx)
        extracted = _json.loads(src_text[idx + len(marker):end])
        assert extracted == self._SOURCES

    def test_marker_stripped_from_output_key(self):
        """After extraction the "output" value is cleaned so the LLM never
        sees the raw JSON blob in its round-2 context."""
        result = self._make_result("output")
        src_text = result.get("output") or result.get("results") or result.get("stdout") or ""
        marker = "<!-- SOURCES:"
        idx = src_text.find(marker)
        clean = src_text[:idx].rstrip()
        # Apply to the correct key (was the bug: only "results"/"stdout" were updated)
        if "output" in result:
            result["output"] = clean
        assert "SOURCES" not in result["output"]
        assert result["output"] == "Search results here."

    # ── Backward compat: "results"/"stdout" keys still work ─────────────

    def test_results_key_still_works(self):
        result = self._make_result("results")
        src_text = result.get("output") or result.get("results") or result.get("stdout") or ""
        assert src_text != ""
        assert "SOURCES" in src_text

    def test_stdout_key_still_works(self):
        result = self._make_result("stdout")
        src_text = result.get("output") or result.get("results") or result.get("stdout") or ""
        assert src_text != ""
        assert "SOURCES" in src_text
