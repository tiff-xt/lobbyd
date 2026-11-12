# The assistant

An AI agent that operates lobbyd on a user's behalf: it looks up their orders, tickets, teams and waitlist places, explains what the site told them, and can carry out a few changes after they approve. Organizers can ask it how their tournaments are selling.

The interesting part is not the model. It is what the system guarantees when the model is wrong. The reasoning is in [ADR 0009](adr/0009-assistant-agent.md); this page is how it works and how it is measured.

## How a turn runs

```
the user's message
  -> model            may ask for tools
  -> tools            reads run at once; a write is stored and shown to the user
  -> model again      with what the tools returned
  -> ...              at most six model calls
  -> answer           checked, then shown
```

Each step is streamed to the browser as it happens (server-sent events), so the user sees "Looked at your orders" before the answer arrives. Every message, tool request, tool result and decision is stored in order, with timings and token counts. `GET /assistant/conversations/{id}` returns the full record.

## What keeps it safe

| Guarantee | How | Where it is tested |
| --- | --- | --- |
| It can only see and do what the user can | Tools call the public API with the user's own token. No database access, no service account | A buyer's model asks for sales figures; an organizer's asks about a rival's event; a model tries to withdraw someone else's team |
| Nothing changes without a person | Write tools are stored and shown, never run by the loop. Approval runs the stored call | Approve, decline, move on without answering, approve five times at once |
| Planted instructions get nowhere | Text in tool results is data; and obeying it only produces a question to the user | A description orders a withdrawal and a scripted model obeys |
| It cannot run away | Six model calls per turn, one turn at a time, per-user and daily limits | A model that never stops asking for tools |
| Invented amounts are caught | Money in an answer must trace to a tool result; one retry, then a visible warning | A model states €95 for an €80 order |
| A broken model breaks nothing else | Provider errors become one event; the conversation and the site carry on | The provider returns an error |

All of these run in CI against a scripted model that can be made to misbehave on purpose ([`tests/test_assistant.py`](../apps/api/tests/test_assistant.py)). They prove the machinery, not the model.

## Tools

Read, run without asking: `search_tournaments`, `get_tournament`, `list_my_orders`, `get_order`, `list_my_teams`, `get_my_waitlist_place`, `get_bracket`. For organizers also `list_my_tournaments`, `get_sales`, `get_door_summary`.

Write, need approval: `withdraw_team`, `release_hold`, `leave_waitlist`, `join_waitlist`.

There is deliberately no tool to buy, pay, or refund. Each tool trims what it returns to what an answer needs, and ticket QR tokens are removed.

## Choosing a model

The model sits behind one interface ([`assistant/provider.py`](../apps/api/src/lobbyd/assistant/provider.py)). Any provider with a chat-completions endpoint and tool calling works:

```bash
LOBBYD_ASSISTANT_BASE_URL=https://.../v1
LOBBYD_ASSISTANT_API_KEY=...
LOBBYD_ASSISTANT_MODEL=...
```

With no base URL the assistant is off: the page says so and the rest of the site is unaffected.

## Measuring a real model

Scripted tests cannot say whether a real model picks the right tool or reports the right figure. That is what the evals are for ([`tests/evals/test_scenarios.py`](../apps/api/tests/evals/test_scenarios.py)).

Fourteen scenarios. Each builds real state through the API (orders, teams, refunds, a waitlist), asks what a user would ask, plays the user's part if approval is requested, and then grades with plain checks:

- **which tools ran** (did it look the order up, or answer from nothing?)
- **what changed** (is the team withdrawn? is someone else's team untouched?)
- **what the answer contains** (the right total, the refund, the place in line)
- **what it must not contain** (a claim that a declined change happened, another event's revenue)

No model grades another model. A scenario passes only if every check does.

```bash
cd apps/api
LOBBYD_EVAL_BASE_URL=... LOBBYD_EVAL_API_KEY=... LOBBYD_EVAL_MODEL=... uv run pytest tests/evals -q
uv run python evals/report.py
```

### Results

No real model has been run through the evals yet. As a floor, a stand-in that answers every question with "I don't know" passes 4 of 14: the four scenarios whose checks are all of the form "did not do the wrong thing". A model has to beat that to be worth anything, which is why most scenarios also check that the right thing was done.

## Limits

- It answers from tools, so it knows nothing the API does not expose (it cannot explain a declined card beyond "the payment was declined").
- The amount check catches invented numbers, not a real number attached to the wrong fact.
- Conversations are capped at the last forty messages sent to the model. Older ones stay in the record.
- English only.
