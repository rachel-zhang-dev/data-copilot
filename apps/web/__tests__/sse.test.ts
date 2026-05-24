/**
 * Unit tests for the hand-rolled SSE parser.
 *
 * The parser is the only thing on the client that *has* to be correct
 * about the wire format — everything else degrades gracefully. We pin
 * each event-type and the most likely failure modes.
 */
import { describe, expect, it } from "vitest";
import { parseSseBlock } from "@/lib/api";

describe("parseSseBlock", () => {
  it("parses a phase event with its diff", () => {
    const block = [
      "event: phase",
      `data: ${JSON.stringify({ node: "generate_sql", diff: { sql: "SELECT 1" }, internal: false })}`,
    ].join("\n");
    const out = parseSseBlock(block);
    expect(out).toEqual({
      type: "phase",
      node: "generate_sql",
      diff: { sql: "SELECT 1" },
      internal: false,
    });
  });

  it("parses pending_confirmation including the risk payload", () => {
    const block = [
      "event: pending_confirmation",
      `data: ${JSON.stringify({
        conversation_id: "abc",
        pending_risk: {
          sql: "SELECT * FROM big",
          total_cost: 9000,
          threshold: 1000,
          reason: "too costly",
        },
      })}`,
    ].join("\n");
    const out = parseSseBlock(block);
    expect(out?.type).toBe("pending_confirmation");
    if (out?.type === "pending_confirmation") {
      expect(out.conversation_id).toBe("abc");
      expect(out.pending_risk.total_cost).toBe(9000);
    }
  });

  it("parses an error event with type and detail", () => {
    const block = [
      "event: error",
      `data: ${JSON.stringify({ detail: "kaboom", type: "RuntimeError" })}`,
    ].join("\n");
    const out = parseSseBlock(block);
    expect(out).toEqual({
      type: "error",
      detail: "kaboom",
      errorType: "RuntimeError",
    });
  });

  it("returns null for an event without a name (heartbeat-style)", () => {
    const block = "data: keep-alive";
    expect(parseSseBlock(block)).toBeNull();
  });

  it("returns null when the data line isn't valid JSON", () => {
    const block = ["event: phase", "data: not actually json"].join("\n");
    expect(parseSseBlock(block)).toBeNull();
  });

  it("returns null for an unrecognised event name", () => {
    const block = ["event: heartbeat", "data: {}"].join("\n");
    expect(parseSseBlock(block)).toBeNull();
  });

  it("parses done events containing the full AskResponse shape", () => {
    const block = [
      "event: done",
      `data: ${JSON.stringify({
        answer: "There are 91.",
        conversation_id: "abc",
        turn_index: 1,
        sql: "SELECT count(*) FROM customers",
        rows: [{ count: 91 }],
        row_count: 91,
        error: null,
        attempts: 1,
        status: "ok",
        pending_risk: null,
        insight: { headline: "91", bullets: [], metric_highlights: [] },
        chart_kind: "kpi",
        chart_spec: null,
        cost: {
          llm_calls: 3,
          embedding_calls: 1,
          db_explain_calls: 1,
          db_select_calls: 1,
          est_tokens_in: 150,
          est_tokens_out: 75,
          est_usd: 0.00004,
        },
      })}`,
    ].join("\n");
    const out = parseSseBlock(block);
    expect(out?.type).toBe("done");
    if (out?.type === "done") {
      expect(out.response.row_count).toBe(91);
      expect(out.response.chart_kind).toBe("kpi");
    }
  });
});
