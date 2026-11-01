import shutil

import pymupdf
import pytest

from helpers import KRR, SCI, make_pdf
from unilib.config import module_by_code
from unilib.organize import (DuplicateError, Part, parse_part, parse_range, read_split_meta, read_split_skip,
                             split)


def test_parse_part():
    assert parse_part("35-80:L:02:Propositional Logic") == Part(35, 80, "L", 2, "Propositional Logic")
    assert parse_part("1-3:X::Admin: housekeeping") == Part(1, 3, "X", None, "Admin: housekeeping")
    with pytest.raises(ValueError):
        parse_part("nonsense")


def test_split_creates_parts_and_archives_original(lib):
    src = make_pdf(lib / "_ingest" / "KRR_All.pdf", [f"slide {i}" for i in range(1, 11)])
    mod = module_by_code(lib, "COMP3002")
    parts = [Part(1, 4, "L", 1, "Intro"), Part(5, 10, "L", 2, "Logic")]
    out = split(lib, src, mod, parts)
    assert [p.name for p in out] == ["L01_Intro.pdf", "L02_Logic.pdf"]
    assert (lib / KRR / "_originals" / "KRR_All.pdf").exists()
    assert not src.exists()
    with pymupdf.open(out[1]) as doc:
        assert doc.page_count == 6
        assert doc[0].get_text().strip() == "slide 5"
        assert read_split_meta(doc) == (f"{KRR}/_originals/KRR_All.pdf", 4)


def test_split_rejects_bad_ranges(lib):
    src = make_pdf(lib / "_ingest" / "d.pdf", ["a", "b", "c"])
    mod = module_by_code(lib, "COMP3002")
    with pytest.raises(ValueError):
        split(lib, src, mod, [Part(1, 2, "L", 1, "A"), Part(2, 3, "L", 2, "B")])  # overlap
    with pytest.raises(ValueError):
        split(lib, src, mod, [Part(1, 5, "L", 1, "A")])  # past end
    assert src.exists()
    assert not list((lib / KRR).glob("*.pdf"))


def test_split_bad_type_is_atomic(lib):
    src = make_pdf(lib / "_ingest" / "d.pdf", ["a", "b", "c"])
    mod = module_by_code(lib, "COMP3002")
    with pytest.raises(ValueError):
        split(lib, src, mod, [Part(1, 1, "L", 1, "A"), Part(2, 3, "Q", 2, "B")])
    assert src.exists()
    assert not list((lib / KRR).glob("*.pdf"))
    assert not list((lib / KRR).glob(".*.partial"))
    assert not (lib / KRR / "_originals").exists()


def test_split_cleans_temp_on_save_failure(lib, monkeypatch):
    src = make_pdf(lib / "_ingest" / "d.pdf", ["a", "b", "c"])
    mod = module_by_code(lib, "COMP3002")
    real = pymupdf.Document.save
    calls = []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(self, *a, **k)
    monkeypatch.setattr(pymupdf.Document, "save", flaky)
    with pytest.raises(OSError):
        split(lib, src, mod, [Part(1, 1, "L", 1, "A"), Part(2, 3, "L", 2, "B")])
    assert src.exists()
    assert not list((lib / KRR).glob("*.pdf"))
    assert not list((lib / KRR).glob(".*.partial"))


def test_parse_part_validation():
    for bad in ("1-2:L:xx:T", "1-2:Q:01:T"):
        with pytest.raises(ValueError):
            parse_part(bad)


def test_read_split_meta_unsplit(tmp_path):
    with pymupdf.open(make_pdf(tmp_path / "a.pdf", ["x"])) as doc:
        assert read_split_meta(doc) == (None, 0)


def test_split_semicolon_filename_keeps_provenance(lib):
    src = make_pdf(lib / "_ingest" / "KRR_All; final.pdf", [f"slide {i}" for i in range(1, 7)])
    out = split(lib, src, module_by_code(lib, "COMP3002"), [Part(1, 2, "L", 1, "A"), Part(3, 6, "L", 2, "B")])
    with pymupdf.open(out[1]) as doc:
        assert read_split_meta(doc) == (f"{KRR}/_originals/KRR_All; final.pdf", 2)


def test_split_archives_sibling_pptx(lib):
    src = make_pdf(lib / "_ingest" / "d.pdf", ["a", "b"])
    (lib / "_ingest" / "d.pptx").write_bytes(b"pptx")
    split(lib, src, module_by_code(lib, "COMP3002"), [Part(1, 2, "L", 1, "A")])
    assert (lib / KRR / "_originals" / "d.pptx").exists()
    assert not (lib / "_ingest" / "d.pptx").exists()


