# 0009. An assistant that acts through the public API

**Status:** Accepted

## Context

A language model with tools can answer "where is my refund?" and carry out "withdraw my team". It can also be wrong, be confused by its own tools, or be talked into things by text it reads: a tournament description is written by an organizer, and the model reads it.

So the question is not how to make the model behave. It is what the system guarantees when it does not.

## Decision

**The model is an untrusted component.** Nothing that keeps the system safe depends on the prompt being followed.

1. **Tools are calls to the public API, made with the user's own token.** The assistant has no database access and no service account. A tool sends the signed-in user's bearer token to the same endpoints the web client uses, in process. Whatever the model asks for, it cannot see or change anything that user could not, and every rule the API already enforces (ownership, sale windows, rate limits, idempotency) applies unchanged. There is no second permission system to keep in step with the first.

2. **Changes wait for a person.** A tool marked as a write is never run by the loop. The call is validated, described in words using real names, stored, and shown to the user. Approving runs *that stored call*, not whatever the model says next. Declining, or simply typing something else, closes it. One change can be proposed per step.

3. **The loop is bounded.** At most six model calls per turn, one turn per conversation at a time, a per-user rate limit, and a daily ceiling across all users so a free model quota cannot be burned through.

4. **Answers are checked.** Money written in an answer must be traceable to a tool result in the conversation (the value itself, or a sum, difference or small multiple of values). An amount with no source sends the draft back once; if the model insists, the answer is shown with a warning naming the amount.

5. **Everything is recorded.** Each message, tool request, tool result and decision is stored in order with its timing and token counts. The record is what a turn resumes from after an approval, and what a person reads when asking "why did it say that?".

6. **Some things have no tool.** The assistant cannot buy, pay, or refund. Tickets' signed QR tokens are stripped from what it is shown.

The model itself sits behind a small interface, so the provider is a setting. Tests run the whole loop against a scripted stand-in that can be made to misbehave on purpose; real models are measured separately by scenario evals.

## What this does not solve

- **A person can approve a bad proposal.** The card states what will happen in plain words, with the team's real name and whether money moves, but a rushed click is still a click. This is why the write tools are few and each maps to something the user could already do in one click elsewhere on the site.
- **Reads are not gated.** Planted text can make the model look up more of the user's *own* data than they asked about. It cannot reach anyone else's, and nothing leaves the conversation.
- **Wrong but grounded.** The amount check catches invented numbers, not a real number attached to the wrong thing ("your refund was €80" when €80 was the order total). The evals measure this; the check does not prevent it.

## Options not taken

- **Tools that call service functions directly.** Faster by a few milliseconds, and then every tool needs its own permission check that must match the API's forever.
- **Let the model decide when to ask for approval.** Then safety depends on the model. Whether a tool writes is a property of the tool, decided in code.
- **A model grading the answers.** The evals grade with plain checks on what happened: which tools ran, what changed in the database, whether the right figures are in the answer. A second model would add cost and its own errors to the thing being measured.
- **Streaming the answer token by token.** Steps are streamed as they happen; the final text arrives whole, because it has to pass the amount check before anyone sees it.

## Consequences

- A weaker, free model is usable: its mistakes are bounded by the design, and the evals show how often it makes them.
- Each tool costs an in-process HTTP request (about 10 ms measured locally). Acceptable next to a model call measured in seconds.
- Adding a capability means adding an API endpoint first, then a tool. That is the point.
