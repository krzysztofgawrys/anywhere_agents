import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { matchCommands, type SlashCommand } from "../commands";
import { useSpeechInput } from "../hooks/useSpeechInput";
import type { PromptImage } from "../types";
import {
  MAX_IMAGE_BYTES,
  fileToPromptImage,
  imagesFromDataTransfer,
  isSupportedImage,
  previewUrl,
} from "../utils/image";

type Props = {
  disabled: boolean;
  streaming: boolean;
  autoApproveActive: boolean;
  /** Extra slash commands (e.g. custom commands loaded from the worker). */
  extraCommands?: SlashCommand[];
  /** Called when the user selects a slash command from autocomplete. */
  onCommand?: (name: string) => void;
  onSubmit: (
    text: string,
    autoApproveOnce: boolean,
    images: PromptImage[],
    streamTokens: boolean,
  ) => void;
  onInterrupt: (pendingPrompt?: {
    text: string;
    autoApprove: boolean;
    images: PromptImage[];
    stream: boolean;
  }) => void;
};

const STREAM_TOKENS_KEY = "claude-web:stream-tokens";

function readStreamPref(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(STREAM_TOKENS_KEY) === "1";
  } catch {
    return false;
  }
}

function writeStreamPref(value: boolean): void {
  try {
    window.localStorage.setItem(STREAM_TOKENS_KEY, value ? "1" : "0");
  } catch {
    // localStorage may be unavailable (private mode, etc.) - silently ignore.
  }
}

