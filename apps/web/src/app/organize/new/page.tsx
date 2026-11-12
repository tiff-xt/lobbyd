"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Notice } from "@/components/ui";
import { api, ApiError, unwrap } from "@/lib/api";

/** A <input type="datetime-local"> value, `days` from now at `hour` o'clock local time. */
function localInput(days: number, hour: number): string {
  const date = new Date();
  date.setDate(date.getDate() + days);
  date.setHours(hour, 0, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:00`;
}

export default function NewTournamentPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  // Sensible starting dates, read once: three weeks out, on sale from now.
  const [form, setForm] = useState(() => ({
    title: "",
    game: "",
    description: "",
    team_size: "5",
    currency: "EUR",
    venue_id: "",
    starts_at: localInput(21, 10),
    ends_at: localInput(21, 20),
    sales_open_at: localInput(0, new Date().getHours()),
    waiting_room: false,
  }));
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  const venues = useQuery({
    queryKey: ["my-venues"],
    queryFn: async () => unwrap(await api.GET("/users/me/venues")),
  });

  const create = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/tournaments", {
          body: {
            title: form.title.trim(),
            game: form.game.trim(),
            description: form.description.trim(),
            team_size: Number(form.team_size),
            currency: form.currency,
            venue_id: form.venue_id || null,
            // The browser's local time, sent with its offset.
            starts_at: new Date(form.starts_at).toISOString(),
            ends_at: new Date(form.ends_at).toISOString(),
            sales_open_at: new Date(form.sales_open_at).toISOString(),
            waiting_room: form.waiting_room,
          },
        }),
      ),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["my-tournaments"] });
      router.push(`/organize/${created.slug}`);
    },
  });
  const problem = create.error instanceof ApiError ? create.error : null;
  const fieldErrors = Array.isArray(problem?.extra.errors)
    ? (problem.extra.errors as { field: string; message: string }[])
    : [];

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <header className="rise">
        <Link href="/organize" className="text-sm text-muted hover:text-text">
          ← Your tournaments
        </Link>
        <h1 className="mt-2 font-display text-3xl font-bold">New tournament</h1>
        <p className="mt-1 text-sm text-muted">
          It starts as a draft. You add what is on sale next, and nothing is public until you publish.
        </p>
      </header>

      <form
        className="panel space-y-4 p-5"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Title">
          <input className="input" required minLength={3} maxLength={120} value={form.title} onChange={(e) => set("title", e.target.value)} />
        </Field>
        <div className="grid gap-4 sm:grid-cols-[1fr_8rem_8rem]">
          <Field label="Game">
            <input className="input" required maxLength={60} value={form.game} onChange={(e) => set("game", e.target.value)} />
          </Field>
          <Field label="Players per team">
            <input className="input" type="number" min={1} max={20} required value={form.team_size} onChange={(e) => set("team_size", e.target.value)} />
          </Field>
          <Field label="Currency">
            <select className="input" value={form.currency} onChange={(e) => set("currency", e.target.value)}>
              {["EUR", "USD", "GBP", "JPY", "CAD"].map((code) => (
                <option key={code}>{code}</option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Description">
          <textarea className="input min-h-20" maxLength={2000} value={form.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
        <Field label="Venue">
          <select className="input" value={form.venue_id} onChange={(e) => set("venue_id", e.target.value)}>
            <option value="">Online only (no seats)</option>
            {venues.data?.map((venue) => (
              <option key={venue.id} value={venue.id}>
                {venue.name}, {venue.city} ({venue.capacity.toLocaleString("en")} seats)
              </option>
            ))}
          </select>
        </Field>
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Starts">
            <input className="input" type="datetime-local" required value={form.starts_at} onChange={(e) => set("starts_at", e.target.value)} />
          </Field>
          <Field label="Ends">
            <input className="input" type="datetime-local" required value={form.ends_at} onChange={(e) => set("ends_at", e.target.value)} />
          </Field>
          <Field label="Sales open">
            <input className="input" type="datetime-local" required value={form.sales_open_at} onChange={(e) => set("sales_open_at", e.target.value)} />
          </Field>
        </div>
        <label className="flex cursor-pointer items-start gap-3 text-sm">
          <input
            type="checkbox"
            className="mt-0.5 size-4 accent-[var(--color-primary)]"
            checked={form.waiting_room}
            onChange={(e) => set("waiting_room", e.target.checked)}
          />
          <span>
            Use a waiting room
            <span className="block text-muted">
              For sales you expect to sell out in minutes. Buyers queue and are let in at a steady rate.
            </span>
          </span>
        </label>

        {problem && (
          <Notice tone="error">
            {problem.message}
            {fieldErrors.length > 0 && (
              <ul className="mt-1 list-disc pl-5">
                {fieldErrors.map((error, index) => (
                  <li key={index}>
                    {error.field ? `${error.field}: ` : ""}
                    {error.message}
                  </li>
                ))}
              </ul>
            )}
          </Notice>
        )}
        <button type="submit" className="btn btn-primary" disabled={create.isPending}>
          {create.isPending ? "Creating" : "Create draft"}
        </button>
      </form>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}
