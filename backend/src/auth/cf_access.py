"""Cloudflare Access JWT verification."""

import os
from typing import Any

import httpx
import jwt
import structlog

logger = structlog.get_logger()

# CF Access config from environment
CF_ACCESS_TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "")  # e.g. "myteam.cloudflareaccess.com"
CF_ACCESS_AUD = os.getenv("CF_ACCESS_AUD", "")  # Application Audience (AUD) tag
CF_ACCESS_ALLOWED_EMAILS = os.getenv("CF_ACCESS_ALLOWED_EMAILS", "")  # comma-separated

_certs_cache: dict[str, Any] = {}


async def _get_public_keys() -> dict[str, Any]:
    """Fetch CF Access public keys (cached)."""
    if _certs_cache:
        return _certs_cache

    if not CF_ACCESS_TEAM_DOMAIN:
        return {}

    url = f"https://{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        _certs_cache.update(data)
        return data


def _is_bypass_mode() -> bool:
    """In dev mode (no CF config), skip JWT verification."""
    return not CF_ACCESS_TEAM_DOMAIN or not CF_ACCESS_AUD


async def verify_cf_access_token(token: str | None) -> dict[str, Any] | None:
    """Verify CF Access JWT. Returns claims dict or None if invalid.

    In dev mode (no CF_ACCESS_TEAM_DOMAIN configured), returns a dummy payload.
    """
    if _is_bypass_mode():
        logger.warning("cf_access_bypass", reason="no CF_ACCESS_TEAM_DOMAIN/AUD configured")
        return {"email": "dev@localhost", "sub": "dev"}

    if not token:
        logger.warning("cf_access_no_token")
        return None

    try:
        certs_data = await _get_public_keys()
        public_keys = certs_data.get("public_certs", [])
        if not public_keys:
            keys_data = certs_data.get("keys", [])
            public_keys = [{"cert": k} for k in keys_data]

        # Try each public key
        for key_data in public_keys:
            cert = key_data.get("cert", "")
            try:
                payload = jwt.decode(
                    token,
                    cert,
                    algorithms=["RS256"],
                    audience=CF_ACCESS_AUD,
                )
                # Check email allowlist
                email = payload.get("email", "")
                if CF_ACCESS_ALLOWED_EMAILS:
                    allowed = [e.strip() for e in CF_ACCESS_ALLOWED_EMAILS.split(",")]
                    if email not in allowed:
                        logger.warning("cf_access_email_denied", email=email)
                        return None

                return payload  # type: ignore[no-any-return]
            except jwt.InvalidTokenError:
                continue

        logger.warning("cf_access_no_valid_key")
        return None

    except Exception as e:
        logger.error("cf_access_verify_error", error=str(e))
        return None
