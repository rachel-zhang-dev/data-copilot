/**
 * Tests for the Phase 2.3 ``CriticBadge``.
 *
 * Three meaningful states: null/ok (hidden), suspicious (warn-coloured
 * with reason + concerns), wrong (error-coloured with the same content).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CriticBadge } from "@/components/CriticBadge";

describe("CriticBadge", () => {
  it("renders nothing when critic is null", () => {
    const { container } = render(<CriticBadge critic={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing on verdict 'ok'", () => {
    const { container } = render(
      <CriticBadge
        critic={{ verdict: "ok", reason: "looks good", concerns: [] }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the warn-styled badge with reason + concerns when verdict is 'suspicious'", () => {
    render(
      <CriticBadge
        critic={{
          verdict: "suspicious",
          reason: "JOIN may fan out duplicates",
          concerns: ["consider DISTINCT", "verify customer_id"],
        }}
      />,
    );
    const badge = screen.getByTestId("critic-badge");
    expect(badge).toHaveAttribute("data-verdict", "suspicious");
    expect(screen.getByText(/double-checking/i)).toBeInTheDocument();
    expect(
      screen.getByText("JOIN may fan out duplicates"),
    ).toBeInTheDocument();
    expect(screen.getByText("consider DISTINCT")).toBeInTheDocument();
    expect(screen.getByText("verify customer_id")).toBeInTheDocument();
  });

  it("renders the error-styled badge with stronger language when verdict is 'wrong'", () => {
    render(
      <CriticBadge
        critic={{
          verdict: "wrong",
          reason: "user asked 1997 but SQL filters 1998",
          concerns: ["WHERE year = 1998 should be 1997"],
        }}
      />,
    );
    const badge = screen.getByTestId("critic-badge");
    expect(badge).toHaveAttribute("data-verdict", "wrong");
    expect(screen.getByText(/wrong/i)).toBeInTheDocument();
    expect(
      screen.getByText("user asked 1997 but SQL filters 1998"),
    ).toBeInTheDocument();
  });

  it("omits the concerns list when empty", () => {
    render(
      <CriticBadge
        critic={{
          verdict: "suspicious",
          reason: "general suspicion",
          concerns: [],
        }}
      />,
    );
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
  });
});
