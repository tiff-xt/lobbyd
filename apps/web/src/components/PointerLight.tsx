"use client";

import { useEffect, useRef } from "react";

/**
 * Feeds the pointer's position to CSS.
 *
 * Two effects use it: a soft light that follows the pointer over the whole
 * page, and a highlight inside whichever [data-spot] element is under it.
 * One listener for the whole app, throttled to one update per frame, and
 * nothing is re-rendered: only CSS variables change.
 */
export function PointerLight() {
  const light = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce), (hover: none)").matches) return;
    let frame = 0;
    let last: PointerEvent | null = null;

    const apply = () => {
      frame = 0;
      if (!last) return;
      const el = light.current;
      if (el) {
        el.style.setProperty("--mx", `${last.clientX}px`);
        el.style.setProperty("--my", `${last.clientY}px`);
        el.dataset.on = "true";
      }
      const spot = (last.target as Element | null)?.closest?.<HTMLElement>("[data-spot]");
      if (spot) {
        const box = spot.getBoundingClientRect();
        spot.style.setProperty("--px", `${last.clientX - box.left}px`);
        spot.style.setProperty("--py", `${last.clientY - box.top}px`);
      }
    };
    const onMove = (event: PointerEvent) => {
      last = event;
      frame ||= requestAnimationFrame(apply);
    };
    const onLeave = () => {
      if (light.current) light.current.dataset.on = "false";
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    document.documentElement.addEventListener("pointerleave", onLeave);
    return () => {
      window.removeEventListener("pointermove", onMove);
      document.documentElement.removeEventListener("pointerleave", onLeave);
      cancelAnimationFrame(frame);
    };
  }, []);

  return <div ref={light} className="cursor-light" aria-hidden />;
}
