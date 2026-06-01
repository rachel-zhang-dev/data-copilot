/**
 * GET /api/conversations/{threadId}/messages — replay dialogue for a
 * saved thread. Phase 1.4 / ADR 0019.
 *
 * The handler is intentionally dumb: it doesn't parse the body, it
 * just streams the upstream JSON through. Server-only so
 * ``API_BASE_URL`` doesn't leak to the browser.
 */
import { NextRequest, NextResponse } from "next/server";

export async function GET(
  _req: NextRequest,
  context: { params: Promise<{ threadId: string }> },
): Promise<NextResponse> {
  const apiBase = process.env.API_BASE_URL ?? "http://localhost:8000";
  const { threadId } = await context.params;
  const upstream = await fetch(
    `${apiBase}/conversations/${encodeURIComponent(threadId)}/messages`,
    { cache: "no-store" },
  );
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
