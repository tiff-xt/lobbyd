"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { MAX_SEATS, SeatPicker } from "@/components/SeatPicker";
import { Notice, Spinner } from "@/components/ui";
import { api, ApiError, idempotencyKey, unwrap } from "@/lib/api";
import { dateTime, money, plural } from "@/lib/format";
import { useSession } from "@/lib/useSession";

const MAX_QUANTITY = 8;

export default function TournamentPage() {
  const { slug } = useParams<{ slug: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { signedIn } = useSession();

  const tournament = useQuery({
    queryKey: ["tournament", slug],
    queryFn: async () => unwrap(await api.GET("/tournaments/{slug}", { params: { path: { slug } } })),
  });
  const venueId = tournament.data?.venue?.id;
  const seatMap = useQuery({
    queryKey: ["seats", venueId],
    queryFn: async () =>
      unwrap(await api.GET("/venues/{venue_id}/seats", { params: { path: { venue_id: venueId! } } })),
    enabled: !!venueId,
    staleTime: Infinity, // a venue's layout never changes
  });
  const availability = useQuery({
    queryKey: ["availability", slug],
    queryFn: async () =>
      unwrap(await api.GET("/tournaments/{slug}/availability", { params: { path: { slug } } })),
    // Other buyers are active: keep the picture fresh.
    refetchInterval: 4000,
  });

  // Read the clock once, when the page opens: render must not depend on it.
  const [now] = useState(() => Date.now());
  const [seats, setSeats] = useState<Set<string>>(new Set());
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const taken = useMemo(
    () => new Set(availability.data?.taken_seat_ids ?? []),
    [availability.data],
  );
  // A seat someone else took while it was selected simply drops out.
  const picked = useMemo(() => new Set([...seats].filter((id) => !taken.has(id))), [seats, taken]);
  const lost = seats.size - picked.size;

  const hold = useMutation({
    // One key per attempt at this exact basket: an automatic retry reuses it.
    mutationFn: async (key: string) =>
      unwrap(
        await api.POST("/holds", {
          params: { header: { "Idempotency-Key": key } },
          body: {
            tournament: slug,
            seat_ids: [...picked],
            items: Object.entries(quantities)
              .filter(([, quantity]) => quantity > 0)
              .map(([ticket_type_id, quantity]) => ({ ticket_type_id, quantity })),
          },
        }),
      ),
    onSuccess: (created) => router.push(`/checkout/${created.id}`),
    onError: () => queryClient.invalidateQueries({ queryKey: ["availability", slug] }),
  });

  if (tournament.isPending) return <Spinner label="Loading tournament" />;
  if (tournament.isError) {
    return <Notice tone="error">{tournament.error.message}</Notice>;
  }

  const t = tournament.data;
  const left = new Map(availability.data?.ticket_types.map((a) => [a.ticket_type_id, a.available]));
  const reserved = t.ticket_types.filter((type) => type.kind === "reserved");
  const counted = t.ticket_types.filter((type) => type.kind !== "reserved");
  const priceOfSeat = new Map<string, number>();
  if (seatMap.data) {
    for (const type of reserved) {
      for (const section of seatMap.data.sections) {
        if (!type.section_ids.includes(section.id)) continue;
        for (const row of section.rows) for (const seat of row.seats) priceOfSeat.set(seat.id, type.price_cents);
      }
    }
  }
  const seatTotal = [...picked].reduce((sum, id) => sum + (priceOfSeat.get(id) ?? 0), 0);
  const countedTotal = counted.reduce((sum, type) => sum + (quantities[type.id] ?? 0) * type.price_cents, 0);
  const itemCount = picked.size + Object.values(quantities).reduce((a, b) => a + b, 0);
  const notOpen = new Date(t.sales_open_at).getTime() > now;
  const closed = !["published", "live"].includes(t.status) || new Date(t.ends_at).getTime() <= now;

  const toggleSeat = (id: string) => {
    hold.reset();
    setSeats((current) => {
      const next = new Set([...current].filter((seat) => !taken.has(seat)));
      if (next.has(id)) next.delete(id);
      else if (next.size < MAX_SEATS) next.add(id);
      return next;
    });
  };
  const setQuantity = (id: string, quantity: number) => {
    hold.reset();
    setQuantities((current) => ({ ...current, [id]: quantity }));
  };

  const error = hold.error instanceof ApiError ? hold.error : null;

  return (
    <div className="space-y-8">
      <header className="rise">
        <Link href="/" className="text-sm text-muted hover:text-text">
          ← All tournaments
        </Link>
        <p className="label mt-4">{t.game}</p>
        <h1 className="mt-1 font-display text-3xl font-bold sm:text-5xl">{t.title}</h1>
        <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-2 text-sm">
          <Fact label="When">{dateTime(t.starts_at, t.venue?.timezone)}</Fact>
          <Fact label="Where">{t.venue ? `${t.venue.name}, ${t.venue.city}` : "Online"}</Fact>
          <Fact label="Format">
            {t.team_size}v{t.team_size}
          </Fact>
        </dl>
        {t.description && <p className="mt-4 max-w-2xl text-muted">{t.description}</p>}
      </header>

      {notOpen && (
        <Notice tone="warn">Sales open {dateTime(t.sales_open_at, t.venue?.timezone)}.</Notice>
      )}
      {closed && <Notice>This tournament is no longer on sale.</Notice>}

      <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
        <div className="space-y-8">
          {counted.length > 0 && (
            <section className="space-y-3">
              <h2 className="font-display text-xl font-semibold">Entry</h2>
              {counted.map((type) => {
                const available = left.get(type.id) ?? 0;
                const quantity = quantities[type.id] ?? 0;
                const max = type.kind === "competitor" ? 1 : Math.min(MAX_QUANTITY, available);
                return (
                  <div key={type.id} className="panel flex flex-wrap items-center gap-4 p-4">
                    <div className="min-w-0 flex-1">
                      <p className="font-medium">{type.name}</p>
                      <p className="text-sm text-muted">
                        {type.kind === "competitor"
                          ? `One slot for a team of ${t.team_size}`
                          : "Standing, no assigned seat"}{" "}
                        ·{" "}
                        <span className={available === 0 ? "text-danger" : available <= 5 ? "text-warn" : ""}>
                          {available === 0 ? "Sold out" : `${available} of ${type.capacity} left`}
                        </span>
                      </p>
                    </div>
                    <p className="font-display text-lg font-semibold">
                      {money(type.price_cents, t.currency)}
                    </p>
                    <Stepper
                      label={type.name}
                      value={quantity}
                      max={max}
                      onChange={(value) => setQuantity(type.id, value)}
                    />
                  </div>
                );
              })}
            </section>
          )}

          {reserved.length > 0 && (
            <section className="space-y-3">
              <div className="flex items-baseline justify-between gap-4">
                <h2 className="font-display text-xl font-semibold">Seats</h2>
                <p className="text-sm text-muted">Up to {MAX_SEATS} per order</p>
              </div>
              {seatMap.isPending || availability.isPending ? (
                <Spinner label="Loading the seat map" />
              ) : seatMap.data ? (
                <SeatPicker
                  seatMap={seatMap.data}
                  ticketTypes={reserved}
                  taken={taken}
                  selected={picked}
                  currency={t.currency}
                  onToggle={toggleSeat}
                />
              ) : (
                <Notice tone="error">Could not load the seat map.</Notice>
              )}
            </section>
          )}
        </div>

        <aside className="lg:sticky lg:top-20 lg:self-start">
          <div className="panel space-y-4 p-5">
            <h2 className="font-display text-xl font-semibold">Your order</h2>
            {itemCount === 0 ? (
              <p className="text-sm text-muted">Pick an entry or some seats to get started.</p>
            ) : (
              <ul className="space-y-1.5 text-sm">
                {counted
                  .filter((type) => (quantities[type.id] ?? 0) > 0)
                  .map((type) => (
                    <Line key={type.id} label={`${quantities[type.id]} × ${type.name}`}>
                      {money((quantities[type.id] ?? 0) * type.price_cents, t.currency)}
                    </Line>
                  ))}
                {picked.size > 0 && (
                  <Line label={plural(picked.size, "seat")}>{money(seatTotal, t.currency)}</Line>
                )}
              </ul>
            )}
            <div className="flex items-baseline justify-between border-t border-line pt-3">
              <span className="label">Total</span>
              <span className="font-display text-2xl font-bold">
                {itemCount === 0 ? "–" : money(seatTotal + countedTotal, t.currency)}
              </span>
            </div>

            {lost > 0 && (
              <Notice tone="warn">
                {plural(lost, "seat")} you picked {lost === 1 ? "was" : "were"} just taken by someone
                else.
              </Notice>
            )}
            {error && (
              <Notice tone="error">
                {error.message}
                {error.code === "hold_exists" && typeof error.extra.hold_id === "string" && (
                  <Link className="mt-1 block underline" href={`/checkout/${error.extra.hold_id}`}>
                    Go to your open checkout
                  </Link>
                )}
              </Notice>
            )}

            {signedIn ? (
              <button
                type="button"
                className="btn btn-primary w-full"
                disabled={itemCount === 0 || hold.isPending || notOpen || closed}
                onClick={() => hold.mutate(idempotencyKey())}
              >
                {hold.isPending ? "Holding" : "Hold for 10 minutes"}
              </button>
            ) : (
              <Link className="btn btn-primary w-full" href={`/login?next=/t/${slug}`}>
                Sign in to book
              </Link>
            )}
            <p className="text-xs text-muted">
              Holding takes your picks off sale while you pay. If any of them is gone, nothing is
              held and you can choose again.
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="label">{label}</dt>
      <dd className="mt-0.5">{children}</dd>
    </div>
  );
}

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <li className="flex justify-between gap-4">
      <span className="text-muted">{label}</span>
      <span>{children}</span>
    </li>
  );
}

function Stepper({
  label,
  value,
  max,
  onChange,
}: {
  label: string;
  value: number;
  max: number;
  onChange: (value: number) => void;
}) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label={`Quantity of ${label}`}>
      <button
        type="button"
        className="btn btn-ghost size-9 !p-0"
        disabled={value <= 0}
        aria-label="One fewer"
        onClick={() => onChange(value - 1)}
      >
        −
      </button>
      <span className="w-8 text-center font-mono" aria-live="polite">
        {value}
      </span>
      <button
        type="button"
        className="btn btn-ghost size-9 !p-0"
        disabled={value >= max}
        aria-label="One more"
        onClick={() => onChange(value + 1)}
      >
        +
      </button>
    </div>
  );
}
