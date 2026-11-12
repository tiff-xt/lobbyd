"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useDeferredValue, useState } from "react";
import { ArenaHero } from "@/components/ArenaHero";
import { Cover } from "@/components/Cover";
import { Notice } from "@/components/ui";
import { api, type TournamentSummary, unwrap } from "@/lib/api";
import { dateTime, day } from "@/lib/format";

export default function Home() {
  const [search, setSearch] = useState("");
  const [game, setGame] = useState<string | null>(null);
  const q = useDeferredValue(search.trim());

  const games = useQuery({
    queryKey: ["games"],
    queryFn: async () => unwrap(await api.GET("/games")),
    staleTime: 60_000,
  });

  const tournaments = useInfiniteQuery({
    queryKey: ["tournaments", q, game],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/tournaments", {
          params: {
            query: { q: q || undefined, game: game ?? undefined, cursor: pageParam, limit: 12 },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = tournaments.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <div className="space-y-8">
      <section className="relative isolate grid items-center gap-6 pt-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,30rem)]">
        <div>
          <p className="label">Tournaments and LAN events</p>
          <h1 className="mt-2 font-display text-4xl font-bold leading-[1.05] sm:text-6xl">
            Grab a seat before the{" "}
            <span className="shimmer-text">
              lobby fills
            </span>
          </h1>
          <p className="mt-4 max-w-xl text-muted">
            Register your squad or pick your spot in the crowd. Seats are held for ten minutes
            while you check out, and nobody can take them from under you.
          </p>
        </div>
        {/* Beside the text on large screens; a faint backdrop behind it on small ones.
            The mask fades every edge out, so it never ends in a hard line. */}
        <ArenaHero className="pointer-events-none absolute inset-x-0 top-0 -z-10 mx-auto w-full max-w-md opacity-30 [mask-image:radial-gradient(ellipse_50%_62%_at_50%_42%,black_55%,transparent_100%)] lg:pointer-events-auto lg:static lg:z-0 lg:max-w-none lg:opacity-100" />
      </section>

      <section className="space-y-3">
        <input
          type="search"
          className="input"
          placeholder="Search by event, game, or keyword"
          aria-label="Search tournaments"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by game">
          <Chip active={game === null} onClick={() => setGame(null)}>
            All games
          </Chip>
          {games.data?.map((entry) => (
            <Chip
              key={entry.game}
              active={game === entry.game}
              onClick={() => setGame(game === entry.game ? null : entry.game)}
            >
              {entry.game} <span className="text-muted">{entry.tournaments}</span>
            </Chip>
          ))}
        </div>
      </section>

      {tournaments.isPending && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" role="status" aria-label="Loading tournaments">
          {Array.from({ length: 6 }, (_, i) => (
            <div key={i} className="skeleton h-64" style={{ animationDelay: `${i * 90}ms` }} />
          ))}
        </div>
      )}
      {tournaments.isError && (
        <Notice tone="error">
          Could not load tournaments. Is the API running? ({tournaments.error.message})
        </Notice>
      )}
      {tournaments.isSuccess && items.length === 0 && (
        <Notice>
          Nothing matches{q ? ` "${q}"` : ""}. Search looks for whole words, so try the full name
          of a game.
        </Notice>
      )}

      <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((tournament, index) => (
          // Each page of twelve arrives as a quick cascade.
          <li key={tournament.id} className="rise" style={{ animationDelay: `${(index % 12) * 45}ms` }}>
            <TournamentCard tournament={tournament} />
          </li>
        ))}
      </ul>

      {tournaments.hasNextPage && (
        <div className="flex justify-center">
          <button
            type="button"
            className="btn btn-ghost"
            disabled={tournaments.isFetchingNextPage}
            onClick={() => tournaments.fetchNextPage()}
          >
            {tournaments.isFetchingNextPage ? "Loading" : "Show more"}
          </button>
        </div>
      )}
    </div>
  );
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-sm transition-colors ${
        active
          ? "border-primary bg-primary/15 text-text"
          : "border-line text-muted hover:border-muted hover:text-text"
      }`}
    >
      {children}
    </button>
  );
}

function TournamentCard({ tournament }: { tournament: TournamentSummary }) {
  const date = day(tournament.starts_at);
  const live = new Date(tournament.starts_at) <= new Date();
  return (
    <Link
      href={`/t/${tournament.slug}`}
      data-spot
      className="panel panel-link group flex h-full flex-col overflow-hidden"
    >
      <div className="relative">
        <Cover game={tournament.game} seed={tournament.slug} className="block h-28 w-full" />
        <div className="absolute left-3 top-3 grid w-12 place-items-center rounded-lg border border-white/15 bg-bg/80 py-1.5 text-center backdrop-blur">
          <span className="font-mono text-[10px] tracking-widest text-primary">{date.month}</span>
          <span className="font-display text-xl font-bold leading-none">{date.day}</span>
        </div>
        {live && (
          <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full bg-bg/80 px-2.5 py-1 font-mono text-[11px] uppercase tracking-widest text-danger backdrop-blur">
            <span className="pulse-soft size-1.5 rounded-full bg-danger" /> Live
          </span>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-3 p-4 pt-3">
        <div>
          <p className="label truncate">{tournament.game}</p>
          <h2 className="mt-0.5 font-display text-lg font-semibold leading-snug group-hover:text-primary">
            {tournament.title}
          </h2>
        </div>
        <p className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-3 text-sm text-muted">
          <span className="text-text">{tournament.venue ? tournament.venue.city : "Online"}</span>
          <span>
            {tournament.team_size}v{tournament.team_size}
          </span>
          <span className="ml-auto">{dateTime(tournament.starts_at, tournament.venue?.timezone)}</span>
        </p>
      </div>
    </Link>
  );
}
