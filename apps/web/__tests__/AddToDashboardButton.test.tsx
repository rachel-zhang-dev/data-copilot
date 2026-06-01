/**
 * Tests for the Phase 2.1.1 ``AddToDashboardButton`` disclosure.
 *
 * We mock ``globalThis.fetch`` so the component goes through its
 * normal ``lib/api`` calls — that exercises the request shape AND
 * the proxy routes the FE expects to hit. Same testing posture as
 * ``PinButton.test.tsx``.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AddToDashboardButton } from "@/components/AddToDashboardButton";
import type { DashboardItemSnapshot } from "@/lib/api";

const SNAPSHOT: DashboardItemSnapshot = {
  title: "How many customers in Germany?",
  sql: "SELECT count(*) FROM customers WHERE country='Germany' LIMIT 100",
  answer: "There are 11 customers based in Germany.",
  chart_kind: "kpi",
  chart_spec: null,
  rows: [{ count: 11 }],
  row_count: 1,
  insight: { headline: "11 customers", bullets: [], metric_highlights: [] },
  source_thread_id: "thr-1",
  source_turn_index: 1,
};

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  globalThis.fetch = vi.fn() as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

function makeFetchMock(handlers: Array<{
  match: (url: string, init?: RequestInit) => boolean;
  response: () => Response;
}>): typeof fetch {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    for (const h of handlers) {
      if (h.match(url, init)) return h.response();
    }
    throw new Error(`unexpected fetch: ${(init?.method ?? "GET")} ${url}`);
  }) as unknown as typeof fetch;
}

describe("AddToDashboardButton", () => {
  it("shows the trigger pill before being opened", () => {
    render(<AddToDashboardButton snapshot={SNAPSHOT} />);
    expect(screen.getByTestId("add-to-dashboard-button")).toBeInTheDocument();
    expect(screen.getByText(/Add to dashboard/)).toBeInTheDocument();
  });

  it("opens, fetches the dashboard list, and posts the snapshot when an existing one is picked", async () => {
    globalThis.fetch = makeFetchMock([
      {
        match: (u, init) => u === "/api/dashboards" && (init?.method ?? "GET") === "GET",
        response: () =>
          new Response(
            JSON.stringify({
              items: [
                {
                  id: "d1",
                  title: "Q3 sales brief",
                  description: null,
                  created_at: "2026-06-01",
                  updated_at: "2026-06-01",
                  item_count: 2,
                },
              ],
            }),
          ),
      },
      {
        match: (u, init) =>
          u === "/api/dashboards/d1/items" && init?.method === "POST",
        response: () =>
          new Response(
            JSON.stringify({
              id: "i1",
              dashboard_id: "d1",
              title: SNAPSHOT.title,
              sql: SNAPSHOT.sql,
              answer: SNAPSHOT.answer,
              chart_kind: SNAPSHOT.chart_kind,
              chart_spec: SNAPSHOT.chart_spec,
              rows: SNAPSHOT.rows,
              row_count: SNAPSHOT.row_count,
              insight: SNAPSHOT.insight,
              source_thread_id: SNAPSHOT.source_thread_id,
              source_turn_index: SNAPSHOT.source_turn_index,
              position_x: 0,
              position_y: 0,
              width: 4,
              height: 3,
              created_at: "2026-06-01",
            }),
          ),
      },
    ]);

    render(<AddToDashboardButton snapshot={SNAPSHOT} />);

    fireEvent.click(screen.getByTestId("add-to-dashboard-button"));

    const option = await screen.findByTestId("add-to-dashboard-option");
    expect(option).toHaveTextContent("Q3 sales brief");

    fireEvent.click(option);

    await waitFor(() =>
      expect(screen.getByTestId("add-to-dashboard-success")).toBeInTheDocument(),
    );

    const postCall = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
      .find(([url, init]) =>
        url === "/api/dashboards/d1/items" && init?.method === "POST",
      );
    expect(postCall).toBeDefined();
    const body = JSON.parse((postCall?.[1] as RequestInit).body as string);
    expect(body).toMatchObject({
      title: SNAPSHOT.title,
      sql: SNAPSHOT.sql,
      chart_kind: "kpi",
      source_thread_id: "thr-1",
      source_turn_index: 1,
    });
  });

  it("creates a new dashboard then immediately adds the card", async () => {
    globalThis.fetch = makeFetchMock([
      {
        match: (u, init) => u === "/api/dashboards" && (init?.method ?? "GET") === "GET",
        response: () => new Response(JSON.stringify({ items: [] })),
      },
      {
        match: (u, init) => u === "/api/dashboards" && init?.method === "POST",
        response: () =>
          new Response(
            JSON.stringify({
              id: "d-new",
              title: "Fresh dashboard",
              description: null,
              created_at: "2026-06-01",
              updated_at: "2026-06-01",
              item_count: 0,
            }),
          ),
      },
      {
        match: (u, init) =>
          u === "/api/dashboards/d-new/items" && init?.method === "POST",
        response: () =>
          new Response(JSON.stringify({ id: "i1", dashboard_id: "d-new" })),
      },
    ]);

    render(<AddToDashboardButton snapshot={SNAPSHOT} />);
    fireEvent.click(screen.getByTestId("add-to-dashboard-button"));

    // Wait for the "no dashboards yet, create form is the only path" UI
    const input = await screen.findByLabelText("New dashboard title");
    fireEvent.change(input, { target: { value: "Fresh dashboard" } });
    fireEvent.click(screen.getByText("Create + add"));

    await waitFor(() =>
      expect(screen.getByTestId("add-to-dashboard-success")).toBeInTheDocument(),
    );

    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    const postedToCreate = calls.find(
      ([url, init]) => url === "/api/dashboards" && init?.method === "POST",
    );
    const postedItem = calls.find(
      ([url, init]) =>
        url === "/api/dashboards/d-new/items" && init?.method === "POST",
    );
    expect(postedToCreate).toBeDefined();
    expect(postedItem).toBeDefined();
    expect(
      JSON.parse((postedToCreate?.[1] as RequestInit).body as string),
    ).toEqual({ title: "Fresh dashboard" });
  });

  it("surfaces a server error inside the menu without crashing", async () => {
    globalThis.fetch = makeFetchMock([
      {
        match: (u) => u === "/api/dashboards",
        response: () => new Response("internal err", { status: 500 }),
      },
    ]);

    render(<AddToDashboardButton snapshot={SNAPSHOT} />);
    fireEvent.click(screen.getByTestId("add-to-dashboard-button"));

    await waitFor(() =>
      expect(screen.getByText(/HTTP 500/)).toBeInTheDocument(),
    );
  });
});
