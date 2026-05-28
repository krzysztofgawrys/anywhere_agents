/**
 * App-level upload UI. Rendered ONCE in App.tsx so it persists across:
 *  - closing the FileBrowser (the upload keeps running)
 *  - sidebar navigation
 *  - switching projects (uploads target the project that was active when
 *    the file was queued, not the currently-active one)
 *
 * Three pieces stacked here:
 *  1. UploadStatusBar - sticky strip pinned to the bottom of the viewport,
 *     visible whenever queue/current/conflict is non-empty. Shows the
 *     current file + per-item progress + queued count.
 *  2. ConflictModal - opens automatically when the worker returns 409;
 *     Overwrite / Keep both / Skip.
 *  3. CompletionToast - flashes a "Uploaded N files" pill when the queue
 *     drains. Also fires a system `Notification` if the page is hidden,
 *     so a backgrounded PWA learns about the result.
 */

import { useEffect } from "react";
import { useResumeUploadConflict } from "../hooks/useUploadPump";
import { useUploadsStore } from "../stores/uploads";

export function UploadOverlay() {
  const queue = useUploadsStore((s) => s.queue);
  const current = useUploadsStore((s) => s.current);
  const conflict = useUploadsStore((s) => s.conflict);
  const progress = useUploadsStore((s) => s.progress);
  const lastCompleted = useUploadsStore((s) => s.lastCompleted);
  const cancelQueued = useUploadsStore((s) => s.cancelQueued);
  const skipConflict = useUploadsStore((s) => s.skipConflict);
  const clearLastCompleted = useUploadsStore((s) => s.clearLastCompleted);
  const resume = useResumeUploadConflict();

  // Fire a system notification on batch completion when the page is hidden
  // (PWA backgrounded). When visible, the in-page toast is enough.
  useEffect(() => {
    if (!lastCompleted) return;
    if (typeof Notification === "undefined") return;
    if (document.visibilityState === "visible") return;
    if (Notification.permission !== "granted") return;
    const { uploaded, failed, skipped } = lastCompleted;
    const total = uploaded + failed + skipped;
    const title =
      failed > 0
        ? `Upload finished with ${failed} failed`
        : `Upload finished (${uploaded}/${total})`;
    const body =
      failed > 0
        ? `Failed: ${lastCompleted.failedNames.slice(0, 3).join(", ")}`
        : skipped > 0
          ? `${uploaded} uploaded, ${skipped} skipped`
          : `${uploaded} file${uploaded === 1 ? "" : "s"} uploaded`;
    try {
      new Notification(title, { body, tag: "claude-web-upload" });
    } catch {
      // Some platforms throw if Notification is constructed without a
      // service worker - benign, we already silenced it in the foreground
      // branch above.
    }
  }, [lastCompleted]);

  // Auto-dismiss the in-page toast after a few seconds when visible. We
  // keep it sticky if the user is offscreen so they can see it on return.
  useEffect(() => {
    if (!lastCompleted) return;
    if (document.visibilityState !== "visible") return;
    const t = setTimeout(() => {
      clearLastCompleted();
    }, 6000);
    return () => clearTimeout(t);
  }, [lastCompleted, clearLastCompleted]);

  const showStrip = current !== null || queue.length > 0 || conflict !== null;
  const progressPct = progress !== null ? Math.round(progress * 100) : null;

  return (
    <>
      {showStrip && (
        <div className="fixed inset-x-0 bottom-0 z-40 border-t border-gray-800 bg-gray-950 px-3 md:px-4 py-2 flex items-center gap-3 text-sm">
          {current ? <UploadSpinner /> : <QueuedDot />}
          <div className="flex-1 min-w-0">
            <div className="truncate">
              {current ? (
                <>
                  Uploading <span className="font-mono">{current.filename}</span>
                  {progressPct !== null && (
                    <span className="text-gray-500"> · {progressPct}%</span>
                  )}
                </>
              ) : conflict ? (
                <>
                  Waiting for you to resolve <span className="font-mono">{conflict.filename}</span>
                </>
              ) : (
                <>{queue.length} file{queue.length === 1 ? "" : "s"} queued</>
              )}
              {(queue.length > 0 && current) && (
                <span className="text-gray-500"> · {queue.length} queued</span>
              )}
            </div>
            {progressPct !== null && current && (
              <div className="mt-1 h-1 w-full bg-gray-800 rounded overflow-hidden">
                <div
                  className="h-full bg-blue-500 transition-[width] duration-150"
                  style={{ width: `${progressPct}%` }}
                />
              </div>
            )}
          </div>
          {queue.length > 0 && (
            <button
              type="button"
              onClick={cancelQueued}
              className="text-xs text-gray-400 hover:text-white px-2 py-1 rounded border border-gray-700 hover:border-gray-500"
              title="Cancel files not yet uploaded (the in-flight file finishes)"
            >
              Cancel queued
            </button>
          )}
        </div>
      )}

      {conflict && (
        <ConflictModal
          filename={conflict.filename}
          dirPath={conflict.dirPath}
          onOverwrite={() => resume("overwrite")}
          onRename={() => resume("rename")}
          onSkip={skipConflict}
        />
      )}

      {lastCompleted && document.visibilityState === "visible" && (
        <CompletionToast
          uploaded={lastCompleted.uploaded}
          renamed={lastCompleted.renamed}
          skipped={lastCompleted.skipped}
          failed={lastCompleted.failed}
          failedNames={lastCompleted.failedNames}
          onDismiss={clearLastCompleted}
        />
      )}
    </>
  );
}

