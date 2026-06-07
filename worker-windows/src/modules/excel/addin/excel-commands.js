/**
 * Office.js Excel command implementations.
 *
 * Each function maps a bridge command name to an Office.js Excel.run()
 * call. The bridge sends {"id", "command", "params"} and expects back
 * {"id", "result"} or {"id", "error"}.
 *
 * All operations use Excel.run() with context.sync() to execute
 * atomically. Commands do NOT change the user's selection or active
 * sheet to avoid interfering with their work.
 *
 * Commands implemented:
 *   list_sheets, get_workbook_info, read_range, read_selection,
 *   write_range, add_formula, format_range, create_chart,
 *   sort_range, auto_fit_columns
 */

/* global Excel */

// Exported as a global so taskpane.js can dispatch commands to it.
// eslint-disable-next-line no-unused-vars
var excelCommands = {

    /**
     * List all worksheet names in the active workbook.
     * @returns {Promise<string[]>} Array of sheet names.
     */
    async list_sheets() {
        return await Excel.run(async function (ctx) {
            var sheets = ctx.workbook.worksheets;
            sheets.load("items/name");
            await ctx.sync();
            return sheets.items.map(function (s) { return s.name; });
        });
    },

    /**
     * Get workbook metadata: name, sheets, active sheet, named ranges.
     * @returns {Promise<object>}
     */
    async get_workbook_info() {
        return await Excel.run(async function (ctx) {
            var wb = ctx.workbook;
            var sheets = wb.worksheets;
            var activeSheet = wb.worksheets.getActiveWorksheet();
            var names = wb.names;

            wb.load("name");
            sheets.load("items/name");
            activeSheet.load("name");
            names.load("items/name,items/value");
            await ctx.sync();

            return {
                workbook_name: wb.name,
                sheets: sheets.items.map(function (s) { return s.name; }),
                active_sheet: activeSheet.name,
                named_ranges: names.items.map(function (n) {
                    return { name: n.name, value: n.value };
                })
            };
        });
    },

    /**
     * Read cell values (and optionally formulas) from a range.
     * @param {object} params
     * @param {string} params.sheet - Worksheet name.
     * @param {string} params.range - A1-style range address.
     * @param {boolean} [params.include_formulas] - Also return formulas.
     * @returns {Promise<object>}
     */
    async read_range(params) {
        return await Excel.run(async function (ctx) {
            var r = ctx.workbook.worksheets
                .getItem(params.sheet)
                .getRange(params.range);

            r.load("values,address,rowCount,columnCount");
            if (params.include_formulas) {
                r.load("formulas");
            }
            await ctx.sync();

            var result = {
                address: r.address,
                rows: r.rowCount,
                columns: r.columnCount,
                values: r.values
            };
            if (params.include_formulas) {
                result.formulas = r.formulas;
            }
            return result;
        });
    },

    /**
     * Read the user's current selection.
     * @returns {Promise<object>} Sheet name, address, dimensions, and values.
     */
    async read_selection() {
        return await Excel.run(async function (ctx) {
            var sel = ctx.workbook.getSelectedRange();
            var activeSheet = ctx.workbook.worksheets.getActiveWorksheet();

            sel.load("values,address,rowCount,columnCount");
            activeSheet.load("name");
            await ctx.sync();

            return {
                sheet: activeSheet.name,
                address: sel.address,
                rows: sel.rowCount,
                columns: sel.columnCount,
                values: sel.values
            };
        });
    },

    /**
     * Write a 2D array of values to a range.
     * @param {object} params
     * @param {string} params.sheet - Worksheet name.
     * @param {string} params.range - A1-style range address.
     * @param {Array<Array<*>>} params.values - 2D array of values.
     * @returns {Promise<object>}
     */
    async write_range(params) {
        return await Excel.run(async function (ctx) {
            var r = ctx.workbook.worksheets
                .getItem(params.sheet)
                .getRange(params.range);

            r.values = params.values;
            await ctx.sync();

            var rowCount = params.values.length;
            var colCount = (rowCount > 0 && params.values[0]) ? params.values[0].length : 0;
            return {
                ok: true,
                cells_written: rowCount * colCount
            };
        });
    },

    /**
     * Write a formula to a single cell.
     * @param {object} params
     * @param {string} params.sheet - Worksheet name.
     * @param {string} params.cell - Cell address (e.g. "B10").
     * @param {string} params.formula - Excel formula starting with =.
     * @returns {Promise<object>}
     */
    async add_formula(params) {
        return await Excel.run(async function (ctx) {
            var r = ctx.workbook.worksheets
                .getItem(params.sheet)
                .getRange(params.cell);

            r.formulas = [[params.formula]];
            await ctx.sync();

            // Read back the computed value
            r.load("values");
            await ctx.sync();

            return {
                ok: true,
                value: r.values[0][0]
            };
        });
    },

    /**
     * Apply formatting to a range of cells.
     *
     * Supported format options: bold, italic, font_color, fill_color,
     * number_format, horizontal_alignment, font_size, borders, auto_fit.
     *
     * @param {object} params
     * @param {string} params.sheet - Worksheet name.
     * @param {string} params.range - A1-style range address.
     * @param {boolean} [params.bold]
     * @param {boolean} [params.italic]
     * @param {string} [params.font_color] - Hex color string.
     * @param {string} [params.fill_color] - Hex background color.
     * @param {string} [params.number_format] - Excel format string.
     * @param {string} [params.horizontal_alignment] - "left", "center", "right".
     * @param {number} [params.font_size] - Font size in points.
     * @param {boolean} [params.borders] - Add thin borders.
     * @param {boolean} [params.auto_fit] - Auto-fit column widths.
     * @returns {Promise<object>}
     */
    async format_range(params) {
        return await Excel.run(async function (ctx) {
            var r = ctx.workbook.worksheets
                .getItem(params.sheet)
                .getRange(params.range);

            // Font properties
            if (params.bold !== undefined) {
                r.format.font.bold = params.bold;
            }
            if (params.italic !== undefined) {
                r.format.font.italic = params.italic;
            }
            if (params.font_color) {
                r.format.font.color = params.font_color;
            }
            if (params.font_size !== undefined) {
                r.format.font.size = params.font_size;
            }

            // Fill
            if (params.fill_color) {
                r.format.fill.color = params.fill_color;
            }

            // Number format - must be applied as a 2D array, but for
            // convenience we set it uniformly across the range
            if (params.number_format) {
                r.numberFormat = [[params.number_format]];
            }

            // Alignment
            if (params.horizontal_alignment) {
                r.format.horizontalAlignment = params.horizontal_alignment;
            }

            // Borders - apply thin borders to all edges
            if (params.borders) {
                var edges = [
                    Excel.BorderIndex.edgeTop,
                    Excel.BorderIndex.edgeBottom,
                    Excel.BorderIndex.edgeLeft,
                    Excel.BorderIndex.edgeRight,
                    Excel.BorderIndex.insideHorizontal,
                    Excel.BorderIndex.insideVertical
                ];
                for (var i = 0; i < edges.length; i++) {
                    var border = r.format.borders.getItem(edges[i]);
                    border.style = Excel.BorderLineStyle.thin;
                    border.color = "#000000";
                }
            }

            await ctx.sync();

            // Auto-fit columns (requires a separate sync)
            if (params.auto_fit) {
                r.format.autofitColumns();
                await ctx.sync();
            }

            return { ok: true };
        });
    },

    /**
     * Create a chart from a data range.
     * @param {object} params
     * @param {string} params.sheet - Worksheet containing the data.
     * @param {string} params.data_range - A1-style range with headers + data.
     * @param {string} [params.chart_type="ColumnClustered"] - Chart type.
     * @param {string} [params.title] - Chart title.
     * @param {string} [params.position] - Cell for top-left placement.
     * @returns {Promise<object>}
     */
    async create_chart(params) {
        return await Excel.run(async function (ctx) {
            var ws = ctx.workbook.worksheets.getItem(params.sheet);
            var range = ws.getRange(params.data_range);
            var chartType = params.chart_type || "ColumnClustered";

            var chart = ws.charts.add(
                chartType,
                range,
                Excel.ChartSeriesBy.auto
            );

            if (params.title) {
                chart.title.text = params.title;
                chart.title.visible = true;
            }

            // Place the chart at the specified cell, or default to E2
            chart.setPosition(params.position || "E2");

            chart.load("name");
            await ctx.sync();

            return {
                ok: true,
                chart_name: chart.name
            };
        });
    },

    /**
     * Sort a range by a column.
     * @param {object} params
     * @param {string} params.sheet - Worksheet name.
     * @param {string} params.range - A1-style range to sort.
     * @param {number} [params.sort_column=0] - 0-based column index.
     * @param {boolean} [params.ascending=true] - Sort direction.
     * @param {boolean} [params.has_headers=true] - First row is headers.
     * @returns {Promise<object>}
     */
    async sort_range(params) {
        return await Excel.run(async function (ctx) {
            var r = ctx.workbook.worksheets
                .getItem(params.sheet)
                .getRange(params.range);

            var sortColumn = (params.sort_column !== undefined) ? params.sort_column : 0;
            var ascending = (params.ascending !== undefined) ? params.ascending : true;
            var hasHeaders = (params.has_headers !== undefined) ? params.has_headers : true;

            r.sort.apply(
                [{ key: sortColumn, ascending: ascending }],
                /* matchCase */ false,
                /* hasHeaders */ hasHeaders
            );
            await ctx.sync();

            return { ok: true };
        });
    },

    /**
     * Auto-fit column widths for the specified range.
     * @param {object} params
     * @param {string} params.sheet - Worksheet name.
     * @param {string} params.range - A1-style range whose columns to auto-fit.
     * @returns {Promise<object>}
     */
    async auto_fit_columns(params) {
        return await Excel.run(async function (ctx) {
            var r = ctx.workbook.worksheets
                .getItem(params.sheet)
                .getRange(params.range);

            r.format.autofitColumns();
            await ctx.sync();

            return { ok: true };
        });
    }
};
