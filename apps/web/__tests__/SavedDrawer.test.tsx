/**
 * Smoke tests for the Phase 1.4 ``SavedDrawer``:
 *
 * * Collapsed state renders icon-only stub with new-chat button.
 * * Expanded state renders the list with row preview text.
 * * Clicking a row fires ``onLoadConversation`` with the thread_id.
 * * Empty state renders a friendly placeholder.
 * * Inline title edit:  Enter saves, Escape cancels.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SavedDrawer } from "@/components/SavedDrawer";
import type { SavedConversation } from "@/lib/api";

const originalFetch = globalThis.fetch;
beforeEach(() => {
  globalThis.fetch = vi.fn() as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = originalFetch;
});

function _item(overrides: Partial<SavedConversation> = {}): SavedConversation {
  return {
    thread_id: "t-1",
    title: "Beverages investigation",
    tags: [],
    notes: null,
    pinned_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    last_question: "Why is Beverages declining?",
    last_answer: "Côte de Blaye dominated.",
    turn_count: 2,
    ...overrides,
  };
}

describe("SavedDrawer — expanded", () => {
  it("renders the new-chat button and list", () => {
    render(
      <SavedDrawer
        items={[_item()]}
        currentThreadId={null}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onLoadConversation={vi.fn()}
        onNewChat={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.getByText("+ New chat")).toBeInTheDocument();
    expect(screen.getByText("Beverages investigation")).toBeInTheDocument();
    expect(screen.getByText(/2 turns/)).toBeInTheDocument();
  });

  it("clicking a row fires onLoadConversation with its thread_id", () => {
    const onLoad = vi.fn();
    render(
      <SavedDrawer
        items={[_item({ thread_id: "abc-123", title: "T" })]}
        currentThreadId={null}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onLoadConversation={onLoad}
        onNewChat={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("T"));
    expect(onLoad).toHaveBeenCalledWith("abc-123");
  });

  it("renders an empty-state hint when no items", () => {
    render(
      <SavedDrawer
        items={[]}
        currentThreadId={null}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onLoadConversation={vi.fn()}
        onNewChat={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.getByText(/No saved conversations yet/)).toBeInTheDocument();
  });

  it("double-click on title puts row into edit mode; Enter saves", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("{}", { status: 200 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    const onRefresh = vi.fn();

    render(
      <SavedDrawer
        items={[_item({ thread_id: "abc", title: "old title" })]}
        currentThreadId="abc"
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onLoadConversation={vi.fn()}
        onNewChat={vi.fn()}
        onRefresh={onRefresh}
      />,
    );

    fireEvent.doubleClick(screen.getByText("old title"));
    const input = screen.getByLabelText("Edit title") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "new title" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await vi.waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/conversations/abc/save",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ title: "new title" }),
      }),
    );
  });

  it("Escape cancels inline edit without firing onRefresh", () => {
    const onRefresh = vi.fn();
    render(
      <SavedDrawer
        items={[_item({ title: "old" })]}
        currentThreadId={null}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onLoadConversation={vi.fn()}
        onNewChat={vi.fn()}
        onRefresh={onRefresh}
      />,
    );
    fireEvent.doubleClick(screen.getByText("old"));
    const input = screen.getByLabelText("Edit title");
    fireEvent.change(input, { target: { value: "abandoned" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.getByText("old")).toBeInTheDocument();
  });
});

describe("SavedDrawer — collapsed", () => {
  it("renders only the toggle + new-chat icons", () => {
    render(
      <SavedDrawer
        items={[_item()]}
        currentThreadId={null}
        collapsed={true}
        onToggleCollapsed={vi.fn()}
        onLoadConversation={vi.fn()}
        onNewChat={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("Expand sidebar")).toBeInTheDocument();
    expect(screen.getByLabelText("New chat")).toBeInTheDocument();
    expect(screen.queryByText("Beverages investigation")).toBeNull();
  });

  it("toggle fires onToggleCollapsed", () => {
    const onToggle = vi.fn();
    render(
      <SavedDrawer
        items={[]}
        currentThreadId={null}
        collapsed={true}
        onToggleCollapsed={onToggle}
        onLoadConversation={vi.fn()}
        onNewChat={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText("Expand sidebar"));
    expect(onToggle).toHaveBeenCalled();
  });
});
