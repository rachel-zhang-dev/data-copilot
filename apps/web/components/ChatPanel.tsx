"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { postAsk, streamAsk } from "@/lib/api";
import type { ChatTurnViewModel, PhaseEvent, StreamEvent } from "@/lib/types";
import { ChatInput } from "./ChatInput";
import { ChatTurn } from "./ChatTurn";

/**
 * The single Client Component that owns the chat-page state. Everything
 * else on the page is server-rendered.
 *
 * State lives in two refs / hooks:
 * - ``turns``           — the rolling list of completed + in-flight turns.
 * - ``conversationId``  — the shared LangGraph thread id; the server
 *                          assigns one on the first response, we reuse it.
 *
 * No Zustand, no Redux. The state machine is small enough that
 * ``useState`` + careful ``useCallback`` keeps it readable.
 */
export function ChatPanel() {
  const [turns, setTurns] = useState<ChatTurnViewModel[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<(() => void) | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to the bottom whenever a new turn or phase lands.
  useEffect(() => {
    scrollerRef.current?.scrollTo({ top: scrollerRef.current.scrollHeight });
  }, [turns]);

  // Clean up an in-flight stream on unmount so the abort propagates
  // through the route handler.
  useEffect(() => () => abortRef.current?.(), []);

  const handleEvent = useCallback(
    (turnId: string, event: StreamEvent) => {
      setTurns((prev) => {
        const idx = prev.findIndex((t) => t.id === turnId);
        if (idx < 0) return prev;
        const next = prev.slice();
        const existing = next[idx];
        if (event.type === "phase") {
          next[idx] = { ...existing, phases: [...existing.phases, event] };
        } else if (event.type === "done") {
          next[idx] = { ...existing, result: event.response };
        } else if (event.type === "pending_confirmation") {
          next[idx] = { ...existing, pending: event };
        } else if (event.type === "error") {
          next[idx] = { ...existing, error: event };
        }
        return next;
      });

      if (event.type === "done") {
        if (event.response.conversation_id) {
          setConversationId(event.response.conversation_id);
        }
        setIsStreaming(false);
      } else if (event.type === "pending_confirmation") {
        if (event.conversation_id) setConversationId(event.conversation_id);
        setIsStreaming(false);
      } else if (event.type === "error") {
        setIsStreaming(false);
      }
    },
    [],
  );

  const startTurn = useCallback(
    (question: string) => {
      const turnId = `t-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setTurns((prev) => [
        ...prev,
        {
          id: turnId,
          question,
          phases: [] as PhaseEvent[],
          result: null,
          pending: null,
          error: null,
        },
      ]);
      setIsStreaming(true);
      abortRef.current = streamAsk(
        { question, conversation_id: conversationId ?? undefined },
        (event) => handleEvent(turnId, event),
      );
    },
    [conversationId, handleEvent],
  );

  const resumeTurn = useCallback(
    async (turnId: string, decision: "approve" | "reject") => {
      if (!conversationId) return;
      setIsStreaming(true);
      // Clear the pending marker on the turn we're resuming so the
      // approve/reject card hides while the request is in-flight.
      setTurns((prev) =>
        prev.map((t) => (t.id === turnId ? { ...t, pending: null } : t)),
      );
      try {
        const response = await postAsk({
          conversation_id: conversationId,
          resume: decision,
        });
        setTurns((prev) =>
          prev.map((t) => (t.id === turnId ? { ...t, result: response } : t)),
        );
      } catch (err) {
        setTurns((prev) =>
          prev.map((t) =>
            t.id === turnId
              ? {
                  ...t,
                  error: {
                    type: "error",
                    detail: (err as Error).message,
                    errorType: (err as Error).name,
                  },
                }
              : t,
          ),
        );
      } finally {
        setIsStreaming(false);
      }
    },
    [conversationId],
  );

  return (
    <div className="mx-auto flex h-svh max-w-3xl flex-col">
      <header className="flex items-center justify-between border-b border-(--color-border) bg-white px-4 py-3">
        <h1 className="text-lg font-semibold">Data Copilot</h1>
        {conversationId && (
          <span className="font-mono text-xs text-(--color-muted)">
            thread: {conversationId.slice(0, 8)}…
          </span>
        )}
      </header>

      <div
        ref={scrollerRef}
        className="chat-scroller flex-1 overflow-y-auto px-4"
      >
        {turns.length === 0 ? (
          <EmptyState />
        ) : (
          turns.map((turn) => (
            <ChatTurn
              key={turn.id}
              turn={turn}
              isStreaming={isStreaming && turn === turns[turns.length - 1]}
              onResume={(d) => void resumeTurn(turn.id, d)}
            />
          ))
        )}
      </div>

      <ChatInput onSubmit={startTurn} disabled={isStreaming} />
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center gap-2 py-12 text-center text-(--color-muted)">
      <p className="text-base">Ask the agent a question to get started.</p>
      <ul className="space-y-1 text-sm">
        <li>“How many customers are there?”</li>
        <li>“Count customers grouped by country.”</li>
        <li>“Which 5 products have the highest total revenue?”</li>
      </ul>
    </div>
  );
}
