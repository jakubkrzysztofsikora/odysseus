"""P6.2 subprocess env-strip helper tests.

The locked decision: strip ambient env on ALL subprocess spawns, not just the
sandbox. These tests pin the deny-by-default behavior so a child never inherits
provider keys / auth tokens.
"""
from src.subprocess_safe import audit_leaky_env, clean_subprocess_env


_DIRTY = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/svc",
    "LANG": "en_US.UTF-8",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "COWORK_AUTH_TOKEN": "deadbeef",
    "OPENAI_API_KEY": "sk-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "SOME_OAUTH_OBO_TOKEN": "obo",
    "RANDOM_APP_FLAG": "1",
}


def test_secrets_are_stripped():
    env = clean_subprocess_env(base_env=_DIRTY)
    assert "ANTHROPIC_API_KEY" not in env
    assert "COWORK_AUTH_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "SOME_OAUTH_OBO_TOKEN" not in env


def test_safe_vars_pass_through():
    env = clean_subprocess_env(base_env=_DIRTY)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/svc"
    assert env["LANG"] == "en_US.UTF-8"


def test_deny_by_default_unknown_nonsecret_var():
    # A var that is neither on the allowlist nor obviously secret is STILL
    # dropped — allowlist, not blocklist.
    env = clean_subprocess_env(base_env=_DIRTY)
    assert "RANDOM_APP_FLAG" not in env


def test_extra_overrides_and_adds():
    env = clean_subprocess_env({"FOO": "bar", "PATH": "/sbin"}, base_env=_DIRTY)
    assert env["FOO"] == "bar"
    assert env["PATH"] == "/sbin"


def test_explicit_passthrough_allows_named_var():
    env = clean_subprocess_env(
        base_env=_DIRTY, passthrough=["RANDOM_APP_FLAG"]
    )
    assert env["RANDOM_APP_FLAG"] == "1"
    # but still no secrets
    assert "ANTHROPIC_API_KEY" not in env


def test_computed_env_has_no_leaks():
    env = clean_subprocess_env(base_env=_DIRTY)
    assert audit_leaky_env(env) == []


def test_audit_flags_secret_shaped_names():
    leaks = audit_leaky_env(_DIRTY)
    assert "ANTHROPIC_API_KEY" in leaks
    assert "COWORK_AUTH_TOKEN" in leaks
    assert "PATH" not in leaks
