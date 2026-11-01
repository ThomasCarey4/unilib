import shutil
import sqlite3

import pytest

from helpers import KRR, SCI, FakeEmbedder, make_notebook, make_pdf
from unilib import cli


class FakeReranker:
    built: list = []

    def __init__(self, model="default-rerank"):
        self.model = model
        FakeReranker.built.append(model)

    def load(self):
        pass

    def score(self, query, texts):
        return [0.0] * len(texts)


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    monkeypatch.setattr(cli, "make_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(cli, "make_reranker", lambda: FakeReranker())
    FakeReranker.built = []


def test_peek_writes_render(lib, capsys):
    src = make_pdf(lib / "_ingest" / "s.pdf", ["MATH3001 Lecture 1"])
    assert cli.main(["peek", str(src)]) == 0
    out = capsys.readouterr().out
    assert "MATH3001 Lecture 1" in out
    assert (lib / ".index" / "peek" / "s-p1.png").exists()
    make_pdf(src, ["a", "b"])
    assert cli.main(["peek", str(src), "--render", "2"]) == 0
    assert (lib / ".index" / "peek" / "s-p2.png").exists()


def test_place_index_search_flow(lib, capsys):
    src = make_pdf(lib / "_ingest" / "slides(1).pdf", ["Armijo line search"])
    assert cli.main(["place", str(src), "math3001", "L03", "Newton methods"]) == 0
    assert (lib / SCI / "L03_Newton-Methods.pdf").exists()
    assert cli.main(["index"]) == 0
    assert cli.main(["search", "armijo"]) == 0
    assert f"{SCI}/L03_Newton-Methods.pdf p.1" in capsys.readouterr().out


def test_split_command(lib):
    src = make_pdf(lib / "_ingest" / "all.pdf", ["a", "b", "c", "d"])
    rc = cli.main(["split", str(src), "COMP3002", "--part", "1-2:L:01:Intro", "--part", "3-4:L:02:Logic"])
    assert rc == 0
    assert (lib / KRR / "L02_Logic.pdf").exists()


def test_outline_command(lib, capsys):
    src = make_pdf(lib / "_ingest" / "d.pdf", ["Lecture 1", "body"])
    assert cli.main(["outline", str(src)]) == 0
    assert "p001 *TITLE*" in capsys.readouterr().out


def test_convert_failure_moves_to_unsorted(lib, monkeypatch, capsys):
    bad = lib / "_ingest" / "x.pptx"
    bad.write_bytes(b"junk")

    def boom(path):
        raise RuntimeError("soffice failed for x.pptx: bad file")

    monkeypatch.setattr(cli, "convert_pptx", boom)
    assert cli.main(["convert", str(bad)]) == 1
    assert (lib / "_unsorted" / "x.pptx").exists()
    assert "bad file" in capsys.readouterr().err


def test_unknown_module_errors(lib, capsys):
    src = make_pdf(lib / "_ingest" / "a.pdf", ["x"])
    assert cli.main(["place", str(src), "COMP0000", "L01", "x"]) == 2
    assert "unknown module" in capsys.readouterr().err


def test_peek_junk_pdf_exits_2(lib, capsys):
    bad = lib / "_ingest" / "junk.pdf"
    bad.write_bytes(b"not a pdf at all")
    assert cli.main(["peek", str(bad)]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_sqlite_error_exits_3(lib, monkeypatch, capsys):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "index_library", locked)
    assert cli.main(["index"]) == 3
    assert "error: database busy or unavailable: database is locked" in capsys.readouterr().err


def test_convert_already_converted(lib, monkeypatch, capsys):
    pptx = lib / "_ingest" / "d.pptx"
    pptx.write_bytes(b"x")
    pdf = make_pdf(lib / "_ingest" / "d.pdf", ["a"])

    def boom(path):
        raise AssertionError("soffice must not run")

    monkeypatch.setattr(cli, "convert_pptx", boom)
    assert cli.main(["convert", str(pptx)]) == 0
    assert f"already converted: {pdf}" in capsys.readouterr().out
    assert pptx.exists() and pdf.exists()


def test_place_duplicate_prints_duplicate(lib, capsys):
    a = make_pdf(lib / "_ingest" / "a.pdf", ["same"])
    shutil.copy(a, lib / "_ingest" / "b.pdf")
    assert cli.main(["place", str(a), "MATH3001", "L01", "One"]) == 0
    assert cli.main(["place", str(lib / "_ingest" / "b.pdf"), "MATH3001", "L02", "Two"]) == 0
    assert f"duplicate: b.pdf == {SCI}/L01_One.pdf (dropped)" in capsys.readouterr().out


def test_split_duplicate_prints_duplicate(lib, capsys):
    a = make_pdf(lib / "_ingest" / "a.pdf", ["x", "y"])
    shutil.copy(a, lib / "_ingest" / "b.pdf")
    assert cli.main(["split", str(a), "COMP3002", "--part", "1-2:L:01:One"]) == 0
    capsys.readouterr()
    assert cli.main(["split", str(lib / "_ingest" / "b.pdf"), "COMP3002", "--part", "1-1:L:02:Two"]) == 0
    assert f"duplicate: b.pdf == {KRR}/_originals/a.pdf (dropped)" in capsys.readouterr().out
    assert not (lib / "_ingest" / "b.pdf").exists()


def test_index_exits_1_on_errors(lib):
    (lib / SCI / "L01_Bad.pdf").write_bytes(b"junk")
    assert cli.main(["index"]) == 1


def test_search_without_index_exits_1(lib, capsys):
    assert cli.main(["search", "anything"]) == 1
    assert "unilib index" in capsys.readouterr().err


def test_split_quoted_multiword_title(lib):
    src = make_pdf(lib / "_ingest" / "all.pdf", ["a", "b"])
    assert cli.main(["split", str(src), "COMP3002", "--part", "1-2:L:01:Propositional Logic"]) == 0
    assert (lib / KRR / "L01_Propositional-Logic.pdf").exists()


def _eval_setup(lib, tmp_path):
    make_pdf(lib / SCI / "L09_A.pdf", ["intro", "Armijo sufficient decrease"])
    assert cli.main(["index"]) == 0
    g = tmp_path / "golden.toml"
    g.write_text(f'[[query]]\nq = "armijo"\nkind = "exact"\nanswers = ["{SCI}/L09_A.pdf:2"]\n')
    return g


def test_eval_command(lib, tmp_path, capsys):
    g = _eval_setup(lib, tmp_path)
    capsys.readouterr()
    assert cli.main(["eval", "--golden", str(g)]) == 0
    assert "hit@1 1.00" in capsys.readouterr().out


def test_eval_defaults_to_library_eval_dir(lib, tmp_path, capsys):
    g = _eval_setup(lib, tmp_path)
    (lib / "eval").mkdir()
    g.rename(lib / "eval" / "golden.toml")
    capsys.readouterr()
    assert cli.main(["eval"]) == 0
    assert "hit@1 1.00" in capsys.readouterr().out


def test_eval_compare_prints_eight_configs(lib, tmp_path, capsys):
    g = _eval_setup(lib, tmp_path)
    capsys.readouterr()
    assert cli.main(["eval", "--golden", str(g), "--compare"]) == 0
    out = capsys.readouterr().out
    assert len([ln for ln in out.splitlines() if ln.startswith("A=")]) == 8


def test_eval_rerank_options(lib, tmp_path, capsys, monkeypatch):
    g = _eval_setup(lib, tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_RANK", cli.RankConfig(and_first=True))
    capsys.readouterr()
    assert cli.main(["eval", "--golden", str(g)]) == 0
    assert FakeReranker.built == []
    out = capsys.readouterr().out
    assert "hit@1 1.00" in out and "p50 " in out and "p95 " in out
    monkeypatch.setattr(cli, "DEFAULT_RANK", cli.RankConfig(and_first=True, rerank=True))
    assert cli.main(["eval", "--golden", str(g), "--no-rerank"]) == 0
    assert FakeReranker.built == []
    assert cli.main(["eval", "--golden", str(g), "--rerank-n", "5"]) == 0
    assert FakeReranker.built == ["default-rerank"]
    capsys.readouterr()
    assert cli.main(["eval", "--golden", str(g), "--compare"]) == 0
    assert FakeReranker.built == ["default-rerank", "default-rerank"]
    assert len([ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("A=") and "E=1" in ln]) == 8


def test_eval_not_indexed(lib, tmp_path, capsys):
    g = tmp_path / "g.toml"
    g.write_text('[[query]]\nq = "x"\nanswers = ["a.pdf:1"]\n')
    assert cli.main(["eval", "--golden", str(g)]) == 1


def _nb(path, n=5):
    cells = [("markdown", f"# Section {i}") for i in range(n)]
    return make_notebook(path, cells)


def test_peek_notebook(lib, capsys):
    src = _nb(lib / "_ingest" / "nb.ipynb", 5)
    assert cli.main(["peek", str(src)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# nb.ipynb (5 cells)")
    assert "Section 2" in out and "Section 3" not in out
    assert not (lib / ".index" / "peek").exists()
    assert cli.main(["peek", str(src), "--render", "2"]) == 0
    assert "not rendered" in capsys.readouterr().out


def test_place_notebook(lib):
    src = _nb(lib / "_ingest" / "nb.ipynb")
    assert cli.main(["place", str(src), "MATH3001", "E09", "Demo"]) == 0
    assert (lib / SCI / "E09_Demo.ipynb").exists()
    assert cli._type_num("E09") == ("E", 9)


def test_outline_split_notebook_pdf_only(lib, capsys):
    src = _nb(lib / "_ingest" / "nb.ipynb")
    assert cli.main(["outline", str(src)]) == 2
    assert "PDF only" in capsys.readouterr().err
    assert cli.main(["split", str(src), "MATH3001", "--part", "1-2:L:01:A"]) == 2
    assert "PDF only" in capsys.readouterr().err


def test_place_uppercase_extension_cli(lib):
    src = _nb(lib / "_ingest" / "Demo.IPYNB")
    assert cli.main(["place", str(src), "MATH3001", "E01", "Demo"]) == 0
    assert (lib / SCI / "E01_Demo.ipynb").exists()


def test_search_reranks_by_default_and_skips_with_flag(lib, capsys):
    make_pdf(lib / SCI / "L03_Newton.pdf", ["intro", "Armijo condition"])
    assert cli.main(["index"]) == 0
    capsys.readouterr()
    assert cli.main(["search", "armijo"]) == 0
    assert FakeReranker.built == ["default-rerank"]
    assert "p.2" in capsys.readouterr().out
    FakeReranker.built = []
    assert cli.main(["search", "armijo", "--no-rerank"]) == 0
    assert FakeReranker.built == []
    assert "p.2" in capsys.readouterr().out


def test_index_reports_counts(lib, capsys):
    from unilib.config import db_path
    make_pdf(lib / SCI / "L03_Newton.pdf", ["intro", "Armijo condition"])
    assert cli.main(["index"]) == 0
    assert db_path(lib).exists()
    assert cli.main(["index"]) == 0
    out = capsys.readouterr().out
    assert "added 1" in out and "added 0, updated 0, removed 0, unchanged 1" in out


def test_place_book_part(lib):
    src = make_pdf(lib / "_ingest" / "ch1.pdf", ["chapter one"])
    assert cli.main(["place", str(src), "MATH3001", "B01", "Burden Ch1 Preliminaries"]) == 0
    assert (lib / SCI / "B01_Burden-Ch1-Preliminaries.pdf").exists()


def test_split_command_with_skip(lib):
    src = make_pdf(lib / "_ingest" / "book.pdf", ["a", "b", "c"])
    assert cli.main(["split", str(src), "MATH3001", "--part", "1-3:B:01:Burden Ch1 Intro", "--skip", "2"]) == 0
    assert (lib / SCI / "B01_Burden-Ch1-Intro.pdf").exists()
    assert (lib / SCI / "_originals" / "books" / "book.pdf").exists()


def test_outline_book_prints_split_args(lib, capsys):
    toc = [[1, "Cover", 1], [1, "Ch 1: Intro", 2], [2, "Exercise Set 1.1", 3], [1, "Index", 5]]
    src = make_pdf(lib / "_ingest" / "book.pdf", ["c", "a", "b", "x", "i"], toc=toc)
    assert cli.main(["outline", str(src), "--book", "--author", "Burden"]) == 0
    out = capsys.readouterr().out
    assert "--part '2-4:B:01:Burden Ch1 Intro'" in out
    assert "--skip 4-4" in out
    assert "not split (stays in the original): Cover (p1-1)" in out


def test_search_books_flag(lib, capsys):
    src = make_pdf(lib / "_ingest" / "book.pdf", ["Newton iteration converges quadratically."])
    assert cli.main(["split", str(src), "MATH3001", "--part", "1-1:B:01:Burden Ch2 Roots"]) == 0
    assert cli.main(["index"]) == 0
    capsys.readouterr()
    assert cli.main(["search", "quadratically", "--no-rerank"]) == 0
    assert "B01_Burden-Ch2-Roots.pdf" not in capsys.readouterr().out
    assert cli.main(["search", "quadratically", "--books", "--no-rerank"]) == 0
    assert "B01_Burden-Ch2-Roots.pdf" in capsys.readouterr().out


def test_webdav_config_reads_module_folders_from_modules_toml(lib, tmp_path, capsys):
    import json
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"port": 8091, "users": [
        {"username": "u", "rules": [{"path": "/_ingest/", "permissions": "CRUD"}]}]}))
    out = tmp_path / "out.json"
    assert cli.main(["webdav-config", str(base), str(out)]) == 0
    rules = json.loads(out.read_text())["users"][0]["rules"]
    assert rules == [{"path": f"/{SCI}/", "permissions": "R"}, {"path": f"/{KRR}/", "permissions": "R"},
                     {"path": "/_ingest/", "permissions": "CRUD"}]
    assert "2 module folders" in capsys.readouterr().out
