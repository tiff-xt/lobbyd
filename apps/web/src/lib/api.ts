import type { paths, Schemas } from "@lobbyd/api-client";
import createClient, { type Middleware } from "openapi-fetch";
import { getTokens, setTokens } from "./session";

export type Tournament = Schemas["TournamentOut"];
export type TournamentSummary = Schemas["TournamentSummary"];
export type TicketType = Schemas["TicketTypeOut"];
export type Availability = Schemas["Availability"];
export type SeatMap = Schemas["SeatMap"];
export type Hold = Schemas["HoldOut"];
export type Order = Schemas["OrderOut"];
export type Ticket = Schemas["TicketOut"];
export type Problem = Schemas["Problem"];
export type QueueStatus = Schemas["QueueStatus"];
export type Bracket = Schemas["BracketOut"];
export type Match = Schemas["MatchOut"];
export type Manifest = Schemas["Manifest"];
export type ScanResult = Schemas["ScanOut"];

/** An error response from the API, already parsed. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    detail: string,
    readonly extra: Record<string, unknown> = {},
  ) {
    super(detail);
  }
}

let refreshing: Promise<boolean> | null = null;

/** Trade the refresh token for a new pair. Shared, so parallel 401s refresh once. */
function refresh(): Promise<boolean> {
  refreshing ??= (async () => {
    const tokens = getTokens();
    if (!tokens) return false;
    const response = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: tokens.refresh_token }),
    });
    if (!response.ok) {
      setTokens(null);
      return false;
    }
    setTokens(await response.json());
    return true;
  })().finally(() => {
    refreshing = null;
  });
  return refreshing;
}

const auth: Middleware = {
  async onRequest({ request }) {
    const tokens = getTokens();
    if (tokens) request.headers.set("Authorization", `Bearer ${tokens.access_token}`);
    return request;
  },
  async onResponse({ request, response }) {
    // An expired access token: get a new one and repeat the request once.
    // Safe for writes too, because they carry an Idempotency-Key.
    if (response.status !== 401 || request.headers.has("X-Retried") || !getTokens()) return response;
    if (!(await refresh())) return response;
    const retry = new Request(request, { headers: new Headers(request.headers) });
    retry.headers.set("Authorization", `Bearer ${getTokens()!.access_token}`);
    retry.headers.set("X-Retried", "1");
    return fetch(retry);
  },
};

export const api = createClient<paths>({ baseUrl: "/api" });
api.use(auth);

/** Unwrap an openapi-fetch result: the data, or a thrown ApiError. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) {
    const problem = (result.error ?? {}) as Partial<Problem> & Record<string, unknown>;
    const { status, code, detail, ...rest } = problem;
    // What is left after the standard fields is specific to the error,
    // such as which seats were taken.
    const extra = Object.fromEntries(
      Object.entries(rest).filter(([name]) => !["title", "type", "instance"].includes(name)),
    );
    throw new ApiError(
      status ?? result.response.status,
      code ?? "unknown_error",
      detail ?? "Something went wrong.",
      extra,
    );
  }
  return result.data as T;
}

/** A fresh key for one intended action. Reuse it when retrying that same action. */
export function idempotencyKey(): string {
  return crypto.randomUUID();
}
