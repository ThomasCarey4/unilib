"""Markdown documents (type D docs/specs): sections by heading, cited as `path §N`."""
import asyncio

import pytest

from helpers import FakeEmbedder
from unilib import server
from unilib.index import index_library
from unilib.markdown import MarkdownError, anchor, frontmatter, section_at, sections
from unilib.organize import build_name, parse_name
from unilib.search import search

DOC = """---
title: Compression settings
source_url: https://docs.example.org/compression/settings
version: TimescaleDB 2.22
---
Intro paragraph before any heading.

# Compression

## Order by

If `compress_orderby` is not set, it is derived.

```sql
# not a heading inside a code fence
ALTER TABLE t SET (timescaledb.compress_orderby = 'ts DESC');
```

## Segment by

Choose low-cardinality columns.

#### Deep heading stays inside its section
text under h4
"""


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "D01_Compression-Settings.md"
    p.write_text(DOC)
    return p


def test_frontmatter_parsed_and_stripped(doc):
    meta, body = frontmatter(doc.read_text())
    assert meta["source_url"].endswith("/settings") and meta["version"] == "TimescaleDB 2.22"
    assert body.lstrip().startswith("Intro paragraph")


def test_no_frontmatter(tmp_path):
    assert frontmatter("# A\ntext") == ({}, "# A\ntext")


def test_bad_frontmatter_raises():
    with pytest.raises(MarkdownError):
        frontmatter("---\n: [\n---\nbody")


def test_sections_split_on_h1_to_h3(doc):
    secs = sections(doc)
    assert [s.num for s in secs] == [1, 2, 3]
    assert secs[0].heading == "" and "Intro paragraph" in secs[0].text
    assert secs[1].heading == "Compression › Order by"
    assert secs[1].text.startswith("Compression › Order by\n")
    assert "# not a heading inside a code fence" in secs[1].text
    assert secs[2].heading == "Compression › Segment by"
    assert "Deep heading" in secs[2].text and "text under h4" in secs[2].text  # h4 stays in its section
    # "# Compression" has no body of its own: no section, but its line is kept in the next one
    assert "\nCompression\n" in secs[1].text


def test_anchor_is_github_style():
    assert anchor("Order by") == "order-by"
    assert anchor("`compress_orderby` & friends!") == "compress_orderby--friends"


def test_section_at(doc):
    assert section_at(doc, 2).heading.endswith("Order by")
    with pytest.raises(ValueError, match="out of range"):
        section_at(doc, 9)


def test_naming_allows_markdown():
    assert build_name("D", 1, "Compression settings", ".md") == "D01_Compression-Settings.md"
    assert parse_name("D01_Compression-Settings.md") == ("D", 1, "Compression Settings")
    with pytest.raises(ValueError):
        build_name("L", 1, "x", ".md")      # lectures are PDFs
    with pytest.raises(ValueError):
        build_name("D", 1, "x", ".ipynb")


@pytest.fixture
def eng(tmp_path, monkeypatch, doc):
    root = tmp_path / "Eng"
    (root / "TSDB").mkdir(parents=True)
    (root / "modules.toml").write_text('[[module]]\ncode = "TSDB"\nname = "TimescaleDB"\nfolder = "TSDB"\n')
    doc.rename(root / "TSDB" / doc.name)
    monkeypatch.setenv("UNILIB_ROOT", str(root))
    index_library(root, FakeEmbedder(), log=lambda s: None)
    return root


def test_markdown_indexed_and_cited_by_section(eng):
    hits = search(eng, FakeEmbedder(), "compress_orderby derived")
    assert hits and hits[0].kind == "md"
    assert hits[0].cite() == "TSDB/D01_Compression-Settings.md §2 [TimescaleDB 2.22]"


def test_get_page_markdown_section(eng, monkeypatch):
    monkeypatch.setattr(server, "_embedder", FakeEmbedder())
    out = asyncio.run(server.mcp.call_tool("get_page", {"path": "TSDB/D01_Compression-Settings.md", "page": 2}))
    text = out.content[0].text
    assert text.startswith("TSDB/D01_Compression-Settings.md §2")
    assert "derived" in text
    assert "https://docs.example.org/compression/settings#order-by" in text
    assert len(out.content) == 1  # no image for markdown


def test_list_library_counts_sections(eng):
    text = asyncio.run(server.mcp.call_tool("list_library", {})).content[0].text
    assert "D01_Compression-Settings.md · 3 sections" in text


def test_ingest_peek_and_place_markdown(lib, doc, monkeypatch):
    import shutil
    from unilib import ingest_tools
    shutil.copy(doc, lib / "_ingest" / "settings.md")
    peeked = asyncio.run(server.mcp.call_tool("ingest_peek", {"file": "settings.md"})).content[0].text
    assert "(3 sections)" in peeked and "source_url: https://docs.example.org" in peeked
    out = asyncio.run(server.mcp.call_tool("ingest_place", {"file": "settings.md", "module": "MATH3001",
                                                          "type_num": "D01", "title": "Compression settings"}))
    assert "D01_Compression-Settings.md" in out.content[0].text
    assert (lib / "MATH3001_Scientific-Computation" / "D01_Compression-Settings.md").is_file()
