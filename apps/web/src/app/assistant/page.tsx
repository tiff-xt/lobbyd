"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Notice, Spinner } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { type AssistantEvent, streamTurn } from "@/lib/assistant";
import { useSession } from "@/lib/useSession";

type Step = { id: string; name: string; state: "running" | "ok" | "refused"; ms?: number };
type Proposal = { id: string; summary: string; state: "pending" | "approved" | "declined" | "failed" };
type Entry =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; steps: Step[]; unverified: string[]; proposal?: Proposal; failed?: string };

const TOOL_LABEL: Record<string, string> = {
  search_tournaments: "Searched tournaments",
  get_tournament: "Read the tournament",
  list_my_orders: "Looked at your orders",
  get_order: "Read the order",
  list_my_teams: "Looked at your teams",
  get_my_waitlist_place: "Checked the waitlist",
  get_bracket: "Read the bracket",
  list_my_tournaments: "Listed your tournaments",
  get_sales: "Read the sales figures",
  get_door_summary: "Checked the door",
  withdraw_team: "Withdraw team",
  release_hold: "Release hold",
  leave_waitlist: "Leave waitlist",
  join_waitlist: "Join waitlist",
};

const FAILURE: Record<string, string> = {
  model_unavailable: "The language model did not answer. Try again in a moment.",
  model_busy: "The language model is at its free limit for now. Try again later.",
  internal_error: "Something went wrong on our side. Nothing was changed.",
  action_not_pending: "That change was already decided.",
};

export default function AssistantPage() {
  const { signedIn } = useSession();
  const status = useQuery({
    queryKey: ["assistant"],
    queryFn: async () => unwrap(await api.GET("/assistant")),
    enabled: signedIn,
  });
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState("");
  const conversation = useRef<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [entries]);

  /** Apply one streamed event to the reply being built (always the last entry). */
  const apply = (event: AssistantEvent) =>
    setEntries((current) => {
      const last = current[current.length - 1];
      if (!last || last.kind !== "assistant") return current;
      const reply = { ...last, steps: [...last.steps] };
      if (event.type === "assistant") {
        for (const call of event.tool_calls) {
          reply.steps.push({ id: call.id, name: call.name, state: "running" });
        }
      } else if (event.type === "tool") {
        reply.steps = reply.steps.map((step) =>
          step.id === event.id ? { ...step, state: event.ok ? "ok" : "refused", ms: event.ms } : step,
        );
      } else if (event.type === "action") {
        // The step that asked for it stops spinning: it is waiting on a person now.
        reply.steps = reply.steps.filter((step) => step.state !== "running");
        reply.proposal = { id: event.id, summary: event.summary, state: "pending" };
      } else if (event.type === "decided" && reply.proposal) {
        reply.proposal = {
          ...reply.proposal,
          state: event.status === "executed" ? "approved" : event.status === "rejected" ? "declined" : "failed",
        };
      } else if (event.type === "final") {
        reply.text = event.text;
        reply.unverified = event.unverified_amounts;
      } else if (event.type === "error") {
        reply.failed = FAILURE[event.code] ?? "The assistant ran into a problem.";
      }
      return [...current.slice(0, -1), reply];
    });

  const turn = useMutation({
    mutationFn: async (request: { path: string; body: unknown }) => {
      await streamTurn(request.path, request.body, apply);
    },
    onError: (error) =>
      setEntries((current) => {
        const last = current[current.length - 1];
        if (!last || last.kind !== "assistant") return current;
        return [...current.slice(0, -1), { ...last, failed: error.message }];
      }),
  });

  const say = async (text: string) => {
    const words = text.trim();
    if (!words || turn.isPending) return;
    setDraft("");
    setEntries((current) => [
      ...current,
      { kind: "user", text: words },
      { kind: "assistant", text: "", steps: [], unverified: [] },
    ]);
    if (!conversation.current) {
      const created = unwrap(await api.POST("/assistant/conversations"));
      conversation.current = created.id;
    }
    turn.mutate({ path: `/assistant/conversations/${conversation.current}/messages`, body: { text: words } });
  };

  const decideOn = (proposal: Proposal, approve: boolean) => {
    // The assistant's report on what happened arrives as a new reply.
    setEntries((current) => [...current, { kind: "assistant", text: "", steps: [], unverified: [] }]);
    // Mark the card straight away, so it cannot be pressed twice.
    setEntries((current) =>
      current.map((entry) =>
        entry.kind === "assistant" && entry.proposal?.id === proposal.id
          ? { ...entry, proposal: { ...entry.proposal, state: approve ? "approved" : "declined" } }
          : entry,
      ),
    );
    turn.mutate({ path: `/assistant/actions/${proposal.id}/decision`, body: { approve } });
  };

  if (!signedIn) {
    return (
      <Notice>
        <Link className="underline" href="/login?next=/assistant">
          Sign in
        </Link>{" "}
        to use the assistant. It works with your own orders, tickets and teams.
      </Notice>
    );
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <header className="rise">
        <p className="label">Assistant</p>
        <h1 className="font-display text-3xl font-bold sm:text-4xl">Ask about your tickets</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted">
          It can look up your orders, teams and waitlist places, and explain what the site told you. It
          sees only what you can see, and it asks before it changes anything.
        </p>
      </header>

      {status.data && !status.data.enabled && (
        <Notice tone="warn">
          The assistant is switched off on this site (no language model is configured). Everything
          else works as usual.
        </Notice>
      )}

      {/* Room at the bottom, so the last reply is never under the message box. */}
      <div className="space-y-4 pb-6" aria-live="polite">
        {entries.length === 0 && status.data?.enabled && (
          <div className="flex flex-wrap gap-2">
            {status.data.suggestions.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                className="rounded-full border border-line px-3.5 py-1.5 text-sm text-muted transition-colors hover:border-primary/60 hover:text-text"
                onClick={() => say(suggestion)}
              >
                {suggestion}
              </button>
            ))}
          </div>
        )}

        {entries.map((entry, index) =>
          entry.kind === "user" ? (
            <div key={index} className="rise flex justify-end">
              <p className="max-w-[85%] rounded-2xl rounded-br-md bg-primary/15 px-4 py-2.5">{entry.text}</p>
            </div>
          ) : (
            <Reply
              key={index}
              entry={entry}
              busy={turn.isPending && index === entries.length - 1}
              onDecide={decideOn}
              locked={turn.isPending}
            />
          ),
        )}
        <div ref={bottom} className="scroll-mb-32" />
      </div>

      <form
        className="panel sticky bottom-4 flex items-end gap-2 p-2"
        onSubmit={(event) => {
          event.preventDefault();
          say(draft);
        }}
      >
        <textarea
          className="input min-h-11 flex-1 resize-none !border-transparent !bg-transparent !shadow-none"
          rows={1}
          maxLength={2000}
          placeholder="Where are my tickets?"
          aria-label="Message the assistant"
          value={draft}
          disabled={!status.data?.enabled}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              say(draft);
            }
          }}
        />
        <button
          type="submit"
          className="btn btn-primary"
          disabled={!draft.trim() || turn.isPending || !status.data?.enabled}
        >
          Send
        </button>
      </form>
      {status.data?.model && (
        <p className="-mt-3 text-center font-mono text-[10px] text-muted">
          {status.data.model} · {status.data.tools.length} tools · changes need your approval
        </p>
      )}
    </div>
  );
}