export function Composer({
  disabled,
  streaming,
  autoApproveActive,
  extraCommands,
  onCommand,
  onSubmit,
  onInterrupt,
}: Props) {
  const [text, setText] = useState("");
  const [autoOnce, setAutoOnce] = useState(false);
  const [images, setImages] = useState<PromptImage[]>([]);
  const [imageError, setImageError] = useState<string | null>(null);
  const [dragHover, setDragHover] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [streamTokens, setStreamTokens] = useState<boolean>(readStreamPref);
  const [cmdIndex, setCmdIndex] = useState(0);

  const cmdMatches = useMemo(
    () => matchCommands(text.trim(), extraCommands),
    [text, extraCommands],
  );
  const showCmds = cmdMatches.length > 0 && !streaming;

  // Touch devices have no Shift+Enter. There, Enter must insert a newline
  // (default textarea behaviour) and sending is done via the Send button.
  // Desktop keeps Enter=send / Shift+Enter=newline.
  const isTouch =
    typeof window !== "undefined" &&
    (window.matchMedia?.("(pointer: coarse)").matches ||
      navigator.maxTouchPoints > 0);

  const fileInputRef = useRef<HTMLInputElement>(null);

  const onSpeechFinal = useCallback((spoken: string) => {
    setText((cur) => (cur ? `${cur} ${spoken}` : spoken));
  }, []);
  const speech = useSpeechInput({ onFinal: onSpeechFinal });

  const addFiles = useCallback(async (files: File[]) => {
    const errors: string[] = [];
    const toAdd: PromptImage[] = [];
    for (const f of files) {
      if (!isSupportedImage(f)) {
        errors.push(`${f.name || "image"}: unsupported type ${f.type}`);
        continue;
      }
      if (f.size > MAX_IMAGE_BYTES) {
        errors.push(`${f.name || "image"}: too large (${(f.size / 1024 / 1024).toFixed(1)}MB)`);
        continue;
      }
      try {
        toAdd.push(await fileToPromptImage(f));
      } catch (e) {
        errors.push(`${f.name || "image"}: read failed`);
      }
    }
    if (toAdd.length > 0) setImages((prev) => [...prev, ...toAdd]);
    setImageError(errors.length > 0 ? errors.join("; ") : null);
  }, []);

  const onPaste = useCallback(
    (e: ClipboardEvent<HTMLTextAreaElement>) => {
      const files = imagesFromDataTransfer(e.clipboardData);
      if (files.length > 0) {
        e.preventDefault();
        void addFiles(files);
      }
    },
    [addFiles]
  );

  const onDrop = useCallback(
    (e: DragEvent<HTMLFormElement>) => {
      e.preventDefault();
      setDragHover(false);
      const files = imagesFromDataTransfer(e.dataTransfer);
      if (files.length > 0) void addFiles(files);
    },
    [addFiles]
  );

  const onFilesPicked = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const list = e.target.files;
      if (!list) return;
      const files: File[] = [];
      for (let i = 0; i < list.length; i++) {
        const f = list.item(i);
        if (f) files.push(f);
      }
      void addFiles(files);
      // Reset so re-picking the same file fires onChange again
      e.target.value = "";
    },
    [addFiles]
  );

  const pickCommand = useCallback(
    (cmd: SlashCommand) => {
      setText("");
      setCmdIndex(0);
      onCommand?.(cmd.name);
    },
    [onCommand],
  );

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if ((!trimmed && images.length === 0) || disabled || streaming) return;
    // If there's an exact slash-command match, execute it instead of sending.
    if (trimmed.startsWith("/") && cmdMatches.length > 0) {
      const exact = cmdMatches.find((c) => `/${c.name}` === trimmed);
      if (exact) {
        pickCommand(exact);
        return;
      }
    }
    onSubmit(trimmed, autoOnce, images, streamTokens);
    setText("");
    setAutoOnce(false);
    setImages([]);
    setImageError(null);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Slash-command autocomplete navigation
    if (showCmds) {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setCmdIndex((i) => (i > 0 ? i - 1 : cmdMatches.length - 1));
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setCmdIndex((i) => (i < cmdMatches.length - 1 ? i + 1 : 0));
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        pickCommand(cmdMatches[cmdIndex]!);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setText("");
        return;
      }
    }
    // On touch devices Enter always = newline; send via the button only.
    if (isTouch) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const removeImage = (i: number) =>
    setImages((prev) => prev.filter((_, idx) => idx !== i));

  const showAutoToggle = !autoApproveActive;
  const hasContent = text.trim().length > 0 || images.length > 0;

  return (
    <form
      onSubmit={submit}
      onDragOver={(e) => {
        e.preventDefault();
        if (e.dataTransfer.types.includes("Files")) setDragHover(true);
      }}
      onDragLeave={() => setDragHover(false)}
      onDrop={onDrop}
      className={`border-t bg-gray-900 p-3 md:p-4 transition-colors ${
        dragHover ? "border-blue-500 bg-blue-950/20" : "border-gray-700"
      }`}
    >
      <div className="max-w-3xl lg:max-w-5xl mx-auto">
        {images.length > 0 && (
          <div className="flex gap-2 mb-2 overflow-x-auto pb-1">
            {images.map((img, i) => (
              <div key={i} className="relative shrink-0">
                <img
                  src={previewUrl(img)}
                  alt={`pasted-${i}`}
                  className="h-16 w-16 object-cover rounded border border-gray-700"
                />
                <button
                  type="button"
                  onClick={() => removeImage(i)}
                  className="absolute -top-1.5 -right-1.5 bg-gray-800 hover:bg-red-700 text-white rounded-full w-5 h-5 flex items-center justify-center text-xs border border-gray-700"
                  title="Remove"
                  aria-label="Remove image"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        {imageError && (
          <div className="text-xs text-red-400 mb-2">{imageError}</div>
        )}
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
        <div className="flex gap-2 items-end relative">
          {showCmds && (
            <div className="absolute bottom-full left-0 mb-1 w-72 rounded-lg bg-gray-800 border border-gray-700 shadow-xl overflow-hidden z-20">
              {cmdMatches.map((cmd, i) => (
                <button
                  key={cmd.name}
                  type="button"
                  onMouseDown={(e) => {
                    e.preventDefault(); // keep textarea focused
                    pickCommand(cmd);
                  }}
                  className={`w-full flex items-baseline gap-2 px-3 py-2 text-sm text-left ${
                    i === cmdIndex
                      ? "bg-blue-600/40 text-white"
                      : "hover:bg-gray-700 text-gray-200"
                  }`}
                >
                  <span className="font-mono font-medium">/{cmd.name}</span>
                  <span className="text-xs text-gray-400 truncate">
                    {cmd.description}
                  </span>
                </button>
              ))}
            </div>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            multiple
            className="hidden"
            onChange={onFilesPicked}
          />
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            placeholder={
              disabled
                ? "Connecting…"
                : isTouch
                  ? "Message agent - tap ➤ to send. Paste / drop images."
                  : "Message agent (Enter to send, Shift+Enter for newline). Paste / drop images."
            }
            rows={4}
            className="flex-1 min-w-0 resize-none rounded-lg bg-gray-800 border border-gray-700 px-3 py-2 text-sm md:text-base focus:outline-none focus:border-blue-500 disabled:opacity-50 min-h-[112px] max-h-60 overflow-y-auto"
            disabled={disabled}
          />
          <div className="flex flex-col gap-1 shrink-0">
            <div className="relative">
              {menuOpen && (
                <>
                  {/* backdrop to close on outside tap */}
                  <div
                    className="fixed inset-0 z-10"
                    onClick={() => setMenuOpen(false)}
                  />
                  <div className="absolute bottom-full right-0 mb-1 z-20 w-44 rounded-lg bg-gray-800 border border-gray-700 shadow-xl overflow-hidden">
                    <button
                      type="button"
                      onClick={() => {
                        setMenuOpen(false);
                        fileInputRef.current?.click();
                      }}
                      disabled={disabled}
                      className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-left hover:bg-gray-700 disabled:opacity-40"
                    >
                      <ClipIcon />
                      <span>Attach image</span>
                    </button>
                    {speech.supported && (
                      <button
                        type="button"
                        onClick={() => {
                          setMenuOpen(false);
                          if (speech.listening) speech.stop();
                          else speech.start();
                        }}
                        disabled={disabled}
                        className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-left hover:bg-gray-700 disabled:opacity-40 border-t border-gray-700"
                      >
                        <MicIcon />
                        <span>{speech.listening ? "Stop dictation" : "Dictate"}</span>
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => {
                        setStreamTokens((v) => {
                          const next = !v;
                          writeStreamPref(next);
                          return next;
                        });
                      }}
                      className="w-full flex items-center gap-2 px-3 py-2.5 text-sm text-left hover:bg-gray-700 border-t border-gray-700"
                      role="menuitemcheckbox"
                      aria-checked={streamTokens}
                    >
                      <span
                        className={`w-4 h-4 rounded border flex items-center justify-center text-xs shrink-0 ${
                          streamTokens
                            ? "bg-blue-600 border-blue-500 text-white"
                            : "border-gray-500"
                        }`}
                        aria-hidden
                      >
                        {streamTokens ? "✓" : ""}
                      </span>
                      <span>Stream tokens</span>
                    </button>
                  </div>
                </>
              )}
              <button
                type="button"
                onClick={() => setMenuOpen((v) => !v)}
                disabled={disabled}
                className={`p-3 rounded-lg disabled:opacity-40 ${
                  speech.listening
                    ? "bg-red-600 hover:bg-red-700 animate-pulse"
                    : "bg-gray-700 hover:bg-gray-600"
                }`}
                title="More"
                aria-label="More options"
                aria-haspopup="true"
                aria-expanded={menuOpen}
              >
                <MenuIcon />
              </button>
            </div>
            {streaming ? (
              <button
                type="button"
                onClick={() => {
                  const trimmed = text.trim();
                  if (trimmed || images.length > 0) {
                    // Interrupt + send the typed prompt after agent stops
                    onInterrupt({
                      text: trimmed,
                      autoApprove: autoOnce,
                      images: [...images],
                      stream: streamTokens,
                    });
                    setText("");
                    setAutoOnce(false);
                    setImages([]);
                    setImageError(null);
                  } else {
                    onInterrupt();
                  }
                }}
                className="p-3 rounded-lg font-medium bg-red-600 hover:bg-red-700"
                title="Stop"
                aria-label="Stop"
              >
                <StopIcon />
              </button>
            ) : (
              <button
                type="submit"
                disabled={disabled || !hasContent}
                className="p-3 rounded-lg font-medium bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Send"
                aria-label="Send"
              >
                <SendIcon />
              </button>
            )}
          </div>
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

function ClipIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M16.5 6.5v9a4.5 4.5 0 0 1-9 0V6a3 3 0 0 1 6 0v8.5a1.5 1.5 0 1 1-3 0V7H9v7.5a3 3 0 1 0 6 0V6a4.5 4.5 0 1 0-9 0v9.5a6 6 0 1 0 12 0v-9h-1.5z" />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M3 20.5v-6l8-2.5-8-2.5v-6l19 8.5z" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
    </svg>
  );
}
