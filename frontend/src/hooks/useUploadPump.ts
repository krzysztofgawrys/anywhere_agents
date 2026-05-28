/**
 * Pump that drives the global uploads store. Mounted ONCE at the App level
 * so it survives FileBrowser unmount, sidebar navigation, etc.
 *
 * Per-item flow:
 *  1. takeNext() pulls the queue head into `current` and sets progress=0.
 *  2. We POST to /api/upload via XMLHttpRequest (fetch doesn't expose upload
 *     progress events, and the user explicitly wants a progress indicator
 *     for large files).
 *  3. On 200 → markUploaded(renamed). On 409 → markConflict (modal opens).
 *     On other failures → markFailed.
 *  4. Effect re-fires when state changes (queue/current/conflict), pulling
 *     the next item if the slot is now free.
 *
 * Why XHR instead of fetch:
 *  - `fetch` has no upload progress callback in browsers. For multi-megabyte
 *    files on a mobile uplink we want to surface progress so the user knows
 *    the upload is alive.
 *  - The Streams API workaround (ReadableStream upload + duplex: "half") has
 *    spotty mobile support; XHR is universal and ~30 lines.
 */

import { useEffect } from "react";
import {
  useUploadsStore,
  type UploadItem,
  type UploadOnConflict,
} from "../stores/uploads";

export function useUploadPump(): void {
  const queueLen = useUploadsStore((s) => s.queue.length);
  const current = useUploadsStore((s) => s.current);
  const conflict = useUploadsStore((s) => s.conflict);
  const takeNext = useUploadsStore((s) => s.takeNext);
  const markUploaded = useUploadsStore((s) => s.markUploaded);
  const markConflict = useUploadsStore((s) => s.markConflict);
  const markFailed = useUploadsStore((s) => s.markFailed);
  const setProgress = useUploadsStore((s) => s.setProgress);

  useEffect(() => {
    if (current || conflict) return;
    if (queueLen === 0) return;
    const item = takeNext();
    if (!item) return;
    // Default conflict policy for the first attempt of a queued item. If
    // it lands on a conflict, the user resolves via the modal and we
    // re-enqueue through resumeConflict with an explicit policy.
    sendUpload(item, "error", {
      onProgress: setProgress,
      onUploaded: markUploaded,
      onConflict: markConflict,
      onFailed: markFailed,
    });
    // The dependency list intentionally only watches "does an upload need
    // to start now?" - not the action refs (stable from zustand) or the
    // item identity (consumed inside).
  }, [queueLen, current, conflict, takeNext, markUploaded, markConflict, markFailed, setProgress]);
}

/** Hook returning a function the components call to retry a conflict item
 *  with a chosen policy. Lives here (next to the pump) so the XHR plumbing
 *  is in one place. */
export function useResumeUploadConflict(): (policy: "overwrite" | "rename") => void {
  const resumeConflict = useUploadsStore((s) => s.resumeConflict);
  const markUploaded = useUploadsStore((s) => s.markUploaded);
  const markConflict = useUploadsStore((s) => s.markConflict);
  const markFailed = useUploadsStore((s) => s.markFailed);
  const setProgress = useUploadsStore((s) => s.setProgress);

  return (policy: "overwrite" | "rename") => {
    const item = resumeConflict();
    if (!item) return;
    sendUpload(item, policy, {
      onProgress: setProgress,
      onUploaded: markUploaded,
      onConflict: markConflict,
      onFailed: markFailed,
    });
  };
}

type SendCallbacks = {
  onProgress: (value: number | null) => void;
  onUploaded: (renamed: boolean) => void;
  onConflict: () => void;
  onFailed: (message: string) => void;
};

function sendUpload(
  item: UploadItem,
  onConflict: UploadOnConflict,
  cbs: SendCallbacks,
): void {
  const form = new FormData();
  form.append("worker_id", item.workerId);
  form.append("project_path", item.projectPath);
  form.append("path", item.dirPath);
  form.append("filename", item.filename);
  form.append("on_conflict", onConflict);
  form.append("file", item.file, item.filename);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload", true);
  // No explicit timeout - large files over slow uplinks may legitimately
  // run for several minutes. The browser tab close / app kill is the
  // ultimate failure mode.
  xhr.timeout = 0;

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable && e.total > 0) {
      cbs.onProgress(e.loaded / e.total);
    }
  };

  xhr.onload = () => {
    // 200/201 → success; parse {path, size, renamed}.
    if (xhr.status >= 200 && xhr.status < 300) {
      let renamed = false;
      try {
        const body = JSON.parse(xhr.responseText);
        renamed = Boolean(body?.renamed);
      } catch {
        // Body parse failure is harmless - server confirmed success.
      }
      cbs.onUploaded(renamed);
      return;
    }
    // 409 from the worker means file already exists with policy=error.
    if (xhr.status === 409) {
      cbs.onConflict();
      return;
    }
    // Everything else: surface the server message if we can parse one.
    let message = `HTTP ${xhr.status}`;
    try {
      const body = JSON.parse(xhr.responseText);
      message = body?.message || body?.detail || message;
    } catch {
      if (xhr.responseText) message = xhr.responseText.slice(0, 200);
    }
    cbs.onFailed(message);
  };

  xhr.onerror = () => cbs.onFailed("network error");
  xhr.onabort = () => cbs.onFailed("aborted");
  xhr.ontimeout = () => cbs.onFailed("timeout");

  xhr.send(form);
}
