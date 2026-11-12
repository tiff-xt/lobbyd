import { describe, expect, it } from "vitest";
import { clock, inOrder, money, plural, seatName } from "@/lib/format";

describe("money", () => {
  it("shows whole amounts without cents, and cents when there are some", () => {
    expect(money(4000, "EUR")).toBe("€40");
    expect(money(1550, "USD")).toBe("$15.50");
  });
  it("says Free, not a zero price", () => {
    expect(money(0, "EUR")).toBe("Free");
  });
});

describe("clock", () => {
  it("formats a countdown", () => {
    expect(clock(600)).toBe("10:00");
    expect(clock(65.9)).toBe("1:05");
    expect(clock(9)).toBe("0:09");
  });
  it("never goes negative once time is up", () => {
    expect(clock(-3)).toBe("0:00");
  });
});

describe("labels", () => {
  it("names a seat", () => {
    expect(seatName({ section: "Floor 1", row: "3", number: 12 })).toBe("Floor 1, row 3, seat 12");
  });
  it("pluralises", () => {
    expect(plural(1, "ticket")).toBe("1 ticket");
    expect(plural(3, "ticket")).toBe("3 tickets");
  });
});

describe("inOrder", () => {
  it("puts entries first, then seats by section, row and number, counting numerically", () => {
    const seat = (section: string, row: string, number: number) => ({ seat: { section, row, number } });
    const lines = [seat("Floor 10", "1", 1), seat("Floor 2", "10", 3), { seat: null }, seat("Floor 2", "9", 12), seat("Floor 2", "9", 2)];
    expect(inOrder(lines)).toEqual([
      { seat: null },
      seat("Floor 2", "9", 2),
      seat("Floor 2", "9", 12),
      seat("Floor 2", "10", 3),
      seat("Floor 10", "1", 1),
    ]);
  });
});
