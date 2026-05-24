import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CostPanel } from "@/components/CostPanel";

describe("CostPanel", () => {
  it("returns null when cost is missing (chitchat / errored turn)", () => {
    const { container } = render(<CostPanel cost={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the cumulative USD figure in the collapsed summary", () => {
    render(
      <CostPanel
        cost={{
          llm_calls: 3,
          embedding_calls: 1,
          db_explain_calls: 1,
          db_select_calls: 1,
          est_tokens_in: 150,
          est_tokens_out: 75,
          est_usd: 0.000042,
        }}
      />,
    );
    expect(screen.getByText(/Cost \(conversation total\)/)).toBeInTheDocument();
    expect(screen.getByText("$0.000042")).toBeInTheDocument();
  });
});
