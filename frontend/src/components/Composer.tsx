import { useState, type FormEvent, type KeyboardEvent } from "react";

type Props = {
  disabled: boolean;
  streaming: boolean;
  onSubmit: (text: string) => void;
  onInterrupt: () => void;
};

export function Composer({ disabled, streaming, onSubmit, onInterrupt }: Props) {
  const [text, setText] = useState("");

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <form
      onSubmit={submit}
      className="border-t border-gray-700 bg-gray-900 p-3 md:p-4"
    >
      <div className="flex gap-2 items-end max-w-3xl mx-auto">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={disabled ? "Connecting…" : "Message Claude (Enter to send, Shift+Enter for newline)"}
          rows={2}
          className="flex-1 resize-none rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-sm md:text-base focus:outline-none focus:border-blue-500 disabled:opacity-50"
          disabled={disabled}
        />
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
    </form>
  );
}
