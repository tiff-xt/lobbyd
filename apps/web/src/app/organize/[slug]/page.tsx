"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { AnimatedNumber } from "@/components/AnimatedNumber";
import { Notice, Spinner, StatusPill } from "@/components/ui";
import { api, type Tournament, unwrap } from "@/lib/api";
import { dateTime, money } from "@/lib/format";

type Kind = "competitor" | "reserved" | "general";
const KIND_LABEL: Record<Kind, string> = {
  competitor: "Team entry",
  reserved: "Reserved seats",
  general: "Standing",
};

/** The kind in words, unless the organizer's own name already says it. */
function kindNote(type: { kind: string; name: string }): string {
  const label = KIND_LABEL[type.kind as Kind] ?? type.kind;
  return label.toLowerCase() === type.name.trim().toLowerCase() ? "" : `${label} · `;
}

export default function ManageTournamentPage() {
  const { slug } = useParams<{ slug: string }>();
  const queryClient = useQueryClient();
  const path = { params: { path: { slug } } };

  const tournament = useQuery({
    queryKey: ["tournament", slug],
    queryFn: async () => unwrap(await api.GET("/tournaments/{slug}", path)),
  });
  const draft = tournament.data?.status === "draft";
  const sales = useQuery({
    queryKey: ["sales", slug],
    queryFn: async () => unwrap(await api.GET("/tournaments/{slug}/sales", path)),
    enabled: tournament.isSuccess && !draft,
    refetchInterval: 5000,
  });
  const publish = useMutation({
    mutationFn: async () => unwrap(await api.POST("/tournaments/{slug}/publish", path)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tournament", slug] });
      queryClient.invalidateQueries({ queryKey: ["my-tournaments"] });
    },
  });

  if (tournament.isPending) return <Spinner label="Loading" />;
  if (tournament.isError) return <Notice tone="error">{tournament.error.message}</Notice>;
  const t = tournament.data;

  return (
    <div className="space-y-8">
      <header className="rise">
        <Link href="/organize" className="text-sm text-muted hover:text-text">
          ← Your tournaments
        </Link>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="label">{t.game}</p>
            <h1 className="font-display text-3xl font-bold sm:text-4xl">{t.title}</h1>
            <p className="mt-1 text-sm text-muted">
              {dateTime(t.starts_at, t.venue?.timezone)} · {t.venue ? `${t.venue.name}, ${t.venue.city}` : "Online"} ·{" "}
              {t.team_size}v{t.team_size}
            </p>
          </div>
          <StatusPill status={t.status} />
        </div>
        {!draft && (
          <div className="mt-4 flex flex-wrap gap-2">
            <Link href={`/t/${slug}`} className="btn btn-ghost !py-1.5 text-sm">
              Public page
            </Link>
            <Link href={`/t/${slug}/door`} className="btn btn-ghost !py-1.5 text-sm">
              Run the door
            </Link>
            <Link href={`/t/${slug}/bracket`} className="btn btn-ghost !py-1.5 text-sm">
              Bracket
            </Link>
          </div>
        )}
      </header>

      {draft ? (
        <>
          <TicketTypes tournament={t} />
          <section className="panel space-y-3 p-5">
            <h2 className="font-display text-xl font-semibold">Publish</h2>
            <p className="text-sm text-muted">
              Publishing puts everything above on sale from {dateTime(t.sales_open_at, t.venue?.timezone)}. After
              that only the title and description can change: buyers have seen the rest.
            </p>
            {publish.error && <Notice tone="error">{publish.error.message}</Notice>}
            <button
              type="button"
              className="btn btn-primary"
              disabled={t.ticket_types.length === 0 || publish.isPending}
              onClick={() => publish.mutate()}
            >
              {publish.isPending ? "Publishing" : "Publish tournament"}
            </button>
            {t.ticket_types.length === 0 && (
              <p className="text-xs text-muted">Add at least one thing to sell first.</p>
            )}
          </section>
        </>
      ) : sales.isPending ? (
        <Spinner label="Loading sales" />
      ) : sales.isError ? (
        <Notice tone="error">{sales.error.message}</Notice>
      ) : (
        <>
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Net sales" hint={sales.data.refunded_cents > 0 ? `${money(sales.data.refunded_cents, sales.data.currency)} refunded` : "No refunds"}>
              <AnimatedNumber value={sales.data.net_cents} format={(cents) => money(cents, sales.data.currency)} />
            </Stat>
            <Stat label="Paid orders" hint={`${sales.data.orders.pending ?? 0} awaiting payment`}>
              <AnimatedNumber value={sales.data.orders.paid ?? 0} format={(n) => n.toLocaleString("en")} />
            </Stat>
            <Stat label="Teams confirmed" hint={`${sales.data.teams.pending} pending · ${sales.data.teams.waitlist} on the waitlist`}>
              <AnimatedNumber value={sales.data.teams.confirmed} format={(n) => n.toLocaleString("en")} />
            </Stat>
            <Stat label="Checked in" hint={`of ${sales.data.tickets.toLocaleString("en")} tickets`}>
              <AnimatedNumber value={sales.data.checked_in} format={(n) => n.toLocaleString("en")} />
            </Stat>
          </section>

          <section className="space-y-3">
            <h2 className="font-display text-xl font-semibold">What is selling</h2>
            <div className="panel divide-y divide-line">
              {sales.data.ticket_types.map((type) => {
                const sold = (type.sold / Math.max(type.capacity, 1)) * 100;
                const held = (type.held / Math.max(type.capacity, 1)) * 100;
                return (
                  <div key={type.id} className="space-y-2 p-4">
                    <div className="flex flex-wrap items-baseline justify-between gap-3">
                      <p>
                        <span className="font-medium">{type.name}</span>{" "}
                        <span className="text-sm text-muted">
                          {kindNote(type)}
                          {money(type.price_cents, sales.data.currency)}
                        </span>
                      </p>
                      <p className="font-mono text-sm tabular-nums">
                        {type.sold.toLocaleString("en")} / {type.capacity.toLocaleString("en")} sold
                        <span className="text-muted"> · {money(type.gross_cents, sales.data.currency)}</span>
                      </p>
                    </div>
                    <div
                      className="flex h-2 overflow-hidden rounded-full bg-line"
                      role="img"
                      aria-label={`${type.sold} sold, ${type.held} in baskets, ${type.available} left`}
                    >
                      <div className="bg-gradient-to-r from-primary to-secondary transition-[width] duration-700" style={{ width: `${sold}%` }} />
                      <div className="bg-warn/70 transition-[width] duration-700" style={{ width: `${held}%` }} />
                    </div>
                    <p className="text-xs text-muted">
                      {type.held > 0 && <span className="text-warn">{type.held} in baskets right now · </span>}
                      {type.available.toLocaleString("en")} left
                    </p>
                  </div>
                );
              })}
            </div>
            <p className="text-xs text-muted">Updates every five seconds.</p>
          </section>
        </>
      )}
    </div>
  );
}

