"""Unit tests for dialogue.py: the per-turn bookkeeping nodes."""

from __future__ import annotations

from copilot.agent.dialogue import (
    append_to_dialogue_node,
    current_turn_index,
    format_dialogue_for_prompt,
    reset_per_turn_node,
)

# ---------------------------------------------------------------------------
# current_turn_index
# ---------------------------------------------------------------------------


def test_current_turn_index_starts_at_one() -> None:
    assert current_turn_index({}) == 1


def test_current_turn_index_increments_per_completed_turn_pair() -> None:
    state = {
        "dialogue": [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ]
    }
    assert current_turn_index(state) == 2

    state["dialogue"].extend(
        [
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
        ]
    )
    assert current_turn_index(state) == 3


# ---------------------------------------------------------------------------
# reset_per_turn_node
# ---------------------------------------------------------------------------


def test_reset_per_turn_clears_turn_local_fields() -> None:
    state = {
        "dialogue": [{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"}],
        "intent": "data",
        "relevant_schema": "Table: x",
        "sql": "SELECT 1",
        "sql_result": [{"x": 1}],
        "row_count": 1,
        "error": "stale",
        "answer": "stale answer",
    }
    out = reset_per_turn_node(state)
    for k in ("intent", "relevant_schema", "sql", "sql_result", "row_count", "error", "answer"):
        assert out[k] is None, f"field {k} should be reset"


def test_reset_per_turn_assigns_turn_index() -> None:
    out = reset_per_turn_node({})
    assert out["turn_index"] == 1

    state = {
        "dialogue": [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ]
    }
    out = reset_per_turn_node(state)
    assert out["turn_index"] == 2


def test_reset_per_turn_does_not_touch_dialogue_or_messages() -> None:
    """The whole point: dialogue / messages persist across turns."""
    state = {
        "dialogue": [{"role": "user", "content": "keep me"}],
        "messages": ["should not be cleared"],
    }
    out = reset_per_turn_node(state)
    assert "dialogue" not in out
    assert "messages" not in out


# ---------------------------------------------------------------------------
# append_to_dialogue_node
# ---------------------------------------------------------------------------


def test_append_to_dialogue_emits_user_assistant_pair_on_data_question() -> None:
    state = {
        "question": "How many customers?",
        "answer": "There are 91 customers.",
        "sql": "SELECT count(*) FROM customers LIMIT 100",
        "row_count": 1,
    }
    out = append_to_dialogue_node(state)
    pair = out["dialogue"]
    assert len(pair) == 2

    assert pair[0]["role"] == "user"
    assert pair[0]["content"] == "How many customers?"

    assert pair[1]["role"] == "assistant"
    assert pair[1]["content"] == "There are 91 customers."
    assert pair[1]["sql"] == "SELECT count(*) FROM customers LIMIT 100"
    assert pair[1]["row_count"] == 1


def test_append_to_dialogue_omits_sql_for_chitchat() -> None:
    state = {"question": "Hello", "answer": "Hi! How can I help?"}
    out = append_to_dialogue_node(state)
    assistant_turn = out["dialogue"][1]
    assert "sql" not in assistant_turn
    assert "row_count" not in assistant_turn


def test_append_to_dialogue_handles_missing_answer_gracefully() -> None:
    state = {"question": "Q"}
    out = append_to_dialogue_node(state)
    assert out["dialogue"][1]["content"] == ""


# ---------------------------------------------------------------------------
# format_dialogue_for_prompt
# ---------------------------------------------------------------------------


def test_format_dialogue_for_prompt_empty() -> None:
    assert format_dialogue_for_prompt([]) == ""


def test_format_dialogue_for_prompt_renders_user_and_assistant() -> None:
    dialogue = [
        {"role": "user", "content": "How many customers?"},
        {"role": "assistant", "content": "91", "sql": "SELECT count(*) FROM customers"},
    ]
    out = format_dialogue_for_prompt(dialogue)
    assert "User: How many customers?" in out
    assert "Assistant: 91" in out
    assert "SELECT count(*) FROM customers" in out


def test_format_dialogue_for_prompt_caps_at_max_turns() -> None:
    dialogue = []
    for i in range(20):
        dialogue.append({"role": "user", "content": f"Q{i}"})
        dialogue.append({"role": "assistant", "content": f"A{i}"})

    out = format_dialogue_for_prompt(dialogue, max_turns=4)
    # Should only include the last 4 turns
    assert "Q19" in out
    assert "Q18" in out
    assert "Q0" not in out
    assert out.count("User:") + out.count("Assistant:") == 4
