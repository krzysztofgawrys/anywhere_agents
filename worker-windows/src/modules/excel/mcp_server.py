"""Excel MCP server - exposes Excel tools over SSE transport.

Architecture:

    Claude CLI --SSE--> MCP Server --WS--> Office.js Add-in --> Excel

Runs as an HTTP server on the module's port, handling:
- GET  /sse         - SSE endpoint for MCP client (Claude CLI)
- POST /messages    - MCP message endpoint
- WS   /addin       - WebSocket for Office.js add-in connections
- GET  /taskpane/*  - Static files for the add-in Task Pane
- GET  /manifest.xml - Office Add-in manifest
- GET  /health      - Health check

The server uses the ``mcp`` Python SDK with SSE transport. Claude SDK
connects to ``http(s)://localhost:{port}/sse`` and sends tool calls to
``http(s)://localhost:{port}/messages/``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from src.modules.excel.bridge import AddinBridge

logger = structlog.get_logger()

# Static files for the Office.js add-in
_ADDIN_DIR = Path(__file__).parent / "addin"

# Default port for the Excel module
_DEFAULT_PORT = 3847

# ---- MCP tool registry ---------------------------------------------------
# Each entry defines a tool's name, description, JSON Schema params, and
# which params are required.

_TOOL_REGISTRY: list[dict[str, Any]] = [
    {
        "name": "excel_list_sheets",
        "description": "List all worksheet names in the active Excel workbook.",
        "params": {},
    },
    {
        "name": "excel_get_workbook_info",
        "description": (
            "Get metadata about the active workbook: name, list of sheets, "
            "active sheet name, and named ranges."
        ),
        "params": {},
    },
    {
        "name": "excel_read_range",
        "description": (
            "Read cell values from a range in the specified worksheet. "
            "Returns a 2D array of values. "
            "Set include_formulas=true to also return formulas."
        ),
        "params": {
            "sheet": {"type": "string", "description": "Worksheet name (e.g. 'Sheet1')"},
            "range": {"type": "string", "description": "A1-style range address (e.g. 'A1:D10')"},
            "include_formulas": {
                "type": "boolean",
                "description": "Also return formulas alongside values",
                "default": False,
            },
        },
        "required": ["sheet", "range"],
    },
    {
        "name": "excel_read_selection",
        "description": (
            "Read the user's current selection in Excel - returns the sheet name, "
            "range address, and cell values of whatever the user has selected."
        ),
        "params": {},
    },
    {
        "name": "excel_write_range",
        "description": "Write a 2D array of values to a range in the specified worksheet.",
        "params": {
            "sheet": {"type": "string", "description": "Worksheet name"},
            "range": {"type": "string", "description": "A1-style range matching dimensions of values"},
            "values": {
                "type": "array",
                "description": (
                    "2D array of values to write. Each inner array is one row. "
                    'Example: [["Name", "Age"], ["Alice", 30], ["Bob", 25]]'
                ),
                "items": {"type": "array"},
            },
        },
        "required": ["sheet", "range", "values"],
    },
    {
        "name": "excel_add_formula",
        "description": "Write an Excel formula to a single cell.",
        "params": {
            "sheet": {"type": "string", "description": "Worksheet name"},
            "cell": {"type": "string", "description": "Cell address (e.g. 'B10')"},
            "formula": {
                "type": "string",
                "description": "Excel formula starting with = (e.g. '=SUM(B1:B9)')",
            },
        },
        "required": ["sheet", "cell", "formula"],
    },
    {
        "name": "excel_format_range",
        "description": (
            "Apply formatting to a range of cells: bold, italic, font/fill colors, "
            "number format, alignment, font size, borders, auto-fit columns."
        ),
        "params": {
            "sheet": {"type": "string", "description": "Worksheet name"},
            "range": {"type": "string", "description": "A1-style range address"},
            "bold": {"type": "boolean", "description": "Set text to bold"},
            "italic": {"type": "boolean", "description": "Set text to italic"},
            "font_color": {
                "type": "string",
                "description": "Font color as hex (e.g. '#FF0000' for red)",
            },
            "fill_color": {
                "type": "string",
                "description": "Background fill color as hex",
            },
            "number_format": {
                "type": "string",
                "description": "Excel number format string (e.g. '#,##0.00', '0%', 'yyyy-mm-dd')",
            },
            "horizontal_alignment": {
                "type": "string",
                "description": "Text alignment",
                "enum": ["left", "center", "right"],
            },
            "font_size": {"type": "number", "description": "Font size in points"},
            "borders": {"type": "boolean", "description": "Add thin borders around all cells"},
            "auto_fit": {"type": "boolean", "description": "Auto-fit column widths for the range"},
        },
        "required": ["sheet", "range"],
    },
    {
        "name": "excel_create_chart",
        "description": "Create a chart from data in the specified range.",
        "params": {
            "sheet": {"type": "string", "description": "Worksheet containing the data"},
            "data_range": {
                "type": "string",
                "description": "A1-style range with headers + data (e.g. 'A1:C10')",
            },
            "chart_type": {
                "type": "string",
                "description": "Chart type",
                "enum": [
                    "ColumnClustered", "ColumnStacked", "BarClustered",
                    "Line", "LineMarkers", "Pie", "Area", "XYScatter",
                ],
                "default": "ColumnClustered",
            },
            "title": {"type": "string", "description": "Optional chart title"},
            "position": {
                "type": "string",
                "description": "Optional cell address for chart top-left placement",
            },
        },
        "required": ["sheet", "data_range"],
    },
    {
        "name": "excel_sort_range",
        "description": "Sort a range by the specified column.",
        "params": {
            "sheet": {"type": "string", "description": "Worksheet name"},
            "range": {"type": "string", "description": "A1-style range to sort (e.g. 'A1:D100')"},
            "sort_column": {
                "type": "integer",
                "description": "0-based column index within the range to sort by",
                "default": 0,
            },
            "ascending": {"type": "boolean", "description": "Sort direction", "default": True},
            "has_headers": {
                "type": "boolean",
                "description": "If true, first row is treated as headers (not sorted)",
                "default": True,
            },
        },
        "required": ["sheet", "range"],
    },
    {
        "name": "excel_auto_fit_columns",
        "description": "Auto-fit column widths for the specified range so content is fully visible.",
        "params": {
            "sheet": {"type": "string", "description": "Worksheet name"},
            "range": {
                "type": "string",
                "description": "A1-style range whose columns to auto-fit (e.g. 'A:F', 'A1:D100')",
            },
        },
        "required": ["sheet", "range"],
    },
]

# Map MCP tool name -> bridge command name sent to the add-in
_TOOL_COMMANDS: dict[str, str] = {
    "excel_list_sheets": "list_sheets",
    "excel_get_workbook_info": "get_workbook_info",
    "excel_read_range": "read_range",
    "excel_read_selection": "read_selection",
    "excel_write_range": "write_range",
    "excel_add_formula": "add_formula",
    "excel_format_range": "format_range",
    "excel_create_chart": "create_chart",
    "excel_sort_range": "sort_range",
    "excel_auto_fit_columns": "auto_fit_columns",
}


def _build_tools() -> list[Tool]:
    """Build MCP Tool objects from the registry."""
    out: list[Tool] = []
    for entry in _TOOL_REGISTRY:
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        for pname, pdef in entry.get("params", {}).items():
            schema["properties"][pname] = pdef
        if entry.get("required"):
            schema["required"] = entry["required"]
        out.append(
            Tool(
                name=entry["name"],
                description=entry["description"],
                inputSchema=schema,
            )
        )
    return out


async def _safe_execute(
    bridge: AddinBridge, command: str, params: dict[str, Any]
) -> str:
    """Execute a bridge command and return a JSON string result.

    Wraps errors in a user-friendly message rather than raising into MCP.
    """
    try:
        result = await bridge.execute(command, params)
        if "error" in result:
            return json.dumps({"error": result["error"]}, ensure_ascii=False)
        return json.dumps(result.get("result", result), ensure_ascii=False, indent=2)
    except ConnectionError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except TimeoutError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


class ExcelModule:
    """Excel automation module implementing the WindowsModule protocol.

    Combines an MCP server (SSE transport), a WebSocket bridge for the
    Office.js add-in, and static file serving for the add-in Task Pane
    into a single HTTP server.

    On start(), creates the Starlette app and runs it with uvicorn in a
    background asyncio task. On stop(), shuts down uvicorn gracefully.
    """

    def __init__(self, port: int = _DEFAULT_PORT) -> None:
        self._port = port
        self._ssl_enabled = False  # set to True in _run_server() when certs are found
        self._bridge = AddinBridge()
        self._mcp_server: Server | None = None
        self._app: Starlette | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._uvicorn_server: uvicorn.Server | None = None

    @property
    def name(self) -> str:
        """Short identifier used in logs and config keys."""
        return "excel"

    def mcp_server_instance(self) -> Server | None:
        """Return the in-process MCP Server instance for direct SDK wiring."""
        return self._mcp_server

    @property
    def port(self) -> int:
        """HTTPS port the module listens on for MCP + add-in traffic."""
        return self._port

    async def start(self) -> None:
        """Start the module's MCP server, WS bridge, and static file serving.

        Creates the Starlette app and runs it with uvicorn in a background task.
        Idempotent - calling start() twice has no effect.
        """
        if self._serve_task is not None and not self._serve_task.done():
            return
        # Detect SSL availability synchronously so mcp_config() returns the
        # correct scheme before write_mcp_settings() is called in lifespan.
        cert_dir = Path.home() / ".office-addin-dev-certs"
        self._ssl_enabled = (
            (cert_dir / "localhost.crt").exists()
            and (cert_dir / "localhost.key").exists()
        )
        self._mcp_server = self._create_mcp_server()
        self._app = self._create_app()
        self._serve_task = asyncio.create_task(self._run_server())
        logger.info("excel_module_started", port=self._port, ssl=self._ssl_enabled)

    async def stop(self) -> None:
        """Gracefully shut down uvicorn and all connections."""
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._serve_task is not None and not self._serve_task.done():
            self._serve_task.cancel()
            try:
                await self._serve_task
            except (asyncio.CancelledError, Exception):
                pass
        self._serve_task = None
        self._uvicorn_server = None
        logger.info("excel_module_stopped")

    def mcp_config(self) -> dict[str, Any]:
        """Return an MCP server config entry for Claude SDK settings.

        The returned dict is merged into the mcpServers block of the
        Claude settings file so the SDK discovers this module's tools
        automatically.
        """
        scheme = "https" if self._ssl_enabled else "http"
        return {
            "url": f"{scheme}://localhost:{self._port}/sse",
            "type": "sse",
        }

    async def health(self) -> dict[str, Any]:
        """Return module health status.

        Returns status 'ok' when the server is running and an add-in is
        connected, 'degraded' when running but no add-in, 'error' when
        the server is not running.
        """
        server_running = (
            self._serve_task is not None and not self._serve_task.done()
        )
        if not server_running:
            status = "error"
        elif not self._bridge.is_connected:
            status = "degraded"
        else:
            status = "ok"
        return {
            "status": status,
            "addin_connected": self._bridge.is_connected,
            "addin_count": self._bridge.connected_count,
            "port": self._port,
        }

    def write_mcp_settings(self) -> None:
        """Write the MCP server entry into Claude's user settings.

        Creates or updates ``~/.claude/settings.json`` with the excel
        MCP server entry so the Claude CLI discovers it on session start.
        Only touches the ``mcpServers.excel`` key; other settings are preserved.
        """
        settings_path = Path.home() / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        settings: dict[str, Any] = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        if not isinstance(settings.get("mcpServers"), dict):
            settings["mcpServers"] = {}

        settings["mcpServers"]["excel"] = self.mcp_config()

        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "excel_mcp_settings_written",
            path=str(settings_path),
            config=self.mcp_config(),
        )

    # ---- Internal helpers -------------------------------------------------

    def _create_mcp_server(self) -> Server:
        """Create the MCP Server with list_tools and call_tool handlers."""
        server = Server("excel-tools")
        bridge = self._bridge
        tools = _build_tools()

        @server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            return tools

        @server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict[str, Any] | None
        ) -> list[TextContent]:
            command = _TOOL_COMMANDS.get(name)
            if command is None:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": f"Unknown tool: {name}"}),
                )]

            # Check if an add-in is connected before dispatching
            if not bridge.is_connected:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "error": (
                            "No Excel add-in connected. Open Excel and load "
                            "the Claude Excel Agent add-in from the Task Pane, "
                            "then retry."
                        )
                    }),
                )]

            # Send the command to the add-in via the bridge, wait for result
            params = arguments or {}
            result = await _safe_execute(bridge, command, params)
            return [TextContent(type="text", text=result)]

        return server

    def _create_app(self) -> Starlette:
        """Create the Starlette ASGI app with all routes."""
        sse_transport = SseServerTransport("/messages/")
        mcp_server = self._mcp_server
        bridge = self._bridge

        async def handle_sse(request: Request) -> None:
            """SSE endpoint for MCP client (Claude CLI)."""
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await mcp_server.run(
                    streams[0],
                    streams[1],
                    mcp_server.create_initialization_options(),
                )

        async def handle_messages(request: Request) -> None:
            """MCP message endpoint (POST)."""
            await sse_transport.handle_post_message(
                request.scope, request.receive, request._send
            )

        async def handle_addin_ws(websocket: WebSocket) -> None:
            """WebSocket handler for Office.js add-in connections."""
            await websocket.accept()
            addin_id = websocket.query_params.get("id") or None
            conn = bridge.register(websocket, addin_id)
            try:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("addin_ws_invalid_json", raw=raw[:200])
                        continue
                    # Route response to the matching pending command
                    bridge.handle_message(conn.addin_id, msg)
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.warning("addin_ws_error", error=str(e))
            finally:
                bridge.unregister_by_id(conn.addin_id)

        async def handle_health(request: Request) -> JSONResponse:
            """Health check endpoint."""
            return JSONResponse({
                "status": "ok",
                "addin_connected": bridge.is_connected,
                "addin_count": bridge.connected_count,
            })

        async def handle_manifest(request: Request) -> Response:
            """Serve the Office Add-in manifest XML."""
            manifest_path = _ADDIN_DIR / "manifest.xml"
            if not manifest_path.exists():
                return Response("manifest.xml not found", status_code=404)
            content = manifest_path.read_text(encoding="utf-8")
            return Response(content, media_type="application/xml")

        routes: list[Route | WebSocketRoute | Mount] = [
            Route("/sse", endpoint=handle_sse),
            Route("/messages/", endpoint=handle_messages, methods=["POST"]),
            WebSocketRoute("/addin", endpoint=handle_addin_ws),
            Route("/health", endpoint=handle_health),
            Route("/manifest.xml", endpoint=handle_manifest),
        ]

        # Serve add-in static files if directory exists
        if _ADDIN_DIR.is_dir():
            routes.append(
                Mount(
                    "/taskpane",
                    app=StaticFiles(directory=str(_ADDIN_DIR), html=True),
                    name="addin-static",
                )
            )

        return Starlette(routes=routes)

    async def _run_server(self) -> None:
        """Run uvicorn serving the Starlette app."""
        config = uvicorn.Config(
            app=self._app,
            host="0.0.0.0",
            port=self._port,
            log_level="warning",
        )

        # Check for dev certs (created by office-addin-dev-certs or mkcert).
        # Office.js add-ins require HTTPS even for localhost.
        cert_dir = Path.home() / ".office-addin-dev-certs"
        cert_file = cert_dir / "localhost.crt"
        key_file = cert_dir / "localhost.key"
        if self._ssl_enabled:
            config.ssl_certfile = str(cert_file)
            config.ssl_keyfile = str(key_file)
            logger.info("excel_mcp_https_enabled", cert_dir=str(cert_dir))
        else:
            logger.warning(
                "excel_mcp_no_https",
                message=(
                    "No SSL certs found. Office.js add-in requires HTTPS. "
                    "Run: npx office-addin-dev-certs install "
                    "or: mkcert -install && mkcert localhost"
                ),
            )

        self._uvicorn_server = uvicorn.Server(config)
        await self._uvicorn_server.serve()
