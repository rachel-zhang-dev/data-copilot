/**
 * Tests for the Phase 2.1.1 ``DashboardCard`` (one cell in the grid).
 *
 * Covers the snapshot-render contract (ADR 0020 §2 — render reads
 * only from snapshot columns; no SQL re-execution) and the
 * rename / delete affordances.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { DashboardCard } from "@/components/DashboardCard";
import type { DashboardItem } from "@/lib/api";

function _item(overrides: Partial<DashboardItem> = {}): DashboardItem {
  return {
    id: "i1",
    dashboard_id: "d1",
    source_thread_id: "t1",
    source_turn_index: 1,
    title: "Germany customer count",
    sql: "SELECT count(*) FROM customers WHERE country='Germany'",
    answer: "11 customers based in Germany.",
    chart_kind: "kpi",
    chart_spec: null,
    rows: [{ count: 11 }],
    row_count: 1,
    insight: {
      headline: "11 customers",
      bullets: ["Top country is USA"],
      metric_highlights: [
        { label: "Germany customers", value: 11, format: "integer" },
      ],
    },
    position_x: 0,
    position_y: 0,
    width: 4,
    height: 3,
    created_at: "2026-06-01",
    ...overrides,
  };
}

describe("DashboardCard", () => {
  it("renders title, insight bullets, metric highlights, and SQL details", () => {
    render(
      <DashboardCard item={_item()} onRename={vi.fn()} onDelete={vi.fn()} />,
    );
    expect(screen.getByText("Germany customer count")).toBeInTheDocument();
    expect(screen.getByText("Top country is USA")).toBeInTheDocument();
    expect(screen.getByText("Germany customers")).toBeInTheDocument();
    expect(screen.getByText(/SELECT count\(\*\)/)).toBeInTheDocument();
  });

  it("double-click on title enters edit mode; Enter fires onRename with the trimmed value", () => {
    const onRename = vi.fn();
    render(
      <DashboardCard item={_item()} onRename={onRename} onDelete={vi.fn()} />,
    );

    fireEvent.doubleClick(screen.getByText("Germany customer count"));
    const input = screen.getByLabelText("Edit card title") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  Renamed card  " } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onRename).toHaveBeenCalledWith("Renamed card");
  });

  it("Escape cancels rename without firing onRename", () => {
    const onRename = vi.fn();
    render(
      <DashboardCard item={_item()} onRename={onRename} onDelete={vi.fn()} />,
    );
    fireEvent.doubleClick(screen.getByText("Germany customer count"));
    const input = screen.getByLabelText("Edit card title");
    fireEvent.change(input, { target: { value: "abandoned" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onRename).not.toHaveBeenCalled();
  });

  it("delete button fires onDelete", () => {
    const onDelete = vi.fn();
    render(
      <DashboardCard item={_item()} onRename={vi.fn()} onDelete={onDelete} />,
    );
    fireEvent.click(screen.getByLabelText("Remove card"));
    expect(onDelete).toHaveBeenCalled();
  });

  it("falls back to the plain answer paragraph when there's no chart or insight (partial snapshot from replay)", () => {
    render(
      <DashboardCard
        item={_item({
          chart_kind: null,
          chart_spec: null,
          rows: null,
          insight: null,
          answer: "There are 11 customers in Germany.",
        })}
        onRename={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(
      screen.getByText("There are 11 customers in Germany."),
    ).toBeInTheDocument();
  });
});
