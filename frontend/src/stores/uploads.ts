/**
 * Global uploads store - lives independently of the FileBrowser so that
 * closing the browser (or navigating anywhere else) does NOT cancel an
 * in-flight upload. Upload progress is rendered by `UploadStatusBar` at
 * the App level, so the user can keep working while big files transfer.
 *
 * Transport: HTTP multipart to `/api/upload` on the hub. Each upload is a
 * single `fetch` POST. We do NOT use the WebSocket here because:
 *  - WS frames cap at uvicorn's `ws_max_size` (16 MiB default). After base64
 *    inflation that's a ~12 MiB ceiling on raw file size, which is exactly
 *    the bug we hit with a 13 MiB file.
 *  - Mobile PWAs lose their WebSocket when backgrounded. `fetch` keeps the
 *    request alive across visibility changes (best-effort - the browser may
 *    still kill long-running fetches on truly dead tabs, but it survives
 *    multi-minute backgrounding far better than WS).
 *
 * State machine per item:
 *   queued → current (fetching) → done | conflict (modal) | failed
 *   conflict → current (replay with overwrite/rename) → done | failed
 *   conflict → skipped (terminal, user pressed Skip)
 */

import { create } from "zustand";

export type UploadOnConflict = "error" | "overwrite" | "rename";

export type UploadItem = {
  /** Local stable id (used for React keys and conflict resolution targeting). */
  id: string;
  /** Worker the file targets - has to come from the user's selected project. */
  workerId: string;
  /** Absolute project root on the worker (from `project.path`). The hub passes
   *  this to the worker so we don't need to remap any per-WS project_id. */
  projectPath: string;
  /** Relative directory under projectPath - "" means project root. */
  dirPath: string;
  /** Original basename (used as the on-disk filename unless renamed for
   *  conflict resolution). */
  filename: string;
  /** The actual File object - kept around so we can re-send on conflict
   *  resolution (Overwrite / Rename) without re-reading from disk. */
  file: File;
  /** Convenience for status strips. */
  sizeBytes: number;
};

/** Summary of a finished batch, used to render the completion toast or
 *  system notification. Resets when a new batch starts. */
export type BatchSummary = {
  uploaded: number;
  renamed: number;
  skipped: number;
  failed: number;
  /** Names of the failed files - included in the completion notification
   *  so the user knows which to retry. */
  failedNames: string[];
};

export type UploadsState = {
  queue: UploadItem[];
  /** Item currently being POSTed to /api/upload. */
  current: UploadItem | null;
  /** Item that returned 409 file_exists - blocks the queue until the user
   *  resolves it via the conflict modal. */
  conflict: UploadItem | null;
  /** Per-item progress (0..1) for the in-flight upload. `null` between
   *  items. Wired by the pump using XHR upload events (fetch doesn't expose
   *  upload progress). */
  progress: number | null;
  /** Aggregated counters for the current batch. Reset on first enqueue
   *  after a fully drained queue. */
  batch: BatchSummary;
  /** Set after the queue drains, until cleared by the toast component or
   *  the user. Drives the completion notification + UI toast. */
  lastCompleted: BatchSummary | null;
  /** Tick that increments whenever a file finishes uploading successfully.
   *  Consumers (e.g. FileBrowser) subscribe to refresh their listing. */
  uploadedTick: number;

  /** Append files to the queue. Starts a new batch if the queue was idle. */
  enqueue: (items: UploadItem[]) => void;
  /** Pop the queue head into `current`. Returns the item or null. */
  takeNext: () => UploadItem | null;
  /** Update progress for the in-flight item (0..1). */
  setProgress: (value: number | null) => void;
  /** Mark `current` as successfully uploaded. */
  markUploaded: (renamed: boolean) => void;
  /** Move `current` into the conflict slot (server returned 409). */
  markConflict: () => void;
  /** Mark `current` as failed (network error, 5xx, ...) and advance. */
  markFailed: (message: string) => void;
  /** Pop the conflict into `current` to be re-sent with a chosen policy. */
  resumeConflict: () => UploadItem | null;
  /** Drop the conflict (user pressed Skip). Counts as `skipped`. */
  skipConflict: () => void;
  /** Drop everything not yet sent (the in-flight item finishes naturally). */
  cancelQueued: () => void;
  /** Clear the completion toast / batch summary. */
  clearLastCompleted: () => void;
};

