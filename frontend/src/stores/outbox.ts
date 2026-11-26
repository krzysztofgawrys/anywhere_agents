/**
 * Durable outbox for user prompts.
 *
 * A prompt is only forgotten once the worker acks it. Until then it lives
 * here AND in localStorage, so it survives every way the old code lost
 * messages: a half-open WebSocket that accepted the frame into a dead
 * buffer, a hub that couldn't reach the worker, a reload, an agent that was
 * mid-turn. The retry driver lives in `hooks/useOutbox.ts`; this module is
 * just the storage.
 *
 * Retries are safe because each entry carries a `client_msg_id` the worker
 * dedups on - a resend after reconnect can never make the agent see the same
 * prompt twice.
 */

import { create } from "zustand";
import type { ClientMessage, PromptImage } from "../types";

const LS_KEY = "claude_web_outbox";
/** Hard cap so a pathological loop can't fill localStorage. */
const MAX_ENTRIES = 25;
/** Entries older than this are dropped on load - too stale to resend. */
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

export type OutboxEntry = {
  /** client_msg_id - also the id of the chat bubble it belongs to. */
  id: string;
  /** Session this prompt was composed in; only flushed back into that one. */
  sessionId: string | null;
  projectId: number | null;
  /** The exact wire message to (re)send. */
  msg: ClientMessage;
  /** Raw text as typed, for handing back to the composer when we give up. */
  text: string;
  images: PromptImage[];
  attempts: number;
  createdAt: number;
  /**
   * True once a `send()` reported success. Only meaningful against a backend
   * that doesn't ack: there it's the sole (weak) evidence of delivery, and
   * the retry policy trusts it rather than risk duplicate prompts. With an
   * acking backend it is ignored.
   */
  optimisticallySent: boolean;
};

type OutboxState = {
  entries: OutboxEntry[];
  add: (entry: OutboxEntry) => void;
  /** Drop an entry (delivered, or given up on). Returns it if it was there. */
  remove: (id: string) => OutboxEntry | null;
  bumpAttempts: (id: string) => number;
  /** Record that a send() call reported success for this entry. */
  markSent: (id: string) => void;
  get: (id: string) => OutboxEntry | null;
  /** Undelivered entries for a session, oldest first. */
  forSession: (sessionId: string | null) => OutboxEntry[];
  /** Oldest undelivered entry - the one a legacy error without an id refers to. */
  oldest: () => OutboxEntry | null;
  clear: () => void;
};

/** Fresh delivery id. crypto.randomUUID needs a secure context; LAN-over-http
 *  installs don't have it, so fall back to a timestamp+random id. */
export function newClientMsgId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function load(): OutboxEntry[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as OutboxEntry[];
    if (!Array.isArray(parsed)) return [];
    const cutoff = Date.now() - MAX_AGE_MS;
    return parsed.filter(
      (e) =>
        e && typeof e.id === "string" && typeof e.createdAt === "number" && e.createdAt > cutoff
    );
  } catch {
    return [];
  }
}

function persist(entries: OutboxEntry[]): void {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(entries));
  } catch {
    // Quota or private-mode failure. In-memory state still works for this
    // tab; only cross-reload recovery is lost.
  }
}

export const useOutboxStore = create<OutboxState>((set, get) => ({
  entries: load(),

  add: (entry) =>
    set((s) => {
      const entries = [...s.entries.filter((e) => e.id !== entry.id), entry].slice(-MAX_ENTRIES);
      persist(entries);
      return { entries };
    }),

  remove: (id) => {
    const found = get().entries.find((e) => e.id === id) ?? null;
    if (found) {
      set((s) => {
        const entries = s.entries.filter((e) => e.id !== id);
        persist(entries);
        return { entries };
      });
    }
    return found;
  },

  markSent: (id) =>
    set((s) => {
      const entries = s.entries.map((e) =>
        e.id === id ? { ...e, optimisticallySent: true } : e
      );
      persist(entries);
      return { entries };
    }),

  bumpAttempts: (id) => {
    let attempts = 0;
    set((s) => {
      const entries = s.entries.map((e) => {
        if (e.id !== id) return e;
        attempts = e.attempts + 1;
        return { ...e, attempts };
      });
      persist(entries);
      return { entries };
    });
    return attempts;
  },

  get: (id) => get().entries.find((e) => e.id === id) ?? null,

  forSession: (sessionId) =>
    get()
      .entries.filter((e) => e.sessionId === sessionId)
      .sort((a, b) => a.createdAt - b.createdAt),

  oldest: () => {
    const sorted = [...get().entries].sort((a, b) => a.createdAt - b.createdAt);
    return sorted[0] ?? null;
  },

  clear: () => {
    persist([]);
    set({ entries: [] });
  },
}));
