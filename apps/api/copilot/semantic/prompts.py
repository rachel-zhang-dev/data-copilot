"""LLM prompts for the semantic-layer router (Phase 3.1 / ADR 0023).

The router's job is small but precise: read the user's question and
decide ONE of two things —

  * **answerable** via the semantic model → emit a ``ResolverSpec``
    JSON object with metric + dimensions + optional time range +
    optional filters. The compiler turns this into deterministic SQL.

  * **not answerable** (the question needs a metric or dimension that
    isn't modelled, or it's a follow-up that references prior turns,
    or it's open-ended) → emit ``{"answerable": false, "reason": "…"}``.
    The graph then falls through to the existing text-to-SQL pipeline.

We use ``response_format={"type": "json_object"}`` on the call so the
model is forced into JSON output mode (same posture as ``coverage_check``
in ADR 0016).

Keeping the system prompt strict about "DEFAULT TO answerable=false
when in doubt" is intentional — a false-positive routes a question
the semantic layer can't actually answer through the deterministic
compiler, which then either produces wrong-but-confident SQL or
raises a ResolverError. A false-negative just costs an extra LLM
call (text-to-SQL takes over). The asymmetric cost shapes the
default.
"""

from __future__ import annotations

from copilot.semantic.models import SemanticModel

METRIC_ROUTER_SYSTEM = """\
You are a router that decides whether a user's data question can be
answered by selecting from a fixed menu of pre-defined business
metrics and dimensions (the "semantic layer") OR whether it needs a
free-form SQL writer (the "fallback path").

You will be given:
  1. The menu of available metrics (with descriptions).
  2. The menu of available dimensions (with descriptions).
  3. The user's natural-language question.

Your output is ONE JSON object — no surrounding prose, no markdown
fences — matching this exact schema:

{
  "answerable": true | false,
  "reason":      str,                          # one short sentence
  "metric":      str | null,                   # required when answerable
  "dimensions":  [str, ...],                   # 0-3 entries when answerable
  "time_range":  { "year": int } | null,
  "filters":     [
    { "dimension": str, "op": "=" | "in", "value": str | number | [..] }
  ]
}

DECISION RULES:

* DEFAULT TO ``answerable: false``. The fallback path is competent
  on its own; routing a question to the semantic layer that the
  semantic layer cannot actually answer produces a worse outcome
  than just letting the LLM write SQL.

* Only set ``answerable: true`` when ALL of the following hold:
  - The question asks for one of the listed metrics (possibly with
    synonyms — "revenue" and "sales" both map to a ``revenue`` metric).
  - Any slicing ("by country", "per month") maps to a listed dimension.
  - Any time window can be expressed as ``{year: N}`` (no quarter-
    over-quarter, no relative windows like "last 30 days").
  - Any filter is a simple equality ("in Germany", "for category
    Beverages") on a listed dimension.

* Set ``answerable: false`` when:
  - The user asks for a metric not in the menu (e.g. "churn rate",
    "conversion funnel", "discount percentage").
  - The user needs a JOIN the menu doesn't model.
  - The question is a follow-up that references prior turns ("and in
    France?", "broken down by month") — the router can't see prior
    turns, only the fallback path can.
  - The question is open-ended / investigative ("why is X declining").
  - You're unsure for any other reason.

* ``reason`` is always required. On ``answerable: true`` it should
  cite the chosen metric + dimensions. On ``false`` it should name
  the specific missing concept so the user (and the next layer) can
  decide what to do.

* Output JSON only. No commentary, no fences, no follow-up text.
"""


METRIC_ROUTER_USER_TEMPLATE = """\
Available metrics:
{metrics}

Available dimensions:
{dimensions}

Time range vocabulary:
  - ``{{"year": N}}``  — restrict to one calendar year (e.g. ``{{"year": 1997}}``).
  - ``null``           — no time filter.

Filter vocabulary:
  - ``{{"dimension": NAME, "op": "=", "value": LITERAL}}``  — equality.
  - ``{{"dimension": NAME, "op": "in", "value": [..]}}``    — set membership.

User question:
{question}

JSON verdict:
"""


def format_menu(model: SemanticModel) -> dict[str, str]:
    """Render the model's metrics + dimensions as compact bullet lists
    suitable for stuffing into ``METRIC_ROUTER_USER_TEMPLATE``."""
    metrics_lines = [
        f"  - {m.name}: {m.description}" for m in model.metrics
    ]
    dim_lines = [f"  - {d.name}: {d.description}" for d in model.dimensions]
    return {
        "metrics": "\n".join(metrics_lines) or "  (none)",
        "dimensions": "\n".join(dim_lines) or "  (none)",
    }
