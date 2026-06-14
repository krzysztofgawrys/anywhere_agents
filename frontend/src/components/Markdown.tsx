import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

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

/** Shared markdown renderer (GFM + LaTeX math + syntax highlighting) used by
 *  chat messages and the file browser's .md preview. Callers wrap it in their
 *  own `prose` container for typography styling.
 *
 *  Math: remark-math parses `$inline$` and `$$block$$` LaTeX; rehype-katex
 *  renders it via KaTeX (stylesheet imported above). Without this, Claude's
 *  `$f_s/2$`-style formulas show as raw dollar-sign text instead of rendering.
 *  Trade-off: single-`$` inline math means literal prices like "$5" can be
 *  misparsed as math - rare in technical chat, and disabling it would break
 *  the exact `$...$` formulas this is meant to fix. */
export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex, rehypeHighlight]}
      components={mdComponents}
    >
      {children}
    </ReactMarkdown>
  );
}
