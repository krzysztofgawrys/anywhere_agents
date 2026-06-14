import { useEffect, useState } from "react";
import { DiagramFrame } from "./DiagramFrame";

// DOMPurify already ships inside the mermaid chunk; still load it via a lazy
// dynamic import so it stays out of the main bundle. Cache the promise so
// concurrent diagrams share one load.
let purifyPromise: Promise<typeof import("dompurify").default> | null = null;
function getPurify() {
  if (!purifyPromise) {
    purifyPromise = import("dompurify").then((m) => m.default);
  }
  return purifyPromise;
}

// SVG straight from the model is untrusted output: strip <script>, event
// handlers, and javascript: URLs, plus <foreignObject> - it would smuggle
// arbitrary HTML and also taint the lightbox's PNG export canvas.
function sanitize(
  DOMPurify: typeof import("dompurify").default,
  raw: string,
): string {
  return DOMPurify.sanitize(raw, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ["foreignObject"],
  });
}

// A model SVG with only a viewBox (no width/height) collapses to height 0 in
// the flex DiagramFrame, so nothing shows. Give it intrinsic pixel size from
// the viewBox; CSS (max-w-full / h-auto) then scales it down responsively.
function withIntrinsicSize(svgStr: string): string {
  try {
    const el = new DOMParser().parseFromString(svgStr, "image/svg+xml")
      .documentElement;
    if (el.nodeName.toLowerCase() !== "svg") return svgStr;
    const hasW = el.getAttribute("width");
    const hasH = el.getAttribute("height");
    if (hasW && hasH) return svgStr;
    const vb = (el.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
    const w = vb[2] ?? 0;
    const h = vb[3] ?? 0;
    if (!(w > 0 && h > 0)) return svgStr;
    if (!hasW) el.setAttribute("width", String(w));
    if (!hasH) el.setAttribute("height", String(h));
    return new XMLSerializer().serializeToString(el);
  } catch {
    return svgStr;
  }
}

/** Renders a model-authored ```svg block (or an inline <svg>, lifted to a fence
 *  in Markdown.tsx) as a sanitized, click-to-zoom diagram. This is the
 *  high-fidelity path: the model lays out every box, colour and connector
 *  itself, so it reaches claude.ai-style block diagrams that mermaid's
 *  auto-layout cannot.
 *
 *  Streaming-safe: we only sanitize and swap in the SVG once a complete
 *  <svg>...</svg> has arrived, otherwise the raw source shows as a code block. */
export function Svg({ code }: { code: string }) {
  const [svg, setSvg] = useState("");

  useEffect(() => {
    const source = code.trim();
    // Wait for a complete element before touching it - guards mid-stream
    // fragments that would sanitize/render as broken partials.
    if (!source.startsWith("<svg") || !source.endsWith("</svg>")) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const DOMPurify = await getPurify();
        const clean = sanitize(DOMPurify, source);
        if (!cancelled && clean.includes("<svg")) {
          setSvg(withIntrinsicSize(clean));
        }
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

  return (
    <pre className="overflow-x-auto">
      <code className="language-svg">{code}</code>
    </pre>
  );
}
