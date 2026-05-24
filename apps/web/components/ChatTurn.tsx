"use client";

import type { ChatTurnViewModel } from "@/lib/types";
import { AgentTrace } from "./AgentTrace";
import { ChartRenderer } from "./ChartRenderer";
import { CostPanel } from "./CostPanel";
import { InsightPanel } from "./InsightPanel";
import { PendingConfirmation } from "./PendingConfirmation";

/**
 * One turn in the chat history. Composes the smaller display
 * components and decides which surfaces to show based on whether
 * the turn finished, paused, or errored.
 */
export function ChatTurn({
  turn,
  isStreaming,
  onResume,
}: {
  turn: ChatTurnViewModel;
  isStreaming: boolean;
  onResume: (decision: "approve" | "reject") => void;
}) {
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

      {turn.result && (
        <>
          {turn.result.answer && (
            <p className="text-base text-(--color-fg)">{turn.result.answer}</p>
          )}
          <InsightPanel insight={turn.result.insight} />
          <ChartRenderer
            kind={turn.result.chart_kind}
            spec={turn.result.chart_spec}
            rows={turn.result.rows}
          />
          {turn.result.sql && (
            <details className="text-xs text-(--color-muted)">
              <summary className="cursor-pointer select-none">
                SQL · {turn.result.attempts} attempt
                {turn.result.attempts === 1 ? "" : "s"}
                {turn.result.row_count !== null
                  ? ` · ${turn.result.row_count} rows`
                  : ""}
              </summary>
              <pre className="mt-2 overflow-x-auto rounded-sm bg-(--color-bg) p-2">
                <code>{turn.result.sql}</code>
              </pre>
            </details>
          )}
          <CostPanel cost={turn.result.cost} />
        </>
      )}
    </article>
  );
}
