/**
 * Modal for completing a worker's bootstrap auth flow.
 *
 * Two render paths:
 *   - flow=api_key:    text field for the user to paste the key.
 *   - flow=device_code: URL + code + status (worker is polling, we
 *                       just confirm the user has activated it).
 *
 * Submit emits `auth_provided` (api_key) or just dismisses (device_code
 * - the worker's SDK is polling the OAuth provider directly).
 * Cancel emits `auth_cancel` so the worker stops waiting.
 */

import { useState } from "react";
import { useAuthStore } from "../stores/auth";
import type { ClientMessage } from "../types";

interface Props {
  send: (msg: ClientMessage) => void;
}

export function AuthBootstrapModal({ send }: Props) {
  const modalFor = useAuthStore((s) => s.modalFor);
  const pending = useAuthStore((s) => s.pending);
  const closeModal = useAuthStore((s) => s.closeModal);
  const dismiss = useAuthStore((s) => s.dismiss);
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (!modalFor) return null;
  const entry = pending[modalFor];
  if (!entry) return null;

  const onCancel = () => {
    send({
      type: "auth_cancel",
      payload: { worker_id: entry.worker_id, request_id: entry.request_id },
    });
    dismiss(entry.worker_id);
    setApiKey("");
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (entry.flow !== "api_key") return;
    const trimmed = apiKey.trim();
    if (!trimmed) return;
    setSubmitting(true);
    send({
      type: "auth_provided",
      payload: {
        worker_id: entry.worker_id,
        request_id: entry.request_id,
        credentials: { api_key: trimmed },
      },
    });
    // We keep the modal open; auth_status will close it on completed.
    // If the worker rejects we'll see state=failed and the entry stays
    // visible with the error message.
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-lg bg-zinc-900 border border-zinc-700 shadow-2xl">
        <div className="border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-zinc-100">
              Authenticate {entry.agent_type}
            </h2>
            <button
              onClick={closeModal}
              className="text-xs text-zinc-400 hover:text-zinc-200"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
          <div className="mt-1 text-xs text-zinc-400">
            Worker: <span className="font-mono">{entry.worker_id}</span>
          </div>
        </div>

        <div className="px-4 py-4 text-sm text-zinc-200 space-y-3">
          {entry.instructions && (
            <p className="text-zinc-300">{entry.instructions}</p>
          )}

          {entry.flow === "api_key" && (
            <form onSubmit={onSubmit} className="space-y-3">
              <label className="block">
                <span className="text-xs text-zinc-400">API key</span>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  autoFocus
                  placeholder="sk-..."
                  className="mt-1 w-full rounded bg-zinc-800 border border-zinc-700 px-3 py-2 font-mono text-sm focus:outline-none focus:border-amber-500"
                />
              </label>
              {entry.state === "failed" && entry.error && (
                <div className="text-xs text-red-400">{entry.error}</div>
              )}
              <div className="flex items-center justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={onCancel}
                  className="rounded px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
                  disabled={submitting}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting || !apiKey.trim()}
                  className="rounded bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-500 disabled:opacity-50"
                >
                  {submitting ? "Sending..." : "Save"}
                </button>
              </div>
            </form>
          )}

          {entry.flow === "device_code" && (
            <div className="space-y-3">
              <div className="rounded bg-zinc-800 px-3 py-2 text-center">
                <div className="text-xs text-zinc-400">Code</div>
                <div className="font-mono text-lg text-zinc-100 tracking-widest">
                  {entry.device_code ?? "(none)"}
                </div>
              </div>
              {entry.verification_url_complete && (
                <a
                  href={entry.verification_url_complete}
                  target="_blank"
                  rel="noreferrer"
                  className="block rounded bg-amber-600 px-3 py-2 text-center text-xs font-medium text-white hover:bg-amber-500"
                >
                  Open activation page
                </a>
              )}
              {entry.verification_url && !entry.verification_url_complete && (
                <div className="text-xs text-zinc-400">
                  Visit{" "}
                  <a
                    href={entry.verification_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-amber-400 underline"
                  >
                    {entry.verification_url}
                  </a>{" "}
                  and enter the code above.
                </div>
              )}
              <div className="text-xs text-zinc-400">
                {entry.state === "polling"
                  ? "Waiting for confirmation..."
                  : "Worker is polling the auth provider."}
              </div>
              <div className="flex items-center justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={onCancel}
                  className="rounded px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