function Stat({ label, hint, children }: { label: string; hint: string; children: React.ReactNode }) {
  return (
    <div className="panel deal p-4" data-spot>
      <p className="label">{label}</p>
      <p className="mt-1 font-display text-3xl font-bold tabular-nums">{children}</p>
      <p className="mt-1 text-xs text-muted">{hint}</p>
    </div>
  );
}

/** What a draft will sell: add and remove ticket types until it is published. */
function TicketTypes({ tournament: t }: { tournament: Tournament }) {
  const queryClient = useQueryClient();
  const slug = t.slug;
  const [kind, setKind] = useState<Kind>("competitor");
  const [name, setName] = useState("");
  const [price, setPrice] = useState("");
  const [capacity, setCapacity] = useState("");
  const [sections, setSections] = useState<string[]>([]);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["tournament", slug] });

  const venue = useQuery({
    queryKey: ["venue", t.venue?.id],
    queryFn: async () =>
      unwrap(await api.GET("/venues/{venue_id}", { params: { path: { venue_id: t.venue!.id } } })),
    enabled: !!t.venue,
  });
  const used = new Set(t.ticket_types.flatMap((type) => type.section_ids));
  const hasSlot = t.ticket_types.some((type) => type.kind === "competitor");

  const add = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/tournaments/{slug}/ticket-types", {
          params: { path: { slug } },
          body: {
            kind,
            name: name.trim() || KIND_LABEL[kind],
            price_cents: Math.round(Number(price || 0) * 100),
            ...(kind === "reserved" ? { section_ids: sections } : { capacity: Number(capacity) }),
          },
        }),
      ),
    onSuccess: () => {
      setName("");
      setPrice("");
      setCapacity("");
      setSections([]);
      refresh();
    },
  });
  const remove = useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await api.DELETE("/tournaments/{slug}/ticket-types/{ticket_type_id}", {
          params: { path: { slug, ticket_type_id: id } },
        }),
      ),
    onSuccess: refresh,
  });
  const complete = kind === "reserved" ? sections.length > 0 : Number(capacity) >= 1;

  return (
    <section className="space-y-3">
      <h2 className="font-display text-xl font-semibold">On sale</h2>
      {t.ticket_types.length > 0 && (
        <ul className="panel divide-y divide-line">
          {t.ticket_types.map((type) => (
            <li key={type.id} className="flex flex-wrap items-center gap-3 p-4">
              <div className="min-w-0 flex-1">
                <p className="font-medium">{type.name}</p>
                <p className="text-sm text-muted">
                  {kindNote(type)}
                  {type.capacity.toLocaleString("en")}{" "}
                  {type.kind === "competitor" ? "team slots" : type.kind === "reserved" ? "seats" : "tickets"}
                </p>
              </div>
              <p className="font-display text-lg font-semibold">{money(type.price_cents, t.currency)}</p>
              <button
                type="button"
                className="btn btn-ghost !py-1.5 text-sm"
                disabled={remove.isPending}
                onClick={() => remove.mutate(type.id)}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <form
        className="panel space-y-4 p-4"
        onSubmit={(event) => {
          event.preventDefault();
          add.mutate();
        }}
      >
        <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="What to add">
          {(["competitor", "general", "reserved"] as const).map((option) => {
            const blocked = (option === "reserved" && !t.venue) || (option === "competitor" && hasSlot);
            return (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={kind === option}
                disabled={blocked}
                title={
                  option === "reserved" && !t.venue
                    ? "Needs a venue"
                    : blocked
                      ? "A tournament has one team entry"
                      : undefined
                }
                className={`btn !py-1.5 text-sm ${kind === option ? "btn-primary" : "btn-ghost"}`}
                onClick={() => {
                  add.reset();
                  setKind(option);
                }}
              >
                {KIND_LABEL[option]}
              </button>
            );
          })}
        </div>
        <div className="grid gap-4 sm:grid-cols-3">
          <label className="block space-y-1.5">
            <span className="label">Name</span>
            <input className="input" maxLength={60} placeholder={KIND_LABEL[kind]} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="block space-y-1.5">
            <span className="label">Price ({t.currency})</span>
            <input className="input" type="number" min={0} step="0.01" placeholder="0 for free" value={price} onChange={(e) => setPrice(e.target.value)} />
          </label>
          {kind !== "reserved" && (
            <label className="block space-y-1.5">
              <span className="label">{kind === "competitor" ? "Team slots" : "Tickets"}</span>
              <input className="input" type="number" min={1} required value={capacity} onChange={(e) => setCapacity(e.target.value)} />
            </label>
          )}
        </div>
        {kind === "reserved" && (
          <fieldset className="space-y-2">
            <legend className="label">Sections sold at this price</legend>
            {venue.isPending ? (
              <Spinner />
            ) : (
              <div className="flex flex-wrap gap-2">
                {venue.data?.sections.map((section) => {
                  const taken = used.has(section.id);
                  const on = sections.includes(section.id);
                  return (
                    <button
                      key={section.id}
                      type="button"
                      aria-pressed={on}
                      disabled={taken}
                      title={taken ? "Already in another ticket type" : undefined}
                      className={`rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-40 ${
                        on ? "border-primary bg-primary/10 text-text" : "border-line text-muted hover:text-text"
                      }`}
                      onClick={() =>
                        setSections((current) =>
                          on ? current.filter((id) => id !== section.id) : [...current, section.id],
                        )
                      }
                    >
                      {section.name} <span className="text-xs text-muted">{section.seat_count}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </fieldset>
        )}
        {(add.error ?? remove.error) && <Notice tone="error">{(add.error ?? remove.error)?.message}</Notice>}
        <button type="submit" className="btn btn-ghost" disabled={!complete || add.isPending}>
          {add.isPending ? "Adding" : "Add to the sale"}
        </button>
      </form>
    </section>
  );
}
