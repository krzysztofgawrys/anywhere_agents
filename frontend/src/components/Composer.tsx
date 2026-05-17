import { useCallback, useState, type FormEvent, type KeyboardEvent } from "react";
import { useSpeechInput } from "../hooks/useSpeechInput";

type Props = {
  disabled: boolean;
  streaming: boolean;
  autoApproveActive: boolean;
  onSubmit: (text: string, autoApproveOnce: boolean) => void;
  onInterrupt: () => void;
};

export function Composer({
  disabled,
  streaming,
  autoApproveActive,
  onSubmit,
  onInterrupt,
}: Props) {
  const [text, setText] = useState("");
  const [autoOnce, setAutoOnce] = useState(false);

  const onSpeechFinal = useCallback((spoken: string) => {
    setText((cur) => (cur ? `${cur} ${spoken}` : spoken));
  }, []);

  const speech = useSpeechInput({ onFinal: onSpeechFinal });

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed, autoOnce);
    setText("");
    setAutoOnce(false);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const showAutoToggle = !autoApproveActive;

  return (
    <form
      onSubmit={submit}
      className="border-t border-gray-700 bg-gray-900 p-3 md:p-4"
    >
      <div className="max-w-3xl mx-auto">
        {showAutoToggle && (
          <label className="flex items-center gap-2 text-xs text-gray-400 mb-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoOnce}
              onChange={(e) => setAutoOnce(e.target.checked)}
              className="accent-blue-500"
            />
            <span>Auto-approve tools for this prompt</span>
          </label>
        )}
        {autoApproveActive && (
          <div className="text-xs text-yellow-300/80 mb-2 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-yellow-400" />
            Auto-approve is ON for this project
          </div>
        )}
        <div className="flex gap-2 items-end">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={
              disabled
                ? "Connecting…"
                : "Message Claude (Enter to send, Shift+Enter for newline)"
            }
            rows={2}
            className="flex-1 resize-none rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-sm md:text-base focus:outline-none focus:border-blue-500 disabled:opacity-50"
            disabled={disabled}
          />
          {speech.supported && (
            <button
              type="button"
              onClick={speech.listening ? speech.stop : speech.start}
              disabled={disabled}
              className={`p-2 rounded-lg font-medium ${
                speech.listening
                  ? "bg-red-600 hover:bg-red-700 animate-pulse"
                  : "bg-gray-700 hover:bg-gray-600"
              } disabled:opacity-40`}
              title={speech.listening ? "Stop dictation" : "Dictate"}
              aria-label={speech.listening ? "Stop dictation" : "Start dictation"}
            >
              <MicIcon />
            </button>
          )}
          {streaming ? (
            <button
              type="button"
              onClick={onInterrupt}
              className="px-4 py-2 rounded-lg font-medium bg-red-600 hover:bg-red-700"
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={disabled || !text.trim()}
              className="px-4 py-2 rounded-lg font-medium bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Send
            </button>
          )}
        </div>
      </div>
    </form>
  );
}

function MicIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z" />
    </svg>
  );
}
