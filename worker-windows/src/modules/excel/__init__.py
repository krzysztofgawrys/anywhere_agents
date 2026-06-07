"""Excel automation module - Office.js add-in bridge + MCP tools.

Exports ExcelModule, the WindowsModule implementation for Excel automation
via an MCP server and Office.js add-in WebSocket bridge.
"""

from src.modules.excel.mcp_server import ExcelModule

__all__ = ["ExcelModule"]
