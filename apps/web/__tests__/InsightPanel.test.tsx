/**
 * Smoke tests for InsightPanel — covers the three meaningful states:
 * empty, bullets only, full envelope with metric highlights.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { InsightPanel } from "@/components/InsightPanel";

describe("InsightPanel", () => {
  it("renders nothing when insight is null", () => {
    const { container } = render(<InsightPanel insight={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when bullets and metrics are both empty", () => {
    const { container } = render(
      <InsightPanel
        insight={{ headline: "x", bullets: [], metric_highlights: [] }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders bullets when present", () => {
    render(
      <InsightPanel
        insight={{
          headline: "Customers by country",
          bullets: ["Germany leads with 11", "21 countries total"],
          metric_highlights: [],
        }}
      />,
    );
    expect(screen.getByText(/Germany leads with 11/)).toBeInTheDocument();
    expect(screen.getByText(/21 countries total/)).toBeInTheDocument();
  });

  it("formats metric highlights according to their format hint", () => {
    render(
      <InsightPanel
        insight={{
          headline: "x",
          bullets: [],
          metric_highlights: [
            { label: "Revenue", value: 1234, format: "currency" },
            { label: "Share", value: 0.42, format: "percent" },
            { label: "Customers", value: 91, format: "integer" },
          ],
        }}
      />,
    );
    expect(screen.getByText("$1,234.00")).toBeInTheDocument();
    expect(screen.getByText("42.0%")).toBeInTheDocument();
    expect(screen.getByText("91")).toBeInTheDocument();
  });
});
