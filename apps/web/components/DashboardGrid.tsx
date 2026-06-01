"use client";

import { useEffect, useRef, useState } from "react";
// v2 of react-grid-layout is a full rewrite with grouped config props
// (``gridConfig``, ``dragConfig`` …). The ``/legacy`` entry preserves
// the v1 flat API and is officially supported as a migration path
// (see the package README, "Migrating from v1"). We use it because the
// extra v2 surface area (custom compactors, position strategies) is
// not needed for our MVP; sticking with the v1 shape keeps the
// component small and lets us upgrade later without an API rewrite.
import GridLayout, { type Layout, type LayoutItem } from "react-grid-layout/legacy";
import {
  deleteDashboardItem,
  updateDashboardItem,
  type DashboardItem,
} from "@/lib/api";
import { DashboardCard } from "./DashboardCard";

/**
 * react-grid-layout wrapper that hosts the dashboard cards.
 *
 * Phase 2.1.1 / ADR 0020.
 *
 * Layout persistence
 * ------------------
 * We persist on ``onDragStop`` / ``onResizeStop`` rather than
 * ``onLayoutChange``. Reason: ``onLayoutChange`` fires on every
 * mount (and on every frame during a drag), so naïve persistence
 * would either fire spurious PATCHes on first render or hit the
 * backend with O(framerate) writes. The ``Stop`` callbacks fire
 * exactly once when the user releases the mouse — that's the
 * "save point" the FE / backend agree on.
 *
 * Width detection
 * ---------------
 * RGL needs an explicit pixel width. ``WidthProvider`` (the legacy
 * HOC) does this via window listeners; in React 19 it's cleaner to
 * own the ResizeObserver here. The fallback (800 px) only matters
 * for SSR + the first paint before the observer fires.
 *
 * Drag handle
 * -----------
 * ``draggableHandle=".drag-handle"`` restricts drag initiation to
 * the card header. Without this, dragging from inside a chart's
 * SVG would both move the card and confuse Vega's pan/zoom.
 */

const COLS = 12;
const ROW_HEIGHT = 60;
const MARGIN: [number, number] = [12, 12];

export function DashboardGrid({
  dashboardId,
  items,
  onItemsChange,
}: {
  dashboardId: string;
  items: DashboardItem[];
  onItemsChange: (items: DashboardItem[]) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(800);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (typeof w === "number" && w > 0) {
        setWidth(Math.max(Math.floor(w), 400));
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const layout: LayoutItem[] = items.map((it) => ({
    i: it.id,
    x: it.position_x,
    y: it.position_y,
    w: it.width,
    h: it.height,
    minW: 2,
    minH: 2,
  }));

  // Diff the post-drag/resize layout against the in-memory items
  // and PATCH only what changed. RGL hands us the FULL layout each
  // time, so we re-check every entry; this is cheap (dashboards
  // are bounded to dozens of cards in practice). ``Layout`` is the
  // readonly array type from RGL 2.x; we treat it as input-only.
  const persistLayout = (next: Layout) => {
    const updates: Promise<unknown>[] = [];
    const updated = items.slice();
    for (const l of next) {
      const idx = updated.findIndex((it) => it.id === l.i);
      if (idx < 0) continue;
      const curr = updated[idx];
      if (
        curr.position_x !== l.x ||
        curr.position_y !== l.y ||
        curr.width !== l.w ||
        curr.height !== l.h
      ) {
        updated[idx] = {
          ...curr,
          position_x: l.x,
          position_y: l.y,
          width: l.w,
          height: l.h,
        };
        updates.push(
          updateDashboardItem(dashboardId, l.i, {
            position_x: l.x,
            position_y: l.y,
            width: l.w,
            height: l.h,
          }),
        );
      }
    }
    if (updates.length > 0) {
      onItemsChange(updated);
      Promise.all(updates).catch((err) => {
        console.error("layout PATCH failed:", err);
      });
    }
  };

  const renameItem = async (itemId: string, title: string) => {
    try {
      const result = await updateDashboardItem(dashboardId, itemId, { title });
      onItemsChange(items.map((it) => (it.id === itemId ? result : it)));
    } catch (err) {
      console.error("rename card failed:", err);
      window.alert("Could not rename the card. See console for details.");
    }
  };

  const removeItem = async (itemId: string) => {
    if (!window.confirm("Remove this card from the dashboard?")) return;
    try {
      await deleteDashboardItem(dashboardId, itemId);
      onItemsChange(items.filter((it) => it.id !== itemId));
    } catch (err) {
      console.error("delete card failed:", err);
      window.alert("Could not remove the card. See console for details.");
    }
  };

  if (items.length === 0) {
    return (
      <div
        className="rounded-md border border-dashed border-(--color-border) bg-white p-8 text-center text-sm text-(--color-muted)"
        data-testid="dashboard-empty"
      >
        No cards yet. Open a chat, ask a question, and click{" "}
        <span className="font-mono">📌 Add to dashboard</span> on the answer.
      </div>
    );
  }

  return (
    <div ref={containerRef} className="w-full" data-testid="dashboard-grid">
      <GridLayout
        className="layout"
        layout={layout}
        cols={COLS}
        rowHeight={ROW_HEIGHT}
        width={width}
        margin={MARGIN}
        draggableHandle=".drag-handle"
        compactType="vertical"
        onDragStop={persistLayout}
        onResizeStop={persistLayout}
      >
        {items.map((it) => (
          <div key={it.id} data-testid="dashboard-card-wrapper">
            <DashboardCard
              item={it}
              onRename={(t) => renameItem(it.id, t)}
              onDelete={() => removeItem(it.id)}
            />
          </div>
        ))}
      </GridLayout>
    </div>
  );
}
