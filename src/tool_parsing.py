"""
tool_parsing.py

Regex-based parsing of tool invocations from LLM response text.
Supports fenced code blocks, [TOOL_CALL] blocks, and XML-style <invoke> blocks.
"""

import re
import json
import logging
from typing import List, Optional

from src.agent_tools import ToolBlock, TOOL_TAGS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Pattern 1: ```bash ... ``` fenced code blocks
_TOOL_BLOCK_RE = re.compile(
    r"```(" + "|".join(TOOL_TAGS) + r")\s*\n([\s\S]*?)```",
    re.IGNORECASE,
)
_FENCED_BLOCK_RE = re.compile(
    r"```(?P<tag>[A-Za-z][\w.-]*)\s*\n(?P<content>[\s\S]*?)```",
    re.IGNORECASE,
)

# Pattern 2: [TOOL_CALL] ... [/TOOL_CALL] blocks (some models use this format)
# Matches: {tool => "shell", args => {--command "ls -la"}} etc.
_TOOL_CALL_RE = re.compile(
    r"\[TOOL_CALL\]\s*\{([\s\S]*?)\}\s*\[/TOOL_CALL\]",
    re.IGNORECASE,
)

# Pattern 3: XML-style tool calls (minimax, some other models)
# <minimax:tool_call><invoke name="bash"><parameter name="command">...</parameter></invoke></minimax:tool_call>
# Also handles: <tool_call><invoke ...>, <function_call><invoke ...>, plain <invoke ...>
_XML_TOOL_CALL_RE = re.compile(
    r"<(?:[\w]+:)?(?:tool_call|function_call)>\s*([\s\S]*?)</(?:[\w]+:)?(?:tool_call|function_call)>",
    re.IGNORECASE,
)
_XML_INVOKE_RE = re.compile(
    r'<invoke\s+name=["\'](\w+)["\']>\s*([\s\S]*?)</invoke>',
    re.IGNORECASE,
)
_XML_PARAM_RE = re.compile(
    r'<parameter\s+name=["\'](\w+)["\']>([\s\S]*?)</parameter>',
    re.IGNORECASE,
)

# Pattern 4: <tool_code> blocks (MiniMax-M2.5 style)
# {tool => 'tool_name', args => '<param>value</param>'}
_TOOL_CODE_RE = re.compile(
    r"<tool_code>\s*\{([\s\S]*?)\}\s*</tool_code>",
    re.IGNORECASE,
)

# Pattern 6: raw function-call lines that leaked into assistant content
# instead of provider-native `tool_calls`. Seen with Mistral-shaped output:
#
#   mcp__0ac61a6b__search\Eloquent{"query": "Circit internal AI"}
#
# The optional "Eloquent" marker is tolerated because some runtimes append it
# between the function name and JSON arguments. We still route through the
# canonical native function-call converter, so unknown names are ignored rather
# than executed.
_RAW_FUNCTION_CALL_RE = re.compile(
    r"(?<![\w.-])(?P<name>mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+|[A-Za-z_][\w.-]*)"
    r"[ \t]*(?:\\?[A-Za-z][A-Za-z0-9_.-]*)?[ \t]*(?=\{)"
)

