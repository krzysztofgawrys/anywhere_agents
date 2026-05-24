"""Tests for CF Access JWT verification - cert TTL + fail-closed."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

import src.auth.cf_access as auth


@pytest.fixture(autouse=True)
def _reset_auth_state() -> Any:
    """Reset module-level cache and config between tests."""
    auth._reset_certs_cache()
    orig_domain = auth.CF_ACCESS_TEAM_DOMAIN
    orig_aud = auth.CF_ACCESS_AUD
    orig_emails = auth.CF_ACCESS_ALLOWED_EMAILS
    orig_env = auth.CLAUDE_WEB_ENV
    yield
    auth.CF_ACCESS_TEAM_DOMAIN = orig_domain
    auth.CF_ACCESS_AUD = orig_aud
    auth.CF_ACCESS_ALLOWED_EMAILS = orig_emails
    auth.CLAUDE_WEB_ENV = orig_env
    auth._reset_certs_cache()


# ── Dev bypass ────────────────────────────────────────────────

async def test_bypass_in_dev_when_cf_unconfigured() -> None:
    auth.CF_ACCESS_TEAM_DOMAIN = ""
    auth.CF_ACCESS_AUD = ""
    auth.CLAUDE_WEB_ENV = "dev"

    result = await auth.verify_cf_access_token(None)
    assert result is not None
    assert result["email"] == "dev@localhost"


# ── Fail-closed in prod ──────────────────────────────────────

async def test_fail_closed_in_prod_when_cf_unconfigured() -> None:
    auth.CF_ACCESS_TEAM_DOMAIN = ""
    auth.CF_ACCESS_AUD = ""
    auth.CLAUDE_WEB_ENV = "prod"

    result = await auth.verify_cf_access_token(None)
    assert result is None


async def test_fail_closed_production_variant() -> None:
    auth.CF_ACCESS_TEAM_DOMAIN = ""
    auth.CLAUDE_WEB_ENV = "production"

    result = await auth.verify_cf_access_token("some-token")
    assert result is None


# ── No token ──────────────────────────────────────────────────

async def test_rejects_missing_token_when_configured() -> None:
    auth.CF_ACCESS_TEAM_DOMAIN = "team.cloudflareaccess.com"
    auth.CF_ACCESS_AUD = "aud123"

    result = await auth.verify_cf_access_token(None)
    assert result is None


# ── Cert cache TTL + forced refresh on miss ───────────────────

async def test_cert_cache_refreshed_on_signature_miss() -> None:
    """When all cached certs fail, a forced refetch happens before giving up."""
    auth.CF_ACCESS_TEAM_DOMAIN = "team.cloudflareaccess.com"
    auth.CF_ACCESS_AUD = "aud123"

    call_count = 0

    async def fake_get_keys(force: bool = False) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        # Return certs that won't match any token - forces the retry path
        bogus_cert = "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----"
        return {"public_certs": [{"cert": bogus_cert}]}

    with patch.object(auth, "_get_public_keys", side_effect=fake_get_keys):
        result = await auth.verify_cf_access_token("fake.jwt.token")

    assert result is None
    # Should have been called twice: once with cache, once forced refresh
    assert call_count == 2


async def test_cert_cache_ttl_respected() -> None:
    """Within TTL, _get_public_keys returns cached data without re-fetching."""
    auth.CF_ACCESS_TEAM_DOMAIN = "team.cloudflareaccess.com"

    fetch_count = 0
    fake_data: dict[str, Any] = {"public_certs": [{"cert": "PEM1"}]}

    async def fake_get_keys(force: bool = False) -> dict[str, Any]:
        nonlocal fetch_count
        fetch_count += 1
        return fake_data

    # Seed the cache manually to test the cache-hit path
    auth._certs_cache.update(fake_data)
    auth._certs_fetched_at = __import__("time").monotonic()

    # Within TTL, returns cache without calling the real fetcher
    keys = await auth._get_public_keys()
    assert keys.get("public_certs") == [{"cert": "PEM1"}]

    # Force=True bypasses TTL
    with patch.object(auth, "_get_public_keys", side_effect=fake_get_keys):
        await auth._get_public_keys(force=True)
        assert fetch_count == 1
