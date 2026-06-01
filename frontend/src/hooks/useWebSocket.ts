import { useCallback, useEffect, useRef, useState } from "react";
import type { ClientMessage, ServerMessage } from "../types";

const HEARTBEAT_INTERVAL_MS = 20_000;
const RECONNECT_DELAY_MS = 2_000;
/** After this many consecutive WS failures, probe HTTP to detect CF Access expiry. */
const AUTH_CHECK_AFTER_FAILURES = 3;

/**
 * Probe the current origin with an HTTP HEAD to see if Cloudflare Access
 * redirects to a login page. Returns true when the session is expired.
 */
async function checkCfAuth(): Promise<boolean> {
  try {
    const resp = await fetch("/", { method: "HEAD", redirect: "manual" });
    // CF Access redirect -> opaqueredirect (status 0) or 302/303
    if (resp.type === "opaqueredirect" || resp.status === 401 || resp.status === 403) {
      return true;
    }
    return false;
  } catch {
    // Network error - could be CF intercepting, but could also be offline.
    // Don't reload on network errors; let the normal retry handle it.
    return false;
  }
}

type Options = {
  url?: string;
  onMessage: (msg: ServerMessage) => void;
  onReconnect?: () => void;
  /** Fired on every successful WS open - first connect AND reconnects. */
  onConnect?: () => void;
};

export function useWebSocket({ url, onMessage, onReconnect, onConnect }: Options) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const pingTimerRef = useRef<number | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const handlerRef = useRef(onMessage);
  const reconnectHandlerRef = useRef(onReconnect);
  const connectHandlerRef = useRef(onConnect);
  const manuallyClosedRef = useRef(false);
  const everConnectedRef = useRef(false);
  const consecutiveFailuresRef = useRef(0);

  useEffect(() => {
    handlerRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    reconnectHandlerRef.current = onReconnect;
  }, [onReconnect]);

  useEffect(() => {
    connectHandlerRef.current = onConnect;
  }, [onConnect]);

  const send = useCallback((msg: ClientMessage) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }, []);

  const connect = useCallback(() => {
    manuallyClosedRef.current = false;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = url ?? `${protocol}//${window.location.host}/ws`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      consecutiveFailuresRef.current = 0;
      const isReconnect = everConnectedRef.current;
      everConnectedRef.current = true;
      setConnected(true);
      // Start heartbeat
      pingTimerRef.current = window.setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping", payload: {} }));
        }
      }, HEARTBEAT_INTERVAL_MS);
      if (isReconnect) {
        // onReconnect fires after a prior disconnect (tab was open, WS dropped)
        reconnectHandlerRef.current?.();
      } else {
        // onConnect fires only on the very first connection of this page load
        connectHandlerRef.current?.();
      }
    };

    ws.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data) as ServerMessage;
        handlerRef.current(parsed);
      } catch (e) {
        console.error("Failed to parse WS message", e);
      }
    };

    ws.onclose = (event) => {
      setConnected(false);
      if (pingTimerRef.current !== null) {
        clearInterval(pingTimerRef.current);
        pingTimerRef.current = null;
      }
      if (manuallyClosedRef.current) return;

      // Hub sends 1008 (Policy Violation) when CF Access JWT is invalid.
      if (event.code === 1008) {
        window.location.reload();
        return;
      }

      consecutiveFailuresRef.current++;
      if (consecutiveFailuresRef.current >= AUTH_CHECK_AFTER_FAILURES) {
        // Multiple consecutive failures - probe HTTP to check if CF
        // Access session expired (the WS upgrade gets rejected by CF
        // before reaching the hub, so we only see code 1006).
        consecutiveFailuresRef.current = 0;
        void checkCfAuth().then((expired) => {
          if (expired) {
            window.location.reload();
          } else {
            reconnectTimerRef.current = window.setTimeout(connect, RECONNECT_DELAY_MS);
          }
        });
        return;
      }

      reconnectTimerRef.current = window.setTimeout(connect, RECONNECT_DELAY_MS);
    };

    ws.onerror = () => {
      // ignore - close handler will run
    };

    wsRef.current = ws;
  }, [url]);

  const disconnect = useCallback(() => {
    manuallyClosedRef.current = true;
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    wsRef.current?.close();
    wsRef.current = null;
  }, []);

  useEffect(() => {
    connect();
    return () => {
      disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { connected, send };
}
