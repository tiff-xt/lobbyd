import type { ReactNode } from "react";

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-sm text-muted">
      <span
        aria-hidden
        className="size-4 animate-spin rounded-full border-2 border-line border-t-secondary"
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
    error: "border-danger/40 bg-danger/10 text-danger",
    success: "border-ok/40 bg-ok/10 text-ok",
    warn: "border-warn/40 bg-warn/10 text-warn",
  };
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`rounded-lg border px-3.5 py-2.5 text-sm ${tones[tone]}`}>
      {children}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  paid: "border-ok/40 bg-ok/10 text-ok",
  confirmed: "border-ok/40 bg-ok/10 text-ok",
  pending: "border-warn/40 bg-warn/10 text-warn",
  cancelled: "border-danger/40 bg-danger/10 text-danger",
  expired: "border-line bg-panel-2 text-muted",
  refunded: "border-muted/50 bg-panel-2 text-text",
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
