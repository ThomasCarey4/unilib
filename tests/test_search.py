import pytest

from helpers import FakeEmbedder, KRR, SCI, add_papers, make_bib, make_notebook, make_pdf, make_report_bib
from unilib.config import module_by_code
from unilib.index import index_library
from unilib.organize import Part, split
from unilib.search import IndexMismatchError, _fts_query, _snippet, search


@pytest.fixture
def indexed(lib):
    make_pdf(lib / SCI / "L03_Newton-Methods.pdf", [
        "Newton iteration for nonlinear systems uses the Jacobian matrix.",
        "Armijo line search: backtrack until sufficient decrease.",
    ])
    make_pdf(lib / KRR / "L02_Propositional-Logic.pdf", [
        "Resolution is a refutation-complete inference rule for propositional logic.",
    ])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    return lib


def test_exact_term_ranks_first(indexed):
    hits = search(indexed, FakeEmbedder(), "Armijo")
    assert hits[0].path == f"{SCI}/L03_Newton-Methods.pdf"
    assert hits[0].page == 2
    assert "Armijo" in hits[0].snippet
    assert hits[0].cite() == f"{SCI}/L03_Newton-Methods.pdf p.2"


def test_module_filter(indexed):
    hits = search(indexed, FakeEmbedder(), "Jacobian resolution", module="COMP3002")
    assert hits and all(h.module == "COMP3002" for h in hits)


def test_one_hit_per_page_and_k(indexed):
    hits = search(indexed, FakeEmbedder(), "the", k=2)
    assert len(hits) <= 2
    assert len({(h.path, h.page) for h in hits}) == len(hits)


def test_split_hits_report_original_page(lib):
    src = make_pdf(lib / "_ingest" / "all.pdf", ["intro", "intro 2", "unification algorithm", "end"])
    split(lib, src, module_by_code(lib, "COMP3002"), [Part(1, 2, "L", 1, "Intro"), Part(3, 4, "L", 2, "Unify")])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    hit = search(lib, FakeEmbedder(), "unification")[0]
    assert (hit.page, hit.original_page) == (1, 3)
    assert hit.cite() == f"{KRR}/L02_Unify.pdf p.1 (orig all.pdf p.3)"


def test_punctuation_only_query_does_not_crash(indexed):
    assert isinstance(search(indexed, FakeEmbedder(), "?!"), list)


def test_fts_query_filters_stopwords():
    assert _fts_query("a newton") == '"newton"'
    assert _fts_query("the") == '"the"'
    assert _fts_query("?!") == ""


def test_snippet_anchors_on_longest_whole_word():
    text = "intro " + "filler " * 80 + "the jacobian appears here " + "tail " * 80
    snip = _snippet(text, "the jacobian")
    assert "jacobian appears" in snip and snip.startswith("…")
    # "at" must not match inside "matrix"; anchors on the whole word near the end
    text = "matrix " * 100 + "look at this"
    assert "look at this" in _snippet(text, "at")


def test_two_chunks_on_one_page_give_one_hit(lib):
    words = [f"w{i}" for i in range(700)]
    for i in (10, 350, 650):
        words[i] = "quasinewton"
    make_pdf(lib / SCI / "N01_Notes.pdf", [" ".join(words)])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    hits = search(lib, FakeEmbedder(), "quasinewton", k=10)
    assert [(h.path, h.page) for h in hits] == [(f"{SCI}/N01_Notes.pdf", 1)]


def test_new_pdf_found_after_reindex_in_same_process(indexed):
    assert all("Broyden" not in h.snippet for h in search(indexed, FakeEmbedder(), "broyden"))
    make_pdf(indexed / SCI / "L04_Broyden.pdf", ["Broyden update for quasi-Newton methods."])
    index_library(indexed, FakeEmbedder(), log=lambda s: None)
    hits = search(indexed, FakeEmbedder(), "broyden")
    assert hits[0].path == f"{SCI}/L04_Broyden.pdf"


def test_k_is_clamped(indexed):
    assert len(search(indexed, FakeEmbedder(), "newton", k=0)) == 1
    assert len(search(indexed, FakeEmbedder(), "newton", k=-5)) == 1
    assert len(search(indexed, FakeEmbedder(), "newton", k=1000)) <= 50


class ExplodingEmbedder(FakeEmbedder):
    def embed_query(self, text):
        raise AssertionError("must not embed an empty query")