def test_split_refuses_file_already_in_library(lib):
    src = make_pdf(lib / KRR / "d.pdf", ["a", "b"])
    with pytest.raises(ValueError, match="already in the library"):
        split(lib, src, module_by_code(lib, "COMP3002"), [Part(1, 2, "L", 1, "A")])
    assert src.exists()


def test_split_duplicate_writes_nothing(lib):
    mod = module_by_code(lib, "COMP3002")
    first = make_pdf(lib / "_ingest" / "d.pdf", ["a", "b"])
    shutil.copy(first, lib / "_ingest" / "d-copy.pdf")
    split(lib, first, mod, [Part(1, 2, "L", 1, "A")])
    dup = lib / "_ingest" / "d-copy.pdf"
    (lib / "_ingest" / "d-copy.pptx").write_bytes(b"pptx")
    with pytest.raises(DuplicateError) as e:
        split(lib, dup, mod, [Part(1, 1, "L", 2, "B")])
    assert e.value.existing == lib / KRR / "_originals" / "d.pdf"
    assert not dup.exists()
    assert not (lib / KRR / "L02_B.pdf").exists()
    assert (lib / "_unsorted" / "d-copy.pptx").exists()


def test_parse_part_book():
    assert parse_part("19-64:B:01:Burden Ch1 Preliminaries") == Part(19, 64, "B", 1, "Burden Ch1 Preliminaries")


def test_parse_range():
    assert parse_range("29-31") == (29, 31)
    assert parse_range("7") == (7, 7)
    with pytest.raises(ValueError, match="page range"):
        parse_range("5-3")
    with pytest.raises(ValueError, match="page range"):
        parse_range("x")


def test_split_book_records_skips_and_archives_to_books(lib):
    src = make_pdf(lib / "_ingest" / "book.pdf", [f"page {i}" for i in range(1, 11)])
    mod = module_by_code(lib, "MATH3001")
    out = split(lib, src, mod, [Part(2, 5, "B", 1, "Burden Ch1 Intro"), Part(6, 10, "B", 2, "Burden Ch2 Roots")],
                skip=[(4, 5), (8, 8)])
    assert [p.name for p in out] == ["B01_Burden-Ch1-Intro.pdf", "B02_Burden-Ch2-Roots.pdf"]
    assert (lib / SCI / "_originals" / "books" / "book.pdf").exists()
    with pymupdf.open(out[0]) as doc:
        assert doc.page_count == 4  # skipped pages stay in the PDF
        assert read_split_meta(doc) == (f"{SCI}/_originals/books/book.pdf", 1)
        assert read_split_skip(doc) == {3, 4}
    with pymupdf.open(out[1]) as doc:
        assert read_split_skip(doc) == {3}


def test_split_without_skip_has_empty_skip_set(lib):
    src = make_pdf(lib / "_ingest" / "deck.pdf", ["a", "b"])
    (out,) = split(lib, src, module_by_code(lib, "COMP3002"), [Part(1, 2, "L", 1, "Intro")])
    with pymupdf.open(out) as doc:
        assert read_split_skip(doc) == frozenset()
        assert read_split_meta(doc) == (f"{KRR}/_originals/deck.pdf", 0)


def test_split_skip_must_lie_inside_one_part(lib):
    src = make_pdf(lib / "_ingest" / "book.pdf", ["a"] * 6)
    mod = module_by_code(lib, "MATH3001")
    with pytest.raises(ValueError, match="inside one part"):
        split(lib, src, mod, [Part(1, 3, "B", 1, "A Ch1 X"), Part(4, 6, "B", 2, "A Ch2 Y")], skip=[(3, 4)])
    assert src.exists()
    assert not list((lib / SCI).glob("*.pdf"))


def test_split_rejects_mixed_book_and_other_types(lib):
    src = make_pdf(lib / "_ingest" / "mix.pdf", ["a", "b"])
    with pytest.raises(ValueError, match="mixed"):
        split(lib, src, module_by_code(lib, "MATH3001"), [Part(1, 1, "B", 1, "A Ch1"), Part(2, 2, "L", 1, "Lec")])
    assert src.exists()


def test_book_original_counts_for_dedupe(lib):
    src = make_pdf(lib / "_ingest" / "book.pdf", ["x", "y"])
    copy = lib / "_ingest" / "copy.pdf"
    shutil.copy(src, copy)
    mod = module_by_code(lib, "MATH3001")
    split(lib, src, mod, [Part(1, 2, "B", 1, "Burden Ch1 X")])
    with pytest.raises(DuplicateError):
        split(lib, copy, mod, [Part(1, 2, "B", 2, "Burden Ch1 X")])
    assert not copy.exists()
