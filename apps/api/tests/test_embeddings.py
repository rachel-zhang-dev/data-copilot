"""Unit tests for the embedding factory.

We do not call the real SiliconFlow API here — those tests live in the
integration suite. We only verify that:

1. The factory wires the configured base URL / model / dim through to
   the underlying ``OpenAIEmbeddings`` client.
2. The API key is wrapped in ``SecretStr`` so it does not leak into
   error messages or repr output.
3. The dimension probe correctly raises on mismatches.
"""

from __future__ import annotations

import pytest
from copilot import embeddings as emb_mod
from copilot.embeddings import EmbeddingError, check_embedding_dimension, get_embedder
from pydantic import SecretStr


def test_get_embedder_uses_configured_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-fake-1234567890")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    from copilot.config import get_settings

    get_settings.cache_clear()

    embedder = get_embedder()
    assert embedder.model == "BAAI/bge-m3"
    assert str(embedder.openai_api_base) == "https://api.example.com/v1"
    assert embedder.dimensions == 1024


def test_get_embedder_wraps_api_key_in_secret_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-super-secret-12345")
    from copilot.config import get_settings

    get_settings.cache_clear()
    embedder = get_embedder()
    assert isinstance(embedder.openai_api_key, SecretStr)
    assert "sk-super-secret-12345" not in repr(embedder)


def test_check_embedding_dimension_passes_when_dim_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubEmbedder:
        def embed_query(self, _text: str) -> list[float]:
            return [0.1] * 1024

    monkeypatch.setattr(emb_mod, "get_embedder", lambda: StubEmbedder())
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    from copilot.config import get_settings

    get_settings.cache_clear()

    assert check_embedding_dimension() == 1024


def test_check_embedding_dimension_raises_on_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubEmbedder:
        def embed_query(self, _text: str) -> list[float]:
            return [0.1] * 768

    monkeypatch.setattr(emb_mod, "get_embedder", lambda: StubEmbedder())
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    from copilot.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(EmbeddingError, match="dim mismatch"):
        check_embedding_dimension()


def test_check_embedding_dimension_wraps_provider_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubEmbedder:
        def embed_query(self, _text: str) -> list[float]:
            raise ConnectionError("no route to host")

    monkeypatch.setattr(emb_mod, "get_embedder", lambda: StubEmbedder())

    with pytest.raises(EmbeddingError, match="probe failed"):
        check_embedding_dimension()
