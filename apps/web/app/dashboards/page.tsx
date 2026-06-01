import { DashboardList } from "@/components/DashboardList";

/**
 * `/dashboards` — index page listing every saved dashboard as a tile.
 *
 * Phase 2.1.1 / ADR 0020. The server shell is intentionally empty;
 * the whole interactive surface lives in the ``DashboardList``
 * Client Component (same RSC + one-client-island pattern as the
 * chat page).
 */
export default function DashboardsIndexPage() {
  return (
    <main className="min-h-svh bg-(--color-bg)">
      <DashboardList />
    </main>
  );
}
