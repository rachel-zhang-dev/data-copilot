import type { CostBreakdown } from "@/lib/types";

/**
 * Renders the cumulative cost breakdown returned by the backend in
 * ``AskResponse.cost``. Collapsed by default behind a ``<details>``
 * so the chat panel stays clean.
 */
export function CostPanel({ cost }: { cost: CostBreakdown | null }) {
  if (!cost) return null;
  return (
    <details className="mt-2 text-xs text-(--color-muted)">
      <summary className="cursor-pointer select-none">
        Cost (conversation total):{" "}
        <span className="font-mono">${cost.est_usd.toFixed(6)}</span>
      </summary>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 font-mono">
        <dt>llm_calls</dt>
        <dd className="text-right">{cost.llm_calls}</dd>
        <dt>embedding_calls</dt>
        <dd className="text-right">{cost.embedding_calls}</dd>
        <dt>db_explain</dt>
        <dd className="text-right">{cost.db_explain_calls}</dd>
        <dt>db_select</dt>
        <dd className="text-right">{cost.db_select_calls}</dd>
        <dt>tokens_in</dt>
        <dd className="text-right">{cost.est_tokens_in.toLocaleString()}</dd>
        <dt>tokens_out</dt>
        <dd className="text-right">{cost.est_tokens_out.toLocaleString()}</dd>
      </dl>
    </details>
  );
}
