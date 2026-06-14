import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { Mermaid } from "./Mermaid";
import { Svg } from "./Svg";

// Recursively collect raw text from a hast node. rehype-highlight may have
// wrapped the code in token <span>s; concatenating the text nodes recovers
// the original source.
function nodeText(node: any): string {
  if (!node) return "";
  if (node.type === "text") return node.value ?? "";
  if (Array.isArray(node.children)) return node.children.map(nodeText).join("");
  return "";
}

// A fenced ```mermaid block arrives as <pre><code class="language-mermaid">.
// Return its source, or null if this isn't a mermaid block.
function mermaidSource(preNode: any): string | null {
  const code = preNode?.children?.[0];
  const classes = code?.properties?.className;
  const isMermaid = Array.isArray(classes)
    ? classes.includes("language-mermaid")
    : classes === "language-mermaid";
  return code?.tagName === "code" && isMermaid ? nodeText(code) : null;
}

// A ```svg fence - or any code block whose body is a complete <svg> element
// (e.g. a model that fenced it as ```xml) - is rendered as a sanitized diagram
// rather than highlighted source. Returns the SVG source, or null.
function svgSource(preNode: any): string | null {
  const code = preNode?.children?.[0];
  if (code?.tagName !== "code") return null;
  const classes = code?.properties?.className;
  const isSvgLang = Array.isArray(classes)
    ? classes.includes("language-svg")
    : classes === "language-svg";
  const text = nodeText(code).trim();
  const looksSvg = text.startsWith("<svg") && text.endsWith("</svg>");
  return isSvgLang || looksSvg ? text : null;
}

// A model sometimes emits a diagram as a bare <svg>...</svg> in prose instead
// of a fenced block. react-markdown (no rehype-raw) would drop it as raw HTML,
// so we lift any complete top-level <svg> into a ```svg fence before parsing,
// keeping all *other* raw HTML escaped. Existing fenced code is left untouched.
function liftInlineSvg(md: string): string {
  if (!md.includes("<svg")) return md;
  const wrap = (s: string) =>
    s.replace(
      /<svg[\s\S]*?<\/svg>/gi,
      (m) => `\n\n\`\`\`svg\n${m}\n\`\`\`\n\n`,
    );
  return md
    .split(/(```[\s\S]*?```)/g)
    .map((part, i) => {
      if (i % 2 === 1) return part; // a closed fenced block - leave as-is
      // A stray ``` here opens a code fence that is still streaming (no closing
      // ``` yet). Only lift <svg> in the prose BEFORE it; leave the unterminated
      // fence untouched so we never double-wrap a ```svg block mid-stream.
      const fence = part.indexOf("```");
      return fence === -1
        ? wrap(part)
        : wrap(part.slice(0, fence)) + part.slice(fence);
    })
    .join("");
}

// Wide content (tables, code blocks) scrolls horizontally *inside* the
// container instead of stretching it past the viewport.
const mdComponents: Components = {
  table: ({ node: _node, ...props }) => (
    <div className="overflow-x-auto my-2 -mx-1 px-1">
      <table {...props} />
    </div>
  ),
  pre: ({ node, ...props }) => {
    // ```mermaid fences become auto-laid-out diagrams; a ```svg fence (or an
    // inline <svg>) becomes a sanitized, model-drawn diagram; everything else
    // stays a code block.
    const mermaid = mermaidSource(node);
    if (mermaid != null) return <Mermaid code={mermaid} />;
    const svg = svgSource(node);
    if (svg != null) return <Svg code={svg} />;
    return <pre className="overflow-x-auto" {...props} />;
  },
  a: ({ node: _node, ...props }) => (
    <a {...props} target="_blank" rel="noopener noreferrer" />
  ),
};

/** Shared markdown renderer (GFM + LaTeX math + syntax highlighting + mermaid
 *  diagrams) used by chat messages and the file browser's .md preview. Callers
 *  wrap it in their own `prose` container for typography styling.
 *
 *  Math: remark-math parses `$inline$` and `$$block$$` LaTeX; rehype-katex
 *  renders it via KaTeX (stylesheet imported above). Without this, Claude's
 *  `$f_s/2$`-style formulas show as raw dollar-sign text instead of rendering.
 *  Trade-off: single-`$` inline math means literal prices like "$5" can be
 *  misparsed as math - rare in technical chat, and disabling it would break
 *  the exact `$...$` formulas this is meant to fix.
 *
 *  Diagrams: a ```mermaid fence is drawn (auto-layout) by the lazy-loaded
 *  `Mermaid` component; a ```svg fence or an inline <svg> is sanitized with
 *  DOMPurify and drawn by `Svg` for high-fidelity, model-authored diagrams.
 *  Both intercept in the `pre` override and render instead of showing source. */
export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex, rehypeHighlight]}
      components={mdComponents}
    >
      {liftInlineSvg(children)}
    </ReactMarkdown>
  );
}
