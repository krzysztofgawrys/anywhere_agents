import { create } from "zustand";
import type { Project, ServerMessage, SessionSummary, WorkerInfo } from "../types";

type ProjectsState = {
  projects: Project[];
  /** Workers the hub is aware of (from workers.json). Populated from the
   * `workers` field of every `projects` payload. Independent of how many
   * projects each worker has - lets the sidebar show empty workers too. */
  workers: WorkerInfo[];
  sessionsByProject: Record<number, SessionSummary[]>;
  activeProjectId: number | null;
  loading: boolean;

  setActive: (projectId: number | null) => void;
  setLoading: () => void;
  handleServerMessage: (msg: ServerMessage) => void;
};

export const useProjectsStore = create<ProjectsState>((set) => ({
  projects: [],
  workers: [],
  sessionsByProject: {},
  activeProjectId: null,
  loading: true,

  setActive: (projectId) => set({ activeProjectId: projectId }),
  setLoading: () => set({ loading: true }),

  handleServerMessage: (msg) => {
    switch (msg.type) {
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
