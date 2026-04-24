"""Embedding providers for the semantic index.

Three backends, selected via ``SEMANTIC_PROVIDER``:

  * ``local``   — sentence-transformers, any model id accepted by the library
                  (e.g. ``all-MiniLM-L6-v2``, ``BAAI/bge-small-en-v1.5``,
                  ``nomic-ai/nomic-embed-text-v1.5``). The model is shared
                  with :mod:`reranker` so we keep one instance per process.
  * ``openai``  — OpenAI ``/v1/embeddings``. Any model the API accepts.
  * ``gemini``  — Google Generative Language API embeddings. Any model id.

Design rules:

  * **The vector store is always local.** Cloud providers are used only
    to compute embeddings — the returned vectors are stored in the local
    Chroma index. ANN search runs locally, so queries never leave the
    machine except for the short text being embedded (one HTTPS call to
    embed the query string).
  * **Provider + model are captured per-record.** Mixing vector spaces
    breaks cosine similarity, so we tag each upserted record with the
    provider/model/dim triple. A query embedder that disagrees with the
    collection is refused with an actionable error.
  * **Graceful failure.** Missing keys or unknown providers raise
    :class:`EmbedderUnavailable` with a one-line fix; callers surface it
    to the user instead of crashing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import httpx
import numpy as np

from .config import config

logger = logging.getLogger(__name__)


class EmbedderUnavailable(RuntimeError):
    pass


_DEFAULT_MODELS = {
    "local": "all-MiniLM-L6-v2",
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-001",
}


@dataclass
class Embedder:
    provider: str
    model: str
    dim: int | None  # None until the first encode
    _encode: Callable[[list[str]], list[list[float]]]
    # Optional separate callable for query-time encoding.
    # Some models (e.g. nomic-embed-text) require different task prompts for
    # document indexing vs. query encoding.  When None, encode_query() falls
    # back to _encode so all non-task-prompt models are unaffected.
    _encode_query: Callable[[list[str]], list[list[float]]] | None = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode documents for indexing."""
        if not texts:
            return []
        vectors = self._encode(texts)
        if not vectors:
            return []
        if self.dim is None:
            self.dim = len(vectors[0])
        return vectors

    def encode_query(self, texts: list[str]) -> list[list[float]]:
        """Encode query strings.  Uses a separate prompt for models that need it."""
        if not texts:
            return []
        fn = self._encode_query if self._encode_query is not None else self._encode
        vectors = fn(texts)
        if not vectors:
            return []
        if self.dim is None:
            self.dim = len(vectors[0])
        return vectors


# ---------------------------------------------------------------------------
# Local (sentence-transformers)
# ---------------------------------------------------------------------------

_local_models: dict[str, object] = {}

# Models that need separate task prompts for documents vs. queries.
# Key: fragment that appears in the model name (case-insensitive).
# Value: (document_prompt_name, query_prompt_name)
_TASK_PROMPT_MODELS: dict[str, tuple[str, str]] = {
    "nomic-embed-text": ("document", "query"),
}
# Models that require trust_remote_code=True to load.
_TRUST_REMOTE_CODE_FRAGMENTS: tuple[str, ...] = ("nomic-embed-text",)

# Per-model batch sizes for encode().  Long-context models (nomic-bert-2048)
# have quadratic attention memory: even small batches blow up fast.
# Batch_size=2 with 2000-char docs (~1k tokens) fits in 24GB without OOM.
# Slow (~50 min for 13k items) but stable and preserves the full (limited) fulltext.
_BATCH_SIZES: dict[str, int] = {
    "nomic-embed-text": 2,
}


