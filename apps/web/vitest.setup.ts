/**
 * Vitest setup: matchers + EventSource shim.
 *
 * jsdom does not ship an ``EventSource`` implementation. Tests that
 * exercise the SSE client install a stub by reassigning
 * ``globalThis.EventSource`` to their own controllable class; this
 * file just provides a do-nothing default so unrelated tests don't
 * blow up on a stray reference.
 */
import "@testing-library/jest-dom/vitest";

class _NoopEventSource {
  url: string;
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener(): void {
    /* no-op */
  }
  removeEventListener(): void {
    /* no-op */
  }
  close(): void {
    this.readyState = 2;
  }
}

if (typeof globalThis.EventSource === "undefined") {
  // @ts-expect-error — minimal stand-in for the EventSource API.
  globalThis.EventSource = _NoopEventSource;
}
