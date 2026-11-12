"use client";

import { useEffect, useRef } from "react";
import { sequence } from "@/lib/art";

type State = "free" | "taken" | "gold";
type Seat = { x: number; y: number; size: number; state: State };

const FILL: Record<State, string> = { free: "#34d399", taken: "#3a362f", gold: "#ffd166" };
const WIDTH = 600;
const HEIGHT = 340;

function buildSeats(): Seat[] {
  const random = sequence(7);
  const seats: Seat[] = [];
  const ROWS = 9;
  for (let row = 0; row < ROWS; row++) {
    const radius = 120 + row * 34;
    const count = 15 + row * 4;
    for (let i = 0; i < count; i++) {
      // Seats sit on arcs that open toward the viewer, like a bowl.
      const angle = (-62 + (124 * i) / (count - 1)) * (Math.PI / 180);
      const roll = random();
      // The middle of the front rows goes first, as it does at any event.
      const centre = 1 - Math.abs(i / (count - 1) - 0.5) * 2;
      const demand = centre * 0.5 + (1 - row / ROWS) * 0.35;
      seats.push({
        x: 300 + Math.sin(angle) * radius * 1.12,
        y: 30 + Math.cos(angle) * radius * 0.62,
        size: 7 + row * 0.75,
        state: roll < demand * 0.18 ? "gold" : roll < demand * 0.9 ? "taken" : "free",
      });
    }
  }
  return seats;
}

// The layout never changes, so it is built once for the whole app.
const SEATS = buildSeats();

/**
 * The hero: an arena seen from the stage, and it is alive.
 *
 * - Stage lights sweep across the crowd.
 * - A follow-spot tracks the pointer; seats under it rise and brighten. With
 *   no pointer nearby it wanders by itself.
 * - Every few seconds a wave runs through the stands.
 * - Seats sell while you watch: a free one flashes gold, then goes dark.
 * - Click anywhere to claim a burst of seats.
 *
 * One animation loop writes styles straight to the seats, so React never
 * re-renders while it runs. It stops when the arena is off screen or the tab
 * is hidden, and does not start at all for people who prefer reduced motion.
 */