def _local_encoder(
    model_name: str,
) -> tuple[Callable[[list[str]], list[list[float]]], Callable[[list[str]], list[list[float]]] | None]:
    """Return (doc_encoder, query_encoder | None).

    ``query_encoder`` is non-None only for models that need a different task
    prompt at query time (e.g. nomic-embed-text-v1.5).
    """
    default = _DEFAULT_MODELS["local"]
    name_lower = model_name.lower()

    # Detect task-prompt requirement.
    task_prompts: tuple[str, str] | None = None
    for fragment, prompts in _TASK_PROMPT_MODELS.items():
        if fragment in name_lower:
            task_prompts = prompts
            break

    needs_trust_remote = any(f in name_lower for f in _TRUST_REMOTE_CODE_FRAGMENTS)

    # Load model (shared cache).
    if model_name == default and not task_prompts:
        # Reuse the reranker's already-loaded instance for the default model.
        from .reranker import _load_model
        model = _load_model()
        if model is None:
            raise EmbedderUnavailable(
                "sentence-transformers model failed to load (see logs)."
            )
    else:
        if model_name not in _local_models:
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as e:
                raise EmbedderUnavailable(
                    "sentence-transformers not installed; run `uv sync --extra semantic` "
                    "or pip install sentence-transformers."
                ) from e
            try:
                kwargs: dict = {}
                if needs_trust_remote:
                    kwargs["trust_remote_code"] = True
                _local_models[model_name] = SentenceTransformer(model_name, **kwargs)
                logger.info("Loaded local embedding model: %s", model_name)
            except Exception as e:
                raise EmbedderUnavailable(
                    f"Failed to load local model '{model_name}': {e}"
                ) from e
        model = _local_models[model_name]

    doc_prompt = task_prompts[0] if task_prompts else None
    query_prompt = task_prompts[1] if task_prompts else None

    # Batch size: small for long-context models to avoid quadratic OOM.
    batch_size = 32
    for fragment, bs in _BATCH_SIZES.items():
        if fragment in name_lower:
            batch_size = bs
            break

    def _encode_docs(texts: list[str]) -> list[list[float]]:
        kwargs: dict = {"normalize_embeddings": True, "batch_size": batch_size}
        if doc_prompt:
            kwargs["prompt_name"] = doc_prompt
        embs = model.encode(texts, **kwargs)
        return [e.tolist() for e in embs]

    if task_prompts:
        def _encode_qry(texts: list[str]) -> list[list[float]]:
            embs = model.encode(
                texts, normalize_embeddings=True,
                prompt_name=query_prompt, batch_size=batch_size,
            )
            return [e.tolist() for e in embs]

        return _encode_docs, _encode_qry

    return _encode_docs, None


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

# Fragment that identifies Qwen3-Embedding models (case-insensitive check).
_QWEN3_EMBED_FRAGMENT = "qwen3-embed"

# Instruction prepended to QUERY strings for Qwen3-Embedding models.
# Documents are NOT prefixed. This is the standard asymmetric retrieval
# pattern and gives ~1–5% retrieval lift for academic content.
_QWEN3_QUERY_INSTRUCTION = (
    "Instruct: Given a research question, retrieve passages from academic "
    "papers or book chapters that answer or support it.\nQuery: {query}"
)


def _l2_normalize(vecs: list[list[float]]) -> list[list[float]]:
    """L2-normalise a list of float vectors."""
    arr = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)  # avoid div-by-zero
    return (arr / norms).tolist()


