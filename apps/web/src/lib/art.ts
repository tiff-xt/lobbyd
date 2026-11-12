/**
 * Deterministic "random" numbers from a string, so a game always gets the
 * same artwork without any image files or stored settings.
 */
export function hash(text: string): number {
  // FNV-1a, 32 bit.
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** A repeatable sequence of numbers in [0, 1) for a seed (mulberry32). */
export function sequence(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const PATTERNS = ["rays", "grid", "rings", "chevrons", "dots"] as const;
export type Pattern = (typeof PATTERNS)[number];

export type GameArt = { hue: number; pattern: Pattern; initials: string };

/** The look of a game: a colour, a pattern, and its initials. */
export function gameArt(game: string): GameArt {
  const h = hash(game.trim().toLowerCase());
  // Hues are spaced around the wheel, skipping the band taken by the brand's
  // gold (roughly 30 to 60 degrees) so artwork never looks like a button.
  const hue = (70 + (h % 290)) % 360;
  const pattern = PATTERNS[(h >>> 9) % PATTERNS.length]!;
  const initials = game
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => word[0]!.toUpperCase())
    .slice(0, 2)
    .join("");
  return { hue, pattern, initials };
}