def test_empty_query_returns_nothing(indexed):
    assert search(indexed, ExplodingEmbedder(), "") == []
    assert search(indexed, ExplodingEmbedder(), "  ?! ") == []


class WideEmbedder(FakeEmbedder):
    dim = 32


def test_dimension_mismatch_raises(indexed):
    with pytest.raises(IndexMismatchError, match="unilib index --full"):
        search(indexed, WideEmbedder(), "newton")


@pytest.mark.slow
def test_paraphrase_with_real_model(indexed):
    from unilib.qwen import QwenEmbedder
    e = QwenEmbedder()
    index_library(indexed, e, full=True, log=lambda s: None)
    hits = search(indexed, e, "how to pick a step length that guarantees enough reduction", k=1)
    assert (hits[0].path, hits[0].page) == (f"{SCI}/L03_Newton-Methods.pdf", 2)


class OtherModelEmbedder(FakeEmbedder):
    model = "other"


def test_model_mismatch_raises(indexed):
    with pytest.raises(IndexMismatchError, match="unilib index --full"):
        search(indexed, OtherModelEmbedder(), "newton")


def test_citation_for_semicolon_original(lib):
    src = make_pdf(lib / "_ingest" / "KRR_All; final.pdf",
                   ["intro slide", "unique zebra resolution", "third"])
    split(lib, src, module_by_code(lib, "COMP3002"), [Part(2, 3, "L", 1, "Logic")])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    hits = search(lib, FakeEmbedder(), "zebra")
    assert hits[0].cite() == f"{KRR}/L01_Logic.pdf p.1 (orig KRR_All; final.pdf p.2)"


NB_CELLS = [
    ("markdown", "# Line search demo"),
    ("code", "import numpy as np"),
    ("markdown", "## Backtracking"),
    ("code", "alpha = 0.5 * alpha  # zorblax shrink"),
    ("markdown", "Wolfe conditions summary"),
]


def test_notebook_hit_cites_cell(lib):
    make_notebook(lib / SCI / "E09_Demo.ipynb", NB_CELLS)
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    hits = search(lib, FakeEmbedder(), "zorblax")
    assert hits[0].kind == "ipynb"
    assert hits[0].cite() == f"{SCI}/E09_Demo.ipynb cell 3"


def test_search_works_on_pre_migration_index(indexed):
    # an index built before notebook support has no docs.kind column until `unilib index` migrates it
    import sqlite3

    from unilib.config import db_path
    conn = sqlite3.connect(db_path(indexed))
    conn.execute("ALTER TABLE docs DROP COLUMN kind")
    conn.commit()
    conn.close()
    hit = search(indexed, FakeEmbedder(), "Armijo")[0]
    assert (hit.page, hit.kind) == (2, "pdf")


def test_ascii_notation_query_finds_greek_slide(lib):
    make_pdf(lib / SCI / "L07_Newton.pdf", [
        "Newton step for x: solve the linear system J(xk)δk = −F(xk) for the update",
        "Python code: J = jacobian(F) evaluated at x, then F(x) is printed",
    ])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    hits = search(lib, FakeEmbedder(), "J(x) delta = -F(x)", k=2)
    assert (hits[0].path, hits[0].page) == (f"{SCI}/L07_Newton.pdf", 1)
    assert "J(xk)δk = −F(xk)" in hits[0].snippet  # snippet shows the original text


