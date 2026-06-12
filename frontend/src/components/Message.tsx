import React from "react";
import type { ChatMessage } from "../types";
import { Markdown } from "./Markdown";
import { ToolBlock } from "./ToolBlock";

type Props = { message: ChatMessage; searchQuery?: string };

function highlightText(text: string, query: string): React.ReactNode {
  if (!query) return text;
  const parts = text.split(new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi"));
  return parts.map((part, i) =>
    part.toLowerCase() === query.toLowerCase()
      ? <mark key={i} className="search-match bg-yellow-400/30 text-yellow-200 rounded-sm">{part}</mark>
      : part
  );
}

export function Message({ message, searchQuery }: Props) {
  const isUser = message.role === "user";
  const sq = searchQuery ?? "";

  // System info banners - thin amber line, no bubble
  if (message.role === "system") {
    const infoBlock = message.blocks.find((b) => b.kind === "info");
    if (infoBlock && infoBlock.kind === "info") {
      return (
        <div className="flex items-center justify-center gap-2 py-1.5 text-xs text-amber-400/80">
          <span className="h-px flex-1 bg-amber-700/30" />
          <span>{infoBlock.text}</span>
          <span className="h-px flex-1 bg-amber-700/30" />
        </div>
      );
    }
    return null;
  }

  return (
    <div className="mb-3">
      <div
        className={`w-full min-w-0 overflow-hidden rounded-2xl px-4 py-3 ${
          isUser ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-100"
        }`}
      >
        {message.blocks.map((block, i) => {
          if (block.kind === "text") {
            // User text is raw input (often pasted code) - render verbatim,
            // preserving whitespace/newlines. Only assistant output is
            // markdown (Claude generates it intentionally).
            if (isUser) {
              return (
                <div
                  key={i}
                  className="whitespace-pre-wrap break-words text-sm md:text-base font-sans"
                >
                  {sq ? highlightText(block.text, sq) : block.text}
                </div>
              );
            }
            // For assistant markdown, when search is active render as plain
            // text so we can highlight without fighting ReactMarkdown.
            if (sq) {
              return (
                <div
                  key={i}
                  className="whitespace-pre-wrap break-words text-sm md:text-base"
                >
                  {highlightText(block.text, sq)}
                </div>
              );
            }
            return (
              <div
                key={i}
                className="prose prose-invert prose-sm md:prose-base max-w-none break-words"
              >
                <Markdown>{block.text}</Markdown>
              </div>
            );
          }
          if (block.kind === "thinking") {
            return (
              <div
                key={i}
                className="text-xs italic text-gray-400 border-l-2 border-gray-600 pl-2 my-2 whitespace-pre-wrap break-words"
              >
                {sq ? highlightText(block.text, sq) : block.text}
              </div>
            );
          }
          if (block.kind === "task") {
            // Inline task lifecycle event (Monitor / TaskCreate). Compact,
            // muted, mono - meant to read as a log line, not a chat bubble.
            const label =
              block.event_type === "notification"
                ? "task notification"
                : block.event_type === "progress"
                ? "task progress"
                : block.event_type === "updated"
                ? "task updated"
                : "task started";
            const status = block.status ? ` · ${block.status}` : "";
            return (
              <div
                key={i}
                className="my-1 px-2 py-1 text-xs font-mono border-l-2 border-amber-600 bg-gray-900/40 break-words"
              >
                <div className="text-amber-500">
                  {label}
                  {status}
                </div>
                {block.summary && (
                  <div className="text-gray-200 whitespace-pre-wrap">{block.summary}</div>
                )}
                {block.description && (
                  <div className="text-gray-400 whitespace-pre-wrap">{block.description}</div>
                )}
                {block.task_id && (
                  <div className="text-gray-500">task_id: {block.task_id}</div>
                )}
              </div>
            );
          }
          if (block.kind === "image") {
            const src = `data:${block.media_type};base64,${block.data_b64}`;
            return (
              <a
                key={i}
                href={src}
                target="_blank"
                rel="noopener noreferrer"
                className="block my-2"
              >
                <img
                  src={src}
                  alt="attachment"
                  className="rounded-lg max-h-80 max-w-full object-contain border border-gray-700"
                />
              </a>
            );
          }
          if (block.kind === "tool") {
            return (
              <ToolBlock
                key={i}
                name={block.name}
                input={block.input}
                result={block.result}
                isError={block.is_error}
              />
            );
          }
          // info blocks inside non-system messages (shouldn't happen, but safe)
          return null;
        })}
        {!message.finished && !isUser && message.blocks.length === 0 && (
          <span className="flex items-center gap-1 text-gray-400 py-1" aria-label="Agent is typing">
            <span className="typing-dot" />
            <span className="typing-dot" />
            <span className="typing-dot" />
          </span>
        )}
      </div>
    </div>
  );
}
