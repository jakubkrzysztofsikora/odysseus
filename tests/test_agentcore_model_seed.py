import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agentcore_model_seed import agentcore_public_model_alias  # noqa: E402


def test_agentcore_public_alias_does_not_use_internal_default_model(monkeypatch):
    monkeypatch.setenv("AGENTCORE_DEFAULT_MODEL", "bartowski/Qwen2.5-7B-Instruct-GGUF")
    monkeypatch.delenv("AGENTCORE_PUBLIC_MODEL_ALIAS", raising=False)
    monkeypatch.delenv("ODYSSEUS_AGENTCORE_PUBLIC_MODEL_ALIAS", raising=False)

    assert agentcore_public_model_alias() == "circitron"


def test_agentcore_public_alias_can_be_overridden(monkeypatch):
    monkeypatch.setenv("AGENTCORE_PUBLIC_MODEL_ALIAS", "circitron-beta")

    assert agentcore_public_model_alias() == "circitron-beta"
