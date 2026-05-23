import { useEffect } from "react";
import type { ClientMessage, DirectoryEntry } from "../types";
import { useFilesStore } from "../stores/files";

type Props = {
  send: (msg: ClientMessage) => boolean;
};

export function FileBrowser({ send }: Props) {
  const project = useFilesStore((s) => s.project);
  const directory = useFilesStore((s) => s.directory);
  const file = useFilesStore((s) => s.file);
  const loading = useFilesStore((s) => s.loading);
  const error = useFilesStore((s) => s.error);
  const close = useFilesStore((s) => s.close);
  const beginNavigate = useFilesStore((s) => s.beginNavigate);
  const beginOpenFile = useFilesStore((s) => s.beginOpenFile);
  const closeFile = useFilesStore((s) => s.closeFile);

  // First-open: ask the server for the project root.
  // Guard on `directory || file`, NOT on `loading` — open() leaves loading=false
  // and `beginNavigate` flips it after we fire the request. If we gated on
  // loading we'd skip the very first send.
  useEffect(() => {
    if (!project) return;
    if (directory || file) return;
    beginNavigate();
    send({
      type: "list_directory",
      payload: { project_id: project.id, path: "" },
    });
  }, [project, directory, file, beginNavigate, send]);

  // Close on Escape.
  useEffect(() => {
    if (!project) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (file) {
        closeFile();
      } else {
        close();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [project, file, close, closeFile]);

  if (!project) return null;

  const navigateTo = (path: string) => {
    if (!project) return;
    beginNavigate();
    send({
      type: "list_directory",
      payload: { project_id: project.id, path },
    });
  };

  const openFile = (path: string) => {
    if (!project) return;
    beginOpenFile(path);
    send({
      type: "read_file",
      payload: { project_id: project.id, path },
    });
  };

  const currentPath = directory?.path ?? "";
  const breadcrumbs = currentPath
    ? `${project.name}/${currentPath}`
    : project.name;

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-gray-900 text-gray-100">
      <header className="border-b border-gray-800 px-3 md:px-4 py-2 md:py-3 flex items-center gap-3 shrink-0">
        <button
          type="button"
          onClick={() => (file ? closeFile() : close())}
          className="p-1 -ml-1 text-gray-400 hover:text-white"
          aria-label={file ? "Back" : "Close file browser"}
          title={file ? "Back to listing" : "Close"}
        >
          {file ? (
            <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor">
              <path d="M12.7 4.3a1 1 0 010 1.4L8.4 10l4.3 4.3a1 1 0 11-1.4 1.4l-5-5a1 1 0 010-1.4l5-5a1 1 0 011.4 0z" />
            </svg>
          ) : (
            <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor">
              <path d="M5.3 4.3a1 1 0 011.4 0L10 7.6l3.3-3.3a1 1 0 111.4 1.4L11.4 9l3.3 3.3a1 1 0 11-1.4 1.4L10 10.4 6.7 13.7a1 1 0 01-1.4-1.4L8.6 9 5.3 5.7a1 1 0 010-1.4z" />
            </svg>
          )}
        </button>
        <div className="flex-1 min-w-0">
          <div className="text-xs text-gray-500 uppercase tracking-wide">
            {file ? "File" : "Files"}
          </div>
          <div className="text-sm md:text-base font-mono truncate" title={project.path + (currentPath ? `/${currentPath}` : "")}>
            {file ? file.path : breadcrumbs}
          </div>
        </div>
        <button
          type="button"
          onClick={close}
          className="text-xs text-gray-500 hover:text-gray-200 px-2 py-1 rounded border border-gray-700 hover:border-gray-500"
        >
          Close
        </button>
      </header>

      <div className="flex-1 overflow-y-auto">
        {error && (
          <div className="m-3 p-3 rounded bg-red-950/40 border border-red-800 text-sm text-red-300">
            {error}
          </div>
        )}

        {!file && (
          <DirectoryListing
            loading={loading}
            directory={directory}
            onNavigate={navigateTo}
            onOpenFile={openFile}
          />
        )}

        {file && (
          <FileViewer
            file={{
              path: file.path,
              size: file.size,
              tooLarge: file.tooLarge,
              encoding: file.encoding,
              content: file.content,
              loading,
            }}
          />
        )}
      </div>
    </div>
  );
}

type ListingProps = {
  loading: boolean;
  directory: { path: string; parent: string | null; entries: DirectoryEntry[] } | null;
  onNavigate: (path: string) => void;
  onOpenFile: (path: string) => void;
};

function DirectoryListing({ loading, directory, onNavigate, onOpenFile }: ListingProps) {
  if (loading && !directory) {
    return <div className="text-sm text-gray-500 p-4">Loading…</div>;
  }
  if (!directory) {
    return null;
  }

  const childPath = (name: string) =>
    directory.path ? `${directory.path}/${name}` : name;

  return (
    <ul className="divide-y divide-gray-800">
      {directory.parent !== null && (
        <li>
          <button
            type="button"
            onClick={() => onNavigate(directory.parent ?? "")}
            className="w-full text-left flex items-center gap-3 px-3 md:px-4 py-2.5 hover:bg-gray-800/50"
          >
            <FolderUpIcon />
            <span className="text-sm text-gray-300">..</span>
          </button>
        </li>
      )}
      {directory.entries.length === 0 && (
        <li className="text-sm text-gray-600 px-4 py-6 text-center italic">
          Empty directory
        </li>
      )}
      {directory.entries.map((entry) => {
        const path = childPath(entry.name);
        return (
          <li key={entry.name}>
            <button
              type="button"
              onClick={() =>
                entry.kind === "dir" ? onNavigate(path) : onOpenFile(path)
              }
              className="w-full text-left flex items-center gap-3 px-3 md:px-4 py-2.5 hover:bg-gray-800/50"
              title={entry.name}
            >
              {entry.kind === "dir" ? <FolderIcon /> : <FileIcon />}
              <span className="flex-1 text-sm truncate text-gray-100">
                {entry.name}
              </span>
              {entry.kind === "file" && entry.size !== null && (
                <span className="text-xs text-gray-500 font-mono shrink-0">
                  {formatSize(entry.size)}
                </span>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

type FileViewerProps = {
  file: {
    path: string;
    size: number;
    tooLarge: boolean;
    encoding: "utf-8" | "base64" | null;
    content: string | null;
    loading: boolean;
  };
};

function FileViewer({ file }: FileViewerProps) {
  if (file.loading && file.content === null) {
    return <div className="text-sm text-gray-500 p-4">Loading…</div>;
  }
  if (file.tooLarge) {
    return (
      <div className="m-3 p-4 rounded bg-yellow-950/30 border border-yellow-800 text-sm text-yellow-200">
        File is too large to preview ({formatSize(file.size)}). Open it from a
        Claude prompt instead.
      </div>
    );
  }

  if (file.encoding === "base64") {
    // Try to render as an image if the path hints at one.
    const ext = file.path.toLowerCase().split(".").pop() ?? "";
    const imageMime: Record<string, string> = {
      png: "image/png",
      jpg: "image/jpeg",
      jpeg: "image/jpeg",
      gif: "image/gif",
      webp: "image/webp",
      svg: "image/svg+xml",
    };
    const mime = imageMime[ext];
    if (mime && file.content) {
      return (
        <div className="p-3 md:p-4 flex items-center justify-center">
          <img
            src={`data:${mime};base64,${file.content}`}
            alt={file.path}
            className="max-w-full max-h-[calc(100dvh-8rem)] rounded shadow"
          />
        </div>
      );
    }
    return (
      <div className="m-3 p-4 rounded bg-gray-800/60 border border-gray-700 text-sm text-gray-300">
        Binary file ({formatSize(file.size)}) — preview not available.
      </div>
    );
  }

  return (
    <pre className="text-xs md:text-sm font-mono whitespace-pre p-3 md:p-4 overflow-x-auto leading-relaxed">
      <code>{file.content ?? ""}</code>
    </pre>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
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

function FolderUpIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 20 20"
      fill="currentColor"
      className="text-gray-400 shrink-0"
    >
      <path d="M2 5a2 2 0 012-2h3.2a2 2 0 011.6.8L9.6 5H16a2 2 0 012 2v7a2 2 0 01-2 2H4a2 2 0 01-2-2V5zm8 8.4l2.3-2.3a1 1 0 10-1.4-1.4l-.6.6V8a1 1 0 10-2 0v2.3l-.6-.6a1 1 0 10-1.4 1.4L10 13.4z" />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 20 20"
      fill="currentColor"
      className="text-gray-400 shrink-0"
    >
      <path
        fillRule="evenodd"
        d="M4 3a2 2 0 012-2h6l4 4v12a2 2 0 01-2 2H6a2 2 0 01-2-2V3zm8 0v3a1 1 0 001 1h3"
        clipRule="evenodd"
      />
    </svg>
  );
}
