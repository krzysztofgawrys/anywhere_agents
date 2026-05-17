import { useEffect, useState } from "react";
import type { ClientMessage, Project, SessionSummary } from "../types";
import { useProjectsStore } from "../stores/projects";

type Props = {
  send: (msg: ClientMessage) => boolean;
  connected: boolean;
  onPickSession: (projectId: number, sessionId: string) => void;
  onNewSession: (projectId: number) => void;
  open: boolean;
  onClose: () => void;
};

export function Sidebar({
  send,
  connected,
  onPickSession,
  onNewSession,
  open,
  onClose,
}: Props) {
  const projects = useProjectsStore((s) => s.projects);
  const sessionsByProject = useProjectsStore((s) => s.sessionsByProject);
  const activeProjectId = useProjectsStore((s) => s.activeProjectId);
  const setActive = useProjectsStore((s) => s.setActive);

  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  useEffect(() => {
    if (connected) {
      send({ type: "list_projects", payload: {} });
    }
  }, [connected, send]);

  const toggleProject = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
        // Fetch sessions for this project
        if (!sessionsByProject[id]) {
          send({ type: "list_sessions", payload: { project_id: id } });
        }
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
        className={`fixed md:relative inset-y-0 left-0 z-40 w-72 md:w-80 bg-gray-950 border-r border-gray-800 flex flex-col transform transition-transform ${
          open ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        }`}
      >
        <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-400">
            Projects
          </h2>
          <button
            type="button"
            onClick={() => send({ type: "list_projects", payload: {} })}
            className="text-xs text-gray-500 hover:text-gray-300"
            title="Refresh"
          >
            ↻
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2 space-y-1">
          {projects.length === 0 && (
            <p className="text-xs text-gray-500 px-2 py-4">
              {connected ? "No projects found." : "Connecting…"}
            </p>
          )}
          {projects.map((p) => (
            <ProjectItem
              key={p.id}
              project={p}
              isActive={activeProjectId === p.id}
              isExpanded={expanded.has(p.id)}
              sessions={sessionsByProject[p.id]}
              onToggle={() => toggleProject(p.id)}
              onPickSession={(sid) => {
                setActive(p.id);
                onPickSession(p.id, sid);
                onClose();
              }}
              onNewSession={() => {
                setActive(p.id);
                onNewSession(p.id);
                onClose();
              }}
            />
          ))}
        </div>
      </aside>
    </>
  );
}

type ProjectItemProps = {
  project: Project;
  isActive: boolean;
  isExpanded: boolean;
  sessions: SessionSummary[] | undefined;
  onToggle: () => void;
  onPickSession: (sessionId: string) => void;
  onNewSession: () => void;
};

function ProjectItem({
  project,
  isActive,
  isExpanded,
  sessions,
  onToggle,
  onPickSession,
  onNewSession,
}: ProjectItemProps) {
  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        className={`w-full text-left px-2 py-2 rounded text-sm flex items-center gap-2 ${
          isActive ? "bg-gray-800" : "hover:bg-gray-800/50"
        }`}
        title={project.path}
      >
        <span className="text-gray-500 text-xs shrink-0">{isExpanded ? "▾" : "▸"}</span>
        <span className="truncate flex-1">{project.name}</span>
        {project.auto_approve && (
          <span
            className="w-1.5 h-1.5 rounded-full bg-yellow-400 shrink-0"
            title="Auto-approve enabled"
          />
        )}
      </button>
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
