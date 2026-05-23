# Code Walkthrough

A line-by-line tour of the codebase, written for newcomers to LangGraph
and modern Python AI app stacks. Read this side-by-side with the source.

> Conventions in this guide:
> - 📁 = file path
> - 🔑 = key concept worth remembering
> - 💡 = "why" / design rationale
> - ⚠️ = a foot-gun to avoid

---

## 1. The big picture

```
            ┌────────────────────────────┐
            │  Browser / curl / frontend │
            └──────────────┬─────────────┘
                           │ HTTP POST /ask  { "question": "..." }
                           ▼
            ┌────────────────────────────┐
            │  apps/api/copilot/main.py  │   ← FastAPI: routing, validation, JSON
            └──────────────┬─────────────┘
                           │ await graph.ainvoke({...})
                           ▼
            ┌────────────────────────────┐
            │  agent/graph.py            │   ← LangGraph: the state machine
            │    (a single node so far)  │
            └──────────────┬─────────────┘
                           │ llm.invoke(messages)
                           ▼
            ┌────────────────────────────┐
            │  llm.py → langchain-openai │   ← Wraps the OpenAI SDK
            └──────────────┬─────────────┘
                           │ HTTPS request
                           ▼
            ┌────────────────────────────┐
            │  DeepSeek API              │   ← The actual LLM
            └────────────────────────────┘
```

🔑 Each layer has one job. FastAPI does HTTP. LangGraph does flow
control. `langchain-openai` does the LLM SDK. DeepSeek does inference.
Swapping any one of them only touches one file.

---

## 2. 📁 `copilot/main.py` — the FastAPI app

### 2.1 Lifespan: build the agent **once**

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_langsmith()
    app.state.graph = build_graph()
    yield
```

🔑 **Why store the graph on `app.state`?**
Compiling a LangGraph is non-trivial — it walks every node, validates
edges, sets up internal data structures. We do it **once at startup** and
reuse the compiled object across every request.

💡 `lifespan` is FastAPI's way of saying "run this code at startup
(before `yield`) and at shutdown (after `yield`)". When you later add a
DB pool, Redis client, or background task scheduler, this is where they
live.

### 2.2 Pydantic schemas as contracts

```python
class AskRequest(BaseModel):
    question: str

class AskResponse(BaseModel):
    answer: str
```

🔑 These three lines do **four** jobs at once:

1. Validate incoming JSON (reject `question: 123`, `question: null`, etc.).
2. Convert JSON ↔ Python objects automatically.
3. Generate the OpenAPI schema you saw at `/docs`.
4. Give your IDE / mypy enough info to autocomplete `req.question`.

⚠️ Don't fall back to plain `dict` parameters — you lose all four benefits.

### 2.3 The `/ask` handler — three lines that matter

```python
@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    graph = app.state.graph                                     # (a)
    result = await graph.ainvoke({"question": req.question})    # (b)  ⭐
    return AskResponse(answer=result["answer"])                 # (c)
```

| Line | What happens |
|------|--------------|
| (a) | Read the pre-compiled graph. **No I/O, no LLM call.** |
| (b) | ⭐ The actual call. LangGraph runs the state machine; inside `echo_node` the LLM is invoked over HTTPS. This is the only line that takes "real time". |
| (c) | Wrap the answer dict into a typed response model. |

🔑 The user's question 👉 LangGraph state 👉 the LLM 👉 the answer all
flows through `(b)`. Almost everything else in the project is plumbing
around that single line.

---

## 3. 📁 `copilot/agent/state.py` — what flows through the graph

```python
class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    question: str
    sql: str
    sql_result: list[dict]
    answer: str
    ...
```

🔑 **State is a `TypedDict`**, not a class instance. Each LangGraph node
takes the state in and returns a dict of fields to merge back. There is
no `self`; there is just data flowing through pure functions.

🔑 **Reducers** (the `Annotated[list, add_messages]` part).
By default, returning `{"messages": [m]}` from a node would *overwrite*
the existing `messages`. The `add_messages` reducer changes that to
*append* — exactly what you want for chat history.

We will introduce more reducers in week 5 (e.g. an "errors" list that
accumulates across retry loops).

---

## 4. 📁 `copilot/agent/graph.py` — the LangGraph itself

### 4.1 What is a node?

```python
def echo_node(state: AgentState) -> dict:
    llm = get_llm(temperature=0.3)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=state["question"]),
    ]
    response = llm.invoke(messages)
    return {
        "messages": [response],
        "answer": response.content,
    }
