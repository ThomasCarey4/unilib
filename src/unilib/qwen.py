"""Qwen3 embedder and reranker. Heavy imports stay lazy.

The settings match the 2026-10-31 benchmark (eval report addendum 3). Change any of
them only with a new eval.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .config import model_cache_dir
from .rerank import GuardedReranker

EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
RERANK_MODEL = "Qwen/Qwen3-Reranker-0.6B"
# One task instruction for query embeddings and the reranker (the model cards recommend
# a task-specific one).
INSTRUCT = "Given a question about university lecture material, retrieve the lecture slide passages that answer it"
RERANK_MAX_TOKENS = 2048  # whole prompt; reranker inputs are ~1500 chars + title, so this rarely truncates

_PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the '
           'Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


class QwenEmbedder:
    model = EMBED_MODEL
    dim = 1024

    def __init__(self, cache_dir: Path | None = None):
        from sentence_transformers import SentenceTransformer  # slow import; keep lazy

        self._m = SentenceTransformer(self.model, device="cpu",
                                      cache_folder=str(cache_dir or model_cache_dir()))
        self._lock = threading.Lock()  # HF fast tokenizers are not safe for concurrent use

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        with self._lock:
            out = self._m.encode(texts, batch_size=16, normalize_embeddings=True, convert_to_numpy=True)
        return np.asarray(out, dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        with self._lock:
            out = self._m.encode([text], prompt=f"Instruct: {INSTRUCT}\nQuery:",
                                 normalize_embeddings=True, convert_to_numpy=True)
        return np.asarray(out[0], dtype=np.float32)


def rerank_prompt(query: str, doc: str) -> str:
    return f"<Instruct>: {INSTRUCT}\n<Query>: {query}\n<Document>: {doc}"


def yes_probability(no_logits, yes_logits):
    """P(yes) from the two answer-token logits, as on the Qwen3-Reranker model card.

    Computed in float32. The 2026-10-31 benchmark did this in bf16, which ties scores; the eval
    was re-run with float32 (addendum 3).
    """
    import torch

    two = torch.stack([no_logits, yes_logits], dim=1).float()
    return torch.nn.functional.log_softmax(two, dim=1)[:, 1].exp()


class QwenReranker(GuardedReranker):
    """Qwen3-Reranker as a causal LM scored by P(yes), in bf16 (the 9600X has AVX512-BF16)."""

    def __init__(self, model: str = RERANK_MODEL, cache_dir: Path | None = None,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__(model, clock)
        self._cache_dir = cache_dir
        self._infer_lock = threading.Lock()  # HF fast tokenizers raise "Already borrowed" under concurrent use; serialise tokenizer + model

    def _load_model(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        cache = str(self._cache_dir or model_cache_dir())
        tok = AutoTokenizer.from_pretrained(self.model, padding_side="left", cache_dir=cache)
        lm = AutoModelForCausalLM.from_pretrained(self.model, dtype=torch.bfloat16, cache_dir=cache).eval()
        return {"tok": tok, "lm": lm, "torch": torch,
                "yes": tok.convert_tokens_to_ids("yes"), "no": tok.convert_tokens_to_ids("no"),
                "pre": tok.encode(_PREFIX, add_special_tokens=False),
                "suf": tok.encode(_SUFFIX, add_special_tokens=False)}

    def _score(self, query: str, texts: list[str]) -> list[float]:
        m = self._model
        tok, torch = m["tok"], m["torch"]
        with self._infer_lock:
            enc = tok([rerank_prompt(query, t) for t in texts], padding=False, truncation="longest_first",
                      return_attention_mask=False, max_length=RERANK_MAX_TOKENS - len(m["pre"]) - len(m["suf"]))
            enc["input_ids"] = [m["pre"] + ids + m["suf"] for ids in enc["input_ids"]]
            batch = tok.pad(enc, padding=True, return_tensors="pt")
            with torch.inference_mode():
                logits = m["lm"](**batch).logits[:, -1, :]
            return yes_probability(logits[:, m["no"]], logits[:, m["yes"]]).tolist()
