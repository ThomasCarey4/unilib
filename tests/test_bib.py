from pathlib import Path

from helpers import make_bib
from unilib.bib import BibEntry, clean_tex, files, parse_bib, parse_bib_text, pdf_for, surname

BBT = r"""
@comment{jabref-meta: x}
@string{foo = "bar"}
@article{vaswani2017attention,
  title = {Attention Is {{All}} You Need},
  author = {Vaswani, Ashish and Shazeer, Noam and {Google Brain}},
  date = {2017-06-12},
  doi = {10.48550/arXiv.1706.03762},
  file = {/z/storage/AB12/Vaswani - 2017.pdf}
}

@inproceedings{he2016deep,
  title = "Deep Residual Learning for Image Recognition",
  author = {He, Kaiming and Zhang, Xiangyu},
  year = 2016,
  file = {Full Text PDF:/z/storage/CD34/He\: ResNet.pdf:application/pdf;Snapshot:/z/s.html:text/html}
}
"""


def test_parse_entries_and_fields():
    a, b = parse_bib_text(BBT)
    assert (a.key, a.type) == ("vaswani2017attention", "article")
    assert a.title == "Attention Is All You Need"
    assert a.year == 2017 and b.year == 2016
    assert a.authors == ["Vaswani, Ashish", "Shazeer, Noam", "Google Brain"]
    assert a.doi == "10.48550/arXiv.1706.03762" and b.doi is None
    assert b.title == "Deep Residual Learning for Image Recognition"


def test_file_field_forms():
    a, b = parse_bib_text(BBT)
    assert files(a) == [Path("/z/storage/AB12/Vaswani - 2017.pdf")]
    assert files(b) == [Path("/z/storage/CD34/He: ResNet.pdf"), Path("/z/s.html")]


def test_pdf_for_picks_first_existing_pdf(tmp_path):
    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(b"%PDF")
    e = BibEntry("k", "article", {"file": f"{tmp_path}/missing.pdf;{tmp_path}/a.html;{pdf}"})
    assert pdf_for(e) == pdf
    assert pdf_for(BibEntry("k", "article", {})) is None


def test_clean_tex():
    assert clean_tex(r"Schr\"{o}dinger and Erd\H{o}s, Ca\~{n}on") == "Schrödinger and Erdős, Cañon"
    assert clean_tex(r"{B}ayesian \emph{inference} \& more") == "Bayesian inference & more"
    assert clean_tex("  multi\n  line  ") == "multi line"


def test_surname():
    assert surname("Vaswani, Ashish") == "Vaswani"
    assert surname("Ashish Vaswani") == "Vaswani"
    assert surname("Google Brain") == "Brain"


def test_and_inside_braces_is_not_a_separator():
    (e,) = parse_bib_text("@book{k, author = {{Smith and Sons} and Doe, J}}")
    assert e.authors == ["Smith and Sons", "Doe, J"]


def test_parse_bib_file(tmp_path):
    p = make_bib(tmp_path / "x.bib", [{"key": "k1", "title": "One"}, {"key": "k2", "type": "online", "title": "Two"}])
    assert [(e.key, e.type, e.title) for e in parse_bib(p)] == [("k1", "article", "One"), ("k2", "online", "Two")]


def test_unbalanced_entry_stops_cleanly():
    assert [e.key for e in parse_bib_text("@article{a, title={x}}\n@article{b, title={y")] == ["a"]
