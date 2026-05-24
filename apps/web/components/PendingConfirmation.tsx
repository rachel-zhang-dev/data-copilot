"use client";

import type { PendingRisk } from "@/lib/types";

/**
 * Renders the HITL approve/reject card the user sees when the agent
 * pauses on an expensive query. ``onDecide`` resolves with the user's
 * choice and is responsible for calling ``/api/ask`` with the
 * appropriate ``resume`` payload.
 */
export function PendingConfirmation({
  risk,
  onDecide,
  busy,
}: {
  risk: PendingRisk;
  onDecide: (decision: "approve" | "reject") => void;
  busy: boolean;
}) {
  return (
    <section
      className="rounded-md border-2 border-(--color-warn) bg-white p-3 text-sm shadow-xs"
      aria-label="confirmation required"
    >
      <header className="mb-2 flex items-center gap-2">
        <span aria-hidden="true">⚠️</span>
        <span className="font-semibold">Confirmation required</span>
      </header>
      <p className="mb-2 text-(--color-fg)">{risk.reason}</p>
      <div className="mb-3 text-xs text-(--color-muted)">
        Estimated planner cost{" "}
        <span className="font-mono">{risk.total_cost.toFixed(1)}</span>{" "}
        (threshold{" "}
        <span className="font-mono">{risk.threshold.toFixed(1)}</span>)
      </div>
      <pre className="mb-3 overflow-x-auto rounded-sm bg-(--color-bg) p-2 text-xs">
        <code>{risk.sql}</code>
      </pre>
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => onDecide("approve")}
          disabled={busy}
          className="rounded-md bg-(--color-accent) px-3 py-1 text-sm font-medium text-(--color-accent-fg) hover:opacity-90 disabled:opacity-50"
        >
          Approve
        </button>
        <button
          type="button"
          onClick={() => onDecide("reject")}
          disabled={busy}
          className="rounded-md border border-(--color-border) px-3 py-1 text-sm hover:bg-(--color-bg) disabled:opacity-50"
        >
          Reject
        </button>
      </div>
    </section>
  );
}
