# Agent MCP smoke testing

Use this smoke after changes to agent prompting, API-token auth, MCP routing, or text-tool parsing.

The regression target is a `chatgpt/*` model on the text-tool path. The agent must:

- create a real agent session through `/api/session`;
- see owner-scoped MCP tools when authenticated with an Odysseus API token;
- call an MCP search tool with a non-empty query;
- call Bash with non-empty arguments;
- continue to a round-2 final answer after tool outputs.

Expected live evidence:

```text
TOOL_STARTS ['mcp__0ac61a6b__search', 'bash', ...]
TOOL_OUTPUTS [('mcp__0ac61a6b__search', 0), ('bash', 0), ...]
METRICS_MODEL chatgpt/gpt-5.5
SMOKE_RESULT {'ok_mcp': True, 'ok_bash': True, 'ok_final': True, ...}
```

Check the server log for the same marker. A passing run shows `Agent round 1` with MCP and Bash tool blocks, followed by `Agent round 2` with a final answer. It must not log missing required MCP arguments or `Tool 'bash' was called with empty arguments`.

If using a temporary DB-created API token for local smoke, delete it after the run and do not print the raw token.
