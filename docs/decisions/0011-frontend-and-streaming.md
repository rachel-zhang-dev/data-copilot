# ADR 0011: Next.js front-end and streaming agent responses

> Status: Accepted · Date: 2026-05 (Week 10) · Supersedes: none

## Context

Through Week 9 the agent is fully featured but only reachable from
`./scripts/dev.sh ask` or a raw `curl` call. The Vega-Lite spec and
the structured `insight` envelope shipped in Week 8 are wire-format
artefacts with no consumer; the `pending_risk` payload from Week 7
HITL exists but no UI to act on it; the `cost` breakdown from Week 9
has nowhere to be displayed.

Week 10 closes that gap with a thin Next.js front-end and a streaming
endpoint that surfaces the agent's progress in real time. The
front-end ships in the same monorepo at `apps/web/` (the placeholder
that has been there since Week 1).

## Decision

### Streaming via Server-Sent Events

We add a new endpoint `POST /ask/stream` that wraps
`graph.astream(stream_mode="updates")` and yields one SSE event per
node activation. Three event types:

```text
event: phase
data: {"node": "<node-name>", "diff": {<fields the node returned>}}

event: pending_confirmation
data: {"pending_risk": {sql, total_cost, threshold, reason}}

event: done
data: {<full AskResponse JSON, mirroring /ask>}
```

A fourth `event: error` is emitted on any unhandled exception so the
client can surface a polite failure without polling a separate
endpoint.

Resume after HITL pause uses the existing `/ask` endpoint with the
`resume="approve"|"reject"` field (already shipped in Week 7) —
*not* a streaming endpoint, because the user-facing latency of one
synchronous response is fine and avoids a second SSE state machine
on the client.

### Why SSE over WebSocket

WebSocket would let us interleave bidirectional messages, but the
agent flow is strictly *server → client* during a turn:

* Phases stream out.
* On pause, the server stops sending and waits for the client to
  call `/ask` (a separate HTTP request) with the resume field.
* The next turn starts a *new* SSE stream.

That is exactly the shape SSE was designed for. The trade-off:

| Concern                | SSE              | WebSocket       |
|------------------------|------------------|-----------------|
| Connection direction   | server → client  | bidirectional   |
| Reconnect-on-drop      | built into spec  | manual          |
| Browser EventSource    | one-liner        | own protocol    |
| Proxy compatibility    | regular HTTP     | needs `Upgrade` |
| Library footprint      | zero on FE       | `socket.io` etc.|

SSE adds **zero** new dependencies on either side; the Next.js
Route Handler streams `text/event-stream` directly and the browser's
built-in `EventSource` consumes it.

### Front-end framework: Next.js 15 (App Router) + Server Components

Three serious candidates:

* **Next.js 15 App Router + RSC.** Default-server-rendered; we drop
  to a single Client Component for the chat-input + streaming
  output panel. Production-friendly out of the box (the deploy
  target in Week 11 is Fly.io).
* **Vite + React SPA.** Smaller surface, faster cold start. But
  every page is a Client Component, every fetch is a network round-
  trip, and we lose Next.js Route Handlers — meaning the SSE proxy
  becomes its own express-ish thing.
* **Remix / TanStack Start.** Comparable to Next.js but ecosystem
  lift on this scale is irrelevant.

Next.js wins because:

1. We already promised it in the Week 1 README.
2. The single Route Handler doubles as the SSE proxy *and* enforces
   one CORS policy for the future deploy.
3. RSC keeps the static parts of the page server-rendered, which
   matters for portfolio screenshots and Lighthouse scores.

### CSS: Tailwind v4 + shadcn/ui

Tailwind v4 ships with zero runtime; shadcn provides accessible
primitives we paste in (so no new package version to track per
component). This combination is the same one Vercel uses for its
own product examples, so reviewers recognise the look.

### Type safety: OpenAPI → TypeScript at build time

