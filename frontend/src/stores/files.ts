import { create } from "zustand";
import type { DirectoryEntry, ServerMessage } from "../types";

export type ProjectRef = { id: number; name: string; path: string };

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

type FilesState = {
  /** Non-null when the browser modal is open. */
  project: ProjectRef | null;
  directory: DirectoryView | null;
  file: FileView | null;
  loading: boolean;
  error: string | null;

  open: (project: ProjectRef) => void;
  close: () => void;
  /** Mark we're about to navigate (so the UI can show a spinner). */
  beginNavigate: () => void;
  /** Mark we're about to open a file. */
  beginOpenFile: (path: string) => void;
  closeFile: () => void;
  setError: (msg: string | null) => void;
  handleServerMessage: (msg: ServerMessage) => void;
};

export const useFilesStore = create<FilesState>((set, get) => ({
  project: null,
  directory: null,
  file: null,
  loading: false,
  error: null,

  open: (project) =>
    set({
      project,
      directory: null,
      file: null,
      // loading stays false here — the FileBrowser effect picks up the change
      // and fires `list_directory`, which switches loading on through beginNavigate.
      loading: false,
      error: null,
    }),

  close: () =>
    set({
      project: null,
      directory: null,
      file: null,
      loading: false,
      error: null,
    }),

  beginNavigate: () => set({ loading: true, error: null, file: null }),
  beginOpenFile: (path) =>
    set({
      loading: true,
      error: null,
      file: { path, size: 0, tooLarge: false, encoding: null, content: null },
    }),
  closeFile: () => set({ file: null, error: null }),

  setError: (msg) => set({ error: msg, loading: false }),

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
      });
      return;
    }

    if (msg.type === "error" && state.loading) {
      // Errors aren't tagged with project_id; if we're the one waiting, claim it.
      set({ error: msg.payload.message, loading: false });
      return;
    }
  },
}));
