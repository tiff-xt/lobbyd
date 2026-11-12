import type { ReactNode } from "react";

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-muted">
      <span
        aria-hidden
        className="size-4 animate-spin rounded-full border-2 border-line border-t-cyan"
      />
      {label}
    </span>
  );
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "error" | "success" | "warn";
  children: ReactNode;
}) {
  const tones = {
    info: "border-line text-muted",
    error: "border-red/40 bg-red/10 text-red",
    success: "border-lime/40 bg-lime/10 text-lime",
    warn: "border-amber/40 bg-amber/10 text-amber",
  };
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`rounded-lg border px-3.5 py-2.5 text-sm ${tones[tone]}`}>
      {children}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  paid: "border-lime/40 bg-lime/10 text-lime",
  pending: "border-amber/40 bg-amber/10 text-amber",
  cancelled: "border-red/40 bg-red/10 text-red",
  expired: "border-line bg-panel-2 text-muted",
  refunded: "border-cyan/40 bg-cyan/10 text-cyan",
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 font-mono text-[11px] uppercase tracking-widest ${
        STATUS_TONE[status] ?? "border-line text-muted"
      }`}
    >
      {status}
    </span>
  );
}
