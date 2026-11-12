"use client";

import QRCode from "qrcode";
import { useEffect, useState } from "react";
import type { Ticket } from "@/lib/api";

export function TicketCard({ ticket, title }: { ticket: Ticket; title: string }) {
  const [qr, setQr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let current = true;
    // The code is a token signed by the server. A scanner can tell it is
    // genuine with only a public key, even with no network.
    QRCode.toString(ticket.token, { type: "svg", margin: 1, color: { dark: "#09090a", light: "#ffffff" } })
      .then((svg) => current && setQr(`data:image/svg+xml;utf8,${encodeURIComponent(svg)}`))
      .catch(() => current && setQr(null));
    return () => {
      current = false;
    };
  }, [ticket.token]);

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
