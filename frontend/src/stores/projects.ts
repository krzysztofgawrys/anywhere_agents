import { create } from "zustand";
import type { Project, ServerMessage, SessionSummary } from "../types";

type ProjectsState = {
  projects: Project[];
  sessionsByProject: Record<number, SessionSummary[]>;
  activeProjectId: number | null;
  loading: boolean;

  setActive: (projectId: number | null) => void;
  setLoading: () => void;
  handleServerMessage: (msg: ServerMessage) => void;
};

export const useProjectsStore = create<ProjectsState>((set) => ({
  projects: [],
  sessionsByProject: {},
  activeProjectId: null,
  loading: true,

  setActive: (projectId) => set({ activeProjectId: projectId }),
  setLoading: () => set({ loading: true }),

  handleServerMessage: (msg) => {
    switch (msg.type) {
      case "projects":
        set({ projects: msg.payload.projects, loading: false });
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
