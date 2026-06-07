/**
 * Office.js Add-in - Task Pane entry point.
 *
 * Connects to the Excel MCP server via WebSocket, receives commands
 * dispatched from Claude's MCP tool calls, executes them using the
 * excelCommands module (Office.js API), and returns results.
 *
 * Message protocol:
 *   Incoming: {"id": "uuid", "command": "read_range", "params": {"sheet": "Sheet1", "range": "A1:D10"}}
 *   Outgoing: {"id": "uuid", "result": {"values": [...]}}
 *          or {"id": "uuid", "error": "message"}
 *
 * The add-in acts as a transparent bridge: it has no UI of its own
 * beyond a status indicator and activity log. All intelligence lives
 * in Claude; the add-in just executes.
 */

/* global Office, excelCommands */

(function () {
    "use strict";

    // ---- Configuration ---------------------------------------------------

    // Server runs on the same host that served this page.
    // Use wss: when the page was served over https:, ws: otherwise.
    var WS_PROTOCOL = location.protocol === "https:" ? "wss:" : "ws:";
    var WS_URL = WS_PROTOCOL + "//" + location.host + "/addin";

    // Exponential backoff settings for reconnection
    var RECONNECT_BASE_MS = 1000;
    var RECONNECT_MAX_MS = 30000;

    // ---- State -----------------------------------------------------------

    var ws = null;
    var reconnectDelay = RECONNECT_BASE_MS;
    var reconnectTimer = null;
    var commandCount = 0;

    // ---- UI helpers ------------------------------------------------------

    /**
     * Update the status indicator dot and text.
     * @param {"connected"|"disconnected"|"connecting"} state
     * @param {string} text
     */
    function setStatus(state, text) {
        var dot = document.getElementById("statusDot");
        var label = document.getElementById("statusText");
        if (dot) {
            dot.className = "status-dot " + state;
        }
        if (label) {
            label.className = "status-text " + state;
            label.textContent = text;
        }
    }

    /**
     * Add an entry to the scrollable command log in the UI.
     * @param {string} text
     * @param {"cmd"|"result"|"error"|"info"} cls
     */
    function logEntry(text, cls) {
        var container = document.getElementById("logContainer");
        if (!container) {
            return;
        }

        var entry = document.createElement("div");
        entry.className = "log-entry " + (cls || "info");

        var ts = new Date().toLocaleTimeString();
        var span = document.createElement("span");
        span.className = "timestamp";
        span.textContent = ts + " ";
        entry.appendChild(span);

        var content = document.createTextNode(text);
        entry.appendChild(content);

        // Newest entries at the top
        if (container.firstChild) {
            container.insertBefore(entry, container.firstChild);
        } else {
            container.appendChild(entry);
        }

        // Keep the log manageable - trim old entries
        while (container.children.length > 200) {
            container.removeChild(container.lastChild);
        }
    }

    // ---- WebSocket -------------------------------------------------------

    /**
     * Establish a WebSocket connection to the MCP server bridge.
     * On success, resets the reconnect delay. On close, schedules
     * reconnection with exponential backoff.
     */
    function connect() {
        // Clear any pending reconnect timer
        if (reconnectTimer !== null) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }

        setStatus("connecting", "Connecting...");

        try {
            ws = new WebSocket(WS_URL);
        } catch (e) {
            logEntry("Failed to create WebSocket: " + e.message, "error");
            scheduleReconnect();
            return;
        }

        ws.onopen = function () {
            reconnectDelay = RECONNECT_BASE_MS;
            setStatus("connected", "Connected to Claude agent");
            logEntry("WebSocket connected", "result");
        };

        ws.onclose = function (event) {
            ws = null;
            var reason = event.reason || "no reason";
            setStatus("disconnected", "Disconnected (code " + event.code + ")");
            logEntry("Disconnected: code=" + event.code + " " + reason, "error");
            scheduleReconnect();
        };

        ws.onerror = function () {
            // The onerror event fires before onclose; just log it.
            logEntry("WebSocket error", "error");
        };

        ws.onmessage = function (event) {
            var msg;
            try {
                msg = JSON.parse(event.data);
            } catch (e) {
                logEntry("Invalid JSON from server", "error");
                return;
            }
            handleCommand(msg);
        };
    }

    /**
     * Schedule a reconnection attempt with exponential backoff.
     * Delay doubles each attempt, capped at RECONNECT_MAX_MS.
     */
    function scheduleReconnect() {
        var delay = reconnectDelay;
        reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
        logEntry("Reconnecting in " + (delay / 1000) + "s...", "info");
        reconnectTimer = setTimeout(function () {
            reconnectTimer = null;
            connect();
        }, delay);
    }

    // ---- Command dispatch ------------------------------------------------

    /**
     * Handle an incoming command from the MCP server bridge.
     * Dispatches to the matching excelCommands handler and sends
     * the result (or error) back over the WebSocket.
     *
     * @param {{id: string, command: string, params: object}} msg
     */
    async function handleCommand(msg) {
        var id = msg.id;
        var command = msg.command;
        var params = msg.params || {};

        if (!id || !command) {
            logEntry("Invalid command message (missing id or command)", "error");
            return;
        }

        commandCount++;
        var paramStr = JSON.stringify(params);
        if (paramStr.length > 100) {
            paramStr = paramStr.substring(0, 97) + "...";
        }
        logEntry("#" + commandCount + " " + command + "(" + paramStr + ")", "cmd");

        // Look up the handler in excel-commands.js
        var handler = excelCommands[command];
        if (!handler) {
            var errMsg = "Unknown command: " + command;
            logEntry("#" + commandCount + " ERROR: " + errMsg, "error");
            sendResponse(id, null, errMsg);
            return;
        }

        try {
            var result = await handler(params);
            logEntry("#" + commandCount + " OK", "result");
            sendResponse(id, result, null);
        } catch (error) {
            var errorMessage = error.message || String(error);
            logEntry("#" + commandCount + " ERROR: " + errorMessage, "error");
            sendResponse(id, null, errorMessage);
        }
    }

    /**
     * Send a response message back to the MCP server bridge.
     *
     * @param {string} id - The call_id from the original command
     * @param {*} result - The result data (if success)
     * @param {string|null} error - The error message (if failure)
     */
    function sendResponse(id, result, error) {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            logEntry("Cannot send response - WebSocket not open", "error");
            return;
        }

        var msg = { id: id };
        if (error !== null && error !== undefined) {
            msg.error = error;
        } else {
            msg.result = result;
        }
        ws.send(JSON.stringify(msg));
    }

    // ---- Office.js initialization ----------------------------------------

    Office.onReady(function (info) {
        if (info.host === Office.HostType.Excel) {
            logEntry("Office.js ready (Excel)", "result");
            connect();
        } else {
            setStatus("disconnected", "This add-in requires Excel");
            logEntry("Unsupported Office host: " + info.host, "error");
        }
    });

})();
