"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  getDashboard,
  updateDashboard,
  type DashboardDetail,
  type DashboardItem,
} from "@/lib/api";
import { DashboardGrid } from "./DashboardGrid";

/**
 * Client shell for ``/dashboards/[id]``. Owns the dashboard state,
 * the title inline-rename, and threads ``onItemsChange`` through to
 * the grid so drag / resize / delete writes stay in sync without a
 * round-trip refresh.
 *
 * Phase 2.1.1 / ADR 0020.
 */
export function DashboardDetailView({ dashboardId }: { dashboardId: string }) {
  const [dashboard, setDashboard] = useState<DashboardDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const d = await getDashboard(dashboardId);
        if (cancelled) return;
        setDashboard(d);
        setDraft(d.title);
      } catch (e) {
        if (cancelled) return;
        setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [dashboardId]);

  const handleItemsChange = (items: DashboardItem[]) => {
    setDashboard((prev) => (prev ? { ...prev, items } : prev));
  };

  const commitTitle = async () => {
    if (!dashboard) return;
    setEditing(false);
    const t = draft.trim();
    if (!t || t === dashboard.title) {
      setDraft(dashboard.title);
      return;
    }
    try {
      const updated = await updateDashboard(dashboardId, { title: t });
      setDashboard({ ...dashboard, title: updated.title });
      setDraft(updated.title);
    } catch (e) {
      setError((e as Error).message);
      setDraft(dashboard.title);
    }
  };

  if (error) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-8">
        <Link
          href="/dashboards"
          className="text-sm text-(--color-muted) hover:underline"
        >
          ← Dashboards
        </Link>
        <div className="mt-4 rounded-md border border-(--color-error) bg-white p-4 text-sm text-(--color-error)">
          {error}
        </div>
      </div>
    );
  }

  if (!dashboard) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-8 text-sm text-(--color-muted)">
        Loading…
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <Link
        href="/dashboards"
        className="text-sm text-(--color-muted) hover:underline"
      >
        ← Dashboards
      </Link>
      <div className="mt-2 mb-6 flex items-center justify-between gap-3">
        {editing ? (
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => void commitTitle()}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void commitTitle();
              } else if (e.key === "Escape") {
                setEditing(false);
                setDraft(dashboard.title);
              }
            }}
            className="flex-1 rounded-md border border-(--color-border) bg-white px-2 py-1 text-2xl font-semibold"
            aria-label="Edit dashboard title"
          />
        ) : (
          <h1
            className="cursor-text text-2xl font-semibold"
            onDoubleClick={() => setEditing(true)}
            title="Double-click to rename"
          >
            {dashboard.title}
          </h1>
        )}
        <div className="text-xs text-(--color-muted)">
          {dashboard.items.length} card
          {dashboard.items.length === 1 ? "" : "s"}
        </div>
      </div>
      <DashboardGrid
        dashboardId={dashboardId}
        items={dashboard.items}
        onItemsChange={handleItemsChange}
      />
    </div>
  );
}
