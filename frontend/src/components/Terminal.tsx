import { useEffect, useRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import type { ClientMessage } from "../types";

type Props = {
  projectId: number;
  send: (msg: ClientMessage) => boolean;
  /** App calls this with a write function once xterm is ready. */
  onReady: (write: (b64: string) => void) => void;
  /** Called when X is clicked — only hides, does NOT kill the PTY. */
  onHide: () => void;
};

export function Terminal({ projectId, send, onReady, onHide }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);

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
      fontSize: 13,
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

    // Expose write function to parent
    onReady((b64: string) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      xterm.write(bytes);
    });

    // Key input → backend
    xterm.onData((data) => {
      send({ type: "terminal_input", payload: { data } });
    });

    // Open PTY on backend
    const { cols, rows } = xterm;
    send({ type: "terminal_open", payload: { project_id: projectId, cols, rows } });

    return () => {
      send({ type: "terminal_close", payload: {} });
      xterm.dispose();
      xtermRef.current = null;
      fitRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Resize observer — tell PTY when the panel changes size
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
    </div>
  );
}
