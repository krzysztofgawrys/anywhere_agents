"""Backward-compatible re-export of ExcelModule.

ExcelModule lives in mcp_server.py. This module exists so that
``from src.modules.excel.module import ExcelModule`` continues to
work for code that imports from this path (e.g. main.py).
"""

from src.modules.excel.mcp_server import ExcelModule

__all__ = ["ExcelModule"]
