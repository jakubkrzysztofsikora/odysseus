"""Cloudflare Access admin helpers."""

from __future__ import annotations

import os


def cloudflare_admin_emails() -> set[str]:
    raw = (
        os.getenv("ODYSSEUS_CLOUDFLARE_ADMIN_EMAILS")
        or os.getenv("CLOUDFLARE_ACCESS_ADMIN_EMAILS")
        or ""
    )
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def is_cloudflare_admin(email: str | None) -> bool:
    user = str(email or "").strip().lower()
    if not user:
        return False
    admins = cloudflare_admin_emails()
    return "*" in admins or user in admins


def cloudflare_mode_enabled() -> bool:
    return os.getenv("ODYSSEUS_AUTH_MODE", "").strip().lower() == "cloudflare_access"
