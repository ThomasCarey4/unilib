import asyncio

import pytest

from helpers import SCI, FakeEmbedder, add_papers, make_bib, make_notebook, make_pdf
from unilib import server
from unilib.config import module_by_code
from unilib.index import index_library
from unilib.organize import Part, split


def call(name, args):
    return asyncio.run(server.mcp.call_tool(name, args))


class PreferReranker:
    model = "fake-rerank"

    def load(self):
        pass

    def score(self, query, texts):
        return [float("preferred" in t) for t in texts]


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    monkeypatch.setattr(server, "_embedder", FakeEmbedder())
    monkeypatch.setattr(server, "_reranker", PreferReranker())


def test_tools_without_index(lib):
    for name, args in (("search", {"query": "x"}), ("list_library", {})):
        text = call(name, args).content[0].text
        assert "run `unilib index`" in text or "not indexed" in text


def test_search_tool_returns_citations(lib):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["intro", "Armijo condition"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    text = call("search", {"query": "armijo", "module": "math3001"}).content[0].text
    assert text.startswith(f"[1] {SCI}/L03_Newton.pdf p.2")


def test_get_page_returns_text_and_image(lib):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["intro", "Armijo condition"])
    res = call("get_page", {"path": f"{SCI}/L03_Newton.pdf", "page": 2})
    assert res.content[0].type == "text" and "Armijo condition" in res.content[0].text
    assert res.content[1].type == "image" and res.content[1].mime_type == "image/png"


def test_get_page_rejects_bad_input(lib):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["intro"])
    assert "No such library PDF" in call("get_page", {"path": "../../etc/passwd", "page": 1}).content[0].text
    assert "out of range" in call("get_page", {"path": f"{SCI}/L03_Newton.pdf", "page": 9}).content[0].text


def test_list_library(lib):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["intro", "x"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    make_pdf(lib / SCI / "L04_BFGS.pdf", ["new"])
    text = call("list_library", {}).content[0].text
    assert "MATH3001 — Scientific Computation (sem 1)" in text
    assert "COMP3002 — Knowledge Representation & Reasoning (sem 1) · keywords: logic" in text
    assert "L03_Newton.pdf · 2 pages" in text
    assert "L04_BFGS.pdf · not indexed" in text
    assert "COMP3002" in text


def test_get_page_rejects_absolute_path(lib):
    assert "No such library PDF" in call("get_page", {"path": "/etc/hosts.pdf", "page": 1}).content[0].text


def test_get_page_rejects_symlink_escaping_library(lib, tmp_path):
    outside = make_pdf(tmp_path / "outside" / "secret.pdf", ["secret"])
    (lib / SCI / "L09_Link.pdf").symlink_to(outside)
    text = call("get_page", {"path": f"{SCI}/L09_Link.pdf", "page": 1}).content[0].text
    assert "No such library PDF" in text and "secret" not in text


def test_get_page_text_only(lib):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["intro", "Armijo condition"])
    res = call("get_page", {"path": f"{SCI}/L03_Newton.pdf", "page": 2, "image": False})
    assert len(res.content) == 1
    assert res.content[0].type == "text" and "Armijo condition" in res.content[0].text


def test_search_empty_query(lib):
    assert call("search", {"query": "  "}).content[0].text == "Empty query."


def test_search_index_mismatch_is_a_message(lib, monkeypatch):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["Armijo condition"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)

    class Narrow(FakeEmbedder):
        dim = 16

    monkeypatch.setattr(server, "_embedder", Narrow())
    text = call("search", {"query": "armijo"}).content[0].text
    assert "unilib index --full" in text


def test_search_db_error_is_a_message(lib):
    db = lib / ".index" / "library.db"
    db.parent.mkdir()
    db.write_bytes(b"")  # exists but has no tables
    text = call("search", {"query": "armijo"}).content[0].text
    assert text.startswith("Index is being built or is unavailable")


def test_list_library_unknown_module(lib):
    text = call("list_library", {"module": "COMP9999X"}).content[0].text
    assert text == "Unknown module COMP9999X; known: MATH3001, COMP3002"


def test_list_library_bad_modules_toml(lib):
    (lib / "modules.toml").write_text("[[module]\nbroken")
    assert "Invalid modules.toml" in call("list_library", {}).content[0].text
    (lib / "modules.toml").unlink()
    assert "No modules.toml" in call("list_library", {}).content[0].text


