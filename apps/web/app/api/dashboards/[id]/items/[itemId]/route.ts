/**
 * PATCH  /api/dashboards/{id}/items/{itemId} — rename + reposition.
 * DELETE /api/dashboards/{id}/items/{itemId} — remove one card.
 *
 * Phase 2.1.1 / ADR 0020. Snapshot columns are NOT in the PATCH
 * surface (see ADR 0020 §4): the backend silently drops anything
 * outside title / position_x / position_y / width / height.
 */
import { NextRequest, NextResponse } from "next/server";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function PATCH(
  req: NextRequest,
  context: { params: Promise<{ id: string; itemId: string }> },
): Promise<NextResponse> {
  const { id, itemId } = await context.params;
  const body = await req.text();
  const upstream = await fetch(
    `${API_BASE}/dashboards/${encodeURIComponent(id)}/items/${encodeURIComponent(itemId)}`,
    {
      method: "PATCH",
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
  context: { params: Promise<{ id: string; itemId: string }> },
): Promise<NextResponse> {
  const { id, itemId } = await context.params;
  const upstream = await fetch(
    `${API_BASE}/dashboards/${encodeURIComponent(id)}/items/${encodeURIComponent(itemId)}`,
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
