"use client";

import type { DashboardItemSnapshot } from "@/lib/api";
import type { AskResponse, ChatTurnViewModel } from "@/lib/types";
import { AddToDashboardButton } from "./AddToDashboardButton";
import { AgentTrace } from "./AgentTrace";
import { ChartRenderer } from "./ChartRenderer";
import { CostPanel } from "./CostPanel";
import { CoverageRefusal } from "./CoverageRefusal";
import { CriticBadge } from "./CriticBadge";
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
              {/* Phase 2.3 — critic badge sits ABOVE the insight so users
                  see the "double-check this" warning before they read the
                  numbers it's warning about. Renders nothing on ok. */}
              <CriticBadge critic={result.critic} />
              <InsightPanel insight={result.insight} />
              <ChartRenderer
                kind={result.chart_kind}
                spec={result.chart_spec}
                rows={result.rows}
              />
              <div className="flex items-start justify-between gap-3">
                {result.sql ? (
                  <details className="min-w-0 flex-1 text-xs text-(--color-muted)">
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
                ) : (
                  <span />
                )}
                {isExtractable(result, turn.question) && (
                  <AddToDashboardButton
                    snapshot={buildSnapshot(result, turn.question)}
                  />
                )}
              </div>
            </>
          )}
          <CostPanel cost={result.cost} />
        </>
      )}
    </article>
  );
}

/**
 * A turn is "extractable" when the assistant turn produced a chart
 * or insight payload — i.e. it ran SQL and rendered output. Replayed
 * turns from the saved-conversation drawer set ``chart_kind=null``
 * deliberately (see ``ChatPanel.loadSavedConversation``); we hide
 * the button on those so users don't extract a partial snapshot
 * that's missing the chart spec / rows. ADR 0020 §"Risks".
 */
function isExtractable(
  result: AskResponse,
  question: string | null,
): boolean {
  if (!question) return false;
  if (result.error) return false;
  return result.chart_kind !== null;
}

/**
 * Project the live ``AskResponse`` into the snapshot shape the
 * backend stores. Title defaults to the user's question capped at
 * 80 chars (matches ``snapshot_from_replay_turn`` in ``dashboards.py``
 * so a re-extract via the future "refresh" path would derive the
 * same title).
 */
function buildSnapshot(
  result: AskResponse,
  question: string | null,
): DashboardItemSnapshot {
  const raw = (question ?? "").trim().replace(/\s+/g, " ");
  const title =
    raw.length === 0
      ? "Untitled card"
      : raw.length <= 80
        ? raw
        : raw.slice(0, 79).trimEnd() + "…";
  return {
    title,
    sql: result.sql,
    answer: result.answer || null,
    chart_kind: result.chart_kind,
    chart_spec: result.chart_spec,
    rows: result.rows,
    row_count: result.row_count,
    insight: result.insight as DashboardItemSnapshot["insight"],
    source_thread_id: result.conversation_id,
    source_turn_index: result.turn_index,
  };
}
