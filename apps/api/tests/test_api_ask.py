"""Tests for ``copilot.main`` request validation.

The week-7 change introduces a two-shape ``AskRequest`` (a "fresh
question" turn vs a "resume a paused turn" turn). The model validator
enforces those shapes server-side so the rest of the endpoint never
has to defensive-code missing fields. These tests pin the contract.
"""

from __future__ import annotations

import pytest
from copilot.main import AskRequest
from pydantic import ValidationError


def test_fresh_request_requires_question() -> None:
    with pytest.raises(ValidationError, match="question is required"):
        AskRequest()


def test_fresh_request_rejects_blank_question() -> None:
    with pytest.raises(ValidationError, match="question is required"):
        AskRequest(question="   ")


def test_fresh_request_is_valid_without_conversation_id() -> None:
    req = AskRequest(question="How many customers?")
    assert req.question == "How many customers?"
    assert req.resume is None


def test_resume_request_requires_conversation_id() -> None:
    with pytest.raises(ValidationError, match="conversation_id is required"):
        AskRequest(resume="approve")


def test_resume_request_must_not_include_question() -> None:
    with pytest.raises(ValidationError, match="question must be omitted"):
        AskRequest(
            question="ignore me",
            conversation_id="abc",
            resume="approve",
        )


def test_resume_request_happy_path() -> None:
    req = AskRequest(conversation_id="abc-123", resume="approve")
    assert req.resume == "approve"
    assert req.question is None


def test_resume_only_accepts_known_decisions() -> None:
    with pytest.raises(ValidationError):
        AskRequest(conversation_id="abc", resume="maybe")  # type: ignore[arg-type]
