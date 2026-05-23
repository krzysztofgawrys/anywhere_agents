import { useState } from "react";
import { DiffView } from "./DiffView";

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
  const diff = extractDiff(name, input);

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
        <span className="text-xs shrink-0 flex items-center gap-1.5">
          {hasResult ? (
            isError ? (
              <span className="text-red-400">error</span>
            ) : (
              <span className="text-green-500">done</span>
            )
          ) : (
            <>
              <svg
                className="animate-spin text-amber-400"
                width="11"
                height="11"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="3"
                strokeLinecap="round"
              >
                <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
              </svg>
              <span className="text-amber-400">running</span>
            </>
          )}
        </span>
      </button>
      {expanded && (
        <div className="border-t border-gray-700 px-3 py-2 space-y-2">
          {diff ? (
            <div>
              <div className="text-xs text-gray-500 mb-1">{diff.label}</div>
              <DiffView before={diff.before} after={diff.after} />
            </div>
          ) : (
            <div>
              <div className="text-xs text-gray-500 mb-1">input</div>
              <pre className="text-xs font-mono bg-black/40 p-2 rounded overflow-x-auto whitespace-pre-wrap break-words">
                {JSON.stringify(input, null, 2)}
              </pre>
            </div>
          )}
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

/** Recognise tool calls that should render as a diff. */
function extractDiff(
  name: string,
  input: Record<string, unknown>
): { label: string; before: string; after: string } | null {
  if (name === "Edit" || name === "MultiEdit") {
    const oldStr = typeof input.old_string === "string" ? (input.old_string as string) : "";
    const newStr = typeof input.new_string === "string" ? (input.new_string as string) : "";
    if (oldStr || newStr) {
      const file = typeof input.file_path === "string" ? (input.file_path as string) : "edit";
      return { label: file, before: oldStr, after: newStr };
    }
  }
  if (name === "Write") {
    const content = typeof input.content === "string" ? (input.content as string) : "";
    if (content) {
      const file = typeof input.file_path === "string" ? (input.file_path as string) : "new file";
      return { label: file, before: "", after: content };
    }
  }
  return null;
}
