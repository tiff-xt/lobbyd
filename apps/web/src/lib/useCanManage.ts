"use client";

import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./api";
import { useSession } from "./useSession";

/**
 * Whether the signed-in user runs this tournament. Only decides what to
 * show: the API checks for itself on every request.
 */
export function useCanManage(slug: string): boolean {
  const { user } = useSession();
  const organizes = user?.role === "organizer";
  const mine = useQuery({
    queryKey: ["my-tournaments", user?.id],
    queryFn: async () => unwrap(await api.GET("/users/me/tournaments")),
    enabled: organizes,
    staleTime: 60_000,
  });
  if (user?.role === "admin") return true;
  return organizes && (mine.data?.some((t) => t.slug === slug) ?? false);
}
