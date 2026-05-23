# Eval reports

This directory holds A/B comparison reports from the eval harness.

## File naming

`<UTC-timestamp>-<experiment>.md` per run, plus a `<timestamp>-summary.md`
that links the three together. The timestamp is set when the runner
launches, so files from one invocation share a prefix.

## Reading a report

Each comparison report has four sections:

1. **Header** with baseline vs treatment config and timestamp.
2. **Summary** — four metrics with deltas: `success_rate`,
   `avg_attempts`, `avg_latency_ms`, `avg_total_tokens`.
3. **By category** — same metrics broken down by case category
   (count / single_table_filter / aggregation / join / follow_up /
   chitchat / destructive / ambiguous). Bold deltas (`**+12.5 pp**`)
   call out shifts ≥10 percentage points.
4. **Fixed by treatment** — cases the baseline failed but treatment
   passed (the most informative evidence). Plus a regressions section
   for the opposite case.

## Generating reports

```bash
./scripts/dev.sh eval                          # all 3 A/Bs, all cases
./scripts/dev.sh eval --experiment schema_rag  # one experiment
./scripts/dev.sh eval --dry-run                # print to stdout, don't write files
```

See [ADR 0007](../decisions/0007-eval-methodology.md) for the
methodology. Cases live in [`data/eval/cases.yaml`](../../data/eval/cases.yaml).
