#!/usr/bin/env python3
"""Live Odysseus multiturn/multitool smoke.

Exercises the real browser API path:
  1. log in,
  2. create a session,
  3. run an agent turn that must call Bash and Atlassian MCP search,
  4. run a second agent turn that must call Bash and remember turn 1,
  5. run a tool-free follow-up that must still remember turn 1.

The script redacts MCP output from stdout but still checks that the MCP tool
returned successfully.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any


def _read_password(args: argparse.Namespace) -> str:
    for value in (args.password, os.getenv("ODYSSEUS_SMOKE_PASSWORD"), os.getenv("ODYSSEUS_ADMIN_PASSWORD")):
        if value:
            return value
    if args.password_file:
        try:
            return Path(args.password_file).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
    raise SystemExit("missing password: set ODYSSEUS_SMOKE_PASSWORD or pass --password-file")


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> urllib.response.addinfourl:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def _login(base_url: str, username: str, password: str) -> str:
    payload = json.dumps({"username": username, "password": password, "remember": False}).encode()
    with _request(
        f"{base_url}/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        timeout=60,
    ) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            raise SystemExit(f"login failed: HTTP {resp.status} {body[:200]}")
        cookie_header = resp.headers.get("Set-Cookie", "")
    cookies = SimpleCookie()
    cookies.load(cookie_header)
    morsel = cookies.get("odysseus_session")
    if morsel is None or not morsel.value:
        raise SystemExit("login did not return odysseus_session cookie")
    return morsel.value


def _create_session(
    base_url: str,
    cookie: str,
    *,
    endpoint_id: str,
    model: str,
    name: str,
) -> str:
    fields = {
        "name": name,
        "endpoint_id": endpoint_id,
        "model": model,
        "skip_validation": "true",
    }
    body = urllib.parse.urlencode(fields).encode()
    with _request(
        f"{base_url}/api/session",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": f"odysseus_session={cookie}",
        },
        timeout=60,
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    session_id = payload.get("id")
    if not session_id:
        raise SystemExit(f"session creation failed: {payload}")
    return session_id


def _stream_turn(
    base_url: str,
    cookie: str,
    session_id: str,
    message: str,
    timeout: int,
    *,
    allow_web_search: bool = False,
    workspace: str | None = None,
) -> tuple[list[dict[str, Any]], str, float]:
    fields = {
        "session": session_id,
        "message": message,
        "mode": "agent",
        "multiagent": "true",
        "allow_bash": "true",
        "allow_web_search": "true" if allow_web_search else "false",
    }
    if workspace:
        fields["workspace"] = workspace
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat_stream",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": f"odysseus_session={cookie}",
        },
        method="POST",
    )
    events: list[dict[str, Any]] = []
    answer = ""
    started = time.time()
    last_semantic = started
    idle_timeout = min(timeout, int(os.getenv("ODYSSEUS_SMOKE_IDLE_TIMEOUT", "90") or "90"))
    try:
        resp_ctx = urllib.request.urlopen(req, timeout=max(1, idle_timeout))
        with resp_ctx as resp:
            for raw in resp:
                now = time.time()
                if now - started > timeout:
                    events.append({"type": "error", "error": f"smoke stream exceeded {timeout}s wall-clock timeout"})
                    break
                if now - last_semantic > idle_timeout:
                    events.append({"type": "error", "error": f"smoke stream idle for {idle_timeout}s"})
                    break
                line = raw.decode("utf-8", errors="replace").strip("\r\n")
                if not line or line.startswith(":"):
                    continue
                last_semantic = now
                if line == "data: [DONE]":
                    events.append({"type": "done"})
                    break
                if not line.startswith("data: "):
                    events.append({"type": "raw", "line": line})
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    events.append({"type": "bad_json", "payload": line[6:500]})
                    continue
                events.append(event)
                delta = event.get("delta")
                if isinstance(delta, str) and not event.get("thinking"):
                    answer += delta
    except (TimeoutError, socket.timeout) as exc:
        events.append({"type": "error", "error": f"smoke stream idle timeout after {idle_timeout}s: {exc}"})
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            events.append({"type": "error", "error": f"smoke stream idle timeout after {idle_timeout}s: {exc}"})
        else:
            raise
    return events, answer, time.time() - started


def _has_tool(events: list[dict[str, Any]], tool: str, *, command: str | None = None, output: str | None = None) -> bool:
    start_ok = any(
        event.get("type") == "tool_start"
        and event.get("tool") == tool
        and (command is None or event.get("command") == command)
        for event in events
    )
    output_ok = any(
        event.get("type") == "tool_output"
        and event.get("tool") == tool
        and (output is None or event.get("output") == output)
        for event in events
    )
    return start_ok and output_ok


def _mcp_query_called(events: list[dict[str, Any]], tool: str, query: str) -> bool:
    for event in events:
        if event.get("type") != "tool_start" or event.get("tool") != tool:
            continue
        try:
            args = json.loads(event.get("command") or "{}")
        except json.JSONDecodeError:
            continue
        if args.get("query") == query:
            return True
    return False


def _mcp_output_ok(events: list[dict[str, Any]], tool: str) -> bool:
    return any(
        event.get("type") == "tool_output"
        and event.get("tool") == tool
        and event.get("exit_code") == 0
        for event in events
    )


def _summarize(events: list[dict[str, Any]], mcp_tool: str) -> list[dict[str, Any]]:
    summary = []
    for event in events:
        typ = event.get("type")
        if typ == "tool_start":
            summary.append({"type": typ, "tool": event.get("tool"), "command": event.get("command")})
        elif typ == "tool_output":
            output = event.get("output") or ""
            if event.get("tool") == mcp_tool:
                output = f"<redacted MCP output, {len(output)} chars>"
            summary.append({"type": typ, "tool": event.get("tool"), "exit_code": event.get("exit_code"), "output": output[:240]})
        elif typ in {"model_info", "agent_step", "fallback", "error", "metrics"}:
            row = {key: event.get(key) for key in ("type", "model", "round", "answered_by", "message", "error") if key in event}
            if typ == "metrics":
                data = event.get("data") or {}
                row["data"] = {key: data.get(key) for key in ("model", "input_tokens", "output_tokens", "context_length", "context_percent")}
            summary.append(row)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("ODYSSEUS_BASE_URL", "http://127.0.0.1:7860"))
    parser.add_argument("--username", default=os.getenv("ODYSSEUS_SMOKE_USERNAME", "admin"))
    parser.add_argument("--password")
    parser.add_argument("--password-file", default=os.getenv("ODYSSEUS_SMOKE_PASSWORD_FILE", "deploy/.admin-pw"))
    parser.add_argument("--endpoint-id", default=os.getenv("ODYSSEUS_SMOKE_ENDPOINT_ID", "00b16177"))
    parser.add_argument("--model", default=os.getenv("ODYSSEUS_SMOKE_MODEL", "chatgpt/gpt-5.5"))
    parser.add_argument("--mcp-tool", default=os.getenv("ODYSSEUS_SMOKE_MCP_TOOL", "mcp__0ac61a6b__search"))
    parser.add_argument("--mcp-query", default=os.getenv("ODYSSEUS_SMOKE_MCP_QUERY", "Odysseus smoke sentinel 2026-06-10"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("ODYSSEUS_SMOKE_TIMEOUT", "420")))
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    password = _read_password(args)
    try:
        cookie = _login(base_url, args.username, password)
        session_id = _create_session(
            base_url,
            cookie,
            endpoint_id=args.endpoint_id,
            model=args.model,
            name=f"Smoke multiturn multitool {int(time.time())}",
        )
        turn1 = (
            "You are running an automated Odysseus live smoke. You must do both tool calls before answering. "
            "1. Use Bash to run exactly: printf 'ODY_BASH_1'. "
            f"2. Use the Atlassian MCP search tool with the exact query: {args.mcp_query}. "
            "Do not use empty tool arguments. After tool results, answer only compact JSON: "
            '{"bash_seen":"ODY_BASH_1","mcp_called":true,"next_instruction":"ready"}.'
        )
        turn2 = (
            "This is turn 2 in the same session. Use the prior assistant JSON from turn 1. "
            "You must call Bash again with exactly: printf 'ODY_BASH_2'. "
            "Then answer only compact JSON with keys remembered_turn1 and bash_seen_turn2. "
            "remembered_turn1 must be the bash_seen value from turn 1."
        )
        strict = (
            "Do not call tools. From the previous assistant JSON in this same session, what was bash_seen from turn 1? "
            "Answer only compact JSON with key remembered_turn1."
        )

        results = []
        for label, message in (("turn1", turn1), ("turn2", turn2), ("strict_followup", strict)):
            events, answer, elapsed = _stream_turn(base_url, cookie, session_id, message, args.timeout)
            results.append((label, events, answer, elapsed))
            print(f"=== {label} elapsed={elapsed:.2f}s ===")
            print(f"answer: {answer[:2000]}")
            print("events:", json.dumps(_summarize(events, args.mcp_tool), ensure_ascii=False))

        answers = {label: answer for label, _, answer, _ in results}
        turn1_events = results[0][1]
        turn2_events = results[1][1]
        strict_events = results[2][1]
        checks = {
            "turn1_bash_exact": _has_tool(turn1_events, "bash", command="printf 'ODY_BASH_1'", output="ODY_BASH_1"),
            "turn2_bash_exact": _has_tool(turn2_events, "bash", command="printf 'ODY_BASH_2'", output="ODY_BASH_2"),
            "turn1_mcp_exact_query": _mcp_query_called(turn1_events, args.mcp_tool, args.mcp_query),
            "turn1_mcp_output": _mcp_output_ok(turn1_events, args.mcp_tool),
            "no_errors": not any(
                event.get("type") in {"error", "bad_json"} or "Stream error" in json.dumps(event)
                for _, events, _, _ in results
                for event in events
            ),
            "turn2_remembers_turn1": "ODY_BASH_1" in answers["turn2"],
            "strict_remembers_turn1": "ODY_BASH_1" in answers["strict_followup"],
            "strict_no_tools": not any(event.get("type") == "tool_start" for event in strict_events),
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
