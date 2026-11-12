"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";
import { api, unwrap } from "./api";
import { getTokens, setTokens, subscribe } from "./session";

export function useSession() {
  // null on the server and on first paint, so the two always match.
  const tokens = useSyncExternalStore(subscribe, getTokens, () => null);
  const queryClient = useQueryClient();

  const me = useQuery({
    queryKey: ["me", tokens?.refresh_token],
    queryFn: async () => unwrap(await api.GET("/users/me")),
    enabled: tokens !== null,
    staleTime: 60_000,
  });

  const signOut = useCallback(async () => {
    const current = getTokens();
    setTokens(null);
    queryClient.clear();
    if (current) {
      await api.POST("/auth/logout", { body: { refresh_token: current.refresh_token } });
    }
  }, [queryClient]);

  return { signedIn: tokens !== null, user: me.data ?? null, signOut };
}
