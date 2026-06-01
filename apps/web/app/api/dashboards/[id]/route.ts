/**
 * GET    /api/dashboards/{id} — one dashboard + every card on it.
 * PATCH  /api/dashboards/{id} — edit title / description.
 * DELETE /api/dashboards/{id} — cascade-delete dashboard + items.
 *
 * Phase 2.1.1 / ADR 0020. Same dumb-proxy pattern as the rest.
 */
import { NextRequest, NextResponse } from "next/server";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

async function forward(
  upstream: Promise<Response>,
): Promise<NextResponse> {
  const r = await upstream;
  const text = await r.text();
  return new NextResponse(text, {
    status: r.status,
    headers: {
      "content-type": r.headers.get("content-type") ?? "application/json",
    },
  });
}

export async function GET(
  _req: NextRequest,
  context: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  const { id } = await context.params;
  return forward(
    fetch(`${API_BASE}/dashboards/${encodeURIComponent(id)}`, {
      cache: "no-store",
    }),
  );
}

export async function PATCH(
  req: NextRequest,
  context: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  const { id } = await context.params;
  const body = await req.text();
  return forward(
    fetch(`${API_BASE}/dashboards/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body,
    }),
  );
}

export async function DELETE(
  _req: NextRequest,
  context: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  const { id } = await context.params;
  return forward(
    fetch(`${API_BASE}/dashboards/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  );
}
