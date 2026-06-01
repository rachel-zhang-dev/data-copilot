/**
 * Client-side helpers for talking to the FastAPI backend.
 *
 * Two shapes:
 *
 * * ``streamAsk(...)`` — kicks off a streaming turn via the Next.js
 *   Route Handler at ``/api/ask/stream`` (which proxies to FastAPI).
 *   Returns a cleanup function the caller invokes to abort.
 * * ``postAsk(...)`` — non-streaming ``/api/ask`` for the resume call
 *   and as a fallback when ``EventSource`` is unavailable.
 *
 * Why a hand-rolled SSE parser instead of the browser's ``EventSource``
 * -------------------------------------------------------------------
 * ``EventSource`` only supports ``GET``. Our streaming endpoint
 * accepts a JSON body via ``POST`` (so the question travels in the
 * body, not the URL). The fetch + ``ReadableStream`` route lets us
 * keep the wire format consistent with what curl users see and
 * avoids smuggling the question through query parameters.
 */
import type {
  AskRequest,
  AskResponse,
  DoneEvent,
  ErrorEvent,
  PendingConfirmationEvent,
  PhaseEvent,
  StreamEvent,
} from "./types";

const SSE_SEP = "\n\n";

type StreamHandler = (event: StreamEvent) => void;

/**
 * Open a streaming turn. ``onEvent`` is invoked once per parsed SSE
 * event. The returned function aborts the underlying fetch — call it
 * from a ``useEffect`` cleanup so React unmounting doesn't leave the
 * connection half-open.
 */
export function streamAsk(req: AskRequest, onEvent: StreamHandler): () => void {
  const controller = new AbortController();

  void (async () => {
    let response: Response;
    try {
      response = await fetch("/api/ask/stream", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "text/event-stream" },
        body: JSON.stringify(req),
        signal: controller.signal,
      });
    } catch (err) {
      if ((err as DOMException)?.name === "AbortError") return;
      onEvent({
        type: "error",
        detail: (err as Error).message,
        errorType: (err as Error).name,
      });
      return;
    }

    if (!response.ok || !response.body) {
      onEvent({
        type: "error",
        detail: `HTTP ${response.status}`,
        errorType: "HttpError",
      });
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let sepAt = buffer.indexOf(SSE_SEP);
        while (sepAt !== -1) {
          const block = buffer.slice(0, sepAt);
          buffer = buffer.slice(sepAt + SSE_SEP.length);
          const parsed = parseSseBlock(block);
          if (parsed) onEvent(parsed);
          sepAt = buffer.indexOf(SSE_SEP);
        }
      }
      // Flush a trailing block if the server didn't end with \n\n.
      if (buffer.trim()) {
        const parsed = parseSseBlock(buffer);
        if (parsed) onEvent(parsed);
      }
    } catch (err) {
      if ((err as DOMException)?.name === "AbortError") return;
      onEvent({
        type: "error",
        detail: (err as Error).message,
        errorType: (err as Error).name,
      });
    }
  })();

  return () => controller.abort();
}

/**
 * Non-streaming JSON POST to ``/api/ask`` — used for resume and as a
 * fallback. Throws on non-2xx so callers can surface the message.
 */
export async function postAsk(req: AskRequest): Promise<AskResponse> {
  const r = await fetch("/api/ask", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!r.ok) {
    const detail = (await r.json().catch(() => null))?.detail ?? `HTTP ${r.status}`;
    throw new Error(detail);
  }
  return (await r.json()) as AskResponse;
}

// ---------------------------------------------------------------------------
// Saved conversations (Phase 1.4 / ADR 0019)
// ---------------------------------------------------------------------------

export interface SavedConversation {
  thread_id: string;
  title: string;
  tags: string[];
  notes: string | null;
  pinned_at: string;
  updated_at: string;
  last_question: string | null;
  last_answer: string | null;
  turn_count: number;
}

export interface ReplayMessage {
  role: "user" | "assistant";
  content: string;
  sql?: string;
  row_count?: number;
}

/** Pin / update a saved-conversation bookmark. ``title`` left null
 * means "auto-derive from first question" on a fresh pin, or
 * "leave unchanged" when the row already exists. */
