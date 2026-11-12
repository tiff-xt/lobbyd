import { describe, expect, it } from "vitest";
import { gameArt, hash, PATTERNS, sequence } from "@/lib/art";

describe("gameArt", () => {
  it("gives a game the same look every time, whatever its capitalisation", () => {
    expect(gameArt("Starfall Tactics")).toEqual(gameArt("Starfall Tactics"));
    expect(gameArt("starfall tactics").hue).toBe(gameArt("Starfall Tactics").hue);
  });
  it("uses up to two initials", () => {
    expect(gameArt("Starfall Tactics").initials).toBe("ST");
    expect(gameArt("Hexbound").initials).toBe("H");
    expect(gameArt("One Two Three").initials).toBe("OT");
  });
  it("keeps artwork out of the brand's gold, so a cover never looks like a button", () => {
    for (const game of ["a", "b", "Neon Circuit", "Voidstrike", "Pixel Brawl", "Sky Raiders", "zz"]) {
      const { hue, pattern } = gameArt(game);
      expect(hue < 30 || hue >= 70).toBe(true);
      expect(PATTERNS).toContain(pattern);
    }
  });
});

describe("sequence", () => {
  it("is repeatable for a seed and differs between seeds", () => {
    const a = sequence(hash("slug-1"));
    const b = sequence(hash("slug-1"));
    const c = sequence(hash("slug-2"));
    const first = [a(), a(), a()];
    expect([b(), b(), b()]).toEqual(first);
    expect([c(), c(), c()]).not.toEqual(first);
    expect(first.every((n) => n >= 0 && n < 1)).toBe(true);
  });
});
