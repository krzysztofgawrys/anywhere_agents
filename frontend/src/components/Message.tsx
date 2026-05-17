import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { ChatMessage } from "../types";
import { ToolBlock } from "./ToolBlock";

type Props = { message: ChatMessage };

export function Message({ message }: Props) {
  const isUser = message.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-3`}>
      <div
        className={`max-w-[85%] md:max-w-[75%] rounded-2xl px-4 py-3 ${
          isUser ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-100"
        }`}
      >
        {message.blocks.map((block, i) => {
          if (block.kind === "text") {
            return (
              <div key={i} className="prose prose-invert prose-sm md:prose-base max-w-none">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  rehypePlugins={[rehypeHighlight]}
                >
                  {block.text}
                </ReactMarkdown>
              </div>
            );
          }
          if (block.kind === "thinking") {
            return (
              <div
                key={i}
                className="text-xs italic text-gray-400 border-l-2 border-gray-600 pl-2 my-2"
              >
                {block.text}
              </div>
            );
          }
          return (
            <ToolBlock
              key={i}
              name={block.name}
              input={block.input}
              result={block.result}
              isError={block.is_error}
            />
          );
        })}
        {!message.finished && !isUser && message.blocks.length === 0 && (
          <span className="inline-block animate-pulse text-gray-400">…</span>
        )}
      </div>
    </div>
  );
}
