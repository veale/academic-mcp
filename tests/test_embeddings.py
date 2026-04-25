"""Tests for the embeddings provider switch."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# resolve_embedder — provider selection
# ---------------------------------------------------------------------------

class TestResolveEmbedder:
    def test_default_is_local_miniml(self, monkeypatch):
        """No env vars → local/all-MiniLM-L6-v2."""
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="local", model=""),
        )
        with patch("academic_mcp.embeddings._local_encoder") as enc_factory:
            enc_factory.return_value = (lambda texts: [[0.1] * 384 for _ in texts], None)
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder()
        assert emb.provider == "local"
        assert emb.model == "all-MiniLM-L6-v2"

    def test_explicit_provider_local(self, monkeypatch):
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="local", model=""),
        )
        with patch("academic_mcp.embeddings._local_encoder") as enc_factory:
            enc_factory.return_value = (lambda texts: [[0.1] * 384 for _ in texts], None)
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder(provider="local", model="BAAI/bge-small-en-v1.5")
        assert emb.provider == "local"
        assert emb.model == "BAAI/bge-small-en-v1.5"

    def test_explicit_provider_openai(self, monkeypatch):
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="openai", model="text-embedding-3-small", openai_key="sk-test"),
        )
        with patch("academic_mcp.embeddings._openai_encoder") as enc_factory:
            enc_factory.return_value = (lambda texts: [[0.1] * 1536 for _ in texts], None)
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder(provider="openai")
        assert emb.provider == "openai"
        assert emb.model == "text-embedding-3-small"

    def test_explicit_provider_gemini(self, monkeypatch):
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="gemini", model="gemini-embedding-001", gemini_key="gk-test"),
        )
        with patch("academic_mcp.embeddings._gemini_encoder") as enc_factory:
            enc_factory.return_value = lambda texts: [[0.1] * 768 for _ in texts]
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder(provider="gemini")
        assert emb.provider == "gemini"
        assert emb.model == "gemini-embedding-001"

    def test_unknown_provider_raises(self, monkeypatch):
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="local", model=""),
        )
        from academic_mcp.embeddings import EmbedderUnavailable, resolve_embedder
        with pytest.raises(EmbedderUnavailable, match="Unknown SEMANTIC_PROVIDER"):
            resolve_embedder(provider="cohere")

    def test_arbitrary_model_name_accepted(self, monkeypatch):
        """Any model string should be accepted without validation (provider decides)."""
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="openai", model="", openai_key="sk-test"),
        )
        arbitrary_model = "text-embedding-my-custom-finetune-v99"
        with patch("academic_mcp.embeddings._openai_encoder") as enc_factory:
            enc_factory.return_value = (lambda texts: [[0.1] * 512 for _ in texts], None)
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder(provider="openai", model=arbitrary_model)
        assert emb.model == arbitrary_model

    def test_openai_without_key_raises(self, monkeypatch):
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="openai", model="text-embedding-3-small", openai_key=""),
        )
        from academic_mcp.embeddings import EmbedderUnavailable, resolve_embedder
        with pytest.raises(EmbedderUnavailable, match="OPENAI_API_KEY"):
            resolve_embedder(provider="openai")

    def test_gemini_without_key_raises(self, monkeypatch):
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="gemini", model="gemini-embedding-001", gemini_key=""),
        )
        from academic_mcp.embeddings import EmbedderUnavailable, resolve_embedder
        with pytest.raises(EmbedderUnavailable, match="GEMINI_API_KEY"):
            resolve_embedder(provider="gemini")


# ---------------------------------------------------------------------------
# Embedder.encode — dim inference
# ---------------------------------------------------------------------------

class TestEmbedderEncode:
    def test_encode_infers_dim(self, monkeypatch):
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="local", model=""),
        )
        with patch("academic_mcp.embeddings._local_encoder") as enc_factory:
            enc_factory.return_value = (
                lambda texts: [[float(i)] * 384 for i, _ in enumerate(texts)],
                None,
            )
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder(provider="local", model="all-MiniLM-L6-v2")
        assert emb.dim is None  # not yet known
        vecs = emb.encode(["hello world"])
        assert emb.dim == 384
        assert len(vecs) == 1
        assert len(vecs[0]) == 384

    def test_encode_empty_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="local", model=""),
        )
        with patch("academic_mcp.embeddings._local_encoder") as enc_factory:
            enc_factory.return_value = (lambda texts: [], None)
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder(provider="local", model="all-MiniLM-L6-v2")
        assert emb.encode([]) == []

    def test_encode_query_uses_query_callable_when_set(self, monkeypatch):
        """encode_query() should use the query encoder, not the doc encoder."""
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="local", model=""),
        )
        doc_calls: list = []
        qry_calls: list = []

        def _doc_enc(texts):
            doc_calls.extend(texts)
            return [[0.1] * 768 for _ in texts]

        def _qry_enc(texts):
            qry_calls.extend(texts)
            return [[0.9] * 768 for _ in texts]

        with patch("academic_mcp.embeddings._local_encoder") as enc_factory:
            enc_factory.return_value = (_doc_enc, _qry_enc)
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder(provider="local", model="nomic-ai/nomic-embed-text-v1.5")

        emb.encode(["some document"])
        emb.encode_query(["my query"])

        assert doc_calls == ["some document"]
        assert qry_calls == ["my query"]

    def test_encode_query_falls_back_to_encode_when_no_query_fn(self, monkeypatch):
        """encode_query() should fall back to _encode when _encode_query is None."""
        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(provider="local", model=""),
        )
        calls: list = []

        def _enc(texts):
            calls.extend(texts)
            return [[0.1] * 384 for _ in texts]

        with patch("academic_mcp.embeddings._local_encoder") as enc_factory:
            enc_factory.return_value = (_enc, None)
            from academic_mcp.embeddings import resolve_embedder
            emb = resolve_embedder(provider="local", model="all-MiniLM-L6-v2")

        emb.encode_query(["test query"])
        assert calls == ["test query"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_config(
    provider: str = "local",
    model: str = "",
    openai_key: str = "",
    gemini_key: str = "",
    openai_base_url: str = "",
):
    cfg = MagicMock()
    cfg.semantic_provider = provider
    cfg.semantic_model = model
    cfg.openai_api_key = openai_key
    cfg.gemini_api_key = gemini_key
    cfg.openai_base_url = openai_base_url
    return cfg


def _make_mock_sentence_transformer():
    m = MagicMock()
    m.encode.return_value = [[0.1] * 384]
    return m


# ---------------------------------------------------------------------------
# OpenAI encoder — base URL and Qwen3 behaviour
# ---------------------------------------------------------------------------

class TestOpenAIEncoderQwen3:
    """Tests for Qwen3-specific and custom-base-URL behaviour in _openai_encoder."""

    def _fake_async_post(self, vecs):
        """Return a factory that creates a fake httpx.AsyncClient patching the class.

        The encoder now uses AsyncClient internally (requests are issued
        concurrently via asyncio.gather).  EOS tokens are appended server-side
        by llama.cpp's tokenizer config; the client does NOT add them.
        """
        captured = {}

        class _FakeResp:
            status_code = 200
            def json(self_inner):
                return {"data": [{"embedding": v} for v in vecs]}

        class _FakeAsyncClient:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, *, headers, json):
                captured["url"] = url
                captured["inputs"] = json.get("input", [])
                return _FakeResp()

        return _FakeAsyncClient, captured

    def test_openai_encoder_uses_custom_base_url(self, monkeypatch):
        """When OPENAI_BASE_URL is set, the endpoint should use that base."""
        import academic_mcp.embeddings as emb_mod

        fake_client, captured = self._fake_async_post([[0.1] * 1024])

        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(
                provider="openai",
                model="qwen3-embedding-0.6b",
                openai_key="sk-test",
                openai_base_url="http://127.0.0.1:8080/v1",
            ),
        )
        monkeypatch.setattr(emb_mod.httpx, "AsyncClient", fake_client)

        doc_enc, _ = emb_mod._openai_encoder("qwen3-embedding-0.6b")
        doc_enc(["hello"])

        assert captured["url"] == "http://127.0.0.1:8080/v1/embeddings"

    def test_openai_encoder_localhost_no_key_uses_sk_noop(self, monkeypatch):
        """When base URL is localhost and key is absent, sk-noop should be used."""
        import academic_mcp.embeddings as emb_mod

        fake_client, _ = self._fake_async_post([[0.1] * 1024])

        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(
                provider="openai",
                model="text-embedding",
                openai_key="",  # no key
                openai_base_url="http://127.0.0.1:8080/v1",
            ),
        )
        monkeypatch.setattr(emb_mod.httpx, "AsyncClient", fake_client)

        # Should not raise EmbedderUnavailable
        doc_enc, _ = emb_mod._openai_encoder("text-embedding")
        doc_enc(["test"])  # should not raise

    def test_qwen3_doc_inputs_passed_verbatim(self, monkeypatch):
        """Document inputs for Qwen3 should be sent as-is.

        EOS token is appended server-side by llama.cpp's tokenizer config;
        the client must NOT add <|endoftext|> (double-EOS produces warnings
        and slightly off-distribution inputs).
        """
        import academic_mcp.embeddings as emb_mod

        fake_client, captured = self._fake_async_post([[0.1, 0.2, 0.3]])

        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(
                provider="openai",
                model="qwen3-embedding-0.6b",
                openai_key="sk-test",
                openai_base_url="http://127.0.0.1:8080/v1",
            ),
        )
        monkeypatch.setattr(emb_mod.httpx, "AsyncClient", fake_client)

        doc_enc, _ = emb_mod._openai_encoder("qwen3-embedding-0.6b")
        doc_enc(["hello world"])

        assert captured["inputs"] == ["hello world"]

    def test_qwen3_query_has_instruction_prefix(self, monkeypatch):
        """Query inputs for Qwen3 models should be wrapped with the instruction prefix.

        EOS is still handled server-side; we only check the Instruct: prefix.
        """
        import academic_mcp.embeddings as emb_mod

        fake_client, captured = self._fake_async_post([[0.1, 0.2, 0.3]])

        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(
                provider="openai",
                model="qwen3-embedding-0.6b",
                openai_key="sk-test",
                openai_base_url="http://127.0.0.1:8080/v1",
            ),
        )
        monkeypatch.setattr(emb_mod.httpx, "AsyncClient", fake_client)

        _, query_enc = emb_mod._openai_encoder("qwen3-embedding-0.6b")
        assert query_enc is not None
        query_enc(["algorithmic bias"])

        sent = captured["inputs"][0]
        assert "Instruct:" in sent
        assert "algorithmic bias" in sent
        assert "<|endoftext|>" not in sent  # server appends EOS, not the client

    def test_qwen3_vectors_are_normalized(self, monkeypatch):
        """Raw response vectors for Qwen3 should be L2-normalised."""
        import academic_mcp.embeddings as emb_mod

        # [3.0, 4.0] has L2 norm = 5.0 → normalised = [0.6, 0.8]
        fake_client, _ = self._fake_async_post([[3.0, 4.0]])

        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(
                provider="openai",
                model="qwen3-embedding-0.6b",
                openai_key="sk-test",
                openai_base_url="http://127.0.0.1:8080/v1",
            ),
        )
        monkeypatch.setattr(emb_mod.httpx, "AsyncClient", fake_client)

        doc_enc, _ = emb_mod._openai_encoder("qwen3-embedding-0.6b")
        vecs = doc_enc(["test"])

        import math
        assert len(vecs) == 1
        assert math.isclose(vecs[0][0], 0.6, abs_tol=1e-5)
        assert math.isclose(vecs[0][1], 0.8, abs_tol=1e-5)

    def test_non_qwen3_openai_no_instruction_no_endoftext(self, monkeypatch):
        """Standard OpenAI models should NOT get Qwen3 wrappers."""
        import academic_mcp.embeddings as emb_mod

        fake_client, captured = self._fake_async_post([[0.1] * 1536])

        monkeypatch.setattr(
            "academic_mcp.embeddings.config",
            _mock_config(
                provider="openai",
                model="text-embedding-3-small",
                openai_key="sk-test",
            ),
        )
        monkeypatch.setattr(emb_mod.httpx, "AsyncClient", fake_client)

        doc_enc, query_enc = emb_mod._openai_encoder("text-embedding-3-small")
        doc_enc(["hello world"])

        assert query_enc is None
        assert "<|endoftext|>" not in captured["inputs"][0]
        assert "Instruct:" not in captured["inputs"][0]
