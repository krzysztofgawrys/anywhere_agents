/**
 * Tests for stores/chat.ts - the core chat state machine.
 *
 * Black-box: dispatch server messages, check the full resulting state -
 * not just the one field we think should change.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useChatStore } from "../stores/chat";
import type { ServerMessage } from "../types";

function dispatch(msg: ServerMessage) {
  useChatStore.getState().handleServerMessage(msg);
}

function state() {
  return useChatStore.getState();
}

beforeEach(() => {
  useChatStore.getState().reset();
});

// ── session_started ──────────────────────────────────────────────────

describe("session_started", () => {
  it("sets session identity fields", () => {
    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/tmp", resumed: false, auto_approve: true, is_busy: false },
    } as ServerMessage);
    const s = state();
    expect(s.activeSessionId).toBe("s1");
    expect(s.activeCwd).toBe("/tmp");
    expect(s.autoApprove).toBe(true);
    expect(s.readOnly).toBe(false);
    expect(s.pendingLock).toBeNull();
    expect(s.pendingPermissions).toEqual([]);
    expect(s.pendingUserInputs).toEqual([]);
  });

  it("sets streaming when is_busy=true and was idle", () => {
    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/", resumed: true, auto_approve: false, is_busy: true },
    } as ServerMessage);
    expect(state().status).toBe("streaming");
  });

  it("keeps streaming when is_busy=true and already streaming", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/", resumed: true, auto_approve: false, is_busy: true },
    } as ServerMessage);
    expect(state().status).toBe("streaming");
  });

  it("clears stale streaming when is_busy=false (the reconnect fix)", () => {
    // This is the exact bug: app backgrounded, agent finishes, WS reconnects
    useChatStore.setState({
      status: "streaming",
      streamingActivity: { kind: "thinking" },
      currentAssistantId: "old-asst",
      messages: [
        { id: "old-asst", role: "assistant", blocks: [{ kind: "text", text: "partial" }], finished: false },
      ],
    });

    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/", resumed: true, auto_approve: false, is_busy: false },
    } as ServerMessage);

    const s = state();
    expect(s.status).toBe("idle");
    expect(s.streamingActivity).toBeNull();
    expect(s.currentAssistantId).toBeNull();
    // Unfinished messages marked as finished
    expect(s.messages[0]!.finished).toBe(true);
  });

  it("does not touch idle state when is_busy=false", () => {
    // Already idle, should stay idle without side effects
    useChatStore.setState({ status: "idle", messages: [] });
    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/", resumed: false, auto_approve: false, is_busy: false },
    } as ServerMessage);
    expect(state().status).toBe("idle");
  });
});

// ── result ───────────────────────────────────────────────────────────

describe("result", () => {
  it("transitions streaming to idle and clears all streaming state", () => {
    useChatStore.setState({
      status: "streaming",
      streamingActivity: { kind: "text" },
      currentAssistantId: "a1",
    });
    dispatch({ type: "result", payload: { subtype: "success", is_error: false } } as ServerMessage);

    const s = state();
    expect(s.status).toBe("idle");
    expect(s.streamingActivity).toBeNull();
    expect(s.currentAssistantId).toBeNull();
  });

  it("marks current assistant message as finished", () => {
    useChatStore.setState({
      status: "streaming",
      currentAssistantId: "a1",
      messages: [
        { id: "a1", role: "assistant", blocks: [{ kind: "text", text: "hi" }], finished: false },
      ],
    });
    dispatch({ type: "result", payload: { subtype: "success", is_error: false } } as ServerMessage);
    expect(state().messages[0]!.finished).toBe(true);
  });

  it("does not mark other messages as finished", () => {
    useChatStore.setState({
      status: "streaming",
      currentAssistantId: "a2",
      messages: [
        { id: "a1", role: "assistant", blocks: [], finished: false },
        { id: "a2", role: "assistant", blocks: [], finished: false },
      ],
    });
    dispatch({ type: "result", payload: { subtype: "success", is_error: false } } as ServerMessage);
    expect(state().messages[0]!.finished).toBe(false); // a1 untouched
    expect(state().messages[1]!.finished).toBe(true);  // a2 finished
  });

  it("is idempotent when already idle", () => {
    useChatStore.setState({ status: "idle", currentAssistantId: null });
    dispatch({ type: "result", payload: { subtype: "success", is_error: false } } as ServerMessage);
    expect(state().status).toBe("idle");
  });
});

// ── error (busy) ────────────────────────────────────────────────────

describe("error busy", () => {
  it("rolls back last user message and stays streaming", () => {
    useChatStore.setState({
      status: "streaming",
      streamingActivity: { kind: "waiting" },
      messages: [
        { id: "a1", role: "assistant", blocks: [], finished: true },
        { id: "u1", role: "user", blocks: [{ kind: "text", text: "hello" }], finished: true },
      ],
    });
    dispatch({
      type: "error",
      payload: { code: "busy", message: "Previous prompt still streaming" },
    } as ServerMessage);

    const s = state();
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0]!.id).toBe("a1"); // assistant kept
    expect(s.status).toBe("streaming");
    expect(s.streamingActivity).toBeNull(); // cleared
  });

  it("does not roll back if last message is assistant", () => {
    useChatStore.setState({
      status: "streaming",
      messages: [
        { id: "a1", role: "assistant", blocks: [], finished: true },
      ],
    });
    dispatch({
      type: "error",
      payload: { code: "busy", message: "Previous prompt still streaming" },
    } as ServerMessage);
    expect(state().messages).toHaveLength(1);
  });

  it("handles empty messages array", () => {
    useChatStore.setState({ status: "streaming", messages: [] });
    dispatch({
      type: "error",
      payload: { code: "busy", message: "Previous prompt still streaming" },
    } as ServerMessage);
    expect(state().messages).toHaveLength(0);
    expect(state().status).toBe("streaming");
  });
});

// ── error (non-busy) ────────────────────────────────────────────────

describe("error non-busy", () => {
  it("sets error status and lastError", () => {
    dispatch({
      type: "error",
      payload: { code: "no_session", message: "Start a session first" },
    } as ServerMessage);
    const s = state();
    expect(s.status).toBe("error");
    expect(s.lastError).toContain("no_session");
    expect(s.streamingActivity).toBeNull();
  });
});

// ── text_delta / thinking / tool_call ───────────────────────────────

describe("streaming events", () => {
  it("text_delta creates assistant message", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({ type: "text_delta", payload: { text: "Hello" } } as ServerMessage);

    const s = state();
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0]!.role).toBe("assistant");
    expect(s.messages[0]!.finished).toBe(false);
    expect(s.messages[0]!.blocks).toEqual([{ kind: "text", text: "Hello" }]);
    expect(s.streamingActivity).toEqual({ kind: "text" });
  });

  it("consecutive text_delta merges into one block", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({ type: "text_delta", payload: { text: "Hel" } } as ServerMessage);
    dispatch({ type: "text_delta", payload: { text: "lo " } } as ServerMessage);
    dispatch({ type: "text_delta", payload: { text: "world" } } as ServerMessage);

    const blocks = state().messages[0]!.blocks;
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toEqual({ kind: "text", text: "Hello world" });
  });

  it("thinking block does not merge with preceding text block", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({ type: "text_delta", payload: { text: "hi" } } as ServerMessage);
    dispatch({ type: "thinking", payload: { text: "hmm" } } as ServerMessage);

    const blocks = state().messages[0]!.blocks;
    expect(blocks).toHaveLength(2);
    expect(blocks[0]!.kind).toBe("text");
    expect(blocks[1]!.kind).toBe("thinking");
  });

  it("tool_call sets streamingActivity with tool name", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({
      type: "tool_call",
      payload: { tool_use_id: "t1", name: "Read", input: { file_path: "/foo" } },
    } as ServerMessage);

    const s = state();
    expect(s.streamingActivity).toEqual({ kind: "tool", name: "Read", detail: "/foo" });
    const block = s.messages[0]!.blocks[0]!;
    expect(block.kind).toBe("tool");
  });
});

// ── appendUserPrompt ────────────────────────────────────────────────

describe("appendUserPrompt", () => {
  it("adds user message with text and sets streaming", () => {
    state().appendUserPrompt("test");
    const s = state();
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0]!.role).toBe("user");
    expect(s.messages[0]!.finished).toBe(true);
    expect(s.messages[0]!.blocks).toEqual([{ kind: "text", text: "test" }]);
    expect(s.status).toBe("streaming");
    expect(s.streamingActivity).toEqual({ kind: "waiting" });
    expect(s.lastError).toBeNull();
  });

  it("adds image blocks when provided", () => {
    state().appendUserPrompt("look", [{ media_type: "image/png", data_b64: "abc" }]);
    const blocks = state().messages[0]!.blocks;
    expect(blocks).toHaveLength(2);
    expect(blocks[0]!.kind).toBe("text");
    expect(blocks[1]!.kind).toBe("image");
  });
});

// ── lock_revoked ────────────────────────────────────────────────────

describe("lock_revoked", () => {
  it("sets readOnly and idle", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({ type: "lock_revoked", payload: { session_id: "s1" } } as ServerMessage);
    const s = state();
    expect(s.readOnly).toBe(true);
    expect(s.status).toBe("idle");
  });
});

// ── permission_request ──────────────────────────────────────────────

describe("permission_request", () => {
  it("adds to pendingPermissions", () => {
    dispatch({
      type: "permission_request",
      payload: { tool_use_id: "t1", name: "Bash", input: { command: "ls" }, description: null },
    } as ServerMessage);
    expect(state().pendingPermissions).toHaveLength(1);
    expect(state().pendingPermissions[0]!.toolUseId).toBe("t1");
  });
});
