"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { OrderView } from "@/components/OrderView";
import { Notice, Spinner } from "@/components/ui";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { clock, inOrder, money, seatName } from "@/lib/format";

/** Test payment methods. Each makes the simulated provider behave differently. */
const METHODS = [
  { id: "pm_success", name: "Pay normally", note: "Settles in about 1.5 seconds." },
  { id: "pm_declined", name: "Declined card", note: "Refused. Your picks go straight back on sale." },
  { id: "pm_duplicate", name: "Duplicate confirmation", note: "The provider confirms twice. You are charged and ticketed once." },
  { id: "pm_reordered", name: "Out-of-order messages", note: "\"Succeeded\" arrives before \"processing\"." },
  { id: "pm_slow", name: "Slow provider", note: "Takes 20 seconds to settle." },
  { id: "pm_never", name: "Silent provider", note: "Pays but never reports back. Found by asking, after about a minute." },
] as const;

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

export default function CheckoutPage() {
  const { holdId } = useParams<{ holdId: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [method, setMethod] = useState<string>("pm_success");
  // One key for "pay for this hold", kept across re-renders so a double
  // click or a retry is recognised by the server as the same request.
  const [payKey] = useState(idempotencyKey);

  const hold = useQuery({
    queryKey: ["hold", holdId],
    queryFn: async () =>
      unwrap(await api.GET("/holds/{hold_id}", { params: { path: { hold_id: holdId } } })),
  });

  // After a reload we no longer know the order's id: find it by its hold.
  const existing = useQuery({
    queryKey: ["order-for-hold", holdId],
    queryFn: async () => {
      const orders = unwrap(await api.GET("/users/me/orders"));
      return orders.find((order) => order.hold_id === holdId) ?? null;
    },
  });

  const pay = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/orders", {
          params: { header: { "Idempotency-Key": payKey } },
          body: { hold_id: holdId, payment_method: method },
        }),
      ),
    onSuccess: (order) => queryClient.setQueryData(["order-for-hold", holdId], order),
  });

  const orderId = existing.data?.id;
  const order = useQuery({
    queryKey: ["order", orderId],
    queryFn: async () =>
      unwrap(await api.GET("/orders/{order_id}", { params: { path: { order_id: orderId! } } })),
    enabled: !!orderId,
    initialData: existing.data ?? undefined,
    // Poll while the payment is in flight, then stop.
    refetchInterval: (query) => (query.state.data?.status === "pending" ? 700 : false),
  });

  const release = useMutation({
    mutationFn: async () =>
      unwrap(await api.DELETE("/holds/{hold_id}", { params: { path: { hold_id: holdId } } })),
    onSuccess: () => router.push(hold.data ? `/t/${hold.data.tournament.slug}` : "/"),
  });

  const waiting = hold.data?.status === "active" && !order.data;
  const now = useNow(waiting || order.data?.status === "pending");

  if (hold.isPending || existing.isPending) return <Spinner label="Loading your hold" />;
  if (hold.isError) {
    return (
      <Notice tone="error">
        {hold.error.message} <Link className="underline" href="/login">Sign in</Link> if you have
        not yet.
      </Notice>
    );
  }

  if (order.data) {
    const pendingFor = (now - new Date(order.data.created_at).getTime()) / 1000;
    return (
      <div className="mx-auto max-w-3xl space-y-6">
        {order.data.status === "pending" && (
          <div className="panel rise flex items-center gap-4 p-5">
            <span aria-hidden className="size-10 animate-spin rounded-full border-4 border-line border-t-secondary" />
            <div>
              <p className="font-display text-xl font-semibold">Processing payment</p>
              <p className="text-sm text-muted" role="status">
                {pendingFor < 8
                  ? "Waiting for the payment provider to confirm."
                  : "The provider is taking a while. Your seats stay held, and if no confirmation arrives we ask the provider directly after a minute."}
              </p>
            </div>
          </div>
        )}
        <OrderView order={order.data} />
        {order.data.status !== "pending" && (
          <div className="flex gap-3">
            <Link className="btn btn-ghost" href="/orders">
              All my tickets
            </Link>
            <Link className="btn btn-ghost" href={`/t/${order.data.tournament.slug}`}>
              Back to the tournament
            </Link>
          </div>
        )}
      </div>
    );
  }

  const h = hold.data;
  const secondsLeft = (new Date(h.expires_at).getTime() - now) / 1000;
  const lapsed = h.status !== "active" || secondsLeft <= 0;

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <header>
        <p className="label">Checkout</p>
        <h1 className="font-display text-3xl font-bold">{h.tournament.title}</h1>
      </header>

      {lapsed ? (
        <Notice tone="warn">
          This hold has ended and its seats are back on sale.{" "}
          <Link className="underline" href={`/t/${h.tournament.slug}`}>
            Choose again
          </Link>
        </Notice>
      ) : (
        <div
          className={`panel flex items-center justify-between gap-4 p-4 ${
            secondsLeft < 60 ? "border-danger/60" : ""
          }`}
        >
          <div>
            <p className="font-medium">Your picks are held</p>
            <p className="text-sm text-muted">
              {secondsLeft < 60
                ? "Less than a minute left. Pay now to keep them."
                : "Nobody else can take them until the timer ends."}
            </p>
          </div>
          <p
            className={`font-mono text-4xl font-semibold tabular-nums ${
              secondsLeft < 60 ? "pulse-soft text-danger" : "text-primary"
            }`}
            role="timer"
            aria-label="Time left on your hold"
          >
            {clock(secondsLeft)}
          </p>
        </div>
      )}

      <div className="panel p-4">
        <ul className="space-y-1.5 text-sm">
          {inOrder(h.items).map((item, index) => (
            <li key={index} className="flex justify-between gap-4">
              <span className="text-muted">
                {item.seat ? seatName(item.seat) : `${item.quantity} × ${item.ticket_type_name}`}
              </span>
              <span>{money(item.quantity * item.unit_price_cents, h.currency)}</span>
            </li>
          ))}
        </ul>
        <div className="mt-3 flex items-baseline justify-between border-t border-line pt-3">
          <span className="label">Total</span>
          <span className="font-display text-2xl font-bold">{money(h.total_cents, h.currency)}</span>
        </div>
      </div>

      {!lapsed && (
        <>
          {h.total_cents > 0 && (
            <fieldset className="space-y-2">
              <legend className="font-display text-xl font-semibold">Payment</legend>
              <p className="text-sm text-muted">
                No real money moves. Each option makes the simulated payment provider behave in a
                different way, so you can watch the system cope.
              </p>
              <div className="grid gap-2 sm:grid-cols-2">
                {METHODS.map((option) => (
                  <label
                    key={option.id}
                    className={`cursor-pointer rounded-lg border p-3 transition-colors ${
                      method === option.id ? "border-primary bg-primary/10" : "border-line hover:border-muted"
                    }`}
                  >
                    <input
                      type="radio"
                      name="method"
                      className="sr-only"
                      value={option.id}
                      checked={method === option.id}
                      onChange={() => setMethod(option.id)}
                    />
                    <span className="block text-sm font-medium">{option.name}</span>
                    <span className="mt-0.5 block text-xs text-muted">{option.note}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          )}

          {pay.isError && <Notice tone="error">{pay.error.message}</Notice>}
          {release.isError && <Notice tone="error">{release.error.message}</Notice>}

          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              className="btn btn-primary min-w-48"
              disabled={pay.isPending || release.isPending}
              onClick={() => pay.mutate()}
            >
              {pay.isPending
                ? "Placing order"
                : h.total_cents > 0
                  ? `Pay ${money(h.total_cents, h.currency)}`
                  : "Confirm free entry"}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={pay.isPending || release.isPending}
              onClick={() => release.mutate()}
            >
              Release and go back
            </button>
          </div>
        </>
      )}
    </div>
  );
}
