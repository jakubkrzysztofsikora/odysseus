"""Startup seed for the shared Circit AgentCore model endpoint."""

from __future__ import annotations

import json
import logging
import os

from core.database import ModelEndpoint, SessionLocal
from src.settings import load_settings, save_settings

logger = logging.getLogger(__name__)

AGENTCORE_ENDPOINT_ID = "circit_agentcore"
AGENTCORE_ENDPOINT_NAME = "Circit AgentCore"
AGENTCORE_PUBLIC_MODEL_ALIAS = "circitron"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def agentcore_public_model_alias() -> str:
    return (
        os.getenv("AGENTCORE_PUBLIC_MODEL_ALIAS")
        or os.getenv("ODYSSEUS_AGENTCORE_PUBLIC_MODEL_ALIAS")
        or AGENTCORE_PUBLIC_MODEL_ALIAS
    ).strip() or AGENTCORE_PUBLIC_MODEL_ALIAS


def _agentcore_model() -> str:
    return agentcore_public_model_alias()


def _adapter_base_url() -> str:
    return (
        os.getenv("AGENTCORE_OPENAI_BASE_URL")
        or os.getenv("ODYSSEUS_AGENTCORE_OPENAI_BASE_URL")
        or "http://127.0.0.1:7000/api/agentcore/openai/v1"
    ).strip().rstrip("/")


def should_seed_agentcore_model_endpoint() -> bool:
    if _truthy(os.getenv("ODYSSEUS_DISABLE_AGENTCORE_MODEL_SEED")):
        return False
    return bool(os.getenv("AGENTCORE_BASE_URL", "").strip())


def seed_agentcore_model_endpoint() -> None:
    """Register AgentCore as the default shared OpenAI-compatible endpoint.

    The endpoint points back at Odysseus's own loopback adapter. Browser users
    still authenticate through Cloudflare Access; server-side chat calls use a
    trusted loopback path that mints the backend gateway assertion.
    """
    if not should_seed_agentcore_model_endpoint():
        return

    model = _agentcore_model()
    models_json = json.dumps([model])
    base_url = _adapter_base_url()

    db = SessionLocal()
    try:
        endpoint = db.query(ModelEndpoint).filter(ModelEndpoint.id == AGENTCORE_ENDPOINT_ID).first()
        if endpoint is None:
            endpoint = ModelEndpoint(
                id=AGENTCORE_ENDPOINT_ID,
                name=AGENTCORE_ENDPOINT_NAME,
                base_url=base_url,
                api_key=None,
                is_enabled=True,
                cached_models=models_json,
                pinned_models=models_json,
                hidden_models=None,
                model_type="llm",
                endpoint_kind="proxy",
                model_refresh_mode="manual",
                supports_tools=True,
                owner=None,
            )
            db.add(endpoint)
        else:
            endpoint.name = AGENTCORE_ENDPOINT_NAME
            endpoint.base_url = base_url
            endpoint.is_enabled = True
            endpoint.cached_models = models_json
            endpoint.pinned_models = models_json
            endpoint.hidden_models = None
            endpoint.model_type = "llm"
            endpoint.endpoint_kind = "proxy"
            endpoint.model_refresh_mode = "manual"
            endpoint.supports_tools = True
            endpoint.owner = None
        db.commit()
    finally:
        db.close()

    settings = load_settings()
    changed = False
    for endpoint_key, model_key in (
        ("default_endpoint_id", "default_model"),
        ("utility_endpoint_id", "utility_model"),
        ("research_endpoint_id", "research_model"),
        ("task_endpoint_id", "task_model"),
    ):
        if not settings.get(endpoint_key) or settings.get(endpoint_key) == AGENTCORE_ENDPOINT_ID:
            settings[endpoint_key] = AGENTCORE_ENDPOINT_ID
            changed = True
        if settings.get(endpoint_key) == AGENTCORE_ENDPOINT_ID and settings.get(model_key) != model:
            settings[model_key] = model
            changed = True
    if changed:
        save_settings(settings)
    logger.info("Seeded shared AgentCore model endpoint %s (%s)", AGENTCORE_ENDPOINT_ID, model)
