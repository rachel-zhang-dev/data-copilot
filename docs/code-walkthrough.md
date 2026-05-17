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

## 9. Glossary (so you do not have to Google mid-read)

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
