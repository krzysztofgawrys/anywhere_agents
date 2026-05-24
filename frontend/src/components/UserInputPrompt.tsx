import { useMemo, useState } from "react";
import type { ClientMessage } from "../types";
import type { PendingUserInput } from "../stores/chat";

type Props = {
  request: PendingUserInput;
  send: (msg: ClientMessage) => boolean;
  onAnswered: (toolUseId: string, answers: string[]) => void;
  /** When true, render only a compact header bar (no body). */
  minimized: boolean;
  /** Toggle the minimized state - lifted so App can hide the composer when expanded. */
  onToggleMinimize: () => void;
};

/**
 * Renders one or more questions from a single AskUserQuestion tool call.
 *
 * AskUserQuestion can pose multiple distinct questions at once (each with
 * its own options + free-text fallback). The agent expects an answer for
 * every question, so we collect them all locally and submit them as a
 * parallel `answers: string[]` array - one entry per question, in order.
 *
 * Layout:
 *  - Header is always visible (with minimize + dismiss buttons).
 *  - Body is internally scrollable (capped at ~60vh) so tall multi-question
 *    panels never push the rest of the UI off-screen.
 *  - Minimizing collapses to just the header so the user can re-read the chat
 *    above (e.g. to recall what the agent is asking), then expand to answer.
 *
 * Picking a quick-reply button auto-fills that question's answer. The user
 * can still edit it in the text field before pressing "Send".
 */