```

🔑 **A node is just a function** that takes state and returns a state
diff. No subclassing, no decorators, no magic. This is one of LangGraph's
biggest strengths over older agent frameworks: nodes are easy to read,
easy to unit-test, and easy to compose.

⚠️ Always return a **dict** with field names matching `AgentState`. If
you return a plain string by accident, LangGraph will not be able to
merge it.

### 4.2 What is the graph?

```python
def build_graph():
    workflow = StateGraph(AgentState)
    workflow.add_node("echo", echo_node)
    workflow.add_edge(START, "echo")
    workflow.add_edge("echo", END)
    return workflow.compile()
```

🔑 Building a graph is always the same three-step recipe:

1. `StateGraph(<state schema>)`
2. `add_node("name", function)` — register every step
3. `add_edge(from, to)` — wire them together

`START` and `END` are sentinels marking the entry and exit. Once we add
the second node next week we will use `add_conditional_edges` to branch
on runtime data (e.g. "if SQL execution failed, go to rewrite_sql,
otherwise go to summarise_result").

🔑 `compile()` returns a runnable, immutable object. Treat it as
read-only. Build it once, reuse it forever.

---

## 5. 📁 `copilot/llm.py` — the model factory

```python
def get_llm(temperature: float = 0.0, **kwargs):
    settings = get_settings()
    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
        **kwargs,
    )
```

🔑 **DeepSeek speaks the OpenAI dialect**, so `langchain-openai` works
out of the box. By centralising model construction in this one factory:

* Switching providers becomes a `.env` change.
* Per-call overrides (`temperature`, `max_tokens`, …) live in one place.
* Tests can monkey-patch this single function instead of hunting for
  `ChatOpenAI(...)` calls scattered through the codebase.

💡 In week 4 we will let different nodes use different models — e.g.
`gpt-4o-mini` for cheap classification, DeepSeek-Chat for SQL
generation. That ergonomics comes "for free" because of this factory.

---

## 6. 📁 `copilot/config.py` — typed settings

```python
class Settings(BaseSettings):
    deepseek_api_key: str = Field(..., description="DeepSeek API key")
    dashscope_api_key: str | None = None
    database_url: str | None = None
    ...
```

🔑 **`pydantic-settings` ⇒ failing fast at startup.** If `.env` is
missing a required key the app crashes immediately with a clear message
— far better than the LLM call failing 500 ms into a request.

🔑 **Optional vs required.** Anything we are not yet using
(`dashscope_api_key`, `database_url`) is `Optional`. Each feature flips
its dependency to required when it is wired up.

🔑 **Locating `.env`.** The helper walks up the file tree until it sees
`pyproject.toml`, so the same code works whether you run uvicorn from
project root, pytest from `apps/api/`, or a Jupyter notebook from
`notebooks/`.

---

## 7. The request lifecycle, end to end

This is the canonical happy path — print it out and pin it to a wall.

```
1. You type in Swagger UI
        { "question": "你好" }
        and click Execute.

2. Browser sends:
        POST /ask
        Content-Type: application/json
        { "question": "你好" }

3. Uvicorn (the web server) accepts the TCP connection.

4. FastAPI matches the URL to the @app.post("/ask") handler.

5. Pydantic parses the JSON body into AskRequest(question="你好")
        — fails 422 immediately if the JSON is malformed.

6. ask() runs:
        graph = app.state.graph
        result = await graph.ainvoke({"question": "你好"})
                                        │
                                        ▼
7. LangGraph routes execution to the "echo" node.

8. echo_node():
        llm = get_llm(temperature=0.3)
        messages = [SystemMessage(...), HumanMessage("你好")]
        response = llm.invoke(messages)
                                │
                                ▼
9. langchain-openai builds the OpenAI-style payload and calls
        POST https://api.deepseek.com/chat/completions
        Authorization: Bearer sk-...
        { "model": "deepseek-chat", "messages": [...] }

