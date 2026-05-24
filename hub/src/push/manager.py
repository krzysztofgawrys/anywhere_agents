"""Web Push notification manager.

Generates VAPID keys on first run (stored in ~/.claude-web/vapid.json),
persists push subscriptions sent by the browser to
~/.claude-web/push_subs.json (so they survive hub rebuilds), and delivers
notifications via pywebpush when the agent finishes a task in the background.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import structlog
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

logger = structlog.get_logger()

_VAPID_PATH = Path(
    os.environ.get("CLAUDE_WEB_VAPID_PATH", "~/.claude-web/vapid.json")
).expanduser()
_SUBS_PATH = Path(
    os.environ.get("CLAUDE_WEB_PUSH_SUBS_PATH", "~/.claude-web/push_subs.json")
).expanduser()
_CONTACT = "mailto:admin@localhost"  # VAPID contact - required by push services


class PushManager:
    """Singleton that manages VAPID keys and push subscriptions."""

    def __init__(self) -> None:
        self._vapid = self._load_or_generate_vapid()
        # subscription endpoint → subscription dict (endpoint, keys)
        self._subscriptions: dict[str, dict[str, Any]] = self._load_subscriptions()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def vapid_public_key_b64(self) -> str:
        """Return the VAPID public key in base64url format expected by browsers."""
        raw = self._vapid.public_key.public_bytes(
            encoding=Encoding.X962,
            format=PublicFormat.UncompressedPoint,
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def add_subscription(self, subscription: dict[str, Any]) -> None:
        """Store a push subscription from a browser client."""
        endpoint = subscription.get("endpoint", "")
        if endpoint:
            existing = self._subscriptions.get(endpoint)
            self._subscriptions[endpoint] = subscription
            if existing != subscription:
                self._save_subscriptions()
            logger.info("push_subscription_added", endpoint=endpoint[:60])

    def remove_subscription(self, endpoint: str) -> None:
        if self._subscriptions.pop(endpoint, None) is not None:
            self._save_subscriptions()

    async def notify_all(self, title: str, body: str = "") -> None:
        """Send a push notification to all registered subscribers."""
        if not self._subscriptions:
            return
        payload = json.dumps({"title": title, "body": body})
        dead: list[str] = []
        for endpoint, sub in list(self._subscriptions.items()):
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=self._vapid,
                    vapid_claims={"sub": _CONTACT},
                )
                logger.info("push_sent", endpoint=endpoint[:60])
            except WebPushException as e:
                status = e.response.status_code if e.response is not None else 0
                logger.warning("push_failed", endpoint=endpoint[:60], status=status)
                if status in (403, 404, 410):
                    dead.append(endpoint)
            except Exception as e:
                logger.warning("push_error", error=str(e))
        if dead:
            for ep in dead:
                self._subscriptions.pop(ep, None)
            self._save_subscriptions()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_subscriptions(self) -> dict[str, dict[str, Any]]:
        """Load persisted subscriptions from disk. Returns empty dict on miss/error."""
        if not _SUBS_PATH.exists():
            return {}
        try:
            data = json.loads(_SUBS_PATH.read_text())
            if not isinstance(data, dict):
                logger.warning("push_subs_load_unexpected_shape")
                return {}
            # Keys are endpoints, values are subscription dicts.
            logger.info("push_subs_loaded", count=len(data))
            return data
        except Exception as e:
            logger.warning("push_subs_load_failed", error=str(e))
            return {}

    def _save_subscriptions(self) -> None:
        """Persist subscriptions to disk so they survive hub restarts."""
        try:
            _SUBS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _SUBS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._subscriptions))
            tmp.replace(_SUBS_PATH)
        except Exception as e:
            logger.warning("push_subs_save_failed", error=str(e))

    def _load_or_generate_vapid(self) -> Vapid:
        _VAPID_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _VAPID_PATH.exists():
            try:
                data = json.loads(_VAPID_PATH.read_text())
                # from_pem is a classmethod - capture the returned instance.
                v = Vapid.from_pem(data["private_pem"].encode())
                logger.info("vapid_loaded", path=str(_VAPID_PATH))
                return v
            except Exception as e:
                logger.warning("vapid_load_failed", error=str(e))
        v = Vapid()
        v.generate_keys()
        _VAPID_PATH.write_text(
            json.dumps({"private_pem": v.private_pem().decode()})
        )
        logger.info("vapid_generated", path=str(_VAPID_PATH))
        return v


# Module-level singleton
push_manager = PushManager()
