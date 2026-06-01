/**
 * Tests for the Phase 2.1.1 ``DashboardList`` index page.
 *
 * Covers the round-trip create + delete flow and the empty state.
 * We use the same fetch-mocking pattern as ``SavedDrawer.test.tsx``
 * so the test exercises the real ``lib/api`` helpers.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DashboardList } from "@/components/DashboardList";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  globalThis.fetch = vi.fn() as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("DashboardList", () => {
  it("renders the empty-state hint when no dashboards exist", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ items: [] })) as unknown as typeof fetch;
    render(<DashboardList />);
    await waitFor(() =>
      expect(screen.getByText(/No dashboards yet/)).toBeInTheDocument(),
    );
  });

  it("lists existing dashboards with title + item count", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        items: [
          {
            id: "d1",
            title: "Q3 brief",
            description: null,
            created_at: "2026-06-01T00:00:00Z",
            updated_at: "2026-06-01T00:00:00Z",
            item_count: 4,
          },
        ],
      }),
    ) as unknown as typeof fetch;
    render(<DashboardList />);
    await waitFor(() =>
      expect(screen.getByText("Q3 brief")).toBeInTheDocument(),
    );
    expect(screen.getByText(/4 cards/)).toBeInTheDocument();
  });

  it("submitting the create form POSTs and refreshes the list", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(async () => jsonResponse({ items: [] }))
      .mockImplementationOnce(async () =>
        jsonResponse({
          id: "d-new",
          title: "Q4 brief",
          description: null,
          created_at: "2026-06-01T00:00:00Z",
          updated_at: "2026-06-01T00:00:00Z",
          item_count: 0,
        }),
      )
      .mockImplementationOnce(async () =>
        jsonResponse({
          items: [
            {
              id: "d-new",
              title: "Q4 brief",
              description: null,
              created_at: "2026-06-01T00:00:00Z",
              updated_at: "2026-06-01T00:00:00Z",
              item_count: 0,
            },
          ],
        }),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    render(<DashboardList />);

    // Wait for the initial list call to settle (empty)
    await waitFor(() =>
      expect(screen.getByText(/No dashboards yet/)).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("New dashboard title"), {
      target: { value: "Q4 brief" },
    });
    fireEvent.click(screen.getByText("Create"));

    await waitFor(() =>
      expect(screen.getByText("Q4 brief")).toBeInTheDocument(),
    );

    const createCall = fetchMock.mock.calls.find(
      ([u, init]) => u === "/api/dashboards" && init?.method === "POST",
    );
    expect(createCall).toBeDefined();
    expect(
      JSON.parse((createCall?.[1] as RequestInit).body as string),
    ).toEqual({ title: "Q4 brief" });
  });
});
