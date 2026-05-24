"use client";

import { useEffect, useRef } from "react";
import type { ChartKind } from "@/lib/types";

/**
 * Render the agent's chart output. The component branches on the
 * server-provided ``chart_kind`` so the front-end never has to inspect
 * the spec itself:
 *
 * * ``kpi``        → a stack of big-number tiles built from
 *                    ``rows[0]``.
 * * ``bar`` /
 *   ``line`` /
 *   ``grouped_bar`` → Vega-Lite spec rendered via the ``vega-embed``
 *                    lib (lazy-imported in a ``useEffect`` so the
 *                    chart bundle never lands in the initial RSC
 *                    payload).
 * * ``table``      → simple HTML table fallback so any shape stays
 *                    viewable.
 *
 * No external chart wrapper library — calling ``vegaEmbed`` directly
 * keeps the dep surface to vega/vega-lite/vega-embed which we already
 * need for Vega-Lite anyway.
 */
export function ChartRenderer({
  kind,
  spec,
  rows,
}: {
  kind: ChartKind | null;
  spec: Record<string, unknown> | null;
  rows: Array<Record<string, unknown>> | null;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!spec || !ref.current) return;
    let disposed = false;
    let view: { finalize: () => void } | null = null;
    void (async () => {
      // Lazy-import so the ~200 KB vega bundle stays out of the
      // initial route. The whole `if` block is the only place this
      // package is touched.
      const { default: vegaEmbed } = await import("vega-embed");
      if (disposed || !ref.current) return;
      try {
        const result = await vegaEmbed(ref.current, spec as unknown as object, {
          actions: false,
          renderer: "svg",
        });
        view = result.view as unknown as { finalize: () => void };
      } catch (err) {
        if (ref.current) {
          ref.current.textContent = `Chart render failed: ${(err as Error).message}`;
        }
      }
    })();
    return () => {
      disposed = true;
      view?.finalize();
    };
  }, [spec]);

  if (!kind) return null;

  if (kind === "kpi") {
    const row = rows?.[0];
    if (!row) return null;
    const numeric = Object.entries(row).filter(
      ([, v]) => typeof v === "number"
    ) as [string, number][];
    if (numeric.length === 0) return null;
    return (
      <div
        className="flex flex-wrap gap-3"
        aria-label="key performance indicators"
      >
        {numeric.map(([k, v]) => (
          <div
            key={k}
            className="min-w-32 rounded-md border border-(--color-border) bg-white p-3 text-center shadow-xs"
          >
            <div className="text-2xl font-semibold tabular-nums">
              {Number(v).toLocaleString()}
            </div>
            <div className="text-xs text-(--color-muted)">{k}</div>
          </div>
        ))}
      </div>
    );
  }

  if (kind === "table") {
    if (!rows || rows.length === 0) return null;
    const cols = Object.keys(rows[0]);
    return (
      <div className="max-h-72 overflow-auto rounded-md border border-(--color-border) bg-white">
        <table className="w-full text-left text-xs">
          <thead className="bg-(--color-bg) text-(--color-muted)">
            <tr>
              {cols.map((c) => (
                <th key={c} className="px-2 py-1 font-medium">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-t border-(--color-border)">
                {cols.map((c) => (
                  <td key={c} className="px-2 py-1 tabular-nums">
                    {String(r[c] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  // bar / line / grouped_bar — Vega-Lite via vega-embed
  return (
    <div className="rounded-md border border-(--color-border) bg-white p-2 shadow-xs">
      <div ref={ref} className="vega-mount" />
    </div>
  );
}