function UploadSpinner() {
  return (
    <svg
      className="animate-spin h-4 w-4 text-blue-400 shrink-0"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
      />
    </svg>
  );
}

function QueuedDot() {
  return <div className="w-2 h-2 rounded-full bg-gray-500 shrink-0" />;
}

type ConflictModalProps = {
  filename: string;
  dirPath: string;
  onOverwrite: () => void;
  onRename: () => void;
  onSkip: () => void;
};

function ConflictModal({
  filename,
  dirPath,
  onOverwrite,
  onRename,
  onSkip,
}: ConflictModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onSkip();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onSkip]);

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-lg bg-gray-900 border border-gray-700 shadow-xl">
        <div className="px-4 py-3 border-b border-gray-800">
          <div className="text-sm text-gray-400">File already exists</div>
          <div className="font-mono text-sm md:text-base text-gray-100 truncate" title={filename}>
            {dirPath ? `${dirPath}/${filename}` : filename}
          </div>
        </div>
        <div className="px-4 py-3 text-sm text-gray-300">
          What do you want to do with the upload?
        </div>
        <div className="px-4 pb-4 flex flex-col gap-2">
          <button
            type="button"
            onClick={onRename}
            className="w-full text-left px-3 py-2 rounded border border-gray-700 hover:border-blue-500 hover:bg-blue-950/30"
          >
            <div className="text-sm text-gray-100">Keep both</div>
            <div className="text-xs text-gray-500">
              Save as <span className="font-mono">{renameHint(filename)}</span>
            </div>
          </button>
          <button
            type="button"
            onClick={onOverwrite}
            className="w-full text-left px-3 py-2 rounded border border-gray-700 hover:border-red-500 hover:bg-red-950/30"
          >
            <div className="text-sm text-gray-100">Overwrite</div>
            <div className="text-xs text-gray-500">Replace the existing file. Cannot be undone.</div>
          </button>
          <button
            type="button"
            onClick={onSkip}
            className="w-full text-left px-3 py-2 rounded border border-gray-700 hover:border-gray-500 hover:bg-gray-800/60"
          >
            <div className="text-sm text-gray-100">Skip</div>
            <div className="text-xs text-gray-500">Don't upload this file. (Esc)</div>
          </button>
        </div>
      </div>
    </div>
  );
}

function renameHint(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot <= 0) return `${filename} (1)`;
  return `${filename.slice(0, dot)} (1)${filename.slice(dot)}`;
}

type CompletionToastProps = {
  uploaded: number;
  renamed: number;
  skipped: number;
  failed: number;
  failedNames: string[];
  onDismiss: () => void;
};

function CompletionToast({
  uploaded,
  renamed,
  skipped,
  failed,
  failedNames,
  onDismiss,
}: CompletionToastProps) {
  const hasFailed = failed > 0;
  return (
    <div
      className={`fixed left-1/2 -translate-x-1/2 bottom-20 z-50 max-w-sm px-4 py-3 rounded-lg shadow-lg border text-sm ${
        hasFailed
          ? "bg-red-950/80 border-red-700 text-red-100"
          : "bg-gray-900/95 border-gray-700 text-gray-100"
      }`}
    >
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="font-medium">
            {hasFailed
              ? `Upload finished with ${failed} failed`
              : `Uploaded ${uploaded} file${uploaded === 1 ? "" : "s"}`}
          </div>
          <div className="text-xs text-gray-300 mt-0.5">
            {[
              uploaded > 0 ? `${uploaded} uploaded` : null,
              renamed > 0 ? `${renamed} renamed` : null,
              skipped > 0 ? `${skipped} skipped` : null,
              failed > 0 ? `${failed} failed` : null,
            ]
              .filter(Boolean)
              .join(" · ")}
          </div>
          {hasFailed && failedNames.length > 0 && (
            <div className="text-xs text-red-300 mt-1 font-mono truncate">
              {failedNames.slice(0, 3).join(", ")}
              {failedNames.length > 3 ? "…" : ""}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-gray-400 hover:text-gray-100 -mr-1"
          aria-label="Dismiss"
        >
          <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor">
            <path d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" />
          </svg>
        </button>
      </div>
    </div>
  );
}
