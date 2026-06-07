import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import type { ClientMessage } from "../types";

type Props = {
  projectId: number;
  send: (msg: ClientMessage) => boolean;
  /** App calls this with a write function once xterm is ready. */
  onReady: (write: (b64: string) => void) => void;
  /** Called when X is clicked - only hides, does NOT kill the PTY. */
  onHide: () => void;
};

/** Accessory keys offered on touch devices - the on-screen keyboard has no
 * Esc/Tab/Ctrl/arrows, which a terminal needs constantly. Sequences are the
 * raw bytes the PTY expects. */
const ARROW_KEYS: { label: string; data: string }[] = [
  { label: "←", data: "\x1b[D" },
  { label: "↑", data: "\x1b[A" },
  { label: "↓", data: "\x1b[B" },
  { label: "→", data: "\x1b[C" },
];
const SYMBOL_KEYS = ["|", "~", "/", "\\", "-", "_", "^", "`", "*", "#"];

export function Terminal({ projectId, send, onReady, onHide }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Sticky Ctrl: armed by the accessory bar, consumed by the next single
  // character (from the bar OR the OS keyboard) which becomes a control code.
  const ctrlArmedRef = useRef(false);
  const [ctrlArmed, setCtrlArmed] = useState(false);

  const isTouch = useMemo(
    () =>
      typeof window !== "undefined" &&
      ((window.matchMedia?.("(pointer: coarse)").matches ?? false) ||
        navigator.maxTouchPoints > 0),
    []
  );

  // Keep `send` reachable from the once-only mount effect without re-running it.
  const sendRef = useRef(send);
  useEffect(() => {
    sendRef.current = send;
  }, [send]);

  // Single funnel for all terminal input. Applies a pending Ctrl modifier to
  // a printable character (e.g. Ctrl + c -> 0x03) then forwards to the PTY.
  const writeData = useCallback((data: string) => {
    let out = data;
    if (ctrlArmedRef.current) {
      ctrlArmedRef.current = false;
      setCtrlArmed(false);
      if (data.length === 1) {
        const code = data.toUpperCase().charCodeAt(0);
        // @ A-Z [ \ ] ^ _  ->  0x00-0x1f
        if (code >= 64 && code <= 95) out = String.fromCharCode(code - 64);
      }
    }
    sendRef.current({ type: "terminal_input", payload: { data: out } });
  }, []);

  const refocus = useCallback(() => textareaRef.current?.focus(), []);

  // Bootstrap xterm once on mount
  useEffect(() => {
    if (!containerRef.current) return;

    const xterm = new XTerm({
      theme: {
        background: "#0d1117",
        foreground: "#e6edf3",
        cursor: "#58a6ff",
        selectionBackground: "#264f7840",
      },
      fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", monospace',
      fontSize: 12,
      lineHeight: 1.2,
      cursorBlink: true,
      allowProposedApi: true,
    });

    const fitAddon = new FitAddon();
    xterm.loadAddon(fitAddon);
    xterm.open(containerRef.current);
    fitAddon.fit();

    xtermRef.current = xterm;
    fitRef.current = fitAddon;

    // Tune xterm's hidden input for a code-friendly mobile keyboard. xterm
    // already sets autocorrect/autocapitalize/spellcheck off; we reinforce
    // those, disable autocomplete, and hint the Enter key as "go" (run).
    const ta = containerRef.current.querySelector(
      ".xterm-helper-textarea"
    ) as HTMLTextAreaElement | null;
    if (ta) {
      ta.setAttribute("autocapitalize", "none");
      ta.setAttribute("autocorrect", "off");
      ta.setAttribute("autocomplete", "off");
      ta.setAttribute("spellcheck", "false");
      ta.setAttribute("inputmode", "text");
      ta.setAttribute("enterkeyhint", "go");
      textareaRef.current = ta;
    }

    // Expose write function to parent
    onReady((b64: string) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      xterm.write(bytes);
    });

    // Key input → backend (through the Ctrl-aware funnel)
    xterm.onData((data) => {
      writeData(data);
    });

    // Open PTY on backend
    const { cols, rows } = xterm;
    send({ type: "terminal_open", payload: { project_id: projectId, cols, rows } });

    return () => {
      send({ type: "terminal_close", payload: {} });
      xterm.dispose();
      xtermRef.current = null;
      fitRef.current = null;
      textareaRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Resize observer - tell PTY when the panel changes size
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const fit = fitRef.current;
      const xterm = xtermRef.current;
      if (!fit || !xterm) return;
      fit.fit();
      send({
        type: "terminal_resize",
        payload: { cols: xterm.cols, rows: xterm.rows },
      });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [send]);

  // Accessory-bar key press. preventDefault on pointerdown keeps focus on the
  // hidden textarea so the OS keyboard doesn't dismiss between taps.
  const pressKey = useCallback(
    (data: string) => {
      writeData(data);
      refocus();
    },
    [writeData, refocus]
  );

  const toggleCtrl = useCallback(() => {
    ctrlArmedRef.current = !ctrlArmedRef.current;
    setCtrlArmed(ctrlArmedRef.current);
    refocus();
  }, [refocus]);

  const keyBtn =
    "shrink-0 px-2.5 py-1.5 rounded text-xs font-mono bg-gray-800 text-gray-200 " +
    "active:bg-gray-700 border border-gray-700 select-none";

  return (
    <div className="flex flex-col h-full border-t border-gray-700 bg-[#0d1117]">
      {/* Panel header */}
      <div className="flex items-center justify-between px-3 py-1 bg-gray-900 border-b border-gray-700 shrink-0">
        <span className="text-xs text-gray-400 font-mono">Terminal</span>
        <button
          type="button"
          onClick={onHide}
          className="text-gray-500 hover:text-gray-200 text-xs px-1"
          aria-label="Close terminal"
        >
          ✕
        </button>
      </div>
      {/* xterm mount point */}
      <div ref={containerRef} className="flex-1 overflow-hidden p-1" />
      {/* Touch-only accessory key bar - terminal keys the soft keyboard lacks */}
      {isTouch && (
        <div className="flex gap-1.5 overflow-x-auto px-2 py-1.5 bg-gray-900 border-t border-gray-700 shrink-0">
          <button
            type="button"
            className={keyBtn}
            onPointerDown={(e) => {
              e.preventDefault();
              pressKey("\x1b");
            }}
          >
            Esc
          </button>
          <button
            type="button"
            className={keyBtn}
            onPointerDown={(e) => {
              e.preventDefault();
              pressKey("\t");
            }}
          >
            Tab
          </button>
          <button
            type="button"
            className={`${keyBtn} ${
              ctrlArmed ? "!bg-blue-600 !text-white !border-blue-500" : ""
            }`}
            aria-pressed={ctrlArmed}
            onPointerDown={(e) => {
              e.preventDefault();
              toggleCtrl();
            }}
          >
            Ctrl
          </button>
          {ARROW_KEYS.map((k) => (
            <button
              key={k.label}
              type="button"
              className={keyBtn}
              onPointerDown={(e) => {
                e.preventDefault();
                pressKey(k.data);
              }}
            >
              {k.label}
            </button>
          ))}
          {SYMBOL_KEYS.map((sym) => (
            <button
              key={sym}
              type="button"
              className={keyBtn}
              onPointerDown={(e) => {
                e.preventDefault();
                pressKey(sym);
              }}
            >
              {sym}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