def _openai_encoder(model_name: str) -> tuple[Callable[[list[str]], list[list[float]]], Callable[[list[str]], list[list[float]]] | None]:
    """Return (doc_encoder, query_encoder | None) for the OpenAI-compatible endpoint.

    When ``config.openai_base_url`` points to localhost/127.0.0.1, the API
    key check is relaxed — any non-empty key is accepted, and a missing key
    defaults to ``sk-noop`` (llama-server ignores the Authorization header).
    """
    base = (config.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    endpoint = f"{base}/embeddings"

    is_local = any(h in base for h in ("127.0.0.1", "localhost"))
    api_key = config.openai_api_key
    if not api_key:
        if is_local:
            api_key = "sk-noop"
        else:
            raise EmbedderUnavailable(
                "SEMANTIC_PROVIDER=openai requires OPENAI_API_KEY in your environment."
            )

    is_qwen3 = _QWEN3_EMBED_FRAGMENT in model_name.lower()

    def _encode_batch(texts: list[str]) -> list[list[float]]:
        # OpenAI accepts batches of up to ~2,000 inputs; keep well under that.
        # llama-server handles up to -b batch size; chunk at 64 client-side.
        batch = 64 if is_local else 96
        out: list[list[float]] = []
        with httpx.Client(timeout=120.0) as client:
            for i in range(0, len(texts), batch):
                chunk = texts[i : i + batch]
                resp = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": model_name, "input": chunk},
                )
                if resp.status_code != 200:
                    raise EmbedderUnavailable(
                        f"OpenAI embedding request failed: {resp.status_code} "
                        f"{resp.text[:200]}"
                    )
                data = resp.json()
                vecs = [item["embedding"] for item in data.get("data", [])]
                if is_qwen3:
                    vecs = _l2_normalize(vecs)
                out.extend(vecs)
        return out

    def _doc_encoder(texts: list[str]) -> list[list[float]]:
        if is_qwen3:
            texts = [t + "<|endoftext|>" for t in texts]
        return _encode_batch(texts)

    if is_qwen3:
        def _query_encoder(texts: list[str]) -> list[list[float]]:
            prefixed = [
                _QWEN3_QUERY_INSTRUCTION.format(query=t) + "<|endoftext|>"
                for t in texts
            ]
            return _encode_batch(prefixed)

        return _doc_encoder, _query_encoder

    return _doc_encoder, None


# ---------------------------------------------------------------------------
# Gemini (Google Generative Language API)
# ---------------------------------------------------------------------------

def _gemini_encoder(model_name: str) -> Callable[[list[str]], list[list[float]]]:
    api_key = config.gemini_api_key
    if not api_key:
        raise EmbedderUnavailable(
            "SEMANTIC_PROVIDER=gemini requires GEMINI_API_KEY in your environment."
        )

    # The batch endpoint accepts up to 100 texts per call.
    base = "https://generativelanguage.googleapis.com/v1beta"

    def _encode(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        batch = 100
        # Gemini model ids sometimes come in with or without the `models/` prefix.
        model_ref = model_name if model_name.startswith("models/") else f"models/{model_name}"
        url = f"{base}/{model_ref}:batchEmbedContents?key={api_key}"
        with httpx.Client(timeout=60.0) as client:
            for i in range(0, len(texts), batch):
                chunk = texts[i : i + batch]
                payload = {
                    "requests": [
                        {"model": model_ref, "content": {"parts": [{"text": t}]}}
                        for t in chunk
                    ]
                }
                resp = client.post(url, json=payload)
                if resp.status_code != 200:
                    raise EmbedderUnavailable(
                        f"Gemini embedding request failed: {resp.status_code} "
                        f"{resp.text[:200]}"
                    )
                data = resp.json()
                for e in data.get("embeddings", []):
                    out.append(list(e.get("values", [])))
        return out

    return _encode


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def resolve_embedder(
    provider: str | None = None,
    model: str | None = None,
) -> Embedder:
    """Build an :class:`Embedder` for the requested provider/model.

    ``None`` arguments fall back to the values in :mod:`config`, which
    themselves default to ``local`` / ``all-MiniLM-L6-v2``.
    """
    prov = (provider or config.semantic_provider or "local").lower()
    if prov not in _DEFAULT_MODELS:
        raise EmbedderUnavailable(
            f"Unknown SEMANTIC_PROVIDER '{prov}'. Expected one of: local, openai, gemini."
        )

    mdl = model or config.semantic_model or _DEFAULT_MODELS[prov]

    query_encoder: Callable[[list[str]], list[list[float]]] | None = None
    if prov == "local":
        doc_encoder, query_encoder = _local_encoder(mdl)
    elif prov == "openai":
        doc_encoder, query_encoder = _openai_encoder(mdl)
    elif prov == "gemini":
        doc_encoder = _gemini_encoder(mdl)
    else:  # pragma: no cover — guarded above
        raise EmbedderUnavailable(f"Unsupported provider: {prov}")

    return Embedder(
        provider=prov,
        model=mdl,
        dim=None,
        _encode=doc_encoder,
        _encode_query=query_encoder,
    )
