"""Base protocol for Windows automation modules.

Each module (Excel, Outlook, Word, ...) implements this protocol. The
worker discovers registered modules on startup and:

1. Calls start() to spin up module-specific servers (MCP, WebSocket bridges).
2. Queries mcp_config() to collect MCP server entries for Claude SDK settings.
3. Calls stop() on shutdown.

Modules are self-contained: they own their MCP tool definitions, their
Office.js add-in bridge, and their static add-in files. The worker only
needs the uniform interface below.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WindowsModule(Protocol):
    """Structural contract every Windows automation module must satisfy."""

    @property
    def name(self) -> str:
        """Short identifier used in logs and config keys (e.g. 'excel')."""
        ...

    @property
    def port(self) -> int:
        """HTTPS port the module listens on for add-in traffic."""
        ...

    async def start(self) -> None:
        """Start the module's servers (MCP endpoint, WS bridge, static files).

        Called once during worker lifespan startup. Must be idempotent.
        """
        ...

    async def stop(self) -> None:
        """Gracefully shut down all module servers and connections.

        Called once during worker lifespan shutdown.
        """
        ...

    def mcp_server_instance(self) -> Any | None:
        """Return the in-process mcp.server.Server instance, or None.

        The SDK session uses this to wire MCP tools directly in-process
        (McpSdkServerConfig) without an HTTP round-trip. Return None if
        the module does not expose an in-process MCP server.
        """
        ...

    def mcp_config(self) -> dict[str, Any]:
        """Return an MCP server config entry (SSE URL) for external access.

        Used for writing to settings.json so standalone Claude CLI can
        connect to the module via SSE. Not used by the in-process session.
        """
        ...

    async def health(self) -> dict[str, Any]:
        """Return module health status.

        At minimum: ``{"status": "ok"|"degraded"|"error", "addin_connected": bool}``.
        """
        ...
