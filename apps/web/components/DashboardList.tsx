"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  createDashboard,
  deleteDashboard,
  listDashboards,
  updateDashboard,
  type Dashboard,
} from "@/lib/api";

/**
 * Index page for dashboards — Phase 2.1.1 / ADR 0020.
 *
 * Renders newest-touched-first tiles (one per dashboard), an inline
 * create form, double-click inline rename, and a hover-revealed
 * delete control. Same interaction palette as ``SavedDrawer`` so
 * users don't have to learn two patterns.
 */
export function DashboardList() {
  const [items, setItems] = useState<Dashboard[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");

  const refresh = async () => {
    try {
      const next = await listDashboards();
      setItems(next);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const handleCreate = async (e?: React.FormEvent) => {
    e?.preventDefault();
    const t = newTitle.trim();
    if (!t) return;
    try {
      await createDashboard({ title: t });
      setNewTitle("");
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm("Delete this dashboard and all its cards?")) return;
    try {
      await deleteDashboard(id);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const handleRename = async (id: string, title: string) => {
    try {
      await updateDashboard(id, { title });
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <div className="mb-6 flex items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Dashboards</h1>
          <p className="mt-1 text-sm text-(--color-muted)">
            Snapshots of chat answers on a 12-column grid. Add cards from any
            chat turn with the 📌 button.
          </p>
        </div>
        <Link
          href="/"
          className="rounded-md border border-(--color-border) bg-white px-3 py-1.5 text-sm hover:bg-(--color-bg)"
        >
          ← Back to chat
        </Link>
      </div>

      <form className="mb-6 flex gap-2" onSubmit={(e) => void handleCreate(e)}>
        <input
          type="text"
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
          placeholder="New dashboard title"
          aria-label="New dashboard title"
          className="flex-1 rounded-md border border-(--color-border) bg-white px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={!newTitle.trim()}
          className="rounded-md border border-(--color-border) bg-(--color-accent) px-4 py-2 text-sm font-medium text-(--color-accent-fg) hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Create
        </button>
      </form>

      {error && (
        <div className="mb-4 rounded-md border border-(--color-error) bg-white p-3 text-sm text-(--color-error)">
          {error}
        </div>
      )}

      {items === null ? (
        <div className="text-sm text-(--color-muted)">Loading…</div>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed border-(--color-border) bg-white p-8 text-center text-sm text-(--color-muted)">
          No dashboards yet. Create one above, then pin chat answers to it
          with the 📌 button.
        </div>
      ) : (
        <ul
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
          data-testid="dashboard-tile-list"
        >
          {items.map((d) => (
            <DashboardTile
              key={d.id}
              dashboard={d}
              onRename={(title) => void handleRename(d.id, title)}
              onDelete={() => void handleDelete(d.id)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function DashboardTile({
  dashboard,
  onRename,
  onDelete,
}: {
  dashboard: Dashboard;
  onRename: (title: string) => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(dashboard.title);

  const commit = () => {
    setEditing(false);
    const t = draft.trim();
    if (!t || t === dashboard.title) {
      setDraft(dashboard.title);
      return;
    }
    onRename(t);
  };

  return (
    <li
      className="group relative flex flex-col gap-1 rounded-md border border-(--color-border) bg-white p-4 hover:shadow-sm"
      data-testid="dashboard-tile"
      data-dashboard-id={dashboard.id}
    >
      {editing ? (
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => commit()}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            } else if (e.key === "Escape") {
              setEditing(false);
              setDraft(dashboard.title);
            }
          }}
          className="w-full rounded-sm border border-(--color-border) bg-white px-1 py-0.5 text-base font-medium"
          aria-label="Edit dashboard title"
        />
      ) : (
        <Link
          href={`/dashboards/${dashboard.id}`}
          onDoubleClick={(e) => {
            e.preventDefault();
            setEditing(true);
          }}
          className="text-base font-medium hover:underline"
          title={`${dashboard.title} — double-click to rename`}
        >
          {dashboard.title}
        </Link>
      )}
      <div className="text-xs text-(--color-muted)">
        {dashboard.item_count} card{dashboard.item_count === 1 ? "" : "s"}
        {dashboard.updated_at
          ? ` · updated ${new Date(dashboard.updated_at).toLocaleDateString()}`
          : ""}
      </div>
      <button
        type="button"
        onClick={onDelete}
        className="absolute top-2 right-2 rounded-sm px-2 py-0.5 text-xs text-(--color-muted) opacity-0 hover:bg-(--color-error) hover:text-white group-hover:opacity-100"
        aria-label="Delete dashboard"
        title="Delete dashboard"
      >
        ×
      </button>
    </li>
  );
}
