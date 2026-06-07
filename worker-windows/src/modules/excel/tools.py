"""MCP tool definitions for Excel automation.

Each tool is a thin wrapper that forwards to the AddinBridge. The bridge
dispatches the command to the connected Office.js add-in over WebSocket,
which executes it via Office.js API (Excel.run + context.sync).

Tools are registered with the MCP server in mcp_server.py.
"""

from __future__ import annotations

import json
from typing import Any

from src.modules.excel.bridge import AddinBridge


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


# ---- Tool implementations ------------------------------------------------
# Each function signature becomes the MCP tool's input schema via FastMCP.
# Docstrings become the tool descriptions visible to Claude.


async def list_sheets(bridge: AddinBridge) -> str:
    """List all worksheet names in the active Excel workbook.

    Returns a JSON array of sheet names, e.g. ["Sheet1", "Data", "Summary"].
    """
    return await _safe_execute(bridge, "list_sheets", {})


async def get_workbook_info(bridge: AddinBridge) -> str:
    """Get metadata about the active Excel workbook.

    Returns workbook name, list of sheets, active sheet name,
    and named ranges.
    """
    return await _safe_execute(bridge, "get_workbook_info", {})


async def read_range(
    bridge: AddinBridge,
    *,
    sheet: str,
    range: str,
    include_formulas: bool = False,
) -> str:
    """Read cell values from a range in the specified worksheet.

    Args:
        sheet: Worksheet name (e.g. "Sheet1").
        range: A1-style range address (e.g. "A1:D10", "B2", "A:C").
        include_formulas: If true, return formulas alongside values.

    Returns JSON with values as a 2D array, plus formulas if requested.
    """
    return await _safe_execute(
        bridge,
        "read_range",
        {"sheet": sheet, "range": range, "include_formulas": include_formulas},
    )


async def read_selection(bridge: AddinBridge) -> str:
    """Read the currently selected range in Excel.

    Returns the sheet name, range address, and cell values of whatever
    the user has selected. Useful for context-aware commands like
    "summarize what I have selected".
    """
    return await _safe_execute(bridge, "read_selection", {})


async def write_range(
    bridge: AddinBridge,
    *,
    sheet: str,
    range: str,
    values: list[list[Any]],
) -> str:
    """Write values to a range in the specified worksheet.

    Args:
        sheet: Worksheet name.
        range: A1-style range address matching the dimensions of values.
        values: 2D array of values to write. Each inner array is one row.
            Example: [["Name", "Age"], ["Alice", 30], ["Bob", 25]]
    """
    return await _safe_execute(
        bridge,
        "write_range",
        {"sheet": sheet, "range": range, "values": values},
    )


async def add_formula(
    bridge: AddinBridge,
    *,
    sheet: str,
    cell: str,
    formula: str,
) -> str:
    """Write a formula to a single cell.

    Args:
        sheet: Worksheet name.
        cell: Cell address (e.g. "B10").
        formula: Excel formula starting with = (e.g. "=SUM(B1:B9)").
    """
    return await _safe_execute(
        bridge,
        "add_formula",
        {"sheet": sheet, "cell": cell, "formula": formula},
    )


async def format_range(
    bridge: AddinBridge,
    *,
    sheet: str,
    range: str,
    bold: bool | None = None,
    italic: bool | None = None,
    font_color: str | None = None,
    fill_color: str | None = None,
    number_format: str | None = None,
    horizontal_alignment: str | None = None,
    font_size: float | None = None,
    borders: bool | None = None,
    auto_fit: bool | None = None,
) -> str:
    """Apply formatting to a range of cells.

    Args:
        sheet: Worksheet name.
        range: A1-style range address.
        bold: Set text to bold.
        italic: Set text to italic.
        font_color: Font color as hex (e.g. "#FF0000" for red).
        fill_color: Background fill color as hex.
        number_format: Excel number format string (e.g. "#,##0.00", "0%", "yyyy-mm-dd").
        horizontal_alignment: "left", "center", "right".
        font_size: Font size in points.
        borders: If true, add thin borders around all cells.
        auto_fit: If true, auto-fit column widths for the range.
    """
    params: dict[str, Any] = {"sheet": sheet, "range": range}
    if bold is not None:
        params["bold"] = bold
    if italic is not None:
        params["italic"] = italic
    if font_color is not None:
        params["font_color"] = font_color
    if fill_color is not None:
        params["fill_color"] = fill_color
    if number_format is not None:
        params["number_format"] = number_format
    if horizontal_alignment is not None:
        params["horizontal_alignment"] = horizontal_alignment
    if font_size is not None:
        params["font_size"] = font_size
    if borders is not None:
        params["borders"] = borders
    if auto_fit is not None:
        params["auto_fit"] = auto_fit
    return await _safe_execute(bridge, "format_range", params)


async def create_chart(
    bridge: AddinBridge,
    *,
    sheet: str,
    data_range: str,
    chart_type: str = "ColumnClustered",
    title: str | None = None,
    position: str | None = None,
) -> str:
    """Create a chart from data in the specified range.

    Args:
        sheet: Worksheet name containing the data.
        data_range: A1-style range with headers + data (e.g. "A1:C10").
        chart_type: One of: ColumnClustered, ColumnStacked, BarClustered,
            Line, LineMarkers, Pie, Area, XYScatter.
        title: Optional chart title.
        position: Optional cell address where the chart top-left is placed.
    """
    params: dict[str, Any] = {
        "sheet": sheet,
        "data_range": data_range,
        "chart_type": chart_type,
    }
    if title is not None:
        params["title"] = title
    if position is not None:
        params["position"] = position
    return await _safe_execute(bridge, "create_chart", params)


async def sort_range(
    bridge: AddinBridge,
    *,
    sheet: str,
    range: str,
    sort_column: int = 0,
    ascending: bool = True,
    has_headers: bool = True,
) -> str:
    """Sort a range by the specified column.

    Args:
        sheet: Worksheet name.
        range: A1-style range to sort (e.g. "A1:D100").
        sort_column: 0-based column index within the range to sort by.
        ascending: Sort direction.
        has_headers: If true, first row is treated as headers (not sorted).
    """
    return await _safe_execute(
        bridge,
        "sort_range",
        {
            "sheet": sheet,
            "range": range,
            "sort_column": sort_column,
            "ascending": ascending,
            "has_headers": has_headers,
        },
    )


async def filter_range(
    bridge: AddinBridge,
    *,
    sheet: str,
    range: str,
    column: int,
    criteria: str,
) -> str:
    """Apply an AutoFilter to a column in the specified range.

    Args:
        sheet: Worksheet name.
        range: A1-style range (e.g. "A1:D100").
        column: 0-based column index to filter.
        criteria: Filter value or criteria string.
    """
    return await _safe_execute(
        bridge,
        "filter_range",
        {"sheet": sheet, "range": range, "column": column, "criteria": criteria},
    )


async def auto_fit_columns(
    bridge: AddinBridge,
    *,
    sheet: str,
    range: str,
) -> str:
    """Auto-fit column widths for the specified range.

    Adjusts column widths so that all content in the range is fully visible.

    Args:
        sheet: Worksheet name.
        range: A1-style range whose columns to auto-fit (e.g. "A:F", "A1:D100").
    """
    return await _safe_execute(
        bridge,
        "auto_fit_columns",
        {"sheet": sheet, "range": range},
    )
