/**
 * Retry driver for the prompt outbox.
 *
 * Every prompt is sent through here and stays owned until the worker acks it
 * (`prompt_ack`) or we give up and hand the text back to the user. Nothing is
 * ever dropped silently - that was the whole bug class this exists to close.
 *
 * Why a timer and not just "resend on reconnect": a half-open WebSocket
 * reports `readyState === OPEN` for minutes after the network is gone, so
 * `send()` returning true proves nothing. The ack is the only real evidence
 * the prompt reached the agent, so an unacked entry is retried on a timer
 * regardless of what the socket claims. Resends are safe: the worker dedups
 * on `client_msg_id`.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";
import type { ClientMessage } from "../types";
import { useChatStore } from "../stores/chat";
import { useOutboxStore, type OutboxEntry } from "../stores/outbox";

/** How long to wait for an ack before resending. */
export const ACK_TIMEOUT_MS = 15_000;
/** Total time an entry may go unacked before we surrender it to the user. */
export const GIVE_UP_AFTER_MS = 3 * 60_000;

export type OutboxAction = "resend" | "park" | "give-up" | "retire";

/**
 * What to do with an unacked entry on a retry tick.
 *
 * Pure so the policy can be tested without a DOM: the timer plumbing around
 * it is trivial, the policy is not.
 *
 * - `park`: the entry's session isn't the one on screen. Prompts route to
 *   whatever session is active, so resending would drop the message into the
 *   wrong conversation. flush() revives it when that session comes back.
 * - `retire`: the backend serving this session has never sent a delivery
 *   receipt, so it predates the ack protocol. Only new workers dedup by
 *   client_msg_id, so retrying there could make the agent answer the same
 *   prompt several times. Fall back to the pre-outbox behaviour and trust the
 *   send that reported success. A send that never went out is still retried -
 *   that part was safe before and stays safe.
 * - `give-up`: acks are supported but none came in time. Hand the text back.
 */
export function nextAction(
  entry: {
    sessionId: string | null;
    createdAt: number;
    optimisticallySent: boolean;
  },
  activeSessionId: string | null,
  now: number,
  ackSupported: boolean
): OutboxAction {
  if (entry.sessionId !== activeSessionId) return "park";
  if (!ackSupported) return entry.optimisticallySent ? "retire" : "resend";
  if (now - entry.createdAt > GIVE_UP_AFTER_MS) return "give-up";
  return "resend";
}

type Options = {
  send: (msg: ClientMessage) => boolean;
  /** Called when a prompt is declared undeliverable. Give the text back. */
  onGiveUp: (entry: OutboxEntry) => void;
};

export function useOutbox({ send, onGiveUp }: Options) {
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const sendRef = useRef(send);
  const giveUpRef = useRef(onGiveUp);
  /**
   * Sessions whose backend has proven it sends delivery receipts.
   *
   * Tracked per session because the fleet is not upgraded atomically - the
   * Windows worker runs as a standalone process on the user's laptop and can
   * lag the containers by days. Retrying against a worker that can't dedup
   * would be worse than the bug we're fixing.
   */
  const ackCapable = useRef(new Set<string>());

  useEffect(() => {
    sendRef.current = send;
  }, [send]);
  useEffect(() => {
    giveUpRef.current = onGiveUp;
  }, [onGiveUp]);

  const clearTimer = useCallback((id: string) => {
    const t = timers.current.get(id);
    if (t !== undefined) {
      clearTimeout(t);
      timers.current.delete(id);
    }
  }, []);

  /** Stop tracking an entry and report it as lost. */
  const giveUp = useCallback(
    (id: string) => {
      clearTimer(id);
      const entry = useOutboxStore.getState().remove(id);
      if (entry) giveUpRef.current(entry);
    },
    [clearTimer]
  );

  // Declared as a ref-held function so tick and arm can reference each other
  // without a circular useCallback dependency.
  const tickRef = useRef<(id: string) => void>(() => {});

  const arm = useCallback((id: string) => {
    const t = setTimeout(() => tickRef.current(id), ACK_TIMEOUT_MS);
    timers.current.set(id, t);
  }, []);

  tickRef.current = (id: string) => {
    clearTimer(id);
    const entry = useOutboxStore.getState().get(id);
    // Gone from the outbox = acked. Nothing to do.
    if (!entry) return;

    const action = nextAction(
      entry,
      useChatStore.getState().activeSessionId,
      Date.now(),
      ackCapable.current.has(entry.sessionId ?? "")
    );
    if (action === "park") return;
    if (action === "give-up") {
      giveUp(id);
      return;
    }
    if (action === "retire") {
      // Pre-ack backend: stop tracking and let the bubble look delivered,
      // exactly as it did before the outbox existed.
      useOutboxStore.getState().remove(id);
      useChatStore.getState().markDelivered(id);
      return;
    }
    if (sendRef.current(entry.msg)) {
      useOutboxStore.getState().markSent(id);
      useOutboxStore.getState().bumpAttempts(id);
    }
    arm(id);
  };

  /** Send a prompt and keep retrying until the worker acks it. */
  const trackAndSend = useCallback(
    (entry: OutboxEntry) => {
      useOutboxStore.getState().add(entry);
      if (sendRef.current(entry.msg)) {
        useOutboxStore.getState().markSent(entry.id);
      }
      clearTimer(entry.id);
      arm(entry.id);
    },
    [arm, clearTimer]
  );

  /** Worker confirmed delivery - stop tracking. */
  const ack = useCallback(
    (id: string) => {
      clearTimer(id);
      const entry = useOutboxStore.getState().remove(id);
      // Remember that this session's backend speaks the ack protocol, so
      // future prompts on it get the full retry treatment.
      const sessionId = entry?.sessionId ?? useChatStore.getState().activeSessionId;
      if (sessionId) ackCapable.current.add(sessionId);
    },
    [clearTimer]
  );

  /**
   * Re-deliver everything still owed for a session, oldest first.
   * Called after a reconnect / resume, once the session is known to be ready.
   */
  const flush = useCallback(
    (sessionId: string | null) => {
      const pending = useOutboxStore.getState().forSession(sessionId);
      for (const entry of pending) {
        if (sendRef.current(entry.msg)) {
          useOutboxStore.getState().markSent(entry.id);
        }
        clearTimer(entry.id);
        arm(entry.id);
      }
      return pending.length;
    },
    [arm, clearTimer]
  );

  useEffect(() => {
    const map = timers.current;
    return () => {
      for (const t of map.values()) clearTimeout(t);
      map.clear();
    };
  }, []);

  // Stable object so callers can put it in dependency arrays.
  return useMemo(
    () => ({ trackAndSend, ack, flush, giveUp }),
    [trackAndSend, ack, flush, giveUp]
  );
}
