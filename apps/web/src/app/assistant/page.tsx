"use client";

import { AssistantChat } from "@/components/AssistantChat";

export default function AssistantPage() {
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
      <AssistantChat />
    </div>
  );
}