export async function saveConversation(
  threadId: string,
  body: { title?: string; tags?: string[]; notes?: string } = {},
): Promise<SavedConversation> {
  const r = await fetch(`/api/conversations/${encodeURIComponent(threadId)}/save`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    throw new Error(`save failed: HTTP ${r.status}`);
  }
  return (await r.json()) as SavedConversation;
}

/** Drop the bookmark — underlying LangGraph history stays intact. */
export async function unsaveConversation(threadId: string): Promise<void> {
  const r = await fetch(`/api/conversations/${encodeURIComponent(threadId)}/save`, {
    method: "DELETE",
  });
  if (!r.ok && r.status !== 404) {
    throw new Error(`unsave failed: HTTP ${r.status}`);
  }
}

/** Newest-first list of pinned conversations + a tiny preview block. */
export async function listSavedConversations(): Promise<SavedConversation[]> {
  const r = await fetch(`/api/conversations/saved`);
  if (!r.ok) {
    throw new Error(`list failed: HTTP ${r.status}`);
  }
  const body = (await r.json()) as { items: SavedConversation[] };
  return body.items;
}

/** Pull the user-visible dialogue for a saved thread so the FE can
 * re-render history when the user clicks the saved row. */
export async function loadConversation(threadId: string): Promise<ReplayMessage[]> {
  const r = await fetch(
    `/api/conversations/${encodeURIComponent(threadId)}/messages`,
  );
  if (!r.ok) {
    throw new Error(`load failed: HTTP ${r.status}`);
  }
  const body = (await r.json()) as { thread_id: string; messages: ReplayMessage[] };
  return body.messages;
}

// ---------------------------------------------------------------------------
// Dashboards (Phase 2.1.1 / ADR 0020)
// ---------------------------------------------------------------------------

/** Header row from ``GET /dashboards``. ``item_count`` is joined in
 * by the API so the list page renders rich tiles in a single round
 * trip. */
export interface Dashboard {
  id: string;
  title: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  item_count: number;
}

/** A snapshot card sitting at a fixed grid position. ADR 0020 §2:
 * the FE renders ONLY from the snapshot columns — SQL is never
 * re-executed at render time. Phase 2.3.1 added ``critic`` so the
 * low-confidence badge survives extraction (ADR 0021). */
export interface DashboardItem {
  id: string;
  dashboard_id: string;
  source_thread_id: string | null;
  source_turn_index: number | null;
  title: string;
  sql: string | null;
  answer: string | null;
  chart_kind: string | null;
  chart_spec: Record<string, unknown> | null;
  rows: Array<Record<string, unknown>> | null;
  row_count: number | null;
  insight: {
    headline?: string;
    bullets?: string[];
    metric_highlights?: Array<{ label: string; value: number; format?: string }>;
  } | null;
  critic: {
    verdict: "ok" | "suspicious" | "wrong";
    reason: string;
    concerns: string[];
  } | null;
  position_x: number;
  position_y: number;
  width: number;
  height: number;
  created_at: string;
}

/** Detail-page payload — dashboard header + every card on it, in
 * insert order. */
export interface DashboardDetail extends Dashboard {
  items: DashboardItem[];
}

/** Body shape for ``POST /dashboards/{id}/items``. Mirrors backend
 * ``DashboardItemRequest`` in ``main.py`` — see ADR 0020 §3 for why
 * the FE owns the snapshot (chart_spec / insight / rows only ever
 * exist on the live ``AskResponse``, never in persisted dialogue). */
export interface DashboardItemSnapshot {
  title: string;
  sql: string | null;
  answer: string | null;
  chart_kind: string | null;
  chart_spec: Record<string, unknown> | null;
  rows: Array<Record<string, unknown>> | null;
  row_count: number | null;
  insight: DashboardItem["insight"];
  // Phase 2.3.1 — forward the critic verdict so a flagged turn
  // stays flagged after extraction (ADR 0021 §"Frontend surface").
  critic: DashboardItem["critic"];
  source_thread_id: string | null;
  source_turn_index: number | null;
  position_x?: number;
  position_y?: number;
  width?: number;
  height?: number;
}

/** Patch shape — backend (ADR 0020 §4) only accepts these four +
 * title; any snapshot field on the wire is silently dropped. */
export interface DashboardItemPatch {
  title?: string;
  position_x?: number;
  position_y?: number;
  width?: number;
  height?: number;
}

