"""Stub permission broker for the worker-copilot skeleton.

Phase 4 wires this up to github-copilot-sdk's on_permission_request callback
(taking a PermissionRequest, returning PermissionRequestResult).
"""

from __future__ import annotations


class PermissionBroker:
    """No-op placeholder - real implementation lands in phase 4."""

    def cancel_permissions(self, reason: str = "Connection closed") -> None:
        return None

    def cancel_all(self, reason: str = "Connection closed") -> None:
        return None
