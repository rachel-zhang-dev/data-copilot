"use client";

import Link from "next/link";
import { useState } from "react";
import type { DashboardItem } from "@/lib/api";
import type { ChartKind, Insight } from "@/lib/types";
import { ChartRenderer } from "./ChartRenderer";
import { InsightPanel } from "./InsightPanel";

/**
 * One snapshot card on a dashboard grid.
 *
 * Phase 2.1.1 / ADR 0020. The card renders ONLY from the snapshot
 * columns stored at extract time. The SQL is preserved but never
 * re-executed at render time — that keeps a 12-card dashboard at
 * $0 per load and means deleting the source chat never breaks a
 * card.
 *
 * Drag affordance: the header carries the ``drag-handle`` class so
 * react-grid-layout only initiates a drag from there. The body
 * (chart, table, insight) stays interactive — users can scroll a
 * long table without accidentally repositioning the card.
 *
 * Rename UX matches ``SavedDrawer`` (double-click → input → Enter
 * saves / Escape cancels), keeping the editing pattern consistent
 * across the app.
 */
export function DashboardCard({
  item,
  onRename,
  onDelete,
}: {
  item: DashboardItem;
  onRename: (title: string) => void | Promise<void>;
  onDelete: () => void | Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.title);

  const commit = async () => {
    setEditing(false);
    const t = draft.trim();
    if (!t || t === item.title) {
      setDraft(item.title);
      return;
    }
    await onRename(t);
  };

  const insight = item.insight as Insight | null;
  const chartKind = item.chart_kind as ChartKind | null;
  const hasInsight =
    insight !== null &&
    ((insight.bullets && insight.bullets.length > 0) ||
      (insight.metric_highlights && insight.metric_highlights.length > 0));

  return (
    <div
      className="flex h-full w-full flex-col overflow-hidden rounded-md border border-(--color-border) bg-white shadow-xs"
      data-testid="dashboard-card"
    >
      <header className="drag-handle flex cursor-move items-center justify-between gap-2 border-b border-(--color-border) bg-(--color-bg) px-3 py-1.5">
        {editing ? (
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => void commit()}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void commit();
              } else if (e.key === "Escape") {
                setEditing(false);
                setDraft(item.title);
              }
            }}
            onMouseDown={(e) => e.stopPropagation()}
            className="flex-1 cursor-text rounded-sm border border-(--color-border) bg-white px-1 py-0.5 text-sm"
            aria-label="Edit card title"
          />
        ) : (
          <button
            type="button"
            onDoubleClick={() => setEditing(true)}
            onMouseDown={(e) => e.stopPropagation()}
            className="flex-1 truncate text-left text-sm font-medium"
            title={`${item.title} — double-click to rename`}
          >
            {item.title}
          </button>
        )}
        <button
          type="button"
          onClick={() => void onDelete()}
          onMouseDown={(e) => e.stopPropagation()}
          className="rounded-sm px-1.5 text-(--color-muted) hover:bg-(--color-error) hover:text-white"
          aria-label="Remove card"
          title="Remove card"
        >
          ×
        </button>
      </header>
      <div className="flex flex-1 flex-col gap-2 overflow-auto p-3">
        {hasInsight && <InsightPanel insight={insight} />}
        {chartKind && (
          <ChartRenderer
            kind={chartKind}
            spec={item.chart_spec}
            rows={item.rows}
          />
        )}
        {!hasInsight && !chartKind && item.answer && (
          <p className="text-sm">{item.answer}</p>
        )}
        {item.sql && (
          <details className="text-xs text-(--color-muted)">
            <summary className="cursor-pointer select-none">
              SQL{item.row_count !== null ? ` · ${item.row_count} rows` : ""}
            </summary>
            <pre className="mt-1 overflow-x-auto rounded-sm bg-(--color-bg) p-2">
              <code>{item.sql}</code>
            </pre>
          </details>
        )}
      </div>
      {item.source_thread_id && (
        // Phase 2.2 — back-link to the conversation that produced this
        // card. ``source_turn_index`` is included when known so the
        // chat panel can scroll the right turn into view. Lives
        // OUTSIDE the scrollable body so it's always visible even on
        // tall cards. Hidden when there's no provenance (a future
        // "ad-hoc card" path could write rows with both columns NULL).
        <footer className="border-t border-(--color-border) bg-(--color-bg) px-3 py-1 text-right">
          <Link
            href={`/?conversation=${encodeURIComponent(item.source_thread_id)}${
              item.source_turn_index
                ? `&turn=${item.source_turn_index}`
                : ""
            }`}
            className="text-xs text-(--color-muted) hover:underline"
            data-testid="view-source-chat-link"
          >
            View source chat →
          </Link>
        </footer>
      )}
    </div>
  );
}
