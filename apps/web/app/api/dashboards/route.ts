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
import { getApiBase, serverHeaders } from "@/lib/server-fetch";

export async function GET(): Promise<NextResponse> {
  const upstream = await fetch(`${getApiBase()}/dashboards`, {
    cache: "no-store",
    headers: serverHeaders(),
  });
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
  const upstream = await fetch(`${getApiBase()}/dashboards`, {
    method: "POST",
    headers: serverHeaders({ "content-type": "application/json" }),
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
