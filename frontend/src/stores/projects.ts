import { create } from "zustand";
import type { Project, ServerMessage, SessionSummary, WorkerInfo } from "../types";
import { useChatStore } from "./chat";

/**
 * Streamed message types that signal a session produced output. When they
 * arrive for a session other than the one on screen, we flag that session as
 * having unseen background activity so the sidebar can highlight it (and its
 * project / worker) in blue. The flag clears when the user opens that session.
 */
const ACTIVITY_TYPES: ReadonlySet<string> = new Set([
  "text_delta",
  "thinking",
  "tool_call",
  "tool_result",
  "task_event",
  "result",
]);

type ProjectsState = {
  projects: Project[];
  /** Workers the hub is aware of (from workers.json). Populated from the
   * `workers` field of every `projects` payload. Independent of how many
   * projects each worker has - lets the sidebar show empty workers too. */
  workers: WorkerInfo[];
  sessionsByProject: Record<number, SessionSummary[]>;
  activeProjectId: number | null;
  /** Session ids with unseen output from a background (non-active) session. */
  backgroundSessions: Set<string>;
  loading: boolean;

  setActive: (projectId: number | null) => void;
  setLoading: () => void;
  /** Clear the background-activity flag for a session (it's now being viewed). */
  clearBackground: (sessionId: string) => void;
  handleServerMessage: (msg: ServerMessage) => void;
};

export const useProjectsStore = create<ProjectsState>((set, get) => ({
  projects: [],
  workers: [],
  sessionsByProject: {},
  activeProjectId: null,
  backgroundSessions: new Set(),
  loading: true,

  setActive: (projectId) => set({ activeProjectId: projectId }),
  setLoading: () => set({ loading: true }),

  clearBackground: (sessionId) =>
    set((s) => {
      if (!s.backgroundSessions.has(sessionId)) return s;
      const next = new Set(s.backgroundSessions);
      next.delete(sessionId);
      return { backgroundSessions: next };
    }),

  handleServerMessage: (msg) => {
    // Flag background sessions that stream output while the user is elsewhere.
    if (ACTIVITY_TYPES.has(msg.type)) {
      const sid = (msg.payload as { session_id?: string }).session_id;
      if (sid && sid !== useChatStore.getState().activeSessionId) {
        const cur = get().backgroundSessions;
        if (!cur.has(sid)) {
          const next = new Set(cur);
          next.add(sid);
          set({ backgroundSessions: next });
        }
      }
      return;
    }

    switch (msg.type) {
      case "session_started": {
        // The user is now attached to this session - drop its background flag.
        const sid = msg.payload.session_id;
        const cur = get().backgroundSessions;
        if (cur.has(sid)) {
          const next = new Set(cur);
          next.delete(sid);
          set({ backgroundSessions: next });
        }
        return;
      }
      case "projects":
        set((s) => ({
          projects: msg.payload.projects,
          // Backend may omit `workers` on older hub builds - keep prior list
          // in that case so the dropdown doesn't blink to empty.
          workers: msg.payload.workers ?? s.workers,
          loading: false,
        }));
        return;
      case "sessions":
        set((s) => ({
          sessionsByProject: {
            ...s.sessionsByProject,
            [msg.payload.project_id]: msg.payload.sessions,
          },
        }));
        return;
      case "project_updated":
        set((s) => ({
          projects: s.projects.map((p) =>
            p.id === msg.payload.project_id
              ? { ...p, auto_approve: msg.payload.auto_approve }
              : p
          ),
        }));
        return;
    }
  },
}));