10. DeepSeek runs inference, returns:
        { "choices": [{"message": {"content": "你好！我是 ..."}}], ... }

11. langchain-openai converts that into an AIMessage object.

12. echo_node returns {"messages": [response], "answer": response.content}.

13. LangGraph merges the diff into state, follows the edge "echo" → END.

14. graph.ainvoke returns the final state dict.

15. ask() reads state["answer"] and returns AskResponse(answer=...).

16. FastAPI serialises that to JSON, sets Content-Type, returns HTTP 200.

17. You see the answer in Swagger UI.
```

---

## 8. Where to look next

| Question                                | File to read |
|-----------------------------------------|--------------|
| How is LangSmith tracing configured?    | `main.py::_configure_langsmith` |
| How do I add a new endpoint?            | `main.py` (copy `/health`) |
| How do I add a new node to the agent?   | `graph.py::build_graph` |
| How do I add a new state field?         | `state.py::AgentState` |
| How do I switch LLM providers?          | `.env` + `llm.py::get_llm` |
| How do I add a config value?            | `config.py::Settings` + `.env.example` |
| How are tests structured?               | `apps/api/tests/test_health.py` |

---

## 9. Week 2 — the multi-node graph

Week 1's `echo_node` has been retired. The graph now branches and the
data path runs a real text-to-SQL pipeline. The wiring is in
[`agent/graph.py`](../apps/api/copilot/agent/graph.py); each node lives
in [`agent/nodes.py`](../apps/api/copilot/agent/nodes.py).

### 9.1 The shape

```
                  classify_intent
                   /          \
              chitchat        data
                |               |
            small_talk     generate_sql
                |               |
                |          validate_sql
                |          /          \
                |     invalid         valid
                |        |              |
                |   finalize_error  execute_sql
                |        ^              |
                |        | db error     |
                |        +--------------+
                |                       |
                |                  summarize_result
                |                       |
                +----------+------------+
                           v
                          END
```

### 9.2 New patterns to notice

* 🔑 **`add_conditional_edges`** lets a node return one of several
  next-node names. Routing decisions live in *plain functions* in
  `nodes.py` (`route_after_classify`, `route_after_validate`,
  `route_after_execute`) — easy to unit-test in isolation.

* 🔑 **Error as state, not exception.** Any node that fails sets
  `state["error"] = "<reason>"` and returns; downstream routers check
  the field. This makes the failure path identical to the happy path
  from LangGraph's perspective and lets us add retries / human review
  later without restructuring nodes.

* 🔑 **Policy and I/O live in separate modules.** `sql_safety.py` is
  pure (parse, validate, rewrite, raise) and trivially testable;
  `db.py` does nothing but talk to Postgres; `nodes.py` glues them
  together. Nothing in the safety module knows about LangGraph; nothing
  in `db.py` knows about the LLM.

* ⚠️ **Schema is pulled lazily.** `generate_sql_node` calls
  `get_schema_ddl()` only if the state does not already contain one.
  This is what lets unit tests inject a tiny schema string without
  touching Postgres.

### 9.3 The end-to-end happy path (data question)

```
POST /ask { "question": "How many customers?" }
  │
  ▼
classify_intent ──► intent = "data"
  │
  ▼
generate_sql   ──► sql = "SELECT COUNT(*) FROM customers"
  │
  ▼
validate_sql   ──► sql = "SELECT COUNT(*) FROM customers LIMIT 100"
  │
  ▼
execute_sql    ──► sql_result = [{"count": 91}], row_count = 1
  │
  ▼
summarize_result ──► answer = "There are 91 customers in total."
  │
  ▼
