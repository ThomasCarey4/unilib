import json
import urllib.error

from helpers import add_papers, make_bib, make_pdf
from unilib import bibcheck
from unilib.bibcheck import check_bib, format_report
from unilib.config import load_papers

CROSSREF_OK = {"message": {"title": ["Attention Is All You Need"], "issued": {"date-parts": [[2017, 6]]},
                           "author": [{"family": "Vaswani", "given": "Ashish"}]}}
OPENALEX = {"results": [{"title": "Deep Residual Learning for Image Recognition", "publication_year": 2016,
                         "doi": "https://doi.org/10.1109/CVPR.2016.90",
                         "authorships": [{"author": {"display_name": "Kaiming He"}}]}]}


def fake_fetch(responses):
    calls = []

    def fetch(url):
        calls.append(url)
        for prefix, resp in responses.items():
            if url.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return None
    fetch.calls = calls
    return fetch


def _bib(tmp_path, entries):
    return make_bib(tmp_path / "refs.bib", entries)


def test_ok_and_mismatch(lib, tmp_path):
    bib = _bib(tmp_path, [
        {"key": "ok", "title": "Attention is all you need", "author": "Vaswani, A.", "date": "2017",
         "doi": "10.1/ok"},
        {"key": "bad", "title": "Attention is all you need", "author": "Smith, J.", "date": "2019",
         "doi": "10.1/bad"}])
    fetch = fake_fetch({bibcheck.CROSSREF: CROSSREF_OK})
    ok, bad = check_bib(load_papers(lib), bib, fetch=fetch)
    assert (ok.status, ok.notes) == ("OK", ())
    assert bad.status == "MISMATCH"
    assert any(n.startswith("year: 2019 vs 2017") for n in bad.notes)
    assert any(n.startswith("first author: Smith vs Vaswani") for n in bad.notes)


def test_doi_not_found(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "gone", "title": "X", "doi": "10.1/none"}])
    (c,) = check_bib(load_papers(lib), bib, fetch=fake_fetch({}))
    assert c.status == "NOT FOUND" and "10.1/none" in c.notes[0]


DATACITE_OK = {"data": {"attributes": {"titles": [{"title": "Attention Is All You Need"}], "publicationYear": 2017,
                                       "creators": [{"name": "Vaswani, Ashish", "familyName": "Vaswani"}],
                                       "doi": "10.48550/arxiv.1706.03762"}}}


def test_doi_falls_back_to_datacite(lib, tmp_path):  # arXiv DOIs are registered with DataCite, not Crossref
    bib = _bib(tmp_path, [{"key": "arx", "title": "Attention is all you need", "author": "Vaswani, A.",
                           "date": "2017", "doi": "10.48550/arXiv.1706.03762"},
                          {"key": "gone", "title": "X", "doi": "10.1/none"}])
    fetch = fake_fetch({bibcheck.DATACITE + "10.48550/": DATACITE_OK})
    ok, gone = check_bib(load_papers(lib), bib, fetch=fetch)
    assert (ok.status, ok.notes) == ("OK", ())
    assert gone.status == "NOT FOUND" and "Crossref or DataCite" in gone.notes[0]


def test_datacite_mismatch_and_name_only_creator(lib, tmp_path):
    reply = {"data": {"attributes": {"titles": [{"title": "Attention Is All You Need"}], "publicationYear": "2018",
                                     "creators": [{"name": "Smith, John"}]}}}
    bib = _bib(tmp_path, [{"key": "arx", "title": "Attention is all you need", "author": "Vaswani, A.",
                           "date": "2017", "doi": "10.48550/arXiv.1706.03762"}])
    (c,) = check_bib(load_papers(lib), bib, fetch=fake_fetch({bibcheck.DATACITE: reply}))
    assert c.status == "MISMATCH" and c.notes == ("year: 2017 vs 2018", "first author: Vaswani vs Smith")


def test_no_doi_uses_openalex_and_suggests_doi(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "he2016", "title": "Deep Residual Learning for Image Recognition",
                           "author": "He, Kaiming", "date": "2016"}])
    (c,) = check_bib(load_papers(lib), bib, fetch=fake_fetch({bibcheck.OPENALEX: OPENALEX}))
    assert c.status == "OK"
    assert c.notes == ("suggest doi = 10.1109/CVPR.2016.90",)


