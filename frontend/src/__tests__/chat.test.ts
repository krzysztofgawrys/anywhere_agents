/**
 * Tests for stores/chat.ts - the core chat state machine.
 *
 * Covers: message handling, streaming state transitions, session_started
 * reconnect behavior, busy error rollback.
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
  it("sets activeSessionId and cwd", () => {
    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/tmp", resumed: false, auto_approve: false, is_busy: false },
    } as ServerMessage);
    expect(state().activeSessionId).toBe("s1");
    expect(state().activeCwd).toBe("/tmp");
  });

  it("restores streaming when is_busy=true", () => {
    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/tmp", resumed: true, auto_approve: false, is_busy: true },
    } as ServerMessage);
    expect(state().status).toBe("streaming");
  });

  it("clears streaming when is_busy=false and was streaming", () => {
    // Simulate prior streaming state (e.g. app backgrounded, agent finished)
    useChatStore.setState({ status: "streaming", streamingActivity: { kind: "thinking" } });

    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/tmp", resumed: true, auto_approve: false, is_busy: false },
    } as ServerMessage);

    expect(state().status).toBe("idle");
    expect(state().streamingActivity).toBeNull();
    expect(state().currentAssistantId).toBeNull();
  });

  it("does not downgrade idle to idle", () => {
    // Already idle, is_busy=false should be a no-op for status
    dispatch({
      type: "session_started",
      payload: { session_id: "s1", cwd: "/tmp", resumed: false, auto_approve: false, is_busy: false },
    } as ServerMessage);
    expect(state().status).toBe("idle");
  });
});

// ── result ───────────────────────────────────────────────────────────

describe("result", () => {
  it("transitions from streaming to idle", () => {
    useChatStore.setState({ status: "streaming", streamingActivity: { kind: "text" } });
    dispatch({ type: "result", payload: { subtype: "success", is_error: false } } as ServerMessage);
    expect(state().status).toBe("idle");
    expect(state().streamingActivity).toBeNull();
    expect(state().currentAssistantId).toBeNull();
  });

  it("marks current assistant message as finished", () => {
    const msgId = "asst-1";
    useChatStore.setState({
      status: "streaming",
      currentAssistantId: msgId,
      messages: [{ id: msgId, role: "assistant", blocks: [], finished: false }],
    });
    dispatch({ type: "result", payload: { subtype: "success", is_error: false } } as ServerMessage);
    expect(state().messages[0]!.finished).toBe(true);
  });
});

// ── error (busy) ────────────────────────────────────────────────────

describe("error busy", () => {
  it("rolls back last user message and stays streaming", () => {
    useChatStore.setState({
      status: "streaming",
      messages: [
        { id: "u1", role: "user", blocks: [{ kind: "text", text: "hello" }], finished: true },
      ],
    });
    dispatch({
      type: "error",
      payload: { code: "busy", message: "Previous prompt still streaming" },
    } as ServerMessage);

    expect(state().messages).toHaveLength(0);
    expect(state().status).toBe("streaming");
  });

  it("does not roll back if last message is not user", () => {
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
});

// ── error (non-busy) ────────────────────────────────────────────────

describe("error non-busy", () => {
  it("sets error status and lastError", () => {
    dispatch({
      type: "error",
      payload: { code: "no_session", message: "Start a session first" },
    } as ServerMessage);
    expect(state().status).toBe("error");
    expect(state().lastError).toContain("no_session");
  });
});

// ── text_delta / thinking ───────────────────────────────────────────

describe("streaming events", () => {
  it("text_delta creates assistant message and appends text", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({ type: "text_delta", payload: { text: "Hello" } } as ServerMessage);

    const msgs = state().messages;
    expect(msgs).toHaveLength(1);
    expect(msgs[0]!.role).toBe("assistant");
    expect(msgs[0]!.blocks[0]).toEqual({ kind: "text", text: "Hello" });
  });

  it("consecutive text_delta merges into one block", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({ type: "text_delta", payload: { text: "Hel" } } as ServerMessage);
    dispatch({ type: "text_delta", payload: { text: "lo" } } as ServerMessage);

    const blocks = state().messages[0]!.blocks;
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toEqual({ kind: "text", text: "Hello" });
  });

  it("thinking sets streamingActivity to thinking", () => {
    useChatStore.setState({ status: "streaming" });
    dispatch({ type: "thinking", payload: { text: "hmm" } } as ServerMessage);
    expect(state().streamingActivity).toEqual({ kind: "thinking" });
  });
});

// ── appendUserPrompt ────────────────────────────────────────────────

describe("appendUserPrompt", () => {
  it("adds user message and sets streaming", () => {
    state().appendUserPrompt("test message");
    expect(state().messages).toHaveLength(1);
    expect(state().messages[0]!.role).toBe("user");
    expect(state().status).toBe("streaming");
    expect(state().streamingActivity).toEqual({ kind: "waiting" });
  });
});
