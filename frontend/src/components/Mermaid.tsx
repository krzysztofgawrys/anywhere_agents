import { useEffect, useState } from "react";
import { DiagramFrame } from "./DiagramFrame";

// Mermaid is ~1MB+; load it lazily on the first diagram so it stays out of the
// main bundle (Vite code-splits the dynamic import). The module-level flag
// keeps initialize() to once per session.
let initialized = false;
let renderSeq = 0;

async function getMermaid() {
  const mermaid = (await import("mermaid")).default;
  if (!initialized) {
    mermaid.initialize({
      startOnLoad: false,
      theme: "dark", // matches the dark chat surface
      securityLevel: "strict", // sanitizes label HTML - we render model output
      logLevel: "fatal", // silence the parse warnings we hit mid-stream
      // Render labels as SVG <text>, not <foreignObject>. HTML labels taint a
      // canvas, which would break the lightbox's PNG export.
      htmlLabels: false,
      flowchart: { htmlLabels: false },
    });
    initialized = true;
  }
  return mermaid;
}

/** Renders a fenced ```mermaid block as an SVG diagram.
 *
 *  Built for streaming: while an assistant message is still arriving, the code
 *  is usually an incomplete (invalid) diagram. We validate with parse() first
 *  and only swap in the SVG once it's valid, so a half-written diagram never
 *  throws or blanks the chat - we show the raw source until it resolves, then
 *  keep the last good SVG if later edits are briefly invalid. A short debounce
 *  coalesces the burst of token updates during streaming. */
export function Mermaid({ code }: { code: string }) {
  const [svg, setSvg] = useState("");

  useEffect(() => {
    const source = code.trim();
    if (!source) {
      setSvg("");
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const mermaid = await getMermaid();
        const valid = await mermaid.parse(source, { suppressErrors: true });
        if (cancelled || !valid) return;
        const { svg: out } = await mermaid.render(`mermaid-${renderSeq++}`, source);
        if (!cancelled) setSvg(out);
      } catch {
        // Keep the last good SVG (or the source fallback below) in place.
      }
    }, 120);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [code]);

  if (svg) return <DiagramFrame svg={svg} />;

  // Before the first successful render (streaming) or if the diagram never
  // becomes valid: fall back to the raw source as an ordinary code block.
  return (
    <pre className="overflow-x-auto">
      <code className="language-mermaid">{code}</code>
    </pre>
  );
}