END  ──► {"answer": "...", "sql": "...", "rows": [...], "row_count": 1}
```

The HTTP response carries the SQL and rows alongside the answer
(`AskResponse` in `main.py`), so the eventual Next.js UI can show
both a chat bubble and a data table.

---

## 10. Week 3 — schema-aware RAG

The week-2 `generate_sql` node received **all 14 tables' DDL** in
every prompt. That works for Northwind but does not scale to
hundreds of tables. Week 3 inserts a `retrieve_schema` node before
SQL generation that picks just the relevant tables.

### 10.1 Three pieces

- `copilot/embeddings.py` — 1-function factory returning a
  `langchain_openai.OpenAIEmbeddings` pointed at SiliconFlow.
  Provider switch is one env-var change.
- `copilot/agent/retriever.py` — the `retrieve_schema_node` plus
  two pure helpers (`directly_named_tables` for the literal-mention
  shortcut, `expand_with_foreign_keys` for FK graph traversal).
- `copilot/indexer.py` — offline indexer; rebuilds
  `schema_embeddings` in one transaction.

### 10.2 Why a separate indexer

"Build vs serve" separation. Doing the embedding work eagerly at
server startup would couple deploy reliability to SiliconFlow's
uptime. Instead:

```
./scripts/dev.sh up            # auto-runs index when table is empty
./scripts/dev.sh index --force # explicit rebuild after schema change
./scripts/dev.sh index --check # inspect current state, no writes
```

If `schema_embeddings` is empty or the embedding API errors, the
agent falls back to dumping the full schema. RAG outage degrades
quality gracefully, never takes the agent offline.

### 10.3 Query-time path

```python
def retrieve_schema_node(state):
    named   = directly_named_tables(state["question"], list_tables())
    top_k   = vector_search_tables(state["question"], schema_top_k)
    seed    = named | set(top_k)
    expanded = expand_with_foreign_keys(seed, get_foreign_keys(), max_hops=1)
    return {"relevant_schema": get_table_ddl(sorted(expanded))}
```

The named-table fast-path is not just optimisation — it is what
keeps the agent useful when the embedding API is flaky.

FK expansion happens here, not in `generate_sql`'s prompt, because
the LLM needs to see the bridge table's actual columns to write a
correct JOIN.

### 10.4 What the LLM sees

For "Top 5 products by total revenue", before week 3 the prompt
included DDL for all 14 tables. After week 3 it includes only
`products`, `categories`, `order_details`, `orders` — about 60%
fewer tokens, with explicit "Foreign keys" / "Referenced by"
markers so the JOIN direction is unambiguous.

### 10.5 Week-2 `get_schema_ddl()` is now the fallback

`get_schema_ddl()` still exists — it backs the "dump everything"
fallback path. It now delegates to `get_table_ddl(list_tables())`,
so there is exactly one place that knows how to format a table
block.

---

## 11. Week 4 — self-healing retry loop

When validation or execution fails, the agent now loops back to
`generate_sql` and gives the LLM a corrective prompt with the
previous SQL and the error message. Bounded per error type so it
cannot run away.

### 11.1 Three new things in the code

- `state.attempts: list[Attempt]` with an `operator.add` reducer.
  Append-only history of failed attempts. Both routers (counting
  failures) and `generate_sql_node` (showing the last failure to
  the LLM) read from it.
- `nodes.classify_error()` and `nodes.can_retry()`. Pure functions,
  ten lines each, fully unit-tested. The whole retry policy fits in
  one screen.
- `RETRY_SQL_SYSTEM` + `RETRY_SQL_USER_TEMPLATE` in `prompts.py`.
  The retry prompt is deliberately structured: schema, original
  question, your previous attempt, the error, "do not just
  re-issue the same SQL".

### 11.2 Why state.attempts and not a counter

A counter is enough for routing, but the retry prompt needs the
last SQL and the last error verbatim. Storing the full history is
basically free, gives us LangSmith traces showing each rewrite, and
lets `AskResponse` expose the count to the API caller — all from
one field.

The reducer is `operator.add`, so any node returning
`{"attempts": [Attempt(...)]}` gets concatenated rather than
overwriting. We get appendingly correct behaviour even when retries
re-execute the same node.

### 11.3 Why validate_sql / execute_sql write the attempt themselves

When a node fails, it already constructs an error message; appending
an `Attempt` is one extra dict literal. Splitting it into a
"record_failure" node would mean two state hops per failure — more
edges in the graph, more places to forget to record, no real
benefit.

### 11.4 Per-class retry budget

```python
RETRY_BUDGET = {
    "execution_failed": 2,   # column / table typos: high LLM fix-rate
    "unsafe_sql":       1,   # one corrective shot, then give up
    "fatal":            0,
}
HARD_RETRY_CEILING = 5       # global override regardless of budget
```

Keeping each class to its own budget avoids two failure modes:
either being too generous on the destructive-intent case (which
wastes tokens defending against the user's actual intent), or being
too stingy on the typo case (which prematurely fails recoverable
queries).

See [ADR 0004](decisions/0004-self-healing-policy.md) for the
rationale and the prompt-design trade-offs we considered.

### 11.5 What's not retried

`fatal` errors — anything not classified as `unsafe_sql` or
`execution_failed`. This catches network blips, programmer
mistakes, and other things the LLM cannot fix by re-prompting.
Better to terminate quickly with a clear error than to obscure the
bug behind retry latency.

### 11.6 What the LLM sees on retry

```
SYSTEM: You are a senior data analyst fixing a SQL query that just failed.
        ... rules ...

