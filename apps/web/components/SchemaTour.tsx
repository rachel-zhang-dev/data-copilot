"use client";

import type { Coverage } from "@/lib/types";

/**
 * Rendered when ``AskResponse.intent === "schema_explore"`` — the user
 * asked something like "what data do you have?" and the agent produced
 * a topic-grouped tour with starter questions.
 *
 * Visually similar to ``CoverageRefusal`` but conveys discovery, not
 * a soft refusal: each topic is a clickable / expandable group; the
 * sample questions at the bottom are real chips the user can click to
 * fire a new turn.
 */
export function SchemaTour({
  coverage,
  onSuggestionClick,
}: {
  coverage: Coverage;
  onSuggestionClick?: (question: string) => void;
}) {
  const topics = coverage.topics ?? [];
  const samples = coverage.suggested_questions ?? [];
  const headline = coverage.headline ?? "";

  return (
    <section
      className="rounded-md border border-(--color-border) bg-white p-3 text-sm"
      aria-label="Schema tour"
      data-testid="schema-tour"
    >
      <div className="mb-2 flex items-center gap-2 text-(--color-muted)">
        <span
          aria-hidden="true"
          className="rounded-sm border border-(--color-border) px-1.5 py-0.5 text-xs font-medium"
        >
          Database tour
        </span>
        <span className="text-xs">
          Browse the schema and pick a starter question.
        </span>
      </div>

      {headline && (
        <p className="mb-3 text-base text-(--color-fg)">{headline}</p>
      )}

      {topics.length > 0 && (
        <ul className="mb-3 flex flex-col gap-2">
          {topics.map((topic) => (
            <li
              key={topic.name}
              className="rounded-sm border border-(--color-border) bg-(--color-bg) p-2"
            >
              <div className="text-sm font-semibold text-(--color-fg)">
                {topic.name}
              </div>
              {topic.summary && (
                <p className="text-xs text-(--color-muted)">{topic.summary}</p>
              )}
              {topic.tables.length > 0 && (
                <ul className="mt-1 flex flex-wrap gap-1">
                  {topic.tables.map((t) => (
                    <li
                      key={t}
                      className="rounded-sm border border-(--color-border) bg-white px-2 py-0.5 font-mono text-xs"
                    >
                      {t}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}

      {samples.length > 0 && (
        <div>
          <span className="text-xs uppercase tracking-wide text-(--color-muted)">
            Try asking
          </span>
          <div className="mt-1 flex flex-wrap gap-2">
            {samples.map((q) => (
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
