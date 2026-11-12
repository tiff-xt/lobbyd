"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Notice, StatusPill } from "@/components/ui";
import { api, ApiError, type TicketType, type Tournament, unwrap } from "@/lib/api";
import { money } from "@/lib/format";

export type TeamDraft = { on: boolean; name: string; players: string[] };

export function emptyDraft(size: number): TeamDraft {
  return { on: false, name: "", players: Array.from({ length: size }, () => "") };
}

/** What is wrong with a draft, in words for the captain. Null when it can be sent. */
export function draftProblem(draft: TeamDraft): string | null {
  if (draft.name.trim().length < 2) return "Give your team a name.";
  const handles = draft.players.map((player) => player.trim().toLowerCase());
  if (handles.some((handle) => handle.length < 2)) return "Fill in every player's in-game name.";
  if (new Set(handles).size !== handles.length) return "Each player can only be listed once.";
  return null;
}

/**
 * Team entry for one tournament: the roster form, the caller's own team if
 * they have one, the waitlist when slots are full, and who is already in.
 */
export function TeamEntry({
  tournament: t,
  slot,
  available,
  signedIn,
  started,
  draft,
  onDraft,
}: {
  tournament: Tournament;
  slot: TicketType;
  available: number;
  signedIn: boolean;
  /** Team entry closes when the first match starts. */
  started: boolean;
  draft: TeamDraft;
  onDraft: (draft: TeamDraft) => void;
}) {
  const queryClient = useQueryClient();
  const slug = t.slug;
  const path = { params: { path: { slug } } };

  const teams = useQuery({
    queryKey: ["teams", slug],
    queryFn: async () => unwrap(await api.GET("/tournaments/{slug}/teams", path)),
    refetchInterval: 15000,
  });
  const mine = useQuery({
    queryKey: ["my-teams"],
    queryFn: async () => unwrap(await api.GET("/users/me/teams")),
    enabled: signedIn,
    refetchInterval: 8000,
  });
  const place = useQuery({
    queryKey: ["waitlist", slug],
    queryFn: async () => {
      try {
        return unwrap(await api.GET("/tournaments/{slug}/waitlist/me", path));
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    enabled: signedIn,
    // A slot can come up at any moment while someone is waiting.
    refetchInterval: (query) => (query.state.data?.status === "waiting" ? 5000 : false),
  });

  const refresh = () => {
    for (const key of ["teams", "waitlist", "availability"]) {
      queryClient.invalidateQueries({ queryKey: [key, slug] });
    }
    queryClient.invalidateQueries({ queryKey: ["my-teams"] });
  };
  const joinWaitlist = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/tournaments/{slug}/waitlist", {
          ...path,
          body: { name: draft.name.trim(), roster: draft.players.map((player) => player.trim()) },
        }),
      ),
    // The form's job is done: take the entry back out of the order summary.
    onSuccess: () => onDraft({ ...draft, on: false }),
    onSettled: refresh,
  });
  const leaveWaitlist = useMutation({
    mutationFn: async () => unwrap(await api.DELETE("/tournaments/{slug}/waitlist/me", path)),
    onSettled: refresh,
  });
  const withdraw = useMutation({
    mutationFn: async (teamId: string) =>
      unwrap(await api.DELETE("/teams/{team_id}", { params: { path: { team_id: teamId } } })),
    onSettled: refresh,
  });

  const myTeam = mine.data?.find((team) => team.tournament_id === t.id);
  const entry = place.data;
  const live = entry && (entry.status === "waiting" || entry.status === "offered");
  const full = available === 0;
  const problem = draftProblem(draft);
  const set = (change: Partial<TeamDraft>) => {
    joinWaitlist.reset();
    onDraft({ ...draft, ...change });
  };

  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="font-display text-xl font-semibold">Compete</h2>
        <p className="text-sm text-muted">
          <span className={full ? "text-danger" : available <= 3 ? "text-warn" : ""}>
            {full
              ? "All team slots taken"
              : started
                ? `${slot.capacity - available} of ${slot.capacity} slots filled`
                : `${available} of ${slot.capacity} team slots left`}
          </span>
        </p>
      </div>

      {myTeam ? (
        <div className="panel rise space-y-3 p-4">
          <div className="flex flex-wrap items-center gap-3">
            <p className="font-display text-lg font-semibold">{myTeam.name}</p>
            <StatusPill status={myTeam.status} />
          </div>
          <Roster players={myTeam.roster} />
          {myTeam.status === "pending" ? (
            <p className="text-sm text-muted">
              Your slot is held while you pay the entry fee.{" "}
              {myTeam.hold_id && (
                <Link className="text-primary underline" href={`/checkout/${myTeam.hold_id}`}>
                  Finish checkout
                </Link>
              )}
            </p>
          ) : (
            <p className="text-sm text-muted">You are in. See you on stage.</p>
          )}
          {withdraw.error && <Notice tone="error">{withdraw.error.message}</Notice>}
          <button
            type="button"
            className="btn btn-ghost"
            disabled={withdraw.isPending}
            onClick={() => {
              if (
                window.confirm(
                  `Withdraw ${myTeam.name}? Your slot goes to the next team waiting, and your entry fee is refunded.`,
                )
              ) {
                withdraw.mutate(myTeam.id);
              }
            }}
          >
            {withdraw.isPending ? "Withdrawing" : "Withdraw team"}
          </button>
        </div>
      ) : live && entry ? (
        <div className="panel rise space-y-3 p-4" role="status">
          <p className="label">Waitlist</p>
          {entry.status === "offered" ? (
            <>
              <p className="font-display text-lg font-semibold text-ok">A slot opened up for {entry.name}</p>
              <p className="text-sm text-muted">
                It is held for you for a limited time. Pay the entry fee to take it.
              </p>
              {entry.offered_hold_id && (
                <Link className="btn btn-primary" href={`/checkout/${entry.offered_hold_id}`}>
                  Claim the slot
                </Link>
              )}
            </>
          ) : (
            <>
              <p className="flex items-baseline gap-2">
                <span className="font-mono text-4xl font-semibold tabular-nums text-primary">
                  {entry.position}
                </span>
                <span className="text-sm text-muted">
                  {entry.position === 1 ? "You are next in line" : "in line"} with {entry.name}
                </span>
              </p>
              <p className="text-sm text-muted">
                If a team drops out, the slot is offered to the waitlist in order. This page updates when it is your turn.
              </p>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={leaveWaitlist.isPending}
                onClick={() => leaveWaitlist.mutate()}
              >
                Leave the waitlist
              </button>
            </>
          )}
        </div>
      ) : started ? (
        <Notice>Team entry closed when the tournament started.</Notice>
      ) : (
        <div className="panel space-y-4 p-4">
          <div className="flex flex-wrap items-center gap-4">
            <div className="min-w-0 flex-1">
              <p className="font-medium">{slot.name}</p>
              <p className="text-sm text-muted">
                {t.team_size === 1 ? "One slot for a solo player." : `One slot for a team of ${t.team_size}.`}{" "}
                Your roster is locked in with the slot.
              </p>
            </div>
            <p className="font-display text-lg font-semibold">{money(slot.price_cents, t.currency)}</p>
            <button
              type="button"
              className={`btn ${draft.on ? "btn-ghost" : "btn-primary"}`}
              aria-expanded={draft.on}
              onClick={() => set({ on: !draft.on })}
            >
              {draft.on ? "Cancel" : full ? "Join the waitlist" : "Enter a team"}
            </button>
          </div>

          {entry?.status === "lapsed" && entry.note && !draft.on && (
            <Notice tone="warn">Your earlier waitlist place lapsed. {entry.note}</Notice>
          )}

          {draft.on && (
            <div className="rise space-y-3 border-t border-line pt-4">
              <label className="block space-y-1.5">
                <span className="label">Team name</span>
                <input
                  className="input"
                  maxLength={40}
                  value={draft.name}
                  onChange={(event) => set({ name: event.target.value })}
                />
              </label>
              <fieldset className="space-y-1.5">
                <legend className="label">Players (in-game names)</legend>
                <div className="grid gap-2 sm:grid-cols-2">
                  {draft.players.map((player, index) => (
                    <input
                      key={index}
                      className="input"
                      maxLength={24}
                      aria-label={`Player ${index + 1}`}
                      placeholder={index === 0 ? "Player 1 (captain)" : `Player ${index + 1}`}
                      value={player}
                      onChange={(event) =>
                        set({ players: draft.players.map((p, i) => (i === index ? event.target.value : p)) })
                      }
                    />
                  ))}
                </div>
              </fieldset>
              {!signedIn ? (
                <Link className="btn btn-primary" href={`/login?next=/t/${slug}`}>
                  Sign in to continue
                </Link>
              ) : full ? (
                <>
                  {joinWaitlist.error && <Notice tone="error">{joinWaitlist.error.message}</Notice>}
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={problem !== null || joinWaitlist.isPending}
                    onClick={() => joinWaitlist.mutate()}
                  >
                    {joinWaitlist.isPending ? "Joining" : "Join the waitlist"}
                  </button>
                  <p className="text-xs text-muted">
                    {problem ?? "Free to join. You only pay if a slot comes up and you take it."}
                  </p>
                </>
              ) : (
                <p className="text-xs text-muted">
                  {problem ??
                    "Added to your order. Hold it to claim the slot, then pay to confirm your team."}
                </p>
              )}
            </div>
          )}
        </div>
      )}

      {teams.data && teams.data.length > 0 && (
        <details className="panel p-4">
          <summary className="cursor-pointer text-sm font-medium">
            {teams.data.length} {teams.data.length === 1 ? "team" : "teams"} confirmed
          </summary>
          <ul className="mt-3 grid gap-3 sm:grid-cols-2">
            {teams.data.map((team) => (
              <li key={team.id}>
                <p className="font-medium">{team.name}</p>
                <Roster players={team.roster} />
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function Roster({ players }: { players: string[] }) {
  return <p className="font-mono text-xs text-muted">{players.join(" · ")}</p>;
}
