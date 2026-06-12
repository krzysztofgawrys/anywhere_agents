import { useCallback, useEffect, useRef, useState } from "react";
import type { ClientMessage, DirectoryEntry } from "../types";
import { useFilesStore } from "../stores/files";
import { useUploadsStore, type UploadItem } from "../stores/uploads";
import { Markdown } from "./Markdown";

type Props = {
  send: (msg: ClientMessage) => boolean;
};

export function FileBrowser({ send }: Props) {
  const project = useFilesStore((s) => s.project);
  const directory = useFilesStore((s) => s.directory);
  const file = useFilesStore((s) => s.file);
  const loading = useFilesStore((s) => s.loading);
  const error = useFilesStore((s) => s.error);
  const editing = useFilesStore((s) => s.editing);
  const editContent = useFilesStore((s) => s.editContent);
  const saving = useFilesStore((s) => s.saving);
  const close = useFilesStore((s) => s.close);
  const beginNavigate = useFilesStore((s) => s.beginNavigate);
  const beginOpenFile = useFilesStore((s) => s.beginOpenFile);
  const closeFile = useFilesStore((s) => s.closeFile);
  const beginEdit = useFilesStore((s) => s.beginEdit);
  const cancelEdit = useFilesStore((s) => s.cancelEdit);
  const setEditContent = useFilesStore((s) => s.setEditContent);
  const beginSave = useFilesStore((s) => s.beginSave);

  // Uploads pipeline is global (see stores/uploads.ts + UploadOverlay). We
  // only need to enqueue here and observe the tick so the browser re-lists
  // its directory after each successful upload.
  const enqueueUploads = useUploadsStore((s) => s.enqueue);
  const uploadedTick = useUploadsStore((s) => s.uploadedTick);

  const fileInputRef = useRef<HTMLInputElement>(null);
  // Counter for the drag overlay: dragenter/dragleave fire for every child
  // element on the way through, so a boolean flicks on/off. A depth counter
  // (incremented on enter, decremented on leave) stays stable across the
  // drag.
  const dragDepthRef = useRef(0);
  const [isDragOver, setIsDragOver] = useState(false);
  // Markdown files open in rendered preview by default; "Raw" in the header
  // switches to the plain source view. Reset on every file open.
  const [mdPreview, setMdPreview] = useState(true);

  // First-open: ask the server for the project root.
  // Guard on `directory || file`, NOT on `loading` - open() leaves loading=false
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
      if (editing) {
        cancelEdit();
      } else if (file) {
        closeFile();
      } else {
        close();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [project, file, editing, close, closeFile, cancelEdit]);

  const saveFile = () => {
    if (!project || !file) return;
    beginSave();
    send({
      type: "write_file",
      payload: { project_id: project.id, path: file.path, content: editContent },
    });
  };

  // ── Uploads ──────────────────────────────────────────────────────────────
  // Enqueue selected files into the GLOBAL uploads store. The pump
  // (App-level useUploadPump) drives the actual HTTP requests so it
  // continues after the browser is closed.
  //
  // We capture project.id / workerId / path AT ENQUEUE TIME via closure -
  // not at upload time - so navigating to a different directory or
  // closing the browser does not redirect an in-flight upload.
  const addFiles = useCallback(
    (files: FileList | File[]) => {
      if (!project) return;
      const list = Array.from(files);
      if (list.length === 0) return;
      // Without worker_id the hub can't route the upload. This is only
      // missing for ProjectRefs constructed before the workerId field was
      // added - real flows from App.tsx always include it.
      if (!project.workerId) {
        console.warn("FileBrowser: project.workerId missing; upload not enqueued");
        return;
      }
      const dirPath = directory?.path ?? "";
      const items: UploadItem[] = list.map((f) => ({
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}-${f.name}`,
        workerId: project.workerId!,
        projectPath: project.path,
        dirPath,
        filename: f.name,
        file: f,
        sizeBytes: f.size,
      }));
      enqueueUploads(items);
    },
    [project, directory?.path, enqueueUploads],
  );

  // Refresh the listing after each successful upload so the file shows up
  // without manual reload. `uploadedTick` increments globally; we only act
  // when this browser is open AND positioned in the directory the upload
  // landed in (other directories' listings are stale already, and that's
  // fine - they'll refresh when revisited).
  const lastTickRef = useRef(uploadedTick);
  useEffect(() => {
    if (uploadedTick === lastTickRef.current) return;
    lastTickRef.current = uploadedTick;
    if (!project) return;
    if (file) return; // Viewing a file, not the listing - don't disturb.
    send({
      type: "list_directory",
      payload: { project_id: project.id, path: directory?.path ?? "" },
    });
  }, [uploadedTick, project, file, directory?.path, send]);

  const onPickFiles = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      addFiles(e.target.files);
      // Reset so re-selecting the SAME file re-fires onChange.
      e.target.value = "";
    }
  };

  const onDragEnter = (e: React.DragEvent) => {
    if (file) return;
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    dragDepthRef.current += 1;
    setIsDragOver(true);
  };
  const onDragOver = (e: React.DragEvent) => {
    if (file) return;
    if (!e.dataTransfer?.types?.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };
  const onDragLeave = (e: React.DragEvent) => {
    if (file) return;
    e.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setIsDragOver(false);
  };
  const onDrop = (e: React.DragEvent) => {
    if (file) return;
    e.preventDefault();
    dragDepthRef.current = 0;
    setIsDragOver(false);
    if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
      addFiles(e.dataTransfer.files);
    }
  };

  if (!project) return null;

  const canEdit = file && file.encoding === "utf-8" && !file.tooLarge && file.content !== null;
  const isMarkdownFile = !!canEdit && !!file && /\.(md|markdown)$/i.test(file.path);

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
    setMdPreview(true);
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
          onClick={() => {
            if (editing) { cancelEdit(); }
            else if (file) { closeFile(); }
            else { close(); }
          }}
          className="p-1 -ml-1 text-gray-400 hover:text-white"
          aria-label={file ? "Back" : "Close file browser"}
          title={editing ? "Cancel edit" : file ? "Back to listing" : "Close"}
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
            {editing ? "Editing" : file ? "File" : "Files"}
          </div>
          <div className="text-sm md:text-base font-mono truncate" title={project.path + (currentPath ? `/${currentPath}` : "")}>
            {file ? file.path : breadcrumbs}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {isMarkdownFile && !editing && (
            <button
              type="button"
              onClick={() => setMdPreview((p) => !p)}
              className="text-xs text-gray-400 hover:text-white px-2 py-1 rounded border border-gray-700 hover:border-gray-500"
              title={mdPreview ? "Show raw markdown source" : "Show rendered markdown"}
            >
              {mdPreview ? "Raw" : "Preview"}
            </button>
          )}
          {file && canEdit && !editing && (
            <button
              type="button"
              onClick={beginEdit}
              className="text-xs text-gray-400 hover:text-white px-2 py-1 rounded border border-gray-700 hover:border-gray-500"
              title="Edit file"
            >
              Edit
            </button>
          )}
          {editing && (
            <>
              <button
                type="button"
                onClick={cancelEdit}
                className="text-xs text-gray-400 hover:text-gray-200 px-2 py-1 rounded border border-gray-700 hover:border-gray-500"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={saveFile}
                disabled={saving}
                className="text-xs text-white px-2 py-1 rounded border border-blue-600 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {saving ? "Saving..." : "Save"}
              </button>
            </>
          )}
          {!file && (
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="text-xs text-gray-300 hover:text-white px-2 py-1 rounded border border-gray-700 hover:border-gray-500"
              title="Upload files into this directory"
            >
              Upload
            </button>
          )}
          <button
            type="button"
            onClick={close}
            className="text-xs text-gray-500 hover:text-gray-200 px-2 py-1 rounded border border-gray-700 hover:border-gray-500"
          >
            Close
          </button>
        </div>
      </header>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        onChange={onPickFiles}
        className="hidden"
      />

      <div
        className="relative flex-1 overflow-y-auto"
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
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
            editing={editing}
            editContent={editContent}
            onEditChange={setEditContent}
            onSave={saveFile}
            saving={saving}
            renderMarkdown={isMarkdownFile && mdPreview}
          />
        )}

        {/* Drag overlay - only over the listing area, only when not viewing a
            single file. Pointer-events allow drop while making clear the
            target is the whole area. */}
        {isDragOver && !file && (
          <div className="pointer-events-none absolute inset-0 z-30 flex items-center justify-center bg-blue-950/50 border-2 border-dashed border-blue-400 rounded-sm">
            <div className="text-blue-100 text-base md:text-lg font-medium px-4 py-3 rounded bg-blue-900/70 shadow">
              Drop files to upload to <span className="font-mono">{directory?.path || project.name}</span>
            </div>
          </div>
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
  editing: boolean;
  editContent: string;
  onEditChange: (content: string) => void;
  onSave: () => void;
  saving: boolean;
  /** Render UTF-8 content as markdown (rendered .md preview) instead of <pre>. */
  renderMarkdown: boolean;
};

function FileViewer({ file, editing, editContent, onEditChange, onSave, saving, renderMarkdown }: FileViewerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Focus textarea when entering edit mode.
  useEffect(() => {
    if (editing && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [editing]);

  if (file.loading && file.content === null) {
    return <div className="text-sm text-gray-500 p-4">Loading…</div>;
  }
  if (file.tooLarge) {
    return (
      <div className="m-3 p-4 rounded bg-yellow-950/30 border border-yellow-800 text-sm text-yellow-200">
        File is too large to preview ({formatSize(file.size)}). Open it from an
        agent prompt instead.
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
        Binary file ({formatSize(file.size)}) - preview not available.
      </div>
    );
  }

  if (editing) {
    return (
      <div className="flex flex-col h-full">
        <textarea
          ref={textareaRef}
          value={editContent}
          onChange={(e) => onEditChange(e.target.value)}
          onKeyDown={(e) => {
            // Ctrl/Cmd+S to save
            if ((e.ctrlKey || e.metaKey) && e.key === "s") {
              e.preventDefault();
              if (!saving) onSave();
            }
          }}
          className="flex-1 w-full bg-gray-950 text-gray-100 text-xs md:text-sm font-mono p-3 md:p-4 resize-none outline-none leading-relaxed"
          spellCheck={false}
          disabled={saving}
        />
      </div>
    );
  }

  if (renderMarkdown) {
    return (
      <div className="prose prose-invert prose-sm md:prose-base max-w-none break-words p-3 md:p-4">
        <Markdown>{file.content ?? ""}</Markdown>
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
