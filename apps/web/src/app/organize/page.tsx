"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Notice, Spinner, StatusPill } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useSession } from "@/lib/useSession";

export default function OrganizePage() {
  const { signedIn, user } = useSession();
  const allowed = user?.role === "organizer" || user?.role === "admin";
  const mine = useQuery({
    queryKey: ["my-tournaments", user?.id],
    queryFn: async () => unwrap(await api.GET("/users/me/tournaments")),
    enabled: allowed,
  });

  if (!signedIn) {
    return (
      <Notice>
        <Link className="underline" href="/login?next=/organize">
          Sign in
        </Link>{" "}
        with an organizer account to run tournaments.
      </Notice>
    );
  }
  if (user && !allowed) {
    return <Notice>This account buys tickets. Running tournaments needs an organizer account.</Notice>;
  }

  return (
    <div className="space-y-6">
      <header className="rise flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="label">Organizer</p>
          <h1 className="font-display text-3xl font-bold sm:text-4xl">Your tournaments</h1>
        </div>
        <Link href="/organize/new" className="btn btn-primary">
          New tournament
        </Link>
      </header>

      {mine.isPending ? (
        <Spinner label="Loading your tournaments" />
      ) : mine.isError ? (
        <Notice tone="error">{mine.error.message}</Notice>
      ) : mine.data.length === 0 ? (
        <div className="panel p-6 text-muted">Nothing yet. Create your first tournament.</div>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2">
          {mine.data.map((t, index) => (
            <li key={t.id} className="deal" style={{ animationDelay: `${Math.min(index, 12) * 40}ms` }}>
              <Link href={`/organize/${t.slug}`} className="panel panel-link block space-y-2 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="label">{t.game}</p>
                    <p className="truncate font-display text-lg font-semibold">{t.title}</p>
                  </div>
                  <StatusPill status={t.status} />
                </div>
                <p className="text-sm text-muted">
                  {dateTime(t.starts_at, t.venue?.timezone)} · {t.venue ? t.venue.name : "Online"}
                </p>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
