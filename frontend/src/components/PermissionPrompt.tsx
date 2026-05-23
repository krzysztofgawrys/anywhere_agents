import type { PendingPermission } from "../stores/chat";

type Props = {
  request: PendingPermission;
  onAllow: (toolUseId: string) => void;
  onDeny: (toolUseId: string) => void;
};

export function PermissionPrompt({ request, onAllow, onDeny }: Props) {
  const summary = summarize(request.name, request.input);
  return (
    <div className="border-t border-yellow-600/40 bg-yellow-950/30 px-3 md:px-4 py-3">
      <div className="max-w-3xl lg:max-w-5xl mx-auto">
        <div className="flex items-start justify-between gap-3 mb-2">
          <div className="min-w-0">
            <div className="text-xs uppercase tracking-wide text-yellow-300/80 font-semibold">
              Permission required
            </div>
            <div className="font-mono text-sm text-white mt-1">
              <span className="text-yellow-300">{request.name}</span>
              {summary && <span className="text-gray-400"> · {summary}</span>}
            </div>
          </div>
          <div className="flex gap-2 shrink-0">
            <button
              type="button"
              onClick={() => onDeny(request.toolUseId)}
              className="px-3 py-1.5 rounded text-sm bg-gray-700 hover:bg-gray-600 text-white"
            >
              Deny
            </button>
            <button
              type="button"
              onClick={() => onAllow(request.toolUseId)}
              className="px-3 py-1.5 rounded text-sm bg-green-600 hover:bg-green-700 text-white font-medium"
            >
              Allow
            </button>
          </div>
        </div>
        <details className="text-xs">
          <summary className="cursor-pointer text-gray-400 hover:text-gray-200">
            details
          </summary>
          <pre className="mt-1 p-2 bg-black/40 rounded font-mono text-xs overflow-x-auto whitespace-pre-wrap break-words max-h-48">
            {JSON.stringify(request.input, null, 2)}
          </pre>
        </details>
      </div>
    </div>
  );
}

function summarize(_name: string, input: Record<string, unknown>): string {
  const keys = ["command", "file_path", "path", "pattern", "query"];
  for (const k of keys) {
    if (typeof input[k] === "string") {
      const s = input[k] as string;
      return s.length > 80 ? s.slice(0, 80) + "…" : s;
    }
  }
  return "";
}
