/**
 * Bootstrap auth store - pending credential requests from workers.
 *
 * A worker that boots with no agent credentials emits `auth_needed`.
 * The hub forwards it to this browser; we record it here keyed by
 * worker_id. The sidebar banner reads this map and renders a CTA;
 * the modal lets the user paste an API key (api_key flow) or
 * acknowledge a device-code URL (device_code flow). On `auth_status`
 * with state=completed/expired/failed we drop the entry.
 *
 * See docs/bootstrap-auth-protocol.md for the WS message shape.
 */

import { create } from "zustand";
import type { ServerMessage } from "../types";

export interface PendingAuth {
  worker_id: string;
  agent_type: string;
  flow: "api_key" | "device_code";
  request_id: string;
  instructions: string;
  expires_at: number;
  device_code?: string;
  verification_url?: string;
  verification_url_complete?: string;
  // Latest status update from the worker (post-submit progress).
  state: "pending" | "polling" | "completed" | "failed" | "expired";
  error?: string;
}

interface AuthState {
  pending: Record<string, PendingAuth>;
  // worker_id of the currently-open modal, or null when nothing focused.
  modalFor: string | null;

  handleServerMessage: (msg: ServerMessage) => void;
  openModal: (workerId: string) => void;
  closeModal: () => void;
  dismiss: (workerId: string) => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  pending: {},
  modalFor: null,

  handleServerMessage: (msg) => {
    if (msg.type === "auth_needed") {
      const p = msg.payload;
      set((state) => ({
        pending: {
          ...state.pending,
          [p.worker_id]: {
            worker_id: p.worker_id,
            agent_type: p.agent_type,
            flow: p.flow,
            request_id: p.request_id,
            instructions: p.instructions,
            expires_at: p.expires_at,
            device_code: p.device_code,
            verification_url: p.verification_url,
            verification_url_complete: p.verification_url_complete,
            state: "pending",
          },
        },
      }));
      return;
    }
    if (msg.type === "auth_status") {
      const p = msg.payload;
      set((state) => {
        const existing = state.pending[p.worker_id];
        if (!existing) return state;
        // Terminal states - drop the entry and any open modal.
        if (
          p.state === "completed" ||
          p.state === "expired" ||
          p.state === "failed"
        ) {
          const { [p.worker_id]: _, ...rest } = state.pending;
          return {
            pending: rest,
            modalFor: state.modalFor === p.worker_id ? null : state.modalFor,
          };
        }
        // Non-terminal: just record the new state on the existing entry.
        return {
          pending: {
            ...state.pending,
            [p.worker_id]: { ...existing, state: p.state, error: p.error },
          },
        };
      });
    }
  },

  openModal: (workerId) => set({ modalFor: workerId }),
  closeModal: () => set({ modalFor: null }),
  dismiss: (workerId) =>
    set((state) => {
      const { [workerId]: _, ...rest } = state.pending;
      return {
        pending: rest,
        modalFor: state.modalFor === workerId ? null : state.modalFor,
      };
    }),
}));
