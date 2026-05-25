"use client";

import type { ChatTurnViewModel } from "@/lib/types";
import { AgentTrace } from "./AgentTrace";
import { ChartRenderer } from "./ChartRenderer";
import { CostPanel } from "./CostPanel";
import { CoverageRefusal } from "./CoverageRefusal";
import { InsightPanel } from "./InsightPanel";
import { PendingConfirmation } from "./PendingConfirmation";
import { SchemaTour } from "./SchemaTour";

/**
 * One turn in the chat history. Composes the smaller display
 * components and decides which surfaces to show based on whether
 * the turn finished, paused, errored, was refused by the Phase 1.1
 * coverage gate, or routed through the schema explorer.
 */
export function ChatTurn({
  turn,
  isStreaming,
  onResume,
  onSuggestionClick,
}: {
  turn: ChatTurnViewModel;
  isStreaming: boolean;
  onResume: (decision: "approve" | "reject") => void;
  onSuggestionClick?: (question: string) => void;
}) {
  const result = turn.result;
  const coverage = result?.coverage ?? null;
  const isRefusal = coverage?.verdict === "refuse";
  const isExplore =
    coverage?.verdict === "explore" || result?.intent === "schema_explore";

  return (
    <article
      className="flex flex-col gap-3 border-b border-(--color-border) py-4"
      data-turn-id={turn.id}
    >
      {turn.question && (
        <div className="text-sm">
          <span className="font-semibold text-(--color-muted)">You:</span>{" "}
          {turn.question}
        </div>
      )}

      <AgentTrace phases={turn.phases} isStreaming={isStreaming} />

      {turn.pending && (
        <PendingConfirmation
          risk={turn.pending.pending_risk}
          onDecide={onResume}
          busy={isStreaming}
        />
      )}

      {turn.error && (
        <div className="rounded-md border border-(--color-error) bg-white p-3 text-sm text-(--color-error)">
          <strong>{turn.error.errorType}:</strong> {turn.error.detail}
        </div>
      )}

      {result && (
        <>
          {/* Phase 1.1 branches replace the usual answer + insight +
              chart stack with a dedicated card. We deliberately do NOT
              render the legacy ``result.answer`` paragraph for these
              branches — the card carries the same content with better
              affordances (chip rows, missing-concept badges). */}
          {isRefusal && coverage ? (
            <CoverageRefusal
              coverage={coverage}
              onSuggestionClick={onSuggestionClick}
            />
          ) : isExplore && coverage ? (
            <SchemaTour
              coverage={coverage}
              onSuggestionClick={onSuggestionClick}
            />
          ) : (
            <>
              {result.answer && (
                <p className="text-base text-(--color-fg)">{result.answer}</p>
              )}
              <InsightPanel insight={result.insight} />
              <ChartRenderer
                kind={result.chart_kind}
                spec={result.chart_spec}
                rows={result.rows}
              />
              {result.sql && (
                <details className="text-xs text-(--color-muted)">
                  <summary className="cursor-pointer select-none">
                    SQL · {result.attempts} attempt
                    {result.attempts === 1 ? "" : "s"}
                    {result.row_count !== null
                      ? ` · ${result.row_count} rows`
                      : ""}
                  </summary>
                  <pre className="mt-2 overflow-x-auto rounded-sm bg-(--color-bg) p-2">
                    <code>{result.sql}</code>
                  </pre>
                </details>
              )}
            </>
          )}
          <CostPanel cost={result.cost} />
        </>
      )}
    </article>
  );
}
