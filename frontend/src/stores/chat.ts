import { create } from "zustand";
import type { ChatBlock, ChatMessage, PromptImage, ServerMessage } from "../types";

type Status = "idle" | "streaming" | "error";

export type StreamingActivity =
  | { kind: "waiting" }
  | { kind: "thinking" }
  | { kind: "tool"; name: string; detail: string }
  | { kind: "text" };

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

export type PendingUserInput = {
  toolUseId: string;
  /**
   * One or more questions in this AskUserQuestion call. Each question gets
   * its own answer in the UI; we submit them as a parallel string[] array.
   */
  questions: { question: string; options: string[] }[];
};

type ChatState = {
  messages: ChatMessage[];
  status: Status;
  lastError: string | null;
  currentAssistantId: string | null;
  activeSessionId: string | null;
  activeCwd: string | null;
  /** What the backend is currently doing - updated on each streamed event. */
  streamingActivity: StreamingActivity | null;
  /** True when this client lost the lock - UI drops to read-only. */
  readOnly: boolean;
  /** Set when server rejected a resume_session due to existing lock. */
  pendingLock: LockInfo | null;
  /** Pagination state. */
  hasMore: boolean;
  oldestUuid: string | null;
  loadingOlder: boolean;
  /** Server says the project/session is in auto-approve. */
  autoApprove: boolean;
  /** Plan mode: prepend planning instruction to prompts, no writes. */
  planMode: boolean;
  /** Selected model passed to new/resume session. null = SDK default. */
  model: string | null;
  /** Selected effort/reasoning level. null = SDK default. */
  effort: string | null;
  /** Outstanding permission requests, oldest first. */
  pendingPermissions: PendingPermission[];
  /** AskUserQuestion requests waiting for the user to answer. */
  pendingUserInputs: PendingUserInput[];

  appendUserPrompt: (text: string, images?: PromptImage[]) => void;
  handleServerMessage: (msg: ServerMessage) => void;
  reset: () => void;
  /** Replace history with a freshly loaded page (most recent N). */
  setHistory: (messages: ChatMessage[], hasMore: boolean, oldestUuid: string | null) => void;
  /** Prepend an older page (pull-to-refresh). */
  prependHistory: (messages: ChatMessage[], hasMore: boolean, oldestUuid: string | null) => void;
  setLoadingOlder: (loading: boolean) => void;
  setPendingLock: (info: LockInfo | null) => void;
  removePendingPermission: (toolUseId: string) => void;
  removeUserInput: (toolUseId: string) => void;
  setAutoApprove: (value: boolean) => void;
  setPlanMode: (value: boolean) => void;
  setModel: (value: string | null) => void;
  setEffort: (value: string | null) => void;
};

function newId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * Streamed/content message types that only affect the chat currently on
 * screen. Every agent's output is forwarded over the single hub WS
 * connection, so when several agents run at once a background agent's events
 * would otherwise bleed into whichever chat the user is viewing. These types
 * carry a `session_id`; if it doesn't match the active session we drop them.
 * The background session's output is persisted on the worker and reloaded
 * from history when the user switches to it, so nothing is lost.
 *
 * Deliberately excluded:
 *  - permission_request: has no resume-time resend path and isn't denied when
 *    switching within the same WS, so dropping it would strand the prompt.
 *  - user_input_request: globally visible by the same reasoning as above.
 *  - session_locked / lock_revoked: relate to the resume handshake, where the
 *    active session id may not match yet.
 */
