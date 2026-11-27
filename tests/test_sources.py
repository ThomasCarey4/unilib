"""sources.toml: per-file version/source/licence/superseded, shown in citations and used to filter."""
import asyncio

import pytest

from helpers import FakeEmbedder, make_pdf
from unilib import server
from unilib.config import SourceMeta, load_sources, source_meta
from unilib.index import connect, index_library
from unilib.search import search

MODULES = """
[library]
title = "Engineering references"
books_default = true

[[module]]
code = "PG"
name = "PostgreSQL"
folder = "PG"

[[module]]
code = "TSDB"
name = "TimescaleDB"
folder = "TSDB"
"""

SOURCES = """
[[source]]
files = ["PG/D01_*.pdf"]
version = "PostgreSQL 16.10"
source_url = "https://www.postgresql.org/files/documentation/pdf/16/postgresql-16-A4.pdf"
licence = "PostgreSQL"
fetched = 2026-11-27

[[source]]
files = ["PG/D02_*.pdf"]
version = "PostgreSQL 15.4"
licence = "PostgreSQL"
superseded = true

[[source]]
files = ["PG/*"]
licence = "unknown"
"""


@pytest.fixture
def eng(tmp_path, monkeypatch):
    root = tmp_path / "Eng"
    for d in ("PG", "TSDB"):
        (root / d).mkdir(parents=True)
    (root / "modules.toml").write_text(MODULES)
    (root / "sources.toml").write_text(SOURCES)
    make_pdf(root / "PG" / "D01_PG16-Btree.pdf", ["btree index row size limit 2704 bytes"])
    make_pdf(root / "PG" / "D02_PG15-Btree.pdf", ["btree index row size limit 2704 bytes"])
    make_pdf(root / "PG" / "B01_Rogov-Ch1-Intro.pdf", ["btree index row size intro"])
    (root / "TSDB" / "D01_Compression.md").write_text(
        "---\nversion: TimescaleDB 2.22\nsource_url: https://docs.example.org/c\n---\n# Order by\nbtree index row size compress\n")
    monkeypatch.setenv("UNILIB_ROOT", str(root))
    index_library(root, FakeEmbedder(), log=lambda s: None)
    return root


def test_source_meta_first_match_wins(eng):
    entries = load_sources(eng)
    assert source_meta(entries, "PG/D01_PG16-Btree.pdf").version == "PostgreSQL 16.10"
    assert source_meta(entries, "PG/D02_PG15-Btree.pdf").superseded is True
    assert source_meta(entries, "PG/B01_Rogov-Ch1-Intro.pdf") == SourceMeta(licence="unknown")
    assert source_meta(entries, "TSDB/x.pdf") == SourceMeta()


def test_no_sources_file(tmp_path):
    assert load_sources(tmp_path) == []


@pytest.mark.parametrize("text, match", [
    ('[[source]]\nversion = "x"\n', "files"),
    ('[[source]]\nfiles = ["a"]\ncolour = "red"\n', "colour"),
    ('[[source]]\nfiles = ["a"]\nsuperseded = "no"\n', "superseded"),
])
def test_bad_sources_rejected(tmp_path, text, match):
    (tmp_path / "sources.toml").write_text(text)
    with pytest.raises(ValueError, match=match):
        load_sources(tmp_path)


def _hits(eng, **kw):
    return search(eng, FakeEmbedder(), "btree index row size", k=10, **kw)


def test_citation_carries_version(eng):
    cites = {h.path: h.cite() for h in _hits(eng)}
    assert cites["PG/D01_PG16-Btree.pdf"] == "PG/D01_PG16-Btree.pdf p.1 [PostgreSQL 16.10]"
    assert cites["TSDB/D01_Compression.md"] == "TSDB/D01_Compression.md §1 [TimescaleDB 2.22]"  # from frontmatter
    assert cites["PG/B01_Rogov-Ch1-Intro.pdf"] == "PG/B01_Rogov-Ch1-Intro.pdf p.1"


def test_superseded_excluded_unless_asked(eng):
    assert "PG/D02_PG15-Btree.pdf" not in {h.path for h in _hits(eng)}
    assert "PG/D02_PG15-Btree.pdf" in {h.path for h in _hits(eng, superseded=True)}


def test_module_comma_list(eng):
    assert {h.module for h in _hits(eng, module="pg,tsdb")} == {"PG", "TSDB"}
    assert {h.module for h in _hits(eng, module="TSDB")} == {"TSDB"}


def test_metadata_change_updates_without_reembedding(eng):
    (eng / "sources.toml").write_text(SOURCES.replace("PostgreSQL 16.10", "PostgreSQL 16.11"))
    logs = []
    stats = index_library(eng, FakeEmbedder(), log=logs.append)
    assert stats.updated == 0 and stats.added == 0
    conn = connect(eng, readonly=True)
    try:
        v = conn.execute("SELECT version FROM docs WHERE path = 'PG/D01_PG16-Btree.pdf'").fetchone()[0]
    finally:
        conn.close()
    assert v == "PostgreSQL 16.11"


def test_mcp_search_and_list_show_version(eng, monkeypatch):
    monkeypatch.setattr(server, "_embedder", FakeEmbedder())
    monkeypatch.setattr(server, "_reranker", None)
    out = asyncio.run(server.mcp.call_tool("search", {"query": "btree index row size", "superseded": True}))
    assert "[PostgreSQL 15.4]" in out.content[0].text
    listing = asyncio.run(server.mcp.call_tool("list_library", {"module": "PG"})).content[0].text
    assert "D01_PG16-Btree.pdf · 1 pages · PostgreSQL 16.10" in listing
    assert "D02_PG15-Btree.pdf · 1 pages · PostgreSQL 15.4 · superseded" in listing
