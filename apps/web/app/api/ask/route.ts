import { NextRequest, NextResponse } from "next/server";

/**
 * JSON proxy to FastAPI's ``POST /ask``. Used for two cases:
 *
 * * Resume after a HITL pause — ``{conversation_id, resume:
 *   "approve"|"reject"}``.
 * * Fallback when the browser can't stream (no ``ReadableStream``).
 *
 * Reasons for the proxy rather than a direct browser → FastAPI call:
 *
 * * Single origin in the browser — no CORS preflight needed.
 * * One place to inject auth headers when Week 13 adds tenancy.
 * * Hides ``API_BASE_URL`` from the client bundle.
 */
export async function POST(req: NextRequest): Promise<NextResponse> {
  const apiBase = process.env.API_BASE_URL ?? "http://localhost:8000";
  const body = await req.text();

  const upstream = await fetch(`${apiBase}/ask`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });

  // Pass through whatever FastAPI returned — including 400/422 from
  // Pydantic validators — so the client can render the same error
  // message that ``curl`` would see.
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
  });
}
