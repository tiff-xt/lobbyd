"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { useSession } from "@/lib/useSession";
import { AssistantChat } from "./AssistantChat";

/**
 * The assistant, one click away on every page: a round button in the bottom
 * right corner that opens a chat panel. Only for signed-in users, since the
 * assistant works with their own orders and teams.
 *
 * The panel stays mounted while closed, so a conversation survives closing
 * it and moving between pages.
 */
export function AssistantWidget() {
  const { signedIn } = useSession();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  // Opened at least once: until then the chat (and its requests) is not loaded at all.
  const [started, setStarted] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // Not on its own full page (it would be two of the same conversation box),
  // and not at the door, where the corner belongs to the scanner.
  if (!signedIn || pathname === "/assistant" || pathname.endsWith("/door")) return null;

  return (
    <div className="fixed bottom-4 right-4 z-30 flex flex-col items-end gap-3 sm:bottom-6 sm:right-6">
      <section
        aria-label="Assistant"
        hidden={!open}
        className="panel rise flex h-[min(34rem,calc(100dvh-7rem))] w-[min(24rem,calc(100vw-2rem))] flex-col overflow-hidden shadow-2xl shadow-black/60"
      >
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <p className="font-display text-base font-semibold">Assistant</p>
          <div className="flex items-center gap-1">
            <Link
              href="/assistant"
              className="rounded-md px-2 py-1 text-xs text-muted hover:text-text"
              onClick={() => setOpen(false)}
            >
              Full page
            </Link>
            <button
              type="button"
              className="rounded-md px-2 py-1 text-xs text-muted hover:text-text"
              onClick={() => setOpen(false)}
            >
              Close
            </button>
          </div>
        </header>
        {started && <AssistantChat compact />}
      </section>

      <button
        type="button"
        aria-expanded={open}
        aria-label={open ? "Close the assistant" : "Open the assistant"}
        className="btn btn-primary grid size-14 place-items-center !rounded-full !p-0 shadow-lg shadow-black/50"
        onClick={() => {
          setStarted(true);
          setOpen((now) => !now);
        }}
      >
        {open ? (
          <svg viewBox="0 0 24 24" className="size-6" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden>
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" className="size-6" fill="currentColor" aria-hidden>
            <path d="M12 3c5.2 0 9 3.4 9 7.8s-3.8 7.8-9 7.8c-.9 0-1.8-.1-2.6-.3L5 21l.9-3.9C4.1 15.700 3 13.900 3 10.800 3 6.400 6.800 3 12 3z" />
          </svg>
        )}
      </button>
    </div>
  );
}
