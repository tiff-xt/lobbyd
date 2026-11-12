"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { CameraScanner } from "@/components/CameraScanner";
import { Notice, Spinner } from "@/components/ui";
import { api, ApiError, type Manifest, unwrap } from "@/lib/api";
import { seatName } from "@/lib/format";

type Outcome = "admitted" | "duplicate" | "void" | "rejected" | "synced";
type LogLine = { id: string; outcome: Outcome; detail: string; offline: boolean };
type Queued = { id: string; token: string; scanned_at: string; ticket_id: string };

const TONE: Record<Outcome, string> = {
  admitted: "border-ok/50 bg-ok/10 text-ok",
  duplicate: "border-warn/50 bg-warn/10 text-warn",
  void: "border-danger/50 bg-danger/10 text-danger",
  rejected: "border-danger/50 bg-danger/10 text-danger",
  synced: "border-line bg-panel-2 text-text",
};
const HEADLINE: Record<Outcome, string> = {
  admitted: "Admit",
  duplicate: "Already used",
  void: "Cancelled ticket",
  rejected: "Not a valid ticket",
  synced: "Synced",
};

function load<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}
function store(key: string, value: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage refused: scans made offline will not survive a reload.
  }
}

function decode(text: string): Uint8Array<ArrayBuffer> {
  const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}
