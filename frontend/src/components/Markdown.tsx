import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

// Wide content (tables, code blocks) scrolls horizontally *inside* the
// container instead of stretching it past the viewport.
const mdComponents: Components = {
  table: ({ node: _node, ...props }) => (
    <div className="overflow-x-auto my-2 -mx-1 px-1">
      <table {...props} />
    </div>
  ),
  pre: ({ node: _node, ...props }) => (
    <pre className="overflow-x-auto" {...props} />
  ),
  a: ({ node: _node, ...props }) => (
    <a {...props} target="_blank" rel="noopener noreferrer" />
  ),
};

/** Shared markdown renderer (GFM + syntax highlighting) used by chat
 *  messages and the file browser's .md preview. Callers wrap it in their
 *  own `prose` container for typography styling. */
export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={mdComponents}
    >
      {children}
    </ReactMarkdown>
  );
}
