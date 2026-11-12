"use client";

import Link from "next/link";
import { Notice, StatusPill } from "@/components/ui";
import type { Order } from "@/lib/api";
import { dateTime, inOrder, money, seatName } from "@/lib/format";
import { TicketCard } from "./TicketCard";

const OUTCOME: Record<string, { tone: "success" | "error" | "info" | "warn"; text: string }> = {
  paid: { tone: "success", text: "Payment received. Your tickets are below." },
  cancelled: {
    tone: "error",
    text: "The payment was declined. Nothing was charged and your picks are back on sale.",
  },
  expired: {
    tone: "warn",
    text: "The hold ran out before payment arrived. If the payment still goes through it is refunded automatically.",
  },
  refunded: {
    tone: "info",
    text: "The payment arrived after your hold had ended, so it was refunded in full.",
  },
};

export function OrderView({ order }: { order: Order }) {
  const outcome = OUTCOME[order.status];
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="label">Order</p>
          <h1 className="font-display text-2xl font-bold sm:text-3xl">
            <Link href={`/t/${order.tournament.slug}`} className="hover:text-cyan">
              {order.tournament.title}
            </Link>
          </h1>
          <p className="mt-1 text-sm text-muted">{dateTime(order.tournament.starts_at, order.tournament.timezone ?? undefined)}</p>
        </div>
        <StatusPill status={order.status} />
      </div>

      {outcome && <Notice tone={outcome.tone}>{outcome.text}</Notice>}

      <div className="panel p-4">
        <ul className="space-y-1.5 text-sm">
          {inOrder(order.items).map((item, index) => (
            <li key={index} className="flex justify-between gap-4">
              <span className="text-muted">
                {item.seat ? seatName(item.seat) : `${item.quantity} × ${item.ticket_type_name}`}
              </span>
              <span>{money(item.quantity * item.unit_price_cents, order.currency)}</span>
            </li>
          ))}
        </ul>
        <div className="mt-3 flex items-baseline justify-between border-t border-line pt-3">
          <span className="label">Total</span>
          <span className="font-display text-xl font-bold">
            {money(order.total_cents, order.currency)}
          </span>
        </div>
      </div>

      {order.tickets.length > 0 && (
        <section className="space-y-3">
          <h2 className="font-display text-xl font-semibold">Tickets</h2>
          <div className="grid gap-3 md:grid-cols-2">
            {inOrder(order.tickets).map((ticket) => (
              <TicketCard key={ticket.id} ticket={ticket} title={order.tournament.title} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
