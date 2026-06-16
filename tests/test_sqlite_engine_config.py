from sqlalchemy.pool import NullPool

from core.database import _engine_kwargs, _sqlite_busy_timeout_ms


def test_sqlite_engine_uses_busy_timeout_and_null_pool(monkeypatch):
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_SECONDS", "12.5")

    kwargs = _engine_kwargs("sqlite:///tmp/test.db")

    assert kwargs["connect_args"]["check_same_thread"] is False
    assert kwargs["connect_args"]["timeout"] == 12.5
    assert kwargs["poolclass"] is NullPool


def test_non_sqlite_engine_keeps_default_pooling():
    assert _engine_kwargs("postgresql://example/test") == {}


def test_sqlite_busy_timeout_ms_uses_seconds_env(monkeypatch):
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_SECONDS", "4.25")

    assert _sqlite_busy_timeout_ms() == 4250
