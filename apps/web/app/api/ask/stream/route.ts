import { NextRequest } from "next/server";

/**
 * SSE proxy. Pipes the FastAPI ``/ask/stream`` body through Next.js'
 * Route Handler runtime without buffering.
 *
 * Two production-relevant details:
 *
 * * ``runtime = "nodejs"`` — the default Edge runtime supports
 *   streaming but its fetch implementation has historically buffered
 *   ``text/event-stream`` until the upstream connection closes; the
 *   Node runtime is the safe choice in 2026 Q1.
 * * ``Cache-Control: no-cache`` and ``X-Accel-Buffering: no`` so any
 *   reverse proxy in front of us (Nginx, Cloudflare, Fly.io's proxy)
 *   does not buffer the response.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest): Promise<Response> {
  const apiBase = process.env.API_BASE_URL ?? "http://localhost:8000";
  const body = await req.text();

  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase}/ask/stream`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
      // Disable Node's default response buffering for SSE.
      // @ts-expect-error — Node-only option ignored by other runtimes.
      duplex: "half",
    });
  } catch (err) {
    return errorResponse(503, `upstream unreachable: ${(err as Error).message}`);
  }

  if (!upstream.ok) {
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { "content-type": "application/json" },
    });
  }
  if (!upstream.body) {
    return errorResponse(502, "upstream returned no body");
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache",
      "x-accel-buffering": "no",
      connection: "keep-alive",
    },
  });
}

function errorResponse(status: number, detail: string): Response {
  // Encode the error as a single SSE ``error`` event so the client's
  // existing handler renders it the same way as backend-emitted
  // errors — no second code path required.
  const payload = `event: error\ndata: ${JSON.stringify({ detail, type: "ProxyError" })}\n\n`;
  return new Response(payload, {
    status,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache",
    },
  });
}