function Reply({
  entry,
  busy,
  locked,
  onDecide,
}: {
  entry: Extract<Entry, { kind: "assistant" }>;
  busy: boolean;
  locked: boolean;
  onDecide: (proposal: Proposal, approve: boolean) => void;
}) {
  const nothingYet = !entry.text && entry.steps.length === 0 && !entry.proposal && !entry.failed;
  return (
    <div className="rise space-y-2">
      {/* What it did to get the answer, so the answer can be checked. */}
      {entry.steps.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {entry.steps.map((step) => (
            <li
              key={step.id}
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[11px] ${
                step.state === "refused" ? "border-warn/40 text-warn" : "border-line text-muted"
              }`}
              title={step.name}
            >
              {step.state === "running" ? (
                <span className="size-2 animate-pulse rounded-full bg-primary" />
              ) : (
                <span aria-hidden>{step.state === "ok" ? "✓" : "!"}</span>
              )}
              {TOOL_LABEL[step.name] ?? step.name}
              {step.ms !== undefined && step.state !== "running" && <span className="opacity-60">{step.ms} ms</span>}
            </li>
          ))}
        </ul>
      )}

      {entry.proposal && (
        <div
          className={`rounded-xl border p-4 ${
            entry.proposal.state === "pending" ? "border-primary/60 bg-primary/5" : "border-line"
          }`}
        >
          <p className="label">The assistant wants to make a change</p>
          <p className="mt-1.5">{entry.proposal.summary}</p>
          {entry.proposal.state === "pending" ? (
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                className="btn btn-primary"
                disabled={locked}
                onClick={() => onDecide(entry.proposal!, true)}
              >
                Approve
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={locked}
                onClick={() => onDecide(entry.proposal!, false)}
              >
                Decline
              </button>
            </div>
          ) : (
            <p className="mt-2 font-mono text-xs uppercase tracking-widest text-muted">
              {entry.proposal.state === "approved"
                ? "You approved this"
                : entry.proposal.state === "declined"
                  ? "You declined this"
                  : "This could not be done"}
            </p>
          )}
        </div>
      )}

      {entry.text && (
        <p className="max-w-[90%] whitespace-pre-wrap rounded-2xl rounded-bl-md border border-line bg-panel px-4 py-2.5">
          {entry.text}
        </p>
      )}
      {entry.unverified.length > 0 && (
        <Notice tone="warn">
          Check this against your orders: the assistant mentioned {entry.unverified.join(", ")}, which does
          not match anything it looked up.
        </Notice>
      )}
      {entry.failed && <Notice tone="error">{entry.failed}</Notice>}
      {busy && nothingYet && <Spinner label="Thinking" />}
    </div>
  );
}
