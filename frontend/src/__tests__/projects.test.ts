/**
 * Tests for the background-activity tracking in stores/projects.ts.
 *
 * A session that streams output while the user is viewing a different session
 * gets flagged in `backgroundSessions` so the sidebar can highlight it (blue).
 * The flag clears when the user attaches to that session.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useProjectsStore } from "../stores/projects";
import { useChatStore } from "../stores/chat";
import type { ServerMessage } from "../types";

function dispatch(msg: ServerMessage) {
  useProjectsStore.getState().handleServerMessage(msg);
}

function bg() {
  return useProjectsStore.getState().backgroundSessions;
}

/** Make `sid` the session currently on screen. */
function setActiveSession(sid: string) {
  useChatStore.setState({ activeSessionId: sid });
}

beforeEach(() => {
  useChatStore.getState().reset();
  useProjectsStore.setState({ backgroundSessions: new Set() });
});

describe("background session activity", () => {
  it("flags a session that streams while another is active", () => {
    setActiveSession("active");
    dispatch({ type: "text_delta", payload: { session_id: "other", text: "hi" } } as ServerMessage);
    expect(bg().has("other")).toBe(true);
  });

  it("ignores activity from the active session", () => {
    setActiveSession("active");
    dispatch({ type: "text_delta", payload: { session_id: "active", text: "hi" } } as ServerMessage);
    expect(bg().has("active")).toBe(false);
    expect(bg().size).toBe(0);
  });

  it("flags on result/tool_call/task_event too", () => {
    setActiveSession("active");
    dispatch({
      type: "tool_call",
      payload: { session_id: "b1", tool_use_id: "t", name: "Read", input: {} },
    } as ServerMessage);
    dispatch({
      type: "result",
      payload: {
        session_id: "b2",
        subtype: "success",
        duration_ms: 1,
        total_cost_usd: 0,
        num_turns: 1,
        is_error: false,
      },
    } as ServerMessage);
    expect(bg().has("b1")).toBe(true);
    expect(bg().has("b2")).toBe(true);
  });

  it("clears the flag when the user attaches to that session (session_started)", () => {
    useProjectsStore.setState({ backgroundSessions: new Set(["other"]) });
    dispatch({
      type: "session_started",
      payload: { session_id: "other", cwd: "/", resumed: true, auto_approve: false, is_busy: false },
    } as ServerMessage);
    expect(bg().has("other")).toBe(false);
  });

  it("clearBackground removes a single session flag", () => {
    useProjectsStore.setState({ backgroundSessions: new Set(["a", "b"]) });
    useProjectsStore.getState().clearBackground("a");
    expect(bg().has("a")).toBe(false);
    expect(bg().has("b")).toBe(true);
  });

  it("produces a new Set reference on change (so selectors re-render)", () => {
    setActiveSession("active");
    const before = bg();
    dispatch({ type: "text_delta", payload: { session_id: "other", text: "hi" } } as ServerMessage);
    expect(bg()).not.toBe(before);
  });
});