def test_greek_query_finds_greek_slide(lib):
    make_pdf(lib / SCI / "L09_Ls.pdf", ["merit function ϕ(λ) for the step", "lambda = 0.5 in the code"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    assert search(lib, FakeEmbedder(), "phi lambda", k=1)[0].page == 1
    assert search(lib, FakeEmbedder(), "ϕ(λ)", k=1)[0].page == 1


def test_split_diaeresis_matches_ascii_query(lib):
    make_pdf(lib / SCI / "L12_Ode.pdf", ["filler page", "Picard–Lindel¨of theorem: existence and uniqueness"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    for q in ("Lindelof", "Lindelöf"):
        assert search(lib, FakeEmbedder(), q, k=1)[0].page == 2


def test_snippet_anchors_on_folded_text():
    text = "intro " + "filler " * 80 + "we solve J(xk)δk = −F(xk) here " + "tail " * 80
    snip = _snippet(text, "delta")
    assert snip.startswith("…") and "J(xk)δk = −F(xk)" in snip


def test_symbol_only_query_is_searched(lib):
    make_pdf(lib / SCI / "L10_Gd.pdf", ["filler page", "descend along the negative gradient ∇ϕ"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    assert search(lib, FakeEmbedder(), "∇", k=1)[0].page == 2


def test_snippet_offsets_survive_combining_marks_and_expansions():
    # decomposed é (2 chars -> 1 folded) and δ (1 char -> 7 folded) precede the anchor
    prefix = "e\u0301 δ " * 40
    text = prefix + "zebra crossing ahead " + "tail " * 80
    snip = _snippet(text, "zebra")
    assert snip.startswith("…") and snip[1:].index("zebra") == 100  # width // 3 before the anchor
    assert "e\u0301 δ" in snip  # original text, not folded


@pytest.fixture
def with_papers(lib, tmp_path):
    z = tmp_path / "zotero"
    a = make_pdf(z / "a.pdf", ["Scaled dot-product attention mechanism"])
    b = make_pdf(z / "b.pdf", ["Residual shortcut attention connections"])
    make_report_bib(tmp_path / "latex" / "cw1" / "refs.bib", [{"key": "he2016", "title": "ResNet"}])
    bib = make_bib(z / "uni.bib", [
        {"key": "vaswani2017", "title": "Attention Is All You Need", "file": str(a)},
        {"key": "he2016", "title": "Deep Residual Learning", "file": str(b)}])
    add_papers(lib, bib, f"{tmp_path}/latex/*/refs.bib")
    make_pdf(lib / SCI / "L01_Attention.pdf", ["attention in lectures"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    return lib


def test_citekey_filter(with_papers):
    hits = search(with_papers, FakeEmbedder(), "attention", citekey="he2016", reranker=None)
    assert {h.citekey for h in hits} == {"he2016"}
    assert hits[0].cite() == "@he2016 p.1"
    assert hits[0].module == "PAPERS"


def test_report_filter(with_papers):
    hits = search(with_papers, FakeEmbedder(), "attention", report="cw1", reranker=None)
    assert [h.citekey for h in hits] == ["he2016"]


def test_papers_module_filter_and_unfiltered_mix(with_papers):
    assert {h.module for h in search(with_papers, FakeEmbedder(), "attention", module="papers", reranker=None)} == {"PAPERS"}
    assert {h.module for h in search(with_papers, FakeEmbedder(), "attention", reranker=None)} == {"PAPERS", "MATH3001"}


def test_slide_citation_unchanged(with_papers):
    (h,) = search(with_papers, FakeEmbedder(), "lectures", module="MATH3001", reranker=None)
    assert h.citekey is None and h.cite() == f"{SCI}/L01_Attention.pdf p.1"


@pytest.fixture
def with_book(indexed):
    src = make_pdf(indexed / "_ingest" / "book.pdf", ["Newton iteration converges quadratically near a simple root."])
    split(indexed, src, module_by_code(indexed, "MATH3001"), [Part(1, 1, "B", 1, "Burden Ch2 Roots")])
    index_library(indexed, FakeEmbedder(), log=lambda s: None)
    return indexed


BOOK = f"{SCI}/B01_Burden-Ch2-Roots.pdf"


def test_books_excluded_by_default(with_book):
    assert BOOK not in {h.path for h in search(with_book, FakeEmbedder(), "Newton iteration quadratically")}
    assert BOOK not in {h.path for h in search(with_book, FakeEmbedder(), "Newton iteration quadratically",
                                               module="MATH3001")}


def test_books_flag_includes_books(with_book):
    hits = search(with_book, FakeEmbedder(), "Newton iteration quadratically", books=True)
    assert BOOK in {h.path for h in hits}
    assert BOOK in {h.path for h in search(with_book, FakeEmbedder(), "quadratically", module="MATH3001",
                                           books=True)}


def test_no_books_keeps_unfiltered_sql(indexed, monkeypatch):
    import unilib.search as search_mod
    seen = []
    real = search_mod._doc_filter
    monkeypatch.setattr(search_mod, "_doc_filter", lambda *a, **kw: seen.append(real(*a, **kw)) or seen[-1])
    search(indexed, FakeEmbedder(), "Newton")
    assert seen == [("", [])]
