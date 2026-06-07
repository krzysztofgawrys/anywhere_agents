import { useState } from "react";

/**
 * In-app soft keyboard for the terminal (hybrid mode). Renders only when the
 * user opts out of the OS keyboard. Every tap is forwarded as raw bytes via
 * onKey - the same path the accessory bar uses - so the Ctrl modifier on the
 * bar composes with these keys too.
 */
type Props = { onKey: (data: string) => void };

const LETTERS: string[][] = [
  "1234567890".split(""),
  "qwertyuiop".split(""),
  "asdfghjkl".split(""),
  "zxcvbnm".split(""),
];

const SYMBOLS: string[][] = [
  "1234567890".split(""),
  ["~", "`", "|", "\\", "/", "_", "-", "+", "=", "*"],
  ["!", "@", "#", "$", "%", "^", "&", "(", ")", ":"],
  ["[", "]", "{", "}", "<", ">", ";", "'", '"', "?"],
];

// preventDefault keeps focus/scroll stable; we never want the tap to blur.
const down = (fn: () => void) => (e: { preventDefault: () => void }) => {
  e.preventDefault();
  fn();
};

export function TerminalKeyboard({ onKey }: Props) {
  const [shift, setShift] = useState(false);
  const [symbols, setSymbols] = useState(false);

  const rows = symbols ? SYMBOLS : LETTERS;
  const lastRow = rows.length - 1;

  const tap = (ch: string) => {
    const out = !symbols && shift ? ch.toUpperCase() : ch;
    onKey(out);
    if (shift) setShift(false); // sticky one-shot, like a phone keyboard
  };

  const key =
    "flex-1 min-w-0 py-2.5 rounded text-sm font-mono bg-gray-800 text-gray-100 " +
    "active:bg-gray-700 border border-gray-700 select-none";
  const wide = `${key} grow-[1.6]`;

  return (
    <div className="flex flex-col gap-1 px-1.5 py-1.5 bg-gray-900 border-t border-gray-700 select-none">
      {rows.map((row, i) => (
        <div key={i} className="flex gap-1">
          {!symbols && i === lastRow && (
            <button
              type="button"
              aria-pressed={shift}
              className={`${wide} ${
                shift ? "!bg-blue-600 !text-white !border-blue-500" : ""
              }`}
              onPointerDown={down(() => setShift((s) => !s))}
            >
              ⇧
            </button>
          )}
          {row.map((ch) => (
            <button
              key={ch}
              type="button"
              className={key}
              onPointerDown={down(() => tap(ch))}
            >
              {!symbols && shift ? ch.toUpperCase() : ch}
            </button>
          ))}
          {i === lastRow && (
            <button
              type="button"
              className={wide}
              aria-label="Backspace"
              onPointerDown={down(() => onKey("\x7f"))}
            >
              ⌫
            </button>
          )}
        </div>
      ))}
      <div className="flex gap-1">
        <button
          type="button"
          className={wide}
          onPointerDown={down(() => setSymbols((s) => !s))}
        >
          {symbols ? "ABC" : "?#"}
        </button>
        <button
          type="button"
          className={`${key} grow-[4]`}
          aria-label="Space"
          onPointerDown={down(() => onKey(" "))}
        >
          space
        </button>
        <button
          type="button"
          className={wide}
          aria-label="Enter"
          onPointerDown={down(() => onKey("\r"))}
        >
          ⏎
        </button>
      </div>
    </div>
  );
}