export function UserInputPrompt({
  request,
  send,
  onAnswered,
  minimized,
  onToggleMinimize,
}: Props) {
  const questions = useMemo(
    () =>
      request.questions.length > 0
        ? request.questions
        : [{ question: "", options: [] as string[] }],
    [request.questions]
  );

  // One free-text answer per question. Picking an option pre-fills the field
  // so users can tweak before sending; the field's value is the source of truth.
  const [answers, setAnswers] = useState<string[]>(() =>
    questions.map(() => "")
  );

  const setAnswer = (i: number, value: string) => {
    setAnswers((prev) => {
      const next = prev.slice();
      next[i] = value;
      return next;
    });
  };

  const allAnswered = answers.every((a) => a.trim().length > 0);
  const answeredCount = answers.filter((a) => a.trim().length > 0).length;

  const submit = () => {
    if (!allAnswered) return;
    const trimmed = answers.map((a) => a.trim());
    send({
      type: "user_input_response",
      payload: { tool_use_id: request.toolUseId, answers: trimmed },
    });
    onAnswered(request.toolUseId, trimmed);
  };

  const dismiss = () => {
    // Send empty strings (one per question) so the backend future resolves
    // and the agent can continue/abort the tool call gracefully.
    const empties = questions.map(() => "");
    send({
      type: "user_input_response",
      payload: { tool_use_id: request.toolUseId, answers: empties },
    });
    onAnswered(request.toolUseId, empties);
  };

  const handleKeyDown = (
    e: React.KeyboardEvent<HTMLInputElement>,
    i: number
  ) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      // Enter on the last field submits if all are answered; otherwise jump
      // focus to the next unanswered field.
      if (i === questions.length - 1) {
        submit();
      } else {
        const form = (e.currentTarget.form ?? null) as HTMLFormElement | null;
        const nextInput = form?.querySelector<HTMLInputElement>(
          `input[data-q-index="${i + 1}"]`
        );
        nextInput?.focus();
      }
    }
  };

  const headerLabel =
    questions.length > 1
      ? `Agent asks ${questions.length} questions`
      : "Agent asks";

  // ── Minimized: a thin clickable strip ──────────────────────────────────
  if (minimized) {
    return (
      <button
        type="button"
        onClick={onToggleMinimize}
        className="w-full border-t border-blue-600/40 bg-blue-950/30 hover:bg-blue-950/50 px-3 md:px-4 py-2 transition-colors text-left"
        title="Click to expand and answer"
      >
        <div className="max-w-3xl lg:max-w-5xl mx-auto flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <QuestionIcon />
            <span className="text-xs uppercase tracking-wide text-blue-300/80 font-semibold shrink-0">
              {headerLabel}
            </span>
            {questions.length > 1 && (
              <span className="text-xs text-blue-300/60 shrink-0">
                · {answeredCount}/{questions.length} answered
              </span>
            )}
            <span className="text-xs text-gray-400 truncate">
              - {questions[0]?.question || ""}
            </span>
          </div>
          <span className="text-xs text-blue-300/80 shrink-0 flex items-center gap-1">
            Expand
            <svg width="12" height="12" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M14.707 12.707a1 1 0 01-1.414 0L10 9.414l-3.293 3.293a1 1 0 01-1.414-1.414l4-4a1 1 0 011.414 0l4 4a1 1 0 010 1.414z"
                clipRule="evenodd"
              />
            </svg>
          </span>
        </div>
      </button>
    );
  }

  // ── Expanded: header + scrollable body, capped at 60vh ─────────────────
  return (
    <div className="border-t border-blue-600/40 bg-blue-950/20 flex flex-col max-h-[60vh] min-h-0">
      {/* Header - always visible */}
      <div className="px-3 md:px-4 py-2.5 shrink-0 border-b border-blue-600/20">
        <div className="max-w-3xl lg:max-w-5xl mx-auto flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <QuestionIcon />
            <span className="text-xs uppercase tracking-wide text-blue-300/80 font-semibold">
              {headerLabel}
            </span>
            {questions.length > 1 && (
              <span className="text-xs text-blue-300/60">
                · {answeredCount}/{questions.length} answered
              </span>
            )}
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={onToggleMinimize}
              className="text-gray-400 hover:text-gray-200 p-1 leading-none rounded transition-colors"
              title="Minimize (re-read the chat, then expand to answer)"
              aria-label="Minimize"
            >
              <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor">
                <path d="M4 10a1 1 0 011-1h10a1 1 0 110 2H5a1 1 0 01-1-1z" />
              </svg>
            </button>
            <button
              type="button"
              onClick={dismiss}
              className="text-gray-400 hover:text-gray-200 p-1 leading-none rounded transition-colors"
              title="Dismiss (send empty answers and let the agent continue)"
              aria-label="Dismiss"
            >
              <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor">
                <path
                  fillRule="evenodd"
                  d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
                  clipRule="evenodd"
                />
              </svg>
            </button>
          </div>
        </div>
      </div>

      {/* Scrollable body - questions + answers */}
      <div className="overflow-y-auto px-3 md:px-4 py-3 flex-1 min-h-0">
        <div className="max-w-3xl lg:max-w-5xl mx-auto">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              submit();
            }}
            className="space-y-4"
          >
            {questions.map((q, i) => (
              <div
                key={i}
                className={
                  questions.length > 1
                    ? "space-y-2 border-l-2 border-blue-600/30 pl-3"
                    : "space-y-2"
                }
              >
                {/* Question */}
                <p className="text-sm text-white leading-snug">
                  {questions.length > 1 && (
                    <span className="text-blue-400/80 font-mono mr-1.5">
                      {i + 1}.
                    </span>
                  )}
                  {q.question}
                </p>

                {/* Option buttons - pre-fill the field on click */}
                {q.options.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {q.options.map((opt, j) => {
                      const active = answers[i] === opt;
                      return (
                        <button
                          key={j}
                          type="button"
                          onClick={() => setAnswer(i, opt)}
                          className={
                            "px-3 py-1.5 rounded-lg text-sm border transition-colors text-left " +
                            (active
                              ? "bg-blue-600 border-blue-400 text-white"
                              : "bg-blue-700/50 hover:bg-blue-600/70 border-blue-600/40 hover:border-blue-500 text-blue-100")
                          }
                        >
                          {opt}
                        </button>
                      );
                    })}
                  </div>
                )}

                {/* Free-text input - the source of truth for this answer */}
                <input
                  type="text"
                  data-q-index={i}
                  value={answers[i] ?? ""}
                  onChange={(e) => setAnswer(i, e.target.value)}
                  onKeyDown={(e) => handleKeyDown(e, i)}
                  placeholder={
                    q.options.length > 0
                      ? "Or type a custom answer…"
                      : "Type your answer…"
                  }
                  className="w-full bg-gray-800 border border-gray-700 focus:border-blue-500 rounded-lg px-3 py-1.5 text-sm text-gray-100 placeholder-gray-500 outline-none"
                  autoFocus={i === 0}
                />
              </div>
            ))}

            {/* Single submit button - answers all questions at once */}
            <div className="flex justify-end pt-1">
              <button
                type="submit"
                disabled={!allAnswered}
                className="px-4 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                {questions.length > 1 ? "Send all" : "Send"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}

function QuestionIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 20 20"
      fill="currentColor"
      className="text-blue-400 shrink-0"
    >
      <path
        fillRule="evenodd"
        d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-8-3a1 1 0 00-.867.5 1 1 0 11-1.731-1A3 3 0 0113 8a3.001 3.001 0 01-2 2.83V11a1 1 0 11-2 0v-1a1 1 0 011-1 1 1 0 100-2zm0 8a1 1 0 100-2 1 1 0 000 2z"
        clipRule="evenodd"
      />
    </svg>
  );
}