const SESSION_SCOPED_TYPES: ReadonlySet<string> = new Set([
  "text_delta",
  "thinking",
  "tool_call",
  "tool_result",
  "task_event",
  "result",
  "system",
]);

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  status: "idle",
  lastError: null,
  currentAssistantId: null,
  activeSessionId: null,
  activeCwd: null,
  streamingActivity: null,
  readOnly: false,
  pendingLock: null,
  hasMore: false,
  oldestUuid: null,
  loadingOlder: false,
  autoApprove: false,
  planMode: false,
  model: null,
  effort: null,
  pendingPermissions: [],
  pendingUserInputs: [],

  reset: () =>
    set({
      messages: [],
      status: "idle",
      lastError: null,
      currentAssistantId: null,
      activeSessionId: null,
      activeCwd: null,
      streamingActivity: null,
      readOnly: false,
      pendingLock: null,
      hasMore: false,
      oldestUuid: null,
      loadingOlder: false,
      autoApprove: false,
      planMode: false,
      model: null,
      effort: null,
      pendingPermissions: [],
      pendingUserInputs: [],
    }),

  setHistory: (messages, hasMore, oldestUuid) =>
    set((s) => ({
      messages,
      // Don't downgrade from "streaming" - if session_started just told us the
      // backend is mid-turn, preserve that state. Otherwise clear to "idle".
      status: s.status === "streaming" ? ("streaming" as const) : ("idle" as const),
      // Always clear currentAssistantId: the prior value was a frontend-
      // generated id (`Date.now()-rand`) attached to a live-streamed bubble
      // we just replaced. Disk-loaded messages use the JSONL entry uuid, so
      // the old id has no match in the new messages array. Without this
      // clear, subsequent stream events from a still-active backend turn
      // call `ensureAssistantMessage` → get the stale id → `appendBlock`
      // can't find it → events silently dropped until the user hits the
      // interrupt button (which clears the id and lets a new bubble form).
      currentAssistantId: null,
      lastError: null,
      hasMore,
      oldestUuid,
      loadingOlder: false,
    })),

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

  removeUserInput: (toolUseId) =>
    set((s) => ({
      pendingUserInputs: s.pendingUserInputs.filter((p) => p.toolUseId !== toolUseId),
    })),

  setAutoApprove: (value) => set({ autoApprove: value }),
  setPlanMode: (value) => set({ planMode: value }),
  setModel: (value) => set({ model: value }),
  setEffort: (value) => set({ effort: value }),

  appendUserPrompt: (text, images) => {
    const blocks: ChatBlock[] = [];
    if (text) blocks.push({ kind: "text", text });
    for (const img of images ?? []) {
      blocks.push({ kind: "image", media_type: img.media_type, data_b64: img.data_b64 });
    }
    const msg: ChatMessage = {
      id: newId(),
      role: "user",
      blocks,
      finished: true,
    };
    set((s) => ({
      messages: [...s.messages, msg],
      status: "streaming",
      streamingActivity: { kind: "waiting" },
      lastError: null,
    }));
  },

  handleServerMessage: (msg) => {
    // Ignore streamed content that belongs to a background session - it must
    // not render in the chat the user is currently viewing. See
    // SESSION_SCOPED_TYPES for why interactive/lock messages are exempt.
    if (SESSION_SCOPED_TYPES.has(msg.type)) {
      const active = get().activeSessionId;
      const sid = (msg.payload as { session_id?: string }).session_id;
      if (active !== null && sid !== undefined && sid !== active) return;
    }
    switch (msg.type) {
      case "session_started":
        set((s) => ({
          activeSessionId: msg.payload.session_id,
          activeCwd: msg.payload.cwd,
          readOnly: false,
          pendingLock: null,
          autoApprove: msg.payload.auto_approve,
          // Backend is the source of truth for the session's model and effort.
          // If the payload includes them, sync the UI; otherwise keep the
          // existing in-memory value (backwards compat with older workers
          // that don't send the field yet).
          ...(msg.payload.model !== undefined ? { model: msg.payload.model ?? null } : {}),
          ...(msg.payload.effort !== undefined ? { effort: msg.payload.effort ?? null } : {}),
          pendingPermissions: [],
          pendingUserInputs: [],
          // Sync streaming state with the backend on (re)connect.
          // Restore streaming if the session is mid-turn, or clear it if
          // the agent finished while we were away (e.g. app backgrounded
          // on mobile, WS dropped, result message missed).
          ...(msg.payload.is_busy && s.status !== "streaming"
            ? { status: "streaming" as const, streamingActivity: { kind: "waiting" as const } }
            : {}),
          ...(!msg.payload.is_busy && s.status === "streaming"
            ? {
                status: "idle" as const,
                streamingActivity: null,
                currentAssistantId: null,
                messages: s.messages.map((m) =>
                  !m.finished ? { ...m, finished: true } : m
                ),
              }
            : {}),
        }));
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

      case "user_input_request": {
        // De-dupe by tool_use_id - on reconnect the backend re-sends pending
        // questions and we don't want to stack duplicates of the same prompt.
        const incoming: PendingUserInput = {
          toolUseId: msg.payload.tool_use_id,
          questions: (msg.payload.questions ?? []).map((q) => ({
            question: q.question,
            options: q.options ?? [],
          })),
        };
        set((s) => {
          const existingIdx = s.pendingUserInputs.findIndex(
            (p) => p.toolUseId === incoming.toolUseId
          );
          if (existingIdx >= 0) {
            const next = s.pendingUserInputs.slice();
            next[existingIdx] = incoming;
            return { pendingUserInputs: next };
          }
          return { pendingUserInputs: [...s.pendingUserInputs, incoming] };
        });
        return;
      }

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
          lastError: "Session taken over by another client - read-only mode.",
        });
        return;

      case "text_delta": {
        set({ streamingActivity: { kind: "text" } });
        const assistantId = ensureAssistantMessage(get, set);
        appendBlock(set, assistantId, { kind: "text", text: msg.payload.text });
        return;
      }

      case "thinking": {
        set({ streamingActivity: { kind: "thinking" } });
        const assistantId = ensureAssistantMessage(get, set);
        appendBlock(set, assistantId, { kind: "thinking", text: msg.payload.text });
        return;
      }

      case "tool_call": {
        const detail = toolDetail(msg.payload.name, msg.payload.input);
        set({ streamingActivity: { kind: "tool", name: msg.payload.name, detail } });
        const assistantId = ensureAssistantMessage(get, set);
        appendBlock(set, assistantId, {
          kind: "tool",
          tool_use_id: msg.payload.tool_use_id,
          name: msg.payload.name,
          input: msg.payload.input,
        });
        return;
      }

      case "task_event": {
        // Monitor / TaskCreate emits task_started/progress/notification
        // between the tool_call and tool_result. Attach to the current
        // assistant message so the user sees progress instead of a frozen
        // "running…" tool block.
        const assistantId = ensureAssistantMessage(get, set);
        appendBlock(set, assistantId, {
          kind: "task",
          event_type: msg.payload.event_type,
          task_id: msg.payload.task_id,
          summary: msg.payload.summary,
          description: msg.payload.description,
          status: msg.payload.status,
          tool_use_id: msg.payload.tool_use_id,
        });
        return;
      }

      case "tool_result": {
        // Tool finished - back to thinking/generating until next event.
        set((s) => ({
          streamingActivity: { kind: "thinking" },
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
            streamingActivity: null,
          };
        });
        return;
      }

      case "error": {
        if (msg.payload.code === "busy") {
          set({ streamingActivity: null });
          // Backend is still processing the previous prompt. Roll back the
          // optimistically-appended user message (if any) and stay in streaming
          // state - the result will arrive shortly and re-enable the composer.
          set((s) => ({
            messages:
              s.messages.length > 0 &&
              s.messages[s.messages.length - 1]!.role === "user"
                ? s.messages.slice(0, -1)
                : s.messages,
            status: "streaming" as const,
          }));
        } else {
          set({ status: "error", lastError: `${msg.payload.code}: ${msg.payload.message}`, streamingActivity: null });
        }
        return;
      }

      case "project_updated":
        // The active project's auto_approve was toggled - reflect in UI.
        set({ autoApprove: msg.payload.auto_approve });
        return;

      case "projects":
      case "sessions":
      case "session_history":
      case "pong":
        return;

      case "system": {
        const sub = msg.payload.subtype;
        if (sub === "model_changed") {
          const data = msg.payload.data as Record<string, unknown> | undefined;
          const model = (data?.model as string) ?? null;
          const label = model
            ? model.replace("claude-", "").split("-202")[0]
            : "default";
          const infoMsg: ChatMessage = {
            id: newId(),
            role: "system",
            blocks: [{ kind: "info", text: `Model changed to ${label}` }],
            finished: true,
          };
          // Persist the model in Zustand so the useEffect in App.tsx
          // picks up the change and writes it to localStorage. Without
          // this, /model commands issued via the SDK CLI only display
          // the info bubble but never update the persisted state -
          // causing the choice to vanish on reload.
          set({ model, messages: [...get().messages, infoMsg] });
        }
        if (sub === "effort_changed") {
          const data = msg.payload.data as Record<string, unknown> | undefined;
          const effort = (data?.effort as string) ?? null;
          const infoMsg: ChatMessage = {
            id: newId(),
            role: "system",
            blocks: [{ kind: "info", text: `Effort changed to ${effort ?? "default"}` }],
            finished: true,
          };
          set({ effort, messages: [...get().messages, infoMsg] });
        }
        return;
      }
    }
  },
}));

