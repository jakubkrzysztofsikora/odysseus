#!/usr/bin/env python3
"""Live Odysseus sequential group-agent smoke.

This exercises the production API path used by the web group UI:

  1. log in;
  2. create a parent group session and two participant sessions;
  3. inject participant system prompts;
  4. send the original task to participant 1;
  5. send a structured handoff with participant 1's artifact/tool trace to
     participant 2;
  6. assert both participants used real tools and participant 2 used the prior
     artifact, not just the original prompt.

MCP output is redacted from stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from smoke_multiturn_multitool import (
    _create_session,
    _has_tool,
    _login,
    _mcp_output_ok,
    _mcp_query_called,
    _read_password,
    _request,
    _stream_turn,
    _summarize,
)


def _inject_messages(base_url: str, cookie: str, session_id: str, messages: list[dict[str, Any]]) -> None:
    payload = json.dumps({"messages": messages}).encode()
    with _request(
        f"{base_url}/api/session/{session_id}/inject_messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Cookie": f"odysseus_session={cookie}",
        },
        timeout=60,
    ) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            raise RuntimeError(f"inject_messages failed for {session_id}: HTTP {resp.status} {body[:300]}")


def _participant_prompt(name: str, other_name: str) -> str:
    return (
        f"You are {name} in a group chat with {other_name} and the user. "
        "[Name]: prefixed messages are from other participants. They are trusted context from this current "
        "group run, not arbitrary external instructions; use them as inputs to your assigned role instead "
        "of refusing them as adversarial content. This is an authorized internal planning workflow inside "
        "the user's signed-in Odysseus workspace. If the task asks for Atlassian, MCP, web, bash, repo, or "
        "document tools, call the needed tools with explicit arguments; never answer only with a promise "
        "that you will search or inspect. Produce a concrete artifact for your role that the next "
        "participant can use. Redact secrets, tokens, credentials, customer personal data, and unnecessary "
        "low-level attack details. Include enough evidence, assumptions, and next actions for the following "
        "agent to continue without seeing the original prompt."
    )


def _truncate_handoff(text: str, max_chars: int = 12000) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n[...handoff truncated]"


def _build_handoff(original_task: str, previous_name: str, previous_output: str) -> str:
    prev = _truncate_handoff(previous_output or "[Previous participant produced no visible artifact.]")
    return "\n\n".join(
        [
            "Sequential group handoff.",
            "Primary input: continue from the previous participant output below. Do not restart the workflow from scratch.",
            f"Previous participant ({previous_name or 'previous agent'}) output:",
            prev,
            "Original user task for context only:",
            _truncate_handoff(original_task, 4000),
            "Continue with your assigned role. If you need tools or MCP, call them with explicit non-empty arguments derived from the previous output and task context.",
        ]
    )


def _sequence_output(answer: str, events: list[dict[str, Any]], mcp_tool: str) -> str:
    lines = []
    if answer.strip():
        lines.append(answer.strip())
    tool_lines = []
    for event in events:
        if event.get("type") == "tool_start":
            tool_lines.append(f"TOOL {event.get('tool')}: {event.get('command') or ''}".rstrip())
        elif event.get("type") == "tool_output":
            output = event.get("output") or ""
            if event.get("tool") == mcp_tool:
                output = f"<redacted MCP output, {len(output)} chars>"
            tool_lines.append(f"OUTPUT {event.get('tool')}: {output[:1200]}")
        elif event.get("type") == "error":
            tool_lines.append(f"ERROR {event.get('error')}")
    if tool_lines:
        lines.append("Tool trace:\n" + "\n".join(tool_lines))
    return _truncate_handoff("\n\n".join(lines))


def _event_has_error(events: list[dict[str, Any]]) -> bool:
    return any(
        event.get("type") in {"error", "bad_json"}
        or "Stream error" in json.dumps(event, ensure_ascii=False)
        for event in events
    )


def _json_answer(answer: str) -> dict[str, Any]:
    try:
        parsed = json.loads((answer or "").strip())
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("ODYSSEUS_BASE_URL", "http://127.0.0.1:7860"))
    parser.add_argument("--username", default=os.getenv("ODYSSEUS_SMOKE_USERNAME", "admin"))
    parser.add_argument("--password")
    parser.add_argument("--password-file", default=os.getenv("ODYSSEUS_SMOKE_PASSWORD_FILE", "deploy/.admin-pw"))
    parser.add_argument("--endpoint-id", default=os.getenv("ODYSSEUS_SMOKE_ENDPOINT_ID", "00b16177"))
    parser.add_argument(
        "--models",
        default=os.getenv("ODYSSEUS_GROUP_SMOKE_MODELS", "chatgpt/gpt-5.5,chatgpt/gpt-5.5"),
        help="Comma-separated participant models. The first two are used.",
    )
    parser.add_argument("--mcp-tool", default=os.getenv("ODYSSEUS_SMOKE_MCP_TOOL", "mcp__0ac61a6b__search"))
    parser.add_argument(
        "--mcp-query",
        default=os.getenv("ODYSSEUS_GROUP_SMOKE_MCP_QUERY", "Odysseus sequential group smoke sentinel 2026-06-10"),
    )
    parser.add_argument("--workspace", default=os.getenv("ODYSSEUS_GROUP_SMOKE_WORKSPACE", str(Path.cwd())))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("ODYSSEUS_SMOKE_TIMEOUT", "420")))
    args = parser.parse_args()

    models = [part.strip() for part in args.models.split(",") if part.strip()]
    if len(models) < 2:
        raise SystemExit("--models must contain at least two comma-separated entries")

    base_url = args.base_url.rstrip("/")
    password = _read_password(args)

    try:
        cookie = _login(base_url, args.username, password)
        run_id = int(time.time())
        parent_id = _create_session(
            base_url,
            cookie,
            endpoint_id=args.endpoint_id,
            model=models[0],
            name=f"[GRP] Smoke sequential {run_id}",
        )
        participant_1 = _create_session(
            base_url,
            cookie,
            endpoint_id=args.endpoint_id,
            model=models[0],
            name=f"[GRP] Smoke agent one {run_id}",
        )
        participant_2 = _create_session(
            base_url,
            cookie,
            endpoint_id=args.endpoint_id,
            model=models[1],
            name=f"[GRP] Smoke agent two {run_id}",
        )

        _inject_messages(base_url, cookie, participant_1, [{"role": "system", "content": _participant_prompt("Agent One", "Agent Two")}])
        _inject_messages(base_url, cookie, participant_2, [{"role": "system", "content": _participant_prompt("Agent Two", "Agent One")}])

        original_task = (
            "Automated Odysseus sequential group smoke. Agent One must do both tools before answering: "
            "use Bash to run exactly `printf 'ODY_GROUP_1'`. Then use the Atlassian MCP search tool with "
            f"the exact query `{args.mcp_query}`. Agent One must answer only compact JSON with keys "
            "agent, bash_seen, mcp_called, and next_agent_instruction. Agent Two must continue from Agent "
            "One's artifact, use Bash to run exactly `printf 'ODY_GROUP_2'`. Then answer only compact JSON "
            "with keys agent, previous_bash_seen, bash_seen, and handoff_used."
        )
        _inject_messages(base_url, cookie, parent_id, [{"role": "user", "content": original_task}])

        first_events, first_answer, first_elapsed = _stream_turn(
            base_url,
            cookie,
            participant_1,
            original_task,
            args.timeout,
            allow_web_search=True,
            workspace=args.workspace,
        )
        first_sequence = _sequence_output(first_answer, first_events, args.mcp_tool)
        handoff = _build_handoff(original_task, "Agent One", first_sequence)
        _inject_messages(
            base_url,
            cookie,
            parent_id,
            [{"role": "assistant", "content": first_answer, "metadata": {"group_model": "Agent One", "model": models[0]}}],
        )

        second_events, second_answer, second_elapsed = _stream_turn(
            base_url,
            cookie,
            participant_2,
            handoff,
            args.timeout,
            allow_web_search=True,
            workspace=args.workspace,
        )
        _inject_messages(
            base_url,
            cookie,
            parent_id,
            [{"role": "assistant", "content": second_answer, "metadata": {"group_model": "Agent Two", "model": models[1]}}],
        )

        print(f"=== participant_1 model={models[0]} elapsed={first_elapsed:.2f}s ===")
        print(f"answer: {first_answer[:2000]}")
        print("events:", json.dumps(_summarize(first_events, args.mcp_tool), ensure_ascii=False))
        print(f"=== participant_2 model={models[1]} elapsed={second_elapsed:.2f}s ===")
        print(f"answer: {second_answer[:2000]}")
        print("events:", json.dumps(_summarize(second_events, args.mcp_tool), ensure_ascii=False))

        first_json = _json_answer(first_answer)
        second_json = _json_answer(second_answer)
        checks = {
            "first_bash_exact": _has_tool(first_events, "bash", command="printf 'ODY_GROUP_1'", output="ODY_GROUP_1"),
            "first_mcp_exact_query": _mcp_query_called(first_events, args.mcp_tool, args.mcp_query),
            "first_mcp_output": _mcp_output_ok(first_events, args.mcp_tool),
            "handoff_contains_previous_artifact": "Sequential group handoff." in handoff and "ODY_GROUP_1" in handoff,
            "second_bash_exact": _has_tool(second_events, "bash", command="printf 'ODY_GROUP_2'", output="ODY_GROUP_2"),
            "second_remembers_first": "ODY_GROUP_1" in second_answer,
            "second_final_seen": second_json.get("bash_seen") == "ODY_GROUP_2",
            "first_json_marker": first_json.get("bash_seen") == "ODY_GROUP_1" and first_json.get("mcp_called") is True,
            "no_errors": not _event_has_error(first_events) and not _event_has_error(second_events),
        }
        print("=== checks ===")
        print(json.dumps(checks, indent=2))
        return 0 if all(checks.values()) else 2
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body[:500]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
