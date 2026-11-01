import os
import time

import pytest

import sqlite3

from helpers import FakeEmbedder, KRR, SCI, add_papers, make_bib, make_notebook, make_pdf, make_report_bib
from unilib.config import db_path, module_by_code
from unilib.index import (SCHEMA_VERSION, chunk_text, connect, get_meta, index_library, library_status,
                          paper_status, strip_references)
from unilib.notebook import cell_count, sections
from unilib.organize import Part, split


def _count(lib, sql):
    conn = connect(lib, readonly=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _quiet(_):
    pass


def test_chunk_text():
    assert chunk_text("a b c") == ["a b c"]
    assert chunk_text("") == []
    assert chunk_text(" \n\t ") == []
    assert chunk_text("  a\n\nb\tc  ") == ["a b c"]
    chunks = chunk_text(" ".join(["w"] * 700), max_words=300)
    assert [len(c.split()) for c in chunks] == [300, 300, 100]
    assert [len(c.split()) for c in chunk_text(" ".join(["w"] * 620))] == [300, 320]
    assert [len(c.split()) for c in chunk_text(" ".join(["w"] * 600))] == [300, 300]
    assert all("  " not in c for c in chunk_text("x  y\n" * 400))


def test_index_adds_docs_and_pages(lib):
    make_pdf(lib / SCI / "L03_Newton-Methods.pdf", ["Newton iteration", "Armijo line search", ""])
    stats = index_library(lib, FakeEmbedder(), log=lambda s: None)
    assert (stats.added, stats.pages) == (1, 3)
    assert _count(lib, "SELECT count(*) FROM docs WHERE module='MATH3001' AND type='L' AND num=3") == 1
    assert _count(lib, "SELECT count(*) FROM chunks WHERE no_text=1") == 1
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'armijo'") == 1


def test_index_is_incremental(lib):
    a = make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    make_pdf(lib / KRR / "L01_B.pdf", ["beta"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)

    stats = index_library(lib, FakeEmbedder(), log=lambda s: None)
    assert (stats.added, stats.updated, stats.removed, stats.unchanged) == (0, 0, 0, 2)

    make_pdf(a, ["alpha changed"])
    (lib / KRR / "L01_B.pdf").unlink()
    stats = index_library(lib, FakeEmbedder(), log=lambda s: None)
    assert (stats.updated, stats.removed, stats.unchanged) == (1, 1, 0)
    assert _count(lib, "SELECT count(*) FROM docs") == 1
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'beta'") == 0
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'changed'") == 1


def test_index_ignores_originals_and_records_split_offset(lib):
    src = make_pdf(lib / "_ingest" / "all.pdf", [f"slide {i}" for i in range(1, 7)])
    split(lib, src, module_by_code(lib, "COMP3002"), [Part(1, 3, "L", 1, "A"), Part(4, 6, "L", 2, "B")])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    assert _count(lib, "SELECT count(*) FROM docs") == 2
    assert _count(lib, "SELECT page_offset FROM docs WHERE path LIKE '%L02_B.pdf'") == 3


def test_full_reindex(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    stats = index_library(lib, FakeEmbedder(), full=True, log=lambda s: None)
    assert stats.added == 1
    assert _count(lib, "SELECT count(*) FROM chunks") == 1


def test_library_status(lib):
    a = make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    make_pdf(lib / KRR / "L01_B.pdf", ["beta"])
    future = time.time() + 60
    os.utime(a, (future, future))
    states = {s.path.split("/")[-1]: s.state for s in library_status(lib)}
    assert states == {"L01_A.pdf": "stale", "L01_B.pdf": "new"}


def test_library_status_without_db(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    assert [s.state for s in library_status(lib)] == ["new"]


def test_touch_without_change_stays_indexed(lib):
    a = make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    future = time.time() + 120
    os.utime(a, (future, future))
    assert [s.state for s in library_status(lib)] == ["stale"]
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert (stats.added, stats.updated, stats.unchanged) == (0, 0, 1)
    assert [s.state for s in library_status(lib)] == ["indexed"]
    assert _count(lib, f"SELECT count(*) FROM docs WHERE mtime = {os.stat(a).st_mtime!r}") == 1


class FailingEmbedder(FakeEmbedder):
    """Raises when embedding any text that contains `poison`."""

    def embed_documents(self, texts):
        if any("poison" in t for t in texts):
            raise RuntimeError("embedder exploded")
        return super().embed_documents(texts)


def test_embedder_error_rolls_back_only_that_doc(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    make_pdf(lib / SCI / "L02_Bad.pdf", ["poison page"])
    make_pdf(lib / KRR / "L01_C.pdf", ["gamma"])
    logs = []
    stats = index_library(lib, FailingEmbedder(), log=logs.append)
    assert (stats.added, stats.errors) == (2, 1)
    assert any(m.startswith(f"error {SCI}/L02_Bad.pdf: ") and "exploded" in m for m in logs)
    assert _count(lib, "SELECT count(*) FROM docs") == 2
    assert _count(lib, "SELECT count(*) FROM docs WHERE path LIKE '%L02_Bad.pdf'") == 0
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'poison'") == 0
    assert _count(lib, "SELECT count(*) FROM chunks WHERE doc_id NOT IN (SELECT id FROM docs)") == 0


class ShortEmbedder(FakeEmbedder):
    def embed_documents(self, texts):
        return super().embed_documents(texts)[:-1]


def test_embedder_returning_wrong_count_is_an_error(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    logs = []
    stats = index_library(lib, ShortEmbedder(), log=logs.append)
    assert (stats.added, stats.errors) == (0, 1)
    assert _count(lib, "SELECT count(*) FROM chunks") == 0


def test_write_error_rolls_back_partial_rows(lib, monkeypatch):
    import unilib.index as index_mod

    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    real = index_mod._bump_generation

    def boom(conn):  # fails after the doc's rows were inserted
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(index_mod, "_bump_generation", boom)
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert stats.errors == 1
    monkeypatch.setattr(index_mod, "_bump_generation", real)
    assert _count(lib, "SELECT count(*) FROM docs") == 0
    assert _count(lib, "SELECT count(*) FROM chunks") == 0


def test_corrupt_pdf_keeps_previous_entry(lib):
    a = make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    make_pdf(lib / SCI / "L02_B.pdf", ["beta"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    a.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 12345\n")
    logs = []
    stats = index_library(lib, FakeEmbedder(), log=logs.append)
    assert (stats.errors, stats.removed, stats.unchanged) == (1, 0, 1)
    assert any(m.startswith(f"error {SCI}/L01_A.pdf") for m in logs)
    assert _count(lib, "SELECT count(*) FROM docs WHERE path LIKE '%L01_A.pdf'") == 1
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'alpha'") == 1


class OtherModelEmbedder(FakeEmbedder):
    model = "fake2"


def test_model_change_forces_full_rebuild(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    logs = []
    stats = index_library(lib, OtherModelEmbedder(), log=logs.append)
    assert "embedding model changed → full rebuild" in logs
    assert (stats.added, stats.unchanged) == (1, 0)
    conn = connect(lib, readonly=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()
    assert (meta["model"], meta["dim"]) == ("fake2", "64")
    # same model again → incremental
    stats = index_library(lib, OtherModelEmbedder(), log=logs.append)
    assert stats.unchanged == 1


def _generation(lib):
    return int(_count(lib, "SELECT value FROM meta WHERE key = 'generation'"))


def test_generation_bumps_only_on_content_change(lib):
    a = make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    g1 = _generation(lib)
    future = time.time() + 120
    os.utime(a, (future, future))
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _generation(lib) == g1
    make_pdf(a, ["alpha changed"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _generation(lib) > g1


def test_no_text_chunks_have_no_embedding(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha", ""])
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _count(lib, "SELECT count(*) FROM chunks WHERE no_text = 1 AND embedding IS NULL") == 1
    assert _count(lib, "SELECT count(*) FROM chunks WHERE no_text = 0 AND embedding IS NOT NULL") == 1


def test_long_page_yields_several_chunks(lib):
    words = " ".join(f"w{i}" for i in range(700))
    make_pdf(lib / SCI / "N01_Notes.pdf", [words])
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert stats.pages == 1
    assert _count(lib, "SELECT count(*) FROM chunks WHERE page = 1") == 3
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'w650'") == 1


def test_module_column_is_unindexed_but_filterable(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'math3001'") == 0
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'alpha' AND module = 'MATH3001'") == 1


def test_db_uses_wal(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _count(lib, "PRAGMA journal_mode") == "wal"


def test_library_status_reports_deleted(lib):
    a = make_pdf(lib / SCI / "L01_A.pdf", ["alpha", "beta"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    a.unlink()
    [st] = library_status(lib)
    assert (st.path, st.module, st.state, st.n_pages) == (f"{SCI}/L01_A.pdf", "MATH3001", "deleted", 2)


DEMO_CELLS = [
    ("markdown", "# Line search demo"),
    ("code", "import numpy as np", [{"output_type": "stream", "name": "stdout", "text": "loaded armijo\n"}]),
    ("markdown", "## Backtracking"),
    ("code", "alpha = 0.5 * alpha"),
    ("markdown", "Wolfe conditions summary"),
]


def test_index_notebook_alongside_pdf(lib):
    make_pdf(lib / SCI / "L09_A.pdf", ["alpha"])
    nb = make_notebook(lib / SCI / "E09_Demo.ipynb", DEMO_CELLS)
    secs = sections(nb)
    assert len(secs) == 3
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert (stats.added, stats.errors) == (2, 0)
    conn = connect(lib, readonly=True)
    try:
        row = conn.execute("SELECT * FROM docs WHERE path LIKE '%E09_Demo.ipynb'").fetchone()
        assert (row["kind"], row["type"], row["num"]) == ("ipynb", "E", 9)
        assert row["n_pages"] == cell_count(nb) == 5
        assert (row["original_file"], row["page_offset"]) == (None, 0)
        pages = [r[0] for r in conn.execute("SELECT page FROM chunks WHERE doc_id = ? ORDER BY id", (row["id"],))]
        assert pages == [s.cell for s in secs] == [1, 3, 5]
        assert conn.execute("SELECT count(*) FROM chunks WHERE doc_id = ? AND (no_text = 1 OR embedding IS NULL)",
                            (row["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT kind FROM docs WHERE path LIKE '%L09_A.pdf'").fetchone()[0] == "pdf"
    finally:
        conn.close()
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'armijo'") == 1
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'wolfe'") == 1


def test_long_notebook_section_is_chunked_on_same_page(lib):
    words = " ".join(f"w{i}" for i in range(700))
    make_notebook(lib / SCI / "E01_Long.ipynb", [("markdown", "intro"), ("markdown", words)])
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _count(lib, "SELECT count(*) FROM chunks WHERE page = 2") == 3
    assert _count(lib, "SELECT count(*) FROM chunks WHERE page = 1") == 1


def test_notebook_keeps_formatting_when_short(lib):
    make_notebook(lib / SCI / "E01_Code.ipynb", [("code", "x = 1\ny = 2")])
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert "x = 1\ny = 2" in _count(lib, "SELECT text FROM chunks")


def test_corrupt_notebook_is_an_error(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    (lib / SCI / "E01_Broken.ipynb").write_text("{not json")
    logs = []
    stats = index_library(lib, FakeEmbedder(), log=logs.append)
    assert (stats.added, stats.errors) == (1, 1)
    assert any(m.startswith(f"error {SCI}/E01_Broken.ipynb") for m in logs)


def test_schema_version_mismatch_forces_rebuild(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    make_notebook(lib / SCI / "E01_Demo.ipynb", DEMO_CELLS)
    index_library(lib, FakeEmbedder(), log=_quiet)
    conn = connect(lib)
    try:
        assert get_meta(conn, "schema_version") == str(SCHEMA_VERSION) == "4"
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    finally:
        conn.close()
    logs = []
    stats = index_library(lib, FakeEmbedder(), log=logs.append)
    assert any("schema" in m for m in logs)
    assert (stats.added, stats.unchanged) == (2, 0)
    conn = connect(lib)
    try:
        assert get_meta(conn, "schema_version") == "4"
    finally:
        conn.close()
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert stats.unchanged == 2


def _make_v1_db(lib):
    """An index as written before notebooks: no docs.kind, no schema_version."""
    p = db_path(lib)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE docs(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, module TEXT NOT NULL,
            type TEXT NOT NULL, num INTEGER, title TEXT NOT NULL, sha256 TEXT NOT NULL, original_file TEXT,
            page_offset INTEGER NOT NULL DEFAULT 0, n_pages INTEGER NOT NULL, indexed_at TEXT NOT NULL,
            mtime REAL, size INTEGER);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES docs(id),
            page INTEGER NOT NULL, text TEXT NOT NULL, no_text INTEGER NOT NULL DEFAULT 0, embedding BLOB);
        CREATE INDEX chunks_doc ON chunks(doc_id);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(text, title, module UNINDEXED, tokenize='porter unicode61');
        INSERT INTO meta VALUES ('model', 'fake'), ('dim', '64'), ('generation', '5');
        INSERT INTO docs VALUES (1, 'MATH3001_Scientific-Computation/L01_Old.pdf', 'MATH3001', 'L', 1,
            'Old', 'x', NULL, 0, 1, '2026-01-01', 1.0, 1);
        INSERT INTO chunks VALUES (1, 1, 1, 'stale words', 0, NULL);
        INSERT INTO chunks_fts(rowid, text, title, module) VALUES (1, 'stale words', 'Old', 'MATH3001');
    """)
    conn.commit()
    conn.close()


def test_old_db_without_kind_is_migrated(lib):
    _make_v1_db(lib)
    # status works on the old DB before migration
    assert [s.state for s in library_status(lib)] == ["deleted"]
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    make_notebook(lib / SCI / "E01_Demo.ipynb", DEMO_CELLS)
    logs = []
    stats = index_library(lib, FakeEmbedder(), log=logs.append)
    assert any("schema" in m for m in logs)
    assert (stats.added, stats.errors) == (2, 0)
    assert _count(lib, "SELECT count(*) FROM docs") == 2
    assert _count(lib, "SELECT count(*) FROM docs WHERE kind = 'ipynb'") == 1
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'stale'") == 0
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'armijo'") == 1
    assert _count(lib, "SELECT value FROM meta WHERE key = 'schema_version'") == "4"
    assert int(_count(lib, "SELECT value FROM meta WHERE key = 'generation'")) > 5


def test_library_status_includes_notebooks(lib):
    nb = make_notebook(lib / SCI / "E01_Demo.ipynb", DEMO_CELLS)
    assert [(s.kind, s.state, s.n_pages) for s in library_status(lib)] == [("ipynb", "new", None)]
    index_library(lib, FakeEmbedder(), log=_quiet)
    [st] = library_status(lib)
    assert (st.kind, st.type, st.num, st.state, st.n_pages) == ("ipynb", "E", 1, "indexed", 5)
    future = time.time() + 60
    os.utime(nb, (future, future))
    assert [s.state for s in library_status(lib)] == ["stale"]
    nb.unlink()
    [st] = library_status(lib)
    assert (st.kind, st.state, st.n_pages) == ("ipynb", "deleted", 5)


def test_index_uppercase_extension_in_module(lib):
    make_pdf(lib / SCI / "L01_Up.PDF", ["alpha"])
    make_notebook(lib / SCI / "E01_Up.IPYNB", DEMO_CELLS)
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert (stats.added, stats.errors) == (2, 0)
    conn = connect(lib, readonly=True)
    try:
        kinds = sorted(r[0] for r in conn.execute("SELECT kind FROM docs"))
    finally:
        conn.close()
    assert kinds == ["ipynb", "pdf"]


def test_fts_stores_folded_text_and_chunks_keep_original(lib):
    make_pdf(lib / SCI / "L07_Newton-Δ.pdf", ["Newton step: solve J(xk)δk = −F(xk), Picard–Lindel¨of"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    text = _count(lib, "SELECT text FROM chunks")
    assert "δk" in text and "−F" in text  # original kept for snippets / get_page
    fts_text, fts_title = _count_row(lib, "SELECT text, title FROM chunks_fts")
    assert "delta k" in fts_text and "-F" in fts_text and "Lindelof" in fts_text
    assert "δ" not in fts_text and "Δ" not in fts_title and "Delta" in fts_title
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'delta'") == 1
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'lindelof'") == 1


def _count_row(lib, sql):
    conn = connect(lib, readonly=True)
    try:
        return tuple(conn.execute(sql).fetchone())
    finally:
        conn.close()


def test_v2_db_with_unfolded_fts_is_migrated(lib):
    make_pdf(lib / SCI / "L07_N.pdf", ["solve J(xk)δk = −F(xk)"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    # rewrite into a v2 index: FTS held the raw text, so 'delta' does not match
    conn = connect(lib)
    try:
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("INSERT INTO chunks_fts(rowid, text, title, module) SELECT c.id, c.text, d.title, d.module"
                     " FROM chunks c JOIN docs d ON d.id = c.doc_id")
    finally:
        conn.close()
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'delta'") == 0
    logs = []
    stats = index_library(lib, FakeEmbedder(), log=logs.append)
    assert any("schema version 2 → 4" in m for m in logs)
    assert (stats.added, stats.unchanged) == (1, 0)
    assert _count(lib, "SELECT value FROM meta WHERE key = 'schema_version'") == "4"
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'delta'") == 1


def _papers(lib, tmp_path, reports=False):
    """A Zotero-like layout: two paper PDFs, a uni.bib pointing at them, [papers] in modules.toml
    (zotero_dir = tmp_path/zotero; with reports, report bibs are tmp_path/latex/*/refs.bib)."""
    z = tmp_path / "zotero"
    a = make_pdf(z / "A1" / "attn.pdf", ["Attention is all you need", "Scaled dot-product attention",
                                          "Results\nReferences\n[1] Bahdanau et al. alignment",
                                          "[2] Sutskever sequence to sequence"])
    b = make_pdf(z / "B2" / "resnet.pdf", ["Deep residual learning", "Identity shortcut connections"])
    bib = make_bib(z / "uni.bib", [
        {"key": "vaswani2017", "title": "Attention Is All You Need", "author": "Vaswani, Ashish",
         "date": "2017", "file": str(a)},
        {"key": "he2016", "title": "Deep Residual Learning", "author": "He, Kaiming", "date": "2016",
         "file": str(b)},
        {"key": "nopdf2020", "title": "No PDF Here", "date": "2020"},
    ])
    add_papers(lib, bib, f"{tmp_path}/latex/*/refs.bib" if reports else None)
    return bib, a, b


def test_papers_indexed_under_papers_module(lib, tmp_path):
    _, a, _ = _papers(lib, tmp_path)
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert stats.added == 2
    assert _count(lib, "SELECT count(*) FROM docs WHERE module='PAPERS'") == 2
    conn = connect(lib, readonly=True)
    try:
        r = conn.execute("SELECT path, type, title, citekey, year, authors FROM docs WHERE citekey='vaswani2017'").fetchone()
    finally:
        conn.close()
    assert tuple(r) == ("zotero:A1/attn.pdf", "P", "Attention Is All You Need", "vaswani2017", 2017, "Vaswani, Ashish")


def test_paper_reference_list_not_indexed(lib, tmp_path):
    _papers(lib, tmp_path)
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'bahdanau OR sutskever'") == 0
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'results'") == 1


def test_strip_references():
    assert strip_references(["Intro", "Body\nReferences\n[1] x", "[2] y"]) == ["Intro", "Body\n"]
    assert strip_references(["References are discussed", "Body"]) == ["References are discussed", "Body"]
    assert strip_references(["References\n[1]"]) == ["References\n[1]"]  # page 1 is never cut
    assert strip_references(["a", "7 REFERENCES\n[1]"]) == ["a", ""]


def test_paper_removed_from_bib_is_deleted(lib, tmp_path):
    bib, a, b = _papers(lib, tmp_path)
    index_library(lib, FakeEmbedder(), log=_quiet)
    make_bib(bib, [{"key": "he2016", "title": "Deep Residual Learning", "file": str(b)}])
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert (stats.removed, stats.unchanged) == (1, 1)
    assert _count(lib, "SELECT count(*) FROM docs WHERE module='PAPERS'") == 1


def test_missing_papers_bib_keeps_indexed_papers(lib, tmp_path):
    bib, _, _ = _papers(lib, tmp_path)
    index_library(lib, FakeEmbedder(), log=_quiet)
    bib.unlink()
    logs = []
    stats = index_library(lib, FakeEmbedder(), log=logs.append)
    assert stats.removed == 0
    assert _count(lib, "SELECT count(*) FROM docs WHERE module='PAPERS'") == 2
    assert any("papers bib not found" in m for m in logs)


def test_report_tags_update_without_reembedding(lib, tmp_path):
    refs = tmp_path / "latex" / "cw1" / "refs.bib"
    make_report_bib(refs, [{"key": "he2016", "title": "Deep Residual Learning"}])
    _papers(lib, tmp_path, reports=True)
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _count(lib, "SELECT reports FROM docs WHERE citekey='he2016'") == "cw1"
    chunk_ids = _count(lib, "SELECT group_concat(id) FROM chunks")
    make_bib(refs, [{"key": "he2016", "title": "x"}, {"key": "vaswani2017", "title": "y"}])
    stats = index_library(lib, FakeEmbedder(), log=_quiet)
    assert stats.unchanged == 2
    assert _count(lib, "SELECT reports FROM docs WHERE citekey='vaswani2017'") == "cw1"
    assert _count(lib, "SELECT group_concat(id) FROM chunks") == chunk_ids


def test_report_bib_entries_are_indexed_too(lib, tmp_path):
    """BBT auto-exports are non-recursive: a paper only in Uni/<report> appears only in refs.bib."""
    extra = make_pdf(tmp_path / "zotero" / "C3" / "old.pdf", ["Backpropagation through time"])
    make_report_bib(tmp_path / "latex" / "cw1" / "refs.bib",
             [{"key": "werbos1990", "title": "BPTT", "file": str(extra)}])
    _papers(lib, tmp_path, reports=True)
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _count(lib, "SELECT reports FROM docs WHERE citekey='werbos1990'") == "cw1"


def test_library_status_ignores_papers(lib, tmp_path):
    _papers(lib, tmp_path)
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert [s.path for s in library_status(lib)] == [f"{SCI}/L01_A.pdf"]


def test_paper_status(lib, tmp_path):
    _, a, _ = _papers(lib, tmp_path)
    assert {s.citekey: s.state for s in paper_status(lib)} == {
        "vaswani2017": "new", "he2016": "new", "nopdf2020": "no pdf"}
    index_library(lib, FakeEmbedder(), log=_quiet)
    make_pdf(a, ["changed"])
    st = {s.citekey: s for s in paper_status(lib)}
    assert (st["vaswani2017"].state, st["he2016"].state, st["he2016"].n_pages) == ("stale", "indexed", 2)


def test_no_papers_section_is_unchanged_behaviour(lib):
    make_pdf(lib / SCI / "L01_A.pdf", ["alpha"])
    assert index_library(lib, FakeEmbedder(), log=_quiet).added == 1
    assert paper_status(lib) == []


def test_skipped_book_pages_not_indexed(lib):
    src = make_pdf(lib / "_ingest" / "book.pdf", [
        "Bisection brackets a root of a continuous function.",
        "Exercise: apply bisection to x^3 - 7 on [1, 2].",
        "Fixed point iteration converges when g is a contraction.",
    ])
    split(lib, src, module_by_code(lib, "MATH3001"), [Part(1, 3, "B", 1, "Burden Ch2 Roots")], skip=[(2, 2)])
    index_library(lib, FakeEmbedder(), log=_quiet)
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'exercise'") == 0
    assert _count(lib, "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'bisection'") == 1
    conn = connect(lib, readonly=True)
    try:
        rows = conn.execute("SELECT c.page, c.no_text FROM chunks c JOIN docs d ON d.id = c.doc_id"
                            " WHERE d.type = 'B' ORDER BY c.page").fetchall()
        n_pages = conn.execute("SELECT n_pages FROM docs WHERE type = 'B'").fetchone()[0]
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [(1, 0), (2, 1), (3, 0)]  # page-aligned, page 2 is a no-text row
    assert n_pages == 3
    again = index_library(lib, FakeEmbedder(), log=_quiet)
    assert (again.added, again.updated, again.removed, again.unchanged) == (0, 0, 0, 1)
