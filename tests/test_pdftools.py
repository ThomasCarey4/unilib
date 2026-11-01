import pymupdf
import pytest

from helpers import make_pdf
from unilib.pdftools import book_plan, outline, peek, render_page


def test_peek_shows_first_pages(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", ["MATH3001 Lecture 3", "Newton method", "Armijo", "hidden"])
    out = peek(pdf, pages=3)
    assert "a.pdf (4 pages)" in out
    assert "MATH3001 Lecture 3" in out and "Armijo" in out
    assert "hidden" not in out


def test_peek_marks_empty_page(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", [""])
    assert "[no text layer]" in peek(pdf)


def test_render_page_png(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", ["x", "y"])
    assert render_page(pdf, 2)[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(ValueError):
        render_page(pdf, 3)


def test_outline_marks_title_pages(tmp_path):
    body = "This is ordinary slide body text with several words on it."
    pdf = make_pdf(
        tmp_path / "deck.pdf",
        ["Propositional Logic", body, body, "Lecture 2: Resolution", body],
        title_pages=(0, 3),
    )
    lines = outline(pdf)
    assert lines[0] == "# deck.pdf (5 pages)"
    pages = [l for l in lines if l.startswith("p0")]
    assert pages[0].startswith("p001 *TITLE*")
    assert pages[3].startswith("p004 *TITLE*")
    assert "*TITLE*" not in pages[1]


def test_outline_includes_bookmarks(tmp_path):
    pdf = make_pdf(tmp_path / "deck.pdf", ["a", "b", "c"], toc=[[1, "Intro", 1], [1, "Logic", 3]])
    lines = outline(pdf)
    assert "## bookmarks" in lines
    assert "p003 Logic" in lines


def test_outline_ignores_midsentence_part_reference(tmp_path):
    body = "We saw in part 2 of the course that the topic 3 approach works well for many problems."
    pdf = make_pdf(tmp_path / "d.pdf", [body])
    assert "*TITLE*" not in outline(pdf)[-1]


def test_outline_regex_title_without_large_font(tmp_path):
    pdf = make_pdf(tmp_path / "d.pdf", ["Lecture 4: Unification"])
    assert outline(pdf)[-1].startswith("p001 *TITLE*")


def test_outline_baseline_robust_to_big_headings(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    for _ in range(4):
        page = doc.new_page()
        page.insert_text((50, 80), "Heading here", fontsize=20)
        for j in range(6):
            page.insert_text((50, 120 + 15 * j), "ordinary body text line number " + str(j), fontsize=11)
    page = doc.new_page()
    page.insert_text((50, 100), "Big Title", fontsize=28)
    path = tmp_path / "h.pdf"
    doc.save(path)
    doc.close()
    pages = [l for l in outline(path) if l.startswith("p0")]
    assert [("*TITLE*" in l) for l in pages] == [False] * 4 + [True]


def test_outline_bookmark_page_minus_one(tmp_path):
    pdf = make_pdf(tmp_path / "b.pdf", ["x", "y"], toc=[[1, "Dead", -1], [1, "Ok", 2]])
    lines = outline(pdf)
    assert any(l.startswith("p--- Dead") for l in lines)
    assert any(l.startswith("p002 Ok") for l in lines)


def test_peek_accepts_str(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", ["hello"])
    assert "a.pdf (1 pages)" in peek(str(pdf))


BOOK_TOC = [
    [1, "Cover", 1], [1, "Contents", 2],
    [1, "Ch 1: Mathematical   Preliminaries ", 3], [2, "1.1: Review of Calculus", 3],
    [2, "Exercise Set 1.1", 5], [2, "1.2: Round-off Errors", 7],
    [1, "Ch 2: Solutions of Equations", 8], [2, "2.1: The Bisection Method", 8],
    [2, "Exercise Set 2.1", 10],
    [1, "Bibliography", 12], [1, "Index", 13],
]


def _book(tmp_path, author=None):
    pdf = make_pdf(tmp_path / "book.pdf", [f"page {i}" for i in range(1, 15)], toc=BOOK_TOC)
    if author:
        with pymupdf.open(pdf) as doc:
            doc.set_metadata({"author": author})
            doc.save(tmp_path / "book-meta.pdf")
        return tmp_path / "book-meta.pdf"
    return pdf


def test_book_plan_chapters_skips_and_unsplit(tmp_path):
    plan = book_plan(_book(tmp_path), author="Burden")
    assert plan.parts == ["3-7:B:01:Burden Ch1 Mathematical Preliminaries",
                          "8-11:B:02:Burden Ch2 Solutions of Equations"]
    assert plan.skips == ["6-6", "11-11"]  # exercise start page kept; clamped to the chapter
    assert plan.unsplit == ["Cover (p1-1)", "Contents (p2-2)", "Bibliography (p12-12)", "Index (p13-14)"]


def test_book_plan_author_from_metadata_and_start(tmp_path):
    plan = book_plan(_book(tmp_path, author="Richard L. Burden, J. Douglas Faires and Annette M. Burden"), start=5)
    assert plan.parts[0] == "3-7:B:05:Burden Ch1 Mathematical Preliminaries"


def test_book_plan_custom_skip_pattern(tmp_path):
    assert book_plan(_book(tmp_path), author="B", skip_pattern="Nothing Matches").skips == []


def test_book_plan_without_author_metadata(tmp_path):
    assert book_plan(_book(tmp_path)).parts[0].startswith("3-7:B:01:Book Ch1 ")


def test_book_plan_strips_nul_padding(tmp_path, monkeypatch):
    real = pymupdf.Document.get_toc
    monkeypatch.setattr(pymupdf.Document, "get_toc",
                        lambda self, *a, **k: [[l, t + "\x00" * 8, p] for l, t, p in real(self, *a, **k)])
    plan = book_plan(_book(tmp_path), author="Burden")
    assert plan.parts[0] == "3-7:B:01:Burden Ch1 Mathematical Preliminaries"
    assert plan.unsplit[0] == "Cover (p1-1)"
    assert not any("\x00" in s for s in plan.parts + plan.skips + plan.unsplit)


def test_book_plan_strips_nul_from_author_metadata(tmp_path, monkeypatch):
    path = _book(tmp_path)
    real_open = pymupdf.open

    class Padded:
        def __init__(self, doc):
            self._doc = doc
            self.metadata = {"author": "Richard Burden\x00\x00\x00"}

        def __getattr__(self, name):
            return getattr(self._doc, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._doc.close()

    monkeypatch.setattr(pymupdf, "open", lambda *a, **k: Padded(real_open(*a, **k)))
    plan = book_plan(path)
    assert plan.parts[0].startswith("3-7:B:01:Burden Ch1 ")
    assert not any("\x00" in s for s in plan.parts + plan.skips + plan.unsplit)
