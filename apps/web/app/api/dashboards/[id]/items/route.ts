/**
 * POST /api/dashboards/{id}/items — extract one assistant turn into
 * a snapshot card. Body is the live ``AskResponse`` payload (chart
 * + insight + rows) that only ever exists in the browser — see
 * ADR 0020 §3 for why the FE owns this snapshot.
 *
 * Phase 2.1.1 / ADR 0020. Dumb passthrough.
 */
import { NextRequest, NextResponse } from "next/server";
import { getApiBase, serverHeaders } from "@/lib/server-fetch";

export async function POST(
  req: NextRequest,
  context: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  const { id } = await context.params;
  const body = await req.text();
  const upstream = await fetch(
    `${getApiBase()}/dashboards/${encodeURIComponent(id)}/items`,
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
