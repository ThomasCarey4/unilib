import sys
import types

import numpy as np
import pytest

from unilib import qwen
from unilib.rerank import RETRY_AFTER_S


class _FakeST:
    def __init__(self, model, device=None, cache_folder=None):
        self.model, self.device, self.cache_folder, self.calls = model, device, cache_folder, []

    def encode(self, texts, **kw):
        self.calls.append((list(texts), kw))
        return np.ones((len(texts), qwen.QwenEmbedder.dim), np.float32)


@pytest.fixture
def fake_st(monkeypatch):
    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)


def test_embedder_prompts_queries_not_documents(fake_st, tmp_path):
    e = qwen.QwenEmbedder(cache_dir=tmp_path)
    assert (e.model, e.dim) == ("Qwen/Qwen3-Embedding-0.6B", 1024)
    assert e._m.device == "cpu" and e._m.cache_folder == str(tmp_path)
    assert e.embed_documents(["a", "b"]).shape == (2, 1024)
    assert e.embed_query("q").shape == (1024,)
    (docs, dkw), (q, qkw) = e._m.calls
    assert "prompt" not in dkw and dkw["normalize_embeddings"] is True
    assert dkw["batch_size"] == 16
    assert q == ["q"] and qkw["prompt"] == f"Instruct: {qwen.INSTRUCT}\nQuery:"
    assert qkw["normalize_embeddings"] is True


def test_embedder_empty_documents(fake_st, tmp_path):
    assert qwen.QwenEmbedder(cache_dir=tmp_path).embed_documents([]).shape == (0, 1024)


def test_rerank_prompt_format():
    body = qwen.rerank_prompt("what is x", "Title\nx is y")
    assert body == f"<Instruct>: {qwen.INSTRUCT}\n<Query>: what is x\n<Document>: Title\nx is y"


def test_yes_probability():
    import torch
    no = torch.tensor([0.0, 0.0, 5.0])
    yes = torch.tensor([0.0, 9.0, 0.0])
    p = qwen.yes_probability(no, yes)
    assert p[0] == pytest.approx(0.5) and p[1] > 0.99 and p[2] < 0.01
    assert p.dtype == torch.float32


def test_reranker_is_lazy_and_backs_off(monkeypatch, tmp_path):
    attempts = []

    def boom(self):
        attempts.append(1)
        raise OSError("offline")

    monkeypatch.setattr(qwen.QwenReranker, "_load_model", boom)
    now = [0.0]
    r = qwen.QwenReranker(cache_dir=tmp_path, clock=lambda: now[0])
    assert r.model == "Qwen/Qwen3-Reranker-0.6B" and attempts == []
    assert r.score("q", []) == [] and attempts == []
    with pytest.raises(OSError):
        r.score("q", ["a"])
    now[0] = RETRY_AFTER_S - 1
    with pytest.raises(RuntimeError, match="offline"):
        r.load()
    assert attempts == [1]


def test_reranker_input_and_score_contract(monkeypatch, tmp_path):
    import torch

    rec = {}

    class FakeTok:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            rec["tok_load"] = (a, kw)
            return cls()

        def convert_tokens_to_ids(self, t):
            return {"yes": 1, "no": 0}[t]

        def encode(self, text, add_special_tokens=True):
            assert add_special_tokens is False
            return {qwen._PREFIX: [100], qwen._SUFFIX: [200]}[text]

        def __call__(self, texts, **kw):
            rec["call"] = (list(texts), kw)
            return {"input_ids": [[10 + i] for i in range(len(texts))]}

        def pad(self, enc, padding=True, return_tensors=None):
            rec["padded"] = [list(x) for x in enc["input_ids"]]
            assert padding is True and return_tensors == "pt"
            return {"input_ids": torch.tensor(enc["input_ids"])}

    class FakeLM:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            rec["lm_load"] = (a, kw)
            return cls()

        def eval(self):
            return self

        def __call__(self, **batch):
            n, seq = batch["input_ids"].shape
            logits = torch.zeros(n, seq, 2)
            logits[0, -1] = torch.tensor([0.0, 5.0])
            logits[1, -1] = torch.tensor([5.0, 0.0])
            return types.SimpleNamespace(logits=logits)

    fake = types.ModuleType("transformers")
    fake.AutoTokenizer, fake.AutoModelForCausalLM = FakeTok, FakeLM
    monkeypatch.setitem(sys.modules, "transformers", fake)

    r = qwen.QwenReranker(cache_dir=tmp_path)
    docs = ["d0", "d1"]
    s = r.score("q", docs)

    assert rec["tok_load"][1] == {"padding_side": "left", "cache_dir": str(tmp_path)}
    assert rec["lm_load"][1] == {"dtype": torch.bfloat16, "cache_dir": str(tmp_path)}
    texts, kw = rec["call"]
    assert texts == [qwen.rerank_prompt("q", d) for d in docs]
    assert kw == {"padding": False, "truncation": "longest_first", "return_attention_mask": False,
                  "max_length": qwen.RERANK_MAX_TOKENS - 2}
    assert rec["padded"] == [[100, 10 + i, 200] for i in range(2)]
    assert s[0] > 0.99 and s[1] < 0.01


@pytest.mark.slow
def test_real_qwen_embedder_paraphrase_closer():
    e = qwen.QwenEmbedder()
    q = e.embed_query("how to pick the step size so the error goes down")
    d = e.embed_documents(["Line search: reduce lambda until the residual decreases",
                           "The French revolution began in 1789"])
    assert float(d[0] @ q) > float(d[1] @ q)


@pytest.mark.slow
def test_real_qwen_reranker_prefers_relevant():
    r = qwen.QwenReranker()
    s = r.score("Armijo condition", ["Armijo condition: phi(x + l d) <= (1 - C l) phi(x)",
                                     "Lecture schedule and coursework deadlines"])
    assert s[0] > 0.5 > s[1]
