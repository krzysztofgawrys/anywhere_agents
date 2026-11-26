import { createContext, useContext } from "react";

/** Identifies the active project so file paths mentioned in chat can be turned
 *  into download links. Null when there is no project context (e.g. the file
 *  browser's own markdown preview), in which case no linkification happens. */
export type WorkerFileCtx = {
  /** Absolute project root on the worker, e.g. /home/kgawrys/code/claude_cloud. */
  projectPath: string;
  /** Worker the project lives on - needed for the /api/download route. */
  workerId: string;
};

export const WorkerFileContext = createContext<WorkerFileCtx | null>(null);

export function useWorkerFileCtx(): WorkerFileCtx | null {
  return useContext(WorkerFileContext);
}

// A path token may carry a `:line` or `:line:col` suffix (the file_path:line
// convention). Strip it - downloads target the whole file.
const LINE_SUFFIX = /:\d+(?::\d+)?$/;
// Conservative filename-extension test: a dot followed by a *letter*-led
// extension (1-8 chars). Excludes version numbers like "4.8" and trailing
// dots in "e.g." so prose tokens don't get linkified.
const FILE_EXT = /\.[A-Za-z][A-Za-z0-9]{0,7}$/;
// Characters we accept in a path token. Anything outside this (spaces,
// parens, quotes, ...) means it isn't a bare path and we leave it alone.
const PATH_CHARS = /^[\w./~@+-]+$/;

/** Given the raw text of an inline-code span, decide whether it names a file on
 *  the worker that we can offer for download, and if so return the
 *  `/api/download?...` URL. Returns null for anything that isn't a downloadable
 *  project-relative path (prose, identifiers, absolute paths outside the
 *  project root, traversal attempts, ...).
 *
 *  Mirrors the hub's /api/download contract: it strips a leading slash and
 *  resolves `path` against `project_path`, so we must hand it a path that is
 *  relative to the project root (or absolute *under* that root). */
export function workerFileDownloadUrl(
  ctx: WorkerFileCtx | null,
  raw: string,
): string | null {
  if (!ctx) return null;

  let s = raw.trim();
  if (!s || /\s/.test(s)) return null; // single token only
  s = s.replace(LINE_SUFFIX, "");
  if (!PATH_CHARS.test(s)) return null;

  // Must actually look like a path: either nested (has a slash) or a filename
  // with a recognizable extension. A bare identifier like `useState` is not.
  if (!s.includes("/") && !FILE_EXT.test(s)) return null;

  const root = ctx.projectPath.replace(/\/+$/, "");
  let rel: string;
  if (s.startsWith("/")) {
    // Absolute path - only downloadable if it lives inside the project root.
    if (s === root) return null;
    if (s.startsWith(root + "/")) rel = s.slice(root.length + 1);
    else return null;
  } else {
    rel = s.replace(/^\.\//, "");
  }

  if (!rel || rel.split("/").includes("..")) return null;

  const params = new URLSearchParams({
    worker_id: ctx.workerId,
    project_path: ctx.projectPath,
    path: rel,
  });
  return `/api/download?${params.toString()}`;
}
