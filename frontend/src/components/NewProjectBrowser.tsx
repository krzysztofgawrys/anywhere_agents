import { useEffect, useRef, useState } from "react";
import type { ClientMessage, DirectoryEntry } from "../types";

type FsDir = {
  path: string;
  parent: string | null;
  entries: DirectoryEntry[];
};

type Props = {
  send: (msg: ClientMessage) => boolean;
  directory: FsDir | null;
  open: boolean;
  onClose: () => void;
  workerId?: string;
};

export function NewProjectBrowser({ send, directory, open, onClose, workerId }: Props) {
  const [newFolderMode, setNewFolderMode] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [creating, setCreating] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Load home directory on first open.
  useEffect(() => {
    if (!open) return;
    if (!directory) {
      send({ type: "browse_fs", payload: { worker_id: workerId } });
    }
  }, [open, directory, send]);

  // Focus new-folder input when it appears.
  useEffect(() => {
    if (newFolderMode) {
      inputRef.current?.focus();
    }
  }, [newFolderMode]);

  // Escape key: cancel new-folder mode or close modal.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (newFolderMode) {
        setNewFolderMode(false);
        setNewFolderName("");
      } else {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, newFolderMode, onClose]);

  if (!open) return null;

  const navigateTo = (path: string) => {
    setNewFolderMode(false);
    setNewFolderName("");
    send({ type: "browse_fs", payload: { path, worker_id: workerId } });
  };

  const goUp = () => {
    if (directory?.parent != null) {
      navigateTo(directory.parent);
    }
  };

  const handleCreateFolder = () => {
    if (!directory || !newFolderName.trim()) return;
    const name = newFolderName.trim();
    const newPath = `${directory.path}/${name}`;
    setCreating(true);
    send({ type: "create_directory", payload: { path: newPath, worker_id: workerId } });
    setNewFolderMode(false);
    setNewFolderName("");
    setCreating(false);
  };

  const handleSelectDirectory = () => {
    if (!directory) return;
    send({ type: "create_project", payload: { path: directory.path, worker_id: workerId } });
  };

  // Breadcrumb: split absolute path into segments
  const pathSegments = directory
    ? directory.path.split("/").filter(Boolean)
    : [];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
      <div className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl flex flex-col w-full max-w-lg mx-4 max-h-[85dvh]">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-800 shrink-0">
          <button
            type="button"
            onClick={goUp}
            disabled={!directory || directory.parent == null}
            className="p-1 text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed rounded"
            title="Go up"
          >
            <svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor">
              <path d="M12.7 4.3a1 1 0 010 1.4L8.4 10l4.3 4.3a1 1 0 11-1.4 1.4l-5-5a1 1 0 010-1.4l5-5a1 1 0 011.4 0z" />
            </svg>
          </button>
          <div className="flex-1 min-w-0">
            <div className="text-xs text-gray-500 uppercase tracking-wide mb-0.5">
              New project - select directory
            </div>
            {/* Breadcrumb */}
            <div className="text-sm font-mono text-gray-200 truncate" title={directory?.path ?? ""}>
              {directory
                ? "/" + pathSegments.join("/")
                : "Loading…"}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-500 hover:text-white rounded"
            title="Cancel"
          >
            <svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor">
              <path d="M5.3 4.3a1 1 0 011.4 0L10 7.6l3.3-3.3a1 1 0 111.4 1.4L11.4 9l3.3 3.3a1 1 0 11-1.4 1.4L10 10.4 6.7 13.7a1 1 0 01-1.4-1.4L8.6 9 5.3 5.7a1 1 0 010-1.4z" />
            </svg>
          </button>
        </div>

        {/* Directory listing */}
        <div className="flex-1 overflow-y-auto">
          {!directory && (
            <div className="text-sm text-gray-500 p-4">Loading…</div>
          )}
          {directory && (
            <ul className="divide-y divide-gray-800/60">
              {directory.entries.length === 0 && (
                <li className="text-sm text-gray-600 px-4 py-6 text-center italic">
                  Empty directory
                </li>
              )}
              {directory.entries.map((entry) => (
                <li key={entry.name}>
                  <button
                    type="button"
                    onClick={() => navigateTo(`${directory.path}/${entry.name}`)}
                    className="w-full text-left flex items-center gap-3 px-4 py-2.5 hover:bg-gray-800/50"
                    title={entry.name}
                  >
                    <FolderIcon />
                    <span className="flex-1 text-sm truncate text-gray-100">
                      {entry.name}
                    </span>
                    <svg
                      width="14"
                      height="14"
                      viewBox="0 0 20 20"
                      fill="currentColor"
                      className="text-gray-600 shrink-0"
                    >
                      <path d="M7.3 4.3a1 1 0 000 1.4L11.6 10l-4.3 4.3a1 1 0 101.4 1.4l5-5a1 1 0 000-1.4l-5-5a1 1 0 00-1.4 0z" />
                    </svg>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* New folder input */}
        {newFolderMode && (
          <div className="px-4 py-3 border-t border-gray-800 bg-gray-950/60 shrink-0">
            <div className="text-xs text-gray-400 mb-1.5">New folder name</div>
            <div className="flex gap-2">
              <input
                ref={inputRef}
                type="text"
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleCreateFolder();
                  if (e.key === "Escape") {
                    setNewFolderMode(false);
                    setNewFolderName("");
                  }
                }}
                placeholder="folder-name"
                className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-blue-500"
              />
              <button
                type="button"
                onClick={handleCreateFolder}
                disabled={!newFolderName.trim() || creating}
                className="px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-700 text-white text-sm disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Create
              </button>
              <button
                type="button"
                onClick={() => {
                  setNewFolderMode(false);
                  setNewFolderName("");
                }}
                className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-gray-200 text-sm"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Footer actions */}
        <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-gray-800 shrink-0">
          <button
            type="button"
            onClick={() => {
              setNewFolderMode(true);
              setNewFolderName("");
            }}
            disabled={!directory || newFolderMode}
            className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor">
              <path d="M10 3a1 1 0 011 1v5h5a1 1 0 110 2h-5v5a1 1 0 11-2 0v-5H4a1 1 0 110-2h5V4a1 1 0 011-1z" />
            </svg>
            New folder
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-1.5 rounded text-sm text-gray-400 hover:text-gray-100 hover:bg-gray-800"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSelectDirectory}
              disabled={!directory}
              className="px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Select this directory
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function FolderIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 20 20"
      fill="currentColor"
      className="text-blue-400 shrink-0"
    >
      <path d="M2 5a2 2 0 012-2h3.2a2 2 0 011.6.8L9.6 5H16a2 2 0 012 2v7a2 2 0 01-2 2H4a2 2 0 01-2-2V5z" />
    </svg>
  );
}
