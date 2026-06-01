"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  listSavedConversations,
  loadConversation,
  streamAsk,
  type SavedConversation,
} from "@/lib/api";
import type { ChatTurnViewModel, PhaseEvent, StreamEvent } from "@/lib/types";
import { ChatInput } from "./ChatInput";
import { ChatTurn } from "./ChatTurn";
import { PinButton } from "./PinButton";
import { SavedDrawer } from "./SavedDrawer";

const SIDEBAR_COLLAPSED_KEY = "data-copilot:sidebar-collapsed";

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
  // Phase 1.4 — saved-conversation drawer state. The drawer's
  // collapsed flag is persisted to localStorage so it survives
  // reloads. ``savedItems`` is refetched on mount and whenever the
  // user pins / unpins / edits a title.
  const [savedItems, setSavedItems] = useState<SavedConversation[]>([]);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const abortRef = useRef<(() => void) | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to the bottom whenever a new turn or phase lands.
  useEffect(() => {
    scrollerRef.current?.scrollTo({ top: scrollerRef.current.scrollHeight });
  }, [turns]);

  // Clean up an in-flight stream on unmount so the abort propagates
  // through the route handler.
  useEffect(() => () => abortRef.current?.(), []);

  // Phase 1.4 — restore the sidebar collapsed state from localStorage
  // on first render. We deliberately do this in an effect (not in
  // useState's initializer) because Next.js renders this component on
  // the server first; touching ``window`` during the SSR pass throws.
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY);
      if (stored === "1") setSidebarCollapsed(true);
    } catch {
      // localStorage can be disabled (private windows, some embeds);
      // sidebar just defaults to expanded — not worth surfacing.
    }
  }, []);

  // Phase 1.4 — fetch the saved-list once on mount and expose a
  // ``refreshSaved`` callback for child components that mutate the
  // list (pin / unpin / rename). Failures are swallowed to a console
  // log because the drawer is non-critical chrome.
  const refreshSaved = useCallback(async () => {
    try {
      const items = await listSavedConversations();
      setSavedItems(items);
    } catch (err) {
      console.error("listSavedConversations failed:", err);
    }
  }, []);
  useEffect(() => {
    void refreshSaved();
  }, [refreshSaved]);

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

  // Phase 1.4 — load a saved conversation: pull its dialogue, project
  // each historical turn into a ``ChatTurnViewModel``, and adopt the
  // thread_id so the next ``startTurn`` continues this thread.
  // History turns are rendered with ``result.answer`` populated and
  // empty phases — they're inert reference, not live streams.
  const loadSavedConversation = useCallback(async (threadId: string) => {
    abortRef.current?.();
    setIsStreaming(false);
    try {
      const messages = await loadConversation(threadId);
      const replayed: ChatTurnViewModel[] = [];
      // The backend returns alternating user / assistant turns. Pair
      // each user turn with the assistant turn that follows.
      let pendingQuestion: string | null = null;
      for (const m of messages) {
        if (m.role === "user") {
          pendingQuestion = m.content;
        } else if (m.role === "assistant") {
          replayed.push({
            id: `replay-${Math.random().toString(36).slice(2, 9)}`,
            question: pendingQuestion,
            phases: [],
            result: {
              answer: m.content,
              conversation_id: threadId,
              turn_index: replayed.length + 1,
              sql: m.sql ?? null,
              rows: null,
              row_count: m.row_count ?? null,
              error: null,
              attempts: m.sql ? 1 : 0,
              attempts_history: null,
              status: "ok",
              pending_risk: null,
              insight: null,
              chart_kind: null,
              chart_spec: null,
              cost: null,
              intent: null,
              coverage: null,
              patterns: null,
            },
            pending: null,
            error: null,
          });
          pendingQuestion = null;
        }
      }
      setTurns(replayed);
      setConversationId(threadId);
    } catch (err) {
      console.error("loadConversation failed:", err);
    }
  }, []);

  // Phase 1.4 — "New chat" button: drop the current thread and start
  // fresh. The next ``startTurn`` will have ``conversation_id=null``
  // so the API allocates a new UUID.
  const startNewChat = useCallback(() => {
    abortRef.current?.();
    setIsStreaming(false);
    setTurns([]);
    setConversationId(null);
  }, []);

  const resumeTurn = useCallback(
    (turnId: string, decision: "approve" | "reject") => {
      if (!conversationId) return;
      // Clear the pending marker on the turn we're resuming so the
      // approve/reject card hides while the post-resume execution
      // streams in.
      setTurns((prev) =>
        prev.map((t) => (t.id === turnId ? { ...t, pending: null } : t)),
      );
      setIsStreaming(true);
      // Week 11 audit fix: route resume through ``/ask/stream`` so the
      // post-approve execution (execute_sql → summarize → visualize)
      // surfaces phase events in the UI exactly like the initial turn.
      // The previous non-streaming postAsk path produced a long pause
      // followed by an abrupt result — accurate but a poor demo.
      abortRef.current = streamAsk(
        { conversation_id: conversationId, resume: decision },
        (event) => handleEvent(turnId, event),
      );
    },
    [conversationId, handleEvent],
  );

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // localStorage unavailable — collapse state is in-memory only.
      }
      return next;
    });
  }, []);

  // Phase 1.4 — derived: is the current thread pinned? Lookup is O(N)
  // but N is the saved-list length (capped at 100 by the API), so it
  // beats threading a memoised map through every render.
  const isCurrentPinned = Boolean(
    conversationId && savedItems.some((s) => s.thread_id === conversationId),
  );

  return (
    <div className="flex h-svh">
      <SavedDrawer
        items={savedItems}
        currentThreadId={conversationId}
        collapsed={sidebarCollapsed}
        onToggleCollapsed={toggleSidebar}
        onLoadConversation={(id) => void loadSavedConversation(id)}
        onNewChat={startNewChat}
        onRefresh={refreshSaved}
      />

      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-(--color-border) bg-white px-4 py-3">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold">Data Copilot</h1>
            {conversationId && (
              <span className="font-mono text-xs text-(--color-muted)">
                thread: {conversationId.slice(0, 8)}…
              </span>
            )}
          </div>
          <PinButton
            threadId={conversationId}
            pinned={isCurrentPinned}
            onChange={refreshSaved}
          />
        </header>

        <div
          ref={scrollerRef}
          className="chat-scroller mx-auto w-full max-w-3xl flex-1 overflow-y-auto px-4"
        >
          {turns.length === 0 ? (
            <EmptyState />
          ) : (
            turns.map((turn) => (
              <ChatTurn
                key={turn.id}
                turn={turn}
                isStreaming={isStreaming && turn === turns[turns.length - 1]}
                onResume={(d) => resumeTurn(turn.id, d)}
                // Phase 1.1: refusal + schema-tour cards expose
                // clickable suggested-question chips. Wiring them to
                // ``startTurn`` keeps the same single source of truth
                // for streaming state. Disabled while a turn is in
                // flight so the user can't double-fire.
                onSuggestionClick={
                  isStreaming ? undefined : (q) => startTurn(q)
                }
              />
            ))
          )}
        </div>

        <div className="mx-auto w-full max-w-3xl">
          <ChatInput onSubmit={startTurn} disabled={isStreaming} />
        </div>
      </div>
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
