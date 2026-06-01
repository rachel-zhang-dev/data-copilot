import type { Semantic } from "@/lib/types";

/**
 * Phase 3.1 / ADR 0023 — "computed via semantic layer" pill.
 *
 * Renders nothing on the fallback path or when ``semantic`` is null
 * (the common case during the soft launch — most questions still
 * route through the LLM text-to-SQL pipeline because the semantic
 * model only covers ~6 metrics).
 *
 * On the semantic path the pill shows the compiled spec at a glance:
 *
 *   ⚖ revenue · by country · 1997
 *
 * which tells a careful reader "this number came from a deterministic
 * compile of a pre-approved metric definition, not from the LLM
 * guessing SQL". Same UX shape as the chat-side ``CriticBadge`` but
 * smaller and informational rather than cautionary.
 */
export function SemanticPill({ semantic }: { semantic: Semantic | null }) {
  if (!semantic || semantic.path !== "semantic_layer" || !semantic.spec) {
    return null;
  }

  const { metric, dimensions, time_range, filters } = semantic.spec;
  const parts: string[] = [metric];
  if (dimensions.length > 0) {
    parts.push("by " + dimensions.join(" + "));
  }
  if (time_range?.year) {
    parts.push(String(time_range.year));
  }
  for (const f of filters) {
    const value = Array.isArray(f.value) ? f.value.join("/") : String(f.value);
    parts.push(`${f.dimension}${f.op === "in" ? " ∈ " : "="}${value}`);
  }

  return (
    <div
      className={
        "inline-flex items-center gap-1.5 self-start rounded-full border " +
        "border-(--color-border) bg-(--color-bg) px-2.5 py-0.5 text-xs " +
        "text-(--color-muted)"
      }
      role="note"
      title={
        "Compiled deterministically from data/semantic.yml. " +
        "The LLM picked the metric and dimensions; the SQL was generated " +
        "by the semantic-layer compiler, not written by the model."
      }
      data-testid="semantic-pill"
      data-metric={metric}
    >
      <span aria-hidden="true">⚖</span>
      <span className="font-medium">{parts.join(" · ")}</span>
    </div>
  );
}
