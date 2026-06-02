/**
 * POST /api/conversations/{threadId}/save  — pin (or update) bookmark.
 * DELETE /api/conversations/{threadId}/save — unpin.
 *
 * Phase 1.4 / ADR 0019. Same proxy pattern as ``app/api/ask/route.ts``.
 */
import { NextRequest, NextResponse } from "next/server";
import { getApiBase, serverHeaders } from "@/lib/server-fetch";

export async function POST(
  req: NextRequest,
  context: { params: Promise<{ threadId: string }> },
): Promise<NextResponse> {
  const { threadId } = await context.params;
  const body = await req.text();
  const upstream = await fetch(
    `${getApiBase()}/conversations/${encodeURIComponent(threadId)}/save`,
    {
      method: "POST",
      headers: serverHeaders({ "content-type": "application/json" }),
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
    `${getApiBase()}/conversations/${encodeURIComponent(threadId)}/save`,
    { method: "DELETE", headers: serverHeaders() },
  );
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
