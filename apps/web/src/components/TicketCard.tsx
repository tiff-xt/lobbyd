"use client";

import { useEffect, useRef, useState } from "react";
import type { Ticket } from "@/lib/api";
import { seatName } from "@/lib/format";
import { BrandedQR } from "./BrandedQR";
import { Logo } from "./Logo";

export function TicketCard({ ticket, title }: { ticket: Ticket; title: string }) {
  const [copied, setCopied] = useState(false);
  const [showing, setShowing] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (showing) dialog.current?.showModal();
    else dialog.current?.close();
  }, [showing]);
  const label = `QR code for ticket ${ticket.id}`;

  return (
    <article
      className={`panel flex h-full overflow-hidden ${ticket.status === "void" ? "opacity-50" : ""}`}
      data-spot
    >
      <div className="min-w-0 flex-1 space-y-3 p-4">
        <div>
          <p className="label">{ticket.ticket_type_name}</p>
          <h3 className="font-display text-lg font-semibold leading-snug">{title}</h3>
        </div>
        {ticket.seat ? (
          <dl className="grid grid-cols-3 gap-2 text-sm">
            <Cell label="Section">{ticket.seat.section}</Cell>
            <Cell label="Row">{ticket.seat.row}</Cell>
            <Cell label="Seat">{ticket.seat.number}</Cell>
          </dl>
        ) : (
          <p className="text-sm text-muted">No assigned seat</p>
        )}
        {ticket.status === "void" ? (
          <p className="font-mono text-[11px] uppercase tracking-widest text-danger">Cancelled</p>
        ) : (
          <button
            type="button"
            className="font-mono text-[10px] text-muted underline-offset-2 hover:text-text hover:underline"
            title="Copy what the QR code contains, to paste into the door scanner"
            onClick={() => {
              navigator.clipboard?.writeText(ticket.token).then(() => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              });
            }}
          >
            {copied ? "Copied" : "Copy ticket code"}
          </button>
        )}
      </div>
      <button
        type="button"
        className="group/qr grid w-36 shrink-0 place-items-center border-l border-dashed border-line bg-[#fffaf0] p-1.5 transition-transform hover:scale-[1.03] disabled:pointer-events-none"
        disabled={ticket.status === "void"}
        title="Show full size for the door"
        aria-label="Show the QR code full size"
        onClick={() => setShowing(true)}
      >
        <BrandedQR value={ticket.token} label={label} />
      </button>

      {/* Full size, bright, and nothing else: what you hold up at the door. */}
      <dialog
        ref={dialog}
        className="m-auto w-[min(92vw,26rem)] rounded-3xl bg-[#fffaf0] p-0 text-[#09090a] shadow-2xl backdrop:bg-black/80 backdrop:backdrop-blur-sm"
        onClose={() => setShowing(false)}
        onClick={(event) => event.target === dialog.current && setShowing(false)}
      >
        {showing && (
          <div className="space-y-3 p-5">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-2 font-display text-lg font-bold tracking-wide">
                <Logo size={26} /> lobbyd
              </span>
              <button
                type="button"
                className="rounded-full px-3 py-1 text-sm text-[#09090a]/60 hover:bg-black/5"
                onClick={() => setShowing(false)}
              >
                Close
              </button>
            </div>
            <BrandedQR value={ticket.token} label={label} />
            <div className="border-t border-dashed border-black/20 pt-3 text-center">
              <p className="font-display text-lg font-semibold leading-snug">{title}</p>
              <p className="text-sm text-[#09090a]/70">
                {ticket.ticket_type_name}
                {ticket.seat ? ` · ${seatName(ticket.seat)}` : ""}
              </p>
            </div>
          </div>
        )}
      </dialog>
    </article>
  );
}

function Cell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="label">{label}</dt>
      <dd className="truncate font-medium">{children}</dd>
    </div>
  );
}
