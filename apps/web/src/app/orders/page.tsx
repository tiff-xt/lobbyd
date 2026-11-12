"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Notice, Spinner, StatusPill } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { dateTime, money, plural } from "@/lib/format";
import { useSession } from "@/lib/useSession";

export default function OrdersPage() {
  const { signedIn } = useSession();
  const orders = useQuery({
    queryKey: ["orders"],
    queryFn: async () => unwrap(await api.GET("/users/me/orders")),
    enabled: signedIn,
  });

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <h1 className="font-display text-3xl font-bold">My tickets</h1>
      {!signedIn && (
        <Notice>
          <Link className="underline" href="/login?next=/orders">
            Sign in
          </Link>{" "}
          to see your orders.
        </Notice>
      )}
      {signedIn && orders.isPending && <Spinner label="Loading your orders" />}
      {orders.isError && <Notice tone="error">{orders.error.message}</Notice>}
      {orders.data?.length === 0 && (
        <Notice>
          Nothing here yet.{" "}
          <Link className="underline" href="/">
            Find a tournament
          </Link>
        </Notice>
      )}
      <ul className="space-y-3">
        {orders.data?.map((order) => (
          <li key={order.id}>
            <Link
              href={order.status === "pending" ? `/checkout/${order.hold_id}` : `/orders/${order.id}`}
              className="panel panel-link flex flex-wrap items-center justify-between gap-4 p-4"
            >
              <div className="min-w-0">
                <p className="truncate font-display text-lg font-semibold">
                  {order.tournament.title}
                </p>
                <p className="text-sm text-muted">
                  {dateTime(order.tournament.starts_at, order.tournament.timezone ?? undefined)} ·{" "}
                  {order.tickets.length > 0
                    ? plural(order.tickets.length, "ticket")
                    : "no tickets"}{" "}
                  · {money(order.total_cents, order.currency)}
                </p>
              </div>
              <StatusPill status={order.status} />
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
