# Agent MCP smoke testing

Use this smoke after changes to agent prompting, API-token auth, native tool schemas, MCP routing, or text-tool parsing.

Run the live smoke from the repo root:

```bash
python3 scripts/smoke_multiturn_multitool.py
```

Run the sequential group-agent smoke from the repo root:

```bash
python3 scripts/smoke_group_sequential.py
```

Defaults target the local app at `http://127.0.0.1:7860`, user `admin`, password file `deploy/.admin-pw`, endpoint `00b16177`, model `chatgpt/gpt-5.5`, and Atlassian search tool `mcp__0ac61a6b__search`. Override with `ODYSSEUS_BASE_URL`, `ODYSSEUS_SMOKE_MODEL`, `ODYSSEUS_GROUP_SMOKE_MODELS`, `ODYSSEUS_SMOKE_ENDPOINT_ID`, or `ODYSSEUS_SMOKE_MCP_TOOL`.

The regression target is a `chatgpt/*` model using native MCP function schemas, with raw text-tool parsing still covered as fallback. The agent must:

- create a real agent session through `/api/session`;
- see owner-scoped MCP tools when authenticated as an admin user, or with an Odysseus API token whose owner is an admin;
- call an MCP search tool with a non-empty query;
- call Bash with non-empty arguments;
- prove Bash succeeded by checking the `tool_output` exit code is `0` and the
  expected marker appears in the actual tool output, not only in final prose;
- repeat a Bash tool call on a second turn and remember the first turn's assistant JSON;
- answer a strict third follow-up from session history without calling tools;
- continue to a final answer after tool outputs;
- pass even when the session stores an OpenAI-compatible base URL such as `/v1`;
  dispatch must normalize it to `/v1/chat/completions`.
- in sequential group mode, pass participant 1's final artifact and tool trace as participant 2's primary input, not the original user prompt again.
- in sequential group mode, prove participant 2 can use a second tool call and include participant 1's marker in its final artifact.

Expected live evidence:

```text
TOOL_STARTS ['mcp__0ac61a6b__search', 'bash', ...]
TOOL_OUTPUTS [('mcp__0ac61a6b__search', 0), ('bash', 0), ...]
METRICS_MODEL chatgpt/gpt-5.5
SMOKE_RESULT {'ok_mcp': True, 'ok_bash': True, 'ok_final': True, ...}
```

Check the server log for the same marker. A passing run shows native MCP calls or parsed MCP/Bash tool blocks across multiple rounds, then a final answer. It must not dispatch missing required MCP arguments or `Tool 'bash' was called with empty arguments`, and LiteLLM must show `POST /v1/chat/completions`, not `POST /v1`.

Streaming reads are capped by `ODYSSEUS_STREAM_READ_TIMEOUT_CAP_SECONDS`
(default `45`) so an upstream HTTP 200 with no assistant/tool-call bytes fails
fast and can route through the normal pre-content fallback path instead of
leaving the agent turn open for the full UI stream timeout.

The native path repairs empty MCP search arguments before dispatch. If GPT emits an empty native Bash call but the latest user instruction explicitly contains the Bash command, that command is repaired before execution. Repaired native tool arguments are echoed back into the assistant `tool_calls` history so the next round sees the executed arguments, not `{}`. If GPT repeats the same repaired empty MCP/search call, Odysseus suppresses that repeat; if an exact Bash command is still pending it nudges Bash next, otherwise it forces a tool-free final artifact.

The text-tool parser accepts both raw MCP call forms: `mcp__server__tool{"query":"..."}` and `mcp__server__tool({"query":"..."})`. Empty parenthesized search calls are parsed, stripped from visible output, and repaired from recent real user or group-agent instructions before dispatch. Sequential group mode must also pass a structured handoff: participant 1 receives the original task, participant 2+ receive the previous participant artifact as the primary input, plus bounded original-task context and bounded tool trace.

Non-admin users intentionally cannot execute Bash or arbitrary `mcp__*` tools. If using a temporary DB-created API token for local smoke, set its `owner` to an admin user, delete it after the run, and do not print the raw token.
