export function money(cents: number, currency: string): string {
  if (cents === 0) return "Free";
  return new Intl.NumberFormat("en", {
    style: "currency",
    currency,
    minimumFractionDigits: cents % 100 === 0 ? 0 : 2,
  }).format(cents / 100);
}

export function dateTime(iso: string, timeZone?: string): string {
  return new Intl.DateTimeFormat("en", {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone,
    timeZoneName: timeZone ? "short" : undefined,
  }).format(new Date(iso));
}

export function day(iso: string): { month: string; day: string } {
  const date = new Date(iso);
  return {
    month: new Intl.DateTimeFormat("en", { month: "short" }).format(date).toUpperCase(),
    day: String(date.getDate()),
  };
}

/** Seconds as m:ss, never below zero. */
export function clock(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export function seatName(seat: { section: string; row: string; number: number }): string {
  return `${seat.section}, row ${seat.row}, seat ${seat.number}`;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

type Line = { seat?: { section: string; row: string; number: number } | null };

/** Entries first, then seats in the order you would walk to them. */
export function inOrder<T extends Line>(lines: T[]): T[] {
  const collator = new Intl.Collator("en", { numeric: true });
  return [...lines].sort((a, b) => {
    if (!a.seat || !b.seat) return Number(!!a.seat) - Number(!!b.seat);
    return (
      collator.compare(a.seat.section, b.seat.section) ||
      collator.compare(a.seat.row, b.seat.row) ||
      a.seat.number - b.seat.number
    );
  });
}
