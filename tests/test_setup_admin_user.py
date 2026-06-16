import importlib.util
import json
from pathlib import Path


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("odysseus_setup_under_test", Path("setup.py"))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_create_default_admin_normalizes_env_username(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    monkeypatch.setattr(setup_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_ADMIN_USER", " AdminUser ")
    monkeypatch.setenv("ODYSSEUS_ADMIN_PASSWORD", "temporary-password")

    assert setup_module.create_default_admin() == "created"

    auth_path = tmp_path / "auth.json"
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "adminuser" in data["users"]
    assert "AdminUser" not in data["users"]


def test_create_default_admin_skips_local_user_in_cloudflare_access_mode(tmp_path, monkeypatch, capsys):
    setup_module = _load_setup_module()
    monkeypatch.setattr(setup_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_AUTH_MODE", "cloudflare_access")

    assert setup_module.create_default_admin() == "cloudflare_access"

    output = capsys.readouterr().out
    assert "local admin setup disabled" in output
    assert "Temporary password" not in output
    assert not (tmp_path / "auth.json").exists()