function uuid(bytes: Uint8Array): string {
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * Check a ticket token with nothing but the manifest: no network. Returns
 * the ticket id it names, or null if the signature is not ours or the
 * ticket is for another event.
 */
async function verifyOffline(token: string, manifest: Manifest): Promise<string | null> {
  try {
    const [prefix, keyId, payload, signature] = token.trim().split(".");
    const key = manifest.keys.find((candidate) => candidate.key_id === keyId);
    if (prefix !== "lbd1" || !key || !payload || !signature) return null;
    const publicKey = await crypto.subtle.importKey("raw", decode(key.public_key), "Ed25519", false, [
      "verify",
    ]);
    const signed = new TextEncoder().encode(`${prefix}.${keyId}.${payload}`);
    if (!(await crypto.subtle.verify("Ed25519", publicKey, decode(signature), signed))) return null;
    const raw = decode(payload);
    if (raw.length !== 32 || uuid(raw.slice(16)) !== manifest.tournament_id) return null;
    return uuid(raw.slice(0, 16));
  } catch {
    return null;
  }
}

export default function DoorPage() {
  const { slug } = useParams<{ slug: string }>();
  const queryClient = useQueryClient();
  const path = { params: { path: { slug } } };
  const manifestKey = `lobbyd.door.${slug}.manifest`;
  const queueKey = `lobbyd.door.${slug}.queue`;

  const [token, setToken] = useState("");
  const [offline, setOffline] = useState(false);
  const [log, setLog] = useState<LogLine[]>([]);
  const [queue, setQueue] = useState<Queued[]>([]);
  const [saved, setSaved] = useState<Manifest | null>(null);
  const [device, setDevice] = useState("door");
  const [camera, setCamera] = useState(false);
  // Stored state is read after the first paint, so server and browser render the same page.
  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect -- one-time read of localStorage */
    setQueue(load<Queued[]>(queueKey, []));
    setSaved(load<Manifest | null>(manifestKey, null));
    let name = load<string | null>("lobbyd.door.device", null);
    if (!name) {
      name = `door-${crypto.randomUUID().slice(0, 4)}`;
      store("lobbyd.door.device", name);
    }
    setDevice(name);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [queueKey, manifestKey]);

  const manifest = useQuery({
    queryKey: ["manifest", slug],
    queryFn: async () => {
      const fresh = unwrap(await api.GET("/checkin/{slug}/manifest", path));
      store(manifestKey, fresh);
      setSaved(fresh);
      return fresh;
    },
    enabled: !offline,
    refetchInterval: 30_000, // refresh whenever there is a connection
    retry: false,
  });
  const summary = useQuery({
    queryKey: ["door-summary", slug],
    queryFn: async () => unwrap(await api.GET("/checkin/{slug}/summary", path)),
    enabled: !offline,
    refetchInterval: 5000,
    retry: false,
  });

  const record = (line: Omit<LogLine, "id">) =>
    setLog((current) => [{ id: crypto.randomUUID(), ...line }, ...current].slice(0, 12));
  const saveQueue = (next: Queued[]) => {
    setQueue(next);
    store(queueKey, next);
  };

  const scan = useMutation({
    mutationFn: async (code: string) => {
      if (!offline) {
        try {
          const result = unwrap(
            await api.POST("/checkin/{slug}/scan", { ...path, body: { token: code, device_id: device } }),
          );
          // Keep this device's saved manifest in step with what it just
          // learned, so it is right if the network drops a moment from now.
          if (saved && result.checked_in_at) {
            const patched = {
              ...saved,
              tickets: saved.tickets.map((ticket) =>
                ticket.id === result.ticket_id
                  ? { ...ticket, checked_in_at: result.checked_in_at ?? null }
                  : ticket,
              ),
            };
            store(manifestKey, patched);
            setSaved(patched);
          }
          record({
            outcome: result.result as Outcome,
            detail: `${result.ticket_type_name}${result.seat ? `, ${seatName(result.seat)}` : ""}${
              result.result === "duplicate" && result.checked_in_at
                ? `. First used ${new Date(result.checked_in_at).toLocaleTimeString()}`
                : ""
            }`,
            offline: false,
          });
        } catch (error) {
          if (error instanceof ApiError && [409, 422, 404].includes(error.status)) {
            record({ outcome: "rejected", detail: error.message, offline: false });
          } else throw error;
        }
        return;
      }
      // Offline: the signature, the manifest, and what this device has
      // already let in are all there is to go on.
      if (!saved) throw new Error("No manifest was downloaded before going offline.");
      const ticketId = await verifyOffline(code, saved);
      const listed = ticketId && saved.tickets.find((ticket) => ticket.id === ticketId);
      if (!ticketId || !listed) {
        record({ outcome: "rejected", detail: "Not a valid ticket for this event.", offline: true });
        return;
      }
      const seen = listed.checked_in_at !== null || queue.some((q) => q.ticket_id === ticketId);
      saveQueue([
        ...queue,
        { id: crypto.randomUUID(), token: code, scanned_at: new Date().toISOString(), ticket_id: ticketId },
      ]);
      record({
        outcome: seen ? "duplicate" : "admitted",
        detail: `${listed.ticket_type_name}. Stored on this device until it syncs.`,
        offline: true,
      });
    },
    onSuccess: () => {
      setToken("");
      queryClient.invalidateQueries({ queryKey: ["door-summary", slug] });
    },
  });

  const sync = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/checkin/{slug}/scans", {
          ...path,
          body: {
            device_id: device,
            scans: queue.map(({ id, token: code, scanned_at }) => ({ id, token: code, scanned_at })),
          },
        }),
      ),
    onSuccess: (result) => {
      const counts = new Map<string, number>();
      for (const item of result.scans) counts.set(item.result, (counts.get(item.result) ?? 0) + 1);
      record({
        outcome: "synced",
        detail: `${result.scans.length} stored ${result.scans.length === 1 ? "scan" : "scans"} uploaded: ${[...counts].map(([name, n]) => `${n} ${name}`).join(", ")}.`,
        offline: false,
      });
      saveQueue([]);
      queryClient.invalidateQueries({ queryKey: ["manifest", slug] });
      queryClient.invalidateQueries({ queryKey: ["door-summary", slug] });
    },
  });

  const denied = manifest.error instanceof ApiError && [401, 403].includes(manifest.error.status);
  if (denied) {
    return <Notice tone="error">Only this tournament&apos;s organizer can run its door.</Notice>;
  }
  const latest = log[0];

  return (
    <div className="space-y-6">
      <header className="rise">
        <Link href={`/t/${slug}`} className="text-sm text-muted hover:text-text">
          ← {saved?.title ?? "Tournament"}
        </Link>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-display text-3xl font-bold sm:text-4xl">Door</h1>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              className="size-4 accent-[var(--color-primary)]"
              checked={offline}
              onChange={(event) => setOffline(event.target.checked)}
            />
            Pretend the network is down
          </label>
        </div>
        <p className="mt-1 text-sm text-muted">
          Scanner <span className="font-mono text-text">{device}</span>
          {saved && ` · manifest of ${saved.tickets.length} tickets on this device`}
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
        <div className="space-y-4">
          {/* The answer first: it is what the person at the door looks at. */}
          {latest && (
            <div key={latest.id} className={`rise rounded-xl border p-5 ${TONE[latest.outcome]}`} role="status">
              <p className="font-display text-3xl font-bold">{HEADLINE[latest.outcome]}</p>
              <p className="mt-1 text-sm">{latest.detail}</p>
            </div>
          )}
          <div className="panel space-y-3 p-4">
            <div className="flex items-center justify-between gap-3">
              <p className="label">Camera</p>
              <button
                type="button"
                className={`btn ${camera ? "btn-ghost" : "btn-primary"} !py-1.5 text-sm`}
                aria-pressed={camera}
                onClick={() => setCamera((on) => !on)}
              >
                {camera ? "Stop camera" : "Scan with camera"}
              </button>
            </div>
            {camera && (
              // While one scan is being checked the camera keeps showing, but
              // does not read, so a slow network cannot queue up a second scan.
              <CameraScanner paused={scan.isPending} onCode={(code) => scan.mutate(code)} />
            )}
          </div>

          <form
            className="panel space-y-3 p-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (token.trim()) scan.mutate(token.trim());
            }}
          >
            <label className="block space-y-1.5">
              <span className="label">Or paste a ticket code</span>
              <textarea
                className="input min-h-20 font-mono text-xs"
                placeholder="Paste what a ticket's QR code contains (use Copy ticket code on a ticket)"
                value={token}
                onChange={(event) => setToken(event.target.value)}
              />
            </label>
            <div className="flex flex-wrap items-center gap-3">
              <button type="submit" className="btn btn-primary" disabled={!token.trim() || scan.isPending}>
                {offline ? "Scan offline" : "Scan"}
              </button>
              {queue.length > 0 && (
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={offline || sync.isPending}
                  onClick={() => sync.mutate()}
                >
                  {sync.isPending ? "Syncing" : `Sync ${queue.length} stored`}
                </button>
              )}
            </div>
            {(scan.error ?? sync.error) && (
              <Notice tone="error">{(scan.error ?? sync.error)?.message}</Notice>
            )}
            {offline && (
              <p className="text-xs text-muted">
                Offline, tickets are checked against their signature and the manifest saved on
                this device. Another door cannot see these scans until both have synced.
              </p>
            )}
          </form>

          {log.length > 1 && (
            <ul className="space-y-1 text-sm">
              {log.slice(1).map((line) => (
                <li key={line.id} className="flex gap-3 text-muted">
                  <span className={`w-28 shrink-0 font-mono text-xs uppercase ${TONE[line.outcome].split(" ")[2]}`}>
                    {line.outcome}
                  </span>
                  <span className="truncate">{line.detail}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <aside className="panel space-y-4 p-5 lg:self-start">
          <h2 className="font-display text-xl font-semibold">Tonight</h2>
          {offline ? (
            <p className="text-sm text-muted">Totals come back with the network.</p>
          ) : summary.data ? (
            <>
              <p className="flex items-baseline gap-2">
                <span className="font-mono text-4xl font-semibold tabular-nums text-primary">
                  {summary.data.checked_in}
                </span>
                <span className="text-sm text-muted">of {summary.data.tickets} in</span>
              </p>
              {summary.data.flagged.length === 0 ? (
                <p className="text-sm text-muted">No duplicate scans.</p>
              ) : (
                <div className="space-y-2">
                  <p className="label">Needs a look</p>
                  <ul className="space-y-2 text-xs">
                    {summary.data.flagged.slice(0, 8).map((scan_) => (
                      <li key={scan_.scan_id} className="rounded-lg border border-line p-2">
                        <span className="text-warn">
                          {scan_.result === "duplicate" ? "Second scan" : "Doubtful clock"}
                        </span>{" "}
                        at <span className="font-mono">{scan_.device_id}</span>
                        {scan_.offline && " (offline)"},{" "}
                        {new Date(scan_.scanned_at).toLocaleTimeString()}
                        {scan_.admitted_by && (
                          <>
                            . Admitted at <span className="font-mono">{scan_.admitted_by}</span>
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          ) : (
            <Spinner />
          )}
        </aside>
      </div>
    </div>
  );
}
