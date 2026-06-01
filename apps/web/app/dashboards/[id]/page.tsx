import "react-grid-layout/css/styles.css";
import { DashboardDetailView } from "@/components/DashboardDetailView";

/**
 * `/dashboards/[id]` — single dashboard with its react-grid-layout
 * grid of cards.
 *
 * Phase 2.1.1 / ADR 0020. Server shell awaits the route params
 * (Next.js 15 `params` is a Promise) and hands the id to the
 * Client Component that owns the interactive grid.
 */
export default async function DashboardDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <main className="min-h-svh bg-(--color-bg)">
      <DashboardDetailView dashboardId={id} />
    </main>
  );
}
