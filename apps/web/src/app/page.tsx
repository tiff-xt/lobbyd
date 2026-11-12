"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useDeferredValue, useState } from "react";
import { Notice, Spinner } from "@/components/ui";
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
      <section className="pt-6">
        <p className="label">Tournaments and LAN events</p>
        <h1 className="mt-2 max-w-3xl font-display text-4xl font-bold leading-[1.05] sm:text-6xl">
          Grab a seat before the{" "}
          <span className="bg-gradient-to-r from-violet to-cyan bg-clip-text text-transparent">
            lobby fills
          </span>
        </h1>
        <p className="mt-4 max-w-2xl text-muted">
          Register your squad or pick your spot in the crowd. Seats are held for ten minutes while
          you check out, and nobody can take them from under you.
        </p>
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

      {tournaments.isPending && <Spinner label="Loading tournaments" />}
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
        {items.map((tournament) => (
          <li key={tournament.id}>
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
          ? "border-violet bg-violet/15 text-text"
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
      className="panel rise group flex h-full flex-col gap-4 p-4 transition-colors hover:border-violet/60"
    >
      <div className="flex items-start gap-3">
        <div className="grid w-12 shrink-0 place-items-center rounded-lg border border-line bg-bg py-1.5 text-center">
          <span className="font-mono text-[10px] tracking-widest text-cyan">{date.month}</span>
          <span className="font-display text-xl font-bold leading-none">{date.day}</span>
        </div>
        <div className="min-w-0">
          <p className="label truncate">{tournament.game}</p>
          <h2 className="mt-0.5 font-display text-lg font-semibold leading-snug group-hover:text-cyan">
            {tournament.title}
          </h2>
        </div>
      </div>
      <dl className="mt-auto grid grid-cols-2 gap-2 text-sm">
        <div>
          <dt className="label">Where</dt>
          <dd className="truncate">{tournament.venue ? tournament.venue.city : "Online"}</dd>
        </div>
        <div>
          <dt className="label">Format</dt>
          <dd>
            {tournament.team_size}v{tournament.team_size}
          </dd>
        </div>
      </dl>
      <p className="flex items-center gap-2 border-t border-line pt-3 text-sm text-muted">
        {live && (
          <span className="inline-flex items-center gap-1.5 text-red">
            <span className="pulse-soft size-1.5 rounded-full bg-red" /> Live now
          </span>
        )}
        {!live && dateTime(tournament.starts_at, tournament.venue?.timezone)}
      </p>
    </Link>
  );
}
