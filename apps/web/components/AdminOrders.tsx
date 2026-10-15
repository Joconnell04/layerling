"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ORDER_STATUSES, type OrderRow, type OrderStatus } from "@/lib/db.types";
import { ago } from "@/lib/format";
import { PRODUCT_LABELS } from "@/lib/products";
import { Button, Empty, ErrorLine, Tag, Tip, cx, inputCls } from "./ui";

export type AdminOrder = OrderRow & { photo_urls: (string | null)[] };

const STATUS_TONE: Record<OrderStatus, string> = {
  new: "text-accent",
  contacted: "text-run",
  quoted: "text-run",
  accepted: "text-ok",
  declined: "text-faint",
  done: "text-ok",
  spam: "text-faint",
};

export function AdminOrders({ fetchImpl = fetch }: { fetchImpl?: typeof fetch }) {
  const [orders, setOrders] = useState<AdminOrder[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<"open" | "all">("open");

  const load = useCallback(async () => {
    try {
      const res = await fetchImpl("/api/admin/orders", { cache: "no-store" });
      if (!res.ok) throw new Error(res.status === 401 ? "Sign in required" : `Load failed (${res.status})`);
      setOrders(((await res.json()) as { orders: AdminOrder[] }).orders);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [fetchImpl]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 15_000);
    return () => clearInterval(t);
  }, [load]);

  const patch = (id: string, row: Partial<AdminOrder>) =>
    setOrders((cur) => (cur ?? []).map((o) => (o.id === id ? { ...o, ...row } : o)));

  const setStatus = async (o: AdminOrder, status: OrderStatus) => {
    setBusy(o.id);
    patch(o.id, { status });
    try {
      const res = await fetchImpl(`/api/admin/orders/${o.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ status }),
      });
      if (!res.ok) throw new Error(`Status change failed (${res.status})`);
    } catch (e) {
      patch(o.id, { status: o.status });
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const startJob = async (o: AdminOrder) => {
    setBusy(o.id);
    try {
      const res = await fetchImpl(`/api/admin/orders/${o.id}/job`, { method: "POST" });
      const body = (await res.json()) as { job_id?: string; error?: string; order?: OrderRow };
      if (!res.ok || !body.job_id) throw new Error(body.error ?? `Start failed (${res.status})`);
      patch(o.id, { job_id: body.job_id, status: body.order?.status ?? "accepted" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const shown = (orders ?? []).filter((o) => filter === "all" || !["declined", "done", "spam"].includes(o.status));

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-[34px] font-bold leading-none tracking-[-0.02em]">Orders</h1>
        <span className="num text-sm text-faint">{shown.length}</span>
        <div className="ml-auto flex gap-1">
          <Button size="sm" variant={filter === "open" ? "default" : "ghost"} onClick={() => setFilter("open")}>
            Open
          </Button>
          <Button size="sm" variant={filter === "all" ? "default" : "ghost"} onClick={() => setFilter("all")}>
            All
          </Button>
        </div>
      </div>
      <ErrorLine>{error}</ErrorLine>
      {orders === null && !error && <Empty>Loading</Empty>}
      {orders && shown.length === 0 && <Empty>No orders</Empty>}
      <ul className="space-y-2">
        {shown.map((o) => (
          <li key={o.id} data-testid={`order-${o.id}`} className="rounded-lg border border-line bg-panel p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-fg">{o.name}</span>
              {o.email && (
                <a href={`mailto:${o.email}`} className="text-sm text-muted hover:text-fg">
                  {o.email}
                </a>
              )}
              {o.phone && <span className="text-sm text-muted">{o.phone}</span>}
              <Tip label={new Date(o.created_at).toLocaleString()}>
                <span className="num text-xs text-faint">{ago(o.created_at)}</span>
              </Tip>
              <span className="ml-auto flex items-center gap-2">
                <NotifyTag o={o} />
                <select
                  aria-label={`Status ${o.name}`}
                  value={o.status}
                  disabled={busy === o.id}
                  onChange={(e) => void setStatus(o, e.target.value as OrderStatus)}
                  className={inputCls(cx("h-6 w-28 px-1 text-xs", STATUS_TONE[o.status]))}
                >
                  {ORDER_STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
                {o.job_id ? (
                  <Link href={`/jobs/${o.job_id}`}>
                    <Button size="sm" tabIndex={-1}>
                      Open job
                    </Button>
                  </Link>
                ) : (
                  <Button size="sm" variant="primary" disabled={busy === o.id} onClick={() => void startJob(o)}>
                    {busy === o.id ? "Starting" : "Start job"}
                  </Button>
                )}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <Tag>{PRODUCT_LABELS[o.product_type] ?? o.product_type}</Tag>
              {o.name_text && <Tag className="font-semibold text-fg">{o.name_text}</Tag>}
              {o.size_tier && <Tag className="font-semibold text-fg">{o.size_tier.toUpperCase()}</Tag>}
              {o.size_mm != null && <Tag className="num">{o.size_mm} mm</Tag>}
              <Tag className="num">x{o.quantity}</Tag>
              {o.source && <Tag>{o.source}</Tag>}
            </div>
            <div className="mt-2 flex gap-3">
              <div className="flex shrink-0 gap-1.5">
                {o.photo_urls.map((u, i) =>
                  u ? (
                    <a key={i} href={u} target="_blank" rel="noreferrer">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={u} alt={`Photo ${i + 1}`} className="size-16 rounded-md border border-line object-cover" />
                    </a>
                  ) : (
                    <span key={i} className="size-16 rounded-md border border-line bg-raised" />
                  ),
                )}
              </div>
              <p className="min-w-0 whitespace-pre-wrap text-sm text-fg">{o.request}</p>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function NotifyTag({ o }: { o: OrderRow }) {
  if (o.notified_at) {
    return (
      <Tip label={`Emailed ${new Date(o.notified_at).toLocaleString()}`}>
        <Tag className="text-ok">Emailed</Tag>
      </Tip>
    );
  }
  if (o.notify_error) {
    return (
      <Tip label={o.notify_error}>
        <Tag className={o.notify_error === "no provider" || o.notify_error === "test order" ? "text-faint" : "text-bad"}>
          {o.notify_error === "test order" ? "Test" : o.notify_error === "no provider" ? "No email" : "Email failed"}
        </Tag>
      </Tip>
    );
  }
  return null;
}
