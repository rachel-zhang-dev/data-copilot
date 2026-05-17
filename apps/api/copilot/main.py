"""FastAPI entry point."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot.agent import build_graph
from copilot.config import get_settings


def _configure_langsmith() -> None:
    """Wire LangSmith tracing via environment variables.

    LangChain reads these at import time of various modules, so we
    set them as early as possible.
    """
    settings = get_settings()
    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
        os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_langsmith()
    app.state.graph = build_graph()
    yield


app = FastAPI(
    title="Data Copilot API",
    description="Enterprise Text-to-SQL agent.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    """Run the agent on a single user question."""
    graph = app.state.graph
    result = await graph.ainvoke({"question": req.question})
    return AskResponse(answer=result["answer"])