def test_list_library_shows_deleted(lib):
    f = make_pdf(lib / SCI / "L03_Newton.pdf", ["intro"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    f.unlink()
    text = call("list_library", {}).content[0].text
    assert "L03_Newton.pdf · deleted (re-run `unilib index`)" in text


NB_CELLS = [
    ("markdown", "# Line search demo"),
    ("code", "import numpy as np"),
    ("markdown", "## Backtracking"),
    ("code", "alpha = 0.5 * alpha"),
    ("markdown", "Wolfe conditions summary"),
    ("markdown", "tail"),
    ("markdown", "end"),
]


def test_get_page_notebook_text_only(lib):
    make_notebook(lib / SCI / "E09_Demo.ipynb", NB_CELLS)
    res = call("get_page", {"path": f"{SCI}/E09_Demo.ipynb", "page": 4, "image": True})
    assert len(res.content) == 1 and res.content[0].type == "text"
    text = res.content[0].text
    assert text.startswith(f"{SCI}/E09_Demo.ipynb cell 3\n") and "alpha = 0.5" in text


def test_get_page_notebook_bad_input(lib):
    make_notebook(lib / SCI / "E09_Demo.ipynb", NB_CELLS)
    res = call("get_page", {"path": f"{SCI}/E09_Demo.ipynb", "page": 99})
    assert "out of range" in res.content[0].text
    (lib / SCI / "E01_Bad.ipynb").write_text("{nope")
    res = call("get_page", {"path": f"{SCI}/E01_Bad.ipynb", "page": 1})
    assert "not a valid notebook" in res.content[0].text


def test_list_library_shows_notebook_cells(lib):
    make_notebook(lib / SCI / "E09_Demo.ipynb", NB_CELLS)
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    assert "E09_Demo.ipynb · 7 cells" in call("list_library", {}).content[0].text


def test_search_tool_shows_collapsed_builds(lib, monkeypatch):
    from unilib import search as search_mod
    monkeypatch.setattr(search_mod, "DEFAULT_RANK", search_mod.RankConfig(collapse=True))
    body = "Newton iteration solve J delta = -F then update x"
    make_pdf(lib / SCI / "L07_N.pdf", [body, body + " ."])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    text = call("search", {"query": "Newton iteration"}).content[0].text
    assert text.startswith(f"[1] {SCI}/L07_N.pdf p.1 (also p.2)")


@pytest.mark.parametrize("rerank, first", [(True, 2), (False, 1)])
def test_search_tool_reranks_per_default(lib, monkeypatch, rerank, first):
    from unilib import search as search_mod
    monkeypatch.setattr(search_mod, "DEFAULT_RANK", search_mod.RankConfig(and_first=True, rerank=rerank))
    make_pdf(lib / SCI / "L09_Ls.pdf", ["armijo armijo armijo condition", "armijo preferred"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    text = call("search", {"query": "armijo"}).content[0].text
    assert text.startswith(f"[1] {SCI}/L09_Ls.pdf p.{first}")


def test_reranker_is_lazy_and_built_once(monkeypatch):
    from unilib import search as search_mod
    built = []

    class Fake:
        def __init__(self):
            built.append(self)

    monkeypatch.setattr(server, "_reranker", None)
    monkeypatch.setattr(server, "make_reranker", Fake)
    monkeypatch.setattr(search_mod, "DEFAULT_RANK", search_mod.RankConfig(rerank=False))
    assert server._get_reranker() is None and not built
    monkeypatch.setattr(search_mod, "DEFAULT_RANK", search_mod.RankConfig(rerank=True))
    r = server._get_reranker()
    assert server._get_reranker() is r and len(built) == 1


def _indexed(lib):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["intro", "Armijo preferred condition"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)


def test_reranker_failure_adds_note(lib, monkeypatch):
    _indexed(lib)

    class Boom:
        model = "boom"

        def load(self):
            pass

        def score(self, query, texts):
            raise RuntimeError("model download failed")

    monkeypatch.setattr(server, "_reranker", Boom())
    text = call("search", {"query": "armijo"}).content[0].text
    assert text.startswith(f"[1] {SCI}/L03_Newton.pdf p.2")
    last = text.splitlines()[-1]
    assert last.startswith("(note: reranker unavailable — ") and "model download failed" in last
    assert last.endswith("; results in fused order)")


def test_no_note_when_reranker_works(lib):
    _indexed(lib)
    assert "note:" not in call("search", {"query": "armijo"}).content[0].text


def test_embedder_failure_returns_message(lib, monkeypatch):
    _indexed(lib)
    monkeypatch.setattr(server, "_embedder", None)

    def boom():
        raise OSError("offline")

    monkeypatch.setattr(server, "make_embedder", boom)
    text = call("search", {"query": "armijo"}).content[0].text
    assert "Embedding model unavailable" in text and "offline" in text and "unilib index" in text


def test_symbol_only_query_is_searched(lib):
    make_pdf(lib / SCI / "L10_Gd.pdf", ["filler page", "descend along the negative gradient ∇ϕ"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    text = call("search", {"query": "∇"}).content[0].text
    assert text != "Empty query." and "L10_Gd.pdf p.2" in text
    assert call("search", {"query": "?!"}).content[0].text == "Empty query."


def test_search_docstring_guidance():
    doc = server.search.__doc__
    assert "about 2 s" in doc and "about 7 s" in doc and "reranked" in doc and "get_page" in doc


def test_server_builds_models_once(monkeypatch):
    built = []
    monkeypatch.setattr(server, "_embedder", None)
    monkeypatch.setattr(server, "_reranker", None)
    monkeypatch.setattr(server, "make_embedder", lambda: built.append("emb") or FakeEmbedder())
    monkeypatch.setattr(server, "make_reranker", lambda: built.append("rr") or PreferReranker())
    server._get_embedder(), server._get_reranker()
    server._get_embedder(), server._get_reranker()
    assert built == ["emb", "rr"]


def _paper_lib(lib, tmp_path):
    pdf = make_pdf(tmp_path / "z" / "attn.pdf", ["Scaled dot-product attention", "Multi-head attention"])
    add_papers(lib, make_bib(tmp_path / "z" / "uni.bib", [
        {"key": "vaswani2017", "title": "Attention Is All You Need", "date": "2017", "file": str(pdf)},
        {"key": "nopdf2020", "title": "Missing", "date": "2020"}]))
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    return pdf


def test_search_tool_citekey(lib, tmp_path):
    _paper_lib(lib, tmp_path)
    text = call("search", {"query": "multi-head attention", "citekey": "vaswani2017"}).content[0].text
    assert text.startswith("[1] @vaswani2017 p.2 — PAPERS · Attention Is All You Need")


def test_get_page_by_citekey(lib, tmp_path):
    _paper_lib(lib, tmp_path)
    res = call("get_page", {"path": "@vaswani2017", "page": 2, "image": False}).content
    assert res[0].text.startswith("@vaswani2017 p.2") and "Multi-head" in res[0].text
    assert "No indexed paper" in call("get_page", {"path": "@nope", "page": 1}).content[0].text


def test_list_library_papers_section(lib, tmp_path):
    _paper_lib(lib, tmp_path)
    text = call("list_library", {"module": "PAPERS"}).content[0].text
    assert "@vaswani2017 · 2017 · Attention Is All You Need · 2 pages" in text
    assert "@nopdf2020 · 2020 · Missing · no PDF" in text
    assert "MATH3001" not in text
    assert "PAPERS" in call("list_library", {}).content[0].text


def test_central_server_has_no_local_tools():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"search", "get_page", "list_library", "ingest_list", "ingest_peek", "ingest_outline",
                     "ingest_convert", "ingest_place", "ingest_split", "ingest_unsort", "index"}


def test_list_library_paper_status_oserror(lib, tmp_path, monkeypatch):
    add_papers(lib, tmp_path / "uni.bib", f"{tmp_path}/*/refs.bib")
    def boom(root):
        raise FileNotFoundError("gone.pdf")
    monkeypatch.setattr(server, "paper_status", boom)
    text = call("list_library", {}).content[0].text
    assert "Could not read a paper PDF" in text


def _book_lib(lib):
    make_pdf(lib / SCI / "L01_Newton.pdf", ["Newton slides"])
    src = make_pdf(lib / "_ingest" / "book.pdf", ["Newton iteration converges quadratically near a simple root."])
    split(lib, src, module_by_code(lib, "MATH3001"), [Part(1, 1, "B", 1, "Burden Ch2 Roots")])
    index_library(lib, FakeEmbedder(), log=lambda s: None)


def test_search_tool_books_flag(lib):
    _book_lib(lib)
    assert "B01_Burden-Ch2-Roots.pdf" not in call("search", {"query": "quadratically"}).content[0].text
    assert "B01_Burden-Ch2-Roots.pdf" in call("search", {"query": "quadratically", "books": True}).content[0].text


def test_list_library_marks_reading_list(lib):
    _book_lib(lib)
    text = call("list_library", {"module": "MATH3001"}).content[0].text
    assert "B01_Burden-Ch2-Roots.pdf · 1 pages · reading list" in text
    assert "L01_Newton.pdf · 1 pages\n" in text + "\n" and "L01_Newton.pdf · 1 pages · reading list" not in text