USER:
Schema:
<focused DDL>

Original question:
How many customers are there?

Your previous attempt (#1) was:
SELECT count(*) FROM customer

The system rejected it with:
relation "customer" does not exist

Corrected SQL (#2):
```

The LLM almost always responds with `SELECT count(*) FROM customers`
on the second try. Empirically this single retry fixes 80%+ of
naming and typo errors on Northwind.

---

## 12. Week 5 — multi-turn dialogue and compaction

The Week 4 graph treated each `/ask` as a fresh universe. Week 5
adds three small bookkeeping nodes that turn it into a real chat:

```
                   reset_per_turn      <-- runs first
                          |
                  classify_intent
                          ...
                  summarize_result   small_talk   finalize_error
                          \             |             /
                           \            |            /
                          append_to_dialogue        <-- appends user+assistant
                                  |
                          compact_history          <-- summarises old turns if long
                                  |
                                 END
```

### 12.1 Persistence: `PostgresSaver` does the heavy lifting

We `compile(checkpointer=PostgresSaver(pool))` instead of plain
`compile()`. With that one change, every `ainvoke` keyed on
`thread_id` automatically loads the prior state and saves the diff
after each node. We did not write a single line of "load state /
save state" code. See `copilot/checkpointer.py`.

When debugging:

```sql
SELECT thread_id, channel_values->'dialogue'
FROM checkpoints
ORDER BY created_at DESC LIMIT 5;
```

You can see exactly what the agent thought a conversation looked
like at any point in time.

### 12.2 The `dialogue` field and its custom reducer

Two distinct mutation patterns share one field:

* `append_to_dialogue_node` adds `[user_turn, assistant_turn]` per
  invocation;
* `compact_history_node` occasionally rewrites the entire list with
  `[summary_turn, *last_N]`.

The custom `replace_or_append` reducer treats a plain list return as
append, but recognises the sentinel dict `{"replace": [...]}` as
"overwrite the field with this list". One reducer, two semantics, no
extra fields, no `RemoveMessage` plumbing.

### 12.3 Per-turn retry budgets

Without per-turn isolation, a follow-up question could be born into
"already at retry limit" because a prior turn racked up
`execution_failed` records. The fix is one extra field on
`Attempt`:

```python
class Attempt(TypedDict):
    sql: str
    error: str
    error_class: ErrorClass
    turn_idx: int   # NEW: which turn this failure belongs to
```

`can_retry(attempts, turn_idx)` filters by `turn_idx` before
counting. Failures from prior turns remain in the list for telemetry
but no longer affect routing.

### 12.4 Compaction is opt-in by token budget

`count_tokens` uses a deliberately cheap heuristic (chars/4). We
compact only when the budget is exceeded; below the threshold,
`compact_history_node` is a zero-cost no-op. When triggered the
older turns get summarised into one synthetic
`[Earlier in this conversation] ...` turn while the most recent N
turns stay verbatim — best of both worlds.

If the LLM call inside the summariser fails, we fall back to hard
truncation (last N verbatim, no summary). The conversation continues
to work; only the early context is silently lost.

### 12.5 What `reset_per_turn` clears (and what it doesn't)

It clears: `intent`, `relevant_schema`, `sql`, `sql_result`,
`row_count`, `error`, `answer`. Sets `turn_index` to the new turn
number.

Notably absent:

* `messages`, `dialogue` — these persist; they are the conversation.
* `attempts` — kept for telemetry; the per-turn `turn_idx` filter
  inside `can_retry` makes earlier failures inert without us having
  to delete them.

Returning `None` for a key sets the field to `None`, which our
helpers treat as "absent" via `state.get(...)`.

---

## 13. Week 6 — eval harness and A/B experiments

The agent now has a reproducible way to ask "did week 3 / 4 / 5
actually help?". The harness lives in
[`apps/api/copilot/eval/`](../apps/api/copilot/eval) and produces
committable markdown reports under `docs/eval/`.

### 13.1 Three layers

```
data/eval/cases.yaml         <- hand-written, ~30 cases, 8 categories
       |
       v
copilot/eval/cases.py        <- strict YAML loader -> typed CaseSpec
copilot/eval/config.py       <- ExperimentConfig presets
       |
       v
copilot/eval/runner.py       <- load -> invoke -> grade -> aggregate
copilot/eval/graders/        <- deterministic (regex / substring / row count)
copilot/eval/experiments/    <- A1 schema_rag, A2 self_healing, A3 dialogue_context
copilot/eval/reports/        <- markdown rendering
copilot/eval/__main__.py     <- CLI entrypoint, wired by `dev.sh eval`
```

### 13.2 Feature flags as the swap mechanism

`copilot.agent.feature_flags` exposes a `with override(...):` context
manager that flips three module-level booleans (`SCHEMA_RAG_ENABLED`,
`DIALOGUE_CONTEXT_ENABLED`) and the `RETRY_BUDGET` dict in unison,
restoring on exit. Production never touches these — they are eval-
only knobs.

The runner does:

```python
with feature_flags.override(
    schema_rag_enabled=cfg.schema_rag_enabled,
    dialogue_context_enabled=cfg.dialogue_context_enabled,
    retry_budget=cfg.retry_budget_override,
):
    for case in cases:
        ...
```

So one Python process can run baseline + treatment back-to-back
without restart and without flag leakage between them.

### 13.3 Follow-up state injection

A naive eval would replay each follow-up case's setup history
through real graph invocations — burning LLM calls for turns we
don't actually want to grade. Instead, the runner pre-populates the
`dialogue` field with the synthetic history before calling
`graph.ainvoke`. The agent sees the same context in `generate_sql`
either way, but only the question we care about consumes LLM budget.

### 13.4 Why deterministic graders only

`copilot.eval.graders.deterministic` runs simple substring / regex
checks over the agent's output. No LLM judge, no RAGAS:

* Reproducibility: same `(case, run)` -> same score forever.
* Cost: a full eval run is ~¥1-2; LLM-judge would 2-3x that.
* Scope: deterministic checks are sufficient to demonstrate
  comparative methodology, which is the portfolio point.

A future commit can layer LangSmith's `criteria` evaluator on top
without restructuring; the file slot is reserved.

### 13.5 What the report looks like

Each A/B produces a markdown file like `20260523-091200-schema_rag.md`
with a summary delta table (success_rate / attempts / latency /
tokens), a per-category breakdown (10pp+ shifts bolded), and a
"fixed by treatment" section listing cases the baseline failed and
the treatment fixed — the most informative evidence in any
experiment.

See [ADR 0007](decisions/0007-eval-methodology.md) for why we chose
comparative methodology over standalone runs / RAGAS / LLM-judge.

---

## 14. Glossary (so you do not have to Google mid-read)

| Term | One-liner |
|------|----------|
| **ASGI** | Asynchronous Server Gateway Interface — the modern Python web protocol. Uvicorn implements it. |
| **`async` / `await`** | Co-operative concurrency: while waiting on I/O, the event loop runs other requests. Crucial for LLM apps because most time is spent waiting on the network. |
| **Lifespan** | A FastAPI hook that runs at startup and shutdown. |
| **Reducer** | In LangGraph state, a function that says "how to merge a node's returned value with the existing field" (e.g. append to list vs replace). |
| **State machine** | A graph of states (nodes) and transitions (edges). LangGraph compiles your nodes into one. |
| **OpenAPI** | A JSON description of a REST API. FastAPI generates this for you and the Swagger UI consumes it. |
| **`uv`** | A very fast Python package manager / venv tool, replacing pip + poetry. |
| **`pgvector`** | A PostgreSQL extension that adds a `vector` column type plus similarity-search operators. Lets one DB hold both your business data and your embeddings. |
