"use client";

import type { Coverage } from "@/lib/types";

/**
 * Rendered when ``AskResponse.coverage.verdict === "refuse"`` — the
 * Phase 1.1 coverage gate decided the database can't honestly answer
 * the question (e.g. "conversion rate" against a sales-only schema).
 *
 * Visual style is intentionally neutral, not error-red, because this
 * isn't a failure — the agent is being deliberately honest. The
 * suggested-question chips let the user pivot to something answerable
 * without retyping.
 */
export function CoverageRefusal({
  coverage,
  onSuggestionClick,
}: {
  coverage: Coverage;
  onSuggestionClick?: (question: string) => void;
}) {
  const missing = coverage.missing_concepts ?? [];
  const bullets = coverage.bullets ?? [];
  const suggestions = coverage.suggested_questions ?? [];
  const headline = coverage.headline ?? coverage.reason ?? "";

  return (
    <section
      className="rounded-md border border-(--color-border) bg-white p-3 text-sm"
      aria-label="The agent declined to answer"
      data-testid="coverage-refusal"
    >
      <div className="mb-2 flex items-center gap-2 text-(--color-muted)">
        <span
          aria-hidden="true"
          className="rounded-sm border border-(--color-border) px-1.5 py-0.5 text-xs font-medium"
        >
          Couldn&rsquo;t answer
        </span>
        <span className="text-xs">
          The schema doesn&rsquo;t cover this question.
        </span>
      </div>

      {headline && (
        <p className="mb-2 text-base text-(--color-fg)">{headline}</p>
      )}

      {missing.length > 0 && (
        <div className="mb-2">
          <span className="text-xs uppercase tracking-wide text-(--color-muted)">
            Missing
          </span>
          <ul className="mt-1 flex flex-wrap gap-1">
            {missing.map((concept) => (
              <li
                key={concept}
                className="rounded-sm border border-(--color-border) bg-(--color-bg) px-2 py-0.5 text-xs"
              >
                {concept}
              </li>
            ))}
          </ul>
        </div>
      )}

      {bullets.length > 0 && (
        <ul className="mb-2 list-disc pl-5 text-sm text-(--color-fg)">
          {bullets.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}

      {suggestions.length > 0 && (
        <div>
          <span className="text-xs uppercase tracking-wide text-(--color-muted)">
            Try instead
          </span>
          <div className="mt-1 flex flex-wrap gap-2">
            {suggestions.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => onSuggestionClick?.(q)}
                disabled={!onSuggestionClick}
                className="rounded-full border border-(--color-border) bg-white px-3 py-1 text-xs hover:bg-(--color-bg) disabled:cursor-not-allowed disabled:opacity-50"
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
