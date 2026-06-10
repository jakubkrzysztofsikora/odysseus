import sys
from unittest.mock import MagicMock

# Clean up any mocks from previous tests to ensure we load real modules
for mod in ['src.agent_tools', 'src.tool_parsing', 'src.tool_schemas', 'src.tool_execution']:
    sys.modules.pop(mod, None)

# Mock heavy database/model dependencies before importing
for mod in [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database', 'core.models', 'core.database', 'core.auth'
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest
import src.agent_tools  # noqa: F401
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.mcp_manager import McpManager


@pytest.mark.parametrize("arguments", [
    '42',            # JSON number
    'true',          # JSON bool
    'null',          # JSON null
])
def test_non_object_arguments_do_not_crash(arguments):
    """A native function call whose arguments are valid JSON but not an object
    must not raise (it used to throw AttributeError: 'list' object has no
    attribute 'get', aborting the entire agent stream)."""
    block = function_call_to_tool_block("bash", arguments)
    # Coerced to empty args -> empty bash command, but importantly NO crash.
    assert block is not None
    assert block.tool_type == "bash"
    assert block.content == ""


@pytest.mark.parametrize("arguments", [
    '["ls -la"]',   # JSON array
    '"ls -la"',     # bare JSON string
])
def test_scalar_arguments_become_primary_tool_argument(arguments):
    block = function_call_to_tool_block("bash", arguments)
    assert block is not None
    assert block.tool_type == "bash"
    assert block.content == "ls -la"


def test_bash_accepts_common_command_aliases():
    for key in ("cmd", "shell_command", "script", "code", "input"):
        block = function_call_to_tool_block("bash", f'{{"{key}": "echo ok"}}')
        assert block is not None
        assert block.tool_type == "bash"
        assert block.content == "echo ok"


def test_bash_trims_trailing_model_statement_terminator():
    block = function_call_to_tool_block("bash", '{"command": "printf \\"ODY_GROUP_2\\";"}')
    assert block is not None
    assert block.tool_type == "bash"
    assert block.content == 'printf "ODY_GROUP_2"'


def test_bash_trims_trailing_sentence_comma_after_quoted_command():
    block = function_call_to_tool_block("bash", '{"command": "printf \\"ODY_GROUP_2\\","}')
    assert block is not None
    assert block.tool_type == "bash"
    assert block.content == 'printf "ODY_GROUP_2"'


def test_incomplete_empty_object_arguments_become_repairable_empty_bash_block():
    block = function_call_to_tool_block("bash", "{\n  ")
    assert block is not None
    assert block.tool_type == "bash"
    assert block.content == ""


def test_scalar_mcp_arguments_become_single_required_argument():
    mgr = McpManager()
    mgr._tools["remote"] = [
        {
            "name": "search",
            "description": "Search Atlassian",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    src.agent_tools.set_mcp_manager(mgr)
    try:
        block = function_call_to_tool_block("mcp__remote__search", '"circit ai"')
    finally:
        src.agent_tools.set_mcp_manager(None)

    assert block is not None
    assert block.tool_type == "mcp__remote__search"
    assert block.content == '{"query": "circit ai"}'


@pytest.mark.parametrize("tool_name", ["bash", "python", "read_file", "write_file", "web_fetch"])
def test_execution_tool_schemas_are_strict(tool_name):
    schema = next(
        item["function"]
        for item in FUNCTION_TOOL_SCHEMAS
        if item.get("function", {}).get("name") == tool_name
    )

    assert schema["strict"] is True
    assert schema["parameters"]["additionalProperties"] is False
