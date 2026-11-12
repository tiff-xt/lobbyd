/**
 * Signed-in state, kept in localStorage so it survives a reload.
 *
 * This is a demo client. A production one would keep the refresh token in an
 * httpOnly cookie, out of reach of any script on the page.
 */
export type Tokens = { access_token: string; refresh_token: string };

const KEY = "lobbyd.tokens";
const listeners = new Set<() => void>();
let cached: Tokens | null | undefined;

function read(): Tokens | null {
  if (typeof window === "undefined") return null;
  if (cached !== undefined) return cached;
  try {
    const raw = window.localStorage.getItem(KEY);
    cached = raw ? (JSON.parse(raw) as Tokens) : null;
  } catch {
    cached = null;
  }
  return cached;
}

export function getTokens(): Tokens | null {
  return read();
}

export function setTokens(tokens: Tokens | null) {
  cached = tokens;
  try {
    if (tokens) window.localStorage.setItem(KEY, JSON.stringify(tokens));
    else window.localStorage.removeItem(KEY);
  } catch {
    // Private browsing can refuse storage. The session then lasts until reload.
  }
  for (const listener of listeners) listener();
}

export function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
