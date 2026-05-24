import { NextResponse } from "next/server";

/**
 * Cheap liveness probe that mirrors the backend's ``/health`` so the
 * front-end can be screwed into a load-balancer health check without
 * roundtripping through the API. Returns ``upstream`` ``"unknown"``
 * when the API is unreachable rather than 500-ing — the front-end
 * itself is alive even if the agent is down.
 */
export async function GET(): Promise<NextResponse> {
  const apiBase = process.env.API_BASE_URL ?? "http://localhost:8000";
  let upstream: "ok" | "down" | "unknown" = "unknown";
  let upstreamVersion: string | undefined;
  try {
    const r = await fetch(`${apiBase}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (r.ok) {
      const body = (await r.json()) as { status?: string; version?: string };
      upstream = body.status === "ok" ? "ok" : "down";
      upstreamVersion = body.version;
    } else {
      upstream = "down";
    }
  } catch {
    upstream = "unknown";
  }
  return NextResponse.json({
    status: "ok",
    upstream,
    upstreamVersion,
  });
}
