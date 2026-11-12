import { api } from "./api";
import { getTokens } from "./session";

/** One step of a turn, as the server streams it. */
export type AssistantEvent =
  | { type: "assistant"; text: string; tool_calls: { id: string; name: string; arguments: Record<string, unknown> }[] }
  | { type: "tool"; id: string; name: string; ok: boolean; ms: number }
  | { type: "action"; id: string; tool: string; summary: string }
  | { type: "decided"; id: string; status: string; ok: boolean }
  | { type: "final"; text: string; unverified_amounts: string[] }
  | { type: "error"; code: string }
  | { type: "done" };

/**
 * POST to a streaming endpoint and hand each event to `onEvent` as it arrives.
 *
 * The typed client cannot read a stream, so this is a plain fetch. It carries
 * the same token and, like the typed client, refreshes it once if it has
 * expired.
 */
export async function streamTurn(
  path: string,
  body: unknown,
  onEvent: (event: AssistantEvent) => void,
): Promise<void> {
  const send = () =>
    fetch(`/api${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getTokens()?.access_token ?? ""}`,
      },
      body: JSON.stringify(body),
    });
  let response = await send();
  if (response.status === 401) {
    await api.GET("/users/me"); // the typed client refreshes the token as a side effect
    response = await send();
  }
  if (!response.ok || !response.body) {
    const problem = (await response.json().catch(() => ({}))) as { code?: string; detail?: string };
    throw Object.assign(new Error(problem.detail ?? "The assistant could not be reached."), {
      code: problem.code ?? "unknown_error",
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // Events are separated by a blank line; a chunk can end mid-event.
    let split: number;
    while ((split = buffer.indexOf("\n\n")) >= 0) {
      const line = buffer.slice(0, split).trim();
      buffer = buffer.slice(split + 2);
      if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as AssistantEvent);
    }
  }
}
