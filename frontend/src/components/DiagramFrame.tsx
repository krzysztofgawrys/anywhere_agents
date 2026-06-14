import { useState } from "react";
import { DiagramLightbox } from "./DiagramLightbox";

/** A rendered diagram <svg> shown inline as a click-to-enlarge thumbnail that
 *  opens the zoom / pan / download lightbox. Shared by the Mermaid and raw-SVG
 *  renderers so both behave identically. The caller is responsible for passing
 *  a trusted SVG string (mermaid output, or model SVG already run through
 *  DOMPurify) - it is injected verbatim. */
export function DiagramFrame({ svg }: { svg: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <div className="group relative my-3 flex justify-center">
        <div
          role="button"
          tabIndex={0}
          title="Click to enlarge"
          aria-label="Enlarge diagram"
          onClick={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              setOpen(true);
            }
          }}
          className="cursor-zoom-in overflow-x-auto rounded [&_svg]:h-auto [&_svg]:max-w-full"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Enlarge diagram"
          className="absolute right-1.5 top-1.5 hidden rounded bg-black/60 px-1.5 py-0.5 text-xs text-gray-200 hover:bg-black/80 group-hover:block"
        >
          &#10530;
        </button>
      </div>
      {open && <DiagramLightbox svg={svg} onClose={() => setOpen(false)} />}
    </>
  );
}
