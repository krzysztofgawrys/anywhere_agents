/**
 * Sidebar banner that lights up when one or more workers are blocked
 * waiting on credentials. Clicking a row opens the bootstrap modal.
 */

import { useAuthStore } from "../stores/auth";

export function AuthBootstrapBanner() {
  const pending = useAuthStore((s) => s.pending);
  const openModal = useAuthStore((s) => s.openModal);

  const entries = Object.values(pending);
  if (entries.length === 0) return null;

  return (
    <div className="mb-2 space-y-1">
      {entries.map((entry) => (
        <button
          key={entry.worker_id}
          onClick={() => openModal(entry.worker_id)}
          className="w-full rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-left text-xs text-amber-100 hover:bg-amber-500/20 transition"
        >
          <div className="flex items-center gap-2">
            <span aria-hidden>🔑</span>
            <span className="font-medium">
              {entry.agent_type} worker needs auth
            </span>
          </div>
          <div className="mt-0.5 text-amber-200/80">
            {entry.worker_id} - click to set up
          </div>
        </button>
      ))}
    </div>
  );
}
