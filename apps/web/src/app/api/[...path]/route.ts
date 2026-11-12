/**
 * Forwards /api/* to the lobbyd API.
 *
 * The browser only ever talks to this site's own origin, so there is no CORS
 * to configure and the API's address is a server-side setting read at run
 * time (LOBBYD_API_URL), not something baked into the JavaScript bundle.
 */
const API_URL = process.env.LOBBYD_API_URL ?? "http://localhost:8000";

// Headers that describe one hop of the connection and must not be passed along.
const HOP_BY_HOP = ["connection", "content-length", "content-encoding", "host", "transfer-encoding"];

async function forward(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const target = new URL(`${API_URL}/${path.map(encodeURIComponent).join("/")}`);
  target.search = new URL(request.url).search;

  const headers = new Headers(request.headers);
  for (const name of HOP_BY_HOP) headers.delete(name);

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      cache: "no-store",
    });
  } catch {
    return Response.json(
      { status: 502, code: "api_unreachable", detail: "The lobbyd API is not reachable." },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers(upstream.headers);
  for (const name of HOP_BY_HOP) responseHeaders.delete(name);
  return new Response(upstream.status === 204 ? null : upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export { forward as DELETE, forward as GET, forward as PATCH, forward as POST };
