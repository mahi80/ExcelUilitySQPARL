"""Local text embeddings (fastembed / ONNX) for the pgvector schema index.

Lazy-loads BAAI/bge-small-en-v1.5 (384-dim, CPU, ~130MB downloaded on first use).
Kept dependency-light (ONNX, no torch). Identical copy in agent/embedder.py.
"""
from __future__ import annotations

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from fastembed import TextEmbedding
        _MODEL = TextEmbedding(MODEL_NAME)
    return _MODEL


def embed(texts: list[str]) -> list[list[float]]:
    return [[float(x) for x in v] for v in _model().embed(list(texts))]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]


def to_pgvector(vec: list[float]) -> str:
    """pgvector text input form: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
