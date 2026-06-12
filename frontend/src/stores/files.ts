import { create } from "zustand";
import type { DirectoryEntry, ServerMessage } from "../types";

export type ProjectRef = {
  id: number;
  name: string;
  path: string;
  /** Worker the project lives on - needed by the global uploads pipeline so
   *  it can target the right hub→worker route. May be undefined for older
   *  callers that haven't been updated to pass it. */
  workerId?: string;
};

export type DirectoryView = {
  path: string;
  parent: string | null;
  entries: DirectoryEntry[];
};

export type FileView = {
  path: string;
  size: number;
  tooLarge: boolean;
  encoding: "utf-8" | "base64" | null;
  content: string | null;
};

// Upload state lives in stores/uploads.ts (global) so it survives closing
// the file browser. This store is just for the browser's directory/file
// viewing + editing state.

type FilesState = {
  /** Non-null when the browser modal is open. */
  project: ProjectRef | null;
  directory: DirectoryView | null;
  file: FileView | null;
  loading: boolean;
  error: string | null;
  /** True when the file viewer is in edit mode. */
  editing: boolean;
  /** Content buffer while editing (diverges from file.content on user edits). */
  editContent: string;
  /** True while a write_file request is in flight. */
  saving: boolean;

  /** Multi-select mode in the directory listing (entered via long-press). */
  selectionMode: boolean;
  /** Selected entries in the current directory, keyed by project-relative
   *  path -> kind. Cleared whenever the listing changes (navigate/close). */
  selected: Record<string, "file" | "dir">;

  open: (project: ProjectRef) => void;
  close: () => void;
  /** Mark we're about to navigate (so the UI can show a spinner). */
  beginNavigate: () => void;
  /** Mark we're about to open a file. */
  beginOpenFile: (path: string) => void;
  closeFile: () => void;
  setError: (msg: string | null) => void;
  /** Enter edit mode (textarea replaces <pre>). */
  beginEdit: () => void;
  /** Cancel edit mode without saving. */
  cancelEdit: () => void;
  /** Update the edit buffer as the user types. */
  setEditContent: (content: string) => void;
  /** Mark a save in progress. */
  beginSave: () => void;
  /** Enter selection mode and select the given entry (long-press). */
  enterSelection: (path: string, kind: "file" | "dir") => void;
  /** Toggle one entry's selection (tap while in selection mode). */
  toggleSelect: (path: string, kind: "file" | "dir") => void;
  /** Leave selection mode and clear the selection. */
  clearSelection: () => void;
  handleServerMessage: (msg: ServerMessage) => void;
};

export const useFilesStore = create<FilesState>((set, get) => ({
  project: null,
  directory: null,
  file: null,
  loading: false,
  error: null,
  editing: false,
  editContent: "",
  saving: false,
  selectionMode: false,
  selected: {},

  open: (project) =>
    set({
      project,
      directory: null,
      file: null,
      loading: false,
      error: null,
      editing: false,
      editContent: "",
      saving: false,
      selectionMode: false,
      selected: {},
    }),

  close: () =>
    set({
      project: null,
      directory: null,
      file: null,
      loading: false,
      error: null,
      editing: false,
      editContent: "",
      saving: false,
      selectionMode: false,
      selected: {},
    }),

  beginNavigate: () => set({ loading: true, error: null, file: null, editing: false, editContent: "", saving: false, selectionMode: false, selected: {} }),
  beginOpenFile: (path) =>
    set({
      loading: true,
      error: null,
      file: { path, size: 0, tooLarge: false, encoding: null, content: null },
      editing: false,
      editContent: "",
      saving: false,
    }),
  closeFile: () => set({ file: null, error: null, editing: false, editContent: "", saving: false }),

  setError: (msg) => set({ error: msg, loading: false, saving: false }),

  beginEdit: () => {
    const { file } = get();
    if (!file || file.encoding !== "utf-8" || file.content === null) return;
    set({ editing: true, editContent: file.content, error: null });
  },

  cancelEdit: () => set({ editing: false, editContent: "", error: null }),

  setEditContent: (content) => set({ editContent: content }),

  beginSave: () => set({ saving: true, error: null }),

  enterSelection: (path, kind) =>
    set({ selectionMode: true, selected: { [path]: kind } }),

  toggleSelect: (path, kind) =>
    set((s) => {
      const next = { ...s.selected };
      if (next[path]) {
        delete next[path];
      } else {
        next[path] = kind;
      }
      // Leaving zero selected keeps selection mode on so the user can pick
      // again without re-long-pressing; Cancel is the explicit exit.
      return { selected: next };
    }),

  clearSelection: () => set({ selectionMode: false, selected: {} }),

  handleServerMessage: (msg) => {
    const state = get();
    if (!state.project) return;

    if (msg.type === "directory" && msg.payload.project_id === state.project.id) {
      set({
        directory: {
          path: msg.payload.path,
          parent: msg.payload.parent,
          entries: msg.payload.entries,
        },
        file: null,
        loading: false,
        error: null,
        editing: false,
        editContent: "",
        saving: false,
        selectionMode: false,
        selected: {},
      });
      return;
    }

    if (
      msg.type === "file_content" &&
      msg.payload.project_id === state.project.id
    ) {
      set({
        file: {
          path: msg.payload.path,
          size: msg.payload.size,
          tooLarge: msg.payload.too_large,
          encoding: msg.payload.encoding,
          content: msg.payload.content,
        },
        loading: false,
        error: null,
        editing: false,
        editContent: "",
        saving: false,
      });
      return;
    }

    if (
      msg.type === "file_written" &&
      msg.payload.project_id === state.project.id
    ) {
      // Save succeeded - update file view with saved content, exit edit mode.
      const { editContent } = get();
      set((s) => ({
        file: s.file
          ? { ...s.file, content: editContent, size: msg.payload.size }
          : s.file,
        editing: false,
        editContent: "",
        saving: false,
        error: null,
      }));
      return;
    }

    if (msg.type === "error") {
      if (state.loading || state.saving) {
        set({ error: msg.payload.message, loading: false, saving: false });
      }
      return;
    }
  },
}));
