"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useState } from "react";
import { Notice, Spinner } from "@/components/ui";
import { api, ApiError, type Bracket, type Match, unwrap } from "@/lib/api";
import { useLive } from "@/lib/live";
import { useCanManage } from "@/lib/useCanManage";

type LiveMessage =
  | { type: "bracket"; bracket: Bracket | null }
  | { type: "matches"; matches: Match[] }
  | { type: "reload" };

/** Fold changed matches into a bracket, never letting an older copy overwrite a newer one. */
function merge(bracket: Bracket, changed: Match[]): Bracket {
  const incoming = new Map(changed.map((match) => [match.id, match]));
  const rounds = bracket.rounds.map((round) => ({
    ...round,
    matches: round.matches.map((match) => {
      const next = incoming.get(match.id);
      return next && next.version > match.version ? next : match;
    }),
  }));
  const final = rounds[rounds.length - 1]?.matches[0];
  const champion =
    final?.winner_id == null
      ? null
      : final.team_a?.id === final.winner_id
        ? final.team_a
        : final.team_b;
  return { ...bracket, rounds, champion: champion ?? null };
}

export default function BracketPage() {
  const { slug } = useParams<{ slug: string }>();
  const queryClient = useQueryClient();
  const canManage = useCanManage(slug);
  const key = ["bracket", slug];

  const tournament = useQuery({
    queryKey: ["tournament", slug],
    queryFn: async () => unwrap(await api.GET("/tournaments/{slug}", { params: { path: { slug } } })),
  });
  const bracket = useQuery({
    queryKey: key,
    queryFn: async (): Promise<Bracket | null> => {
      try {
        return unwrap(await api.GET("/tournaments/{slug}/bracket", { params: { path: { slug } } }));
      } catch (error) {
        if (error instanceof ApiError && error.code === "bracket_not_found") return null;
        throw error;
      }
    },
  });

  const onMessage = useCallback(
    (raw: unknown) => {
      const message = raw as LiveMessage;
      if (message.type === "bracket") {
        queryClient.setQueryData<Bracket | null>(["bracket", slug], message.bracket);
      } else if (message.type === "matches") {
        queryClient.setQueryData<Bracket | null>(["bracket", slug], (current) =>
          current ? merge(current, message.matches) : current,
        );
      } else if (message.type === "reload") {
        queryClient.invalidateQueries({ queryKey: ["bracket", slug] });
      }
    },
    [queryClient, slug],
  );
  const live = useLive(`/tournaments/${slug}/live`, onMessage);

  const draw = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/tournaments/{slug}/bracket", { params: { path: { slug } } })),
    onSuccess: (created) => queryClient.setQueryData(key, created),
  });

  return (
    <div className="space-y-6">
      <header className="rise">
        <Link href={`/t/${slug}`} className="text-sm text-muted hover:text-text">
          ← {tournament.data?.title ?? "Tournament"}
        </Link>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-display text-3xl font-bold sm:text-4xl">Bracket</h1>
          <span
            className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 font-mono text-[11px] uppercase tracking-widest ${
              live ? "border-ok/40 text-ok" : "border-line text-muted"
            }`}
            role="status"
          >
            <span className={`size-2 rounded-full ${live ? "pulse-soft bg-ok" : "bg-muted"}`} />
            {live ? "Live" : "Reconnecting"}
          </span>
        </div>
      </header>

      {bracket.isPending ? (
        <Spinner label="Loading the bracket" />
      ) : bracket.isError ? (
        <Notice tone="error">{bracket.error.message}</Notice>
      ) : bracket.data === null ? (
        <div className="panel space-y-3 p-6">
          <p className="text-muted">
            The bracket has not been drawn yet. It appears here the moment it is, no reload needed.
          </p>
          {canManage && (
            <>
              {draw.error && <Notice tone="error">{draw.error.message}</Notice>}
              <button
                type="button"
                className="btn btn-primary"
                disabled={draw.isPending}
                onClick={() => draw.mutate()}
              >
                {draw.isPending ? "Drawing" : "Draw the bracket"}
              </button>
              <p className="text-xs text-muted">
                Uses the teams confirmed right now, seeded in the order they registered.
              </p>
            </>
          )}
        </div>
      ) : (
        <>
          {bracket.data.champion && (
            <div className="panel rise border-primary/50 p-5 text-center" data-spot>
              <p className="label">Champion</p>
              <p className="font-display text-3xl font-bold text-primary sm:text-4xl">
                {bracket.data.champion.name}
              </p>
            </div>
          )}
          <div className="overflow-x-auto pb-2">
            <div className="flex min-w-max items-stretch gap-6">
              {bracket.data.rounds.map((round) => (
                <section key={round.round} className="flex w-64 flex-col">
                  <h2 className="label mb-3">{round.name}</h2>
                  <div className="flex flex-1 flex-col justify-around gap-4">
                    {round.matches.map((match) => (
                      <MatchCard key={match.id} match={match} canManage={canManage} slug={slug} />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function MatchCard({ match, canManage, slug }: { match: Match; canManage: boolean; slug: string }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [scores, setScores] = useState<[string, string]>(["", ""]);
  const ready = match.team_a !== null && match.team_b !== null;
  const bye = match.status === "completed" && !ready;

  const apply = (updated: Match) => {
    queryClient.setQueryData<Bracket | null>(["bracket", slug], (current) =>
      current ? merge(current, [updated]) : current,
    );
    setEditing(false);
  };
  const path = { params: { path: { match_id: match.id } } };
  const start = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/matches/{match_id}/start", { ...path, body: { version: match.version } })),
    onSuccess: apply,
  });
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/matches/{match_id}/result", {
          ...path,
          body: { score_a: Number(scores[0]), score_b: Number(scores[1]), version: match.version },
        }),
      ),
    onSuccess: apply,
    // Someone else changed it first: show theirs, and let this organizer decide again.
    onError: () => queryClient.invalidateQueries({ queryKey: ["bracket", slug] }),
  });
  const error = save.error ?? start.error;
  const valid = scores.every((score) => /^\d{1,3}$/.test(score)) && scores[0] !== scores[1];

  return (
    <article
      className={`panel overflow-hidden text-sm transition-colors ${
        match.status === "live" ? "border-primary/60" : ""
      }`}
    >
      {match.status === "live" && (
        <p className="bg-primary/10 px-3 py-1 font-mono text-[10px] uppercase tracking-widest text-primary">
          Playing now
        </p>
      )}
      {[match.team_a, match.team_b].map((team, index) => {
        const score = index === 0 ? match.score_a : match.score_b;
        const won = team !== null && match.winner_id === team.id;
        return (
          <div
            key={index}
            className={`flex items-center justify-between gap-3 px-3 py-2 ${
              index === 0 ? "border-b border-line" : ""
            } ${won ? "bg-ok/10 font-semibold text-text" : match.winner_id ? "text-muted" : ""}`}
          >
            <span className="truncate">
              {team?.name ?? <span className="text-muted">{bye ? "Bye" : "To be decided"}</span>}
            </span>
            {editing ? (
              <input
                className="input !w-14 !px-2 !py-1 text-center font-mono"
                inputMode="numeric"
                aria-label={`Score for ${team?.name ?? "team"}`}
                value={scores[index]}
                onChange={(event) => {
                  save.reset();
                  setScores((current) => {
                    const next: [string, string] = [...current];
                    next[index] = event.target.value;
                    return next;
                  });
                }}
              />
            ) : (
              <span className="font-mono tabular-nums">{score ?? ""}</span>
            )}
          </div>
        );
      })}
      {canManage && ready && (
        <div className="space-y-2 border-t border-line bg-bg/40 px-3 py-2">
          {error && <p className="text-xs text-danger">{error.message}</p>}
          {editing ? (
            <div className="flex gap-2">
              <button
                type="button"
                className="btn btn-primary !px-3 !py-1 text-xs"
                disabled={!valid || save.isPending}
                onClick={() => save.mutate()}
              >
                Save result
              </button>
              <button
                type="button"
                className="btn btn-ghost !px-3 !py-1 text-xs"
                onClick={() => setEditing(false)}
              >
                Cancel
              </button>
            </div>
          ) : (
            <div className="flex gap-2">
              {match.status === "scheduled" && (
                <button
                  type="button"
                  className="btn btn-ghost !px-3 !py-1 text-xs"
                  disabled={start.isPending}
                  onClick={() => start.mutate()}
                >
                  Start
                </button>
              )}
              <button
                type="button"
                className="btn btn-ghost !px-3 !py-1 text-xs"
                onClick={() => {
                  save.reset();
                  start.reset();
                  setScores([String(match.score_a ?? ""), String(match.score_b ?? "")]);
                  setEditing(true);
                }}
              >
                {match.status === "completed" ? "Correct result" : "Enter result"}
              </button>
            </div>
          )}
        </div>
      )}
    </article>
  );
}