# Pattern 5: DeepSeek DSML markup leaking into content. When deepseek
# models can't emit structured tool_calls (e.g. we sent no tool schemas
# that round, or the API didn't parse them), they fall back to raw
# markup using fullwidth-pipe delimiters:
#   <｜｜DSML｜｜tool_calls>
#     <｜｜DSML｜｜invoke name="web_search">
#       <｜｜DSML｜｜parameter name="query" string="true">QUERY</｜｜DSML｜｜parameter>
#     </｜｜DSML｜｜invoke>
#   </｜｜DSML｜｜tool_calls>
# We normalize it into the standard <invoke>/<parameter> form so the
# existing XML parser + stripper handle it (parse → execute; strip →
# never show the garbage to the user). The pipe run is tolerant of
# fullwidth (U+FF5C) and ascii '|' in any count.
_DSML_PIPES = r"[｜|]+"
def _normalize_dsml(text: str) -> str:
    if not isinstance(text, str):
        return ""
    if "DSML" not in text:
        return text
    t = text
    t = re.sub(rf"<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*tool_calls\s*>", "<tool_call>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*tool_calls\s*>", "</tool_call>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*invoke\s+name=", "<invoke name=", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*invoke\s*>", "</invoke>", t, flags=re.IGNORECASE)
    # parameter open tag — drop any extra attrs (e.g. string="true").
    t = re.sub(rf'<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*parameter\s+name=(["\'][^"\']+["\'])[^>]*>',
               r"<parameter name=\1>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*parameter\s*>", "</parameter>", t, flags=re.IGNORECASE)
    return t

# Map model tool names to our tool types
_TOOL_NAME_MAP = {
    "shell": "bash",
    "bash": "bash",
    "terminal": "bash",
    "command": "bash",
    "execute": "bash",
    "run": "bash",
    "python": "python",
    "code": "python",
    "search": "web_search",
    "web_search": "web_search",
    "websearch": "web_search",
    "google_search": "web_search",
    "google_search_retrieval": "web_search",
    "google_search_grounding": "web_search",
    "web_fetch": "web_fetch",
    "webfetch": "web_fetch",
    "fetch_url": "web_fetch",
    "fetch": "web_fetch",
    "read": "read_file",
    "read_file": "read_file",
    "cat": "read_file",
    "write": "write_file",
    "write_file": "write_file",
    "save": "write_file",
    "document": "update_document",
    "update_document": "update_document",
    "create_document": "create_document",
    "edit": "edit_document",
    "edit_document": "edit_document",
    "search_chats": "search_chats",
    "search_conversations": "search_chats",
    "find_chat": "search_chats",
    "chat_with_model": "chat_with_model",
    "ask_model": "chat_with_model",
    "chat_model": "chat_with_model",
    "create_session": "create_session",
    "new_session": "create_session",
    "list_sessions": "list_sessions",
    "send_to_session": "send_to_session",
    "message_session": "send_to_session",
    "pipeline": "pipeline",
    "chain": "pipeline",
    "manage_session": "manage_session",
    "session_control": "manage_session",
    "manage_memory": "manage_memory",
    "memory": "manage_memory",
    "manage_tasks": "manage_tasks",
    "tasks": "manage_tasks",
    "schedule": "manage_tasks",
    "list_models": "list_models",
    "models": "list_models",
    "available_models": "list_models",
    "ui_control": "ui_control",
    "ui": "ui_control",
    "control": "ui_control",
    "api_call": "api_call",
    "api": "api_call",
    "integration": "api_call",
    "ask_teacher": "ask_teacher",
    "teacher": "ask_teacher",
    "manage_skills": "manage_skills",
    "skills": "manage_skills",
    "skill": "manage_skills",
    "suggest_document": "suggest_document",
    "suggest": "suggest_document",
    "review_document": "suggest_document",
    "manage_endpoints": "manage_endpoints",
    "endpoints": "manage_endpoints",
    "manage_mcp": "manage_mcp",
    "mcp_servers": "manage_mcp",
    "manage_webhooks": "manage_webhooks",
    "webhooks": "manage_webhooks",
    "manage_tokens": "manage_tokens",
    "tokens": "manage_tokens",
    "manage_documents": "manage_documents",
    "documents": "manage_documents",
    "manage_research": "manage_research",
    "list_research": "manage_research",
    "read_research": "manage_research",
    "open_research": "manage_research",
    "delete_research": "manage_research",
    "manage_settings": "manage_settings",
    "settings": "manage_settings",
    "preferences": "manage_settings",
    "manage_notes": "manage_notes",
    "notes": "manage_notes",
    "todo": "manage_notes",
    "todos": "manage_notes",
}


# ---------------------------------------------------------------------------
# Parsing functions
# ---------------------------------------------------------------------------

def _parse_tool_call_block(raw: str) -> Optional[ToolBlock]:
    """Parse a [TOOL_CALL] block into a ToolBlock.

    Handles formats like:
      {tool => "shell", args => {--command "ls -la"}}
      {tool: "shell", command: "ls -la"}
    """
    # Try to extract tool name
    tool_match = re.search(r'tool\s*(?:=>|:|=)\s*["\']?(\w+)["\']?', raw, re.IGNORECASE)
    if not tool_match:
        return None

    tool_name = tool_match.group(1).lower()
    # Fall back to the raw name when it's a real tool but not in the alias
    # map, so known tools (e.g. manage_calendar) aren't silently dropped.
    mapped = _TOOL_NAME_MAP.get(tool_name) or (tool_name if tool_name in TOOL_TAGS else None)
    if not mapped:
        return None

    # Extract the command/content — try several patterns
    content = None

    # Pattern: --command "value" or --command 'value'
    cmd_match = re.search(r'--command\s+["\'](.+?)["\']', raw, re.DOTALL)
    if cmd_match:
        content = cmd_match.group(1)

    # Pattern: command => "value" or command: "value"
    if not content:
        cmd_match = re.search(r'command\s*(?:=>|:|=)\s*["\'](.+?)["\']', raw, re.DOTALL)
        if cmd_match:
            content = cmd_match.group(1)

    # Pattern: args => {content} — extract everything inside the nested braces
    if not content:
        args_match = re.search(r'args\s*(?:=>|:|=)\s*\{([\s\S]*)\}', raw, re.DOTALL)
        if args_match:
            inner = args_match.group(1).strip()
            # Strip quotes and key prefixes
            inner = re.sub(r'^--?\w+\s+', '', inner)
            inner = inner.strip('\'"')
            if inner:
                content = inner

    # Pattern: query/path/code => "value"
    if not content:
        for key in ("query", "path", "code", "content", "text", "file"):
            m = re.search(rf'{key}\s*(?:=>|:|=)\s*["\'](.+?)["\']', raw, re.DOTALL)
            if m:
                content = m.group(1)
                break

    # Last resort: take everything after the tool declaration
    if not content:
        rest = raw[tool_match.end():].strip()
        rest = re.sub(r'^[,;]\s*', '', rest)
        rest = rest.strip('{} \t\n\'"')
        if rest:
            content = rest

    if content:
        return ToolBlock(mapped, content.strip())
    return None


def _parse_xml_invoke(inv_match) -> Optional[ToolBlock]:
    """Parse an <invoke name="tool"><parameter ...>...</parameter></invoke> match.

    Delegates content-shaping to function_call_to_tool_block — the SAME
    converter used for native function calls — so the full tool set (every
    name in TOOL_TAGS, plus email + MCP tools) and the correct per-tool
    content format are handled in ONE place. The previous version duplicated
    a partial, hand-maintained tool-name map plus a `key: value` serializer:
    any tool missing from that map (e.g. `manage_calendar`) was silently
    dropped, and JSON-arg tools got an unparseable `k: v` blob. Both bugs
    made deepseek's DSML `create_event` calls vanish with no execution.
    """
    # Lowercase the tool name: models often emit capitalized invoke names
    # (e.g. <invoke name="Bash">) and function_call_to_tool_block matches
    # case-sensitively against the lowercase _TOOL_NAME_MAP / TOOL_TAGS, so a
    # raw capitalized name would be silently dropped.
    tool_name = inv_match.group(1).lower()
    body = inv_match.group(2)
    params = {}
    for pm in _XML_PARAM_RE.finditer(body):
        params[pm.group(1)] = pm.group(2).strip()
    # Local import to avoid a circular import at module load.
    from src.tool_schemas import function_call_to_tool_block
    return function_call_to_tool_block(tool_name, json.dumps(params))


def _parse_tool_code_block(raw: str) -> Optional[ToolBlock]:
    """Parse a <tool_code>{tool => 'name', args => '...'}</tool_code> block (MiniMax style)."""
    # Extract tool name
    tool_match = re.search(r"tool\s*=>\s*['\"](\S+?)['\"]", raw)
    if not tool_match:
        return None
    tool_name = tool_match.group(1).lower().replace('-', '_')
    # Strip MCP prefixes like "mcp__server__" or "cli-mcp-server-"
    for prefix in ("mcp__", "cli_mcp_server_", "desktop_commander_", "mcp_code_executor_"):
        if tool_name.startswith(prefix):
            tool_name = tool_name[len(prefix):]
            break

    mapped = _TOOL_NAME_MAP.get(tool_name)

    # Extract args content
    args_match = re.search(r"args\s*=>\s*['\"]?\s*([\s\S]*?)\s*['\"]?\s*$", raw, re.DOTALL)
    args_body = args_match.group(1).strip().strip("'\"") if args_match else ""

    # Parse XML params inside args (e.g. <command>ls</command>)
    xml_params = {}
    for pm in re.finditer(r"<(\w+)>([\s\S]*?)</\1>", args_body):
        xml_params[pm.group(1)] = pm.group(2).strip()

    # When the model gave structured params, hand them to the canonical
    # converter (same as native calls + <invoke>) so the full tool set and
    # correct per-tool content format apply — not a partial map + k:v blob.
    if xml_params:
        from src.tool_schemas import function_call_to_tool_block
        block = function_call_to_tool_block(mapped or tool_name, json.dumps(xml_params))
        if block:
            return block

    # No structured params: args_body is a raw single value (e.g. a bash
    # command). Keep the freeform special-casing for the simple tools.
    if mapped:
        if mapped == "bash":
            content = xml_params.get("command", args_body)
        elif mapped == "python":
            content = xml_params.get("code", args_body)
        elif mapped == "web_search":
            content = xml_params.get("query", args_body)
        elif mapped == "web_fetch":
            content = xml_params.get("url", args_body)
        elif mapped in ("read_file", "write_file"):
            content = xml_params.get("path", xml_params.get("file_path", args_body))
        else:
            content = "\n".join(f"{k}: {v}" for k, v in xml_params.items()) if xml_params else args_body
        if content:
            return ToolBlock(mapped, content.strip())
    elif tool_name and args_body:
        # Unknown tool — try as MCP tool call
        content = "\n".join(f"{k}: {v}" for k, v in xml_params.items()) if xml_params else args_body
        return ToolBlock(tool_name, content.strip())
    return None


def _raw_function_call_spans(text: str):
    """Parse raw single-line function calls emitted in assistant content.

    This catches provider/runtime leaks where the model writes the function
    call directly as content instead of using native `tool_calls`. Each match is
    still validated by function_call_to_tool_block; unknown names are dropped.
    """
    if not isinstance(text, str) or "{" not in text:
        return []

    decoder = json.JSONDecoder()
    from src.tool_schemas import function_call_to_tool_block
    spans = []

    for match in _RAW_FUNCTION_CALL_RE.finditer(text):
        name = match.group("name").lower()
        try:
            args, end = decoder.raw_decode(text[match.end():])
        except json.JSONDecodeError:
            continue
        block = function_call_to_tool_block(name, json.dumps(args))
        if block:
            spans.append((match.start(), match.end() + end, block))
    return spans


def _parse_raw_function_calls(text: str) -> List[ToolBlock]:
    return [block for _start, _end, block in _raw_function_call_spans(text)]


_BARE_JSON_TOOL_NAME_KEYS = ("tool", "tool_name", "name", "function")
_BARE_JSON_ARGUMENT_KEYS = ("arguments", "args", "parameters")
_BARE_JSON_BASH_KEYS = ("cmd", "command", "shell_command")


def _bare_json_object_to_tool_block(obj: dict) -> Optional[ToolBlock]:
    """Convert a standalone JSON object into a tool block when unambiguous."""
    if not isinstance(obj, dict):
        return None

    from src.tool_schemas import function_call_to_tool_block

    tool_name = None
    for key in _BARE_JSON_TOOL_NAME_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            tool_name = value.strip()
            break
    if tool_name:
        args = None
        for key in _BARE_JSON_ARGUMENT_KEYS:
            if key in obj:
                args = obj.get(key)
                break
        if args is None:
            excluded = set(_BARE_JSON_TOOL_NAME_KEYS)
            args = {k: v for k, v in obj.items() if k not in excluded}
        return function_call_to_tool_block(tool_name, json.dumps(args if args is not None else {}))

    for key in _BARE_JSON_BASH_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return ToolBlock("bash", value.strip())

    return None


def _bare_json_tool_spans(text: str):
    if not isinstance(text, str) or "{" not in text:
        return []
    decoder = json.JSONDecoder()
    spans = []
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            i = start + 1
            continue
        block = _bare_json_object_to_tool_block(obj) if isinstance(obj, dict) else None
        if block:
            spans.append((start, start + end, block))
            i = start + end
        else:
            i = start + 1
    return spans


def _parse_bare_json_tool_objects(text: str) -> List[ToolBlock]:
    """Parse provider fallbacks like {"cmd": "pwd"} emitted as content."""
    return [block for _start, _end, block in _bare_json_tool_spans(text)]


def _norm_mcp_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _mcp_alias_fence_to_tool_block(tag: str, content: str) -> Optional[ToolBlock]:
    """Map ```atlassian {"query": "..."} ``` style fallbacks to MCP tools."""
    tag_norm = _norm_mcp_alias(tag)
    if not tag_norm or tag_norm in {t.lower() for t in TOOL_TAGS}:
        return None
    try:
        args = json.loads((content or "").strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(args, dict):
        return None

    explicit_name = args.get("tool") or args.get("tool_name") or args.get("name") or args.get("function")
    if isinstance(explicit_name, str) and explicit_name.strip():
        tool_args = args.get("arguments", args.get("args", args.get("parameters", {})))
        from src.tool_schemas import function_call_to_tool_block
        return function_call_to_tool_block(explicit_name.strip(), json.dumps(tool_args or {}))

    try:
        from src import agent_tools

        mcp = agent_tools.get_mcp_manager()
        if not mcp:
            return None
        all_tools = mcp.get_all_tools({}) or []
        explicit_tag_match = None
        for tool in all_tools:
            server_id = str(tool.get("server_id") or "")
            server_name = str(tool.get("server_name") or "")
            tool_name = str(tool.get("name") or "")
            qualified = str(tool.get("qualified_name") or "")
            aliases = {
                "mcp" + _norm_mcp_alias(server_id) + _norm_mcp_alias(tool_name),
                "mcp" + _norm_mcp_alias(server_name) + _norm_mcp_alias(tool_name),
                _norm_mcp_alias(qualified),
            }
            if tag_norm in aliases:
                explicit_tag_match = tool
                break
        if explicit_tag_match:
            qualified = str(explicit_tag_match.get("qualified_name") or "")
            if not qualified.startswith("mcp__"):
                qualified = f"mcp__{explicit_tag_match.get('server_id')}__{explicit_tag_match.get('name')}"
            if hasattr(mcp, "coerce_tool_arguments"):
                args = mcp.coerce_tool_arguments(qualified, args)
            return ToolBlock(qualified, json.dumps(args) if args else "{}")

        candidates = []
        for tool in all_tools:
            server_id = str(tool.get("server_id") or "")
            server_name = str(tool.get("server_name") or "")
            if tag_norm not in {
                _norm_mcp_alias(server_id),
                _norm_mcp_alias(server_name),
            }:
                continue
            candidates.append(tool)
        if not candidates:
            return None
        preferred = None
        if any(k in args for k in ("query", "q", "search")):
            preferred = next((t for t in candidates if str(t.get("name") or "").lower() == "search"), None)
            preferred = preferred or next((t for t in candidates if "search" in str(t.get("name") or "").lower()), None)
        preferred = preferred or (candidates[0] if len(candidates) == 1 else None)
        if not preferred:
            return None
        qualified = str(preferred.get("qualified_name") or "")
        if not qualified.startswith("mcp__"):
            qualified = f"mcp__{preferred.get('server_id')}__{preferred.get('name')}"
        if hasattr(mcp, "coerce_tool_arguments"):
            args = mcp.coerce_tool_arguments(qualified, args)
        return ToolBlock(qualified, json.dumps(args) if args else "{}")
    except Exception as e:
        logger.debug("MCP alias fence parsing failed for %s: %s", tag, e)
        return None


def _mcp_alias_fence_spans(text: str):
    if not isinstance(text, str) or "```" not in text:
        return []
    spans = []
    for match in _FENCED_BLOCK_RE.finditer(text):
        tag = match.group("tag")
        if tag.lower() in {t.lower() for t in TOOL_TAGS}:
            continue
        block = _mcp_alias_fence_to_tool_block(tag, match.group("content"))
        if block:
            spans.append((match.start(), match.end(), block))
    return spans


def _parse_mcp_alias_fenced_blocks(text: str) -> List[ToolBlock]:
    return [block for _start, _end, block in _mcp_alias_fence_spans(text)]


def _strip_raw_function_calls(text: str) -> str:
    if not isinstance(text, str) or "{" not in text:
        return "" if text is None else text

    decoder = json.JSONDecoder()
    spans = []
    for match in _RAW_FUNCTION_CALL_RE.finditer(text):
        name = match.group("name").lower()
        try:
            args, end = decoder.raw_decode(text[match.end():])
        except json.JSONDecodeError:
            continue

        from src.tool_schemas import function_call_to_tool_block
        if not function_call_to_tool_block(name, json.dumps(args)):
            continue

        abs_end = match.end() + end
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", abs_end)
        if line_end < 0:
            line_end = len(text)
        prefix = text[line_start:match.start()].strip()
        suffix = text[abs_end:line_end].strip()
        if not prefix and not suffix:
            span_end = line_end + (1 if line_end < len(text) else 0)
            spans.append((line_start, span_end))
        else:
            spans.append((match.start(), abs_end))

    if not spans:
        return text

    cleaned_parts = []
    cursor = 0
    for start, end in spans:
        cleaned_parts.append(text[cursor:start])
        cursor = end
    cleaned_parts.append(text[cursor:])
    return "".join(cleaned_parts)


def _strip_bare_json_tool_objects(text: str) -> str:
    spans = _bare_json_tool_spans(text)
    if not spans:
        return text
    cleaned_parts = []
    cursor = 0
    for start, end, _block in spans:
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end < 0:
            line_end = len(text)
        prefix = text[line_start:start].strip()
        suffix = text[end:line_end].strip()
        if not prefix and not suffix:
            span_start = line_start
            span_end = line_end + (1 if line_end < len(text) else 0)
        else:
            span_start = start
            span_end = end
        cleaned_parts.append(text[cursor:span_start])
        cursor = span_end
    cleaned_parts.append(text[cursor:])
    return "".join(cleaned_parts)


def _strip_mcp_alias_fenced_blocks(text: str) -> str:
    spans = _mcp_alias_fence_spans(text)
    if not spans:
        return text
    cleaned_parts = []
    cursor = 0
    for start, end, _block in spans:
        cleaned_parts.append(text[cursor:start])
        cursor = end
    cleaned_parts.append(text[cursor:])
    return "".join(cleaned_parts)


def parse_tool_blocks(text: str) -> List[ToolBlock]:
    """Extract executable tool blocks from LLM response text.

    Supports multiple formats:
    1. ```bash ... ``` fenced code blocks (standard)
    2. [TOOL_CALL] ... [/TOOL_CALL] blocks (some models)
    3. XML-style <tool_call>/<invoke> blocks
    4. <tool_code> blocks (MiniMax-M2.5 style)
    5. DeepSeek DSML markup (normalized to <invoke> first)
    6. Raw native function-call lines leaked into assistant content
    7. Bare JSON tool objects leaked into assistant content
    """
    blocks = []

    # Normalize DeepSeek DSML markup into standard <invoke> form so the
    # XML patterns below catch it.
    text = _normalize_dsml(text)

    # Pattern 1 plus content-leaked function calls. Collect spans first so
    # mixed formats execute in the same order the model emitted them.
    block_spans = []
    for m in _TOOL_BLOCK_RE.finditer(text):
        tag = m.group(1).lower()
        content = m.group(2).strip()
        if not content:
            continue
        # If a code block's content is an <invoke> XML call (some models wrap
        # tool calls in ```python or ```xml fences), parse the invoke instead.
        if '<invoke' in content:
            invoked = False
            for inv in _XML_INVOKE_RE.finditer(content):
                block = _parse_xml_invoke(inv)
                if block:
                    block_spans.append((m.start(), m.end(), block))
                    invoked = True
            if invoked:
                continue
        block_spans.append((m.start(), m.end(), ToolBlock(tag, content)))

    block_spans.extend(_mcp_alias_fence_spans(text))

    def overlaps_existing(start: int, end: int) -> bool:
        return any(start < span_end and end > span_start for span_start, span_end, _ in block_spans)

    for start, end, block in _raw_function_call_spans(text):
        if not overlaps_existing(start, end):
            block_spans.append((start, end, block))

    for start, end, block in _bare_json_tool_spans(text):
        if not overlaps_existing(start, end):
            block_spans.append((start, end, block))

    if block_spans:
        blocks.extend(block for _start, _end, block in sorted(block_spans, key=lambda item: item[0]))

    # Pattern 2: [TOOL_CALL] blocks (only if no fenced blocks found)
    if not blocks:
        for m in _TOOL_CALL_RE.finditer(text):
            block = _parse_tool_call_block(m.group(1))
            if block:
                blocks.append(block)

    # Pattern 3: XML-style <tool_call>/<invoke> blocks
    if not blocks:
        # Try wrapped: <tool_call><invoke ...>...</invoke></tool_call>
        for m in _XML_TOOL_CALL_RE.finditer(text):
            for inv in _XML_INVOKE_RE.finditer(m.group(1)):
                block = _parse_xml_invoke(inv)
                if block:
                    blocks.append(block)
        # Try bare <invoke> without wrapper
        if not blocks:
            for inv in _XML_INVOKE_RE.finditer(text):
                block = _parse_xml_invoke(inv)
                if block:
                    blocks.append(block)

    # Pattern 4: <tool_code> blocks (MiniMax-M2.5 style)
    if not blocks:
        for m in _TOOL_CODE_RE.finditer(text):
            block = _parse_tool_code_block(m.group(1))
            if block:
                blocks.append(block)

    return blocks


def strip_tool_blocks(text: str) -> str:
    """Remove executable tool blocks from text for clean display."""
    # Normalize DSML first so its markup gets stripped by the <invoke>
    # / <tool_call> removers below instead of leaking to the user.
    text = _normalize_dsml(text)
    cleaned = _TOOL_BLOCK_RE.sub('', text)
    cleaned = _strip_mcp_alias_fenced_blocks(cleaned)
    cleaned = _TOOL_CALL_RE.sub('', cleaned)
    cleaned = _XML_TOOL_CALL_RE.sub('', cleaned)
    cleaned = _TOOL_CODE_RE.sub('', cleaned)
    # Strip bare <invoke> blocks not wrapped in <tool_call>
    cleaned = re.sub(r'<invoke\s+name=["\'].*?</invoke>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = _strip_raw_function_calls(cleaned)
    cleaned = _strip_bare_json_tool_objects(cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()
