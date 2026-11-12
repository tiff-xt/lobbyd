"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { OrderView } from "@/components/OrderView";
import { Notice, Spinner } from "@/components/ui";
import { api, unwrap } from "@/lib/api";

export default function OrderPage() {
  const { id } = useParams<{ id: string }>();
  const order = useQuery({
    queryKey: ["order", id],
    queryFn: async () =>
      unwrap(await api.GET("/orders/{order_id}", { params: { path: { order_id: id } } })),
    refetchInterval: (query) => (query.state.data?.status === "pending" ? 1000 : false),
  });

  if (order.isPending) return <Spinner label="Loading order" />;
  if (order.isError) return <Notice tone="error">{order.error.message}</Notice>;
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Link href="/orders" className="text-sm text-muted hover:text-text">
        ← All my tickets
      </Link>
      <OrderView order={order.data} />
    </div>
  );
}