export function ArenaHero({ className = "" }: { className?: string }) {
  const seats = SEATS;
  const svg = useRef<SVGSVGElement>(null);
  const spot = useRef<SVGCircleElement>(null);
  const nodes = useRef<(SVGRectElement | null)[]>([]);

  useEffect(() => {
    const root = svg.current;
    if (!root || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const states = seats.map((seat) => seat.state);
    const paint = (i: number, state: State) => {
      states[i] = state;
      const node = nodes.current[i];
      if (node) node.style.fill = FILL[state];
    };

    const pointer = { x: 300, y: 130, active: false };
    const light = { x: 300, y: 130 };
    const tilt = { x: 0, y: 0 };
    const toArena = (event: PointerEvent) => {
      const box = root.getBoundingClientRect();
      return {
        x: ((event.clientX - box.left) / box.width) * WIDTH,
        y: ((event.clientY - box.top) / box.height) * HEIGHT,
      };
    };
    const onMove = (event: PointerEvent) => Object.assign(pointer, toArena(event), { active: true });
    const onLeave = () => {
      pointer.active = false;
    };
    const timers = new Set<ReturnType<typeof setTimeout>>();
    const later = (fn: () => void, ms: number) => {
      const timer = setTimeout(() => {
        timers.delete(timer);
        fn();
      }, ms);
      timers.add(timer);
    };

    // Click: seats ripple to gold outward from the pointer, then settle as sold.
    const onDown = (event: PointerEvent) => {
      const at = toArena(event);
      seats.forEach((seat, i) => {
        const distance = Math.hypot(seat.x - at.x, seat.y - at.y);
        if (distance > 95 || states[i] === "gold") return;
        later(() => {
          paint(i, "gold");
          later(() => paint(i, "taken"), 1400 + distance * 6);
        }, distance * 4);
      });
    };

    // Seats sell on their own, and now and then one comes back, so the
    // picture keeps changing without ever filling up.
    const market = setInterval(() => {
      const i = Math.floor(Math.random() * seats.length);
      if (states[i] === "free") {
        paint(i, "gold");
        later(() => paint(i, "taken"), 2200);
      } else if (states[i] === "taken" && Math.random() < 0.45) {
        paint(i, "free");
      }
    }, 420);

    let frame = 0;
    let visible = true;
    const draw = (now: number) => {
      frame = requestAnimationFrame(draw);
      if (!visible || document.hidden) return;
      const t = now / 1000;

      // The follow-spot eases toward the pointer, or wanders when left alone.
      const targetX = pointer.active ? pointer.x : 300 + Math.cos(t * 0.45) * 190;
      const targetY = pointer.active ? pointer.y : 135 + Math.sin(t * 0.7) * 60;
      light.x += (targetX - light.x) * 0.1;
      light.y += (targetY - light.y) * 0.1;
      spot.current?.setAttribute("cx", light.x.toFixed(1));
      spot.current?.setAttribute("cy", light.y.toFixed(1));

      // The whole arena leans a little toward the pointer.
      tilt.x += ((pointer.active ? (pointer.y / HEIGHT - 0.5) * -7 : 0) - tilt.x) * 0.06;
      tilt.y += ((pointer.active ? (pointer.x / WIDTH - 0.5) * 9 : 0) - tilt.y) * 0.06;
      root.style.transform = `perspective(900px) rotateX(${tilt.x.toFixed(2)}deg) rotateY(${tilt.y.toFixed(2)}deg)`;

      // A wave crosses the stands for the first third of every nine seconds.
      const waveX = -120 + ((t % 9) / 3) * (WIDTH + 240);
      for (let i = 0; i < seats.length; i++) {
        const node = nodes.current[i];
        if (!node) continue;
        const seat = seats[i]!;
        const near = Math.max(0, 1 - Math.hypot(seat.x - light.x, seat.y - light.y) / 100) ** 2;
        const wave = Math.exp(-(((seat.x - waveX) / 38) ** 2));
        const lift = near + wave * 0.7;
        node.style.transform =
          lift < 0.01 ? "" : `translateY(${(-lift * 6).toFixed(1)}px) scale(${(1 + near * 0.6 + wave * 0.2).toFixed(2)})`;
        if (states[i] === "free") node.style.fillOpacity = (0.42 + Math.min(1, lift) * 0.58).toFixed(2);
        else node.style.fillOpacity = "1";
      }
    };
    frame = requestAnimationFrame(draw);

    const observer = new IntersectionObserver(([entry]) => {
      visible = entry?.isIntersecting ?? true;
    });
    observer.observe(root);
    root.addEventListener("pointermove", onMove);
    root.addEventListener("pointerleave", onLeave);
    root.addEventListener("pointerdown", onDown);
    return () => {
      cancelAnimationFrame(frame);
      clearInterval(market);
      timers.forEach(clearTimeout);
      observer.disconnect();
      root.removeEventListener("pointermove", onMove);
      root.removeEventListener("pointerleave", onLeave);
      root.removeEventListener("pointerdown", onDown);
    };
  }, [seats]);

  return (
    <svg
      ref={svg}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      className={`${className} cursor-crosshair touch-none select-none will-change-transform`}
      aria-hidden
    >
      <defs>
        <radialGradient id="arena-wash" cx="0.5" cy="0" r="0.85">
          <stop offset="0" stopColor="#ffd166" stopOpacity="0.42" />
          <stop offset="0.45" stopColor="#ff9f1c" stopOpacity="0.1" />
          <stop offset="1" stopColor="#ff9f1c" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="arena-spot">
          <stop offset="0" stopColor="#fff3c4" stopOpacity="0.75" />
          <stop offset="0.4" stopColor="#ffd166" stopOpacity="0.28" />
          <stop offset="1" stopColor="#ffd166" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="arena-beam" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffe9a8" stopOpacity="0.55" />
          <stop offset="1" stopColor="#ffd166" stopOpacity="0" />
        </linearGradient>
        <filter id="arena-glow" x="-100%" y="-100%" width="300%" height="300%">
          <feGaussianBlur stdDeviation="3" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      <ellipse cx="300" cy="20" rx="330" ry="330" fill="url(#arena-wash)" />
      {/* Two stage lights, sweeping in opposite directions. */}
      <polygon className="beam beam-a" points="300,24 150,360 290,360" fill="url(#arena-beam)" />
      <polygon className="beam beam-b" points="300,24 320,360 470,360" fill="url(#arena-beam)" />

      {seats.map((seat, i) => (
        <rect
          key={i}
          ref={(node) => {
            nodes.current[i] = node;
          }}
          className="arena-seat"
          x={seat.x - seat.size / 2}
          y={seat.y - seat.size / 2}
          width={seat.size}
          height={seat.size}
          rx={seat.size * 0.28}
          style={{ fill: FILL[seat.state], fillOpacity: seat.state === "free" ? 0.42 : 1 }}
        />
      ))}

      {/* The follow-spot. "screen" blending makes it brighten what is under it. */}
      <circle ref={spot} cx="300" cy="130" r="105" fill="url(#arena-spot)" style={{ mixBlendMode: "screen" }} />
      {/* The stage, lit from above. */}
      <path
        d="M215 30 Q300 62 385 30 L385 18 Q300 44 215 18Z"
        fill="#ffd166"
        opacity="0.9"
        filter="url(#arena-glow)"
      />
    </svg>
  );
}
