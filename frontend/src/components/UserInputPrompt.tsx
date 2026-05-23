import { useRef, useState } from "react";
import type { ClientMessage } from "../types";
import type { PendingUserInput } from "../stores/chat";

type Props = {
  request: PendingUserInput;
  send: (msg: ClientMessage) => boolean;
  onAnswered: (toolUseId: string, answer: string) => void;
};

export function UserInputPrompt({ request, send, onAnswered }: Props) {
  const [customText, setCustomText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const submit = (answer: string) => {
    const trimmed = answer.trim();
    if (!trimmed) return;
    send({
      type: "user_input_response",
      payload: { tool_use_id: request.toolUseId, answer: trimmed },
    });
    onAnswered(request.toolUseId, trimmed);
  };

  const dismiss = () => {
    // Send empty string so the backend future resolves and the agent continues.
    send({
      type: "user_input_response",
      payload: { tool_use_id: request.toolUseId, answer: "" },
    });
    onAnswered(request.toolUseId, "");
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit(customText);
    }
  };

  return (
    <div className="border-t border-blue-600/40 bg-blue-950/20 px-3 md:px-4 py-3">
      <div className="max-w-3xl lg:max-w-5xl mx-auto space-y-3">
        {/* Header */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
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
            <span className="text-xs uppercase tracking-wide text-blue-300/80 font-semibold">
              Agent asks
            </span>
          </div>
          <button
            type="button"
            onClick={dismiss}
            className="text-gray-500 hover:text-gray-300 text-sm px-1 leading-none"
            title="Dismiss"
          >
            ✕
          </button>
        </div>

        {/* Question */}
        <p className="text-sm text-white leading-snug">{request.question}</p>

        {/* Option buttons */}
        {request.options.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {request.options.map((opt, i) => (
              <button
                key={i}
                type="button"
                onClick={() => submit(opt)}
                className="px-3 py-1.5 rounded-lg text-sm bg-blue-700/50 hover:bg-blue-600/70 text-blue-100 border border-blue-600/40 hover:border-blue-500 transition-colors text-left"
              >
                {opt}
              </button>
            ))}
          </div>
        )}

        {/* Free-text input */}
        <div className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={customText}
            onChange={(e) => setCustomText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              request.options.length > 0
                ? "Or type a custom answer…"
                : "Type your answer…"
            }
            className="flex-1 bg-gray-800 border border-gray-700 focus:border-blue-500 rounded-lg px-3 py-1.5 text-sm text-gray-100 placeholder-gray-500 outline-none"
            autoFocus
          />
          <button
            type="button"
            onClick={() => submit(customText)}
            disabled={!customText.trim()}
            className="px-4 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
