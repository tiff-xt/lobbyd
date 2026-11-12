import { useId } from "react";
import { gameArt, hash, type Pattern, sequence } from "@/lib/art";

/**
 * Generated cover art for a tournament. The game decides the colour and the
 * pattern, so every event of one game is recognisably related; the tournament
 * itself shifts the details, so no two covers are identical.
 */
export function Cover({
  game,
  seed,
  className = "",
}: {
  game: string;
  seed: string;
  className?: string;
}) {
  const id = useId();
  const { hue, pattern, initials } = gameArt(game);
  const random = sequence(hash(seed));
  const shift = random();
  const bright = `hsl(${hue} 85% 62%)`;

  return (
    <svg
      viewBox="0 0 400 160"
      preserveAspectRatio="xMidYMid slice"
      className={`cover-art ${className}`}
      role="img"
      aria-label={`${game} artwork`}
    >
      <defs>
        <linearGradient id={`${id}bg`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={`hsl(${hue} 70% 34%)`} />
          <stop offset="1" stopColor={`hsl(${(hue + 40) % 360} 75% 14%)`} />
        </linearGradient>
        <radialGradient id={`${id}glow`} cx={0.2 + shift * 0.6} cy="0" r="0.9">
          <stop offset="0" stopColor={bright} stopOpacity="0.55" />
          <stop offset="1" stopColor={bright} stopOpacity="0" />
        </radialGradient>
        <linearGradient id={`${id}fade`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0.35" stopColor="#131314" stopOpacity="0" />
          <stop offset="1" stopColor="#131314" stopOpacity="0.92" />
        </linearGradient>
      </defs>
      <rect width="400" height="160" fill={`url(#${id}bg)`} />
      <rect width="400" height="160" fill={`url(#${id}glow)`} />
      <g className="cover-pattern" stroke={bright} fill="none" strokeWidth="1.2" opacity="0.5">
        <Shapes pattern={pattern} shift={shift} fill={bright} />
      </g>
      <text
        x="388"
        y="128"
        textAnchor="end"
        fontFamily="'Chakra Petch', sans-serif"
        fontWeight="700"
        fontSize="150"
        fill="#fff"
        opacity="0.13"
      >
        {initials}
      </text>
      <rect width="400" height="160" fill={`url(#${id}fade)`} />
    </svg>
  );
}

function Shapes({ pattern, shift, fill }: { pattern: Pattern; shift: number; fill: string }) {
  switch (pattern) {
    case "rays": {
      const x = 60 + shift * 120;
      return (
        <>
          {Array.from({ length: 14 }, (_, i) => (
            <line key={i} x1={x} y1="190" x2={-120 + i * 52} y2="-10" />
          ))}
        </>
      );
    }
    case "grid": {
      // A floor running to a horizon: verticals fan out, horizontals bunch up.
      const horizon = 54 + shift * 20;
      return (
        <>
          {Array.from({ length: 15 }, (_, i) => (
            <line key={`v${i}`} x1="200" y1={horizon} x2={-360 + i * 80} y2="170" />
          ))}
          {Array.from({ length: 7 }, (_, i) => {
            const y = horizon + (i + 1) ** 2 * 2.4;
            return <line key={`h${i}`} x1="0" y1={y} x2="400" y2={y} />;
          })}
        </>
      );
    }
    case "rings": {
      const cx = 250 + shift * 120;
      return (
        <>
          {Array.from({ length: 9 }, (_, i) => (
            <circle key={i} cx={cx} cy="46" r={18 + i * 24} />
          ))}
        </>
      );
    }
    case "chevrons":
      return (
        <>
          {Array.from({ length: 12 }, (_, i) => {
            const x = -30 + i * 38 + shift * 30;
            return <polyline key={i} points={`${x},0 ${x + 50},80 ${x},160`} strokeWidth="5" opacity="0.5" />;
          })}
        </>
      );
    case "dots":
      return (
        <g stroke="none" fill={fill}>
          {Array.from({ length: 9 }, (_, row) =>
            Array.from({ length: 22 }, (_, col) => (
              <circle
                key={`${row}-${col}`}
                cx={10 + col * 18 + (row % 2) * 9}
                cy={8 + row * 17}
                // Dots swell toward a point that moves with the tournament.
                r={0.6 + 3.2 * Math.max(0, 1 - Math.abs(col / 22 - shift) * 1.6)}
              />
            )),
          )}
        </g>
      );
  }
}
