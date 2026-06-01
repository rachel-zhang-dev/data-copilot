/**
 * Tests for the Phase 3.1 ``SemanticPill``.
 *
 * The pill is intentionally hidden on the fallback path and when the
 * semantic field is null — most questions today still route through
 * the LLM text-to-SQL pipeline, and a pill on every turn would be
 * noise. We verify show/hide rules + that the spec rendering string
 * carries metric, dimensions, time range, and filters.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SemanticPill } from "@/components/SemanticPill";

describe("SemanticPill", () => {
  it("renders nothing when semantic is null (fallback or pre-3.1 turns)", () => {
    const { container } = render(<SemanticPill semantic={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing on the fallback path even when an envelope is present", () => {
    const { container } = render(
      <SemanticPill
        semantic={{ path: "fallback", answerable: false, reason: "declined" }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows metric on the semantic path", () => {
    render(
      <SemanticPill
        semantic={{
          path: "semantic_layer",
          answerable: true,
          spec: {
            metric: "customer_count",
            dimensions: [],
            time_range: null,
            filters: [],
          },
        }}
      />,
    );
    const pill = screen.getByTestId("semantic-pill");
    expect(pill).toHaveAttribute("data-metric", "customer_count");
    expect(pill).toHaveTextContent("customer_count");
  });

  it("shows metric · dimensions · year when all three are present", () => {
    render(
      <SemanticPill
        semantic={{
          path: "semantic_layer",
          answerable: true,
          spec: {
            metric: "revenue",
            dimensions: ["country", "month"],
            time_range: { year: 1997 },
            filters: [],
          },
        }}
      />,
    );
    const pill = screen.getByTestId("semantic-pill");
    expect(pill).toHaveTextContent("revenue");
    expect(pill).toHaveTextContent("by country + month");
    expect(pill).toHaveTextContent("1997");
  });

  it("renders equality filters as dimension=value", () => {
    render(
      <SemanticPill
        semantic={{
          path: "semantic_layer",
          answerable: true,
          spec: {
            metric: "order_count",
            dimensions: [],
            time_range: null,
            filters: [{ dimension: "country", op: "=", value: "Germany" }],
          },
        }}
      />,
    );
    expect(screen.getByTestId("semantic-pill")).toHaveTextContent(
      "country=Germany",
    );
  });
});
