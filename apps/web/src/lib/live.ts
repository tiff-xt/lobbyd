"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Where the API's WebSocket endpoint lives. The page's other requests go
 * through this site's own /api proxy, but a route handler cannot carry a
 * WebSocket, so the browser connects to the API directly.
 */
function liveUrl(path: string): string {
  const base =
    process.env.NEXT_PUBLIC_LOBBYD_WS_URL ??
    `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.hostname}:8000`;
  return `${base}${path}`;
}

/**
 * Keep a WebSocket open to `path`, handing each message to `onMessage`.
 * Reconnects by itself, waiting a little longer after each failure. The
 * server sends the current state first on every connection, so a gap while
 * disconnected heals as soon as the socket is back.
 */
export function useLive(path: string, onMessage: (message: unknown) => void): boolean {
  const [connected, setConnected] = useState(false);
  const handler = useRef(onMessage);
  useEffect(() => {
    handler.current = onMessage;
  });

  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let stopped = false;

    const open = () => {
      socket = new WebSocket(liveUrl(path));
      socket.onopen = () => {
        attempt = 0;
        setConnected(true);
      };
      socket.onmessage = (event) => {
        try {
          handler.current(JSON.parse(String(event.data)));
        } catch {
          // Not ours to make sense of. The next snapshot puts things right.
        }
      };
      socket.onclose = (event) => {
        setConnected(false);
        // 1008: the server says there is nothing here to watch. Do not insist.
        if (stopped || event.code === 1008) return;
        attempt += 1;
        timer = setTimeout(open, Math.min(500 * 2 ** attempt, 10_000));
      };
    };
    open();
    return () => {
      stopped = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, [path]);

  return connected;
}
