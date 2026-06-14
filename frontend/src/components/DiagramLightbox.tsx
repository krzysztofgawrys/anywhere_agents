import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

const BG = "#0d1117"; // matches the github-dark code surface diagrams sit on
const MIN_SCALE = 0.1;
const MAX_SCALE = 16;
const TAP_SLOP = 6; // px of movement still counted as a click, not a drag

const clamp = (v: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, v));

// Turn a mermaid <svg> string into a standalone, explicitly-sized SVG. Mermaid
// emits `width="100%"` plus a `max-width` style, which leave a saved file or an
// <img> with no intrinsic size; we pull real dimensions from the viewBox.
function standaloneSvg(svgStr: string): { text: string; w: number; h: number } {
  const el = new DOMParser().parseFromString(svgStr, "image/svg+xml")
    .documentElement as unknown as SVGSVGElement;
  const vb = (el.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
  const w = vb[2] || 0;
  const h = vb[3] || 0;
  el.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  if (w && h) {
    el.setAttribute("width", String(w));
    el.setAttribute("height", String(h));
  }
  el.style.maxWidth = "none";
  return { text: new XMLSerializer().serializeToString(el), w, h };
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

type Transform = { scale: number; x: number; y: number };

/** Fullscreen viewer for a rendered mermaid diagram: pan (drag / one finger),
 *  zoom (wheel, pinch, or the +/- buttons), and download as SVG or PNG.
 *  Rendered through a portal so `position: fixed` escapes any transformed
 *  ancestor in the chat transcript. */
export function DiagramLightbox({
  svg,
  onClose,
}: {
  svg: string;
  onClose: () => void;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const natural = useRef({ w: 0, h: 0 });
  const [t, setT] = useState<Transform>({ scale: 1, x: 0, y: 0 });
  const [ready, setReady] = useState(false);

  // Center + scale the diagram to fit the viewport with a small margin.
  const fit = useCallback(() => {
    const stage = stageRef.current;
    const { w, h } = natural.current;
    if (!stage || !w || !h) return;
    const scale = Math.min(stage.clientWidth / w, stage.clientHeight / h) * 0.9;
    setT({
      scale,
      x: (stage.clientWidth - w * scale) / 2,
      y: (stage.clientHeight - h * scale) / 2,
    });
  }, []);

  // Give the injected <svg> its intrinsic pixel size so the transform works in
  // real units, record that size, then fit it to the screen.
  useLayoutEffect(() => {
    const el = contentRef.current?.querySelector("svg");
    if (!el) return;
    const vb = el.viewBox?.baseVal;
    const w = vb?.width || el.getBoundingClientRect().width;
    const h = vb?.height || el.getBoundingClientRect().height;
    el.style.width = `${w}px`;
    el.style.height = `${h}px`;
    el.style.maxWidth = "none";
    natural.current = { w, h };
    fit();
    setReady(true);
  }, [svg, fit]);

  // Escape closes; lock body scroll while open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  // Zoom while keeping the point under (clientX, clientY) pinned on screen.
  const zoomAt = useCallback(
    (clientX: number, clientY: number, factor: number) => {
      const stage = stageRef.current;
      if (!stage) return;
      const rect = stage.getBoundingClientRect();
      const px = clientX - rect.left;
      const py = clientY - rect.top;
      setT((prev) => {
        const scale = clamp(prev.scale * factor, MIN_SCALE, MAX_SCALE);
        const k = scale / prev.scale;
        return { scale, x: px - (px - prev.x) * k, y: py - (py - prev.y) * k };
      });
    },
    [],
  );

  // Native wheel listener so preventDefault works (React's onWheel is passive).
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.15 : 1 / 1.15);
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const zoomFromButton = (factor: number) => {
    const stage = stageRef.current;
    if (!stage) return;
    const r = stage.getBoundingClientRect();
    zoomAt(r.left + stage.clientWidth / 2, r.top + stage.clientHeight / 2, factor);
  };

  // Pointer-driven pan (mouse / one finger) and pinch-zoom (two fingers). A
  // press that barely moves and lands off the diagram is treated as a click on
  // the backdrop and closes the viewer.
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const drag = useRef<{ x: number; y: number } | null>(null);
  const pinch = useRef<number | null>(null);
  const tap = useRef<{ x: number; y: number; onContent: boolean } | null>(null);

  const onPointerDown = (e: React.PointerEvent) => {
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 1) {
      drag.current = { x: e.clientX, y: e.clientY };
      tap.current = {
        x: e.clientX,
        y: e.clientY,
        onContent: !!contentRef.current?.contains(e.target as Node),
      };
    } else {
      tap.current = null; // multi-touch is never a backdrop tap
      if (pointers.current.size === 2) {
        drag.current = null;
        const [a, b] = [...pointers.current.values()];
        if (a && b) pinch.current = Math.hypot(a.x - b.x, a.y - b.y);
      }
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 2 && pinch.current != null) {
      const [a, b] = [...pointers.current.values()];
      if (!a || !b) return;
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, dist / pinch.current);
      pinch.current = dist;
      return;
    }
    if (drag.current) {
      if (
        tap.current &&
        Math.hypot(e.clientX - tap.current.x, e.clientY - tap.current.y) >
          TAP_SLOP
      ) {
        tap.current = null;
      }
      const dx = e.clientX - drag.current.x;
      const dy = e.clientY - drag.current.y;
      drag.current = { x: e.clientX, y: e.clientY };
      setT((prev) => ({ ...prev, x: prev.x + dx, y: prev.y + dy }));
    }
  };

  const onPointerUp = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    if (pointers.current.size === 0) {
      drag.current = null;
      const tp = tap.current;
      tap.current = null;
      if (tp && !tp.onContent) onClose();
    } else {
      const [p] = [...pointers.current.values()];
      if (p) drag.current = { x: p.x, y: p.y };
    }
  };

  const downloadSvg = () => {
    const { text } = standaloneSvg(svg);
    saveBlob(
      new Blob([text], { type: "image/svg+xml;charset=utf-8" }),
      "diagram.svg",
    );
  };

  // Rasterize at 2x for a crisp PNG. Relies on labels being SVG <text> (mermaid
  // is configured with htmlLabels:false) - a <foreignObject> would taint the
  // canvas and make toBlob throw.
  const downloadPng = () => {
    const { text, w, h } = standaloneSvg(svg);
    if (!w || !h) return;
    const url = URL.createObjectURL(
      new Blob([text], { type: "image/svg+xml;charset=utf-8" }),
    );
    const img = new Image();
    img.onload = () => {
      const ratio = 2;
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(w * ratio);
      canvas.height = Math.round(h * ratio);
      const ctx = canvas.getContext("2d");
      if (ctx) {
        ctx.fillStyle = BG;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas.toBlob((b) => b && saveBlob(b, "diagram.png"), "image/png");
      }
      URL.revokeObjectURL(url);
    };
    img.onerror = () => URL.revokeObjectURL(url);
    img.src = url;
  };

  const btn =
    "rounded px-2 py-1 text-xs font-medium text-gray-200 hover:bg-white/10";

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Diagram viewer"
      className="fixed inset-0 z-[70] bg-black/70 backdrop-blur-sm"
    >
      <div
        className="absolute left-1/2 top-3 z-10 flex -translate-x-1/2 items-center gap-1 rounded-lg border border-gray-700 bg-gray-900/95 px-1.5 py-1 shadow-xl"
      >
        <button
          className={btn}
          onClick={() => zoomFromButton(1 / 1.3)}
          aria-label="Zoom out"
        >
          &minus;
        </button>
        <button className={btn} onClick={fit} aria-label="Fit to screen">
          Fit
        </button>
        <button
          className={btn}
          onClick={() => zoomFromButton(1.3)}
          aria-label="Zoom in"
        >
          +
        </button>
        <span className="mx-1 h-4 w-px bg-gray-700" />
        <button className={btn} onClick={downloadSvg}>
          SVG
        </button>
        <button className={btn} onClick={downloadPng}>
          PNG
        </button>
        <span className="mx-1 h-4 w-px bg-gray-700" />
        <button className={btn} onClick={onClose} aria-label="Close">
          &#10005;
        </button>
      </div>

      <div
        ref={stageRef}
        className="h-full w-full touch-none overflow-hidden cursor-grab active:cursor-grabbing"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <div
          ref={contentRef}
          className="origin-top-left transition-opacity duration-150"
          style={{
            transform: `translate(${t.x}px, ${t.y}px) scale(${t.scale})`,
            opacity: ready ? 1 : 0,
          }}
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>

      <p className="pointer-events-none absolute bottom-3 left-1/2 -translate-x-1/2 text-xs text-gray-400">
        drag to pan &middot; scroll or pinch to zoom &middot; Esc to close
      </p>
    </div>,
    document.body,
  );
}
