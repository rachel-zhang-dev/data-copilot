/**
 * GET  /api/dashboards — list dashboards newest-touched-first.
 * POST /api/dashboards — create a new (empty) dashboard.
 *
 * Phase 2.1.1 / ADR 0020. Same passthrough-proxy pattern as
 * ``app/api/conversations/...``: the Route Handler keeps
 * ``API_BASE_URL`` out of the client bundle and gives the browser a
 * single origin so no CORS preflight is needed.
 */
import { NextRequest, NextResponse } from "next/server";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function GET(): Promise<NextResponse> {
  const upstream = await fetch(`${API_BASE}/dashboards`, { cache: "no-store" });
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}

export async function POST(req: NextRequest): Promise<NextResponse> {
  const body = await req.text();
  const upstream = await fetch(`${API_BASE}/dashboards`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
  });
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
