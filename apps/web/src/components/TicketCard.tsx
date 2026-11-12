"use client";

import QRCode from "qrcode";
import { useEffect, useState } from "react";
import type { Ticket } from "@/lib/api";

export function TicketCard({ ticket, title }: { ticket: Ticket; title: string }) {
  const [qr, setQr] = useState<string | null>(null);
  useEffect(() => {
    let current = true;
    // For now the code is just the ticket's id. Signed tokens that a scanner
    // can verify offline arrive with check-in (roadmap phase 7).
    QRCode.toString(ticket.id, { type: "svg", margin: 1, color: { dark: "#07080d", light: "#ffffff" } })
      .then((svg) => current && setQr(`data:image/svg+xml;utf8,${encodeURIComponent(svg)}`))
      .catch(() => current && setQr(null));
    return () => {
      current = false;
    };
  }, [ticket.id]);

  return (
    <article className="panel rise flex overflow-hidden">
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
        <p className="font-mono text-[10px] text-muted">{ticket.id}</p>
      </div>
      <div className="grid w-32 shrink-0 place-items-center border-l border-dashed border-line bg-white p-2">
        {qr ? (
          // eslint-disable-next-line @next/next/no-img-element -- a generated data URL, nothing to optimise
          <img src={qr} alt={`QR code for ticket ${ticket.id}`} className="size-full" />
        ) : (
          <span className="text-xs text-bg">QR</span>
        )}
      </div>
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
