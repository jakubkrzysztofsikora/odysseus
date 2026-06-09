# Agent MCP smoke testing

Use this smoke after changes to agent prompting, API-token auth, native tool schemas, MCP routing, or text-tool parsing.

The regression target is a `chatgpt/*` model using native MCP function schemas, with raw text-tool parsing still covered as fallback. The agent must:

- create a real agent session through `/api/session`;
- see owner-scoped MCP tools when authenticated as an admin user, or with an Odysseus API token whose owner is an admin;
- call an MCP search tool with a non-empty query;
- call Bash with non-empty arguments;
- continue to a final answer after tool outputs;
- pass even when the session stores an OpenAI-compatible base URL such as `/v1`;
  dispatch must normalize it to `/v1/chat/completions`.
- in sequential group mode, pass participant 1's final artifact as participant 2's primary input, not the original user prompt again.

Expected live evidence:

```text
TOOL_STARTS ['mcp__0ac61a6b__search', 'bash', ...]
TOOL_OUTPUTS [('mcp__0ac61a6b__search', 0), ('bash', 0), ...]
METRICS_MODEL chatgpt/gpt-5.5
SMOKE_RESULT {'ok_mcp': True, 'ok_bash': True, 'ok_final': True, ...}
```

Check the server log for the same marker. A passing run shows native MCP calls or parsed MCP/Bash tool blocks across multiple rounds, then a final answer. It must not dispatch missing required MCP arguments or `Tool 'bash' was called with empty arguments`, and LiteLLM must show `POST /v1/chat/completions`, not `POST /v1`.

The native path repairs empty MCP search arguments before dispatch. If GPT emits an empty native Bash call but the latest user instruction explicitly contains the Bash command, that command is repaired before execution. Repaired native tool arguments are echoed back into the assistant `tool_calls` history so the next round sees the executed arguments, not `{}`. If GPT repeats the same repaired empty MCP/search call, Odysseus suppresses that repeat; if an exact Bash command is still pending it nudges Bash next, otherwise it forces a tool-free final artifact.

The text-tool parser accepts both raw MCP call forms: `mcp__server__tool{"query":"..."}` and `mcp__server__tool({"query":"..."})`. Empty parenthesized search calls are parsed, stripped from visible output, and repaired from recent real user or group-agent instructions before dispatch. Sequential group mode must also pass a structured handoff: participant 1 receives the original task, participant 2+ receive the previous participant artifact as the primary input, plus bounded original-task context and bounded tool trace.

Non-admin users intentionally cannot execute Bash or arbitrary `mcp__*` tools. If using a temporary DB-created API token for local smoke, set its `owner` to an admin user, delete it after the run, and do not print the raw token.
