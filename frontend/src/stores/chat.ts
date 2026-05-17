import { create } from "zustand";
import type { ChatBlock, ChatMessage, ServerMessage } from "../types";

type Status = "idle" | "streaming" | "error";

export type LockInfo = {
  sessionId: string;
  lockedBy: string;
  lockedAt: number;
  projectId: number | null;
};

export type PendingPermission = {
  toolUseId: string;
  name: string;
  input: Record<string, unknown>;
  description: string | null;
};

type ChatState = {
  messages: ChatMessage[];
  status: Status;
  lastError: string | null;
  cost: number;
  currentAssistantId: string | null;
  activeSessionId: string | null;
  activeCwd: string | null;
  /** True when this client lost the lock — UI drops to read-only. */
  readOnly: boolean;
  /** Set when server rejected a resume_session due to existing lock. */
  pendingLock: LockInfo | null;
  /** Pagination state. */
  hasMore: boolean;
  oldestUuid: string | null;
  loadingOlder: boolean;
  /** Server says the project/session is in auto-approve. */
  autoApprove: boolean;
  /** Outstanding permission requests, oldest first. */
  pendingPermissions: PendingPermission[];

  appendUserPrompt: (text: string) => void;
  handleServerMessage: (msg: ServerMessage) => void;
  reset: () => void;
  /** Replace history with a freshly loaded page (most recent N). */
  setHistory: (messages: ChatMessage[], hasMore: boolean, oldestUuid: string | null) => void;
  /** Prepend an older page (pull-to-refresh). */
  prependHistory: (messages: ChatMessage[], hasMore: boolean, oldestUuid: string | null) => void;
  setLoadingOlder: (loading: boolean) => void;
  setPendingLock: (info: LockInfo | null) => void;
  removePendingPermission: (toolUseId: string) => void;
  setAutoApprove: (value: boolean) => void;
};

function newId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  status: "idle",
  lastError: null,
  cost: 0,
  currentAssistantId: null,
  activeSessionId: null,
  activeCwd: null,
  readOnly: false,
  pendingLock: null,
  hasMore: false,
  oldestUuid: null,
  loadingOlder: false,
  autoApprove: false,
  pendingPermissions: [],

  reset: () =>
    set({
      messages: [],
      status: "idle",
      lastError: null,
      cost: 0,
      currentAssistantId: null,
      activeSessionId: null,
      activeCwd: null,
      readOnly: false,
      pendingLock: null,
      hasMore: false,
      oldestUuid: null,
      loadingOlder: false,
      autoApprove: false,
      pendingPermissions: [],
    }),

  setHistory: (messages, hasMore, oldestUuid) =>
    set({
      messages,
      status: "idle",
      currentAssistantId: null,
      lastError: null,
      hasMore,
      oldestUuid,
      loadingOlder: false,
    }),

  prependHistory: (older, hasMore, oldestUuid) =>
    set((s) => ({
      messages: [...older, ...s.messages],
      hasMore,
      oldestUuid,
      loadingOlder: false,
    })),

  setLoadingOlder: (loading) => set({ loadingOlder: loading }),
  setPendingLock: (info) => set({ pendingLock: info }),

  removePendingPermission: (toolUseId) =>
    set((s) => ({
      pendingPermissions: s.pendingPermissions.filter((p) => p.toolUseId !== toolUseId),
    })),

  setAutoApprove: (value) => set({ autoApprove: value }),

  appendUserPrompt: (text) => {
    const msg: ChatMessage = {
      id: newId(),
      role: "user",
      blocks: [{ kind: "text", text }],
      finished: true,
    };
    set((s) => ({
      messages: [...s.messages, msg],
      status: "streaming",
      lastError: null,
    }));
  },

  handleServerMessage: (msg) => {
    switch (msg.type) {
      case "session_started":
        set({
          activeSessionId: msg.payload.session_id,
          activeCwd: msg.payload.cwd,
          readOnly: false,
          pendingLock: null,
          autoApprove: msg.payload.auto_approve,
          pendingPermissions: [],
        });
        return;

      case "permission_request":
        set((s) => ({
          pendingPermissions: [
            ...s.pendingPermissions,
            {
              toolUseId: msg.payload.tool_use_id,
              name: msg.payload.name,
              input: msg.payload.input,
              description: msg.payload.description,
            },
          ],
        }));
        return;

      case "session_locked":
        set({
          pendingLock: {
            sessionId: msg.payload.session_id,
            lockedBy: msg.payload.locked_by,
            lockedAt: msg.payload.locked_at,
            projectId: null,
          },
        });
        return;

      case "lock_revoked":
        set({
          readOnly: true,
          status: "idle",
          lastError: "Session taken over by another client — read-only mode.",
        });
        return;

      case "text_delta": {
        const assistantId = ensureAssistantMessage(get, set);
        appendBlock(set, assistantId, { kind: "text", text: msg.payload.text });
        return;
      }

      case "thinking": {
        const assistantId = ensureAssistantMessage(get, set);
        appendBlock(set, assistantId, { kind: "thinking", text: msg.payload.text });
        return;
      }

      case "tool_call": {
        const assistantId = ensureAssistantMessage(get, set);
        appendBlock(set, assistantId, {
          kind: "tool",
          tool_use_id: msg.payload.tool_use_id,
          name: msg.payload.name,
          input: msg.payload.input,
        });
        return;
      }

      case "tool_result": {
        set((s) => ({
          messages: s.messages.map((m) => ({
            ...m,
            blocks: m.blocks.map((b) =>
              b.kind === "tool" && b.tool_use_id === msg.payload.tool_use_id
                ? { ...b, result: msg.payload.content, is_error: msg.payload.is_error }
                : b
            ),
          })),
        }));
        return;
      }

      case "result": {
        set((s) => {
          const messages = s.messages.map((m) =>
            m.id === s.currentAssistantId ? { ...m, finished: true } : m
          );
          return {
            messages,
            status: "idle",
            currentAssistantId: null,
            cost: s.cost + (msg.payload.total_cost_usd ?? 0),
          };
        });
        return;
      }

      case "error": {
        set({ status: "error", lastError: `${msg.payload.code}: ${msg.payload.message}` });
        return;
      }

      case "project_updated":
        // The active project's auto_approve was toggled — reflect in UI.
        set({ autoApprove: msg.payload.auto_approve });
        return;

      case "projects":
      case "sessions":
      case "session_history":
      case "system":
      case "pong":
        return;
    }
  },
}));

function ensureAssistantMessage(
  get: () => ChatState,
  set: (partial: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>)) => void
): string {
  const current = get().currentAssistantId;
  if (current) return current;
  const id = newId();
  const msg: ChatMessage = { id, role: "assistant", blocks: [], finished: false };
  set((s) => ({ messages: [...s.messages, msg], currentAssistantId: id }));
  return id;
}

function appendBlock(
  set: (partial: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>)) => void,
  messageId: string,
  block: ChatBlock
): void {
  set((s) => ({
    messages: s.messages.map((m) => {
      if (m.id !== messageId) return m;
      if (block.kind === "text" && m.blocks.length > 0) {
        const last = m.blocks[m.blocks.length - 1];
        if (last && last.kind === "text") {
          return {
            ...m,
            blocks: [...m.blocks.slice(0, -1), { kind: "text", text: last.text + block.text }],
          };
        }
      }
      return { ...m, blocks: [...m.blocks, block] };
    }),
  }));
}
