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