const _emptyBatch = (): BatchSummary => ({
  uploaded: 0,
  renamed: 0,
  skipped: 0,
  failed: 0,
  failedNames: [],
});

export const useUploadsStore = create<UploadsState>((set, get) => ({
  queue: [],
  current: null,
  conflict: null,
  progress: null,
  batch: _emptyBatch(),
  lastCompleted: null,
  uploadedTick: 0,

  enqueue: (items) =>
    set((s) => {
      const wasIdle =
        !s.current && !s.conflict && s.queue.length === 0;
      return {
        queue: [...s.queue, ...items],
        // Starting a fresh batch: clear the previous completion summary
        // (the toast for it has either been shown or dismissed already)
        // and reset counters.
        batch: wasIdle ? _emptyBatch() : s.batch,
        lastCompleted: wasIdle ? null : s.lastCompleted,
      };
    }),

  takeNext: () => {
    const { queue, current, conflict } = get();
    if (current || conflict) return null;
    const head = queue[0];
    if (!head) return null;
    set({ queue: queue.slice(1), current: head, progress: 0 });
    return head;
  },

  setProgress: (value) => set({ progress: value }),

  markUploaded: (renamed) =>
    set((s) => {
      // Aggregate into the batch summary, then check whether the queue is
      // fully drained (no queue, no conflict). If so, snapshot the batch
      // into `lastCompleted` so the App-level toast can render it.
      const nextBatch: BatchSummary = {
        ...s.batch,
        uploaded: s.batch.uploaded + 1,
        renamed: s.batch.renamed + (renamed ? 1 : 0),
      };
      const drained = s.queue.length === 0 && !s.conflict;
      return {
        current: null,
        progress: null,
        batch: drained ? _emptyBatch() : nextBatch,
        lastCompleted: drained ? nextBatch : s.lastCompleted,
        uploadedTick: s.uploadedTick + 1,
      };
    }),

  markConflict: () =>
    set((s) => {
      if (!s.current) return s;
      return {
        current: null,
        conflict: s.current,
        progress: null,
      };
    }),

  markFailed: (message) =>
    set((s) => {
      const failedName = s.current?.filename ?? "unknown";
      const nextBatch: BatchSummary = {
        ...s.batch,
        failed: s.batch.failed + 1,
        failedNames: [...s.batch.failedNames, failedName],
      };
      const drained = s.queue.length === 0 && !s.conflict;
      // Log to console so DevTools shows the error even before the toast
      // implementation surfaces it. Cheap diagnostic.
      console.warn("upload failed", failedName, message);
      return {
        current: null,
        progress: null,
        batch: drained ? _emptyBatch() : nextBatch,
        lastCompleted: drained ? nextBatch : s.lastCompleted,
      };
    }),

  resumeConflict: () => {
    const { conflict } = get();
    if (!conflict) return null;
    set({ conflict: null, current: conflict, progress: 0 });
    return conflict;
  },

  skipConflict: () =>
    set((s) => {
      if (!s.conflict) return s;
      const nextBatch: BatchSummary = {
        ...s.batch,
        skipped: s.batch.skipped + 1,
      };
      const drained = s.queue.length === 0;
      return {
        conflict: null,
        batch: drained ? _emptyBatch() : nextBatch,
        lastCompleted: drained ? nextBatch : s.lastCompleted,
      };
    }),

  cancelQueued: () => set({ queue: [], conflict: null }),

  clearLastCompleted: () => set({ lastCompleted: null }),
}));
