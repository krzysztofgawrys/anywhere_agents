import { useState } from "react";

type Props = {
  name: string;
  input: Record<string, unknown>;
  result?: unknown;
  isError?: boolean;
};

export function ToolBlock({ name, input, result, isError }: Props) {
  const [expanded, setExpanded] = useState(false);
  const hasResult = result !== undefined;

  const summary = summarizeInput(name, input);

  return (
    <div
      className={`my-2 rounded-lg border ${
        isError ? "border-red-700 bg-red-950/30" : "border-gray-700 bg-gray-800/50"
      }`}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full text-left px-3 py-2 text-xs md:text-sm font-mono flex items-center justify-between gap-2 hover:bg-gray-700/30"
      >
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-blue-400 shrink-0">{expanded ? "▾" : "▸"}</span>
          <span className="text-yellow-400 shrink-0">{name}</span>
          <span className="text-gray-400 truncate">{summary}</span>
        </span>
        <span className="text-xs text-gray-500 shrink-0">
          {hasResult ? (isError ? "error" : "done") : "running…"}
        </span>
      </button>
      {expanded && (
        <div className="border-t border-gray-700 px-3 py-2 space-y-2">
          <div>
            <div className="text-xs text-gray-500 mb-1">input</div>
            <pre className="text-xs font-mono bg-black/40 p-2 rounded overflow-x-auto whitespace-pre-wrap break-words">
              {JSON.stringify(input, null, 2)}
            </pre>
          </div>
          {hasResult && (
            <div>
              <div className="text-xs text-gray-500 mb-1">result</div>
              <pre className="text-xs font-mono bg-black/40 p-2 rounded overflow-x-auto whitespace-pre-wrap break-words max-h-80 overflow-y-auto">
                {renderResult(result)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function summarizeInput(_name: string, input: Record<string, unknown>): string {
  const keys = ["command", "file_path", "path", "pattern", "query", "description"];
  for (const k of keys) {
    if (typeof input[k] === "string") {
      const s = input[k] as string;
      return s.length > 80 ? s.slice(0, 80) + "…" : s;
    }
  }
  return "";
}

function renderResult(result: unknown): string {
  if (typeof result === "string") return result;
  return JSON.stringify(result, null, 2);
}
