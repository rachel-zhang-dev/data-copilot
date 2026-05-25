/**
 * Smoke tests for the Phase 1.1 coverage cards:
 *
 * * ``CoverageRefusal`` — renders missing concepts, optional bullets,
 *   and clickable suggested-question chips.
 * * ``SchemaTour`` — renders topic groups and sample-question chips.
 *
 * The chip click handlers wire back to ``startTurn`` in ``ChatPanel``;
 * we verify the click reaches the supplied callback with the right
 * question string.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { Coverage } from "@/lib/types";
import { CoverageRefusal } from "@/components/CoverageRefusal";
import { SchemaTour } from "@/components/SchemaTour";

describe("CoverageRefusal", () => {
  const refuseCoverage: Coverage = {
    verdict: "refuse",
    headline: "This database has no conversion funnel data.",
    reason: "no funnel",
    missing_concepts: ["conversion rate", "funnel"],
    bullets: ["Northwind tracks orders, not user sessions."],
    suggested_questions: [
      "Top customers by total order value?",
      "Monthly revenue trend in 1997?",
    ],
  };

  it("renders missing concepts and bullets", () => {
    render(<CoverageRefusal coverage={refuseCoverage} />);
    expect(screen.getByText(/conversion funnel data/)).toBeInTheDocument();
    expect(screen.getByText("conversion rate")).toBeInTheDocument();
    expect(screen.getByText("funnel")).toBeInTheDocument();
    expect(
      screen.getByText(/Northwind tracks orders/),
    ).toBeInTheDocument();
  });

  it("invokes onSuggestionClick when a chip is clicked", () => {
    const onClick = vi.fn();
    render(
      <CoverageRefusal
        coverage={refuseCoverage}
        onSuggestionClick={onClick}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Top customers by total order value?",
      }),
    );
    expect(onClick).toHaveBeenCalledWith("Top customers by total order value?");
  });

  it("disables chips when no click handler is supplied", () => {
    render(<CoverageRefusal coverage={refuseCoverage} />);
    const chip = screen.getByRole("button", {
      name: "Top customers by total order value?",
    });
    expect(chip).toBeDisabled();
  });

  it("renders without bullets / suggestions when omitted", () => {
    render(
      <CoverageRefusal
        coverage={{ verdict: "refuse", headline: "h" }}
      />,
    );
    expect(screen.getByText("h")).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});

describe("SchemaTour", () => {
  const exploreCoverage: Coverage = {
    verdict: "explore",
    headline: "Northwind has 13 tables across 3 topics.",
    topics: [
      {
        name: "Customers & Sales",
        summary: "Who buys what, and when.",
        tables: ["customers", "orders", "order_details"],
      },
      {
        name: "Products",
        summary: "What we sell.",
        tables: ["products", "categories", "suppliers"],
      },
    ],
    suggested_questions: [
      "How many orders shipped in 1997?",
      "Top 5 products by revenue?",
    ],
  };

  it("renders the topic groups with table chips", () => {
    render(<SchemaTour coverage={exploreCoverage} />);
    expect(screen.getByText(/Northwind has 13 tables/)).toBeInTheDocument();
    expect(screen.getByText("Customers & Sales")).toBeInTheDocument();
    expect(screen.getByText("Products")).toBeInTheDocument();
    expect(screen.getByText("orders")).toBeInTheDocument();
    expect(screen.getByText("categories")).toBeInTheDocument();
  });

  it("invokes onSuggestionClick when a sample-question chip is clicked", () => {
    const onClick = vi.fn();
    render(
      <SchemaTour
        coverage={exploreCoverage}
        onSuggestionClick={onClick}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Top 5 products by revenue?" }),
    );
    expect(onClick).toHaveBeenCalledWith("Top 5 products by revenue?");
  });

  it("renders without topics or suggestions when omitted", () => {
    render(
      <SchemaTour
        coverage={{ verdict: "explore", headline: "Empty tour." }}
      />,
    );
    expect(screen.getByText("Empty tour.")).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
