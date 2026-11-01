import asyncio

import pytest

from helpers import KRR, SCI, FakeEmbedder, make_notebook, make_pdf
from unilib import server


def call(name, args=None):
    return asyncio.run(server.mcp.call_tool(name, args or {})).content


def text(name, args=None):
    return call(name, args)[0].text


@pytest.fixture(autouse=True)
def fake_models(monkeypatch):
    monkeypatch.setattr(server, "_embedder", FakeEmbedder())
    monkeypatch.setattr(server.search_mod, "DEFAULT_RANK", server.search_mod.RankConfig(and_first=True))


def test_list_peek_place_index_search(lib):
    make_pdf(lib / "_ingest" / "slides(1).pdf", ["MATH3001 Lecture 3", "Armijo condition"])
    assert "slides(1).pdf" in text("ingest_list")
    peek = call("ingest_peek", {"file": "slides(1).pdf"})
    assert "Lecture 3" in peek[0].text and peek[1].mime_type == "image/png"
    out = text("ingest_place", {"file": "slides(1).pdf", "module": "MATH3001", "type_num": "L03",
                                "title": "Newton methods"})
    assert f"placed: slides(1).pdf -> {SCI}/L03_Newton-Methods.pdf" in out
    assert (lib / SCI / "L03_Newton-Methods.pdf").is_file() and not (lib / "_ingest" / "slides(1).pdf").exists()
    assert text("index").startswith("added 1, updated 0, removed 0")
    assert f"{SCI}/L03_Newton-Methods.pdf p.2" in text("search", {"query": "armijo"})


def test_split_into_parts(lib):
    make_pdf(lib / "_ingest" / "krr.pdf", ["intro", "more intro", "logic", "resolution"])
    out = text("ingest_split", {"file": "krr.pdf", "module": "COMP3002",
                                "parts": ["1-2:L:01:Intro", "3-4:L:02:Logic"]})
    assert f"{KRR}/L01_Intro.pdf" in out and f"{KRR}/L02_Logic.pdf" in out


def test_peek_notebook_has_no_image(lib):
    make_notebook(lib / "_ingest" / "demo.ipynb", [("markdown", "# Line search demo"), ("code", "x = 1")])
    out = call("ingest_peek", {"file": "demo.ipynb"})
    assert len(out) == 1 and "Line search demo" in out[0].text


@pytest.mark.parametrize("name", ["../modules.toml", "/etc/passwd", "missing.pdf", f"../{SCI}"])
def test_files_outside_ingest_are_refused(lib, name):
    for tool, extra in (("ingest_place", {"module": "MATH3001", "type_num": "X", "title": "t"}),
                        ("ingest_peek", {}), ("ingest_convert", {})):
        assert "no file" in text(tool, {"file": name, **extra})


def test_cli_errors_are_returned(lib):
    make_pdf(lib / "_ingest" / "a.pdf", ["x"])
    out = text("ingest_place", {"file": "a.pdf", "module": "NOPE1", "type_num": "L01", "title": "t"})
    assert "unknown module" in out and "(exit 2)" in out


def test_unsort_moves_without_clobbering(lib):
    make_pdf(lib / "_ingest" / "m.pdf", ["?"])
    make_pdf(lib / "_unsorted" / "m.pdf", ["older"])
    out = text("ingest_unsort", {"file": "m.pdf"})
    assert out.startswith("moved m.pdf -> _unsorted/") and not (lib / "_ingest" / "m.pdf").exists()
    assert len(list((lib / "_unsorted").glob("*.pdf"))) == 2
