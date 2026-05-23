/** Shown immediately after a prompt is sent, before the first token streams in. */
export function TypingIndicator() {
  return (
    <div className="flex justify-start mb-3">
      <div className="bg-gray-800 text-gray-100 rounded-2xl px-4 py-3 flex items-center gap-2 text-sm text-gray-400">
        <svg
          className="shrink-0 animate-spin"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
        >
          <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
        </svg>
        <span>Thinking…</span>
      </div>
    </div>
  );
}
