"use client";

import { useEffect, useState } from "react";
import {
  addDashboardItem,
  createDashboard,
  listDashboards,
  type Dashboard,
  type DashboardItemSnapshot,
} from "@/lib/api";

/**
 * Per-turn "📌 Add to dashboard" disclosure.
 *
 * Phase 2.1.1 / ADR 0020. The chat turn owns the live ``AskResponse``
 * (chart_spec + insight + rows) — that data only ever exists in
 * the browser, never in persisted dialogue (ADR 0020 §3). So the
 * picker POSTs the snapshot itself; the backend stores it verbatim.
 *
 * The picker is a native ``<details>`` disclosure: open / close
 * is a built-in browser affordance, no portal / z-index gymnastics,
 * keyboard-accessible by default.
 *
 * UX:
 *   - Closed     → "📌 Add to dashboard" pill button.
 *   - Open       → list of existing dashboards (lazy-loaded), plus
 *                  a single-input row to create a fresh one.
 *   - Post-add   → button replaced for 2.5 s by "✓ Added to <name>".
 *
 * Hidden entirely when no chart_kind is available (replayed turns
 * from saved-conversation reload — see ADR 0020 §"Risks"). The
 * parent (``ChatTurn``) does that gate; we accept any snapshot
 * here.
 */
export function AddToDashboardButton({
  snapshot,
}: {
  snapshot: DashboardItemSnapshot;
}) {
  const [open, setOpen] = useState(false);
  const [dashboards, setDashboards] = useState<Dashboard[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    listDashboards()
      .then(setDashboards)
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [open]);

  const finishAdd = (dashboardTitle: string) => {
    setSuccess(dashboardTitle);
    setOpen(false);
    setBusy(false);
    setTimeout(() => setSuccess(null), 2500);
  };

  const pickExisting = async (d: Dashboard) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await addDashboardItem(d.id, snapshot);
      finishAdd(d.title);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const createAndAdd = async () => {
    const title = newTitle.trim();
    if (!title || busy) return;
    setBusy(true);
    setError(null);
    try {
      const d = await createDashboard({ title });
      await addDashboardItem(d.id, snapshot);
      setNewTitle("");
      finishAdd(d.title);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  if (success) {
    return (
      <span
        className="text-xs text-(--color-muted)"
        data-testid="add-to-dashboard-success"
      >
        ✓ Added to <span className="font-medium">{success}</span>
      </span>
    );
  }

  return (
    <details
      className="relative inline-block"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary
        className={
          "flex cursor-pointer list-none items-center gap-1.5 rounded-md " +
          "border border-(--color-border) bg-white px-2.5 py-1 text-xs " +
          "text-(--color-muted) hover:bg-(--color-bg)"
        }
        data-testid="add-to-dashboard-button"
      >
        <span aria-hidden="true">📌</span>
        <span>Add to dashboard</span>
      </summary>
      <div
        className={
          "absolute right-0 z-20 mt-1 min-w-64 rounded-md border " +
          "border-(--color-border) bg-white p-2 shadow-md"
        }
        role="menu"
        data-testid="add-to-dashboard-menu"
      >
        {loading && (
          <div className="px-2 py-1 text-xs text-(--color-muted)">
            Loading dashboards…
          </div>
        )}
        {error && (
          <div className="px-2 py-1 text-xs text-(--color-error)">{error}</div>
        )}
        {dashboards && dashboards.length > 0 && (
          <ul className="mb-2 max-h-48 overflow-y-auto">
            {dashboards.map((d) => (
              <li key={d.id}>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void pickExisting(d)}
                  className="block w-full truncate rounded-sm px-2 py-1 text-left text-sm hover:bg-(--color-bg) disabled:opacity-50"
                  title={d.description ?? d.title}
                  data-testid="add-to-dashboard-option"
                >
                  {d.title}
                  <span className="ml-1 text-xs text-(--color-muted)">
                    ({d.item_count})
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="border-t border-(--color-border) pt-2">
          <div className="px-2 pb-1 text-xs uppercase tracking-wide text-(--color-muted)">
            New dashboard
          </div>
          <div className="flex gap-1 px-2 pb-1">
            <input
              type="text"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void createAndAdd();
                }
              }}
              placeholder="Dashboard title"
              aria-label="New dashboard title"
              className="flex-1 rounded-sm border border-(--color-border) bg-white px-2 py-1 text-sm"
              disabled={busy}
            />
            <button
              type="button"
              onClick={() => void createAndAdd()}
              disabled={!newTitle.trim() || busy}
              className="rounded-sm border border-(--color-border) bg-(--color-bg) px-2 py-1 text-xs hover:bg-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              Create + add
            </button>
          </div>
        </div>
      </div>
    </details>
  );
}
