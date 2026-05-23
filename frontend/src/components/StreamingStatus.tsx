import type { StreamingActivity } from "../stores/chat";

type Props = {
  activity: StreamingActivity;
};

/** Icons for each activity kind */
function ActivityIcon({ kind }: { kind: StreamingActivity["kind"] }) {
  if (kind === "thinking") {
    return (
      <svg
        className="shrink-0 animate-spin"
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      >
        <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
      </svg>
    );
  }
  if (kind === "tool") {
    return (
      <svg
        className="shrink-0 animate-spin"
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
      </svg>
    );
  }
  if (kind === "text") {
    return (
      <span className="flex gap-0.5 items-center shrink-0">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </span>
    );
  }
  // waiting
  return (
    <svg
      className="shrink-0 animate-pulse"
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="currentColor"
    >
      <circle cx="12" cy="12" r="5" />
    </svg>
  );
}

function activityLabel(activity: StreamingActivity): string {
  switch (activity.kind) {
    case "waiting":
      return "Sending…";
    case "thinking":
      return "Thinking…";
    case "text":
      return "Generating…";
    case "tool": {
      // Pretty-print well-known tool names
      const nameMap: Record<string, string> = {
        Bash: "Running",
        Read: "Reading",
        Write: "Writing",
        Edit: "Editing",
        MultiEdit: "Editing",
        Glob: "Searching",
        Grep: "Searching",
        WebFetch: "Fetching",
        WebSearch: "Searching",
        Agent: "Spawning agent",
        Task: "Running task",
        TodoWrite: "Updating todos",
      };
      const verb = nameMap[activity.name] ?? activity.name;
      return activity.detail && activity.detail !== activity.name
        ? `${verb} · ${activity.detail}`
        : verb;
    }
  }
}

/**
 * Slim one-line status bar shown above the composer while Claude is working.
 * Disappears when `result` arrives (caller removes it by not rendering).
 */
export function StreamingStatus({ activity }: Props) {
  if (activity.kind === "text") return null; // text is self-evident — don't duplicate

  const label = activityLabel(activity);
  const isWaiting = activity.kind === "waiting";
  const isTool = activity.kind === "tool";

  return (
    <div
      className={`flex items-center gap-2 px-4 py-1.5 text-xs border-t select-none ${
        isTool
          ? "bg-amber-950/20 border-gray-700/50 text-amber-400/90"
          : isWaiting
          ? "bg-gray-800/50 border-gray-700/50 text-gray-500"
          : "bg-gray-800/50 border-gray-700/50 text-blue-400/80"
      }`}
    >
      <ActivityIcon kind={activity.kind} />
      <span className="truncate">{label}</span>
    </div>
  );
}
