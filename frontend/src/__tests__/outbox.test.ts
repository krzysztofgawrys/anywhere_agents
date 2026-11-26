/**
 * Tests for the prompt outbox - the store that guarantees a typed message is
 * never the only copy of itself, and the retry policy around it.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessage } from "../types";

// The store reads localStorage at import time, so stub it first.
const store = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
});

const { useOutboxStore, newClientMsgId } = await import("../stores/outbox");
const { nextAction, GIVE_UP_AFTER_MS } = await import("../hooks/useOutbox");

function entry(id: string, sessionId: string | null = "s1", createdAt = 1000) {
  const msg: ClientMessage = {
    type: "prompt",
    payload: { text: `text-${id}`, client_msg_id: id },
  };
  return {
    id,
    sessionId,
    projectId: 1,
    msg,
    text: `text-${id}`,
    images: [],
    attempts: 0,
    createdAt,
    optimisticallySent: false,
  };
}

beforeEach(() => {
  store.clear();
  useOutboxStore.getState().clear();
});

// ── store ───────────────────────────────────────────────────────────

describe("outbox store", () => {
  it("keeps an entry until it is removed", () => {
    useOutboxStore.getState().add(entry("m1"));
    expect(useOutboxStore.getState().get("m1")?.text).toBe("text-m1");

    const removed = useOutboxStore.getState().remove("m1");
    expect(removed?.id).toBe("m1");
    expect(useOutboxStore.getState().get("m1")).toBeNull();
    expect(useOutboxStore.getState().remove("m1")).toBeNull();
  });

  it("persists to localStorage so a reload can still recover the text", () => {
    useOutboxStore.getState().add(entry("m1"));
    const raw = store.get("claude_web_outbox");
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!)[0].text).toBe("text-m1");
  });

  it("re-adding the same id replaces rather than duplicates", () => {
    useOutboxStore.getState().add(entry("m1"));
    useOutboxStore.getState().add(entry("m1"));
    expect(useOutboxStore.getState().entries).toHaveLength(1);
  });

  it("returns only a given session's entries, oldest first", () => {
    useOutboxStore.getState().add(entry("m2", "s1", 2000));
    useOutboxStore.getState().add(entry("m1", "s1", 1000));
    useOutboxStore.getState().add(entry("other", "s2", 500));

    const forS1 = useOutboxStore.getState().forSession("s1");
    expect(forS1.map((e) => e.id)).toEqual(["m1", "m2"]);
  });

  it("reports the oldest entry across sessions", () => {
    useOutboxStore.getState().add(entry("new", "s1", 5000));
    useOutboxStore.getState().add(entry("old", "s2", 100));
    expect(useOutboxStore.getState().oldest()?.id).toBe("old");
  });

  it("counts attempts", () => {
    useOutboxStore.getState().add(entry("m1"));
    expect(useOutboxStore.getState().bumpAttempts("m1")).toBe(1);
    expect(useOutboxStore.getState().bumpAttempts("m1")).toBe(2);
    expect(useOutboxStore.getState().get("m1")?.attempts).toBe(2);
  });

  it("mints unique ids", () => {
    const ids = new Set(Array.from({ length: 50 }, () => newClientMsgId()));
    expect(ids.size).toBe(50);
  });
});

// ── retry policy ────────────────────────────────────────────────────

describe("retry policy", () => {
  const now = 1_000_000;
  const acking = (e: Parameters<typeof nextAction>[0], active: string | null = "s1") =>
    nextAction(e, active, now, true);

  it("resends an unacked prompt for the active session", () => {
    expect(
      acking({ sessionId: "s1", createdAt: now, optimisticallySent: true })
    ).toBe("resend");
  });

  it("parks a prompt whose session is not on screen", () => {
    // Prompts route to the active session, so resending here would deliver
    // the message into a different conversation.
    const e = { sessionId: "s1", createdAt: now, optimisticallySent: true };
    expect(acking(e, "s2")).toBe("park");
    expect(acking(e, null)).toBe("park");
  });

  it("gives up once the entry is too old, handing the text back", () => {
    expect(
      acking({
        sessionId: "s1",
        createdAt: now - GIVE_UP_AFTER_MS - 1,
        optimisticallySent: true,
      })
    ).toBe("give-up");
  });

  it("keeps retrying right up to the deadline", () => {
    expect(
      acking({
        sessionId: "s1",
        createdAt: now - GIVE_UP_AFTER_MS + 1,
        optimisticallySent: true,
      })
    ).toBe("resend");
  });

  describe("against a backend that never acks (not yet upgraded)", () => {
    it("retires a prompt whose send reported success", () => {
      // Old workers don't dedup, so retrying could make the agent answer the
      // same message repeatedly. Trust the send, like the old code did.
      expect(
        nextAction(
          { sessionId: "s1", createdAt: now, optimisticallySent: true },
          "s1",
          now,
          false
        )
      ).toBe("retire");
    });

    it("still retries a prompt that never left the browser", () => {
      expect(
        nextAction(
          { sessionId: "s1", createdAt: now, optimisticallySent: false },
          "s1",
          now,
          false
        )
      ).toBe("resend");
    });

    it("never gives up and never grabs the text back", () => {
      // No receipts means no evidence of loss - surfacing an error here would
      // be a regression against a backend that behaves like it always did.
      expect(
        nextAction(
          {
            sessionId: "s1",
            createdAt: now - GIVE_UP_AFTER_MS - 1,
            optimisticallySent: true,
          },
          "s1",
          now,
          false
        )
      ).toBe("retire");
    });
  });
});
