"""Tests for hub.src.auth.cf_access - Cloudflare Access JWT verification."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.auth import cf_access


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    cf_access._reset_certs_cache()


# ── Dev mode bypass ────────────────────────────────────────────────────


@patch.object(cf_access, "CLAUDE_WEB_ENV", "dev")
@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "")
@patch.object(cf_access, "CF_ACCESS_AUD", "")
async def test_dev_bypass_when_cf_not_configured() -> None:
    """Dev mode without CF config returns a dummy claims dict."""
    result = await cf_access.verify_cf_access_token(None)
    assert result is not None
    assert result["email"] == "dev@localhost"


@patch.object(cf_access, "CLAUDE_WEB_ENV", "dev")
@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "")
@patch.object(cf_access, "CF_ACCESS_AUD", "")
async def test_dev_bypass_ignores_token() -> None:
    """Dev bypass works even if a (bogus) token is passed."""
    result = await cf_access.verify_cf_access_token("bogus.jwt.token")
    assert result is not None


# ── Prod fail-closed ───────────────────────────────────────────────────


@patch.object(cf_access, "CLAUDE_WEB_ENV", "prod")
@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "")
@patch.object(cf_access, "CF_ACCESS_AUD", "")
async def test_prod_rejects_when_cf_not_configured() -> None:
    result = await cf_access.verify_cf_access_token("some.token")
    assert result is None


@patch.object(cf_access, "CLAUDE_WEB_ENV", "production")
@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "")
@patch.object(cf_access, "CF_ACCESS_AUD", "")
async def test_production_alias_also_fails_closed() -> None:
    result = await cf_access.verify_cf_access_token(None)
    assert result is None


# ── CF configured but no token ─────────────────────────────────────────


@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
@patch.object(cf_access, "CF_ACCESS_AUD", "aud-123")
async def test_rejects_missing_token() -> None:
    result = await cf_access.verify_cf_access_token(None)
    assert result is None


@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
@patch.object(cf_access, "CF_ACCESS_AUD", "aud-123")
async def test_rejects_empty_token() -> None:
    result = await cf_access.verify_cf_access_token("")
    assert result is None


# ── Email allowlist ────────────────────────────────────────────────────


@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
@patch.object(cf_access, "CF_ACCESS_AUD", "aud-123")
@patch.object(cf_access, "CF_ACCESS_ALLOWED_EMAILS", "alice@co.com, bob@co.com")
async def test_email_denied() -> None:
    """Valid JWT but email not in allowlist is rejected."""
    with patch.object(cf_access, "_get_public_keys", new_callable=AsyncMock, return_value={"public_certs": []}), \
         patch.object(cf_access, "_decode_with_any", return_value={"email": "eve@evil.com", "sub": "x"}):
        result = await cf_access.verify_cf_access_token("valid.jwt.token")
    assert result is None


@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
@patch.object(cf_access, "CF_ACCESS_AUD", "aud-123")
@patch.object(cf_access, "CF_ACCESS_ALLOWED_EMAILS", "alice@co.com, bob@co.com")
async def test_email_allowed() -> None:
    with patch.object(cf_access, "_get_public_keys", new_callable=AsyncMock, return_value={"public_certs": []}), \
         patch.object(cf_access, "_decode_with_any", return_value={"email": "alice@co.com", "sub": "x"}):
        result = await cf_access.verify_cf_access_token("valid.jwt.token")
    assert result is not None
    assert result["email"] == "alice@co.com"


# ── No allowlist = any email accepted ──────────────────────────────────


@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
@patch.object(cf_access, "CF_ACCESS_AUD", "aud-123")
@patch.object(cf_access, "CF_ACCESS_ALLOWED_EMAILS", "")
async def test_no_allowlist_accepts_any_email() -> None:
    with patch.object(cf_access, "_get_public_keys", new_callable=AsyncMock, return_value={"public_certs": []}), \
         patch.object(cf_access, "_decode_with_any", return_value={"email": "anyone@anywhere.com", "sub": "x"}):
        result = await cf_access.verify_cf_access_token("valid.jwt.token")
    assert result is not None


# ── Key rotation retry ─────────────────────────────────────────────────


@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
@patch.object(cf_access, "CF_ACCESS_AUD", "aud-123")
@patch.object(cf_access, "CF_ACCESS_ALLOWED_EMAILS", "")
async def test_retries_with_force_on_key_miss() -> None:
    """When first attempt returns None, forces a key refresh and retries."""
    call_count = {"n": 0}

    def decode_side_effect(token, keys):
        call_count["n"] += 1
        # First call fails (old keys), second succeeds (rotated keys)
        if call_count["n"] == 1:
            return None
        return {"email": "ok@co.com", "sub": "x"}

    with patch.object(cf_access, "_get_public_keys", new_callable=AsyncMock, return_value={"public_certs": []}), \
         patch.object(cf_access, "_extract_public_keys", return_value=["fake-key"]), \
         patch.object(cf_access, "_decode_with_any", side_effect=decode_side_effect):
        result = await cf_access.verify_cf_access_token("rotated.jwt")

    assert result is not None
    assert call_count["n"] == 2


# ── Exception handling ─────────────────────────────────────────────────


@patch.object(cf_access, "CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
@patch.object(cf_access, "CF_ACCESS_AUD", "aud-123")
async def test_exception_returns_none() -> None:
    with patch.object(cf_access, "_get_public_keys", new_callable=AsyncMock, side_effect=Exception("network")):
        result = await cf_access.verify_cf_access_token("some.token")
    assert result is None