/** Produce a short human-readable detail string from a tool call's input. */
function toolDetail(name: string, input: Record<string, unknown>): string {
  // Priority: command > file_path > path > pattern > query > description
  const pick = ["command", "file_path", "path", "pattern", "query", "description"];
  for (const k of pick) {
    if (typeof input[k] === "string") {
      const s = input[k] as string;
      return s.length > 60 ? s.slice(0, 60) + "…" : s;
    }
  }
  // Fallback: first string value in the object
  for (const v of Object.values(input)) {
    if (typeof v === "string" && v.length > 0) {
      return v.length > 60 ? v.slice(0, 60) + "…" : v;
    }
  }
  return name;
}

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
      // Merge consecutive text/thinking deltas into one growing block so the
      // bubble doesn't fragment into hundreds of tiny pieces during streaming.
      if (
        (block.kind === "text" || block.kind === "thinking") &&
        m.blocks.length > 0
      ) {
        const last = m.blocks[m.blocks.length - 1];
        if (last && last.kind === block.kind) {
          return {
            ...m,
            blocks: [
              ...m.blocks.slice(0, -1),
              { kind: block.kind, text: last.text + block.text },
            ],
          };
        }
      }
      return { ...m, blocks: [...m.blocks, block] };
    }),
  }));
}
