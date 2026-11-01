"""The embedder and reranker: Qwen3 (qwen.py). torch is imported only when a model is built."""
from __future__ import annotations

from .embed import Embedder
from .rerank import Reranker


def make_embedder() -> Embedder:
    from . import qwen

    return qwen.QwenEmbedder()


def make_reranker() -> Reranker:
    """Built unloaded; the model loads on first `load`/`score`."""
    from . import qwen

    return qwen.QwenReranker()
