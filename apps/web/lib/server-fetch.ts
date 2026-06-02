/**
 * Server-only fetch helpers used by the Next.js Route Handlers
 * (Phase 3.2 / ADR 0024).
 *
 * Two responsibilities:
 *
 * 1. **API key forwarding** — When ``DEMO_API_KEY`` is set on the
 *    web container's environment, every server → API call attaches
 *    ``X-API-Key: <value>``. The browser never sees the key; the
 *    API rejects direct hits without it (401). This lets us put
 *    the deployed URL anywhere without worrying about scrapers
 *    burning the DeepSeek bill.
 *
 * 2. **API base URL resolution** — Pulled into one place so a
 *    rename of the env var doesn't ripple through every Route
 *    Handler.
 *
 * The functions live under ``lib/`` (not ``app/``) so they're
 * intuitively "shared utility" but they MUST only be called from
 * server contexts (Route Handlers, Server Components, ``page.tsx``).
 * Importing into a client component would leak ``DEMO_API_KEY``
 * into the browser bundle — Next.js's "use client" boundary would
 * normally catch this, but type-check or grep for
 * ``server-fetch`` in client components if you're suspicious.
 */

export function getApiBase(): string {
  return process.env.API_BASE_URL ?? "http://localhost:8000";
}

/**
 * Build a headers object with the API key attached (when configured).
 *
 * Pass any base headers the caller wants; the API key is added on
 * top. Idempotent — calling twice produces the same map.
 */
export function serverHeaders(
  base: Record<string, string> = {},
): Record<string, string> {
  const out: Record<string, string> = { ...base };
  const key = process.env.DEMO_API_KEY;
  if (key) {
    out["x-api-key"] = key;
  }
  return out;
}
