import { useEffect, useRef, useState } from "react";
import type { ClientMessage, Project, SessionSummary } from "../types";
import { useProjectsStore } from "../stores/projects";

type Props = {
  send: (msg: ClientMessage) => boolean;
  connected: boolean;
  onPickSession: (projectId: number, sessionId: string) => void;
  onNewSession: (projectId: number) => void;
  onBrowseFiles: (project: Project) => void;
  onNewProject: (workerId?: string) => void;
  open: boolean;
  onClose: () => void;
};

export function Sidebar({
  send,
  connected,
  onPickSession,
  onNewSession,
  onBrowseFiles,
  onNewProject,
  open,
  onClose,
}: Props) {
  const projects = useProjectsStore((s) => s.projects);
  const sessionsByProject = useProjectsStore((s) => s.sessionsByProject);
  const activeProjectId = useProjectsStore((s) => s.activeProjectId);
  const loading = useProjectsStore((s) => s.loading);
  const setActive = useProjectsStore((s) => s.setActive);
  const setLoading = useProjectsStore((s) => s.setLoading);

  // Worker filter
  const workerList = Array.from(
    new Map(
      projects
        .filter((p) => p.worker_id)
        .map((p) => [p.worker_id!, { id: p.worker_id!, label: p.worker_label || p.worker_id! }])
    ).values()
  );
  const multiWorker = workerList.length > 1;
  const [workerFilter, setWorkerFilter] = useState<string | null>(null);
  const [workerDropdownOpen, setWorkerDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close dropdown on click outside
  useEffect(() => {
    if (!workerDropdownOpen) return;
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setWorkerDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [workerDropdownOpen]);

  const filteredProjects = workerFilter
    ? projects.filter((p) => p.worker_id === workerFilter)
    : projects;

  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [logoutUrl, setLogoutUrl] = useState<string | null>(null);

  // Fetch logout URL once on mount.
  useEffect(() => {
    fetch("/api/auth/info")
      .then((r) => r.json())
      .then((d: { logout_url: string | null }) => setLogoutUrl(d.logout_url))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (connected) {
      setLoading();
      send({ type: "list_projects", payload: {} });
    }
  }, [connected, send, setLoading]);

  const toggleProject = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
        // Fetch sessions for this project
        // Always re-fetch so the list is fresh (picks up sessions created
      // since the last expand, including ones just started).
      send({ type: "list_sessions", payload: { project_id: id } });
      }
      return next;
    });
  };

  return (
    <>
      {/* Mobile overlay */}
      {open && (
        <div
          className="fixed inset-0 bg-black/60 z-30 md:hidden"
          onClick={onClose}
        />
      )}
      <aside
        className={`fixed md:relative inset-y-0 left-0 z-40 w-72 md:w-80 bg-gray-950 border-r border-gray-800 flex flex-col transform transition-transform h-full md:h-auto ${
          open ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        }`}
        style={{ maxHeight: "100dvh" }}
      >
        <div className="px-4 py-3 border-b border-gray-800">
          <div className="flex items-center justify-between">
            {multiWorker ? (
              <div className="relative" ref={dropdownRef}>
                <button
                  type="button"
                  onClick={() => setWorkerDropdownOpen((v) => !v)}
                  className="flex items-center gap-1.5 text-sm font-semibold uppercase tracking-wide text-gray-400 hover:text-gray-200 transition-colors"
                >
                  <span>{workerFilter ? workerList.find((w) => w.id === workerFilter)?.label ?? "All" : "All workers"}</span>
                  <svg
                    width="12"
                    height="12"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                    className={`transition-transform ${workerDropdownOpen ? "rotate-180" : ""}`}
                  >
                    <path d="M5.3 7.3a1 1 0 011.4 0L10 10.6l3.3-3.3a1 1 0 111.4 1.4l-4 4a1 1 0 01-1.4 0l-4-4a1 1 0 010-1.4z" />
                  </svg>
                </button>
                {workerDropdownOpen && (
                  <div className="absolute top-full left-0 mt-1.5 min-w-[160px] bg-gray-900 border border-gray-700 rounded-lg shadow-xl z-50 py-1 overflow-hidden">
                    <button
                      type="button"
                      onClick={() => { setWorkerFilter(null); setWorkerDropdownOpen(false); }}
                      className={`w-full text-left px-3 py-1.5 text-sm transition-colors ${
                        workerFilter === null
                          ? "text-white bg-gray-800"
                          : "text-gray-300 hover:bg-gray-800 hover:text-white"
                      }`}
                    >
                      All workers
                    </button>
                    {workerList.map((w) => (
                      <button
                        key={w.id}
                        type="button"
                        onClick={() => { setWorkerFilter(w.id); setWorkerDropdownOpen(false); }}
                        className={`w-full text-left px-3 py-1.5 text-sm transition-colors ${
                          workerFilter === w.id
                            ? "text-white bg-gray-800"
                            : "text-gray-300 hover:bg-gray-800 hover:text-white"
                        }`}
                      >
                        {w.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-400">
                Projects
              </h2>
            )}
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => onNewProject(workerFilter ?? undefined)}
                className="w-6 h-6 flex items-center justify-center rounded text-gray-500 hover:text-gray-100 hover:bg-gray-700"
                title="Add new project"
                aria-label="Add new project"
              >
                <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor">
                  <path d="M10 3a1 1 0 011 1v5h5a1 1 0 110 2h-5v5a1 1 0 11-2 0v-5H4a1 1 0 110-2h5V4a1 1 0 011-1z" />
                </svg>
              </button>
              <button
                type="button"
                onClick={() => { setLoading(); send({ type: "list_projects", payload: {} }); }}
                className="w-6 h-6 flex items-center justify-center rounded text-gray-500 hover:text-gray-300 hover:bg-gray-700 text-base leading-none"
                title="Refresh"
              >
                ↻
              </button>
            </div>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2 space-y-1">
          {loading && (
            <div className="flex items-center gap-2.5 px-3 py-6 justify-center">
              <span className="flex items-center gap-1 text-gray-400">
                <span className="typing-dot" />
                <span className="typing-dot" />
                <span className="typing-dot" />
              </span>
              <span className="text-xs text-gray-500">Loading projects…</span>
            </div>
          )}
          {!loading && filteredProjects.length === 0 && (
            <p className="text-xs text-gray-500 px-2 py-4">
              No projects found.
            </p>
          )}
          {filteredProjects.map((p) => (
            <ProjectItem
              key={p.id}
              project={p}
              isActive={activeProjectId === p.id}
              isExpanded={expanded.has(p.id)}
              sessions={sessionsByProject[p.id]}
              showWorker={multiWorker}
              onToggle={() => {
                if (p.available) toggleProject(p.id);
              }}
              onPickSession={(sid) => {
                if (!p.available) return;
                setActive(p.id);
                onPickSession(p.id, sid);
                onClose();
              }}
              onNewSession={() => {
                if (!p.available) return;
                setActive(p.id);
                onNewSession(p.id);
                onClose();
              }}
              onBrowseFiles={() => {
                if (!p.available) return;
                onBrowseFiles(p);
                onClose();
              }}
            />
          ))}
        </div>

        {/* Logout */}
        {logoutUrl && (
          <div className="px-4 py-3 border-t border-gray-800 shrink-0">
            <a
              href={logoutUrl}
              className="flex items-center gap-2 text-sm text-gray-500 hover:text-gray-200 transition-colors"
            >
              <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M3 4.5A1.5 1.5 0 014.5 3h5A1.5 1.5 0 0111 4.5v1a.5.5 0 01-1 0v-1A.5.5 0 009.5 4h-5a.5.5 0 00-.5.5v11a.5.5 0 00.5.5h5a.5.5 0 00.5-.5v-1a.5.5 0 011 0v1A1.5 1.5 0 019.5 17h-5A1.5 1.5 0 013 15.5v-11z" clipRule="evenodd" />
                <path fillRule="evenodd" d="M14.854 10.354a.5.5 0 000-.708l-3-3a.5.5 0 10-.708.708L13.293 9.5H7a.5.5 0 000 1h6.293l-2.147 2.146a.5.5 0 00.708.708l3-3z" clipRule="evenodd" />
              </svg>
              Logout
            </a>
          </div>
        )}
      </aside>
    </>
  );
}

type ProjectItemProps = {
  project: Project;
  isActive: boolean;
  isExpanded: boolean;
  sessions: SessionSummary[] | undefined;
  showWorker: boolean;
  onToggle: () => void;
  onPickSession: (sessionId: string) => void;
  onNewSession: () => void;
  onBrowseFiles: () => void;
};

function ProjectItem({
  project,
  isActive,
  isExpanded,
  sessions,
  showWorker,
  onToggle,
  onPickSession,
  onNewSession,
  onBrowseFiles,
}: ProjectItemProps) {
  const unavailable = !project.available;

  return (
    <div className={unavailable ? "opacity-40" : ""}>
      <div
        className={`flex items-center gap-1 rounded ${
          unavailable
            ? "cursor-not-allowed"
            : isActive
              ? "bg-gray-800"
              : "hover:bg-gray-800/50"
        }`}
        title={unavailable ? `${project.path} (not mounted)` : project.path}
      >
        <button
          type="button"
          onClick={onToggle}
          disabled={unavailable}
          className={`flex-1 min-w-0 text-left pl-2 pr-1 py-2 text-sm flex items-center gap-2 ${
            unavailable ? "cursor-not-allowed" : ""
          }`}
        >
          <span className="text-gray-500 text-xs shrink-0">
            {unavailable ? "○" : isExpanded ? "▾" : "▸"}
          </span>
          <span className="truncate flex-1">{project.name}</span>
          {showWorker && project.worker_label && (
            <span className="shrink-0 text-[10px] px-1 py-0.5 rounded bg-gray-700 text-gray-400">
              {project.worker_label}
            </span>
          )}
          {project.auto_approve && !unavailable && (
            <span
              className="w-1.5 h-1.5 rounded-full bg-yellow-400 shrink-0"
              title="Auto-approve enabled"
            />
          )}
        </button>
        {!unavailable && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onBrowseFiles();
            }}
            className="shrink-0 p-1.5 mr-1 text-gray-500 hover:text-gray-100 rounded hover:bg-gray-700"
            title="Browse files"
            aria-label={`Browse files in ${project.name}`}
          >
            <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor">
              <path d="M2 5a2 2 0 012-2h3.2a2 2 0 011.6.8L9.6 5H16a2 2 0 012 2v7a2 2 0 01-2 2H4a2 2 0 01-2-2V5z" />
            </svg>
          </button>
        )}
      </div>
      {isExpanded && (
        <div className="pl-6 pr-1 mt-1 space-y-0.5">
          <button
            type="button"
            onClick={onNewSession}
            className="w-full text-left text-xs px-2 py-1.5 rounded text-blue-400 hover:bg-blue-950/30"
          >
            + New session
          </button>
          {sessions === undefined && (
            <p className="text-xs text-gray-600 px-2 py-1">loading…</p>
          )}
          {sessions !== undefined && sessions.length === 0 && (
            <p className="text-xs text-gray-600 px-2 py-1">No sessions yet.</p>
          )}
          {sessions?.map((s) => (
            <button
              type="button"
              key={s.id}
              onClick={() => onPickSession(s.id)}
              className="w-full text-left px-2 py-1.5 rounded text-xs hover:bg-gray-800/50 group"
              title={s.id}
            >
              <div className="truncate text-gray-300 group-hover:text-white">
                {s.title ?? s.preview ?? s.id.slice(0, 8)}
              </div>
              <div className="truncate text-gray-600 text-[10px]">
                {s.message_count} msg · {new Date(s.mtime * 1000).toLocaleString()}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
