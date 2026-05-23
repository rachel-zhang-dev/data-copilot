# ADR 0005: Conversation persistence and compaction

> Status: Accepted · Date: 2026-05 (Week 5) · Supersedes: none

## Context

Through Week 4 every `/ask` call was independent: there was no way to
ask a follow-up like "and how about France?" because the agent had no
memory of the prior turn. Real product use is overwhelmingly
multi-turn — the user explores, refines, and references prior
results. Week 5 adds that capability with three dependent decisions:

1. **Where does conversation state live between requests?**
2. **How do we keep prompts under control on long conversations?**
3. **How do we keep retry budgets sensible per turn rather than per
   conversation?**

## Decision

### 1. State lives in Postgres via LangGraph's `PostgresSaver`

We attach a `PostgresSaver` checkpointer to the compiled graph at
startup. Every `/ask` call passes a `thread_id` (== `conversation_id`)
in the LangGraph config; LangGraph loads the prior state of that
thread before any node runs and persists the diff after each one.
Implementation lives in [`apps/api/copilot/checkpointer.py`](../../apps/api/copilot/checkpointer.py).

### 2. A token-budget compaction node summarises older turns

A new `compact_history_node` runs at the end of every turn. When the
cumulative `dialogue` field exceeds `compaction_threshold_tokens`
(default 4000), it asks the LLM to summarise the older turns into one
synthetic `[Earlier in this conversation] ...` `Turn` and replaces
`[older..., last_N]` with `[summary, last_N]`. Implementation in
[`apps/api/copilot/agent/compaction.py`](../../apps/api/copilot/agent/compaction.py).

### 3. Retry budgets are per turn, keyed by `Attempt.turn_idx`

The Week 4 `Attempt` record gains a `turn_idx` field. `can_retry`
filters attempts by the current turn, so a follow-up question always
starts with a fresh self-healing budget regardless of what happened in
prior turns of the same conversation.

## Alternatives considered

### Stateless server, history sent by client

Simpler in some ways: the client owns the message list and POSTs it
every call. Reasons we rejected:

* Cannot resume on a new device.
* Token cost is paid by clients sending growing payloads — eventually
  the request body itself becomes unwieldy.
* No server-side audit / time-travel.
* The server would have to validate / sanitise client-sent history,
  which is an opaque attack surface.

### Hand-rolled Redis session store

Plausible, but Redis is one more service to operate. Postgres is
already in our stack and `PostgresSaver` is a one-line integration.
Adopting Redis would mean writing a custom serializer for our state
shape and a custom keying scheme — both of which `PostgresSaver`
solves out of the box.

### Heavier-weight memory frameworks (mem0, zep)

Out of scope for a portfolio project at this stage. They optimise for
long-term memory, fact extraction, and cross-conversation retrieval —
none of which we need for "let me ask a follow-up about the same
data". Easy to add a layer above `dialogue` later if the project ever
needs it.

## Why per-class retry budgets per turn (not per conversation)

A budget that spans an entire conversation has the wrong shape: a
conversation that legitimately consists of many turns would
accumulate failures and eventually refuse to retry anything. Worse,
each turn would be at the mercy of the previous turn's bad SQL.

By tagging each `Attempt` with its `turn_idx` and filtering inside
`can_retry`, every new question gets a fresh budget while the full
attempt history remains observable for telemetry and tests.

## Why a custom `replace_or_append` reducer for `dialogue`

`dialogue` has two distinct mutation patterns:

* every turn appends one user + one assistant entry;
* the compactor occasionally replaces the entire list with
  `[summary, last_N]`.

LangGraph's standard reducers cover one or the other; we wrote a
two-line shim that switches behaviour based on whether the value
returned by a node is a plain `list[Turn]` (append) or a sentinel
`{"replace": [...]}` dict (replace). The protocol is internal — only
`compact_history_node` returns the sentinel form — and the type
checker keeps it honest.

## Why heuristic token counting (`chars / 4`)

We do not import a tokenizer. The compaction trigger is a budget,
not a precise figure-out-context-length operation. Empirically:

* English averages ≈4 characters per BPE token.
* Chinese averages ≈1-1.5 characters per token (so chars/4
  *underestimates* tokens, which means compaction triggers earlier
  than necessary — safer side).
* Real DeepSeek context is 64K tokens; threshold defaults to 4K, so
  we have a 16x safety margin even with bad heuristics.

If profiling shows we want precision later, replacing `count_tokens`
with `tiktoken` is a one-function change.

## Consequences

### Good

* `/ask` becomes a real chat endpoint; every product surface (CLI,
  Slack, future Next.js UI) gains continuation for free.
* Conversation state is durable, multi-replica safe, and visible
  through Postgres SQL when debugging — `SELECT thread_id,
  channel_values->'dialogue' FROM checkpoints` is a debugging
  superpower.
* Per-turn retry budgets keep the Week 4 self-healing logic clean
  and predictable.

### Bad / accepted trade-offs

* Two new tables (`checkpoints`, `checkpoint_writes`) plus their
  blobs grow proportional to conversation count × turns. Not a
  problem at this scale; if it ever is, LangGraph supports custom
  TTL via cron jobs.
* Compaction is one extra LLM call per turn ABOVE the threshold —
  costs money but only once the conversation is already long enough
  that a summary is genuinely useful.
* `PostgresSaver.setup()` runs at startup; if the database is down
  the API fails to start. We consider this correct (failure should
  be loud) but it does mean a chicken-and-egg dependency on Postgres
  availability that didn't exist before Week 5.

## Future work

* Week 6 (eval) can use multi-turn fixtures (e.g. "How many German
  customers? And France?") to measure follow-up resolution quality.
* Week 7 (HITL) can naturally insert a human-confirmation node on
  destructive turns, with the conversation continuing afterwards.
* Week 10 (frontend) can list all `thread_id`s belonging to a user
  in a sidebar — Postgres makes this a trivial query.
