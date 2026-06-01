/**
 * Smoke tests for the Phase 1.4 ``PinButton``.
 *
 * The button calls the backend via ``saveConversation`` /
 * ``unsaveConversation`` (from ``lib/api``); we mock ``fetch`` at the
 * global level rather than poking into the module so the test
 * exercises the actual ``lib/api`` codepath.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { PinButton } from "@/components/PinButton";

const originalFetch = globalThis.fetch;

beforeEach(() => {
  globalThis.fetch = vi.fn() as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("PinButton", () => {
  it("renders nothing when there's no conversation yet", () => {
    const { container } = render(
      <PinButton threadId={null} pinned={false} onChange={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows ☆ Save when not pinned", () => {
    render(<PinButton threadId="t1" pinned={false} onChange={vi.fn()} />);
    expect(screen.getByText("Save")).toBeInTheDocument();
    expect(screen.getByText("☆")).toBeInTheDocument();
  });

  it("shows ★ Saved when pinned", () => {
    render(<PinButton threadId="t1" pinned={true} onChange={vi.fn()} />);
    expect(screen.getByText("Saved")).toBeInTheDocument();
    expect(screen.getByText("★")).toBeInTheDocument();
  });

  it("POSTs to /api/conversations/{id}/save when clicked unpinned", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          thread_id: "t1",
          title: "T",
          tags: [],
          notes: null,
          pinned_at: "x",
          updated_at: "x",
        }),
        { status: 200 },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    const onChange = vi.fn();

    render(<PinButton threadId="t1" pinned={false} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button"));

    await vi.waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/conversations/t1/save",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("DELETEs when clicked pinned", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("{}", { status: 200 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    const onChange = vi.fn();

    render(<PinButton threadId="t1" pinned={true} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button"));

    await vi.waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/conversations/t1/save",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
