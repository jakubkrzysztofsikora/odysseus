"""P7.1 — caller-audit for the CONTESTED is_public_blocked_tool(None) verdict.

Plan v3.3 §7.1 left `is_public_blocked_tool(None)` as a SPLIT verdict and
ordered: "AUDIT THE CALLERS first ... do NOT blindly encode None->blocked."

AUDIT RESULT (verified against code, 2026-06-10)
------------------------------------------------
Sole production caller: src/tool_execution.py:1175
    if is_public_blocked_tool(tool) and not _owner_is_admin(owner):

`tool` originates from `block.tool_type` (tool_execution.py:1132). A ToolBlock
is only ever constructed with a concrete, non-empty tool name — the parsers in
src/tool_parsing.py and src/tool_schemas.py return `None` (no block) when they
cannot extract a name, rather than a block with tool_type None/"". So in the
real call path:
  * a malformed / non-string identifier  -> MUST be blocked (fail closed)
  * None / ""                            -> means "there is NO tool to gate",
                                            never "a blockable tool whose name
                                            went missing"

The function-name mangling concern (CopilotAgentPlugin.cs:73) is on the .NET
Host side and never feeds this Python gate. So the current behaviour
(None/"" -> False, non-string -> True) is INTENDED and correct. No one-line fix
is assigned to P1.3 for this gate.

The UNAMBIGUOUS fail-open the plan flagged is the un-applied url_security
blocklist on the LLM egress path — covered by
test_p7_llm_egress_blocklist_reduntil_p62.py, not here.

This file PINS that resolution so a future refactor that starts feeding
None/"" into the gate as a "should-block" path is caught by review.
"""
import inspect

from src.tool_security import is_public_blocked_tool, NON_ADMIN_BLOCKED_TOOLS


def test_resolved_contract_none_and_empty_are_not_gated():
    # Intended: no tool name -> nothing to gate.
    assert is_public_blocked_tool(None) is False
    assert is_public_blocked_tool("") is False


def test_resolved_contract_nonstring_is_blocked():
    # A non-string identifier cannot be validated -> fail closed.
    assert is_public_blocked_tool(5) is True
    assert is_public_blocked_tool(["bash"]) is True
    assert is_public_blocked_tool(object()) is True


def test_resolved_contract_real_blocked_tools_and_mcp_namespace():
    assert is_public_blocked_tool("bash") is True
    assert is_public_blocked_tool("manage_tokens") is True
    assert is_public_blocked_tool("mcp__anything__call") is True
    # A genuinely benign tool name stays allowed.
    assert is_public_blocked_tool("web_search") is False


def test_sole_caller_passes_block_tool_type_not_a_raw_none():
    """Guard the audit premise: the production caller reads `block.tool_type`,
    and the gate is called positionally with that value — not with a hard-coded
    None. If someone rewrites the call to pass None/"" meaning "block", this
    source-level assertion fails and forces a re-audit.
    """
    import src.tool_execution as te

    src = inspect.getsource(te.execute_tool_block)
    # The gate is invoked on the parsed tool name (first positional arg). The
    # optional second arg is the owner, used only to honor explicit per-user
    # privilege grants — it can never turn a non-blocked tool INTO a blocked one.
    assert "is_public_blocked_tool(tool, owner)" in src
    # `tool` is bound from the parsed block, never to a literal None/"".
    assert "tool = block.tool_type" in src


def test_blocklist_is_nonempty_and_covers_runtime_surface():
    # Sanity: the blocklist still gates the dangerous runtime tools the audit
    # relied on (bash/python/file IO + token/secret management).
    for t in ("bash", "python", "read_file", "write_file", "manage_tokens",
              "vault_unlock", "send_email"):
        assert t in NON_ADMIN_BLOCKED_TOOLS


def test_owner_arg_only_relaxes_for_explicit_privilege_grant(monkeypatch):
    """The owner arg may only UNLOCK a tool the owner was explicitly granted via
    an auth.json privilege flag; it must never block a tool that is otherwise
    allowed, and must not unlock tools outside the delegable family."""
    import src.tool_security as ts

    # A non-admin owner with can_use_bash granted unlocks ONLY the shell/file
    # family; manage_*/email/vault stay admin-only.
    monkeypatch.setattr(ts, "_owner_privileges",
                        lambda o: {"can_use_bash": True})
    assert ts.is_public_blocked_tool("bash", "granted") is False
    assert ts.is_public_blocked_tool("python", "granted") is False
    assert ts.is_public_blocked_tool("read_file", "granted") is False
    assert ts.is_public_blocked_tool("manage_tokens", "granted") is True
    assert ts.is_public_blocked_tool("send_email", "granted") is True
    assert ts.is_public_blocked_tool("vault_unlock", "granted") is True

    # No grant -> the full blocklist still applies.
    monkeypatch.setattr(ts, "_owner_privileges", lambda o: {})
    assert ts.is_public_blocked_tool("bash", "plain") is True

    # The owner arg never turns a benign tool INTO a blocked one.
    assert ts.is_public_blocked_tool("web_search", "granted") is False
