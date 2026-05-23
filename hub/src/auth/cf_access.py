"""Cloudflare Access JWT verification.

Security model:
- WS handshake carries `Cf-Access-Jwt-Assertion`. We verify signature (RS256),
  audience and expiry against Cloudflare's published signing keys.
- Signing keys ROTATE. We cache them with a TTL and additionally force a
  refresh when a token fails to verify against every cached key (handles
  rotation immediately instead of waiting for the backend to restart).
- Fail-closed in production: when CLAUDE_WEB_ENV=prod and CF Access is not
  configured, every connection is rejected. The dev bypass only applies in
  the default (dev) environment.
"""

import os
import time
from typing import Any

import httpx
import jwt
import structlog

logger = structlog.get_logger()

# CF Access config from environment
CF_ACCESS_TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "")  # e.g. "myteam.cloudflareaccess.com"
CF_ACCESS_AUD = os.getenv("CF_ACCESS_AUD", "")  # Application Audience (AUD) tag
CF_ACCESS_ALLOWED_EMAILS = os.getenv("CF_ACCESS_ALLOWED_EMAILS", "")  # comma-separated

# Deployment environment. "prod"/"production" => auth is mandatory (fail-closed).
CLAUDE_WEB_ENV = os.getenv("CLAUDE_WEB_ENV", "dev").strip().lower()

# How long cached signing keys stay valid before a forced refetch.
CERTS_TTL_SECONDS = 3600

_certs_cache: dict[str, Any] = {}
_certs_fetched_at: float = 0.0


def _is_prod() -> bool:
    return CLAUDE_WEB_ENV in ("prod", "production")


def _cf_configured() -> bool:
    return bool(CF_ACCESS_TEAM_DOMAIN) and bool(CF_ACCESS_AUD)


def _reset_certs_cache() -> None:
    """Test helper — clear the in-memory key cache."""
    global _certs_fetched_at
    _certs_cache.clear()
    _certs_fetched_at = 0.0


async def _get_public_keys(force: bool = False) -> dict[str, Any]:
    """Fetch CF Access signing keys, cached with a TTL.

    `force=True` bypasses the cache (used after a verification miss to pick up
    rotated keys immediately).
    """
    global _certs_fetched_at

    age = time.monotonic() - _certs_fetched_at
    if not force and _certs_cache and age < CERTS_TTL_SECONDS:
        return _certs_cache

    if not CF_ACCESS_TEAM_DOMAIN:
        return {}

    url = f"https://{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    _certs_cache.clear()
    _certs_cache.update(data)
    _certs_fetched_at = time.monotonic()
    return _certs_cache


def _extract_public_keys(certs_data: dict[str, Any]) -> list[Any]:
    """Extract RSA public keys from the CF certs response.

    CF returns X509 certificates (BEGIN CERTIFICATE), but PyJWT 2.x expects
    raw public keys. We extract them via cryptography.
    """
    from cryptography.x509 import load_pem_x509_certificate

    raw_certs = certs_data.get("public_certs", [])
    pems = [k.get("cert", "") for k in raw_certs if k.get("cert")]
    if not pems:
        pems = [k for k in certs_data.get("keys", []) if isinstance(k, str)]

    keys: list[Any] = []
    for pem in pems:
        try:
            x509 = load_pem_x509_certificate(pem.encode())
            keys.append(x509.public_key())
        except Exception:
            logger.warning("cf_access_cert_parse_failed", cert_prefix=pem[:40])
    return keys


def _decode_with_any(token: str, keys: list[Any]) -> dict[str, Any] | None:
    """Try to verify `token` against any of the supplied public keys."""
    for key in keys:
        try:
            return jwt.decode(  # type: ignore[no-any-return]
                token,
                key,
                algorithms=["RS256"],
                audience=CF_ACCESS_AUD,
            )
        except jwt.InvalidSignatureError:
            continue  # wrong key — try the next
        except jwt.InvalidTokenError:
            # aud/exp/format invalid — no other key will help
            return None
    return None


async def verify_cf_access_token(token: str | None) -> dict[str, Any] | None:
    """Verify a CF Access JWT. Returns claims dict, or None if invalid/denied.

    - CF configured: always enforce (dev or prod).
    - CF not configured + prod: reject everything (fail-closed).
    - CF not configured + dev: bypass with a loud warning (returns a dummy).
    """
    if not _cf_configured():
        if _is_prod():
            logger.error(
                "cf_access_fail_closed",
                reason="CLAUDE_WEB_ENV=prod but CF_ACCESS_TEAM_DOMAIN/AUD unset",
            )
            return None
        logger.warning(
            "cf_access_bypass",
            reason="no CF_ACCESS_TEAM_DOMAIN/AUD configured (dev mode)",
        )
        return {"email": "dev@localhost", "sub": "dev"}

    if not token:
        logger.warning("cf_access_no_token")
        return None

    try:
        certs = _extract_public_keys(await _get_public_keys())
        payload = _decode_with_any(token, certs)

        # No cert matched — keys may have rotated. Force a refresh and retry once.
        if payload is None:
            certs = _extract_public_keys(await _get_public_keys(force=True))
            payload = _decode_with_any(token, certs)

        if payload is None:
            logger.warning("cf_access_no_valid_key")
            return None

        email = payload.get("email", "")
        if CF_ACCESS_ALLOWED_EMAILS:
            allowed = [e.strip() for e in CF_ACCESS_ALLOWED_EMAILS.split(",")]
            if email not in allowed:
                logger.warning("cf_access_email_denied", email=email)
                return None

        return payload

    except Exception as e:
        logger.error("cf_access_verify_error", error=str(e))
        return None