export async function listDashboards(): Promise<Dashboard[]> {
  const r = await fetch("/api/dashboards", { cache: "no-store" });
  if (!r.ok) throw new Error(`list dashboards failed: HTTP ${r.status}`);
  const body = (await r.json()) as { items: Dashboard[] };
  return body.items;
}

export async function createDashboard(body: {
  title: string;
  description?: string;
}): Promise<Dashboard> {
  const r = await fetch("/api/dashboards", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`create dashboard failed: HTTP ${r.status}`);
  return (await r.json()) as Dashboard;
}

export async function getDashboard(id: string): Promise<DashboardDetail> {
  const r = await fetch(`/api/dashboards/${encodeURIComponent(id)}`, {
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`get dashboard failed: HTTP ${r.status}`);
  return (await r.json()) as DashboardDetail;
}

export async function updateDashboard(
  id: string,
  body: { title?: string; description?: string },
): Promise<Dashboard> {
  const r = await fetch(`/api/dashboards/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`update dashboard failed: HTTP ${r.status}`);
  return (await r.json()) as Dashboard;
}

export async function deleteDashboard(id: string): Promise<void> {
  const r = await fetch(`/api/dashboards/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!r.ok && r.status !== 404) {
    throw new Error(`delete dashboard failed: HTTP ${r.status}`);
  }
}

export async function addDashboardItem(
  dashboardId: string,
  snapshot: DashboardItemSnapshot,
): Promise<DashboardItem> {
  const r = await fetch(
    `/api/dashboards/${encodeURIComponent(dashboardId)}/items`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(snapshot),
    },
  );
  if (!r.ok) throw new Error(`add item failed: HTTP ${r.status}`);
  return (await r.json()) as DashboardItem;
}

export async function updateDashboardItem(
  dashboardId: string,
  itemId: string,
  patch: DashboardItemPatch,
): Promise<DashboardItem> {
  const r = await fetch(
    `/api/dashboards/${encodeURIComponent(dashboardId)}/items/${encodeURIComponent(itemId)}`,
    {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    },
  );
  if (!r.ok) throw new Error(`update item failed: HTTP ${r.status}`);
  return (await r.json()) as DashboardItem;
}

export async function deleteDashboardItem(
  dashboardId: string,
  itemId: string,
): Promise<void> {
  const r = await fetch(
    `/api/dashboards/${encodeURIComponent(dashboardId)}/items/${encodeURIComponent(itemId)}`,
    { method: "DELETE" },
  );
  if (!r.ok && r.status !== 404) {
    throw new Error(`delete item failed: HTTP ${r.status}`);
  }
}

// ---------------------------------------------------------------------------
// SSE parsing — exported for tests
// ---------------------------------------------------------------------------

/**
 * Parse one ``event: ...\ndata: ...`` block into a tagged union event.
 *
 * Exported so the parser can be tested in isolation without standing
 * up a fake fetch. Returns ``null`` for blocks we don't recognise.
 */
export function parseSseBlock(block: string): StreamEvent | null {
  const lines = block.split("\n");
  let name = "";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith("event: ")) {
      name = line.slice("event: ".length).trim();
    } else if (line.startsWith("data: ")) {
      dataLines.push(line.slice("data: ".length));
    }
  }
  if (!name) return null;
  let data: Record<string, unknown> = {};
  if (dataLines.length > 0) {
    try {
      data = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
    } catch {
      // Malformed data — skip rather than crashing the consumer.
      return null;
    }
  }
  return toStreamEvent(name, data);
}

function toStreamEvent(name: string, data: Record<string, unknown>): StreamEvent | null {
  if (name === "phase") {
    return {
      type: "phase",
      node: String(data.node ?? ""),
      diff: (data.diff as Record<string, unknown>) ?? {},
      internal: Boolean(data.internal),
    } satisfies PhaseEvent;
  }
  if (name === "pending_confirmation") {
    return {
      type: "pending_confirmation",
      conversation_id: String(data.conversation_id ?? ""),
      pending_risk: data.pending_risk as PendingConfirmationEvent["pending_risk"],
    };
  }
  if (name === "done") {
    return {
      type: "done",
      response: data as unknown as DoneEvent["response"],
    };
  }
  if (name === "error") {
    return {
      type: "error",
      detail: String(data.detail ?? ""),
      errorType: String(data.type ?? "Error"),
    } satisfies ErrorEvent;
  }
  return null;
}
