/**
 * POST /api/conversations/{threadId}/save  — pin (or update) bookmark.
 * DELETE /api/conversations/{threadId}/save — unpin.
 *
 * Phase 1.4 / ADR 0019. Same proxy pattern as ``app/api/ask/route.ts``.
 */
import { NextRequest, NextResponse } from "next/server";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function POST(
  req: NextRequest,
  context: { params: Promise<{ threadId: string }> },
): Promise<NextResponse> {
  const { threadId } = await context.params;
  const body = await req.text();
  const upstream = await fetch(
    `${API_BASE}/conversations/${encodeURIComponent(threadId)}/save`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    },
  );
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}

export async function DELETE(
  _req: NextRequest,
  context: { params: Promise<{ threadId: string }> },
): Promise<NextResponse> {
  const { threadId } = await context.params;
  const upstream = await fetch(
    `${API_BASE}/conversations/${encodeURIComponent(threadId)}/save`,
    { method: "DELETE" },
  );
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