def test_no_doi_title_mismatch_is_not_found(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "x", "title": "Quantum gravity in cats"}])
    (c,) = check_bib(load_papers(lib), bib, fetch=fake_fetch({bibcheck.OPENALEX: OPENALEX}))
    assert c.status == "NOT FOUND" and "closest" in c.notes[0]


def test_online_without_doi_skipped(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "pytorch", "type": "online", "title": "PyTorch docs", "url": "https://pytorch.org"}])
    fetch = fake_fetch({})
    (c,) = check_bib(load_papers(lib), bib, fetch=fetch)
    assert c.status == "SKIPPED" and fetch.calls == []


def test_network_error_is_reported_not_raised(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "k", "title": "T", "doi": "10.1/x"}])
    (c,) = check_bib(load_papers(lib), bib, fetch=fake_fetch({bibcheck.CROSSREF: urllib.error.URLError("offline")}))
    assert c.status == "ERROR" and "offline" in c.notes[0]


def test_pdf_flag_from_papers_bib(lib, tmp_path):
    pdf = make_pdf(tmp_path / "z" / "a.pdf", ["x"])
    add_papers(lib, make_bib(tmp_path / "z" / "uni.bib", [{"key": "has", "title": "A", "file": str(pdf)},
                                                         {"key": "nofile", "title": "B"}]))
    other = make_pdf(tmp_path / "z" / "b.pdf", ["y"])
    bib = _bib(tmp_path, [{"key": "has", "type": "online", "title": "A"},
                          {"key": "nofile", "type": "online", "title": "B"},
                          {"key": "unknown", "type": "online", "title": "C"},
                          {"key": "reportonly", "type": "online", "title": "D", "file": str(other)}])
    # a key outside Uni still has a PDF when its own entry links one (report bibs export file fields too)
    assert [c.pdf for c in check_bib(load_papers(lib), bib, fetch=fake_fetch({}))] == [True, False, False, True]


def test_cache_avoids_second_fetch(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "ok", "title": "Attention Is All You Need", "doi": "10.1/ok"}])
    fetch = fake_fetch({bibcheck.CROSSREF: CROSSREF_OK})
    check_bib(load_papers(lib), bib, fetch=fetch)
    check_bib(load_papers(lib), bib, fetch=fetch)
    assert len(fetch.calls) == 1
    assert json.loads((tmp_path / "cache" / "bibcheck.json").read_text())


def test_format_report(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "gone", "title": "X", "doi": "10.1/none"}])
    text = format_report(check_bib(load_papers(lib), bib, fetch=fake_fetch({})))
    assert "NOT FOUND" in text and "@gone" in text and "1 entries" in text


def test_failed_lookup_not_cached(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "ok", "title": "Attention Is All You Need", "doi": "10.1/ok"}])
    (first,) = check_bib(load_papers(lib), bib, fetch=fake_fetch({bibcheck.CROSSREF: urllib.error.URLError("offline")}))
    assert first.status == "ERROR"
    fetch = fake_fetch({bibcheck.CROSSREF: CROSSREF_OK})
    (second,) = check_bib(load_papers(lib), bib, fetch=fetch)
    assert second.status == "OK"
    assert len(fetch.calls) == 1


def test_not_found_is_retried_next_run(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "ok", "title": "Attention Is All You Need", "doi": "10.1/ok"}])
    (first,) = check_bib(load_papers(lib), bib, fetch=fake_fetch({}))
    assert first.status == "NOT FOUND"
    fetch = fake_fetch({bibcheck.CROSSREF: CROSSREF_OK})
    (second,) = check_bib(load_papers(lib), bib, fetch=fetch)
    assert second.status == "OK"
    assert len(fetch.calls) == 1


def test_empty_openalex_result_not_cached(lib, tmp_path):
    bib = _bib(tmp_path, [{"key": "he2016", "title": "Deep Residual Learning for Image Recognition"}])
    (first,) = check_bib(load_papers(lib), bib, fetch=fake_fetch({bibcheck.OPENALEX: {"results": []}}))
    assert first.status == "NOT FOUND"
    fetch = fake_fetch({bibcheck.OPENALEX: OPENALEX})
    (second,) = check_bib(load_papers(lib), bib, fetch=fetch)
    assert second.status == "OK"
    assert len(fetch.calls) == 1
