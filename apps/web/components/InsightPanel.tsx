import type { Insight } from "@/lib/types";

/**
 * Renders the structured ``Insight`` envelope produced by
 * ``summarize_result_node``. Falls through to ``null`` when there's
 * nothing to show — the parent renders the legacy ``answer`` string
 * elsewhere so we never produce a blank box.
 */
export function InsightPanel({ insight }: { insight: Insight | null }) {
  if (!insight) return null;
  const hasBullets = insight.bullets && insight.bullets.length > 0;
  const hasHighlights =
    insight.metric_highlights && insight.metric_highlights.length > 0;
  if (!hasBullets && !hasHighlights) return null;

  return (
    <section
      className="rounded-md border border-(--color-border) bg-white p-3 text-sm shadow-xs"
      aria-label="Insight"
    >
      {hasHighlights && (
        <div className="mb-2 flex flex-wrap gap-3">
          {insight.metric_highlights.map((m, i) => (
            <div
              key={`${m.label}-${i}`}
              className="rounded-md border border-(--color-border) px-2 py-1"
            >
              <div className="text-xs text-(--color-muted)">{m.label}</div>
              <div className="text-base font-semibold">
                {formatMetric(m.value, m.format)}
              </div>
            </div>
          ))}
        </div>
      )}
      {hasBullets && (
        <ul className="list-disc space-y-1 pl-5 text-(--color-fg)">
          {insight.bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

function formatMetric(value: number, format?: string): string {
  switch (format) {
    case "currency":
      return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
      }).format(value);
    case "percent":
      return `${(value * 100).toFixed(1)}%`;
    case "integer":
      return Math.round(value).toLocaleString();
    default:
      return value.toLocaleString();
  }
}
