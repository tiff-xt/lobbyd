"use client";

import { useMemo, useState } from "react";
import type { SeatMap, TicketType } from "@/lib/api";
import { money } from "@/lib/format";

export const MAX_SEATS = 8;

type Props = {
  seatMap: SeatMap;
  /** Reserved-seating ticket types: which sections each one sells, and its price. */
  ticketTypes: TicketType[];
  taken: Set<string>;
  selected: Set<string>;
  currency: string;
  onToggle: (seatId: string) => void;
};

/**
 * Two levels, so a 12,000 seat arena stays usable: pick a section from the
 * overview, then pick seats inside it. Only one section's seats are ever in
 * the page at a time.
 */
export function SeatPicker({ seatMap, ticketTypes, taken, selected, currency, onToggle }: Props) {
  const typeBySection = useMemo(() => {
    const map = new Map<string, TicketType>();
    for (const type of ticketTypes) for (const id of type.section_ids) map.set(id, type);
    return map;
  }, [ticketTypes]);

  const sections = useMemo(
    () =>
      seatMap.sections
        .filter((section) => typeBySection.has(section.id))
        .map((section) => {
          const seats = section.rows.flatMap((row) => row.seats);
          return {
            ...section,
            total: seats.length,
            free: seats.filter((seat) => !taken.has(seat.id)).length,
            picked: seats.filter((seat) => selected.has(seat.id)).length,
            type: typeBySection.get(section.id)!,
          };
        }),
    [seatMap, typeBySection, taken, selected],
  );

  const [openId, setOpenId] = useState<string | null>(null);
  const open = sections.find((section) => section.id === openId) ?? sections[0];
  if (!open) return null;
  const full = selected.size >= MAX_SEATS;

  return (
    <div className="space-y-4">
      <div
        className="grid grid-cols-2 gap-1.5 sm:grid-cols-4 xl:grid-cols-6"
        role="tablist"
        aria-label="Sections"
      >
        {sections.map((section) => {
          const ratio = section.total ? section.free / section.total : 0;
          const active = section.id === open.id;
          return (
            <button
              key={section.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => setOpenId(section.id)}
              className={`relative overflow-hidden rounded-lg border px-2.5 py-1.5 text-left transition-colors ${
                active ? "border-primary bg-primary/10" : "border-line hover:border-muted"
              }`}
            >
              <span className="flex items-baseline justify-between gap-2">
                <span className="truncate text-sm font-medium">{section.name}</span>
                {section.picked > 0 && (
                  <span className="rounded bg-primary px-1.5 text-xs font-bold text-bg">
                    {section.picked}
                  </span>
                )}
              </span>
              <span className="mt-0.5 block text-xs text-muted">
                {section.free === 0 ? "Sold out" : `${section.free} left`} ·{" "}
                {money(section.type.price_cents, currency)}
              </span>
              {/* How full the section is, at a glance. */}
              <span
                aria-hidden
                className={`absolute inset-x-0 bottom-0 h-0.5 origin-left ${
                  ratio > 0.5 ? "bg-ok" : ratio > 0.15 ? "bg-warn" : "bg-danger"
                }`}
                style={{ transform: `scaleX(${ratio})` }}
              />
            </button>
          );
        })}
      </div>

      <div className="panel overflow-x-auto p-4" data-spot role="tabpanel" aria-label={open.name}>
        <div className="mx-auto mb-4 w-2/3 rounded-b-3xl border border-t-0 border-secondary/30 bg-secondary/5 py-1 text-center">
          <span className="label !text-secondary">Stage</span>
        </div>
        <div className="mx-auto flex w-max flex-col gap-1">
          {open.rows.map((row, rowIndex) => (
            // Keyed by section too, so the rows cascade in again on each switch.
            <div
              key={`${open.id}-${row.label}`}
              className="seat-in flex items-center gap-2"
              style={{ animationDelay: `${Math.min(rowIndex, 24) * 18}ms` }}
            >
              <span className="w-6 text-right font-mono text-[10px] text-muted">{row.label}</span>
              <div className="flex gap-1">
                {row.seats.map((seat) => {
                  const isTaken = taken.has(seat.id);
                  const isSelected = selected.has(seat.id);
                  return (
                    <button
                      key={seat.id}
                      type="button"
                      disabled={isTaken || (full && !isSelected)}
                      aria-pressed={isSelected}
                      aria-label={`Row ${row.label}, seat ${seat.number}${isTaken ? ", taken" : ""}`}
                      title={`Row ${row.label}, seat ${seat.number}`}
                      onClick={() => onToggle(seat.id)}
                      className={`size-5 rounded-[5px] border text-[0px] transition-[transform,background-color] hover:enabled:scale-125 ${
                        isSelected
                          ? "seat-pop border-primary bg-primary shadow-[0_0_12px] shadow-primary/70"
                          : isTaken
                            ? "cursor-not-allowed border-transparent bg-line/60"
                            : "border-ok/50 bg-ok/15 hover:bg-ok/50 disabled:cursor-not-allowed disabled:opacity-40"
                      }`}
                    >
                      {seat.number}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
        <div className="mt-4 flex flex-wrap justify-center gap-4 text-xs text-muted">
          <Legend className="border-ok/50 bg-ok/15" label="Available" />
          <Legend className="border-primary bg-primary" label="Your pick" />
          <Legend className="border-transparent bg-line/60" label="Taken" />
        </div>
      </div>
    </div>
  );
}

function Legend({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={`size-3 rounded-[3px] border ${className}`} />
      {label}
    </span>
  );
}
