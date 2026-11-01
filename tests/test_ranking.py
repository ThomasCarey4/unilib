import numpy as np
import pytest
from helpers import SCI, make_pdf
from unilib import search as search_mod
from unilib.index import connect, index_library
from unilib.search import Hit, RankConfig, _fts_ranked, bm25_weight, search


class KeyedEmbedder:
    """Vectors chosen by keyword so tests control semantic similarity."""
    model, dim = "keyed", 4
    AXES = {"armijo": 0, "decrease": 0, "step": 1, "time": 1, "logic": 2}

    def _v(self, text):
        v = np.full(self.dim, 0.01, np.float32)
        for w, i in self.AXES.items():
            if w in text.lower():
                v[i] += 1
        return v / np.linalg.norm(v)

    def embed_documents(self, texts):
        return np.array([self._v(t) for t in texts], np.float32).reshape(len(texts), self.dim)

    def embed_query(self, text):
        # paraphrase queries "mean" Armijo even without the word
        return self._v(text + (" armijo" if "enough" in text else ""))


def test_bm25_weight_curve():
    assert bm25_weight(1) == 1.0 and bm25_weight(3) == 1.0
    assert bm25_weight(8) == 0.4 and bm25_weight(12) == 0.4
    assert 0.4 < bm25_weight(5) < 1.0


def test_long_query_semantic_beats_shared_keyword(lib):
    make_pdf(lib / SCI / "L15_Pde.pdf", ["for each time step solve the nonlinear system step step"])
    make_pdf(lib / SCI / "L09_Ls.pdf", ["Armijo condition guarantees sufficient decrease"])
    index_library(lib, KeyedEmbedder(), log=lambda s: None)
    q = "how to choose the step length so the cost decreases enough"
    top = search(lib, KeyedEmbedder(), q, k=1, config=RankConfig(length_weighting=True))[0]
    assert top.path.endswith("L09_Ls.pdf")


def test_short_exact_term_still_wins(lib):
    make_pdf(lib / SCI / "L06_Dekker.pdf", ["Dekker method combines bisection and secant"])
    make_pdf(lib / SCI / "L09_Ls.pdf", ["Armijo condition decrease"])
    index_library(lib, KeyedEmbedder(), log=lambda s: None)
    top = search(lib, KeyedEmbedder(), "Dekker", k=1, config=RankConfig(length_weighting=True))[0]
    assert top.path.endswith("L06_Dekker.pdf")


def test_and_first_falls_back_to_or(lib, monkeypatch):
    monkeypatch.setattr(search_mod, "AND_MIN_ROWS", 5)
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha beta", "alpha only", "beta only"])
    index_library(lib, KeyedEmbedder(), log=lambda s: None)
    hits = search(lib, KeyedEmbedder(), "alpha beta", k=5, config=RankConfig(and_first=True))
    assert {h.page for h in hits} >= {1, 2, 3}  # AND gave 1 row (<5) so OR was used


@pytest.mark.parametrize("min_rows, and_first, expect", [
    (5, True, {1, 2, 3}),   # AND has 1 row < 5: OR fallback
    (1, True, {1}),         # AND has 1 row >= 1: AND rows only
    (1, False, {1, 2, 3}),  # switch off: always OR
])
def test_fts_candidates_and_threshold(lib, monkeypatch, min_rows, and_first, expect):
    monkeypatch.setattr(search_mod, "AND_MIN_ROWS", min_rows)
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha beta", "alpha only", "beta only"])
    index_library(lib, KeyedEmbedder(), log=lambda s: None)
    conn = connect(lib, readonly=True)
    try:
        pages = {conn.execute("SELECT page FROM chunks WHERE id = ?", (cid,)).fetchone()[0]
                 for cid in _fts_ranked(conn, "alpha beta", "", [], 50, and_first)}
    finally:
        conn.close()
    assert pages == expect


def test_collapse_near_identical_builds(lib):
    body = "Newton iteration solve J delta = -F then update x"
    make_pdf(lib / SCI / "L07_N.pdf", [body, body + " .", "completely different slide about bisection"])
    index_library(lib, KeyedEmbedder(), log=lambda s: None)
    hits = search(lib, KeyedEmbedder(), "Newton iteration", k=5, config=RankConfig(collapse=True))
    first = hits[0]
    assert first.page == 1 and first.also_pages == (2,)
    assert first.cite().endswith("p.1 (also p.2)")
    assert not any(h.page == 2 for h in hits)
    no = search(lib, KeyedEmbedder(), "Newton iteration", k=5, config=RankConfig())
    assert {1, 2} <= {h.page for h in no}


def test_collapse_keeps_dissimilar_consecutive_pages(lib):
    make_pdf(lib / SCI / "L07_N.pdf", ["Newton iteration converges quadratically near the root",
                                       "Newton iteration needs the Jacobian matrix assembled each step"])
    index_library(lib, KeyedEmbedder(), log=lambda s: None)
    hits = search(lib, KeyedEmbedder(), "Newton iteration", k=5, config=RankConfig(collapse=True))
    assert {h.page for h in hits} == {1, 2} and all(h.also_pages == () for h in hits)


def test_cite_also_pages_runs():
    h = Hit("a.pdf", "M", "T", 8, None, None, "", 0.0, also_pages=(9,))
    assert h.cite() == "a.pdf p.8 (also p.9)"
    h = Hit("a.pdf", "M", "T", 8, None, None, "", 0.0, also_pages=(10, 9))
    assert h.cite() == "a.pdf p.8 (also p.9–10)"
    h = Hit("a.pdf", "M", "T", 8, None, None, "", 0.0, also_pages=(7, 9, 10))
    assert h.cite() == "a.pdf p.8 (also p.7, p.9–10)"
    h = Hit("n.ipynb", "M", "T", 3, None, None, "", 0.0, kind="ipynb", also_pages=(4,))
    assert h.cite() == "n.ipynb cell 3 (also cell 4)"