The FastAPI app already publishes a complete OpenAPI document
(thanks to Pydantic v2). We add a `pnpm gen:types` script that
runs `openapi-typescript` against the local FastAPI's `/openapi.json`
and writes `apps/web/lib/api-types.ts`. The front-end's
`AskResponse` / `AskRequest` types come from there — no hand
duplication, no drift.

In CI (Week 11) we'll add a check that the generated file is up to
date so a backend schema change can't merge without regenerating
front-end types.

### Front-end state management: none

The chat UI's state is just the rolling list of turns plus the
in-flight stream's accumulated events. Three React `useState`
hooks cover everything; no Zustand / Redux / Jotai. The URL
carries `?conversation_id=...` so refreshing preserves the thread.

## API surface introduced in Week 10

| Route                        | Method | Body                                  | Response                       |
|------------------------------|--------|---------------------------------------|---------------------------------|
| `POST /ask/stream`           | POST   | `AskRequest` (same as `/ask`)         | `text/event-stream` (above)    |
| `POST /ask`                  | POST   | `AskRequest` (incl. optional resume)  | `AskResponse` (unchanged)      |
| Next.js: `/api/ask/stream`   | POST   | `AskRequest`                          | passes through to FastAPI SSE  |
| Next.js: `/api/ask`          | POST   | `AskRequest`                          | passes through to FastAPI JSON |
| Next.js: `/api/health`       | GET    | —                                     | `{status, version}`            |

The Next.js Route Handlers exist purely to keep all browser-facing
traffic on the Next origin — no CORS prompts, and a single place to
add auth headers when Week 13 (multi-tenancy) lands.

## Failure modes

| Scenario                                | Behaviour |
|-----------------------------------------|-----------|
| Backend disconnects mid-stream          | EventSource auto-reconnects; the front-end shows a "reconnecting…" toast and the conversation_id replays correctly because checkpoints survived |
| `event: error` from backend             | Front-end stops the spinner and surfaces the error string in the failed turn (no toast — easier to debug from screenshots) |
| Browser doesn't support EventSource     | We don't ship a polyfill. Modern browsers do; if a corporate IE-mode env hits us, fall back to `/ask` JSON (still works) |
| OpenAPI types out of date               | CI fails the front-end build; backend schema change forces a `pnpm gen:types` regenerate |
| `pending_confirmation` with no `cid`    | Impossible by construction — `/ask/stream` only allocates a thread ID before the first phase event, so a pause always carries one |

## Consequences

### Good

* Vega-Lite specs, structured insights, cost breakdowns and HITL
  pauses *all become visible*. Week 7-9 features stop being
  wire-format artefacts.
* The streaming UX changes the perceived latency from "wait 8 s"
  to "watch the agent think for 8 s" — same total but different
  story.
* Monorepo + auto-generated types means the front-end can never
  ship a schema mismatch.

### Bad / accepted trade-offs

* SSE is one-shot per turn; if a user wants to cancel mid-stream
  they have to close the EventSource on the client AND we have to
  decide what to do server-side (currently: nothing — the LangGraph
  run finishes, just nobody listens to the result). A proper
  cancellation story is Week 12 polish.
* The Route-Handler proxy adds one network hop. Worth it for the
  CORS + future-auth cleanliness.
* Next.js + shadcn means about 200 MB of `node_modules`. Gitignored
  but the dev-machine footprint is non-trivial.

## Future work

* **Cancel mid-stream.** Either let the client send a `DELETE
  /ask/stream/<thread_id>` to abort, or wire up an SSE control event
  the client interprets. Either way needs LangGraph's
  cancellation primitives, which arrived in 1.x but aren't tested
  here yet.
* **Streaming inside a single node.** The LLM-generated SQL
  currently arrives whole; with `llm.astream()` we could surface
  tokens as they're produced. Big perceived-latency win on
  `generate_sql` and `summarize_result`; skipped this week to keep
  scope tight.
* **Resume via SSE too.** Today resume is a normal `/ask` call;
  unifying the two surfaces under SSE would be cleaner but the user
  doesn't see any difference today.
* **Lighthouse / a11y pass.** Week 12 polish.
* **Real auth + session.** Belongs to ADR 0006 (multi-tenancy).
