import { sequence } from "@/lib/art";

const COLOURS = ["#ffd166", "#ff9f1c", "#fff3c4", "#34d399", "#f4f1e8"];

/** A one-off burst of confetti, for the moment an order is paid. Pure CSS. */
export function Confetti({ pieces = 44 }: { pieces?: number }) {
  const random = sequence(2026);
  return (
    <div aria-hidden className="pointer-events-none absolute left-1/2 top-6 z-10 size-0">
      {Array.from({ length: pieces }, (_, i) => {
        const angle = random() * Math.PI * 2;
        const distance = 120 + random() * 260;
        return (
          <span
            key={i}
            className="confetti"
            style={
              {
                background: COLOURS[i % COLOURS.length],
                animationDelay: `${random() * 0.18}s`,
                "--dx": `${Math.cos(angle) * distance}px`,
                // Thrown outward, then pulled down.
                "--dy": `${Math.sin(angle) * distance * 0.6 + 180}px`,
                "--rot": `${(random() - 0.5) * 900}deg`,
              } as React.CSSProperties
            }
          />
        );
      })}
    </div>
  );
}
