import time

import pytest

from helpers import KRR, SCI, FakeEmbedder, make_pdf
from unilib import search as search_mod
from unilib.index import index_library
from unilib.rerank import RETRY_AFTER_S, GuardedReranker
from unilib.search import RankConfig, search

ON = RankConfig(and_first=True, rerank=True)
OFF = RankConfig(and_first=True, rerank=False)


class FakeReranker:
    """Scores a text by how often it contains `word`; records every call."""
    model = "fake-rerank"

    def __init__(self, word="preferred"):
        self.word, self.calls = word, []

    def load(self):
        pass

    def score(self, query, texts):
        self.calls.append((query, list(texts)))
        return [float(t.lower().count(self.word)) for t in texts]


class RaisingReranker:
    model = "raising"

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("no network")

    def load(self):
        pass

    def score(self, query, texts):
        raise self.exc


class LoadFailsReranker(RaisingReranker):
    def load(self):
        raise self.exc


class ShortReranker:
    model = "short"

    def load(self):
        pass

    def score(self, query, texts):
        return [1.0] * (len(texts) - 1)


class ExplodingReranker:
    model = "boom"

    def load(self):
        pass

    def score(self, query, texts):
        raise AssertionError("reranker must not be called")


@pytest.fixture
def two_pages(lib):
    # page 1 wins on keywords (armijo x3); page 2 is what the cross-encoder prefers
    make_pdf(lib / SCI / "L09_Line-Search.pdf", ["armijo armijo armijo condition", "armijo preferred slide"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    return lib


def _pages(hits):
    return [h.page for h in hits]


def test_rerank_reorders(two_pages):
    assert _pages(search(two_pages, FakeEmbedder(), "armijo", k=2, config=OFF)) == [1, 2]
    rr = FakeReranker()
    assert _pages(search(two_pages, FakeEmbedder(), "armijo", k=2, config=ON, reranker=rr)) == [2, 1]
    assert len(rr.calls) == 1 and rr.calls[0][0] == "armijo"


def test_no_rerank_without_reranker_or_switch(two_pages):
    base = _pages(search(two_pages, FakeEmbedder(), "armijo", k=2, config=OFF))
    assert _pages(search(two_pages, FakeEmbedder(), "armijo", k=2, config=ON)) == base
    assert _pages(search(two_pages, FakeEmbedder(), "armijo", k=2, config=OFF,
                         reranker=ExplodingReranker())) == base


def test_reranker_sees_title_and_original_text(lib):
    long = "δk armijo " + "x" * 3000
    make_pdf(lib / SCI / "L09_Line-Search.pdf", [long])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    rr = FakeReranker()
    hit = search(lib, FakeEmbedder(), "armijo", k=1, config=ON, reranker=rr)[0]
    (text,) = rr.calls[0][1]
    title, _, body = text.partition("\n")
    assert title == hit.title
    assert body.startswith("δk armijo")  # original, not notation-folded
    assert len(body) == 1500


def test_only_top_n_candidates_are_rescored(lib, monkeypatch):
    monkeypatch.setattr(search_mod, "RERANK_N", 2)
    make_pdf(lib / SCI / "L01_A.pdf", ["newton newton newton", "newton newton", "newton preferred"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    rr = FakeReranker()
    hits = search(lib, FakeEmbedder(), "newton", k=3, config=ON, reranker=rr)
    assert len(rr.calls[0][1]) == 2
    # page 3 is outside the top 2 fused candidates, so the reranker cannot lift it
    assert _pages(hits)[-1] == 3
    rr = FakeReranker()
    search(lib, FakeEmbedder(), "newton", k=3, config=ON, reranker=rr, rerank_n=3)
    assert len(rr.calls[0][1]) == 3


def test_rerank_respects_k_and_module(lib):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["newton jacobian", "newton preferred"])
    make_pdf(lib / KRR / "L02_Logic.pdf", ["newton preferred preferred preferred"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    rr = FakeReranker()
    hits = search(lib, FakeEmbedder(), "newton", k=1, config=ON, reranker=rr)
    assert len(hits) == 1 and hits[0].path.startswith(KRR)
    rr = FakeReranker()
    hits = search(lib, FakeEmbedder(), "newton", module="math3001", k=5, config=ON, reranker=rr)
    assert hits and all(h.module == "MATH3001" for h in hits)
    assert _pages(hits)[0] == 2
    assert all("Logic" not in t for t in rr.calls[0][1])


def test_rerank_then_collapse(lib):
    body = "Newton iteration: solve the linear system J delta = -F for the step, then update x and repeat until converged"
    make_pdf(lib / SCI / "L07_N.pdf", ["bisection newton iteration", body, body + " preferred"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    cfg = RankConfig(and_first=True, collapse=True, rerank=True)
    hits = search(lib, FakeEmbedder(), "newton iteration", k=5, config=cfg, reranker=FakeReranker())
    # page 3 reranks first and absorbs its near-identical neighbour, page 2
    assert (hits[0].page, hits[0].also_pages) == (3, (2,))
    assert _pages(hits) == [3, 1]


class _LenReranker(GuardedReranker):
    """Scores a text by its length; the first `fail_times` loads raise."""

    def __init__(self, fail_times=0, clock=time.monotonic):
        super().__init__("len", clock)
        self.fail_times, self.attempts = fail_times, 0

    def _load_model(self):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise OSError("offline")
        return object()

    def _score(self, query, texts):
        return [float(len(t)) for t in texts]


def test_guarded_reranker_loads_lazily():
    r = _LenReranker()
    assert r.attempts == 0
    assert r.score("q", []) == [] and r.attempts == 0
    assert r.score("q", ["a", "abc"]) == [1.0, 3.0]
    r.score("q", ["x"])
    assert r.attempts == 1


def test_scoring_failure_keeps_fused_order_and_records_error(two_pages):
    base = _pages(search(two_pages, FakeEmbedder(), "armijo", k=2, config=OFF))
    assert search_mod.last_rerank_error() is None
    hits = search(two_pages, FakeEmbedder(), "armijo", k=2, config=ON, reranker=RaisingReranker())
    assert _pages(hits) == base
    assert "no network" in search_mod.last_rerank_error()
    search(two_pages, FakeEmbedder(), "armijo", k=2, config=ON, reranker=FakeReranker())
    assert search_mod.last_rerank_error() is None  # reset on every search


def test_load_failure_keeps_fused_order(two_pages):
    base = _pages(search(two_pages, FakeEmbedder(), "armijo", k=2, config=OFF))
    hits = search(two_pages, FakeEmbedder(), "armijo", k=2, config=ON,
                  reranker=LoadFailsReranker(OSError("cannot download")))
    assert _pages(hits) == base and "cannot download" in search_mod.last_rerank_error()


def test_reranker_loaded_before_db_connection(two_pages, monkeypatch):
    events = []

    class R(FakeReranker):
        def load(self):
            events.append("load")

    real = search_mod.connect
    monkeypatch.setattr(search_mod, "connect", lambda *a, **k: (events.append("connect"), real(*a, **k))[1])
    search(two_pages, FakeEmbedder(), "armijo", k=2, config=ON, reranker=R())
    assert events == ["load", "connect"]


def test_score_count_mismatch_is_a_reranker_failure(two_pages):
    base = _pages(search(two_pages, FakeEmbedder(), "armijo", k=2, config=OFF))
    hits = search(two_pages, FakeEmbedder(), "armijo", k=2, config=ON, reranker=ShortReranker())
    assert _pages(hits) == base and "scores" in search_mod.last_rerank_error()


def test_guarded_reranker_backs_off_after_load_failure():
    now = [1000.0]
    r = _LenReranker(fail_times=1, clock=lambda: now[0])
    with pytest.raises(OSError):
        r.score("q", ["a"])
    for _ in range(3):  # inside the window: fail fast, no new load attempt
        now[0] += 100
        with pytest.raises(RuntimeError, match="offline"):
            r.score("q", ["a"])
        with pytest.raises(RuntimeError):
            r.load()
    assert r.attempts == 1
    now[0] = 1000.0 + RETRY_AFTER_S + 1
    assert r.score("q", ["ab"]) == [2.0]
    assert r.attempts == 2
