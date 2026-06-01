/**
 * GET /api/conversations/saved — proxy to the FastAPI saved-list
 * endpoint. Phase 1.4 / ADR 0019.
 *
 * Same pattern as ``app/api/ask/route.ts``: server-only Route Handler
 * forwards JSON verbatim so the browser only ever talks to its own
 * origin (no CORS preflight) and ``API_BASE_URL`` stays out of the
 * client bundle.
 */
import { NextResponse } from "next/server";

export async function GET(): Promise<NextResponse> {
  const apiBase = process.env.API_BASE_URL ?? "http://localhost:8000";
  const upstream = await fetch(`${apiBase}/conversations/saved`, {
    cache: "no-store",
  });
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
