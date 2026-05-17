import { create } from "zustand";
import type { ChatBlock, ChatMessage, ServerMessage } from "../types";

type Status = "idle" | "streaming" | "error";

type ChatState = {
  messages: ChatMessage[];
  status: Status;
  lastError: string | null;
  cost: number;
  currentAssistantId: string | null;
  activeSessionId: string | null;
  activeCwd: string | null;

  appendUserPrompt: (text: string) => void;
  handleServerMessage: (msg: ServerMessage) => void;
  reset: () => void;
  /** Load history into the message list (e.g. on resume). */
  loadHistory: (messages: ChatMessage[]) => void;
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

  reset: () =>
    set({
      messages: [],
      status: "idle",
      lastError: null,
      cost: 0,
      currentAssistantId: null,
      activeSessionId: null,
      activeCwd: null,
    }),

  loadHistory: (messages) =>
    set({
      messages,
      status: "idle",
      currentAssistantId: null,
      lastError: null,
    }),

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

      case "projects":
      case "sessions":
      case "session_history":
      case "project_updated":
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
